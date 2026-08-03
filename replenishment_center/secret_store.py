from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path


def _key_path(db_path: str | Path) -> Path:
    return Path(db_path).with_suffix(".key")


def _load_key(db_path: str | Path) -> bytes:
    environment_key = os.environ.get("REPLENISH_CREDENTIAL_KEY", "").strip()
    if environment_key:
        return hashlib.sha256(environment_key.encode("utf-8")).digest()
    path = _key_path(db_path)
    if path.exists():
        key = path.read_bytes()
        if len(key) != 32:
            raise ValueError("本机 API 凭证密钥文件格式不正确。")
        return key
    path.parent.mkdir(parents=True, exist_ok=True)
    key = os.urandom(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as file_obj:
        file_obj.write(key)
    return key


def _stream(key: bytes, nonce: bytes, length: int) -> bytes:
    output = bytearray()
    counter = 0
    while len(output) < length:
        output.extend(hmac.new(key, b"enc" + nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(output[:length])


def seal(db_path: str | Path, value: str) -> str:
    plain = str(value or "").encode("utf-8")
    if not plain:
        return ""
    key = _load_key(db_path)
    nonce = os.urandom(16)
    cipher = bytes(left ^ right for left, right in zip(plain, _stream(key, nonce, len(plain))))
    tag = hmac.new(key, b"tag" + nonce + cipher, hashlib.sha256).digest()
    payload = base64.urlsafe_b64encode(nonce + tag + cipher).decode("ascii")
    return f"v1:{payload}"


def unseal(db_path: str | Path, value: str) -> str:
    encoded = str(value or "")
    if not encoded:
        return ""
    if not encoded.startswith("v1:"):
        raise ValueError("API 凭证密文版本不受支持。")
    try:
        payload = base64.urlsafe_b64decode(encoded[3:].encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ValueError("API 凭证密文损坏。") from exc
    if len(payload) < 48:
        raise ValueError("API 凭证密文损坏。")
    nonce, tag, cipher = payload[:16], payload[16:48], payload[48:]
    key = _load_key(db_path)
    expected = hmac.new(key, b"tag" + nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise ValueError("API 凭证无法解密，请检查本机凭证密钥。")
    plain = bytes(left ^ right for left, right in zip(cipher, _stream(key, nonce, len(cipher))))
    return plain.decode("utf-8")
