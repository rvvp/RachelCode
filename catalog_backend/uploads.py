from __future__ import annotations

from mimetypes import guess_type
import os
from pathlib import Path
from shutil import copyfileobj
from time import time
from uuid import uuid4


MEDIA_URL_PREFIX = "/media/"
MAX_IMAGE_BYTES = 3 * 1024 * 1024 * 1024
MAX_GENERIC_UPLOAD_BYTES = 20 * 1024 * 1024
IMAGE_BACKUP_RETENTION_SECONDS = 2 * 24 * 60 * 60
UPLOAD_COPY_CHUNK_BYTES = 8 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
ALLOWED_DOCUMENT_EXTENSIONS = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".zip": "application/zip",
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def ensure_upload_dir(upload_dir: str | Path) -> Path:
    path = Path(upload_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def upload_file_path(upload_dir: str | Path, stored_path: str) -> Path:
    base_path = ensure_upload_dir(upload_dir).resolve()
    candidate = (base_path / stored_path).resolve()
    if base_path != candidate and base_path not in candidate.parents:
        raise ValueError("上传文件路径不合法。")
    return candidate


def upload_stream_size(file_obj) -> int:
    try:
        file_obj.seek(0, 2)
        size = int(file_obj.tell())
        file_obj.seek(0)
    except (AttributeError, OSError, TypeError, ValueError) as error:
        raise ValueError("上传文件无法读取，请重新选择文件后再试。") from error
    return size


def formatted_size_limit(max_bytes: int) -> str:
    if max_bytes % (1024 * 1024 * 1024) == 0:
        return f"{max_bytes // (1024 * 1024 * 1024)}GB"
    if max_bytes % (1024 * 1024) == 0:
        return f"{max_bytes // (1024 * 1024)}MB"
    return f"{max_bytes} 字节"


def read_validated_image_upload(file_item) -> dict | None:
    if file_item is None or not getattr(file_item, "filename", ""):
        return None
    original_filename = Path(file_item.filename).name
    extension = Path(original_filename).suffix.lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("图片只支持 JPG、PNG、WEBP 或 GIF 格式。")
    size_bytes = upload_stream_size(file_item.file)
    if not size_bytes:
        return None
    if size_bytes > MAX_IMAGE_BYTES:
        raise ValueError(f"图片大小不能超过 {formatted_size_limit(MAX_IMAGE_BYTES)}。")
    return {
        "original_filename": original_filename,
        "extension": extension,
        "file": file_item.file,
        "size_bytes": size_bytes,
        "content_type": ALLOWED_IMAGE_EXTENSIONS[extension],
    }


def read_validated_image_uploads(file_items) -> list[dict]:
    if file_items is None:
        return []
    if not isinstance(file_items, list):
        file_items = [file_items]
    uploads = []
    for item in file_items:
        upload = read_validated_image_upload(item)
        if upload:
            uploads.append(upload)
    return uploads


def read_validated_file_upload(
    file_item,
    *,
    allowed_extensions: dict[str, str] | None = None,
    max_bytes: int = MAX_GENERIC_UPLOAD_BYTES,
    load_content: bool = True,
) -> dict | None:
    if file_item is None or not getattr(file_item, "filename", ""):
        return None
    allowed_extensions = allowed_extensions or ALLOWED_DOCUMENT_EXTENSIONS
    original_filename = Path(file_item.filename).name
    extension = Path(original_filename).suffix.lower()
    if extension not in allowed_extensions:
        raise ValueError("文件只支持 Excel、CSV、ZIP、PDF 或常见图片格式。")
    size_bytes = upload_stream_size(file_item.file)
    if not size_bytes:
        return None
    if size_bytes > max_bytes:
        raise ValueError(f"上传文件大小不能超过 {formatted_size_limit(max_bytes)}。")
    payload = {
        "original_filename": original_filename,
        "extension": extension,
        "size_bytes": size_bytes,
        "content_type": allowed_extensions.get(extension, "application/octet-stream"),
    }
    if load_content:
        payload["content"] = file_item.file.read()
        file_item.file.seek(0)
    else:
        payload["file"] = file_item.file
    return payload


def read_validated_file_uploads(
    file_items,
    *,
    allowed_extensions: dict[str, str] | None = None,
    max_bytes: int = MAX_GENERIC_UPLOAD_BYTES,
) -> list[dict]:
    if file_items is None:
        return []
    if not isinstance(file_items, list):
        file_items = [file_items]
    uploads = []
    for item in file_items:
        upload = read_validated_file_upload(
            item,
            allowed_extensions=allowed_extensions,
            max_bytes=max_bytes,
        )
        if upload:
            uploads.append(upload)
    return uploads


def save_image_upload(upload_dir: str | Path, upload_payload: dict) -> str:
    path = ensure_upload_dir(upload_dir)
    filename = f"{uuid4().hex}{upload_payload['extension']}"
    destination = path / filename
    try:
        with destination.open("wb") as output:
            if upload_payload.get("file") is not None:
                source = upload_payload["file"]
                source.seek(0)
                copyfileobj(source, output, length=UPLOAD_COPY_CHUNK_BYTES)
                source.seek(0)
            else:
                output.write(upload_payload.get("content") or b"")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return MEDIA_URL_PREFIX + filename


def save_generic_upload(upload_dir: str | Path, upload_payload: dict, subdir: str = "billing") -> str:
    clean_subdir = str(subdir or "").strip("/").strip()
    if clean_subdir:
        path = ensure_upload_dir(upload_dir) / clean_subdir
        path.mkdir(parents=True, exist_ok=True)
        relative_dir = clean_subdir
    else:
        path = ensure_upload_dir(upload_dir)
        relative_dir = ""
    filename = f"{uuid4().hex}{upload_payload['extension']}"
    (path / filename).write_bytes(upload_payload["content"])
    return f"{relative_dir}/{filename}" if relative_dir else filename


def is_local_media_path(value: str | None) -> bool:
    return bool(value and str(value).startswith(MEDIA_URL_PREFIX))


def media_file_path(upload_dir: str | Path, media_path: str) -> Path:
    return ensure_upload_dir(upload_dir) / Path(media_path).name


def delete_generic_upload(upload_dir: str | Path, stored_path: str | None) -> None:
    if not stored_path:
        return
    file_path = upload_file_path(upload_dir, str(stored_path))
    if file_path.exists():
        file_path.unlink()


def delete_local_media(upload_dir: str | Path, media_path: str | None) -> None:
    if not is_local_media_path(media_path):
        return
    file_path = media_file_path(upload_dir, str(media_path))
    if file_path.exists():
        file_path.unlink()


def retain_local_media_backup(
    upload_dir: str | Path,
    media_path: str | None,
    *,
    retained_at: float | None = None,
) -> None:
    if not is_local_media_path(media_path):
        return
    file_path = media_file_path(upload_dir, str(media_path))
    if not file_path.exists() or not file_path.is_file():
        return
    timestamp = float(retained_at if retained_at is not None else time())
    os.utime(file_path, (timestamp, timestamp))


def cleanup_expired_local_media_backups(
    upload_dir: str | Path,
    referenced_media_paths,
    *,
    now: float | None = None,
    retention_seconds: int = IMAGE_BACKUP_RETENTION_SECONDS,
) -> list[str]:
    upload_path = ensure_upload_dir(upload_dir)
    referenced = {str(value or "").strip() for value in referenced_media_paths if str(value or "").strip()}
    cutoff = float(now if now is not None else time()) - max(0, int(retention_seconds))
    removed = []
    for file_path in upload_path.iterdir():
        if file_path.is_symlink() or not file_path.is_file():
            continue
        if file_path.suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
            continue
        media_path = MEDIA_URL_PREFIX + file_path.name
        try:
            modified_at = file_path.stat().st_mtime
        except FileNotFoundError:
            continue
        if media_path in referenced or modified_at > cutoff:
            continue
        try:
            file_path.unlink()
        except FileNotFoundError:
            continue
        removed.append(media_path)
    return removed


def media_content_type(media_path: str) -> str:
    extension = Path(media_path).suffix.lower()
    return ALLOWED_IMAGE_EXTENSIONS.get(extension, "application/octet-stream")


def detect_image_content_type(content: bytes, declared_type: str = "", source: str = "") -> str:
    """Return a safe image MIME type using the response and file signature.

    Some product-image CDNs return an opaque URL or ``application/octet-stream``
    even though the payload is a normal image. MIME sniffing is limited to
    common image signatures so arbitrary HTML or JSON responses are never
    served as an image by the internal proxy.
    """
    normalized_declared = str(declared_type or "").split(";", 1)[0].strip().lower()
    if normalized_declared.startswith("image/"):
        return normalized_declared
    payload = bytes(content or b"")
    if payload.startswith(b"\xFF\xD8\xFF"):
        return "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    if payload.startswith(b"BM"):
        return "image/bmp"
    if len(payload) >= 12 and payload[4:8] == b"ftyp":
        brand = payload[8:12].lower()
        if brand in {b"avif", b"avis"}:
            return "image/avif"
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic"
    guessed_type = guess_type(str(source or ""))[0] or ""
    return guessed_type if guessed_type.startswith("image/") else ""


def generic_content_type(stored_path: str) -> str:
    guessed_type, _ = guess_type(stored_path)
    if guessed_type:
        return guessed_type
    extension = Path(stored_path).suffix.lower()
    return ALLOWED_DOCUMENT_EXTENSIONS.get(extension, "application/octet-stream")
