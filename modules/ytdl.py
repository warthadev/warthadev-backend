import os
import yt_dlp
import uuid
import random
import subprocess
import tempfile
import importlib
import re
from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTasks

router = APIRouter(prefix="/extract")

class VideoRequest(BaseModel):
    url: str

class MuxRequest(BaseModel):
    video_url: str
    audio_url: str
    resolution: str
    title: str

def get_ydl_opts():
    # User agent terbaru untuk mengelabui deteksi bot
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1'
    ]
    return {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'socket_timeout': 30,
        'retries': 5,
        'format': 'bestvideo+bestaudio/best',
        'http_headers': {
            'User-Agent': random.choice(user_agents),
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        }
    }

def extract_video_logic(url: str):
    target_url = url.strip()
    
    # 1. Konversi YouTube Shorts
    if '/shorts/' in target_url:
        target_url = target_url.replace('/shorts/', '/watch?v=')
    
    # 2. Bersihkan parameter tracking (Krusial untuk FB & TikTok)
    if 'facebook.com' in target_url or 'tiktok.com' in target_url:
        target_url = target_url.split('?')[0]

    try:
        opts = get_ydl_opts()
        
        # 3. Handling Referer Khusus TikTok
        if 'tiktok.com' in target_url:
            opts['http_headers']['Referer'] = 'https://www.tiktok.com/'

        with yt_dlp.YoutubeDL(opts) as ydl:
            # Ekstraksi pertama
            info = ydl.extract_info(target_url, download=False)
            
            # 4. Resolve Redirect (Khusus link vt.tiktok.com)
            if info.get('extractor_key') == 'Generic' and info.get('url'):
                info = ydl.extract_info(info.get('url'), download=False)

            if not info:
                return {"success": False, "error": "Metadata tidak ditemukan"}

            raw_formats = info.get('formats', [])
            
            audio_only = [f for f in raw_formats if f.get('acodec') != 'none' and (f.get('vcodec') == 'none' or f.get('vcodec') is None)]
            best_audio = next(iter(sorted(audio_only, key=lambda x: (x.get('abr') or 0), reverse=True)), None)

            mp4_formats = []
            seen_res = set()
            
            video_formats = [f for f in raw_formats if f.get('height') is not None or f.get('vcodec') != 'none']

            for f in sorted(video_formats, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), reverse=True):
                h = f.get('height') or 0
                w = f.get('width') or 0
                
                # FIX LOGIKA: Gunakan sisi terpendek agar video Vertikal terdeteksi benar
                shorter_side = min(w, h) if w > 0 and h > 0 else h

                if h == 0:
                    format_id = (f.get('format_id') or '').lower()
                    if 'hd' in format_id: shorter_side = 720
                    elif 'sd' in format_id: shorter_side = 360
                    else: continue

                # Penentuan Label Resolusi
                if shorter_side >= 2160: res_label = "4K"
                elif shorter_side >= 1440: res_label = "2K"
                elif shorter_side >= 1080: res_label = "1080p FHD"
                elif shorter_side >= 720: res_label = "720p HD"
                elif shorter_side >= 480: res_label = "480p"
                else: res_label = f"{shorter_side}p"

                if res_label in seen_res:
                    continue
                seen_res.add(res_label)

                has_audio = f.get('acodec') not in [None, 'none', 'unknown']

                mp4_formats.append({
                    "resolution": res_label,
                    "video_url": f.get('url'),
                    "audio_url": None if has_audio else (best_audio.get('url') if best_audio else None),
                    "needs_mux": not has_audio,
                    "height": h,
                    "width": w,
                    "real_res": f"{w}x{h}"
                })

            return {
                "success": True,
                "title": info.get('title'),
                "uploader": info.get('uploader') or info.get('channel') or info.get('creator'),
                "duration": info.get('duration_string'),
                "thumbnail": info.get('thumbnail'),
                "mp4_formats": mp4_formats[:12],
                "mp3_formats": [{"quality": "HQ", "audio_url": f.get('url')} for f in audio_only[:2]],
                "platform": info.get('extractor_key'),
            }

    except Exception as e:
        # Menangkap error spesifik untuk debugging
        return {"success": False, "message": str(e)[:150]}

@router.post("/mux")
async def mux_video(request: MuxRequest, background_tasks: BackgroundTasks):
    tmp_dir = tempfile.mkdtemp()
    output_path = os.path.join(tmp_dir, "output.mp4")
    background_tasks.add_task(lambda: (importlib.import_module('shutil').rmtree(tmp_dir) if os.path.exists(tmp_dir) else None))
    
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", request.video_url,
            "-i", request.audio_url,
            "-c:v", "copy",
            "-c:a", "aac",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            output_path
        ]
        subprocess.run(cmd, capture_output=True, timeout=300)
        
        # Bersihkan judul dari karakter terlarang OS
        clean_title = re.sub(r'[\\/*?:"<>|]', "", request.title)
        filename = f"{clean_title}_{request.resolution.replace(' ', '_')}.mp4"
        
        return FileResponse(output_path, media_type="video/mp4", filename=filename)
    except Exception as e:
        return {"success": False, "message": str(e)}

@router.post("/ytdl")
async def handle_ytdl(request: VideoRequest):
    return extract_video_logic(request.url)
