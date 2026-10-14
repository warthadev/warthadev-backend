# modules/ytdl.py
import os
import re
import shutil
import random
import tempfile
import asyncio
import logging
import subprocess
import time
from typing import Dict, Any, Optional, Tuple, List
from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, validator
import yt_dlp

from . import diskcache as dc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/extract")

COBALT_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 600
MIN_VALID_SIZE = 100 * 1024

DIRECT_EXTS = ['.mp4', '.mkv', '.webm', '.avi', '.flv', '.mov', '.m4v', '.3gp']
HLS_EXTS = ['.m3u8']
DASH_EXTS = ['.mpd']

SITES_NEED_EXTRACTOR = [
    'youtube.com', 'youtu.be', 'youtube-nocookie.com',
    'facebook.com', 'fb.watch', 'instagram.com',
    'twitter.com', 'x.com', 'tiktok.com',
    'vimeo.com', 'dailymotion.com', 'twitch.tv',
    'bilibili.com', 'nicovideo.jp', 'soundcloud.com',
    'reddit.com', 'redd.it',
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

BOT_BLOCK_MARKERS = [
    "sign in to confirm",
    "confirm you're not a bot",
    "confirm you’re not a bot",
    "bot check",
]

RATE_LIMIT_MARKERS = [
    "429",
    "too many requests",
]

YOUTUBE_CLIENT_ROTATIONS = [
    ["android", "web_safari"],
    ["ios", "mweb"],
    ["tv_embedded"],
    ["web"],
]


class VideoRequest(BaseModel):
    url: str

    @validator("url")
    def validate_url(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("URL must not be empty")
        return v.strip()


class MuxRequest(BaseModel):
    video_url: str
    audio_url: str
    resolution: str
    title: str

    @validator("video_url", "audio_url")
    def validate_urls(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Video/Audio URL must not be empty")
        return v.strip()

    @validator("title")
    def validate_title(cls, v: str) -> str:
        if not v or not v.strip():
            return "video_download"
        return v.strip()


def _is_bot_block(error_msg: str) -> bool:
    lower = error_msg.lower()
    return any(marker in lower for marker in BOT_BLOCK_MARKERS)


def _is_rate_limit(error_msg: str) -> bool:
    lower = error_msg.lower()
    return any(marker in lower for marker in RATE_LIMIT_MARKERS)


class CobaltAPI:
    def __init__(self):
        self.api_url = "https://api.cobalt.tools/api/json"
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

    async def fetch(self, url: str, audio_only: bool = False) -> Dict[str, Any]:
        payload = {
            "url": url,
            "videoQuality": "720" if not audio_only else "max",
            "audioFormat": "mp3" if audio_only else "best",
            "filenameStyle": "pretty",
            "disableMetadata": False
        }

        try:
            import aiohttp
            timeout = aiohttp.ClientTimeout(total=COBALT_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.api_url,
                    json=payload,
                    headers=self.headers
                ) as response:
                    try:
                        result = await response.json()
                    except Exception:
                        return {"success": False, "message": f"Cobalt returned non-JSON (HTTP {response.status})"}

                    if not isinstance(result, dict):
                        return {"success": False, "message": "Cobalt returned unexpected response"}

                    status = result.get("status")

                    if status == "error":
                        code = result.get("error", {}).get("code", "unknown")
                        return {"success": False, "message": f"Cobalt error: {code}"}

                    if status == "picker":
                        items = result.get("picker", [])
                        return {
                            "success": True,
                            "type": "picker",
                            "picker": items
                        }

                    if status in ("tunnel", "redirect"):
                        return {
                            "success": True,
                            "url": result.get("url"),
                            "filename": result.get("filename", "media"),
                            "duration": result.get("duration")
                        }

                    return {"success": False, "message": "Unknown response"}

        except asyncio.TimeoutError:
            return {"success": False, "message": "Request timeout"}
        except Exception as e:
            logger.error(f"Cobalt error: {e}")
            return {"success": False, "message": str(e)}

    def format_response(self, result: Dict, title: str) -> Optional[Dict]:
        if not result.get("success"):
            return None

        download_url = result.get("url")
        if not download_url:
            return None

        return {
            "success": True,
            "title": title,
            "platform": "cobalt",
            "mp4_formats": [{
                "resolution": "720p",
                "video_url": download_url,
                "audio_url": None,
                "needs_mux": False,
                "filesize": None,
                "is_cobalt": True
            }],
            "mp3_formats": [],
            "best_muxed": {
                "video_url": download_url,
                "resolution": "720p",
                "ext": "mp4"
            }
        }


cobalt = CobaltAPI()


def get_ydl_opts(proxy: Optional[str] = None, cookies_file: Optional[str] = None) -> Dict[str, Any]:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "socket_timeout": 15,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 1,
        "format": "bv*+ba/b",
        "http_headers": {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        },
    }

    if proxy:
        opts["proxy"] = proxy

    if cookies_file and os.path.exists(cookies_file):
        opts["cookiefile"] = cookies_file

    return opts


def _normalize_url(url: str) -> str:
    target_url = url.strip()
    if "/shorts/" in target_url:
        target_url = target_url.replace("/shorts/", "/watch?v=")
    return target_url


def _select_best_formats(info: Dict[str, Any]) -> Dict[str, Any]:
    raw_formats = info.get("formats", []) or []
    if not raw_formats:
        return {"success": False, "message": "No formats found"}

    audio_only = [
        f for f in raw_formats
        if f.get("acodec") not in [None, "none"]
        and (f.get("vcodec") in [None, "none"] or f.get("resolution") == "audio only")
    ]
    best_audio = None
    if audio_only:
        best_audio = max(audio_only, key=lambda x: (x.get("abr") or x.get("tbr") or 0))

    video_formats = [
        f for f in raw_formats
        if f.get("height") is not None and f.get("vcodec") not in [None, "none"]
    ]

    mp4_formats = []
    seen_res = set()
    sorted_videos = sorted(video_formats, key=lambda x: (x.get("height") or 0), reverse=True)

    for f in sorted_videos:
        h = f.get("height") or 0
        w = f.get("width") or 0
        if h == 0:
            continue

        shorter_side = min(w, h) if w > 0 and h > 0 else h
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

        if res_label in seen_res:
            continue
        seen_res.add(res_label)

        has_audio = f.get("acodec") not in [None, "none", "unknown"]
        video_url = f.get("url")
        if not video_url:
            continue

        size = f.get("filesize") or f.get("filesize_approx")

        mp4_formats.append({
            "resolution": res_label,
            "video_url": video_url,
            "audio_url": None if has_audio else (best_audio.get("url") if best_audio else None),
            "needs_mux": not has_audio,
            "height": h,
            "width": w,
            "filesize": size,
        })

    mp4_formats = mp4_formats[:10]

    mp3_formats = []
    for idx, f in enumerate(audio_only[:3], 1):
        quality = "HQ" if idx == 1 else f"Quality {idx}"
        mp3_formats.append({
            "quality": quality,
            "audio_url": f.get("url"),
            "bitrate": f.get("abr") or f.get("tbr"),
        })

    return {
        "success": True,
        "mp4_formats": mp4_formats,
        "mp3_formats": mp3_formats,
    }


def extract_youtube_info(url: str, proxy: Optional[str] = None, cookies_file: Optional[str] = None) -> Dict[str, Any]:
    target_url = _normalize_url(url)
    last_error: Optional[str] = None
    bot_blocked = False
    rate_limited = False

    for client_index, client in enumerate(YOUTUBE_CLIENT_ROTATIONS):
        try:
            logger.info(f"YouTube attempt {client_index + 1} client={client}: {target_url}")
            opts = get_ydl_opts(proxy=proxy, cookies_file=cookies_file)
            opts["extractor_args"] = {
                "youtube": {
                    "player_client": client,
                    "skip": ["hls", "dash"],
                },
            }

            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target_url, download=False)

            if not info:
                last_error = "Could not extract info"
                continue

            if "entries" in info and isinstance(info["entries"], list):
                if not info["entries"]:
                    last_error = "No entries found"
                    continue
                info = info["entries"][0]

            selected = _select_best_formats(info)
            if not selected.get("success"):
                last_error = selected.get("message") or "No formats"
                continue

            return {
                "success": True,
                "title": info.get("title", "Video"),
                "uploader": info.get("uploader") or info.get("channel"),
                "duration": info.get("duration_string") or f"{info.get('duration', 0)}s",
                "thumbnail": info.get("thumbnail"),
                "platform": info.get("extractor_key", "youtube"),
                "mp4_formats": selected["mp4_formats"],
                "mp3_formats": selected["mp3_formats"],
            }
        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e)
            last_error = error_msg
            logger.warning(f"YouTube attempt {client_index + 1} failed: {error_msg[:200]}")

            if _is_bot_block(error_msg):
                bot_blocked = True

            if _is_rate_limit(error_msg):
                rate_limited = True
                logger.info(f"YouTube rate limited (429), breaking rotation early")
                break

            if "private" in error_msg.lower():
                return {"success": False, "message": "Video bersifat private"}
            continue
        except Exception as e:
            last_error = str(e)
            logger.warning(f"YouTube attempt {client_index + 1} unexpected error: {last_error[:200]}")
            continue

    if bot_blocked or rate_limited:
        logger.info(f"YouTube blocked (bot={bot_blocked}, rate={rate_limited}), signalling frontend to use Colab")
        return {
            "success": False,
            "message": "YOUTUBE_BOT_BLOCK",
            "detail": "YouTube memblokir IP server. Gunakan tombol Run in Colab untuk download via Colab.",
        }

    if last_error:
        logger.error(f"YouTube extraction exhausted all clients: {last_error[:200]}")
        return {"success": False, "message": last_error[:150]}

    return {"success": False, "message": "Extraction failed"}


async def extract_video_logic(url: str) -> Dict[str, Any]:
    target_url = url.strip()

    is_instagram = "instagram.com" in target_url.lower()
    is_tiktok = "tiktok.com" in target_url.lower()
    is_twitter = "twitter.com" in target_url.lower() or "x.com" in target_url.lower()
    is_facebook = "facebook.com" in target_url.lower() or "fb.watch" in target_url.lower()
    is_youtube = "youtube.com" in target_url.lower() or "youtu.be" in target_url.lower()

    if is_instagram or is_tiktok or is_twitter or is_facebook:
        logger.info(f"Using Cobalt API for: {target_url}")

        result = await cobalt.fetch(target_url)

        if result.get("success") and result.get("url"):
            title = "Instagram Video"
            if is_instagram:
                title = "Instagram Reel"
            elif is_tiktok:
                title = "TikTok Video"
            elif is_twitter:
                title = "Twitter Video"

            formatted = cobalt.format_response(result, title)
            if formatted:
                return formatted
            return {
                "success": False,
                "message": "Failed to process with Cobalt"
            }
        else:
            return {
                "success": False,
                "message": result.get("message", "Cobalt API failed")
            }

    if is_youtube:
        logger.info(f"Using yt-dlp for: {target_url}")
        result = extract_youtube_info(target_url)

        if result.get("success"):
            return result

        if result.get("message") == "YOUTUBE_BOT_BLOCK":
            logger.info(f"YouTube blocked, trying Cobalt fallback for: {target_url}")
            try:
                cobalt_result = await cobalt.fetch(target_url, audio_only=False)
            except Exception as e:
                logger.warning(f"Cobalt video fetch failed: {e}")
                cobalt_result = {"success": False}

            if cobalt_result.get("success") and cobalt_result.get("url"):
                formatted = cobalt.format_response(cobalt_result, "YouTube Video")
                if formatted:
                    formatted["platform"] = "youtube-cobalt"
                    formatted["colab_only"] = True
                    return formatted

            try:
                cobalt_audio = await cobalt.fetch(target_url, audio_only=True)
            except Exception as e:
                logger.warning(f"Cobalt audio fetch failed: {e}")
                cobalt_audio = {"success": False}

            if cobalt_audio.get("success") and cobalt_audio.get("url"):
                return {
                    "success": True,
                    "title": "YouTube Audio",
                    "platform": "youtube-cobalt",
                    "colab_only": True,
                    "mp4_formats": [],
                    "mp3_formats": [{
                        "quality": "HQ",
                        "audio_url": cobalt_audio.get("url"),
                        "bitrate": None,
                    }],
                    "best_muxed": {
                        "video_url": cobalt_audio.get("url"),
                        "resolution": "audio",
                        "ext": "mp3"
                    }
                }

            return {
                "success": False,
                "message": "YOUTUBE_BOT_BLOCK",
                "detail": "YouTube memblokir IP server. Gunakan tombol Run in Colab untuk download via Colab.",
            }

        return result

    logger.info(f"Fallback to yt-dlp for: {target_url}")
    return extract_youtube_info(target_url)


def sanitize_filename(filename: str, max_length: int = 80) -> str:
    clean = re.sub(r"[^\x00-\x7F]+", "", filename)
    clean = re.sub(r'[\\/*?:"<>|]', "", clean)
    clean = " ".join(clean.split())
    clean = clean[:max_length].strip()
    return clean if clean else "video_download"


def _detect_url_type(url: str) -> str:
    url_lower = url.lower()
    path = url_lower.split('?')[0].split('#')[0]
    if any(path.endswith(ext) for ext in HLS_EXTS):
        return 'hls'
    if any(path.endswith(ext) for ext in DASH_EXTS):
        return 'dash'
    if any(path.endswith(ext) for ext in DIRECT_EXTS):
        return 'direct'
    return 'extractor'


def _is_valid_file(filepath: str, min_size: int = MIN_VALID_SIZE) -> Tuple[bool, str]:
    if not os.path.exists(filepath):
        return False, "File not found"
    size = os.path.getsize(filepath)
    if size < min_size:
        return False, f"File too small ({size} bytes)"
    try:
        with open(filepath, 'rb') as f:
            header = f.read(16)
        if len(header) >= 8 and header[4:8] == b'ftyp':
            return True, "MP4 valid"
        if header[:4] == b'\x1a\x45\xdf\xa3':
            return True, "WebM/MKV valid"
        if header[:3] == b'ID3' or header[:2] == b'\xff\xfb' or header[:2] == b'\xff\xf3':
            return True, "MP3 valid"
        if header[:4] == b'RIFF':
            return True, "AVI/WAV valid"
        if header[:3] == b'FLV':
            return True, "FLV valid"
        if header[:1] == b'\x47':
            return True, "MPEG-TS valid"
        if header[:5].lower() in (b'<!doc', b'<html') or header[:1] == b'<':
            return False, "Server returned HTML"
        return True, f"File OK ({size} bytes)"
    except Exception as e:
        return False, f"Error: {e}"


def _download_with_curl(url: str, output_path: str, max_retries: int = 3) -> bool:
    for attempt in range(max_retries):
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass

        try:
            cmd = [
                "curl", "-L", "-f",
                "--max-time", str(DOWNLOAD_TIMEOUT_SECONDS),
                "--retry", "3",
                "--user-agent", random.choice(USER_AGENTS),
                "-o", output_path, url
            ]
            process = subprocess.run(cmd, capture_output=True, timeout=DOWNLOAD_TIMEOUT_SECONDS + 30)
            if process.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                return True
        except Exception:
            pass

        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)

    return False


def _download_with_ytdlp(url: str, output_path: str, format_str: str = "best[ext=mp4]/best") -> Tuple[bool, Optional[str]]:
    temp_dir = tempfile.mkdtemp(prefix="ytdl_")
    target_basename = os.path.basename(output_path)
    temp_output = os.path.join(temp_dir, target_basename)

    try:
        opts = {
            "outtmpl": temp_output,
            "format": format_str,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "fragment_retries": 30,
            "retries": 30,
            "socket_timeout": 60,
            "concurrent_fragment_downloads": 16,
            "http_headers": {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept-Language": "en-US,en;q=0.9",
            },
            "ffmpeg_location": shutil.which("ffmpeg"),
            "paths": {"temp": temp_dir},
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])

        actual_path = temp_output
        if not os.path.exists(actual_path):
            base, _ = os.path.splitext(temp_output)
            for ext in ['.mp4', '.mkv', '.webm', '.mp3', '.m4a', '.ts', '.flv']:
                if os.path.exists(base + ext):
                    actual_path = base + ext
                    break

        if not os.path.exists(actual_path):
            try:
                for f in os.listdir(temp_dir):
                    potential = os.path.join(temp_dir, f)
                    if os.path.isfile(potential) and os.path.getsize(potential) > MIN_VALID_SIZE:
                        actual_path = potential
                        break
            except Exception:
                pass

        if not os.path.exists(actual_path):
            return False, "File not produced"

        valid, msg = _is_valid_file(actual_path)
        if not valid:
            return False, msg

        dst_dir = os.path.dirname(output_path) or '.'
        os.makedirs(dst_dir, exist_ok=True)
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
        shutil.move(actual_path, output_path)

        return True, None
    except Exception as e:
        logger.error(f"yt-dlp download error: {e}")
        return False, str(e)[:200]
    finally:
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


def _download_with_ffmpeg_stream(url: str, output_path: str, stream_type: str = 'hls') -> Tuple[bool, Optional[str]]:
    temp_dir = tempfile.mkdtemp(prefix="ffmpeg_")
    target_basename = os.path.basename(output_path)
    temp_output = os.path.join(temp_dir, target_basename)

    try:
        ua = random.choice(USER_AGENTS)
        custom_headers = f"User-Agent: {ua}\r\n"

        cmd = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
            '-headers', custom_headers,
            '-user_agent', ua,
            '-protocol_whitelist', 'file,http,https,tcp,tls,crypto',
            '-reconnect', '1',
            '-reconnect_streamed', '1',
            '-reconnect_delay_max', '5',
        ]

        if stream_type == 'dash':
            cmd.extend(['-allowed_extensions', 'ALL'])

        cmd.extend([
            '-i', url,
            '-c', 'copy',
            '-bsf:a', 'aac_adtstoasc',
            '-movflags', '+faststart',
            temp_output
        ])

        process = subprocess.run(cmd, capture_output=True, timeout=DOWNLOAD_TIMEOUT_SECONDS + 60)

        if process.returncode != 0:
            return False, f"FFmpeg exit code {process.returncode}"

        if not os.path.exists(temp_output):
            return False, "File not produced"

        valid, msg = _is_valid_file(temp_output, min_size=100 * 1024)
        if not valid:
            return False, msg

        dst_dir = os.path.dirname(output_path) or '.'
        os.makedirs(dst_dir, exist_ok=True)
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
        shutil.move(temp_output, output_path)

        return True, None
    except Exception as e:
        logger.error(f"FFmpeg stream error: {e}")
        return False, str(e)[:200]
    finally:
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


async def _download_url_to_file(url: str, output_path: str) -> Tuple[bool, Optional[str]]:
    url_type = _detect_url_type(url)

    if url_type == 'direct':
        loop = asyncio.get_event_loop()
        ok = await loop.run_in_executor(None, _download_with_curl, url, output_path)
        if ok:
            valid, msg = _is_valid_file(output_path)
            if valid:
                return True, None
            try:
                os.remove(output_path)
            except Exception:
                pass

    if url_type in ('hls', 'dash'):
        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(None, _download_with_ytdlp, url, output_path, "best[ext=mp4]/best")
        if ok:
            return True, None
        ok, err2 = await loop.run_in_executor(None, _download_with_ffmpeg_stream, url, output_path, url_type)
        if ok:
            return True, None
        return False, err or err2

    if url_type == 'extractor':
        loop = asyncio.get_event_loop()
        ok, err = await loop.run_in_executor(None, _download_with_ytdlp, url, output_path, "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best")
        if ok:
            return True, None
        return False, err

    return False, f"Unsupported URL type: {url_type}"


@router.post("/ytdl")
async def handle_ytdl(request: VideoRequest):
    if not request.url or not request.url.strip():
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "URL must not be empty"}
        )
    try:
        dc.log_disk_stats("[extract] ")
        result = await extract_video_logic(request.url)
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as e:
        logger.error(f"Endpoint error: {e}")
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": str(e)[:100]}
        )


@router.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "video-extractor-multi-platform"
    }


@router.post("/mux")
async def mux_video(request: MuxRequest, background_tasks: BackgroundTasks):
    if shutil.which("ffmpeg") is None:
        raise HTTPException(500, "ffmpeg is not installed on the server")

    dc.cleanup_disk(force=False)
    dc.log_disk_stats("[mux] before ")

    tmp_dir = tempfile.mkdtemp(prefix="videomux_")
    video_path = os.path.join(tmp_dir, "video.mp4")
    audio_path = os.path.join(tmp_dir, "audio.m4a")
    output_path = os.path.join(tmp_dir, "output.mp4")

    def cleanup():
        try:
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    background_tasks.add_task(cleanup)

    try:
        if not request.video_url or not request.audio_url:
            raise HTTPException(400, "Invalid URLs")

        loop = asyncio.get_event_loop()
        results = await asyncio.gather(
            loop.run_in_executor(None, _download_with_curl, request.video_url, video_path),
            loop.run_in_executor(None, _download_with_curl, request.audio_url, audio_path),
        )

        if not all(results):
            raise HTTPException(503, "Failed to download video/audio")

        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "128k",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            "-movflags", "+faststart",
            output_path
        ]

        process = subprocess.run(cmd, capture_output=True, timeout=300)

        if process.returncode != 0 or not os.path.exists(output_path):
            raise HTTPException(500, "Muxing failed")

        clean_title = sanitize_filename(request.title)
        filename = f"{clean_title}_{request.resolution.replace(' ', '_')}.mp4"

        return FileResponse(
            output_path,
            media_type="video/mp4",
            filename=filename
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Mux error: {e}")
        raise HTTPException(500, str(e)[:200])


@router.get("/netcheck")
async def netcheck():
    import urllib.request

    def run(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            return (r.stdout + r.stderr).strip()[:200]
        except Exception as e:
            return f"error: {str(e)[:150]}"

    def py_get(u):
        try:
            return urllib.request.urlopen(u, timeout=10).status
        except Exception as e:
            return str(e)[:150]

    curl = ["curl", "-sS", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}"]
    return {
        "curl_youtube": run(curl + ["https://www.youtube.com"]),
        "curl_google": run(curl + ["https://www.google.com"]),
        "python_youtube": py_get("https://www.youtube.com"),
        "yt_dlp": run(["yt-dlp", "--version"]),
        "node": run(["node", "-v"]),
    }
