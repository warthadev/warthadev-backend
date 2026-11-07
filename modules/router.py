import os

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter(prefix="/router", tags=["router"])
UPSTREAM = os.environ.get(
    "ROUTER_UPSTREAM",
    "https://20128-agq2firibhstlf6sr7k3ydjoim.instabox.tech/v1",
).rstrip("/")
TIMEOUT = httpx.Timeout(connect=20.0, read=600.0, write=60.0, pool=20.0)


def _auth_headers(request: Request) -> dict[str, str]:
    value = request.headers.get("authorization", "").strip()
    return {"Authorization": value} if value else {}


async def _forward(request: Request, path: str, method: str) -> Response:
    body = await request.body() if method != "GET" else None
    headers = _auth_headers(request)
    if body is not None:
        headers["Content-Type"] = request.headers.get("content-type", "application/json")
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
            upstream = await client.request(
                method,
                f"{UPSTREAM}/{path}",
                headers=headers,
                content=body,
            )
        response_headers = {}
        if upstream.headers.get("content-type"):
            response_headers["Content-Type"] = upstream.headers["content-type"]
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers,
        )
    except httpx.HTTPError as exc:
        message = str(exc).replace('"', "'")
        return Response(
            content=f'{{"error":"router upstream unavailable: {message}"}}',
            status_code=502,
            media_type="application/json",
        )


@router.get("/models")
async def models(request: Request):
    return await _forward(request, "models", "GET")


@router.post("/chat/completions")
async def chat_completions(request: Request):
    return await _forward(request, "chat/completions", "POST")
