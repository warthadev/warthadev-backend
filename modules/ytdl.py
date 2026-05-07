import os
import yt_dlp
import uuid
import random
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/extract")

class VideoRequest(BaseModel):
    url: str

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
        'fragment_retries': 5,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web'],
            }
        },
        'http_headers': {
            'User-Agent': f'Mozilla/5.0 (iPhone; CPU iPhone OS 17_{random.randint(0,5)} like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'X-IG-Device-ID': generate_random_device_id(),
            'X-MID': str(uuid.uuid4()),
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

            video_only = [f for f in raw_formats if f.get('vcodec') not in (None, 'none') and f.get('acodec') in (None, 'none')]
            combined   = [f for f in raw_formats if f.get('vcodec') not in (None, 'none') and f.get('acodec') not in (None, 'none')]
            audio_only = [f for f in raw_formats if f.get('acodec') not in (None, 'none') and f.get('vcodec') in (None, 'none')]

            best_audio = next(
                iter(sorted(audio_only, key=lambda x: x.get('abr') or 0, reverse=True)),
                None
            )

            mp4_formats = []
            seen = set()

            for f in sorted(video_only, key=lambda x: x.get('height') or 0, reverse=True):
                height = f.get('height') or 0
                res = f"{height}p" if height > 0 else "hd"
                if res in seen:
                    continue
                seen.add(res)
                mp4_formats.append({
                    "resolution": res,
                    "ext": "mp4",
                    "video_url": f.get('url'),
                    "audio_url": best_audio.get('url') if best_audio else None,
                    "needs_mux": best_audio is not None,
                    "note": "mp4",
                    "height": height,
                })

            for f in sorted(combined, key=lambda x: x.get('height') or 0, reverse=True):
                height = f.get('height') or 0
                res = f"{height}p" if height > 0 else "hd"
                if res in seen:
                    continue
                seen.add(res)
                mp4_formats.append({
                    "resolution": res,
                    "ext": "mp4",
                    "video_url": f.get('url'),
                    "audio_url": None,
                    "needs_mux": False,
                    "note": "mp4",
                    "height": height,
                })

            mp4_formats.sort(key=lambda x: x['height'], reverse=True)

            mp3_formats = []
            seen_abr = set()
            for f in sorted(audio_only, key=lambda x: x.get('abr') or 0, reverse=True):
                abr = f.get('abr') or 0
                label = f"{int(abr)}kbps" if abr else "audio"
                if label in seen_abr:
                    continue
                seen_abr.add(label)
                mp3_formats.append({
                    "resolution": label,
                    "ext": "mp3",
                    "audio_url": f.get('url'),
                    "video_url": None,
                    "needs_mux": False,
                    "note": "mp3",
                    "height": 0,
                    "abr": abr,
                })

            return {
                "success": True,
                "title": info.get('title') or "video result",
                "thumbnail": info.get('thumbnail'),
                "mp4_formats": mp4_formats[:8],
                "mp3_formats": mp3_formats[:5],
                "platform": info.get('extractor_key'),
            }

    except Exception as e:
        err = str(e)
        err_lower = err.lower()

        if "sign in" in err_lower or "bot" in err_lower:
            return {"success": False, "error": "auth_required", "message": "platform minta verifikasi. coba url lain atau tunggu beberapa saat"}
        if "timed out" in err_lower:
            return {"success": False, "error": "timeout", "message": "koneksi timeout. coba lagi"}
        if "ssl" in err_lower or "eof" in err_lower:
            return {"success": False, "error": "ssl_error", "message": "koneksi ssl gagal. coba lagi dalam beberapa detik"}
        if "private" in err_lower:
            return {"success": False, "error": "private", "message": "konten private atau tidak tersedia"}
        if "unavailable" in err_lower or "removed" in err_lower:
            return {"success": False, "error": "unavailable", "message": "video tidak tersedia atau sudah dihapus"}

        return {"success": False, "error": "fault", "message": err[:150]}

@router.post("/ytdl")
async def handle_ytdl(request: VideoRequest):
    return extract_video_logic(request.url)