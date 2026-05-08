import os
import yt_dlp
import uuid
import random
import subprocess
import tempfile
import re
import asyncio
from typing import Optional, Dict, Any
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, validator, HttpUrl
from starlette.background import BackgroundTasks
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/extract")

class VideoRequest(BaseModel):
    url: str
    
    @validator('url')
    def validate_url(cls, v):
        if not v or not v.strip():
            raise ValueError('URL tidak boleh kosong')
        return v.strip()

class MuxRequest(BaseModel):
    video_url: str
    audio_url: str
    resolution: str
    title: str
    
    @validator('video_url', 'audio_url')
    def validate_urls(cls, v):
        if not v or not v.strip():
            raise ValueError('URL video/audio tidak boleh kosong')
        # Validasi format URL dasar
        if not v.startswith(('http://', 'https://')):
            raise ValueError('URL harus dimulai dengan http:// atau https://')
        return v.strip()
    
    @validator('title')
    def validate_title(cls, v):
        if not v or not v.strip():
            return "video_download"
        return v.strip()

def get_ydl_opts():
    """Konfigurasi yt-dlp dengan retry dan error handling yang lebih baik"""
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    ]
    return {
        'quiet': False,  # Ubah ke False untuk debugging
        'no_warnings': False,
        'skip_download': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'socket_timeout': 30,
        'retries': 10,  # Tingkatkan retry
        'fragment_retries': 10,
        'format': 'bestvideo+bestaudio/best',
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'web', 'mweb', 'android'],
                'skip': ['hls', 'dash']  # Skip format yang kompleks
            },
            'instagram': {
                'check_headers': True
            }
        },
        'http_headers': {
            'User-Agent': random.choice(user_agents),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Site': 'none',
            'Connection': 'keep-alive'
        }
    }

def extract_video_logic(url: str) -> Dict[str, Any]:
    """Ekstraksi informasi video dengan error handling yang lebih baik"""
    target_url = url.strip()
    
    # Normalisasi URL YouTube Shorts
    if '/shorts/' in target_url:
        target_url = target_url.replace('/shorts/', '/watch?v=')
    
    # Normalisasi URL Instagram
    if 'instagram.com' in target_url:
        # Hapus query parameters yang tidak perlu
        target_url = target_url.split('?')[0]

    try:
        logger.info(f"Memulai ekstraksi untuk URL: {target_url}")
        
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(target_url, download=False)
            
            if not info:
                logger.error("Tidak ada data yang ditemukan")
                return {
                    "success": False, 
                    "message": "Tidak dapat mengekstrak informasi video. Coba lagi."
                }

            # Ambil semua format yang tersedia
            raw_formats = info.get('formats', [])
            
            if not raw_formats:
                logger.error("Tidak ada format yang tersedia")
                return {
                    "success": False,
                    "message": "Tidak ada format video yang tersedia untuk diunduh"
                }
            
            # Filter audio-only formats
            audio_only = [
                f for f in raw_formats 
                if f.get('acodec') not in [None, 'none'] 
                and (f.get('vcodec') in [None, 'none'] or f.get('resolution') == 'audio only')
            ]
            
            # Pilih audio terbaik berdasarkan bitrate
            best_audio = None
            if audio_only:
                best_audio = max(
                    audio_only, 
                    key=lambda x: (x.get('abr') or x.get('tbr') or 0)
                )
                logger.info(f"Audio terbaik: {best_audio.get('format_id')} - {best_audio.get('abr')}kbps")

            # Filter dan sortir video formats
            video_formats = [
                f for f in raw_formats 
                if f.get('height') is not None and f.get('vcodec') not in [None, 'none']
            ]
            
            mp4_formats = []
            seen_res = set()
            
            # Sortir berdasarkan kualitas (height, lalu bitrate)
            sorted_videos = sorted(
                video_formats, 
                key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), 
                reverse=True
            )
            
            for f in sorted_videos:
                h = f.get('height') or 0
                w = f.get('width') or 0
                
                if h == 0:
                    continue
                
                # Tentukan resolusi untuk portrait dan landscape
                shorter_side = min(w, h) if w > 0 and h > 0 else h
                
                # Label resolusi
                if shorter_side >= 2160: 
                    res_label = "4K"
                elif shorter_side >= 1440: 
                    res_label = "2K"
                elif shorter_side >= 1080: 
                    res_label = "1080p FHD"
                elif shorter_side >= 720: 
                    res_label = "720p HD"
                elif shorter_side >= 480: 
                    res_label = "480p"
                elif shorter_side >= 360: 
                    res_label = "360p"
                else: 
                    res_label = f"{shorter_side}p"

                # Skip duplikat resolusi
                if res_label in seen_res:
                    continue
                seen_res.add(res_label)

                # Cek apakah video sudah include audio
                has_audio = f.get('acodec') not in [None, 'none', 'unknown']
                
                # Dapatkan URL yang valid
                video_url = f.get('url')
                if not video_url:
                    logger.warning(f"Format {res_label} tidak punya URL, skip")
                    continue

                mp4_formats.append({
                    "resolution": res_label,
                    "video_url": video_url,
                    "audio_url": None if has_audio else (best_audio.get('url') if best_audio else None),
                    "needs_mux": not has_audio,
                    "height": h,
                    "width": w,
                    "real_res": f"{w}x{h}",
                    "filesize": f.get('filesize') or f.get('filesize_approx'),
                    "format_note": f.get('format_note', '')
                })

            # Format audio untuk MP3
            mp3_formats = []
            for idx, f in enumerate(audio_only[:3], 1):
                quality = "HQ" if idx == 1 else f"Quality {idx}"
                mp3_formats.append({
                    "quality": quality,
                    "audio_url": f.get('url'),
                    "bitrate": f.get('abr') or f.get('tbr')
                })

            logger.info(f"Berhasil ekstraksi: {len(mp4_formats)} format video, {len(mp3_formats)} format audio")

            return {
                "success": True,
                "title": info.get('title', 'Video'),
                "uploader": info.get('uploader') or info.get('channel') or info.get('uploader_id'),
                "duration": info.get('duration_string') or str(info.get('duration', 0)) + 's',
                "thumbnail": info.get('thumbnail'),
                "mp4_formats": mp4_formats[:12],
                "mp3_formats": mp3_formats,
                "platform": info.get('extractor_key'),
                "view_count": info.get('view_count'),
                "like_count": info.get('like_count')
            }

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.error(f"DownloadError: {error_msg}")
        
        if "Private video" in error_msg or "members-only" in error_msg:
            return {"success": False, "message": "Video ini bersifat private atau hanya untuk member."}
        elif "Video unavailable" in error_msg:
            return {"success": False, "message": "Video tidak tersedia atau telah dihapus."}
        elif "Sign in to confirm your age" in error_msg:
            return {"success": False, "message": "Video memerlukan verifikasi umur. Coba URL lain."}
        elif "content isn't available" in error_msg:
            return {"success": False, "message": "Konten Instagram private/dibatasi."}
        else:
            return {"success": False, "message": f"Gagal mengunduh: {error_msg[:150]}"}
    
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        return {
            "success": False, 
            "message": f"Error: {str(e)[:150]}"
        }

def sanitize_filename(filename: str, max_length: int = 50) -> str:
    """Bersihkan nama file dari karakter ilegal dan emoji"""
    # Hapus emoji dan karakter non-ASCII
    clean = re.sub(r'[^\x00-\x7F]+', '', filename)
    # Hapus karakter ilegal untuk nama file
    clean = re.sub(r'[\\/*?:"<>|]', '', clean)
    # Hapus whitespace berlebih
    clean = ' '.join(clean.split())
    # Potong ke panjang maksimal
    clean = clean[:max_length].strip()
    # Fallback jika kosong
    return clean if clean else "video_download"

async def download_with_retry(url: str, output_path: str, max_retries: int = 3) -> bool:
    """Download file dengan retry mechanism"""
    for attempt in range(max_retries):
        try:
            logger.info(f"Download attempt {attempt + 1}/{max_retries}: {url[:50]}...")
            
            # Gunakan curl untuk download yang lebih reliable
            cmd = [
                "curl", "-L",  # Follow redirects
                "-f",  # Fail silently on HTTP errors
                "--max-time", "120",  # Timeout 2 menit
                "--retry", "3",
                "-o", output_path,
                url
            ]
            
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), 
                timeout=150
            )
            
            if process.returncode == 0 and os.path.exists(output_path):
                file_size = os.path.getsize(output_path)
                if file_size > 0:
                    logger.info(f"Download berhasil: {file_size} bytes")
                    return True
                else:
                    logger.warning("File downloaded tapi ukurannya 0")
            
            logger.warning(f"Download gagal (attempt {attempt + 1}): {stderr.decode()[:200]}")
            
        except asyncio.TimeoutError:
            logger.warning(f"Timeout pada attempt {attempt + 1}")
        except Exception as e:
            logger.error(f"Error download (attempt {attempt + 1}): {str(e)}")
        
        if attempt < max_retries - 1:
            await asyncio.sleep(2 ** attempt)  # Exponential backoff
    
    return False

@router.post("/mux")
async def mux_video(request: MuxRequest, background_tasks: BackgroundTasks):
    """Gabungkan video dan audio dengan error handling yang robust"""
    
    tmp_dir = tempfile.mkdtemp(prefix="videomux_")
    video_path = os.path.join(tmp_dir, "video.mp4")
    audio_path = os.path.join(tmp_dir, "audio.m4a")
    output_path = os.path.join(tmp_dir, "output.mp4")
    
    def cleanup():
        """Bersihkan temporary files"""
        try:
            if os.path.exists(tmp_dir):
                import shutil
                shutil.rmtree(tmp_dir, ignore_errors=True)
                logger.info(f"Cleaned up temp dir: {tmp_dir}")
        except Exception as e:
            logger.error(f"Cleanup error: {str(e)}")
    
    background_tasks.add_task(cleanup)
    
    try:
        logger.info(f"Starting mux for resolution: {request.resolution}")
        
        # Validasi URL tidak kosong
        if not request.video_url or not request.audio_url:
            raise HTTPException(
                status_code=400, 
                detail="URL video atau audio tidak valid/kosong"
            )
        
        # Download video dan audio secara paralel
        logger.info("Downloading video and audio files...")
        
        download_tasks = [
            download_with_retry(request.video_url, video_path),
            download_with_retry(request.audio_url, audio_path)
        ]
        
        results = await asyncio.gather(*download_tasks, return_exceptions=True)
        
        # Cek hasil download
        if not all(results):
            failed = []
            if not results[0]:
                failed.append("video")
            if not results[1]:
                failed.append("audio")
            
            raise HTTPException(
                status_code=503,
                detail=f"Gagal download {', '.join(failed)}. URL mungkin sudah kedaluwarsa."
            )
        
        logger.info("Files downloaded successfully, starting mux...")
        
        # FFmpeg command untuk mux
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",  # Copy video stream tanpa re-encode
            "-c:a", "aac",   # Encode audio ke AAC
            "-b:a", "128k",  # Audio bitrate 128k
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",  # Potong ke durasi terpendek
            "-movflags", "+faststart",  # Optimasi untuk streaming
            output_path
        ]
        
        # Jalankan ffmpeg dengan timeout
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=300  # 5 menit timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            raise HTTPException(
                status_code=504,
                detail="Mux timeout (>5 menit). Video terlalu besar."
            )
        
        if process.returncode != 0:
            error_output = stderr.decode('utf-8', errors='ignore')
            logger.error(f"FFmpeg error: {error_output}")
            
            raise HTTPException(
                status_code=500,
                detail="Gagal menggabungkan video dan audio. Coba resolusi lain."
            )
        
        # Validasi output file
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise HTTPException(
                status_code=500,
                detail="File output tidak valid atau kosong"
            )
        
        # Buat nama file yang aman
        clean_title = sanitize_filename(request.title)
        filename = f"{clean_title}_{request.resolution.replace(' ', '_')}.mp4"
        
        logger.info(f"Mux successful: {filename} ({os.path.getsize(output_path)} bytes)")
        
        return FileResponse(
            output_path,
            media_type="video/mp4",
            filename=filename,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-cache"
            }
        )
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unexpected mux error: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Mux gagal: {str(e)[:100]}"
        )

@router.post("/ytdl")
async def handle_ytdl(request: VideoRequest):
    """Endpoint untuk ekstraksi informasi video"""
    try:
        result = extract_video_logic(request.url)
        
        if not result.get("success"):
            return JSONResponse(
                status_code=400,
                content=result
            )
        
        return result
        
    except Exception as e:
        logger.error(f"YTDL endpoint error: {str(e)}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": f"Server error: {str(e)[:100]}"
            }
        )

# Health check endpoint
@router.get("/health")
async def health_check():
    """Check if service is running"""
    return {
        "status": "healthy",
        "service": "video-extractor",
        "ffmpeg": subprocess.run(
            ["ffmpeg", "-version"], 
            capture_output=True
        ).returncode == 0
    }
