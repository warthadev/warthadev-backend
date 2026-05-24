# main.py
import os
import sys
import subprocess
import importlib
from fastapi import FastAPI
import uvicorn
from fastapi.middleware.cors import CORSMiddleware

# Setup Path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

app = FastAPI(title="Wartha Sensei API")

# CORS
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
    tools = {"ffmpeg": ["ffmpeg", "-version"], "p7zip": ["7z", "--help"]}
    for name, cmd in tools.items():
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            status_module["system_tools"][name] = "Installed"
        except:
            status_module["system_tools"][name] = "Not Found"

def load_modules():
    """Import router dari modules"""
    modules_to_load = ["ytdl", "telegram"]   # pastikan telegram ada
    
    for mod_name in modules_to_load:
        try:
            module = importlib.import_module(f"modules.{mod_name}")
            if hasattr(module, "router"):
                app.include_router(module.router)
                status_module["modules"][mod_name] = "Success"
                print(f"✅ Loaded module: {mod_name}")
            else:
                status_module["modules"][mod_name] = "Error: Missing Router"
                print(f"❌ Module {mod_name} has no router")
        except Exception as e:
            status_module["modules"][mod_name] = f"Error: {str(e)}"
            print(f"❌ Failed to load {mod_name}: {e}")

@app.on_event("startup")
async def startup():
    check_system()
    load_modules()
    # Start Telegram client
    try:
        from modules.telegram import start_client
        await start_client()
        print("✅ Telegram client started")
    except Exception as e:
        print(f"⚠️ Telegram client error: {e}")
    print("✅ Server started with modules:", list(status_module["modules"].keys()))

@app.on_event("shutdown")
async def shutdown():
    try:
        from modules.telegram import shutdown_client
        await shutdown_client()
    except:
        pass

@app.get("/")
def home():
    return {
        "server": "Online",
        "status": "Ready",
        "details": status_module
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        "main:app", 
        host="0.0.0.0", 
        port=port, 
        proxy_headers=True, 
        forwarded_allow_ips="*"
    )