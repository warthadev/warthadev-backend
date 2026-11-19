import logging
import os

import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from modules.chat import router as chat_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_frontend_config():
    return {
        "firebase": {
            "apiKey": os.environ.get("FIREBASE_API_KEY", ""),
            "authDomain": os.environ.get("FIREBASE_AUTH_DOMAIN", ""),
            "projectId": os.environ.get("FIREBASE_PROJECT_ID", ""),
            "storageBucket": os.environ.get("FIREBASE_STORAGE_BUCKET", ""),
            "messagingSenderId": os.environ.get("FIREBASE_MESSAGING_SENDER_ID", ""),
            "appId": os.environ.get("FIREBASE_APP_ID", ""),
            "measurementId": os.environ.get("FIREBASE_MEASUREMENT_ID", ""),
        }
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("Chat-only API started")
    yield
    logger.info("Chat-only API shutting down")


app = FastAPI(
    title="Wartha Sensei Chat API",
    description="Authenticated text chat and account-isolated model credentials.",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(chat_router)


def _parse_allowed_origins() -> list[str]:
    raw = os.environ.get("ALLOWED_ORIGINS", "*")
    if raw.strip() == "*":
        return [
            "*",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
            "https://warthasensei.web.app",
            "https://warthasensei.firebaseapp.com",
        ]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


ALLOWED_ORIGINS = _parse_allowed_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "PUT", "POST", "DELETE", "HEAD", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def ensure_cors_headers(request: Request, call_next):
    response = Response(status_code=204) if request.method == "OPTIONS" else await call_next(request)
    origin = request.headers.get("origin")
    if origin and "access-control-allow-origin" not in {key.lower() for key in response.headers}:
        if "*" in ALLOWED_ORIGINS or origin in ALLOWED_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = "*" if "*" in ALLOWED_ORIGINS else origin
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "Internal server error. Please try again later."})


@app.head("/")
async def head_root():
    return Response(status_code=200)


@app.head("/health")
async def head_health():
    return Response(status_code=200)


@app.get("/")
def home():
    return {"server": "Online", "status": "Ready", "version": "2.0.0", "modules": ["chat"]}


@app.get("/health")
def health_check():
    return {"status": "healthy", "modules": ["chat"]}


@app.get("/config")
def get_frontend_config_endpoint():
    return get_frontend_config()


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 7860)), proxy_headers=True, forwarded_allow_ips="*", workers=1)
