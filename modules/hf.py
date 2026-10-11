# modules/hf.py
import os
import asyncio
import logging
import time
import hashlib
import urllib.parse
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, validator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hf", tags=["hf"])

VALID_REPO_TYPES = {"dataset", "model", "space"}

_FOLDER_CACHE: Dict[str, Dict[str, Any]] = {}
_FOLDER_CACHE_TTL = 60


def _get_token_key(token: str) -> str:
    if not token:
        return "anon"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


class HFBulkRequest(BaseModel):
    repo_id: str
    repo_type: str
    token: str

    @validator("repo_id")
    def validate_repo_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("repo_id must not be empty")
        return v.strip()

    @validator("repo_type")
    def validate_repo_type(cls, v: str) -> str:
        if v not in VALID_REPO_TYPES:
            raise ValueError(f"repo_type must be one of {VALID_REPO_TYPES}")
        return v

    @validator("token")
    def validate_token(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("token must not be empty")
        return v.strip()


class HFListFoldersRequest(HFBulkRequest):
    pass


class HFCreateFolderRequest(HFBulkRequest):
    folder_path: str

    @validator("folder_path")
    def validate_folder(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("folder_path must not be empty")
        return v.strip().strip("/")


class HFRenameRequest(HFBulkRequest):
    old_path: str
    new_path: str

    @validator("old_path", "new_path")
    def validate_paths(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("path must not be empty")
        return v.strip()


class HFMoveRequest(HFBulkRequest):
    src_path: str
    dst_path: str

    @validator("src_path", "dst_path")
    def validate_paths(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("path must not be empty")
        return v.strip()


class HFMoveItem(BaseModel):
    src_path: str
    dst_path: str

    @validator("src_path", "dst_path")
    def validate_paths(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("path must not be empty")
        return v.strip()


class HFMoveBulkRequest(HFBulkRequest):
    moves: List[HFMoveItem]

    @validator("moves")
    def validate_moves(cls, v: List[HFMoveItem]) -> List[HFMoveItem]:
        if not v:
            raise ValueError("moves must not be empty")
        return v


class HFMoveBulkAnyRequest(HFBulkRequest):
    src_paths: List[str]
    dst_folder: str = ""
    base_path: str = ""

    @validator("src_paths")
    def validate_src_paths(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("src_paths must not be empty")
        cleaned = [p.strip().strip("/") for p in v if p and p.strip()]
        if not cleaned:
            raise ValueError("src_paths must contain at least one valid path")
        return cleaned

    @validator("dst_folder")
    def validate_dst_folder(cls, v: str) -> str:
        if v is None:
            return ""
        return v.strip().strip("/")

    @validator("base_path")
    def validate_base_path(cls, v: str) -> str:
        if v is None:
            return ""
        return v.strip().strip("/")


class HFMoveFolderRequest(HFBulkRequest):
    src_folder: str
    dst_folder: str

    @validator("src_folder", "dst_folder")
    def validate_folders(cls, v: str) -> str:
        if v is None:
            raise ValueError("folder path must not be None")
        return v.strip().strip("/")


class HFDeleteRequest(HFBulkRequest):
    paths: List[str]

    @validator("paths")
    def validate_paths(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("paths must not be empty")
        cleaned = [p.strip() for p in v if p and p.strip()]
        if not cleaned:
            raise ValueError("paths must contain at least one valid path")
        return cleaned


class HFDeleteFolderRequest(HFBulkRequest):
    folder_path: str

    @validator("folder_path")
    def validate_folder(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("folder_path must not be empty")
        return v.strip().strip("/")


class HFCrossCopyRequest(BaseModel):
    src_repo_id: str
    src_repo_type: str
    src_paths: List[str]
    dst_repo_id: str
    dst_repo_type: str
    dst_path_prefix: str = ""
    src_base_path: str = ""
    src_token: str
    dst_token: str
    delete_source: bool = True

    @validator("src_repo_id", "dst_repo_id")
    def validate_repo_id(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("repo_id must not be empty")
        return v.strip()

    @validator("src_repo_type", "dst_repo_type")
    def validate_repo_type(cls, v: str) -> str:
        if v not in VALID_REPO_TYPES:
            raise ValueError(f"repo_type must be one of {VALID_REPO_TYPES}")
        return v

    @validator("src_paths")
    def validate_src_paths(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError("src_paths must not be empty")
        cleaned = [p.strip() for p in v if p and p.strip()]
        if not cleaned:
            raise ValueError("src_paths must contain at least one valid path")
        return cleaned

    @validator("dst_path_prefix")
    def validate_prefix(cls, v: str) -> str:
        if v is None:
            return ""
        return v.strip().strip("/")

    @validator("src_base_path")
    def validate_src_base_path(cls, v: str) -> str:
        if v is None:
            return ""
        return v.strip().strip("/")

    @validator("src_token", "dst_token")
    def validate_tokens(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("token must not be empty")
        return v.strip()


class HFRenameRepoRequest(HFBulkRequest):
    new_name: str

    @validator("new_name")
    def validate_new_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("new_name must not be empty")
        cleaned = v.strip()
        if "/" in cleaned:
            raise ValueError("new_name must not contain '/'")
        if any(c in cleaned for c in " \t\n\r"):
            raise ValueError("new_name must not contain whitespace")
        return cleaned


class HFDeleteRepoRequest(HFBulkRequest):
    pass


class HFUpdateVisibilityRequest(HFBulkRequest):
    private: bool


class HFSquashHistoryRequest(HFBulkRequest):
    pass


class HFCreateRepoRequest(BaseModel):
    name: str
    repo_type: str
    token: str
    private: bool = True

    @validator("name")
    def validate_name(cls, v: str) -> str:
        cleaned = (v or "").strip()
        if not cleaned:
            raise ValueError("name must not be empty")
        if "/" in cleaned:
            raise ValueError("name must not contain '/'")
        if any(c in cleaned for c in " \t\n\r"):
            raise ValueError("name must not contain whitespace")
        return cleaned

    @validator("repo_type")
    def validate_repo_type(cls, v: str) -> str:
        if v not in VALID_REPO_TYPES:
            raise ValueError(f"repo_type must be one of {VALID_REPO_TYPES}")
        return v

    @validator("token")
    def validate_token(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("token must not be empty")
        return v.strip()


def _get_hf_api(token: str):
    try:
        from huggingface_hub import HfApi
    except ImportError:
        raise HTTPException(
            500,
            "huggingface_hub is not installed on the server. Please contact the administrator."
        )
    return HfApi(token=token)


def _map_repo_type(repo_type: str) -> str:
    if repo_type == "dataset":
        return "dataset"
    if repo_type == "model":
        return "model"
    if repo_type == "space":
        return "space"
    raise HTTPException(400, f"Unsupported repo_type: {repo_type}")


def _validate_path_security(path: str) -> str:
    cleaned = path.strip().lstrip("/")
    if not cleaned:
        raise HTTPException(400, "path must not be empty")
    if ".." in cleaned.split("/"):
        raise HTTPException(400, "path must not contain '..'")
    if "\x00" in cleaned:
        raise HTTPException(400, "path must not contain null bytes")
    return cleaned


def _hf_error_to_response(error: Exception) -> HTTPException:
    message = str(error)
    lower = message.lower()
    if "401" in lower or "unauthorized" in lower or "invalid user token" in lower:
        return HTTPException(401, "Invalid or expired Hugging Face token")
    if "403" in lower or "forbidden" in lower:
        return HTTPException(403, "Access denied. Check your token permissions.")
    if "404" in lower or "not found" in lower:
        return HTTPException(404, "Repository or file not found")
    if "429" in lower or "rate limit" in lower:
        return HTTPException(429, "Rate limit exceeded. Try again later.")
    if "revision" in lower or "branch" in lower:
        return HTTPException(400, message)
    return HTTPException(500, f"Hugging Face error: {message}")


def _normalize_path(path: str) -> str:
    if not path:
        return ""
    try:
        decoded = urllib.parse.unquote(path)
    except Exception:
        decoded = path
    return decoded.strip("/")


def _list_folder_files(api, repo_id: str, repo_type: str, folder_path: str) -> List[str]:
    files: List[str] = []
    try:
        items = api.list_repo_tree(
            repo_id=repo_id,
            path_in_repo=folder_path if folder_path else None,
            repo_type=repo_type,
            recursive=True,
        )
        for item in items:
            path = getattr(item, "path", None) or getattr(item, "rpath", None)
            if not path:
                continue
            size = getattr(item, "size", None)
            if size is None:
                continue
            files.append(path)
    except Exception as e:
        logger.error(f"List folder files error: {e}")
        raise _hf_error_to_response(e)
    return files


def _list_all_folders(api, repo_id: str, repo_type: str) -> List[str]:
    try:
        items = api.list_repo_tree(
            repo_id=repo_id,
            repo_type=repo_type,
            recursive=True,
        )
        folders = set()
        for item in items:
            path = getattr(item, "path", None) or getattr(item, "rpath", None)
            if not path:
                continue
            size = getattr(item, "size", None)
            if size is not None:
                continue
            cleaned = path.strip("/")
            if not cleaned:
                continue
            folders.add(cleaned)
        return sorted(folders)
    except Exception as e:
        logger.error(f"List all folders error: {e}")
        raise _hf_error_to_response(e)


def _get_cached_folders(repo_id: str, repo_type: str, token: str) -> Optional[List[str]]:
    key = f"{repo_type}:{repo_id}:{_get_token_key(token)}"
    entry = _FOLDER_CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry["timestamp"] > _FOLDER_CACHE_TTL:
        del _FOLDER_CACHE[key]
        return None
    return entry["folders"]


def _set_cached_folders(repo_id: str, repo_type: str, token: str, folders: List[str]) -> None:
    key = f"{repo_type}:{repo_id}:{_get_token_key(token)}"
    _FOLDER_CACHE[key] = {"folders": folders, "timestamp": time.time()}


def _invalidate_folder_cache(repo_id: str, repo_type: str, token: str) -> None:
    key = f"{repo_type}:{repo_id}:{_get_token_key(token)}"
    _FOLDER_CACHE.pop(key, None)


def _expand_src_paths(
    api,
    repo_id: str,
    repo_type: str,
    src_paths: List[str],
    base_path: str = "",
) -> List[Dict[str, Any]]:
    try:
        all_items = api.list_repo_tree(
            repo_id=repo_id,
            repo_type=repo_type,
            recursive=True,
        )
    except Exception as e:
        logger.error(f"_expand_src_paths: failed to list repo tree: {e}")
        raise _hf_error_to_response(e)

    all_files: List[Dict[str, Any]] = []
    for item in all_items:
        path = getattr(item, "path", None) or getattr(item, "rpath", None)
        if not path:
            continue
        size = getattr(item, "size", None)
        if size is None:
            continue
        all_files.append({
            "path": path.strip("/"),
            "norm": _normalize_path(path),
            "size": size,
        })

    file_map: Dict[str, Dict[str, Any]] = {}
    for f in all_files:
        file_map[f["norm"]] = f

    all_folders: set = set()
    for f in all_files:
        parts = f["norm"].split("/")
        for i in range(1, len(parts)):
            all_folders.add("/".join(parts[:i]))

    base_clean = _normalize_path(base_path) if base_path else ""

    def _rel_from_base(full_norm: str) -> str:
        if not base_clean:
            return full_norm
        prefix = base_clean + "/"
        if full_norm.startswith(prefix):
            return full_norm[len(prefix):]
        return full_norm

    files: List[Dict[str, Any]] = []
    seen_targets: set = set()

    for raw_path in src_paths:
        clean = _normalize_path(raw_path)
        if not clean:
            continue

        if clean in file_map:
            matched = file_map[clean]
            if matched["norm"] not in seen_targets:
                seen_targets.add(matched["norm"])
                files.append({
                    "src_path": matched["path"],
                    "rel_path": _rel_from_base(matched["norm"]),
                    "size": matched["size"],
                })
            continue

        if clean in all_folders:
            prefix = clean + "/"
            for f in all_files:
                if not f["norm"].startswith(prefix):
                    continue
                if f["norm"] in seen_targets:
                    continue
                seen_targets.add(f["norm"])
                files.append({
                    "src_path": f["path"],
                    "rel_path": _rel_from_base(f["norm"]),
                    "size": f["size"],
                })
            continue

        logger.warning(f"_expand_src_paths: path not found in repo: '{clean}'")

    return files


@router.get("/health")
async def health():
    try:
        import huggingface_hub
        version = getattr(huggingface_hub, "__version__", "unknown")
        return {
            "status": "healthy",
            "huggingface_hub_version": version,
        }
    except ImportError:
        return {
            "status": "degraded",
            "error": "huggingface_hub not installed",
        }


@router.post("/list-folders")
async def list_folders(request: HFListFoldersRequest):
    cached = _get_cached_folders(request.repo_id, request.repo_type, request.token)
    if cached is not None:
        return {
            "ok": True,
            "folders": cached,
            "count": len(cached),
            "cached": True,
        }

    api = _get_hf_api(request.token)
    repo_type = _map_repo_type(request.repo_type)

    folders = _list_all_folders(api, request.repo_id, repo_type)
    _set_cached_folders(request.repo_id, request.repo_type, request.token, folders)

    return {
        "ok": True,
        "folders": folders,
        "count": len(folders),
        "cached": False,
    }


@router.post("/create-folder")
async def create_folder(request: HFCreateFolderRequest):
    folder_path = _validate_path_security(request.folder_path)

    if not folder_path:
        raise HTTPException(400, "folder_path must not be empty")

    api = _get_hf_api(request.token)
    repo_type = _map_repo_type(request.repo_type)

    try:
        from huggingface_hub import CommitOperationAdd
    except ImportError as e:
        raise HTTPException(500, f"huggingface_hub import error: {e}")

    try:
        try:
            existing_folders = _list_all_folders(api, request.repo_id, repo_type)
            if folder_path in existing_folders:
                raise HTTPException(409, f"Folder '{folder_path}' already exists")
        except HTTPException:
            raise
        except Exception as e:
            logger.warning(f"create_folder: precheck folders failed: {e}")

        gitkeep_path = f"{folder_path}/.gitkeep"

        api.create_commit(
            repo_id=request.repo_id,
            repo_type=repo_type,
            operations=[
                CommitOperationAdd(
                    path_in_repo=gitkeep_path,
                    path_or_fileobj=b"",
                ),
            ],
            commit_message=f"Create folder {folder_path}",
        )

        _invalidate_folder_cache(request.repo_id, request.repo_type, request.token)

        return {
            "ok": True,
            "folder_path": folder_path,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"HF create folder error: {e}")
        raise _hf_error_to_response(e)


@router.post("/rename")
async def rename_file(request: HFRenameRequest):
    old_path = _validate_path_security(request.old_path)
    new_path = _validate_path_security(request.new_path)

    if old_path == new_path:
        return {"ok": True, "message": "Source and destination are the same"}

    api = _get_hf_api(request.token)
    repo_type = _map_repo_type(request.repo_type)

    try:
        from huggingface_hub import CommitOperationCopy, CommitOperationDelete

        api.create_commit(
            repo_id=request.repo_id,
            repo_type=repo_type,
            operations=[
                CommitOperationCopy(
                    src_path_in_repo=old_path,
                    path_in_repo=new_path,
                ),
            ],
            commit_message=f"Copy {old_path} to {new_path}",
        )

        api.create_commit(
            repo_id=request.repo_id,
            repo_type=repo_type,
            operations=[
                CommitOperationDelete(path_in_repo=old_path),
            ],
            commit_message=f"Delete {old_path} after rename",
        )

        _invalidate_folder_cache(request.repo_id, request.repo_type, request.token)

        return {
            "ok": True,
            "old_path": old_path,
            "new_path": new_path,
        }
    except ImportError as e:
        raise HTTPException(500, f"huggingface_hub import error: {e}")
    except Exception as e:
        logger.error(f"HF rename error: {e}")
        raise _hf_error_to_response(e)


@router.post("/move")
async def move_file(request: HFMoveRequest):
    src_path = _validate_path_security(request.src_path)
    dst_path = _validate_path_security(request.dst_path)

    if src_path == dst_path:
        return {"ok": True, "message": "Source and destination are the same"}

    api = _get_hf_api(request.token)
    repo_type = _map_repo_type(request.repo_type)

    try:
        from huggingface_hub import CommitOperationCopy, CommitOperationDelete

        api.create_commit(
            repo_id=request.repo_id,
            repo_type=repo_type,
            operations=[
                CommitOperationCopy(
                    src_path_in_repo=src_path,
                    path_in_repo=dst_path,
                ),
            ],
            commit_message=f"Move {src_path} to {dst_path}",
        )

        api.create_commit(
            repo_id=request.repo_id,
            repo_type=repo_type,
            operations=[
                CommitOperationDelete(path_in_repo=src_path),
            ],
            commit_message=f"Delete {src_path} after move",
        )

        _invalidate_folder_cache(request.repo_id, request.repo_type, request.token)

        return {
            "ok": True,
            "src_path": src_path,
            "dst_path": dst_path,
        }
    except ImportError as e:
        raise HTTPException(500, f"huggingface_hub import error: {e}")
    except Exception as e:
        logger.error(f"HF move error: {e}")
        raise _hf_error_to_response(e)


@router.post("/move-bulk")
async def move_bulk(request: HFMoveBulkAnyRequest):
    cleaned_paths = [_validate_path_security(p) for p in request.src_paths]
    dst_folder = _validate_path_security(request.dst_folder) if request.dst_folder else ""
    base_path = request.base_path or ""

    if not cleaned_paths:
        raise HTTPException(400, "No valid src_paths")

    api = _get_hf_api(request.token)
    repo_type = _map_repo_type(request.repo_type)

    try:
        from huggingface_hub import CommitOperationCopy, CommitOperationDelete
    except ImportError as e:
        raise HTTPException(500, f"huggingface_hub import error: {e}")

    try:
        expanded = _expand_src_paths(api, request.repo_id, repo_type, cleaned_paths, base_path)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Move-bulk: expand src paths failed: {e}")
        raise _hf_error_to_response(e)

    if not expanded:
        return {
            "ok": True,
            "moved": [],
            "count": 0,
            "skipped": cleaned_paths,
            "message": "No matching files or folders found",
        }

    operations = []
    target_map: List[Dict[str, Any]] = []

    for entry in expanded:
        src_path = entry["src_path"]
        rel_path = entry["rel_path"]

        if dst_folder:
            dst_path = f"{dst_folder}/{rel_path}".strip("/")
        else:
            dst_path = rel_path.strip("/")

        if src_path == dst_path:
            continue

        if dst_path.startswith(src_path + "/"):
            raise HTTPException(400, f"Cannot move '{src_path}' into itself")

        operations.append(
            CommitOperationCopy(
                src_path_in_repo=src_path,
                path_in_repo=dst_path,
            )
        )
        operations.append(
            CommitOperationDelete(path_in_repo=src_path)
        )
        target_map.append({
            "src_path": src_path,
            "dst_path": dst_path,
        })

    if not operations:
        return {
            "ok": True,
            "moved": [],
            "count": 0,
            "message": "Nothing to move",
        }

    try:
        api.create_commit(
            repo_id=request.repo_id,
            repo_type=repo_type,
            operations=operations,
            commit_message=f"Move {len(target_map)} item(s) to {dst_folder or '/'}",
        )
    except Exception as e:
        logger.error(f"Move-bulk commit failed: {e}")
        raise _hf_error_to_response(e)

    _invalidate_folder_cache(request.repo_id, request.repo_type, request.token)

    return {
        "ok": True,
        "moved": target_map,
        "count": len(target_map),
        "dst_folder": dst_folder,
        "base_path": base_path,
    }


@router.post("/move-folder")
async def move_folder(request: HFMoveFolderRequest):
    src_folder = _validate_path_security(request.src_folder) if request.src_folder else ""
    dst_folder = _validate_path_security(request.dst_folder) if request.dst_folder else ""

    if not src_folder:
        raise HTTPException(400, "src_folder must not be empty")

    if src_folder == dst_folder:
        return {"ok": True, "message": "Source and destination are the same"}

    if dst_folder.startswith(src_folder + "/"):
        raise HTTPException(400, "Cannot move folder into itself")

    api = _get_hf_api(request.token)
    repo_type = _map_repo_type(request.repo_type)

    try:
        from huggingface_hub import CommitOperationCopy, CommitOperationDelete

        files = _list_folder_files(api, request.repo_id, repo_type, src_folder)

        if not files:
            return {
                "ok": True,
                "moved": [],
                "count": 0,
                "message": f"No files found in {src_folder}",
            }

        operations = []
        moved = []
        for file_path in files:
            if src_folder and file_path.startswith(src_folder + "/"):
                rel = file_path[len(src_folder) + 1:]
            elif file_path == src_folder:
                rel = os.path.basename(file_path)
            else:
                rel = file_path

            if dst_folder:
                new_path = f"{dst_folder}/{rel}"
            else:
                new_path = rel

            if new_path == file_path:
                continue

            operations.append(
                CommitOperationCopy(
                    src_path_in_repo=file_path,
                    path_in_repo=new_path,
                )
            )
            operations.append(
                CommitOperationDelete(path_in_repo=file_path)
            )
            moved.append({"src_path": file_path, "dst_path": new_path})

        if not operations:
            return {
                "ok": True,
                "moved": [],
                "count": 0,
                "message": "Nothing to move",
            }

        api.create_commit(
            repo_id=request.repo_id,
            repo_type=repo_type,
            operations=operations,
            commit_message=f"Move folder {src_folder} to {dst_folder or '/'} ({len(moved)} files)",
        )

        _invalidate_folder_cache(request.repo_id, request.repo_type, request.token)

        return {
            "ok": True,
            "src_folder": src_folder,
            "dst_folder": dst_folder,
            "moved": moved,
            "count": len(moved),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"HF move folder error: {e}")
        raise _hf_error_to_response(e)


@router.post("/rename-folder")
async def rename_folder(request: HFMoveFolderRequest):
    src_folder = _validate_path_security(request.src_folder) if request.src_folder else ""
    dst_folder = _validate_path_security(request.dst_folder) if request.dst_folder else ""

    if not src_folder:
        raise HTTPException(400, "src_folder must not be empty")

    if not dst_folder:
        raise HTTPException(400, "dst_folder must not be empty")

    if src_folder == dst_folder:
        return {"ok": True, "message": "Source and destination are the same"}

    api = _get_hf_api(request.token)
    repo_type = _map_repo_type(request.repo_type)

    try:
        from huggingface_hub import CommitOperationCopy, CommitOperationDelete

        files = _list_folder_files(api, request.repo_id, repo_type, src_folder)

        if not files:
            return {
                "ok": True,
                "renamed": [],
                "count": 0,
                "message": f"No files found in {src_folder}",
            }

        operations = []
        renamed = []
        for file_path in files:
            if file_path.startswith(src_folder + "/"):
                rel = file_path[len(src_folder) + 1:]
            else:
                rel = os.path.basename(file_path)

            new_path = f"{dst_folder}/{rel}"

            if new_path == file_path:
                continue

            operations.append(
                CommitOperationCopy(
                    src_path_in_repo=file_path,
                    path_in_repo=new_path,
                )
            )
            operations.append(
                CommitOperationDelete(path_in_repo=file_path)
            )
            renamed.append({"src_path": file_path, "dst_path": new_path})

        if not operations:
            return {
                "ok": True,
                "renamed": [],
                "count": 0,
                "message": "Nothing to rename",
            }

        api.create_commit(
            repo_id=request.repo_id,
            repo_type=repo_type,
            operations=operations,
            commit_message=f"Rename folder {src_folder} to {dst_folder} ({len(renamed)} files)",
        )

        _invalidate_folder_cache(request.repo_id, request.repo_type, request.token)

        return {
            "ok": True,
            "old_folder": src_folder,
            "new_folder": dst_folder,
            "renamed": renamed,
            "count": len(renamed),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"HF rename folder error: {e}")
        raise _hf_error_to_response(e)


@router.post("/delete")
async def delete_files(request: HFDeleteRequest):
    cleaned_paths = [_validate_path_security(p) for p in request.paths]

    if not cleaned_paths:
        raise HTTPException(400, "No valid paths to delete")

    api = _get_hf_api(request.token)
    repo_type = _map_repo_type(request.repo_type)

    try:
        from huggingface_hub import CommitOperationDelete

        operations = [CommitOperationDelete(path_in_repo=p) for p in cleaned_paths]

        api.create_commit(
            repo_id=request.repo_id,
            repo_type=repo_type,
            operations=operations,
            commit_message=f"Delete {len(cleaned_paths)} file(s)",
        )

        _invalidate_folder_cache(request.repo_id, request.repo_type, request.token)

        return {
            "ok": True,
            "deleted": cleaned_paths,
            "count": len(cleaned_paths),
        }
    except ImportError as e:
        raise HTTPException(500, f"huggingface_hub import error: {e}")
    except Exception as e:
        logger.error(f"HF delete error: {e}")
        raise _hf_error_to_response(e)


@router.post("/delete-folder")
async def delete_folder(request: HFDeleteFolderRequest):
    folder_path = _validate_path_security(request.folder_path)

    if not folder_path:
        raise HTTPException(400, "folder_path must not be empty")

    api = _get_hf_api(request.token)
    repo_type = _map_repo_type(request.repo_type)

    try:
        from huggingface_hub import CommitOperationDelete

        files = _list_folder_files(api, request.repo_id, repo_type, folder_path)

        if not files:
            return {
                "ok": True,
                "deleted": [],
                "count": 0,
                "message": f"No files found in {folder_path}",
            }

        operations = [CommitOperationDelete(path_in_repo=f) for f in files]

        api.create_commit(
            repo_id=request.repo_id,
            repo_type=repo_type,
            operations=operations,
            commit_message=f"Delete folder {folder_path} ({len(files)} files)",
        )

        _invalidate_folder_cache(request.repo_id, request.repo_type, request.token)

        return {
            "ok": True,
            "deleted": files,
            "count": len(files),
            "folder_path": folder_path,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"HF delete folder error: {e}")
        raise _hf_error_to_response(e)


@router.post("/cross-copy")
async def cross_copy(request: HFCrossCopyRequest):
    src_repo_type = _map_repo_type(request.src_repo_type)
    dst_repo_type = _map_repo_type(request.dst_repo_type)

    cleaned_src_paths = [_validate_path_security(p) for p in request.src_paths]
    if not cleaned_src_paths:
        raise HTTPException(400, "No valid src_paths")

    src_base_path = request.src_base_path or ""

    src_api = _get_hf_api(request.src_token)
    dst_api = _get_hf_api(request.dst_token)

    try:
        from huggingface_hub import CommitOperationCopy, CommitOperationDelete
    except ImportError as e:
        raise HTTPException(500, f"huggingface_hub import error: {e}")

    try:
        dst_api.repo_info(repo_id=request.dst_repo_id, repo_type=dst_repo_type)
    except Exception as e:
        logger.error(f"Cross-copy: dst repo check failed: {e}")
        raise _hf_error_to_response(e)

    try:
        expanded = _expand_src_paths(src_api, request.src_repo_id, src_repo_type, cleaned_src_paths, src_base_path)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Cross-copy: expand src paths failed: {e}")
        raise _hf_error_to_response(e)

    if not expanded:
        return {
            "ok": True,
            "copied": [],
            "failed_copy": [],
            "deleted_src": [],
            "failed_delete_src": [],
            "message": "No files found in source paths",
        }

    copy_operations = []
    target_map: List[Dict[str, Any]] = []
    for entry in expanded:
        src_path = entry["src_path"]
        rel_path = entry["rel_path"]
        if request.dst_path_prefix:
            dst_path = f"{request.dst_path_prefix}/{rel_path}".strip("/")
        else:
            dst_path = rel_path.strip("/")

        if request.src_repo_id == request.dst_repo_id and src_path == dst_path:
            continue

        copy_operations.append(
            CommitOperationCopy(
                src_path_in_repo=src_path,
                path_in_repo=dst_path,
                src_repo_id=request.src_repo_id,
                src_repo_type=src_repo_type,
            )
        )
        target_map.append({
            "src_path": src_path,
            "dst_path": dst_path,
            "size": entry.get("size"),
        })

    if not copy_operations:
        return {
            "ok": True,
            "copied": [],
            "failed_copy": [],
            "deleted_src": [],
            "failed_delete_src": [],
            "message": "Nothing to copy",
        }

    try:
        dst_api.create_commit(
            repo_id=request.dst_repo_id,
            repo_type=dst_repo_type,
            operations=copy_operations,
            commit_message=f"Cross-copy from {request.src_repo_id} ({len(copy_operations)} files)",
        )
    except Exception as e:
        logger.error(f"Cross-copy commit failed: {e}")
        raise _hf_error_to_response(e)

    copied_paths = [t["dst_path"] for t in target_map]

    if not request.delete_source:
        # Plain copy: leave the source untouched, nothing to verify or delete.
        _invalidate_folder_cache(request.dst_repo_id, request.dst_repo_type, request.dst_token)
        return {
            "ok": True,
            "copied": copied_paths,
            "failed_copy": [],
            "deleted_src": [],
            "failed_delete_src": [],
            "not_verified": [],
            "count_copied": len(copied_paths),
            "count_deleted_src": 0,
            "count_failed_delete_src": 0,
            "src_repo_id": request.src_repo_id,
            "dst_repo_id": request.dst_repo_id,
        }

    time.sleep(1.0)

    try:
        dst_files = set(_list_folder_files(dst_api, request.dst_repo_id, dst_repo_type, request.dst_path_prefix or ""))
    except Exception:
        dst_files = set()

    verified: List[Dict[str, Any]] = []
    not_verified: List[Dict[str, Any]] = []
    if dst_files:
        dst_files_norm = {p.strip("/") for p in dst_files}
        for t in target_map:
            if t["dst_path"].strip("/") in dst_files_norm:
                verified.append(t)
            else:
                not_verified.append(t)
    else:
        verified = list(target_map)

    deleted_src: List[str] = []
    failed_delete_src: List[Dict[str, str]] = []

    if verified:
        delete_operations = [CommitOperationDelete(path_in_repo=t["src_path"]) for t in verified]
        try:
            src_api.create_commit(
                repo_id=request.src_repo_id,
                repo_type=src_repo_type,
                operations=delete_operations,
                commit_message=f"Delete {len(delete_operations)} file(s) after cross-copy to {request.dst_repo_id}",
            )
            deleted_src = [t["src_path"] for t in verified]
        except Exception as e:
            logger.error(f"Cross-copy delete src failed: {e}")
            for t in verified:
                failed_delete_src.append({"src_path": t["src_path"], "error": str(e)[:200]})

    _invalidate_folder_cache(request.src_repo_id, request.src_repo_type, request.src_token)
    _invalidate_folder_cache(request.dst_repo_id, request.dst_repo_type, request.dst_token)

    return {
        "ok": True,
        "copied": copied_paths,
        "failed_copy": [],
        "deleted_src": deleted_src,
        "failed_delete_src": failed_delete_src,
        "not_verified": [t["src_path"] for t in not_verified],
        "count_copied": len(copied_paths),
        "count_deleted_src": len(deleted_src),
        "count_failed_delete_src": len(failed_delete_src),
        "src_repo_id": request.src_repo_id,
        "dst_repo_id": request.dst_repo_id,
    }


@router.post("/rename-repo")
async def rename_repo(request: HFRenameRepoRequest):
    api = _get_hf_api(request.token)
    repo_type = _map_repo_type(request.repo_type)

    old_repo_id = request.repo_id.strip()
    new_name = request.new_name.strip()

    if "/" not in old_repo_id:
        raise HTTPException(400, "repo_id must be in format 'owner/name'")

    owner = old_repo_id.split("/", 1)[0]
    old_name = old_repo_id.split("/", 1)[1]

    if new_name == old_name:
        return {
            "ok": True,
            "repo_id": old_repo_id,
            "new_repo_id": old_repo_id,
            "message": "Name unchanged",
        }

    new_repo_id = f"{owner}/{new_name}"

    try:
        api.move_repo(
            from_id=old_repo_id,
            to_id=new_repo_id,
            repo_type=repo_type,
        )

        _invalidate_folder_cache(old_repo_id, request.repo_type, request.token)
        _invalidate_folder_cache(new_repo_id, request.repo_type, request.token)

        return {
            "ok": True,
            "repo_id": old_repo_id,
            "new_repo_id": new_repo_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"HF rename repo error: {e}")
        raise _hf_error_to_response(e)


@router.post("/delete-repo")
async def delete_repo(request: HFDeleteRepoRequest):
    api = _get_hf_api(request.token)
    repo_type = _map_repo_type(request.repo_type)

    repo_id = request.repo_id.strip()

    if "/" not in repo_id:
        raise HTTPException(400, "repo_id must be in format 'owner/name'")

    try:
        api.delete_repo(
            repo_id=repo_id,
            repo_type=repo_type,
        )

        _invalidate_folder_cache(repo_id, request.repo_type, request.token)

        return {
            "ok": True,
            "repo_id": repo_id,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"HF delete repo error: {e}")
        raise _hf_error_to_response(e)


@router.post("/create-repo")
async def create_repo(request: HFCreateRepoRequest):
    api = _get_hf_api(request.token)
    repo_type = _map_repo_type(request.repo_type)

    try:
        who = await asyncio.to_thread(api.whoami)
        owner = (who or {}).get("name")
        if not owner:
            raise HTTPException(400, "Could not resolve account name from token")

        repo_id = f"{owner}/{request.name}"

        kwargs: Dict[str, Any] = {
            "repo_id": repo_id,
            "repo_type": repo_type,
            "private": request.private,
            "exist_ok": False,
        }
        if repo_type == "space":
            kwargs["space_sdk"] = "static"

        await asyncio.to_thread(api.create_repo, **kwargs)

        return {
            "ok": True,
            "repo_id": repo_id,
            "repo_type": request.repo_type,
            "private": request.private,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"HF create repo error: {e}")
        raise _hf_error_to_response(e)


@router.post("/squash-history")
async def squash_history(request: HFSquashHistoryRequest):
    api = _get_hf_api(request.token)
    repo_type = _map_repo_type(request.repo_type)

    repo_id = request.repo_id.strip()

    if "/" not in repo_id:
        raise HTTPException(400, "repo_id must be in format 'owner/name'")

    try:
        # super_squash_history is blocking and can take a while on big repos,
        # so run it in a worker thread to keep the event loop free.
        await asyncio.to_thread(
            api.super_squash_history,
            repo_id=repo_id,
            repo_type=repo_type,
            commit_message="Clean commit history and prune unreferenced LFS cache",
        )

        _invalidate_folder_cache(repo_id, request.repo_type, request.token)

        return {
            "ok": True,
            "repo_id": repo_id,
            "message": "History squashed. Storage usage is updated after Hugging Face garbage collection (can take a few hours).",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"HF squash history error: {e}")
        raise _hf_error_to_response(e)


@router.post("/update-repo-visibility")
async def update_repo_visibility(request: HFUpdateVisibilityRequest):
    api = _get_hf_api(request.token)
    repo_type = _map_repo_type(request.repo_type)

    repo_id = request.repo_id.strip()

    if "/" not in repo_id:
        raise HTTPException(400, "repo_id must be in format 'owner/name'")

    try:
        api.update_repo_settings(
            repo_id=repo_id,
            repo_type=repo_type,
            private=request.private,
        )

        return {
            "ok": True,
            "repo_id": repo_id,
            "private": request.private,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"HF update visibility error: {e}")
        raise _hf_error_to_response(e)