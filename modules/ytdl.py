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
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (iPhone; CPU iPhone OS 17_2_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1'
    ]
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'format': 'bestvideo+bestaudio/best',
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'android'],
            }
        },
        'http_headers': {
            'User-Agent': random.choice(user_agents),
            'Accept-Language': 'en-US,en;q=0.9',
            'Referer': 'https://www.google.com/',
        }
    }
    if os.path.exists('cookies.txt'):
        opts['cookiefile'] = 'cookies.txt'
    return opts

def extract_video_logic(url: str):
    url = url.split('?')[0].strip()
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return {"success": False, "error": "No data found"}

            raw_formats = info.get('formats', [])
            audio_only = [f for f in raw_formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
            best_audio = next(iter(sorted(audio_only, key=lambda x: (x.get('abr') or 0, x.get('tbr') or 0), reverse=True)), None)

            mp4_formats = []
            seen_res = set()
            video_formats = [f for f in raw_formats if f.get('vcodec') != 'none']
            
            for f in sorted(video_formats, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), reverse=True):
                height = f.get('height') or 0
                if height == 0: continue
                
                # AKURASI LABEL FIXED
                if height >= 2160: res_label = f"4K ({height}p)"
                elif height >= 1440: res_label = f"2K ({height}p)"
                elif height >= 1080: res_label = "1080p FHD"
                elif height >= 720: res_label = "720p HD"
                else: res_label = f"{height}p"

                fps = f.get('fps')
                if fps and fps > 30: res_label += f" {int(fps)}fps"

                if res_label in seen_res: continue
                seen_res.add(res_label)

                is_combined = f.get('acodec') != 'none' and f.get('acodec') != 'unknown'
                
                mp4_formats.append({
                    "resolution": res_label,
                    "ext": "mp4",
                    "video_url": f.get('url'),
                    "audio_url": None if is_combined else (best_audio.get('url') if best_audio else None),
                    "needs_mux": not is_combined,
                    "height": height
                })

            return {
                "success": True,
                "title": info.get('title'),
                "duration": info.get('duration_string'),
                "thumbnail": info.get('thumbnail'),
                "mp4_formats": mp4_formats[:15],
                "mp3_formats": [{"quality": f"{int(f.get('abr', 0))}kbps", "audio_url": f.get('url')} for f in audio_only[:3]],
                "platform": info.get('extractor_key'),
            }
    except Exception as e:
        return {"success": False, "message": str(e)}

@router.post("/mux")
async def mux_video(request: MuxRequest, background_tasks: BackgroundTasks):
    tmp_dir = tempfile.mkdtemp()
    output_path = os.path.join(tmp_dir, "output.mp4")
    background_tasks.add_task(lambda: (importlib.import_module('shutil').rmtree(tmp_dir) if os.path.exists(tmp_dir) else None))
    try:
        cmd = ["ffmpeg", "-y", "-i", request.video_url, "-i", request.audio_url, "-c:v", "copy", "-c:a", "aac", "-shortest", output_path]
        subprocess.run(cmd, capture_output=True, timeout=300)
        return FileResponse(output_path, media_type="video/mp4", filename=f"video_{request.resolution}.mp4")
    except Exception as e:
        return {"success": False, "message": str(e)}

@router.post("/ytdl")
async def handle_ytdl(request: VideoRequest):
    return extract_video_logic(request.url)
