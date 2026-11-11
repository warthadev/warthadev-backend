import os
import sys
import time
import subprocess
import requests

def run_colab_worker(ngrok_token=None, model_name=None, backend_url=None):
    token = ngrok_token or os.environ.get("NGROK_AUTHTOKEN") or "2qAbzTpVWixz118VmWvJQ9mvt99_2FMTEH9iopeNo5JW3ADmH"
    model = model_name or os.environ.get("MODEL_NAME") or "hf.co/xtools-at/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic"

    os.system("fuser -k 11434/tcp > /dev/null 2>&1")
    os.system("pkill ollama > /dev/null 2>&1")

    try:
        from pyngrok import ngrok
        ngrok.kill()
    except ImportError:
        os.system("pip install pyngrok requests -q")
        from pyngrok import ngrok

    ngrok.set_auth_token(token)

    if subprocess.call(["which", "ollama"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        os.system("curl -fsSL https://ollama.com/install.sh | sh")

    subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    for _ in range(15):
        try:
            res = requests.get("http://localhost:11434", timeout=1)
            if res.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)

    os.system(f"ollama pull {model}")

    tunnel = ngrok.connect(11434, "http")
    public_url = tunnel.public_url

    print("WORKER ACTIVE")
    print(f"URL: {public_url}")
    print(f"Base URL: {public_url}/v1")
    print(f"Model: {model}")

    if backend_url:
        try:
            requests.post(
                f"{backend_url.rstrip('/')}/api/config/update-worker",
                json={"colab_url": public_url},
                timeout=5
            )
        except Exception:
            pass

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass

    return public_url

if __name__ == "__main__":
    run_colab_worker()
  
