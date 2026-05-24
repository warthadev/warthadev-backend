# modules/ytdl.py
import os
import yt_dlp
import random
import subprocess
import tempfile
import re
import asyncio
from typing import Dict, Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, validator
from starlette.background import BackgroundTasks
import logging

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/extract")


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
        if not v.startswith(("http://", "https://")):
            raise ValueError("URL must start with http:// or https://")
        return v.strip()

    @validator("title")
    def validate_title(cls, v: str) -> str:
        if not v or not v.strip():
            return "video_download"
        return v.strip()


def get_ydl_opts() -> Dict[str, Any]:
    """
    yt-dlp configuration with retry and safe headers for major sites.
    Public mode only (no cookies / auth).
    """
    user_agents = [
        (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    ]

    return {
        "quiet": False,
        "no_warnings": False,
        "skip_download": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "socket_timeout": 30,
        "retries": 10,
        "fragment_retries": 10,
        "format": "bv*+ba/b",
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "web", "mweb", "android"],
                "skip": ["hls", "dash"],
            },
            "instagram": {
                "check_headers": True,
            },
            "generic": ["impersonate"],
        },
        "http_headers": {
            "User-Agent": random.choice(user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Site": "none",
            "Connection": "keep-alive",
        },
    }


def _normalize_url(url: str) -> str:
    """
    Normalize input URL (YouTube Shorts, Instagram Reels/query) before extraction.
    """
    target_url = url.strip()

    if "/shorts/" in target_url:
        target_url = target_url.replace("/shorts/", "/watch?v=")

    if "instagram.com" in target_url:
        base = target_url.split("?", 1)[0]
        match = re.search(r"(https://www\.instagram\.com/reel/[^/?#]+)", base)
        if match:
            target_url = match.group(1) + "/"
        else:
            target_url = base

    return target_url


def _select_best_formats(info: Dict[str, Any]) -> Dict[str, Any]:
    """
    From yt-dlp info, pick:
    - best muxed MP4 (video+audio) as overall best
    - fallback: best video-only + best audio-only combination
    - limited list of resolutions for user selection
    """
    raw_formats = info.get("formats", []) or []

    if not raw_formats:
        logger.error("No formats available")
        return {
            "success": False,
            "message": "No downloadable video formats were found.",
        }

    audio_only = [
        f
        for f in raw_formats
        if f.get("acodec") not in [None, "none"]
        and (f.get("vcodec") in [None, "none"] or f.get("resolution") == "audio only")
    ]
    best_audio = None
    if audio_only:
        best_audio = max(
            audio_only,
            key=lambda x: (x.get("abr") or x.get("tbr") or 0),
        )
        logger.info(
            "Best audio: %s - %s kbps",
            best_audio.get("format_id"),
            best_audio.get("abr") or best_audio.get("tbr"),
        )

    video_formats = [
        f
        for f in raw_formats
        if f.get("height") is not None and f.get("vcodec") not in [None, "none"]
    ]

    muxed_candidates = [
        f
        for f in video_formats
        if f.get("acodec") not in [None, "none", "unknown"]
        and (f.get("ext") == "mp4" or "mp4" in (f.get("container") or ""))
    ]

    best_muxed = None
    if muxed_candidates:
        best_muxed = max(
            muxed_candidates,
            key=lambda x: (x.get("height") or 0, x.get("tbr") or 0),
        )
        logger.info(
            "Best muxed mp4: %s %sx%s",
            best_muxed.get("format_id"),
            best_muxed.get("width"),
            best_muxed.get("height"),
        )

    mp4_formats = []
    seen_res = set()

    sorted_videos = sorted(
        video_formats,
        key=lambda x: (x.get("height") or 0, x.get("tbr") or 0),
        reverse=True,
    )

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
            logger.warning("Format %s has no URL, skipping", res_label)
            continue

        size = f.get("filesize") or f.get("filesize_approx")
        if size is not None and size <= 0:
            logger.warning(
                "Format %s has invalid filesize=%s, skipping", res_label, size
            )
            continue

        mp4_formats.append(
            {
                "resolution": res_label,
                "video_url": video_url,
                "audio_url": None
                if has_audio
                else (best_audio.get("url") if best_audio else None),
                "needs_mux": not has_audio,
                "height": h,
                "width": w,
                "real_res": f"{w}x{h}",
                "filesize": size,
                "format_note": f.get("format_note", ""),
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
            }
        )

    mp4_formats = mp4_formats[:12]

    mp3_formats = []
    for idx, f in enumerate(audio_only[:3], 1):
        quality = "HQ" if idx == 1 else f"Quality {idx}"
        mp3_formats.append(
            {
                "quality": quality,
                "audio_url": f.get("url"),
                "bitrate": f.get("abr") or f.get("tbr"),
            }
        )

    result: Dict[str, Any] = {
        "success": True,
        "mp4_formats": mp4_formats,
        "mp3_formats": mp3_formats,
    }

    if best_muxed is not None and best_muxed.get("url"):
        result["best_muxed"] = {
            "video_url": best_muxed.get("url"),
            "resolution": f"{best_muxed.get('width')}x{best_muxed.get('height')}",
            "ext": best_muxed.get("ext"),
            "filesize": best_muxed.get("filesize") or best_muxed.get("filesize_approx"),
        }

    if not mp4_formats and not mp3_formats:
        return {
            "success": False,
            "message": "No playable formats could be selected for this video.",
        }

    return result


def extract_video_logic(url: str) -> Dict[str, Any]:
    """
    Extract video info using yt-dlp with special handling for Instagram Reels and Shorts.
    """
    target_url = _normalize_url(url)

    try:
        logger.info("Starting extraction for URL: %s", target_url)

        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(target_url, download=False)

        if not info:
            logger.error("No metadata returned by yt-dlp")
            return {
                "success": False,
                "message": "Could not extract video information. Please try again.",
            }

        if "entries" in info and isinstance(info["entries"], list):
            if not info["entries"]:
                return {
                    "success": False,
                    "message": "No video entries were found.",
                }
            info = info["entries"][0]

        selected = _select_best_formats(info)
        if not selected.get("success"):
            return selected

        logger.info(
            "Extraction successful: %s video formats, %s audio formats",
            len(selected["mp4_formats"]),
            len(selected["mp3_formats"]),
        )

        duration_val = info.get("duration") or 0
        if duration_val and duration_val > 0:
            duration_str = info.get("duration_string") or f"{duration_val}s"
        else:
            duration_str = "unknown"

        base: Dict[str, Any] = {
            "success": True,
            "title": info.get("title", "Video"),
            "uploader": info.get("uploader")
            or info.get("channel")
            or info.get("uploader_id"),
            "duration": duration_str,
            "thumbnail": info.get("thumbnail"),
            "platform": info.get("extractor_key"),
            "view_count": info.get("view_count"),
            "like_count": info.get("like_count"),
        }
        base.update(
            {
                "mp4_formats": selected["mp4_formats"],
                "mp3_formats": selected["mp3_formats"],
                "best_muxed": selected.get("best_muxed"),
            }
        )

        return base

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)
        logger.error("DownloadError: %s", error_msg)

        if "Private video" in error_msg or "members-only" in error_msg:
            return {
                "success": False,
                "message": "This video is private or members-only and cannot be downloaded in public mode.",
            }
        elif "Video unavailable" in error_msg:
            return {
                "success": False,
                "message": "The video is unavailable or has been removed.",
            }
        elif "Sign in to confirm your age" in error_msg:
            return {
                "success": False,
                "message": "This video requires age verification and cannot be downloaded in public mode.",
            }
        elif "content isn't available" in error_msg:
            return {
                "success": False,
                "message": "This Instagram content is private or restricted.",
            }
        else:
            return {
                "success": False,
                "message": f"Download failed: {error_msg[:150]}",
            }

    except Exception as e:
        logger.error("Unexpected error: %s", str(e), exc_info=True)
        return {
            "success": False,
            "message": f"Unexpected error: {str(e)[:150]}",
        }


def sanitize_filename(filename: str, max_length: int = 50) -> str:
    """
    Clean a filename from illegal characters and emojis.
    """
    clean = re.sub(r"[^\x00-\x7F]+", "", filename)
    clean = re.sub(r'[\\/*?:"<>|]', "", clean)
    clean = " ".join(clean.split())
    clean = clean[:max_length].strip()
    return clean if clean else "video_download"


async def download_with_retry(url: str, output_path: str, max_retries: int = 3) -> bool:
    """
    Download a file using curl with simple retry backoff.
    """
    for attempt in range(max_retries):
        try:
            logger.info(
                "Download attempt %s/%s: %s...", attempt + 1, max_retries, url[:50]
            )

            cmd = [
                "curl",
                "-L",
                "-f",
                "--max-time",
                "120",
                "--retry",
                "3",
                "-o",
                output_path,
                url,
            ]

            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=150
            )

            if process.returncode == 0 and os.path.exists(output_path):
                file_size = os.path.getsize(output_path)
                if file_size > 0:
                    logger.info("Download successful: %s bytes", file_size)
                    return True
                logger.warning("Downloaded file has size 0")

            logger.warning(
                "Download failed (attempt %s): %s",
                attempt + 1,
                stderr.decode(errors="ignore")[:200],
            )

        except asyncio.TimeoutError:
            logger.warning("Download timeout on attempt %s", attempt + 1)
        except Exception as e:
            logger.error(
                "Download error on attempt %s: %s", attempt + 1, str(e)
            )

        if attempt < max_retries - 1:
            await asyncio.sleep(2**attempt)

    return False


@router.post("/mux")
async def mux_video(request: MuxRequest, background_tasks: BackgroundTasks):
    """
    Combine separate video and audio streams with FFmpeg and return a single MP4.
    """
    tmp_dir = tempfile.mkdtemp(prefix="videomux_")
    video_path = os.path.join(tmp_dir, "video.mp4")
    audio_path = os.path.join(tmp_dir, "audio.m4a")
    output_path = os.path.join(tmp_dir, "output.mp4")

    def cleanup() -> None:
        try:
            if os.path.exists(tmp_dir):
                import shutil

                shutil.rmtree(tmp_dir, ignore_errors=True)
                logger.info("Cleaned up temp dir: %s", tmp_dir)
        except Exception as e:
            logger.error("Cleanup error: %s", str(e))

    background_tasks.add_task(cleanup)

    try:
        logger.info("Starting mux for resolution: %s", request.resolution)

        if not request.video_url or not request.audio_url:
            raise HTTPException(
                status_code=400,
                detail="Video or audio URL is invalid or empty.",
            )

        logger.info("Downloading video and audio streams...")
        download_tasks = [
            download_with_retry(request.video_url, video_path),
            download_with_retry(request.audio_url, audio_path),
        ]

        results = await asyncio.gather(*download_tasks, return_exceptions=True)

        if not all(results):
            failed = []
            if not results[0]:
                failed.append("video")
            if not results[1]:
                failed.append("audio")

            raise HTTPException(
                status_code=503,
                detail=f"Failed to download {', '.join(failed)}. URLs might be expired.",
            )

        logger.info("Both files downloaded successfully. Starting FFmpeg mux...")

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            video_path,
            "-i",
            audio_path,
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-shortest",
            "-movflags",
            "+faststart",
            output_path,
        ]

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=300
            )
        except asyncio.TimeoutError:
            process.kill()
            raise HTTPException(
                status_code=504,
                detail="Muxing timeout (>5 minutes). The video is too large.",
            )

        if process.returncode != 0:
            error_output = stderr.decode("utf-8", errors="ignore")
            logger.error("FFmpeg error: %s", error_output)
            raise HTTPException(
                status_code=500,
                detail="Failed to merge video and audio. Try another resolution.",
            )

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise HTTPException(
                status_code=500,
                detail="Muxed output file is invalid or empty.",
            )

        clean_title = sanitize_filename(request.title)
        filename = f"{clean_title}_{request.resolution.replace(' ', '_')}.mp4"

        logger.info(
            "Mux successful: %s (%s bytes)", filename, os.path.getsize(output_path)
        )

        return FileResponse(
            output_path,
            media_type="video/mp4",
            filename=filename,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-cache",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Unexpected mux error: %s", str(e), exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Mux failed: {str(e)[:100]}",
        )


@router.post("/ytdl")
async def handle_ytdl(request: VideoRequest):
    """
    Endpoint for video information extraction (public mode).
    """
    try:
        result = extract_video_logic(request.url)
        if not result.get("success"):
            return JSONResponse(status_code=400, content=result)
        return result
    except Exception as e:
        logger.error("YTDL endpoint error: %s", str(e), exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": f"Server error: {str(e)[:100]}",
            },
        )


@router.get("/health")
async def health_check():
    """
    Simple health check endpoint for monitoring.
    """
    return {
        "status": "healthy",
        "service": "video-extractor",
        "ffmpeg": subprocess.run(
            ["ffmpeg", "-version"], capture_output=True
        ).returncode
        == 0,
    }