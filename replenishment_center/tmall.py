from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PRODUCTION_GATEWAY = "https://eco.taobao.com/router/rest"
SANDBOX_GATEWAY = "https://gw.api.tbsandbox.com/router/rest"
OAUTH_AUTHORIZE_URL = "https://oauth.taobao.com/authorize"
OAUTH_TOKEN_URL = "https://oauth.taobao.com/token"
SHOP_METHOD = "taobao.shop.seller.get"


class TmallAPIError(RuntimeError):
    def __init__(self, code: str, message: str, *, http_status: int | None = None, payload: dict | None = None):
        self.code = str(code or "tmall-api-error")
        self.message = str(message or "天猫接口返回未知错误")
        self.http_status = http_status
        self.payload = payload or {}
        super().__init__(f"{self.code}: {self.message}")


@dataclass(frozen=True)
class TmallConfig:
    environment: str
    app_key: str
    app_secret: str
    session_key: str
    expected_store_name: str = "马天奴天猫官方旗舰店"

    @property
    def gateway(self) -> str:
        return SANDBOX_GATEWAY if self.environment == "sandbox" else PRODUCTION_GATEWAY

    @property
    def complete(self) -> bool:
        return bool(self.app_key and self.app_secret and self.session_key)


def calculate_sign(params: dict[str, str], app_secret: str) -> str:
    signing_text = "".join(f"{key}{params[key]}" for key in sorted(params) if key != "sign")
    return hmac.new(
        app_secret.encode("utf-8"), signing_text.encode("utf-8"), hashlib.md5
    ).hexdigest().upper()


class TmallClient:
    def __init__(self, config: TmallConfig, *, timeout: int = 25):
        self.config = config
        self.timeout = timeout

    def call(self, method: str, payload: dict | None = None) -> dict:
        if not self.config.complete:
            raise TmallAPIError("credentials-missing", "缺少 AppKey、AppSecret 或店铺授权 SessionKey。")
        params = {
            "method": method,
            "app_key": self.config.app_key,
            "session": self.config.session_key,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "format": "json",
            "v": "2.0",
            "sign_method": "hmac",
        }
        for key, value in (payload or {}).items():
            if value is not None:
                params[str(key)] = str(value)
        params["sign"] = calculate_sign(params, self.config.app_secret)
        request = Request(
            self.config.gateway,
            data=urlencode(params).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                "User-Agent": "ReplenishmentCenter/1.0",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.status
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status = exc.code
        except URLError as exc:
            raise TmallAPIError("network-error", f"无法连接淘宝开放平台网关：{exc.reason}") from exc
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TmallAPIError(
                "invalid-response", "淘宝开放平台没有返回有效 JSON。", http_status=status
            ) from exc
        error = result.get("error_response")
        if isinstance(error, dict):
            raise TmallAPIError(
                str(error.get("sub_code") or error.get("code") or "top-error"),
                str(error.get("sub_msg") or error.get("msg") or "接口调用失败"),
                http_status=status,
                payload=result,
            )
        if status >= 400:
            raise TmallAPIError("http-error", f"淘宝开放平台返回 HTTP {status}", http_status=status)
        return result


def probe_gateway(environment: str = "production", *, timeout: int = 12) -> dict:
    gateway = SANDBOX_GATEWAY if environment == "sandbox" else PRODUCTION_GATEWAY
    params = {
        "method": SHOP_METHOD,
        "format": "json",
        "v": "2.0",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    request = Request(
        gateway,
        data=urlencode(params).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "ReplenishmentCenter/1.0",
        },
        method="POST",
    )
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except URLError as exc:
        return {
            "ok": False,
            "gateway": gateway,
            "latency_ms": None,
            "message": f"官方网关不可达：{exc.reason}",
        }
    latency = int((time.monotonic() - started) * 1000)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    error = payload.get("error_response") or {}
    message_text = f"{error.get('msg', '')}{error.get('sub_msg', '')}".lower()
    gateway_alive = status < 500 and isinstance(error, dict) and (
        str(error.get("code")) == "28" or "app key" in message_text or "app_key" in message_text
    )
    return {
        "ok": gateway_alive,
        "gateway": gateway,
        "latency_ms": latency,
        "message": "淘宝开放平台官方网关可达" if gateway_alive else f"官方网关响应异常（HTTP {status}）",
    }


def _shop_from_response(response: dict) -> dict:
    result = response.get("shop_seller_get_response") or {}
    shop = result.get("shop") or {}
    if not isinstance(shop, dict) or not shop:
        raise TmallAPIError("shop-response-invalid", "店铺信息接口返回结构无法识别。")
    return shop


def _store_name_matches(expected: str, actual: str) -> bool:
    def normalize(value: str) -> str:
        text = "".join(str(value or "").lower().split())
        for word in ("天猫", "官方", "旗舰店"):
            text = text.replace(word, "")
        return text

    target = normalize(expected)
    observed = normalize(actual)
    return bool(target and (target in observed or observed in target))


def test_store_connection(client: TmallClient) -> dict:
    response = client.call(
        SHOP_METHOD,
        {"fields": "sid,cid,title,nick,desc,bulletin,pic_path,created,modified"},
    )
    shop = _shop_from_response(response)
    store_name = str(shop.get("title") or shop.get("nick") or "").strip()
    if not _store_name_matches(client.config.expected_store_name, store_name):
        raise TmallAPIError(
            "store-mismatch",
            f"授权店铺“{store_name or '未知'}”与预期“{client.config.expected_store_name}”不匹配。",
        )
    return {
        "shop_id": str(shop.get("sid") or ""),
        "seller_nick": str(shop.get("nick") or ""),
        "store_name": store_name,
        "raw": shop,
    }


def test_configured_connection(db_path, store_id: int) -> dict:
    from replenishment_center import db

    config = db.tmall_client_config(db_path, store_id)
    gateway = probe_gateway(config.environment)
    if not config.complete:
        message = f"{gateway['message']}；尚缺少 AppKey、AppSecret 或店铺授权 SessionKey。"
        db.record_tmall_api_test(
            db_path, store_id, status="credentials_missing", message=message
        )
        return {
            "ok": False,
            "credentials_complete": False,
            "gateway": gateway,
            "message": message,
            "store": {},
        }
    try:
        store = test_store_connection(TmallClient(config))
    except TmallAPIError as exc:
        message = f"官方网关可达，但店铺鉴权失败：{exc.message}（{exc.code}）"
        db.record_tmall_api_test(db_path, store_id, status="failed", message=message)
        return {
            "ok": False,
            "credentials_complete": True,
            "gateway": gateway,
            "message": message,
            "store": {},
        }
    message = f"鉴权成功，已绑定店铺：{store['store_name'] or store['seller_nick']}。"
    db.record_tmall_api_test(
        db_path, store_id, status="connected", message=message, store=store
    )
    return {
        "ok": True,
        "credentials_complete": True,
        "gateway": gateway,
        "message": message,
        "store": store,
    }
