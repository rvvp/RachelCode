from __future__ import annotations

from mimetypes import guess_type
from pathlib import Path
from uuid import uuid4


MEDIA_URL_PREFIX = "/media/"
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_GENERIC_UPLOAD_BYTES = 20 * 1024 * 1024
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


def read_validated_image_upload(file_item) -> dict | None:
    if file_item is None or not getattr(file_item, "filename", ""):
        return None
    original_filename = Path(file_item.filename).name
    extension = Path(original_filename).suffix.lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("图片只支持 JPG、PNG、WEBP 或 GIF 格式。")
    content = file_item.file.read()
    if not content:
        return None
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError("图片大小不能超过 5MB。")
    return {
        "original_filename": original_filename,
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


def read_validated_file_upload(
    file_item,
    *,
    allowed_extensions: dict[str, str] | None = None,
    max_bytes: int = MAX_GENERIC_UPLOAD_BYTES,
) -> dict | None:
    if file_item is None or not getattr(file_item, "filename", ""):
        return None
    allowed_extensions = allowed_extensions or ALLOWED_DOCUMENT_EXTENSIONS
    original_filename = Path(file_item.filename).name
    extension = Path(original_filename).suffix.lower()
    if extension not in allowed_extensions:
        raise ValueError("文件只支持 Excel、CSV、ZIP、PDF 或常见图片格式。")
    content = file_item.file.read()
    if not content:
        return None
    if len(content) > max_bytes:
        raise ValueError("上传文件大小不能超过 20MB。")
    return {
        "original_filename": original_filename,
        "extension": extension,
        "content": content,
        "content_type": allowed_extensions.get(extension, "application/octet-stream"),
    }


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
    (path / filename).write_bytes(upload_payload["content"])
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


def media_content_type(media_path: str) -> str:
    extension = Path(media_path).suffix.lower()
    return ALLOWED_IMAGE_EXTENSIONS.get(extension, "application/octet-stream")


def generic_content_type(stored_path: str) -> str:
    guessed_type, _ = guess_type(stored_path)
    if guessed_type:
        return guessed_type
    extension = Path(stored_path).suffix.lower()
    return ALLOWED_DOCUMENT_EXTENSIONS.get(extension, "application/octet-stream")
