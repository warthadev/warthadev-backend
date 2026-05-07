import os
import sys
import subprocess
import importlib
from fastapi import FastAPI
import uvicorn
from fastapi.middleware.cors import CORSMiddleware

# Setup Path: Memastikan folder 'modules' terbaca sebagai package
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

app = FastAPI(title="Wartha Sensei API")

# Konfigurasi CORS Paling Agresif
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

status_module = {"modules": {}, "system_tools": {}}

def check_system():
    """Cek ketersediaan binary FFmpeg dan 7z di OS."""
    tools = {"ffmpeg": ["ffmpeg", "-version"], "p7zip": ["7z", "--help"]}
    for name, cmd in tools.items():
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            status_module["system_tools"][name] = "Installed"
        except:
            status_module["system_tools"][name] = "Not Found"

def load_modules():
    """Import router dari ytdl.py dan igdl.py secara dinamis."""
    # Pastikan folder di GitHub/Local lo namanya 'modules'
    for mod_name in ["ytdl", "igdl"]:
        try:
            # UPDATE: Menggunakan 'modules' bukan 'module'
            module = importlib.import_module(f"modules.{mod_name}")
            if hasattr(module, "router"):
                app.include_router(module.router)
                status_module["modules"][mod_name] = "Success"
            else:
                status_module["modules"][mod_name] = "Error: Missing Router Object"
        except Exception as e:
            status_module["modules"][mod_name] = f"Error: {str(e)}"

@app.on_event("startup")
async def startup():
    check_system()
    load_modules()

@app.get("/")
def home():
    """Root endpoint untuk pengecekan status via browser."""
    return {
        "server": "Online",
        "status": "Ready",
        "details": status_module
    }

if __name__ == "__main__":
    # Port 7860 adalah standar Hugging Face Spaces
    uvicorn.run(
        "main:app", 
        host="0.0.0.0", 
        port=7860, 
        proxy_headers=True, 
        forwarded_allow_ips="*"
    )
