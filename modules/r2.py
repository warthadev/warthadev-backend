# modules/r2.py
import os
import boto3
import asyncio
from botocore.config import Config
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse
from typing import Optional

router = APIRouter(prefix="/r2", tags=["r2"])

# ========== KONFIGURASI ==========
R2_ACCESS_KEY_ID     = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_ACCOUNT_ID        = os.environ.get("R2_ACCOUNT_ID", "")
R2_BUCKET_NAME       = os.environ.get("R2_BUCKET_NAME", "telegram-videos")

R2_ENDPOINT = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

def get_r2_client():
    """Buat boto3 S3 client yang konek ke R2"""
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )

def is_configured() -> bool:
    return all([R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ACCOUNT_ID, R2_BUCKET_NAME])


# ========== HELPER ==========

def get_r2_key(chat_id: int, message_id: int, filename: str) -> str:
    """Generate key/path di R2"""
    return f"telegram/{chat_id}/{message_id}/{filename}"

def check_exists(r2, key: str) -> Optional[int]:
    """
    Cek apakah file sudah ada di R2.
    Return file size jika ada, None jika tidak.
    """
    try:
        resp = r2.head_object(Bucket=R2_BUCKET_NAME, Key=key)
        return resp["ContentLength"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "404":
            return None
        raise

def generate_presigned_url(r2, key: str, expires: int = 3600) -> str:
    """
    Generate presigned URL untuk stream langsung dari R2.
    Browser bisa akses langsung tanpa lewat Render.
    expires = detik (default 1 jam)
    """
    return r2.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET_NAME, "Key": key},
        ExpiresIn=expires,
    )


# ========== UPLOAD: Telegram → R2 ==========

async def upload_telegram_to_r2(
    telegram_client,
    chat_id: int,
    message_id: int,
) -> dict:
    """
    Download file dari Telegram lalu upload ke R2.
    Pakai multipart upload agar efisien untuk file besar.
    Return: dict dengan key, size, url
    """
    if not is_configured():
        raise Exception("R2 not configured. Check environment variables.")

    from pyrogram import Client
    msg = await telegram_client.get_messages(chat_id, message_id)
    if not msg:
        raise Exception("Message not found")

    # Tentukan media dan filename
    if msg.video:
        media    = msg.video
        filename = media.file_name or f"video_{message_id}.mp4"
        mime     = "video/mp4"
    elif msg.document:
        media    = msg.document
        filename = media.file_name or f"file_{message_id}"
        mime     = media.mime_type or "application/octet-stream"
    elif msg.audio:
        media    = msg.audio
        filename = media.file_name or f"audio_{message_id}.mp3"
        mime     = "audio/mpeg"
    else:
        raise Exception("No downloadable media in this message")

    file_size = media.file_size
    key       = get_r2_key(chat_id, message_id, filename)

    r2 = get_r2_client()

    # Kalau sudah ada di R2, langsung return URL
    existing_size = check_exists(r2, key)
    if existing_size:
        print(f"✅ File sudah ada di R2: {key}")
        url = generate_presigned_url(r2, key)
        return {"key": key, "size": existing_size, "url": url, "cached": True}

    print(f"⬆️ Mulai upload ke R2: {key} ({file_size} bytes)")

    # Mulai multipart upload
    mpu = r2.create_multipart_upload(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        ContentType=mime,
    )
    upload_id = mpu["UploadId"]
    parts     = []
    part_num  = 1
    buffer    = bytearray()
    MIN_PART  = 5 * 1024 * 1024  # 5MB minimum per part (syarat S3)

    try:
        async for chunk in telegram_client.stream_media(media.file_id, limit=0):
            buffer.extend(chunk)

            # Upload part ketika buffer >= 5MB
            while len(buffer) >= MIN_PART:
                data = bytes(buffer[:MIN_PART])
                buffer = buffer[MIN_PART:]

                resp = await asyncio.to_thread(
                    r2.upload_part,
                    Bucket=R2_BUCKET_NAME,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_num,
                    Body=data,
                )
                parts.append({"PartNumber": part_num, "ETag": resp["ETag"]})
                print(f"  Part {part_num} uploaded ({len(data) // 1024} KB)")
                part_num += 1

        # Upload sisa buffer (part terakhir, boleh < 5MB)
        if buffer:
            resp = await asyncio.to_thread(
                r2.upload_part,
                Bucket=R2_BUCKET_NAME,
                Key=key,
                UploadId=upload_id,
                PartNumber=part_num,
                Body=bytes(buffer),
            )
            parts.append({"PartNumber": part_num, "ETag": resp["ETag"]})
            print(f"  Part {part_num} uploaded (final, {len(buffer) // 1024} KB)")

        # Complete multipart upload
        r2.complete_multipart_upload(
            Bucket=R2_BUCKET_NAME,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )
        print(f"✅ Upload selesai: {key}")

    except Exception as e:
        # Batalkan multipart upload jika error
        r2.abort_multipart_upload(
            Bucket=R2_BUCKET_NAME,
            Key=key,
            UploadId=upload_id,
        )
        raise Exception(f"Upload failed: {e}")

    url = generate_presigned_url(r2, key)
    return {"key": key, "size": file_size, "url": url, "cached": False}


# ========== ENDPOINTS ==========

@router.get("/health")
async def r2_health():
    """Cek koneksi ke R2"""
    if not is_configured():
        return {"status": "not_configured", "missing": [
            k for k, v in {
                "R2_ACCESS_KEY_ID": R2_ACCESS_KEY_ID,
                "R2_SECRET_ACCESS_KEY": R2_SECRET_ACCESS_KEY,
                "R2_ACCOUNT_ID": R2_ACCOUNT_ID,
                "R2_BUCKET_NAME": R2_BUCKET_NAME,
            }.items() if not v
        ]}
    try:
        r2 = get_r2_client()
        r2.head_bucket(Bucket=R2_BUCKET_NAME)
        return {"status": "ok", "bucket": R2_BUCKET_NAME}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.post("/upload/{chat_id}/{message_id}")
async def upload_to_r2(chat_id: int, message_id: int):
    """
    Upload file dari Telegram ke R2.
    Panggil sekali, lalu gunakan /stream untuk streaming langsung.
    """
    if not is_configured():
        raise HTTPException(500, "R2 not configured")

    try:
        from modules.telegram import telegram_client
        result = await upload_telegram_to_r2(telegram_client, chat_id, message_id)
        return {
            "success": True,
            "key": result["key"],
            "size": result["size"],
            "cached": result["cached"],
            "stream_url": f"/r2/stream/{chat_id}/{message_id}",
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/stream/{chat_id}/{message_id}")
async def stream_from_r2(chat_id: int, message_id: int):
    """
    Generate presigned URL dan redirect browser langsung ke R2.
    Browser akan stream + seek langsung dari R2 — Render tidak ikut.
    URL berlaku 1 jam.
    """
    if not is_configured():
        raise HTTPException(500, "R2 not configured")

    try:
        r2 = get_r2_client()

        # Cari key yang sesuai (cek semua ekstensi umum)
        prefix = f"telegram/{chat_id}/{message_id}/"
        resp   = r2.list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix=prefix)

        objects = resp.get("Contents", [])
        if not objects:
            raise HTTPException(
                404,
                f"File not found in R2. Upload dulu via POST /r2/upload/{chat_id}/{message_id}"
            )

        # Ambil file pertama yang ditemukan
        key = objects[0]["Key"]
        url = generate_presigned_url(r2, key, expires=3600)

        # Redirect browser langsung ke R2 — Render tidak streaming sama sekali
        return RedirectResponse(url=url, status_code=302)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/url/{chat_id}/{message_id}")
async def get_r2_url(chat_id: int, message_id: int):
    """
    Return presigned URL tanpa redirect (untuk dipakai di frontend/player).
    """
    if not is_configured():
        raise HTTPException(500, "R2 not configured")

    try:
        r2      = get_r2_client()
        prefix  = f"telegram/{chat_id}/{message_id}/"
        resp    = r2.list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix=prefix)
        objects = resp.get("Contents", [])

        if not objects:
            raise HTTPException(404, "File not found in R2. Upload dulu.")

        key = objects[0]["Key"]
        url = generate_presigned_url(r2, key, expires=3600)

        return {"url": url, "key": key, "expires_in": 3600}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/delete/{chat_id}/{message_id}")
async def delete_from_r2(chat_id: int, message_id: int):
    """Hapus file dari R2"""
    if not is_configured():
        raise HTTPException(500, "R2 not configured")

    try:
        r2     = get_r2_client()
        prefix = f"telegram/{chat_id}/{message_id}/"
        resp   = r2.list_objects_v2(Bucket=R2_BUCKET_NAME, Prefix=prefix)
        objs   = resp.get("Contents", [])

        if not objs:
            raise HTTPException(404, "File not found")

        for obj in objs:
            r2.delete_object(Bucket=R2_BUCKET_NAME, Key=obj["Key"])

        return {"success": True, "deleted": [o["Key"] for o in objs]}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
