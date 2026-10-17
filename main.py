# main.py
import os
import sys
import time
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Response, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

MODULE_STATUS = {"modules": {}}


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
        },
        "features": {
            "telegram": bool(os.environ.get("TELEGRAM_API_ID") and os.environ.get("TELEGRAM_API_HASH")),
            "gemini": bool(os.environ.get("GEMINI_API_KEY")),
        }
    }


def load_modules(app_instance):
    modules_to_load = ["telegram", "token_api", "proxy", "hf", "torrent"]
    for mod_name in modules_to_load:
        start_ts = time.perf_counter()
        try:
            module = __import__(f"modules.{mod_name}", fromlist=["router"])
            if hasattr(module, "router"):
                app_instance.include_router(module.router)
                elapsed = (time.perf_counter() - start_ts) * 1000
                MODULE_STATUS["modules"][mod_name] = "Success"
                logger.info(f"Loaded module: {mod_name} ({elapsed:.0f}ms)")
            else:
                MODULE_STATUS["modules"][mod_name] = "Error: Missing Router"
                logger.error(f"Module {mod_name} has no router")
        except Exception as e:
            MODULE_STATUS["modules"][mod_name] = f"Error: {str(e)}"
            logger.error(f"Failed to load {mod_name}: {e}")


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    start_ts = time.perf_counter()
    load_modules(app_instance)
    elapsed = (time.perf_counter() - start_ts) * 1000
    logger.info(f"Server started with modules: {list(MODULE_STATUS['modules'].keys())} ({elapsed:.0f}ms)")
    yield
    logger.info("Server shutting down...")


app = FastAPI(
    title="Wartha Sensei API",
    description="Backend for WarthaCV - Torrent Downloader, Telegram Integration, Token API",
    version="1.0.0",
    lifespan=lifespan
)


def _parse_allowed_origins() -> list:
    raw = os.environ.get("ALLOWED_ORIGINS", "*")
    if not raw:
        parts = ["*"]
    else:
        parts = [origin.strip() for origin in raw.split(",") if origin.strip()]
        if not parts:
            parts = ["*"]

    defaults = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
        "https://warthasensei.web.app",
        "https://warthasensei.firebaseapp.com",
    ]

    if parts == ["*"]:
        for origin in defaults:
            if origin not in parts:
                parts.append(origin)

    return parts


ALLOWED_ORIGINS = _parse_allowed_origins()
logger.info(f"Allowed CORS origins: {ALLOWED_ORIGINS}")


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "Content-Range",
        "Accept-Ranges",
        "Content-Length",
        "Content-Type",
        "Content-Disposition",
        "X-Subtitle-Format",
        "X-Subtitle-Track",
        "X-Subtitle-Language",
        "X-Subtitle-Codec-Source"
    ],
)


@app.middleware("http")
async def ensure_cors_headers(request: Request, call_next):
    if request.method == "OPTIONS":
        response = Response(status_code=204)
    else:
        response = await call_next(request)

    origin = request.headers.get("origin")
    if origin and "access-control-allow-origin" not in {k.lower() for k in response.headers.keys()}:
        if "*" in ALLOWED_ORIGINS or origin in ALLOWED_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin if "*" not in ALLOWED_ORIGINS else "*"
            response.headers["Access-Control-Allow-Methods"] = "*"
            response.headers["Access-Control-Allow-Headers"] = "*"

    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception on {request.method} {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error. Please try again later."}
    )


@app.head("/")
async def head_root():
    return Response(status_code=200)


@app.head("/health")
async def head_health():
    return Response(status_code=200)


@app.get("/")
def home():
    return {
        "server": "Online",
        "status": "Ready",
        "version": "1.0.0",
        "modules": MODULE_STATUS
    }


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "modules": list(MODULE_STATUS["modules"].keys()),
        "module_status": MODULE_STATUS["modules"]
    }


@app.get("/config")
def get_frontend_config_endpoint():
    return get_frontend_config()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*"
    )