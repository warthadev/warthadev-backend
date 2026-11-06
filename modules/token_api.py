# modules/token_api.py
import os
import httpx
import logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/token", tags=["token"])

GEMINI_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


class ModelResponse(BaseModel):
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    supported_generation_methods: Optional[List[str]] = None


def _get_api_key() -> str:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="Gemini API key not configured")
    return api_key


@router.get("/gemini/models")
async def get_gemini_models():
    api_key = _get_api_key()

    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"

    try:
        async with httpx.AsyncClient(timeout=GEMINI_TIMEOUT) as client:
            response = await client.get(url)
            data = response.json()

            if response.status_code != 200:
                error_detail = "Unknown error"
                if isinstance(data, dict):
                    error_obj = data.get("error") or {}
                    error_detail = error_obj.get("message") or error_detail
                raise HTTPException(status_code=response.status_code, detail=error_detail)

            raw_models = []
            if isinstance(data, dict):
                raw_models = data.get("models") or []
            if not isinstance(raw_models, list):
                raw_models = []

            models = []
            for model in raw_models:
                if not isinstance(model, dict):
                    continue
                models.append(ModelResponse(
                    name=model.get("name", ""),
                    display_name=model.get("displayName", ""),
                    description=model.get("description", ""),
                    supported_generation_methods=model.get("supportedGenerationMethods", [])
                ))

            return {"models": models, "total": len(models)}
    except HTTPException:
        raise
    except httpx.ConnectTimeout:
        raise HTTPException(status_code=504, detail="Failed to connect to Gemini API")
    except httpx.ReadTimeout:
        raise HTTPException(status_code=504, detail="Gemini API did not respond in time")
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Cannot reach Gemini API")
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=str(e))
    except httpx.HTTPError as e:
        logger.error(f"Gemini HTTP error: {e}")
        raise HTTPException(status_code=502, detail="Gemini API request failed")
    except Exception as e:
        logger.error(f"Unexpected error in get_gemini_models: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error while fetching models")