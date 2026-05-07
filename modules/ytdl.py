import os
import yt_dlp
import uuid
import random
import subprocess
import tempfile
import importlib
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

def get_ydl_opts():
    user_agents = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1'
    ]
    return {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'socket_timeout': 60,
        'retries': 10,
        'format': 'bestvideo+bestaudio/best',
        'extractor_args': {
            'youtube': {
                'player_client': ['tv', 'ios', 'android', 'mweb'],
                'skip': ['dash', 'hls'],
            }
        },
        'http_headers': {
            'User-Agent': random.choice(user_agents),
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.google.com/',
        }
    }

def extract_video_logic(url: str):
    target_url = url.strip()
    if '/shorts/' in target_url:
        target_url = target_url.replace('/shorts/', '/watch?v=')
    
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(target_url, download=False)
            if not info:
                return {"success": False, "error": "No data found"}

            raw_formats = info.get('formats', [])
            audio_only = [f for f in raw_formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
            best_audio = next(iter(sorted(audio_only, key=lambda x: (x.get('abr') or 0), reverse=True)), None)

            mp4_formats = []
            seen_res = set()
            video_formats = [f for f in raw_formats if f.get('vcodec') != 'none']
            
            # Sortir berdasarkan resolusi asli (H x W)
            for f in sorted(video_formats, key=lambda x: (x.get('height') or 0, x.get('width') or 0, x.get('tbr') or 0), reverse=True):
                h = f.get('height') or 0
                w = f.get('width') or 0
                if h == 0 or w == 0: continue
                
                # DETEKSI VERTIKAL
                is_vertical = h > w
                
                # PENENTUAN LABEL BERDASARKAN SISI TERPENDEK (Agar 720p gak jadi 1080p)
                short_side = w if is_vertical else h
                long_side = h if is_vertical else w

                if short_side >= 2160: base_label = f"4K ({short_side}p)"
                elif short_side >= 1440: base_label = f"2K ({short_side}p)"
                elif short_side >= 1080: base_label = "1080p FHD"
                elif short_side >= 720: base_label = "720p HD"
                else: base_label = f"{short_side}p"

                orientation = "Vertical " if is_vertical else ""
                res_label = f"{orientation}{base_label}"

                if res_label in seen_res: continue
                seen_res.add(res_label)

                is_combined = f.get('acodec') != 'none' and f.get('acodec') != 'unknown'
                
                mp4_formats.append({
                    "resolution": res_label,
                    "video_url": f.get('url'),
                    "audio_url": None if is_combined else (best_audio.get('url') if best_audio else None),
                    "needs_mux": not is_combined,
                    "height": h,
                    "width": w,
                    "real_res": f"{w}x{h}"
                })

            return {
                "success": True,
                "title": info.get('title'),
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
    background_tasks.add_task(lambda: (importlib.import_module('shutil').rmtree(tmp_dir) if os.path.exists(tmp_dir) else None))
    try:
        # Gunakan pemetaan stream yang ketat agar orientasi tidak berubah
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
        
        # Nama file hasil download akan menyertakan resolusi asli
        filename = f"video_{request.resolution.replace(' ', '_')}.mp4"
        return FileResponse(output_path, media_type="video/mp4", filename=filename)
    except Exception as e:
        return {"success": False, "message": str(e)}

@router.post("/ytdl")
async def handle_ytdl(request: VideoRequest):
    return extract_video_logic(request.url)
