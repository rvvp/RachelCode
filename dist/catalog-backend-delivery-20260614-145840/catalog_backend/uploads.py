from __future__ import annotations

from pathlib import Path
from uuid import uuid4


MEDIA_URL_PREFIX = "/media/"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
ALLOWED_IMAGE_EXTENSIONS = {
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


def read_validated_image_upload(file_item) -> dict | None:
    if file_item is None or not getattr(file_item, "filename", ""):
        return None
    extension = Path(file_item.filename).suffix.lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("图片只支持 JPG、PNG、WEBP 或 GIF 格式。")
    content = file_item.file.read()
    if not content:
        return None
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError("图片大小不能超过 5MB。")
    return {
        "extension": extension,
        "content": content,
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


def save_image_upload(upload_dir: str | Path, upload_payload: dict) -> str:
    path = ensure_upload_dir(upload_dir)
    filename = f"{uuid4().hex}{upload_payload['extension']}"
    (path / filename).write_bytes(upload_payload["content"])
    return MEDIA_URL_PREFIX + filename


def is_local_media_path(value: str | None) -> bool:
    return bool(value and str(value).startswith(MEDIA_URL_PREFIX))


def media_file_path(upload_dir: str | Path, media_path: str) -> Path:
    return ensure_upload_dir(upload_dir) / Path(media_path).name


def delete_local_media(upload_dir: str | Path, media_path: str | None) -> None:
    if not is_local_media_path(media_path):
        return
    file_path = media_file_path(upload_dir, str(media_path))
    if file_path.exists():
        file_path.unlink()


def media_content_type(media_path: str) -> str:
    extension = Path(media_path).suffix.lower()
    return ALLOWED_IMAGE_EXTENSIONS.get(extension, "application/octet-stream")
