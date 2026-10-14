# modules/extractaudio.py
import os
import re
import time
import shutil
import logging
import subprocess
import tempfile
from typing import Optional
from urllib.parse import urlparse
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, validator
import httpx

from . import diskcache as dc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audio", tags=["audio"])

DOWNLOAD_TIMEOUT = 300
CONVERT_TIMEOUT = 300
MAX_AUDIO_BYTES = 200 * 1024 * 1024
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

STREAM_EXTS = (".m3u8", ".mpd")
PROTOCOL_WHITELIST = "file,http,https,tcp,tls,crypto,data"


class ConvertRequest(BaseModel):
    audio_url: str
    title: str
    artist: Optional[str] = ""
    album: Optional[str] = ""
    year: Optional[str] = ""
    comment: Optional[str] = ""
    cover_url: Optional[str] = ""
    cover_size: Optional[int] = 500
    bitrate: Optional[str] = "320k"

    @validator("audio_url")
    def validate_audio_url(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("audio_url must not be empty")
        return v.strip()

    @validator("title")
    def validate_title(cls, v: str) -> str:
        if not v or not v.strip():
            return "audio"
        return v.strip()

    @validator("bitrate")
    def validate_bitrate(cls, v: str) -> str:
        if not v or not v.strip():
            return "320k"
        value = v.strip().lower()
        if value not in ("128k", "192k", "256k", "320k"):
            return "320k"
        return value

    @validator("cover_size")
    def validate_cover_size(cls, v: Optional[int]) -> int:
        try:
            size = int(v) if v is not None else 500
        except Exception:
            size = 500
        if size < 100:
            size = 100
        if size > 1500:
            size = 1500
        return size


def _sanitize_filename(name: str, max_length: int = 80) -> str:
    clean = re.sub(r"[^\x00-\x7F]+", "", name)
    clean = re.sub(r'[\\/*?:"<>|]', "", clean)
    clean = " ".join(clean.split())
    clean = clean[:max_length].strip()
    return clean if clean else "audio"


def _check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _is_stream_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        path = (parsed.path or "").lower()
        return path.endswith(STREAM_EXTS)
    except Exception:
        return False


async def _download_direct(url: str, output_path: str) -> bool:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30.0, read=DOWNLOAD_TIMEOUT, write=30.0, pool=30.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    logger.error(f"Audio download failed: HTTP {resp.status_code}")
                    return False

                content_length = resp.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > MAX_AUDIO_BYTES:
                            logger.error(f"Audio too large: {content_length} bytes")
                            return False
                    except Exception:
                        pass

                total_written = 0
                with open(output_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                        total_written += len(chunk)
                        if total_written > MAX_AUDIO_BYTES:
                            logger.error(f"Audio exceeded limit: {total_written}")
                            return False
                        f.write(chunk)

        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except Exception as e:
        logger.error(f"Audio download error: {e}")
        return False


async def _download_cover(url: str, output_path: str) -> bool:
    if not url:
        return False
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return False
            with open(output_path, "wb") as f:
                f.write(resp.content)
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except Exception as e:
        logger.warning(f"Cover download error: {e}")
        return False


def _process_cover(input_path: str, output_path: str, size: int) -> bool:
    try:
        from PIL import Image
        img = Image.open(input_path).convert("RGB")
        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        img = img.resize((size, size), Image.LANCZOS)
        img.save(output_path, format="JPEG", quality=90, optimize=True)
        return os.path.exists(output_path)
    except Exception as e:
        logger.warning(f"Cover process error: {e}")
        return False


def _probe_duration(file_path: str) -> Optional[float]:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                file_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            value = result.stdout.strip()
            if value:
                return float(value)
    except Exception:
        pass
    return None


def _convert_to_mp3(input_source: str, mp3_path: str, bitrate: str, is_stream: bool = False) -> bool:
    if not _check_ffmpeg():
        logger.error("ffmpeg not available")
        return False

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
    ]

    if is_stream:
        cmd.extend([
            "-protocol_whitelist", PROTOCOL_WHITELIST,
            "-user_agent", USER_AGENT,
            "-headers", f"User-Agent: {USER_AGENT}\r\n",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
        ])

    cmd.extend([
        "-i", input_source,
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", bitrate,
        "-ar", "44100",
        "-ac", "2",
        "-id3v2_version", "3",
        "-write_id3v1", "1",
        mp3_path,
    ])

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=CONVERT_TIMEOUT)
        if result.returncode != 0:
            err = result.stderr.decode("utf-8", errors="ignore")[:500] if isinstance(result.stderr, bytes) else str(result.stderr)[:500]
            logger.error(f"ffmpeg convert failed: {err}")
            return False
        return os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 0
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg convert timed out")
        return False
    except Exception as e:
        logger.error(f"ffmpeg convert error: {e}")
        return False


def _embed_id3(mp3_path: str, req: ConvertRequest, cover_bytes: Optional[bytes] = None) -> bool:
    try:
        from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TDRC, COMM
        from mutagen.id3 import error as ID3Error

        try:
            tags = ID3(mp3_path)
        except ID3Error:
            tags = ID3()

        tags.delall("TIT2")
        tags.delall("TPE1")
        tags.delall("TALB")
        tags.delall("TDRC")
        tags.delall("COMM")
        tags.delall("APIC")

        tags.add(TIT2(encoding=3, text=str(req.title)))

        if req.artist:
            tags.add(TPE1(encoding=3, text=str(req.artist)))

        if req.album:
            tags.add(TALB(encoding=3, text=str(req.album)))

        if req.year:
            tags.add(TDRC(encoding=3, text=str(req.year)))

        if req.comment:
            tags.add(COMM(encoding=3, lang="eng", desc="Source", text=str(req.comment)))

        if cover_bytes:
            tags.add(APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,
                desc="Cover",
                data=cover_bytes,
            ))

        tags.save(mp3_path, v2_version=3)
        return True
    except Exception as e:
        logger.warning(f"ID3 embed error: {e}")
        return False


@router.post("/convert")
async def convert_audio(req: ConvertRequest):
    if not _check_ffmpeg():
        raise HTTPException(500, "ffmpeg not installed on server")

    dc.cleanup_disk(force=False)
    dc.log_disk_stats("[audio/convert] before ")

    tmp_dir = tempfile.mkdtemp(prefix="audio_")
    raw_path = os.path.join(tmp_dir, "input.raw")
    mp3_path = os.path.join(tmp_dir, "output.mp3")
    cover_in = os.path.join(tmp_dir, "cover_in.jpg")
    cover_out = os.path.join(tmp_dir, "cover_out.jpg")

    is_stream = _is_stream_url(req.audio_url)
    input_source = req.audio_url

    try:
        logger.info(f"audio/convert start: {req.audio_url[:80]} stream={is_stream}")

        if not is_stream:
            ok = await _download_direct(req.audio_url, raw_path)
            if not ok:
                raise HTTPException(502, "Failed to download audio source")

            size = os.path.getsize(raw_path)
            logger.info(f"audio downloaded: {size} bytes")

            if size < 4096:
                logger.error(f"audio file too small: {size} bytes")
                raise HTTPException(502, "Audio source too small or invalid")

            input_source = raw_path
        else:
            logger.info("HLS/DASH stream detected, using ffmpeg direct fetch")

        converted = _convert_to_mp3(input_source, mp3_path, req.bitrate, is_stream=is_stream)
        if not converted:
            raise HTTPException(500, "Failed to convert audio to MP3")

        logger.info(f"mp3 converted: {os.path.getsize(mp3_path)} bytes")

        cover_bytes: Optional[bytes] = None
        if req.cover_url:
            got_cover = await _download_cover(req.cover_url, cover_in)
            if got_cover:
                processed = _process_cover(cover_in, cover_out, req.cover_size or 500)
                if processed:
                    try:
                        with open(cover_out, "rb") as f:
                            cover_bytes = f.read()
                        logger.info(f"cover ready: {len(cover_bytes)} bytes")
                    except Exception:
                        cover_bytes = None

        _embed_id3(mp3_path, req, cover_bytes)

        safe_title = _sanitize_filename(req.title)
        if req.artist:
            safe_artist = _sanitize_filename(req.artist)
            filename = f"{safe_artist} - {safe_title}.mp3"
        else:
            filename = f"{safe_title}.mp3"

        logger.info(f"audio/convert done: {filename}")

        return FileResponse(
            mp3_path,
            media_type="audio/mpeg",
            filename=filename,
        )
    except HTTPException:
        try:
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        raise
    except Exception as e:
        logger.error(f"audio/convert unexpected error: {e}")
        try:
            if os.path.exists(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        raise HTTPException(500, f"Convert failed: {str(e)[:200]}")


@router.get("/health")
async def health():
    return {
        "status": "healthy",
        "service": "audio-converter",
        "ffmpeg": _check_ffmpeg(),
    }
