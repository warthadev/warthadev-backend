# modules/proxy.py
import os
import json
import time
import base64
import hashlib
import logging
import urllib.parse
from typing import Optional
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse, Response, RedirectResponse
from pydantic import BaseModel
from cryptography.fernet import Fernet, InvalidToken
import httpx

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/proxy", tags=["proxy"])

CHUNK_SIZE = 256 * 1024
STREAM_TIMEOUT = httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=30.0)


def _safe_unquote(value: str) -> str:
    if not value:
        return value
    try:
        decoded = urllib.parse.unquote(value)
        if decoded and decoded != value:
            return decoded
        return value
    except Exception:
        return value


def _sanitize_filename(name: str) -> str:
    if not name:
        return "download"
    cleaned = name.replace("\r", "").replace("\n", "")
    cleaned = "".join(ch for ch in cleaned if ch.isprintable() or ch == " ")
    cleaned = cleaned.replace('"', "")
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    cleaned = " ".join(cleaned.split())
    cleaned = cleaned.strip()
    if not cleaned:
        return "download"
    if len(cleaned) > 200:
        cleaned = cleaned[:200]
    return cleaned


def _build_content_disposition(filename: str) -> str:
    safe_ascii = filename.encode("ascii", errors="ignore").decode("ascii")
    if not safe_ascii:
        safe_ascii = "download"
    safe_ascii = _sanitize_filename(safe_ascii)
    encoded = urllib.parse.quote(filename, safe="")
    return f"attachment; filename=\"{safe_ascii}\"; filename*=UTF-8''{encoded}"


def _sanitize_header_value(value: str) -> Optional[str]:
    if not value:
        return None
    if "\r" in value or "\n" in value:
        return None
    return value.strip()


def _validate_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "invalid url scheme")
    if not parsed.netloc:
        raise HTTPException(400, "invalid url host")
    return url


def _validate_referer(referer: Optional[str]) -> Optional[str]:
    if not referer:
        return None
    parsed = urllib.parse.urlparse(referer)
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.netloc:
        return None
    return referer


def _build_forward_headers(request: Request, token: Optional[str]) -> dict:
    headers = {}
    ua = request.headers.get("user-agent")
    if ua:
        safe_ua = _sanitize_header_value(ua)
        if safe_ua:
            headers["User-Agent"] = safe_ua
    referer = _validate_referer(request.query_params.get("referer"))
    if referer:
        headers["Referer"] = referer
    if token:
        safe_token = _sanitize_header_value(token)
        if safe_token:
            headers["Authorization"] = f"Bearer {safe_token}"
    range_header = request.headers.get("range")
    if range_header:
        safe_range = _sanitize_header_value(range_header)
        if safe_range:
            headers["Range"] = safe_range
    if_none_match = request.headers.get("if-none-match")
    if if_none_match:
        safe_inm = _sanitize_header_value(if_none_match)
        if safe_inm:
            headers["If-None-Match"] = safe_inm
    return headers


def _forward_response_headers(upstream: httpx.Response) -> dict:
    out = {}
    passthrough = [
        "content-type",
        "content-length",
        "content-range",
        "accept-ranges",
        "etag",
        "last-modified",
    ]
    for key in passthrough:
        value = upstream.headers.get(key)
        if value:
            out[key.title()] = value
    return out


def _build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=STREAM_TIMEOUT, follow_redirects=True)


def _get_target_url(request: Request) -> tuple:
    url = request.query_params.get("url")
    if not url:
        raise HTTPException(400, "url is required")

    url = _validate_url(url)

    filename = request.query_params.get("filename") or ""
    if filename:
        filename = _safe_unquote(filename)

    if not filename:
        parsed = urllib.parse.urlparse(url)
        filename = os.path.basename(parsed.path) or "download"
        filename = _safe_unquote(filename)

    filename = _sanitize_filename(filename)

    token = request.query_params.get("token") or None

    return url, filename, token


@router.head("/download", include_in_schema=False)
async def proxy_download_head(request: Request):
    url, filename, token = _get_target_url(request)
    forward_headers = _build_forward_headers(request, token)

    client = _build_client()
    try:
        try:
            upstream = await client.head(url, headers=forward_headers)
        except Exception as exc:
            raise HTTPException(502, f"upstream error: {exc}")

        try:
            headers = _forward_response_headers(upstream)
            headers["Content-Disposition"] = _build_content_disposition(filename)
            return Response(status_code=upstream.status_code, headers=headers)
        finally:
            try:
                await upstream.aclose()
            except Exception:
                pass
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


@router.get("/download")
async def proxy_download_get(request: Request):
    url, filename, token = _get_target_url(request)
    forward_headers = _build_forward_headers(request, token)

    client = _build_client()

    try:
        req = client.build_request("GET", url, headers=forward_headers)
        upstream = await client.send(req, stream=True)
    except Exception as exc:
        try:
            await client.aclose()
        except Exception:
            pass
        raise HTTPException(502, f"upstream error: {exc}")

    if upstream.status_code >= 400:
        try:
            await upstream.aclose()
        finally:
            try:
                await client.aclose()
            except Exception:
                pass
        raise HTTPException(upstream.status_code, "upstream returned error")

    response_headers = _forward_response_headers(upstream)
    response_headers["Content-Disposition"] = _build_content_disposition(filename)
    response_headers["Cache-Control"] = "no-cache"

    async def stream_body():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=CHUNK_SIZE):
                yield chunk
        finally:
            try:
                await upstream.aclose()
            except Exception:
                pass
            try:
                await client.aclose()
            except Exception:
                pass

    status_code = 206 if upstream.status_code == 206 else 200

    return StreamingResponse(
        stream_body(),
        status_code=status_code,
        media_type=response_headers.get("Content-Type", "application/octet-stream"),
        headers=response_headers,
    )


HF_AUTH_HOSTS = ("huggingface.co", "hf.co")
STREAM_TTL_DEFAULT_H = int(os.environ.get("STREAM_TICKET_TTL_HOURS", "12"))
STREAM_TTL_MAX_H = int(os.environ.get("STREAM_TICKET_TTL_MAX_HOURS", "168"))
MAX_REDIRECT_HOPS = 6

_secret = os.environ.get("STREAM_TICKET_SECRET")
if not _secret:
    _secret = base64.urlsafe_b64encode(os.urandom(32)).decode()
    logger.warning(
        "STREAM_TICKET_SECRET is not set: stream links will stop working "
        "whenever this Space restarts. Set it in the Space secrets."
    )
_fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(_secret.encode()).digest()))


def _is_hf_host(host: Optional[str]) -> bool:
    host = (host or "").lower()
    return any(host == h or host.endswith("." + h) for h in HF_AUTH_HOSTS)


def _is_hf_auth_host(host: Optional[str]) -> bool:
    return (host or "").lower() in HF_AUTH_HOSTS


class StreamLinkBody(BaseModel):
    url: str
    token: Optional[str] = None
    filename: Optional[str] = None
    ttl_hours: Optional[int] = None


@router.post("/stream-link")
async def create_stream_link(body: StreamLinkBody):
    url = _validate_url(body.url)
    if not _is_hf_auth_host(urllib.parse.urlparse(url).hostname):
        raise HTTPException(400, "only huggingface.co urls are supported")

    token = _sanitize_header_value(body.token or "") or None
    ttl_h = body.ttl_hours or STREAM_TTL_DEFAULT_H
    ttl_h = max(1, min(ttl_h, STREAM_TTL_MAX_H))
    exp = int(time.time()) + ttl_h * 3600

    payload = json.dumps({"u": url, "t": token, "x": exp}).encode()
    ticket = _fernet.encrypt(payload).decode()

    name = body.filename or os.path.basename(urllib.parse.urlparse(url).path) or "video"
    name = _sanitize_filename(_safe_unquote(name))
    return {
        "path": f"/proxy/stream/{ticket}/{urllib.parse.quote(name, safe='')}",
        "expires_in": ttl_h * 3600,
    }


def _read_ticket(ticket: str) -> dict:
    try:
        data = json.loads(_fernet.decrypt(ticket.encode()))
    except (InvalidToken, ValueError):
        raise HTTPException(403, "invalid stream link")
    if int(data.get("x", 0)) < time.time():
        raise HTTPException(410, "stream link expired")
    return data


@router.api_route("/stream/{ticket}/{filename:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def proxy_stream(ticket: str, filename: str, request: Request):
    data = _read_ticket(ticket)
    token = data.get("t")
    current = data["u"]
    method = request.method

    base_headers = _build_forward_headers(request, None)
    client = httpx.AsyncClient(timeout=STREAM_TIMEOUT, follow_redirects=False)

    async def _close_client():
        try:
            await client.aclose()
        except Exception:
            pass

    upstream = None
    try:
        for _ in range(MAX_REDIRECT_HOPS):
            headers = dict(base_headers)
            if token and _is_hf_auth_host(urllib.parse.urlparse(current).hostname):
                headers["Authorization"] = f"Bearer {token}"

            req = client.build_request(method, current, headers=headers)
            upstream = await client.send(req, stream=True)

            if upstream.status_code in (301, 302, 303, 307, 308):
                location = upstream.headers.get("location")
                await upstream.aclose()
                upstream = None
                if not location:
                    raise HTTPException(502, "upstream redirect without location")
                nxt = urllib.parse.urljoin(current, location)
                host = urllib.parse.urlparse(nxt).hostname
                if _is_hf_auth_host(host):
                    current = nxt
                    continue
                if not _is_hf_host(host):
                    raise HTTPException(502, "unexpected redirect target")
                await _close_client()
                return RedirectResponse(
                    nxt, status_code=302, headers={"Cache-Control": "no-store"}
                )
            break
        else:
            raise HTTPException(502, "too many redirects")
    except HTTPException:
        if upstream is not None:
            await upstream.aclose()
        await _close_client()
        raise
    except Exception as exc:
        await _close_client()
        raise HTTPException(502, f"upstream error: {exc}")

    if upstream.status_code >= 400:
        code = upstream.status_code
        await upstream.aclose()
        await _close_client()
        raise HTTPException(code, "upstream returned error")

    response_headers = _forward_response_headers(upstream)
    response_headers["Cache-Control"] = "no-store"

    if method == "HEAD":
        status = upstream.status_code
        await upstream.aclose()
        await _close_client()
        return Response(status_code=status, headers=response_headers)

    async def stream_body():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=CHUNK_SIZE):
                yield chunk
        finally:
            try:
                await upstream.aclose()
            except Exception:
                pass
            await _close_client()

    return StreamingResponse(
        stream_body(),
        status_code=206 if upstream.status_code == 206 else 200,
        media_type=response_headers.get("Content-Type", "application/octet-stream"),
        headers=response_headers,
    )