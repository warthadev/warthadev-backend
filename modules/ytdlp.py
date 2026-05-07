import os
import yt_dlp
import uuid
import random
import subprocess
import tempfile
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

def generate_random_device_id():
    random_hex = ''.join(random.choices('0123456789abcdef', k=16))
    return f"android-{random_hex}"

def get_ydl_opts():
    return {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'nocheckcertificate': True,
        'geo_bypass': True,
        'socket_timeout': 20,
        'retries': 5,
        # Mengutamakan format mp4 agar lebih kompatibel
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web'],
            }
        },
        'http_headers': {
            'User-Agent': f'Mozilla/5.0 (iPhone; CPU iPhone OS 17_{random.randint(0,5)} like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            'Accept-Language': 'en-US,en;q=0.9',
            'X-IG-Device-ID': generate_random_device_id(),
        }
    }

def extract_video_logic(url: str):
    url = url.split('?')[0].strip()
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return {"success": False, "error": "empty data"}

            raw_formats = info.get('formats', []) or [info]
            
            # Pisahkan format
            audio_only = [f for f in raw_formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
            best_audio = next(iter(sorted(audio_only, key=lambda x: x.get('abr') or 0, reverse=True)), None)

            mp4_formats = []
            seen_res = set()

            # 1. Prioritaskan format yang sudah ada Video + Audio (Langsung bunyi)
            combined = [f for f in raw_formats if f.get('vcodec') != 'none' and f.get('acodec') != 'none']
            for f in sorted(combined, key=lambda x: x.get('height') or 0, reverse=True):
                height = f.get('height') or 0
                res = f"{height}p" if height > 0 else "hd"
                if res in seen_res: continue
                seen_res.add(res)
                mp4_formats.append({
                    "resolution": res,
                    "ext": "mp4",
                    "video_url": f.get('url'),
                    "audio_url": None,
                    "needs_mux": False, # Ini langsung ada suaranya
                    "note": "direct",
                    "height": height
                })

            # 2. Ambil Video Only yang resolusinya belum ada di 'combined'
            video_only = [f for f in raw_formats if f.get('vcodec') != 'none' and f.get('acodec') == 'none']
            for f in sorted(video_only, key=lambda x: x.get('height') or 0, reverse=True):
                height = f.get('height') or 0
                res = f"{height}p" if height > 0 else "hd"
                if res in seen_res: continue
                seen_res.add(res)
                mp4_formats.append({
                    "resolution": res,
                    "ext": "mp4",
                    "video_url": f.get('url'),
                    "audio_url": best_audio.get('url') if best_audio else None,
                    "needs_mux": True, # Perlu panggil endpoint /mux biar ada suara
                    "note": "needs_mux",
                    "height": height
                })

            mp4_formats.sort(key=lambda x: x['height'], reverse=True)

            # MP3 Formats
            mp3_formats = []
            seen_abr = set()
            for f in sorted(audio_only, key=lambda x: x.get('abr') or 0, reverse=True):
                abr = int(f.get('abr') or 0)
                label = f"{abr}kbps" if abr > 0 else "audio"
                if label in seen_abr: continue
                seen_abr.add(label)
                mp3_formats.append({
                    "resolution": label,
                    "ext": "mp3",
                    "audio_url": f.get('url'),
                    "note": "mp3"
                })

            return {
                "success": True,
                "title": info.get('title'),
                "thumbnail": info.get('thumbnail'),
                "mp4_formats": mp4_formats[:10],
                "mp3_formats": mp3_formats[:5],
                "platform": info.get('extractor_key'),
            }
    except Exception as e:
        return {"success": False, "message": str(e)[:150]}

def remove_temp_dir(path: str):
    import shutil
    if os.path.exists(path):
        shutil.rmtree(path)

@router.post("/mux")
async def mux_video(request: MuxRequest, background_tasks: BackgroundTasks):
    tmp_dir = tempfile.mkdtemp()
    output_path = os.path.join(tmp_dir, f"result_{uuid.uuid4().hex[:8]}.mp4")
    # Hapus folder temp setelah kirim file
    background_tasks.add_task(remove_temp_dir, tmp_dir)

    try:
        # Gunakan filter aac agar kompatibel di semua HP
        cmd = [
            "ffmpeg", "-y",
            "-i", request.video_url,
            "-i", request.audio_url,
            "-c:v", "copy",
            "-c:a", "aac",
            "-strict", "experimental",
            "-shortest",
            output_path
        ]
        
        # Penambahan timeout lebih lama buat video durasi panjang
        process = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        
        if process.returncode != 0:
            return {"success": False, "error": "FFmpeg error", "log": process.stderr[-200:]}

        return FileResponse(
            output_path,
            media_type="video/mp4",
            filename=f"wartha_{request.resolution}.mp4"
        )
    except Exception as e:
        return {"success": False, "message": str(e)}

@router.post("/ytdl")
async def handle_ytdl(request: VideoRequest):
    return extract_video_logic(request.url)
