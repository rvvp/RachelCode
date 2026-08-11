from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PRODUCTION_GATEWAY = "https://vop.vipapis.com"
SANDBOX_GATEWAY = "http://sandbox.vipapis.com"
OAUTH_AUTHORIZE_URL = "https://auth.vip.com/oauth2/authorize"
OAUTH_TOKEN_URL = "https://auth.vip.com/oauth2/token"
OAUTH_TOKEN_INFO_URL = "https://auth.vip.com/oauth2/token_info"
STORE_SERVICE = "vipapis.marketplace.store.StoreInfoService"
PRODUCT_SERVICE = "vipapis.marketplace.product.ProductService"
ORDER_SERVICE = "vipapis.marketplace.delivery.SovDeliveryService"
INVENTORY_SERVICE = "vipapis.marketplace.inventory.InventoryService"


class VipshopAPIError(RuntimeError):
    def __init__(self, code: str, message: str, *, http_status: int | None = None, payload: dict | None = None):
        self.code = str(code or "vipshop-api-error")
        self.message = str(message or "唯品会接口返回未知错误")
        self.http_status = http_status
        self.payload = payload or {}
        super().__init__(f"{self.code}: {self.message}")


@dataclass(frozen=True)
class VipshopConfig:
    environment: str
    app_key: str
    app_secret: str
    access_token: str
    expected_store_name: str = "马天奴"

    @property
    def gateway(self) -> str:
        return SANDBOX_GATEWAY if self.environment == "sandbox" else PRODUCTION_GATEWAY

    @property
    def complete(self) -> bool:
        return bool(self.app_key and self.app_secret and self.access_token)


def build_authorization_url(app_key: str, redirect_uri: str, state: str) -> str:
    app_key = str(app_key or "").strip()
    redirect_uri = str(redirect_uri or "").strip()
    state = str(state or "").strip()
    if not app_key or not redirect_uri or not state:
        raise ValueError("生成唯品授权地址需要 AppKey、回调地址和 state。")
    params = {
        'client_id': app_key,
        'response_type': 'code',
        'redirect_uri': redirect_uri,
        'state': state,
    }
    return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"


def _oauth_post(url: str, payload: dict[str, str], *, timeout: int = 25) -> dict:
    request = Request(
        url,
        data=urlencode(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "MerchandiseMonitor/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except URLError as exc:
        raise VipshopAPIError("oauth-network-error", f"无法连接唯品授权服务器：{exc.reason}") from exc
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VipshopAPIError(
            "oauth-invalid-response", "唯品授权服务器没有返回有效 JSON。", http_status=status
        ) from exc
    error = result.get("error") or result.get("code")
    if error and str(error).lower() not in {"0", "200", "success"}:
        message = result.get("error_description") or result.get("msg") or result.get("message") or "OAuth 请求失败"
        raise VipshopAPIError(str(error), str(message), http_status=status, payload=result)
    if status >= 400:
        raise VipshopAPIError(
            "oauth-http-error", f"唯品授权服务器返回 HTTP {status}", http_status=status, payload=result
        )
    return result


def inspect_access_token(access_token: str, *, timeout: int = 20) -> dict:
    token = str(access_token or "").strip()
    if not token:
        raise ValueError("缺少待校验的 AccessToken。")
    result = _oauth_post(OAUTH_TOKEN_INFO_URL, {"access_token": token}, timeout=timeout)
    returned_token = str(result.get("access_token") or "").strip()
    if not returned_token:
        message = str(result.get("msg") or result.get("message") or "TokenInfo 未返回 AccessToken。")
        raise VipshopAPIError("oauth-token-invalid", message, payload=result)
    if not secrets_compare(token, returned_token):
        raise VipshopAPIError("oauth-token-mismatch", "唯品返回的 Token 校验结果不一致。", payload=result)
    return result


def secrets_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(str(left).encode("utf-8"), str(right).encode("utf-8"))


def exchange_authorization_code(
    *,
    app_key: str,
    app_secret: str,
    code: str,
    redirect_uri: str,
    request_client_ip: str,
    timeout: int = 25,
) -> dict:
    values = {
        "client_id": str(app_key or "").strip(),
        "client_secret": str(app_secret or "").strip(),
        "grant_type": "authorization_code",
        "redirect_uri": str(redirect_uri or "").strip(),
        "request_client_ip": str(request_client_ip or "").strip(),
        "code": str(code or "").strip(),
    }
    if not all(values.values()):
        raise ValueError("兑换唯品 AccessToken 的参数不完整。")
    result = _oauth_post(OAUTH_TOKEN_URL, values, timeout=timeout)
    access_token = str(result.get("access_token") or "").strip()
    if not access_token:
        raise VipshopAPIError("oauth-token-missing", "唯品授权成功响应中缺少 AccessToken。", payload=result)
    token_info = inspect_access_token(access_token, timeout=timeout)
    normalized = dict(result)
    normalized["token_info"] = token_info
    normalized["open_id"] = str(result.get("open_id") or token_info.get("open_id") or "").strip()
    return normalized


def canonical_json(payload: dict | None) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def calculate_sign(system_params: dict[str, str], request_body: str, app_secret: str) -> str:
    signing_text = "".join(f"{key}{system_params[key]}" for key in sorted(system_params) if key not in {"sign", "appSecret"})
    signing_text += request_body
    return hmac.new(app_secret.encode("utf-8"), signing_text.encode("utf-8"), hashlib.md5).hexdigest().upper()


class VipshopClient:
    def __init__(self, config: VipshopConfig, *, timeout: int = 25):
        self.config = config
        self.timeout = timeout

    def call(self, service: str, method: str, payload: dict | None = None, *, version: str = "1.0.0") -> dict:
        if not self.config.complete:
            raise VipshopAPIError("credentials-missing", "缺少 AppKey、AppSecret 或 AccessToken。")
        request_body = canonical_json(payload)
        params = {
            "service": service,
            "method": method,
            "version": version,
            "timestamp": str(int(time.time())),
            "format": "json",
            "appKey": self.config.app_key,
            "accessToken": self.config.access_token,
        }
        params["sign"] = calculate_sign(params, request_body, self.config.app_secret)
        url = f"{self.config.gateway}?{urlencode(params)}"
        request = Request(
            url,
            data=request_body.encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "ReplenishmentCenter/1.0"},
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
            raise VipshopAPIError("network-error", f"无法连接唯品会网关：{exc.reason}") from exc
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise VipshopAPIError("invalid-response", "唯品会网关没有返回有效 JSON。", http_status=status) from exc
        return_code = result.get("returnCode") or result.get("code")
        if return_code and str(return_code).lower() not in {"0", "200", "success", "vipapis.success"}:
            message = result.get("returnMessage") or result.get("message") or result.get("msg") or "接口调用失败"
            raise VipshopAPIError(str(return_code), str(message), http_status=status, payload=result)
        if status >= 400:
            raise VipshopAPIError("http-error", f"唯品会网关返回 HTTP {status}", http_status=status, payload=result)
        return result


def probe_gateway(environment: str = "production", *, timeout: int = 12) -> dict:
    gateway = SANDBOX_GATEWAY if environment == "sandbox" else PRODUCTION_GATEWAY
    request = Request(gateway, headers={"User-Agent": "ReplenishmentCenter/1.0"}, method="GET")
    started = time.monotonic()
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = exc.code
    except URLError as exc:
        return {"ok": False, "gateway": gateway, "latency_ms": None, "message": f"官方网关不可达：{exc.reason}"}
    latency = int((time.monotonic() - started) * 1000)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = {}
    gateway_alive = status < 500 and payload.get("returnCode") == "vipapis.miss-parameter"
    return {
        "ok": gateway_alive,
        "gateway": gateway,
        "latency_ms": latency,
        "message": "官方网关可达" if gateway_alive else f"官方网关响应异常（HTTP {status}）",
    }


def _success(response: dict):
    if "success" in response:
        return response["success"]
    if "data" in response and isinstance(response["data"], dict) and "success" in response["data"]:
        return response["data"]["success"]
    return response.get("data", response)


def test_store_connection(client: VipshopClient) -> dict:
    response = client.call(STORE_SERVICE, "getStoreInfo", {})
    store = _success(response)
    if not isinstance(store, dict):
        raise VipshopAPIError("store-response-invalid", "店铺信息接口返回结构无法识别。")
    expected = client.config.expected_store_name.strip()
    actual = str(store.get("store_name") or "").strip()
    if expected and expected not in actual:
        raise VipshopAPIError("store-mismatch", f"授权店铺“{actual or '未知'}”与预期“{expected}”不匹配。")
    return store


def _batches(values: list[str], size: int):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _property_value(properties: dict, candidates: tuple[str, ...]) -> str:
    for key, value in (properties or {}).items():
        normalized = str(key).strip().lower()
        if any(candidate in normalized for candidate in candidates):
            return str(value or "").strip()
    return ""


def collect_store_data(client: VipshopClient, *, as_of: date | None = None, max_products: int = 1000) -> dict:
    as_of = as_of or date.today()
    store = test_store_connection(client)
    external_skus: dict[str, dict] = {}
    product_count = 0
    page = 1
    while product_count < max_products:
        response = client.call(PRODUCT_SERVICE, "getProducts", {"request": {"limit": 200, "page": page}})
        result = _success(response) or {}
        products = result.get("products") or []
        for product in products:
            if product_count >= max_products:
                break
            product_count += 1
            spu_id = str(product.get("spu_id") or "").strip()
            if not spu_id:
                continue
            detail_response = client.call(PRODUCT_SERVICE, "getProductById", {"spu_id": spu_id})
            detail = _success(detail_response) or {}
            for sku in detail.get("skus") or []:
                external_id = str(sku.get("sku_id") or "").strip()
                if not external_id:
                    continue
                properties = sku.get("sale_props") or {}
                external_skus[external_id] = {
                    "external_sku_id": external_id,
                    "external_spu_id": str(detail.get("spu_id") or spu_id),
                    "outer_sku_id": str(sku.get("outer_sku_id") or "").strip(),
                    "style_code": str(detail.get("outer_spu_id") or product.get("outer_spu_id") or "").strip(),
                    "style_name": str(detail.get("title") or product.get("title") or "").strip(),
                    "color_name": _property_value(properties, ("颜色", "color", "134")),
                    "size_name": _property_value(properties, ("尺码", "尺寸", "size", "453")),
                    "sale_props": properties,
                }
        if not result.get("has_next") or not products:
            break
        page += 1

    start = datetime.combine(as_of - timedelta(days=13), datetime.min.time()).strftime("%Y-%m-%d %H:%M:%S")
    end = datetime.combine(as_of, datetime.max.time().replace(microsecond=0)).strftime("%Y-%m-%d %H:%M:%S")
    order_headers: dict[str, dict] = {}
    page = 1
    while True:
        response = client.call(
            ORDER_SERVICE,
            "getOrders",
            {"request": {"query_start_time": start, "query_end_time": end, "date_type": 1, "limit": 200, "page": page, "order_type": "OUTBOUND"}},
        )
        result = _success(response) or {}
        orders = result.get("orders") or []
        for order in orders:
            order_id = str(order.get("order_id") or "").strip()
            if order_id:
                order_headers[order_id] = order
        total = _int(result.get("total"), len(order_headers))
        if not orders or len(order_headers) >= total or len(orders) < 200:
            break
        page += 1

    sales: dict[tuple[str, str], int] = defaultdict(int)
    order_ids = list(order_headers)
    for batch in _batches(order_ids, 50):
        details_response = client.call(ORDER_SERVICE, "getOrderDetail", {"order_ids": batch, "order_type": "OUTBOUND"})
        details = _success(details_response) or []
        if isinstance(details, dict):
            details = details.get("orders") or details.get("order_details") or []
        for detail in details:
            order_id = str(detail.get("order_id") or "").strip()
            header = order_headers.get(order_id, {})
            if str(header.get("status") or "") in {"70", "97"}:
                continue
            sale_date = str(header.get("created") or header.get("store_add_time") or "")[:10]
            if not sale_date:
                continue
            for product in detail.get("order_products") or []:
                external_id = str(product.get("sku_id") or "").strip()
                if not external_id:
                    continue
                sales[(external_id, sale_date)] += max(0, _int(product.get("num")))
                sku = external_skus.setdefault(external_id, {"external_sku_id": external_id, "sale_props": {}})
                sku.update(
                    {
                        "external_spu_id": str(product.get("spu_id") or sku.get("external_spu_id") or "").strip(),
                        "outer_sku_id": str(product.get("outer_sku_id") or sku.get("outer_sku_id") or "").strip(),
                        "style_code": str(product.get("outer_spu_id") or sku.get("style_code") or "").strip(),
                        "style_name": str(product.get("title") or sku.get("style_name") or "").strip(),
                        "color_name": str(product.get("color") or sku.get("color_name") or "").strip(),
                        "size_name": str(product.get("size") or sku.get("size_name") or "").strip(),
                    }
                )

    inventories = []
    for external_id in sorted(external_skus):
        response = client.call(INVENTORY_SERVICE, "getSkuStock", {"getSkuStockRequest": {"sku_id": external_id}})
        result = _success(response) or {}
        stocks = result.get("sku_stocks") or []
        leaving = sum(max(0, _int(stock.get("leaving_stock"))) for stock in stocks)
        cart_hold = sum(max(0, _int(stock.get("cart_hold"))) for stock in stocks)
        order_hold = sum(max(0, _int(stock.get("order_hold"))) for stock in stocks)
        inventories.append(
            {
                "external_sku_id": external_id,
                "on_hand": leaving + cart_hold + order_hold,
                "locked": cart_hold + order_hold,
                "defective": 0,
                "inbound": 0,
                "inbound_date": "",
            }
        )

    return {
        "store": store,
        "skus": list(external_skus.values()),
        "sales": [
            {"external_sku_id": external_id, "sale_date": sale_date, "gross_units": units, "return_units": 0}
            for (external_id, sale_date), units in sorted(sales.items())
        ],
        "inventory": inventories,
        "sales_through_date": as_of.isoformat(),
        "inventory_snapshot_at": datetime.now().replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
        "order_count": len(order_headers),
        "product_count": product_count,
    }


def test_configured_connection(db_path, store_id: int | None = None) -> dict:
    from replenishment_center import db

    config = db.vipshop_client_config(db_path, store_id)
    gateway = probe_gateway(config.environment)
    if not config.complete:
        message = f"{gateway['message']}；尚缺少 AppKey、AppSecret 或 AccessToken。"
        db.record_api_test(db_path, store_id, status="credentials_missing", message=message)
        return {"ok": False, "credentials_complete": False, "gateway": gateway, "message": message, "store": {}}
    try:
        store = test_store_connection(VipshopClient(config))
    except VipshopAPIError as exc:
        message = f"官方网关可达，但鉴权失败：{exc.message}（{exc.code}）"
        db.record_api_test(db_path, store_id, status="failed", message=message)
        return {"ok": False, "credentials_complete": True, "gateway": gateway, "message": message, "store": {}}
    message = f"鉴权成功，已绑定店铺：{store.get('store_name') or '未知店铺'}。"
    db.record_api_test(db_path, store_id, status="connected", message=message, store=store)
    return {"ok": True, "credentials_complete": True, "gateway": gateway, "message": message, "store": store}


def sync_to_database(
    db_path,
    *,
    user_id: int | None = None,
    as_of: date | None = None,
    max_products: int = 1000,
    store_id: int | None = None,
) -> dict:
    from replenishment_center import db

    config = db.vipshop_client_config(db_path, store_id)
    if not config.complete:
        message = "无法执行唯品会同步：缺少 AppKey、AppSecret 或 AccessToken。"
        db.record_api_sync_failure(db_path, message, user_id, store_id=store_id)
        raise VipshopAPIError("credentials-missing", message)
    try:
        data = collect_store_data(VipshopClient(config), as_of=as_of, max_products=max_products)
        return db.apply_vipshop_data(db_path, data, user_id, store_id=store_id)
    except VipshopAPIError as exc:
        message = f"唯品会 API 同步失败：{exc.message}（{exc.code}）"
        db.record_api_sync_failure(db_path, message, user_id, store_id=store_id)
        raise
