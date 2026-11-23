from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/router", tags=["router"])


async def _forward(request: Request, path: str, method: str) -> JSONResponse:
    return JSONResponse(
        status_code=410,
        content={
            "detail": "The shared router proxy is disabled. Use the authenticated, account-scoped Agent provider gateway."
        },
    )


@router.get("/models")
async def models(request: Request):
    return await _forward(request, "models", "GET")


@router.post("/chat/completions")
async def chat_completions(request: Request):
    return await _forward(request, "chat/completions", "POST")
