import os
import yt_dlp
import uuid
import random
import subprocess
import tempfile
import importlib
import re
import shutil
from fastapi import APIRouter, HTTPException
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
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
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
        }
    }

def extract_video_logic(url: str):
    target_url = url.strip()
    
    if '/shorts/' in target_url:
        target_url = target_url.replace('/shorts/', '/watch?v=')
    
    if any(p in target_url for p in ['facebook.com', 'tiktok.com', 'instagram.com']):
        target_url = target_url.split('?')[0]

    try:
        opts = get_ydl_opts()
        if 'tiktok.com' in target_url:
            opts['http_headers']['Referer'] = 'https://www.tiktok.com/'

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target_url, download=False)
            
            if info.get('extractor_key') == 'Generic' and info.get('url'):
                info = ydl.extract_info(info.get('url'), download=False)

            if not info:
                return {"success": False, "error": "No data found"}

            raw_formats = info.get('formats', [])
            audio_only = [f for f in raw_formats if f.get('acodec') != 'none' and (f.get('vcodec') == 'none' or f.get('vcodec') is None)]
            best_audio = next(iter(sorted(audio_only, key=lambda x: (x.get('abr') or 0), reverse=True)), None)

            mp4_formats = []
            seen_res = set()
            video_formats = [f for f in raw_formats if f.get('height') is not None or f.get('vcodec') != 'none']

            for f in sorted(video_formats, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), reverse=True):
                h = f.get('height') or 0
                w = f.get('width') or 0
                
                # --- LOGIKA VERTIKAL & LANDSKAP ---
                # Menggunakan sisi terpanjang untuk menentukan kelas resolusi
                logical_res = max(w, h) if w > 0 and h > 0 else h

                if h == 0:
                    format_id = (f.get('format_id') or '').lower()
                    if 'hd' in format_id: logical_res = 1280
                    elif 'sd' in format_id: logical_res = 640
                    else: continue

                if logical_res >= 3840: res_label = "4K"
                elif logical_res >= 2560: res_label = "2K"
                elif logical_res >= 1920: res_label = "1080p FHD"
                elif logical_res >= 1280: res_label = "720p HD"
                elif logical_res >= 854: res_label = "480p"
                else: res_label = f"{min(w, h)}p"

                if res_label in seen_res: continue
                seen_res.add(res_label)

                has_audio = f.get('acodec') not in [None, 'none', 'unknown']

                mp4_formats.append({
                    "resolution": res_label,
                    "video_url": f.get('url'),
                    "audio_url": None if has_audio else (best_audio.get('url') if best_audio else None),
                    "needs_mux": not has_audio,
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
        return {"success": False, "message": str(e)[:150]}

@router.post("/mux")
async def mux_video(request: MuxRequest, background_tasks: BackgroundTasks):
    tmp_dir = tempfile.mkdtemp()
    output_path = os.path.join(tmp_dir, "output.mp4")
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    
    try:
        # Menambahkan flag reconnect agar FFmpeg tahan banting terhadap gangguan koneksi
        cmd = [
            "ffmpeg", "-y",
            "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
            "-headers", f"User-Agent: {ua}\r\n",
            "-i", request.video_url,
            "-headers", f"User-Agent: {ua}\r\n",
            "-i", request.audio_url,
            "-c:v", "copy",
            "-c:a", "aac",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-map_metadata", "0",  # Penting: Menjaga metadata rotasi portrait
            "-shortest",
            output_path
        ]
        
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        
        if process.returncode != 0:
            raise Exception(f"FFmpeg Error: {process.stderr}")

        clean_title = re.sub(r'[\\/*?:"<>|]', "", request.title)
        filename = f"{clean_title}_{request.resolution.replace(' ', '_')}.mp4"
        
        background_tasks.add_task(lambda: (shutil.rmtree(tmp_dir) if os.path.exists(tmp_dir) else None))
        return FileResponse(output_path, media_type="video/mp4", filename=filename)
        
    except Exception as e:
        if os.path.exists(tmp_dir): shutil.rmtree(tmp_dir)
        raise HTTPException(status_code=422, detail=str(e))

@router.post("/ytdl")
async def handle_ytdl(request: VideoRequest):
    return extract_video_logic(request.url)
    
