from __future__ import annotations

import cgi
import html
import io
import secrets
from collections import Counter
from datetime import datetime
from http import cookies
from pathlib import Path
from urllib.parse import parse_qs, quote
from zoneinfo import ZoneInfo

from replenishment_center import db
from replenishment_center.browser_capture import launch_dedicated_browser, run_browser_worker
from replenishment_center.excel import data_template_bytes, import_data_workbook, plan_workbook_bytes
from replenishment_center.tmall import test_configured_connection as test_tmall_connection
from replenishment_center.vipshop import VipshopAPIError, sync_to_database, test_configured_connection


SESSIONS: dict[str, int] = {}
ROLE_LABELS = {"merchandise": "商品部", "followup": "跟单部", "manager": "管理层", "admin": "管理员"}
PLAN_STATUS = {
    "merchandise_pending": "待商品部确认",
    "merchandise_editing": "商品部修正中",
    "followup_pending": "待跟单部确认",
    "followup_processing": "跟单处理中",
    "completed": "已完成",
    "superseded": "已被新条件替代",
}
RISK_LABELS = {"critical": "7天内缺货", "warning": "14天内缺货", "watch": "库存关注", "healthy": "库存健康", "no_sales": "暂无销量"}
WEEKDAY_LABELS = {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}
FOLLOWUP_LABELS = {"pending": "待处理", "confirmed": "可供确认", "limited": "供应受限", "ordered": "已下单", "arrived": "已到货"}


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def number(value) -> str:
    return f"{int(value or 0):,}"


def decimal(value, digits: int = 1) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}"


def local_time(value) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return text


class ReplenishmentApplication:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        query = {key: values[0] for key, values in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()}
        user = self.current_user(environ)
        try:
            if path == "/favicon.ico":
                start_response("204 No Content", [])
                return [b""]
            if path == "/healthz":
                return self.text_response(start_response, "ok")
            if path == "/login":
                if method == "GET":
                    return self.html_response(start_response, self.render_login())
                return self.handle_login(environ, start_response)
            if path == "/logout" and method == "POST":
                return self.handle_logout(environ, start_response)
            if not user:
                return self.redirect(start_response, "/login")
            if path == "/":
                return self.redirect(start_response, "/dashboard")
            if path == "/dashboard" and method == "GET":
                return self.html_response(start_response, self.render_dashboard(user, query))
            if path == "/plans" and method == "GET":
                return self.html_response(start_response, self.render_plans(user, query))
            if path == "/followup" and method == "GET":
                return self.html_response(start_response, self.render_followup_inbox(user))
            if path == "/data" and method == "GET":
                self.require_role(user, {"merchandise", "admin"})
                return self.html_response(start_response, self.render_data_center(user, query))
            if path == "/data/api" and method == "GET":
                self.require_role(user, {"merchandise", "admin"})
                return self.html_response(start_response, self.render_api_config(user, query))
            if path == "/data/api/config" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                return self.handle_api_config(environ, start_response, user)
            if path == "/data/api/test" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                store = self.selected_store(query)
                if store["platform_code"] != "vip":
                    raise ValueError("当前店铺不是唯品会店铺。")
                result = test_configured_connection(self.db_path, store["id"])
                return self.redirect(
                    start_response,
                    f"/data/api?store={quote(store['store_code'])}&message="
                    + quote(result["message"]),
                )
            if path == "/data/api/sync" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                return self.handle_api_sync(start_response, user, query)
            if path == "/data/tmall-api" and method == "GET":
                self.require_role(user, {"merchandise", "admin"})
                return self.html_response(start_response, self.render_tmall_api_config(user, query))
            if path == "/data/tmall-api/config" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                return self.handle_tmall_api_config(environ, start_response, user)
            if path == "/data/tmall-api/test" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                store = db.get_store(self.db_path, "TMALL-MTN-FLAGSHIP")
                result = test_tmall_connection(self.db_path, store["id"])
                return self.redirect(
                    start_response,
                    "/data/tmall-api?store=TMALL-MTN-FLAGSHIP&message=" + quote(result["message"]),
                )
            if path == "/data/browser" and method == "GET":
                self.require_role(user, {"merchandise", "admin"})
                return self.html_response(start_response, self.render_browser_capture(user, query))
            if path == "/data/browser/open" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                config = db.get_browser_capture_config(self.db_path)
                status = launch_dedicated_browser(
                    self.db_path, port=int(config["debug_port"]), url=config["backend_url"]
                )
                db.record_browser_status(self.db_path, status)
                return self.redirect(start_response, "/data/browser?message=" + quote(status["message"]))
            if path == "/data/browser/check" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                config = db.get_browser_capture_config(self.db_path)
                status = run_browser_worker(self.db_path, port=int(config["debug_port"]))
                db.record_browser_status(self.db_path, status)
                return self.redirect(start_response, "/data/browser?message=" + quote(status["message"]))
            if path == "/data/browser/save-page" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                return self.handle_browser_save_page(environ, start_response, user)
            if path == "/data/browser/capture" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                return self.handle_browser_capture(environ, start_response, user)
            if path == "/data/browser/scan" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                completed = db.process_browser_capture_jobs(self.db_path)
                message = f"已接收 {len(completed)} 份新报表" if completed else "尚未发现新的完整下载文件"
                return self.redirect(start_response, "/data/browser?message=" + quote(message))
            if path == "/data/browser/import" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                return self.handle_browser_import(start_response, user)
            if path == "/data/template.xlsx" and method == "GET":
                self.require_role(user, {"merchandise", "admin"})
                return self.file_response(start_response, data_template_bytes(), "智能补货中心-数据模板.xlsx")
            if path == "/data/import" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                return self.handle_data_import(environ, start_response, user)
            if path == "/data/test-sync" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                db.record_test_sync(self.db_path, source="manual_test")
                return self.redirect(start_response, "/data?message=" + quote("试跑数据校验完成"))
            if path == "/plans/generate" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                plan_id = db.generate_plan(self.db_path, generation_type="manual", created_by=user["id"], force=True)
                return self.redirect(start_response, f"/plans/{plan_id}?message=" + quote("补货计划已生成"))
            if path == "/settings" and method == "GET":
                self.require_role(user, {"merchandise", "admin"})
                return self.html_response(start_response, self.render_settings(user, query))
            if path == "/settings" and method == "POST":
                self.require_role(user, {"merchandise", "admin"})
                return self.handle_settings(environ, start_response, user)
            if path == "/notifications/read" and method == "POST":
                store = self.selected_store(query)
                db.mark_notifications_read(
                    self.db_path, user["id"], store_id=store["id"]
                )
                return self.redirect(start_response, environ.get("HTTP_REFERER", "/dashboard"))
            if path.startswith("/plans/"):
                return self.handle_plan_route(environ, start_response, user, path, method, query)
            return self.error_response(start_response, "404 Not Found", "页面不存在")
        except PermissionError as exc:
            return self.error_response(start_response, "403 Forbidden", str(exc) or "没有权限执行此操作")
        except ValueError as exc:
            return self.error_response(start_response, "400 Bad Request", str(exc))
        except Exception as exc:
            return self.error_response(start_response, "500 Internal Server Error", f"系统处理失败：{exc}")

    def handle_plan_route(self, environ, start_response, user, path: str, method: str, query: dict):
        parts = [part for part in path.split("/") if part]
        if len(parts) < 2 or not parts[1].isdigit():
            return self.error_response(start_response, "404 Not Found", "计划不存在")
        plan_id = int(parts[1])
        suffix = parts[2] if len(parts) > 2 else ""
        if not suffix and method == "GET":
            return self.html_response(start_response, self.render_plan_detail(user, plan_id, query))
        if suffix == "save" and method == "POST":
            self.require_role(user, {"merchandise", "admin"})
            return self.handle_plan_save(environ, start_response, user, plan_id)
        if suffix == "submit" and method == "POST":
            self.require_role(user, {"merchandise", "admin"})
            db.submit_to_followup(self.db_path, plan_id, user["id"])
            return self.redirect(start_response, f"/plans/{plan_id}?message=" + quote("已提交跟单部"))
        if suffix == "followup" and method == "GET":
            self.require_role(user, {"followup", "admin"})
            return self.html_response(start_response, self.render_followup_plan(user, plan_id, query))
        if suffix == "followup" and method == "POST":
            self.require_role(user, {"followup", "admin"})
            return self.handle_followup_save(environ, start_response, user, plan_id)
        if suffix == "export.xlsx" and method == "GET":
            plan = db.get_plan(self.db_path, plan_id)
            if not plan:
                raise ValueError("计划不存在。")
            content = plan_workbook_bytes(plan, db.get_plan_items(self.db_path, plan_id))
            return self.file_response(start_response, content, f"{plan['plan_no']}-补货明细.xlsx")
        return self.error_response(start_response, "404 Not Found", "页面不存在")

    def current_user(self, environ) -> dict | None:
        raw_cookie = environ.get("HTTP_COOKIE", "")
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw_cookie)
        except cookies.CookieError:
            return None
        session_cookie = jar.get("replenishment_session")
        if not session_cookie:
            return None
        user_id = SESSIONS.get(session_cookie.value)
        return db.get_user(self.db_path, user_id) if user_id else None

    def handle_login(self, environ, start_response):
        form = self.parse_urlencoded(environ)
        user = db.authenticate(self.db_path, form.get("username", ""), form.get("password", ""))
        if not user:
            return self.html_response(start_response, self.render_login("账号或密码不正确"), status="401 Unauthorized")
        token = secrets.token_urlsafe(32)
        SESSIONS[token] = user["id"]
        headers = [("Location", "/dashboard"), ("Set-Cookie", f"replenishment_session={token}; Path=/; HttpOnly; SameSite=Lax")]
        start_response("302 Found", headers)
        return [b""]

    def handle_logout(self, environ, start_response):
        jar = cookies.SimpleCookie()
        jar.load(environ.get("HTTP_COOKIE", ""))
        token = jar.get("replenishment_session")
        if token:
            SESSIONS.pop(token.value, None)
        start_response(
            "302 Found",
            [("Location", "/login"), ("Set-Cookie", "replenishment_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")],
        )
        return [b""]

    def handle_data_import(self, environ, start_response, user):
        form = cgi.FieldStorage(fp=environ["wsgi.input"], environ=environ, keep_blank_values=True)
        upload = form["data_file"] if "data_file" in form else None
        if upload is None or not getattr(upload, "file", None):
            raise ValueError("请选择需要导入的 Excel 文件。")
        counts = import_data_workbook(self.db_path, upload.file, user["id"])
        message = f"导入成功：SKU {counts['sku']} 行，销售 {counts['sales']} 行，库存 {counts['inventory']} 行"
        return self.redirect(start_response, "/data?message=" + quote(message))

    def handle_api_config(self, environ, start_response, user):
        form = self.parse_urlencoded(environ)
        store = db.get_store(self.db_path, form.get("store_code", "VIP-MTN"))
        if not store or store["platform_code"] != "vip":
            raise ValueError("当前店铺不是唯品会店铺。")
        db.save_api_config(
            self.db_path,
            {
                "environment": form.get("environment", "production"),
                "app_key": form.get("app_key", ""),
                "app_secret": form.get("app_secret", ""),
                "access_token": form.get("access_token", ""),
                "expected_store_name": form.get("expected_store_name", "马天奴"),
                "clear_app_secret": form.get("clear_app_secret") == "1",
                "clear_access_token": form.get("clear_access_token") == "1",
            },
            user["id"],
            store["id"],
        )
        return self.redirect(
            start_response,
            f"/data/api?store={quote(store['store_code'])}&message="
            + quote("唯品会 API 配置已加密保存，请执行测试连接"),
        )

    def handle_api_sync(self, start_response, user, query):
        store = self.selected_store(query)
        if store["platform_code"] != "vip":
            raise ValueError("当前店铺不是唯品会店铺。")
        redirect_url = f"/data/api?store={quote(store['store_code'])}&message="
        try:
            result = sync_to_database(
                self.db_path, user_id=user["id"], store_id=store["id"]
            )
        except (VipshopAPIError, ValueError) as exc:
            return self.redirect(start_response, redirect_url + quote(str(exc)))
        plan_id = db.generate_plan(
            self.db_path,
            generation_type="manual_api",
            created_by=user["id"],
            force=True,
            store_id=store["id"],
        )
        return self.redirect(
            start_response,
            f"/plans/{plan_id}?message=" + quote(f"{result['message']} 已生成真实数据补货批次。"),
        )

    def handle_tmall_api_config(self, environ, start_response, user):
        form = self.parse_urlencoded(environ)
        store = db.get_store(self.db_path, "TMALL-MTN-FLAGSHIP")
        db.save_tmall_api_config(
            self.db_path,
            store["id"],
            {
                "environment": form.get("environment", "production"),
                "app_key": form.get("app_key", ""),
                "app_secret": form.get("app_secret", ""),
                "session_key": form.get("session_key", ""),
                "expected_store_name": form.get(
                    "expected_store_name", "马天奴天猫官方旗舰店"
                ),
                "clear_app_secret": form.get("clear_app_secret") == "1",
                "clear_session_key": form.get("clear_session_key") == "1",
            },
            user["id"],
        )
        return self.redirect(
            start_response,
            "/data/tmall-api?store=TMALL-MTN-FLAGSHIP&message="
            + quote("天猫 API 配置已加密保存，请执行测试连接"),
        )

    def handle_browser_save_page(self, environ, start_response, user):
        form = self.parse_urlencoded(environ)
        kind = form.get("kind", "")
        config = db.get_browser_capture_config(self.db_path)
        status = run_browser_worker(self.db_path, port=int(config["debug_port"]))
        db.record_browser_status(self.db_path, status)
        current_url = (status.get("current") or {}).get("url", "")
        if status.get("loginRequired"):
            raise ValueError("唯品后台尚未登录，请先在专用 Chrome 中完成登录。")
        db.save_browser_report_url(self.db_path, kind, current_url, user["id"])
        label = {"sales": "销售", "inventory": "库存", "master": "商品主数据"}.get(kind, kind)
        return self.redirect(start_response, "/data/browser?message=" + quote(f"已将当前页面记录为{label}报表页"))

    def handle_browser_capture(self, environ, start_response, user):
        form = self.parse_urlencoded(environ)
        kind = form.get("kind", "")
        if kind not in {"sales", "inventory", "master"}:
            raise ValueError("报表类型不正确。")
        config = db.get_browser_capture_config(self.db_path)
        report_url = {
            "sales": config["sales_report_url"],
            "inventory": config["inventory_report_url"],
            "master": config["master_report_url"],
        }[kind]
        if not report_url:
            raise ValueError("请先在唯品专用 Chrome 中进入对应报表，并记录当前页面地址。")
        status = run_browser_worker(
            self.db_path, port=int(config["debug_port"]), action="open", url=report_url
        )
        db.record_browser_status(self.db_path, status)
        if status.get("loginRequired"):
            raise ValueError("唯品登录已失效，请重新登录后再采集。")
        db.create_browser_capture_job(self.db_path, kind, user["id"])
        label = {"sales": "销售", "inventory": "库存", "master": "商品主数据"}[kind]
        return self.redirect(
            start_response,
            "/data/browser?message=" + quote(f"已打开{label}报表页，请在专用 Chrome 中筛选并导出，系统会自动接收文件"),
        )

    def handle_browser_import(self, start_response, user):
        result = db.import_latest_browser_reports(self.db_path, user["id"])
        plan_id = result["plan_id"]
        counts = result["counts"]
        stats = result["stats"]
        message = (
            f"真实报表已导入：{stats['date_count']}天、{counts['sku']}个SKU，"
            f"未匹配条码{stats['unmatched_barcodes']}个。"
        )
        return self.redirect(start_response, f"/plans/{plan_id}?message=" + quote(message))

    def handle_settings(self, environ, start_response, user):
        form = self.parse_urlencoded(environ)
        payload = {
            "schedule_weekdays": [int(value) for value in form.getall("schedule_weekdays")],
            "schedule_time": form.get("schedule_time", ""),
            "target_days": form.get("target_days", "45"),
            "safety_days": form.get("safety_days", "7"),
            "min_sales_7": form.get("min_sales_7", "5"),
            "min_sales_14": form.get("min_sales_14", "10"),
            "min_consecutive_sales_days": form.get("min_consecutive_sales_days", "3"),
            "max_coverage_days": form.get("max_coverage_days", "14"),
            "auto_generate": form.get("auto_generate") == "1",
        }
        db.update_settings(self.db_path, payload, user["id"])
        return self.redirect(start_response, "/settings?message=" + quote("补货频率与计算参数已更新"))

    def handle_plan_save(self, environ, start_response, user, plan_id: int):
        form = self.parse_urlencoded(environ)
        items = db.get_plan_items(self.db_path, plan_id)
        quantities = {item["id"]: max(0, int(form.get(f"qty_{item['id']}", item["confirmed_qty"]))) for item in items}
        reasons = {item["id"]: form.get(f"reason_{item['id']}", "") for item in items}
        db.save_merchandise_adjustments(self.db_path, plan_id, quantities, reasons, user["id"])
        if form.get("command") == "submit":
            db.submit_to_followup(self.db_path, plan_id, user["id"])
            return self.redirect(start_response, f"/plans/{plan_id}?message=" + quote("商品部数量已确认并提交跟单部"))
        return self.redirect(start_response, f"/plans/{plan_id}?message=" + quote("商品部修正已保存"))

    def handle_followup_save(self, environ, start_response, user, plan_id: int):
        form = self.parse_urlencoded(environ)
        items = db.get_plan_items(self.db_path, plan_id)
        payloads = {}
        for item in items:
            payloads[item["id"]] = {
                "followup_qty": form.get(f"followup_qty_{item['id']}", item["confirmed_qty"]),
                "expected_order_date": form.get(f"order_date_{item['id']}", ""),
                "expected_arrival_date": form.get(f"arrival_date_{item['id']}", ""),
                "followup_status": form.get(f"followup_status_{item['id']}", "pending"),
                "followup_note": form.get(f"followup_note_{item['id']}", ""),
            }
        complete = form.get("command") == "complete"
        db.save_followup_response(self.db_path, plan_id, payloads, user["id"], complete=complete)
        message = "跟单确认已完成" if complete else "跟单进度已保存"
        return self.redirect(start_response, f"/plans/{plan_id}/followup?message=" + quote(message))

    def parse_urlencoded(self, environ):
        length = int(environ.get("CONTENT_LENGTH") or 0)
        body = environ["wsgi.input"].read(length).decode("utf-8")
        values = parse_qs(body, keep_blank_values=True)

        class FormValues(dict):
            def getall(self, key):
                return values.get(key, [])

        return FormValues({key: items[0] for key, items in values.items()})

    def require_role(self, user: dict, roles: set[str]) -> None:
        if user["role"] not in roles:
            raise PermissionError("当前账号没有该模块的操作权限。")

    def selected_store(self, query: dict | None = None) -> dict:
        query = query or {}
        requested = str(query.get("store") or "").strip()
        if requested:
            store = db.get_store(self.db_path, requested)
            if store:
                return store
        settings = db.get_settings(self.db_path)
        return db.get_store(self.db_path, settings["store_code"])

    def render_login(self, error: str = "") -> str:
        error_html = f'<div class="login-error">{esc(error)}</div>' if error else ""
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>补货监控中心</title><style>{self.css()}</style></head>
<body class="login-body"><main class="login-shell">
  <section class="login-brand">
    <div class="brand-kicker">REPLENISHMENT OPERATIONS</div>
    <h1>补货监控中心</h1>
    <p>马天奴 · 多店铺销售库存监控</p>
    <div class="login-status"><span class="live-dot"></span> 马天奴唯品会运行中 · 天猫与 BNX 联调中</div>
  </section>
  <section class="login-form-wrap">
    <div class="login-form-head"><span>内部协作入口</span><strong>登录</strong></div>
    {error_html}
    <form method="post" action="/login" class="stack-form">
      <label>账号<input name="username" autocomplete="username" required></label>
      <label>密码<input type="password" name="password" autocomplete="current-password" required></label>
      <button class="button primary wide" type="submit">进入工作台</button>
    </form>
    <div class="demo-accounts"><span>商品部 merch</span><span>跟单部 followup</span><span>密码 demo123</span></div>
  </section>
</main></body></html>"""

    def render_dashboard(self, user: dict, query: dict) -> str:
        store = self.selected_store(query)
        if store["platform_code"] == "tmall":
            return self.render_tmall_dashboard(user, store, query)
        if store["store_code"] != "VIP-MTN":
            return self.render_vip_api_dashboard(user, store, query)
        data = db.dashboard_data(self.db_path, store_id=store["id"])
        settings = db.get_settings(self.db_path)
        api_config = db.get_api_config(self.db_path, store["id"])
        sync = db.latest_sync(self.db_path, store_id=store["id"]) or {}
        current = data["current"]
        schedule_text = "、".join(WEEKDAY_LABELS[day] for day in settings["schedule_weekdays"])
        if settings["data_source_mode"] == "api":
            source_text = f"唯品会 API · {api_config['verified_store_name']}"
        elif settings["data_source_mode"] == "browser":
            source_text = "唯品浏览器官方报表"
        else:
            source_text = "单店试跑 · API 凭证待配置"
        message = query.get("message", "")
        if current:
            task_href = f"/plans/{current['id']}"
            if user["role"] == "followup" and current["status"] in {"followup_pending", "followup_processing"}:
                task_href += "/followup"
            task_action = "处理本次任务" if current["status"] != "completed" else "查看本次结果"
            task_band = f"""
            <section class="task-band">
              <div><span class="eyebrow">本期补货任务</span><h2>{esc(current['plan_no'])}</h2>
              <p>销售截至 {esc(current['sales_through_date'])} · 库存快照 {esc(current['inventory_snapshot_at'])} · 目标覆盖 {current['target_days']} 天</p></div>
              <div class="task-actions"><span class="status status-{esc(current['status'])}">{esc(PLAN_STATUS.get(current['status'], current['status']))}</span>
              <a class="button primary" href="{task_href}">{task_action}</a></div>
            </section>"""
        else:
            task_band = '<section class="empty-band"><strong>尚未生成补货计划</strong><span>商品部可从数据中心手动生成首个任务。</span></section>'
        risk_rows = "".join(self.render_risk_row(group, current["id"] if current else 0) for group in data["risk_styles"][:8])
        if not risk_rows:
            risk_rows = '<tr><td colspan="10" class="empty-cell">当前没有命中条件1或条件2的补货货号</td></tr>'
        health_total = max(1, len(data["groups"]))
        critical_width = data["critical_styles"] / health_total * 100
        warning_width = data["warning_styles"] / health_total * 100
        healthy_width = max(0, 100 - critical_width - warning_width)
        frequency_action = '<a class="button ghost" href="/settings">修改频率</a>' if user["role"] in {"merchandise", "admin"} else ""
        data_link = '<a href="/data">查看数据中心</a>' if user["role"] in {"merchandise", "admin"} else '<a href="/plans">查看补货计划</a>'
        content = f"""
        {self.flash(message)}
        <header class="page-heading"><div><span class="eyebrow">SALES & INVENTORY MONITOR</span><h1>销售库存监控</h1>
        <p>{esc(settings['platform_name'])} / {esc(settings['store_name'])}，数据与补货任务集中处理。</p></div>
        <div class="heading-actions">{frequency_action}</div></header>
        {task_band}
        <section class="metric-strip">
          <div><span>库存支撑 ≤ 7天</span><strong class="danger-text">{data['critical_styles']}</strong><small>货号</small></div>
          <div><span>库存支撑 8–14天</span><strong class="warning-text">{data['warning_styles']}</strong><small>货号</small></div>
          <div><span>核心尺码断码</span><strong>{data['broken_core']}</strong><small>尺码</small></div>
          <div><span>本期补货货号数</span><strong>{data['goods_count']}</strong><small>货号</small></div>
          <div><span>本期确认建议</span><strong>{number(data['suggested_total'])}</strong><small>件</small></div>
        </section>
        <div class="dashboard-grid">
          <section class="content-section span-2">
            <div class="section-heading"><div><h2>满足补货条件的货号</h2><p>条件1：7天 ≥ {settings['min_sales_7']} 且14天 ≥ {settings['min_sales_14']}；或条件2：连续销售 ≥ {settings['min_consecutive_sales_days']}天；共同要求支撑 ≤ {decimal(settings['max_coverage_days'], 0)}天</p></div><a href="/plans/{current['id'] if current else ''}">查看完整计划</a></div>
            <div class="table-wrap"><table><thead><tr><th>款号 / 款名</th><th>货号</th><th>颜色</th><th>7天</th><th>14天</th><th>连续</th><th>可售</th><th>支撑</th><th>命中</th><th>建议</th></tr></thead>
            <tbody>{risk_rows}</tbody></table></div>
          </section>
          <section class="content-section health-panel">
            <div class="section-heading"><div><h2>候选货号分布</h2><p>当前补货批次按货号统计</p></div></div>
            <div class="health-bar"><span class="bar-critical" style="width:{critical_width:.1f}%"></span><span class="bar-warning" style="width:{warning_width:.1f}%"></span><span class="bar-healthy" style="width:{healthy_width:.1f}%"></span></div>
            <dl class="health-legend"><div><dt><i class="dot critical"></i>7天内缺货</dt><dd>{data['critical_styles']} 款</dd></div><div><dt><i class="dot warning"></i>14天内缺货</dt><dd>{data['warning_styles']} 款</dd></div><div><dt><i class="dot healthy"></i>健康/低动销</dt><dd>{data['healthy_styles']} 款</dd></div></dl>
            <div class="sync-panel"><span>数据状态</span><strong>{esc(sync.get('inventory_snapshot_at') or '无库存快照')}</strong><p>{esc(sync.get('message') or '尚无同步记录')}</p>{data_link}</div>
          </section>
        </div>
        <section class="schedule-line"><div><span>自动生成频率</span><strong>{esc(schedule_text)} {esc(settings['schedule_time'])}</strong></div><div><span>目标覆盖</span><strong>{settings['target_days']} 天 + {settings['safety_days']} 天安全库存</strong></div><div><span>取数方式</span><strong>{esc(source_text)}</strong></div></section>
        """
        return self.base_page(user, "dashboard", content, "销售库存监控", store=store)

    def render_vip_api_dashboard(self, user: dict, store: dict, query: dict) -> str:
        config = db.get_api_config(self.db_path, store["id"])
        data = db.dashboard_data(self.db_path, store_id=store["id"])
        sync = db.latest_sync(self.db_path, store_id=store["id"]) or {}
        current = data["current"]
        status_labels = {
            "connected": "API 已鉴权",
            "failed": "鉴权失败",
            "credentials_missing": "凭证待配置",
            "not_tested": "待测试",
        }
        status_label = status_labels.get(config["last_test_status"], config["last_test_status"])
        api_action = (
            f'<a class="button primary" href="/data/api?store={quote(store["store_code"])}">API 配置与试连</a>'
            if user["role"] in {"merchandise", "admin"} else ""
        )
        if current:
            stage_title = esc(current["plan_no"])
            stage_note = (
                f"销售截至 {esc(current['sales_through_date'])} · "
                f"库存快照 {esc(current['inventory_snapshot_at'])}"
            )
            task_action = f'<a class="button primary" href="/plans/{current["id"]}">处理本次任务</a>'
        else:
            stage_title = "BNX 唯品会 API 单店联调"
            stage_note = esc(
                config["last_test_message"]
                or "已建立独立店铺工作区，等待 VOP 应用许可和店铺 AccessToken。"
            )
            task_action = api_action
        content = f"""
        {self.flash(query.get('message', ''))}
        <header class="page-heading"><div><span class="eyebrow">STORE WORKSPACE · VIPSHOP API</span><h1>销售库存监控</h1>
        <p>{esc(store['store_name'])}，应用凭证、销售库存、补货批次和通知均独立管理。</p></div></header>
        <section class="task-band"><div><span class="eyebrow">当前阶段</span><h2>{stage_title}</h2>
        <p>{stage_note}</p></div><div class="task-actions"><span class="status status-{esc(config['last_test_status'])}">{esc(status_label)}</span>{task_action}</div></section>
        <section class="metric-strip">
          <div><span>库存支撑 ≤ 7天</span><strong>{data['critical_styles'] if current else '-'}</strong><small>货号</small></div>
          <div><span>库存支撑 8–14天</span><strong>{data['warning_styles'] if current else '-'}</strong><small>货号</small></div>
          <div><span>核心尺码断码</span><strong>{data['broken_core'] if current else '-'}</strong><small>尺码</small></div>
          <div><span>本期补货货号数</span><strong>{data['goods_count']}</strong><small>货号</small></div>
          <div><span>本期确认建议</span><strong>{number(data['suggested_total'])}</strong><small>件</small></div>
        </section>
        <div class="dashboard-grid">
          <section class="content-section"><div class="section-heading"><div><h2>VOP API 取数范围</h2><p>许可到位后按单店只读口径逐项验证</p></div></div>
            <div class="table-wrap"><table><thead><tr><th>数据域</th><th>接口能力</th><th>补货中心用途</th><th>状态</th></tr></thead><tbody>
              <tr><td>店铺身份</td><td>getStoreInfo</td><td>校验授权店铺名称和店铺 ID</td><td>{esc(status_label)}</td></tr>
              <tr><td>商品 / SKU</td><td>getProducts / getProductById</td><td>款号、货号、颜色、尺码和商家编码</td><td>待许可</td></tr>
              <tr><td>近14天销售</td><td>getOrders / getOrderDetail</td><td>按 SKU 和自然日汇总有效销量</td><td>待许可</td></tr>
              <tr><td>在售库存</td><td>getSkuStock</td><td>读取可售与锁定库存并留存快照</td><td>待许可</td></tr>
            </tbody></table></div>
          </section>
          <section class="content-section"><div class="section-heading"><div><h2>连接状态</h2><p>BNX 与马天奴唯品会凭证完全隔离</p></div></div>
            <dl class="detail-list"><div><dt>官方网关</dt><dd>vop.vipapis.com</dd></div>
            <div><dt>应用凭证</dt><dd>{'已齐全' if config['credentials_complete'] else '待补齐'}</dd></div>
            <div><dt>绑定店铺</dt><dd>{esc(config['verified_store_name'] or '待验证')}</dd></div>
            <div><dt>最近测试</dt><dd>{esc(local_time(config['last_test_at']))}</dd></div>
            <div><dt>最近同步</dt><dd>{esc(local_time(config['last_sync_at']))}</dd></div></dl>
            <p class="subtle-note">AppKey 和 AppSecret 获批后仍需 BNX 店铺授权 AccessToken；店铺身份及三类数据权限全部通过前不生成补货建议。</p>
          </section>
        </div>
        <section class="schedule-line"><div><span>店铺单元</span><strong>{esc(store['store_code'])}</strong></div>
        <div><span>试跑规则</span><strong>首轮沿用当前筛选与覆盖口径</strong></div><div><span>自动频率</span><strong>单店闭环通过后启用</strong></div></section>
        """
        return self.base_page(user, "dashboard", content, store["store_name"], store=store)

    def render_tmall_dashboard(self, user: dict, store: dict, query: dict) -> str:
        config = db.get_tmall_api_config(self.db_path, store["id"])
        status_labels = {
            "connected": "API 已鉴权",
            "failed": "鉴权失败",
            "credentials_missing": "凭证待配置",
            "not_tested": "待测试",
        }
        status_label = status_labels.get(config["last_test_status"], config["last_test_status"])
        api_action = (
            '<a class="button primary" href="/data/tmall-api?store=TMALL-MTN-FLAGSHIP">API 配置与试连</a>'
            if user["role"] in {"merchandise", "admin"} else ""
        )
        content = f"""
        {self.flash(query.get('message', ''))}
        <header class="page-heading"><div><span class="eyebrow">STORE WORKSPACE · TMALL</span><h1>销售库存监控</h1>
        <p>{esc(store['store_name'])}，店铺数据、补货规则与流转批次独立管理。</p></div></header>
        <section class="task-band"><div><span class="eyebrow">当前阶段</span><h2>天猫 API 联调</h2>
        <p>{esc(config['last_test_message'] or '官方网关已确认，待店铺授权后验证店铺、商品、订单与库存权限。')}</p></div>
        <div class="task-actions"><span class="status status-{esc(config['last_test_status'])}">{esc(status_label)}</span>{api_action}</div></section>
        <section class="metric-strip">
          <div><span>库存支撑 ≤ 7天</span><strong>-</strong><small>货号</small></div>
          <div><span>库存支撑 8–14天</span><strong>-</strong><small>货号</small></div>
          <div><span>核心尺码断码</span><strong>-</strong><small>尺码</small></div>
          <div><span>本期补货货号数</span><strong>0</strong><small>货号</small></div>
          <div><span>本期确认建议</span><strong>0</strong><small>件</small></div>
        </section>
        <div class="dashboard-grid">
          <section class="content-section"><div class="section-heading"><div><h2>API 取数范围</h2><p>店铺鉴权通过后按只读口径逐项验证</p></div></div>
            <div class="table-wrap"><table><thead><tr><th>数据域</th><th>拟用 TOP 能力</th><th>补货中心口径</th><th>状态</th></tr></thead><tbody>
              <tr><td>店铺身份</td><td>taobao.shop.seller.get</td><td>校验授权店铺与马天奴天猫官旗一致</td><td>{esc(status_label)}</td></tr>
              <tr><td>商品 / SKU</td><td>items.onsale / item.skus</td><td>款号、货号、颜色、尺码和商家编码</td><td>待鉴权</td></tr>
              <tr><td>近14天销售</td><td>trades.sold / trade.fullinfo</td><td>按支付或成交口径汇总到 SKU 和自然日</td><td>待鉴权</td></tr>
              <tr><td>在售库存</td><td>item / sku quantity</td><td>按商家 SKU 读取可售数，与销售同次留存快照</td><td>待鉴权</td></tr>
            </tbody></table></div>
          </section>
          <section class="content-section"><div class="section-heading"><div><h2>连接状态</h2><p>天猫店与唯品店凭证完全隔离</p></div></div>
            <dl class="detail-list"><div><dt>官方网关</dt><dd>eco.taobao.com</dd></div>
            <div><dt>店铺鉴权</dt><dd>{esc(status_label)}</dd></div>
            <div><dt>绑定店铺</dt><dd>{esc(config['verified_store_name'] or '待验证')}</dd></div>
            <div><dt>最近测试</dt><dd>{esc(local_time(config['last_test_at']))}</dd></div></dl>
            <p class="subtle-note">在店铺鉴权、SKU 映射和库存口径全部通过前，不会生成天猫补货建议。</p>
          </section>
        </div>
        <section class="schedule-line"><div><span>店铺单元</span><strong>{esc(store['store_code'])}</strong></div>
        <div><span>补货规则</span><strong>将独立配置</strong></div><div><span>自动频率</span><strong>鉴权后启用</strong></div></section>
        """
        return self.base_page(
            user, "dashboard", content, "马天奴天猫官方旗舰店", store=store
        )

    def render_risk_row(self, group: dict, plan_id: int) -> str:
        conditions = self.render_condition_tags(group["selection_reasons"])
        return f"""<tr><td><a class="item-link" href="/plans/{plan_id}#{esc(group['style_code'])}"><strong>{esc(group['style_code'])}</strong><span>{esc(group['style_name'])}</span></a></td>
        <td><strong>{esc(group['outer_sku_id'])}</strong></td><td>{esc(group['color_name'])}</td><td>{group['sales_7']}</td><td>{group['sales_14']}</td><td>{group['consecutive_sales_days']}天</td><td>{group['sellable']}</td>
        <td>{decimal(group['coverage_days'])}天</td><td>{conditions}</td><td><strong>{group['confirmed_qty']}</strong></td></tr>"""

    def render_condition_tags(self, reasons: list[str]) -> str:
        labels = {"condition_1": "条件1", "condition_2": "条件2"}
        return '<span class="condition-tags">' + "".join(
            f'<span class="condition-tag {esc(reason)}">{esc(labels.get(reason, reason))}</span>' for reason in reasons
        ) + "</span>"

    def render_plans(self, user: dict, query: dict) -> str:
        store = self.selected_store(query)
        plans = db.list_plans(self.db_path, store_id=store["id"])
        rows = "".join(
            f"""<tr><td><a class="item-link" href="/plans/{plan['id']}"><strong>{esc(plan['plan_no'])}</strong><span>{esc(plan['sales_through_date'])}</span></a></td>
            <td>{esc(plan['store_name'])}</td><td>{plan['target_days']} 天</td><td>{plan['style_count']}</td><td>{plan['risk_sku_count']}</td><td>{number(plan['confirmed_total'])}</td>
            <td><span class="status status-{esc(plan['status'])}">{esc(PLAN_STATUS.get(plan['status'], plan['status']))}</span></td><td><a href="/plans/{plan['id']}">查看</a></td></tr>"""
            for plan in plans
        ) or '<tr><td colspan="8" class="empty-cell">暂无补货计划</td></tr>'
        content = f"""<header class="page-heading"><div><span class="eyebrow">REPLENISHMENT RUNS</span><h1>补货计划</h1><p>{esc(store['store_name'])}的每次计算均保留独立数据快照、人工修正和流转结果。</p></div></header>
        <section class="content-section"><div class="table-wrap"><table><thead><tr><th>计划编号</th><th>店铺</th><th>目标覆盖</th><th>货号</th><th>补货尺码</th><th>确认数量</th><th>流程状态</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></section>"""
        return self.base_page(user, "plans", content, "补货计划", store=store)

    def render_plan_detail(self, user: dict, plan_id: int, query: dict) -> str:
        plan = db.get_plan(self.db_path, plan_id)
        if not plan:
            raise ValueError("计划不存在。")
        groups = db.grouped_plan_styles(db.get_plan_items(self.db_path, plan_id))
        editable = user["role"] in {"merchandise", "admin"} and plan["status"] in {"merchandise_pending", "merchandise_editing"}
        sections = "".join(self.render_merchandise_style(group, editable) for group in groups)
        actions = f'<a class="button ghost" href="/plans/{plan_id}/export.xlsx">导出 Excel</a>'
        if user["role"] in {"followup", "admin"} and plan["status"] in {"followup_pending", "followup_processing"}:
            actions += f'<a class="button primary" href="/plans/{plan_id}/followup">进入跟单确认</a>'
        form_open = f'<form method="post" action="/plans/{plan_id}/save">' if editable else ""
        form_close = "</form>" if editable else ""
        bottom_actions = ""
        if editable:
            bottom_actions = """<div class="sticky-actions"><div><strong>商品部确认</strong><span>修改系统建议时必须选择原因</span></div><div><button class="button ghost" type="submit" name="command" value="save">保存修正</button><button class="button primary" type="submit" name="command" value="submit">确认并提交跟单部</button></div></div>"""
        content = f"""
        {self.flash(query.get('message', ''))}
        <header class="page-heading plan-heading"><div><a class="back-link" href="/plans">返回补货计划</a><span class="eyebrow">{esc(plan['platform_name'])} · {esc(plan['store_name'])}</span><h1>{esc(plan['plan_no'])}</h1>
        <p>销售截至 {esc(plan['sales_through_date'])} · 库存快照 {esc(plan['inventory_snapshot_at'])} · 覆盖 {plan['target_days']} + 安全 {plan['safety_days']} 天</p></div><div class="heading-actions">{actions}</div></header>
        <section class="formula-line"><span>条件1：7天 ≥ {plan['min_sales_7']} 且14天 ≥ {plan['min_sales_14']}</span><span>或 条件2：连续销售 ≥ {plan['min_consecutive_sales_days']}天</span><span>共同要求：支撑 ≤ {decimal(plan['max_coverage_days'], 0)}天</span><span class="status status-{esc(plan['status'])}">{esc(PLAN_STATUS.get(plan['status'], plan['status']))}</span></section>
        {form_open}<div class="plan-groups">{sections}</div>{bottom_actions}{form_close}
        """
        return self.base_page(user, "plans", content, plan["plan_no"])

    def render_merchandise_style(self, style: dict, editable: bool) -> str:
        goods = "".join(self.render_merchandise_group(group, editable) for group in style["goods"])
        return f"""<section class="style-group" id="{esc(style['style_code'])}">
          <div class="style-heading"><div><span class="risk {esc(style['risk_level'])}">{esc(RISK_LABELS[style['risk_level']])}</span><h2>{esc(style['style_code'])} · {esc(style['style_name'])}</h2></div>
          <div class="style-totals"><span>补货货号 <strong>{style['goods_count']}</strong></span><span>确认补货 <strong>{style['confirmed_qty']}</strong></span></div></div>
          <div class="goods-groups">{goods}</div>
        </section>"""

    def render_merchandise_group(self, group: dict, editable: bool) -> str:
        rows = "".join(self.render_merchandise_item(item, editable) for item in group["items"])
        conditions = self.render_condition_tags(group["selection_reasons"])
        return f"""<section class="goods-group">
          <div class="goods-heading"><div><h3>货号 {esc(group['outer_sku_id'])}</h3><p>{esc(group['color_name'])} · {esc(group['category'])}</p>{conditions}</div>
          <div class="goods-totals"><span>7天 <strong>{group['sales_7']}</strong></span><span>14天 <strong>{group['sales_14']}</strong></span><span>连续 <strong>{group['consecutive_sales_days']}天</strong></span><span>支撑 <strong>{decimal(group['coverage_days'])}天</strong></span><span>补货 <strong>{group['confirmed_qty']}</strong></span></div></div>
          <div class="table-wrap"><table class="dense-table"><thead><tr><th>尺码</th><th>7天</th><th>14天</th><th>尺码配比</th><th>可售</th><th>在途</th><th>可售天数</th><th>14天结余</th><th>系统建议</th><th>商品部确认</th><th>调整原因</th></tr></thead><tbody>{rows}</tbody></table></div>
        </section>"""

    def render_merchandise_item(self, item: dict, editable: bool) -> str:
        size = f"{esc(item['size_name'])}{'<small>核心</small>' if item['core_size'] else ''}"
        confirmed = (
            f'<input class="qty-input" type="number" min="0" step="{item["pack_size"]}" name="qty_{item["id"]}" value="{item["confirmed_qty"]}">'
            if editable else f"<strong>{item['confirmed_qty']}</strong>"
        )
        reason_options = ["", "活动备货", "销量趋势", "款式生命周期", "供应限制", "预计退货", "准备下架", "仓间调拨", "其他"]
        if editable:
            options = "".join(f'<option value="{esc(option)}"{(" selected" if option == item["adjustment_reason"] else "")}>{esc(option or "未调整")}</option>' for option in reason_options)
            reason = f'<select name="reason_{item["id"]}">{options}</select>'
        else:
            reason = esc(item["adjustment_reason"] or "-")
        return f"""<tr class="risk-row-{esc(item['risk_level'])}"><td><strong class="size-label">{size}</strong></td><td>{item['sales_7']}</td><td>{item['sales_14']}</td><td>{item['size_share']*100:.0f}%</td>
        <td>{item['sellable']}</td><td>{item['inbound'] or '-'}</td><td>{decimal(item['coverage_days'])}</td><td class="{'danger-text' if item['projected_14'] < 0 else ''}">{decimal(item['projected_14'])}</td>
        <td><strong>{item['suggested_qty']}</strong><small class="cell-note">{item['pack_size']}件/组</small></td><td>{confirmed}</td><td>{reason}</td></tr>"""

    def render_followup_inbox(self, user: dict) -> str:
        plans = [plan for plan in db.list_plans(self.db_path) if plan["status"] in {"followup_pending", "followup_processing", "completed"}]
        rows = "".join(
            f"""<tr><td><a class="item-link" href="/plans/{plan['id']}/followup"><strong>{esc(plan['plan_no'])}</strong><span>{esc(plan['sales_through_date'])}</span></a></td><td>{plan['style_count']}</td><td>{number(plan['confirmed_total'])}</td>
            <td><span class="status status-{esc(plan['status'])}">{esc(PLAN_STATUS.get(plan['status'], plan['status']))}</span></td><td><a href="/plans/{plan['id']}/followup">{'查看结果' if plan['status']=='completed' else '处理'}</a></td></tr>"""
            for plan in plans
        ) or '<tr><td colspan="5" class="empty-cell">暂无流转给跟单部的任务</td></tr>'
        content = f"""<header class="page-heading"><div><span class="eyebrow">FOLLOW-UP INBOX</span><h1>跟单任务</h1><p>接收商品部确认数量，回复供应能力、下单日期和预计到货。</p></div></header>
        <section class="content-section"><div class="table-wrap"><table><thead><tr><th>计划编号</th><th>货号</th><th>商品部数量</th><th>状态</th><th></th></tr></thead><tbody>{rows}</tbody></table></div></section>"""
        return self.base_page(user, "followup", content, "跟单任务")

    def render_followup_plan(self, user: dict, plan_id: int, query: dict) -> str:
        plan = db.get_plan(self.db_path, plan_id)
        if not plan:
            raise ValueError("计划不存在。")
        if plan["status"] not in {"followup_pending", "followup_processing", "completed"}:
            raise ValueError("该计划尚未流转至跟单部。")
        editable = plan["status"] != "completed"
        groups = db.grouped_plan_styles(db.get_plan_items(self.db_path, plan_id))
        sections = "".join(self.render_followup_style(group, editable) for group in groups)
        controls = ""
        if editable:
            controls = '<div class="sticky-actions"><div><strong>跟单部回填</strong><span>确认供应数量、下单及到货时间</span></div><div><button class="button ghost" name="command" value="save">保存进度</button><button class="button primary" name="command" value="complete">完成跟单确认</button></div></div>'
        content = f"""{self.flash(query.get('message',''))}<header class="page-heading"><div><a class="back-link" href="/followup">返回跟单任务</a><span class="eyebrow">FOLLOW-UP CONFIRMATION</span><h1>{esc(plan['plan_no'])}</h1><p>商品部已确认补货数量，请回复供应能力和交期。</p></div><div class="heading-actions"><a class="button ghost" href="/plans/{plan_id}/export.xlsx">导出 Excel</a></div></header>
        <form method="post" action="/plans/{plan_id}/followup"><div class="plan-groups">{sections}</div>{controls}</form>"""
        return self.base_page(user, "followup", content, plan["plan_no"])

    def render_followup_style(self, style: dict, editable: bool) -> str:
        goods = "".join(self.render_followup_group(group, editable) for group in style["goods"])
        return f"""<section class="style-group"><div class="style-heading"><div><h2>{esc(style['style_code'])} · {esc(style['style_name'])}</h2></div><div class="style-totals"><span>货号 <strong>{style['goods_count']}</strong></span><span>商品部确认 <strong>{style['confirmed_qty']}</strong></span><span>跟单确认 <strong>{style['followup_qty']}</strong></span></div></div><div class="goods-groups">{goods}</div></section>"""

    def render_followup_group(self, group: dict, editable: bool) -> str:
        rows = "".join(self.render_followup_item(item, editable) for item in group["items"])
        return f"""<section class="goods-group"><div class="goods-heading"><div><h3>货号 {esc(group['outer_sku_id'])}</h3><p>{esc(group['color_name'])}</p></div><div class="goods-totals"><span>商品部确认 <strong>{group['confirmed_qty']}</strong></span><span>跟单确认 <strong>{group['followup_qty']}</strong></span></div></div>
        <div class="table-wrap"><table class="dense-table"><thead><tr><th>尺码</th><th>系统建议</th><th>商品部确认</th><th>跟单确认</th><th>预计下单</th><th>预计到货</th><th>供应状态</th><th>备注</th></tr></thead><tbody>{rows}</tbody></table></div></section>"""

    def render_followup_item(self, item: dict, editable: bool) -> str:
        followup_qty = item["followup_qty"] if item["followup_qty"] is not None else item["confirmed_qty"]
        if not editable:
            return f"""<tr><td><strong>{esc(item['size_name'])}</strong></td><td>{item['suggested_qty']}</td><td>{item['confirmed_qty']}</td><td><strong>{followup_qty}</strong></td><td>{esc(item['expected_order_date'] or '-')}</td><td>{esc(item['expected_arrival_date'] or '-')}</td><td>{esc(FOLLOWUP_LABELS.get(item['followup_status'], item['followup_status']))}</td><td>{esc(item['followup_note'] or '-')}</td></tr>"""
        options = "".join(f'<option value="{key}"{(" selected" if key == item["followup_status"] else "")}>{label}</option>' for key, label in FOLLOWUP_LABELS.items())
        return f"""<tr><td><strong>{esc(item['size_name'])}</strong></td><td>{item['suggested_qty']}</td><td>{item['confirmed_qty']}</td>
        <td><input class="qty-input" type="number" min="0" name="followup_qty_{item['id']}" value="{followup_qty}"></td>
        <td><input type="date" name="order_date_{item['id']}" value="{esc(item['expected_order_date'])}"></td><td><input type="date" name="arrival_date_{item['id']}" value="{esc(item['expected_arrival_date'])}"></td>
        <td><select name="followup_status_{item['id']}">{options}</select></td><td><input name="followup_note_{item['id']}" value="{esc(item['followup_note'])}" placeholder="供应限制或单号"></td></tr>"""

    def render_data_center(self, user: dict, query: dict) -> str:
        sync = db.latest_sync(self.db_path) or {}
        settings = db.get_settings(self.db_path)
        api_config = db.get_api_config(self.db_path)
        if api_config["last_test_status"] == "connected":
            mode_title = f"唯品会 API 已鉴权 · {api_config['verified_store_name']}"
            mode_note = "可执行真实商品、订单和库存同步；同步成功后会生成独立补货批次。"
            mode_badge = "API 已连接"
        elif settings["data_source_mode"] == "browser":
            mode_title = "唯品浏览器官方报表"
            mode_note = "定时任务会检查唯品登录状态；新报表接收并校验完成后才生成补货批次。"
            mode_badge = "浏览器取数"
        elif api_config["credentials_complete"]:
            mode_title = "唯品会 API 凭证已保存"
            mode_note = "尚未通过店铺鉴权校验，请先执行测试连接。"
            mode_badge = "待测试"
        else:
            mode_title = "单店试跑数据"
            mode_note = "官方网关可接入，当前缺少 AppKey、AppSecret 或 AccessToken。"
            mode_badge = "凭证待配置"
        content = f"""{self.flash(query.get('message',''))}<header class="page-heading"><div><span class="eyebrow">DATA CONTROL</span><h1>数据中心</h1><p>唯品会马天奴单店的数据校验、应急导入和计划生成入口。</p></div><div class="heading-actions"><a class="button ghost" href="/data/browser">浏览器报表采集</a></div></header>
        <section class="environment-banner"><div><span>当前取数模式</span><strong>{esc(mode_title)}</strong><p>{esc(mode_note)}</p></div><div class="heading-actions"><span class="status status-test">{esc(mode_badge)}</span><a class="button primary" href="/data/api">API 配置与试连</a></div></section>
        <div class="two-column">
          <section class="content-section"><div class="section-heading"><div><h2>数据快照</h2><p>最近一次同步或导入结果</p></div></div>
            <dl class="detail-list"><div><dt>销售数据截至</dt><dd>{esc(sync.get('sales_through_date') or '-')}</dd></div><div><dt>库存快照</dt><dd>{esc(sync.get('inventory_snapshot_at') or '-')}</dd></div><div><dt>数据行数</dt><dd>{number(sync.get('row_count'))}</dd></div><div><dt>来源</dt><dd>{esc(sync.get('source') or '-')}</dd></div></dl>
            <p class="subtle-note">{esc(sync.get('message') or '尚无同步记录')}</p>
            <form method="post" action="/data/test-sync"><button class="button ghost" type="submit">校验试跑数据</button></form>
          </section>
          <section class="content-section"><div class="section-heading"><div><h2>Excel 应急导入</h2><p>接口不可用时仍可完成补货批次</p></div><a href="/data/template.xlsx">下载模板</a></div>
            <form method="post" action="/data/import" enctype="multipart/form-data" class="upload-form"><label class="file-field"><span>选择数据文件</span><input type="file" name="data_file" accept=".xlsx" required></label><button class="button primary" type="submit">校验并导入</button></form>
            <p class="subtle-note">工作簿需包含“SKU资料”“销售数据”“库存及在途”三个工作表。</p>
          </section>
        </div>
        <section class="content-section generate-section"><div><h2>生成补货任务</h2><p>按照当前参数生成一个新的冻结批次。自动任务默认在周二、周五 {esc(settings['schedule_time'])} 执行。</p></div><form method="post" action="/plans/generate"><button class="button primary" type="submit">立即生成补货计划</button></form></section>"""
        return self.base_page(user, "data", content, "数据中心")

    def render_browser_capture(self, user: dict, query: dict) -> str:
        config = db.get_browser_capture_config(self.db_path)
        jobs = db.list_browser_capture_jobs(self.db_path)
        status_labels = {
            "browser_closed": "浏览器未启动",
            "login_required": "需要登录",
            "session_present": "会话可用",
            "unknown": "状态未知",
        }
        job_labels = {
            "waiting_download": "等待导出",
            "ready_for_import": "字段校验通过",
            "needs_mapping": "待字段映射",
            "needs_conversion": "待转换文件",
            "imported": "已导入计划",
        }
        kind_labels = {"sales": "销售报表", "inventory": "库存报表", "master": "商品主数据"}
        rows = ""
        for job in jobs:
            analysis = job.get("analysis") or {}
            missing = "-" if job["status"] == "imported" else ("、".join(analysis.get("missing") or []) or "-")
            rows += f"""<tr><td>{job['id']}</td><td>{esc(kind_labels.get(job['kind'], job['kind']))}</td><td><span class="status status-{esc(job['status'])}">{esc(job_labels.get(job['status'], job['status']))}</span></td><td>{esc(job['file_name'] or '-')}</td><td>{number(analysis.get('row_count')) if analysis else '-'}</td><td>{esc(missing)}</td><td>{esc(job['created_at'])}</td></tr>"""
        if not rows:
            rows = '<tr><td colspan="7" class="empty-cell">尚未创建浏览器报表采集任务</td></tr>'
        sales_ready = bool(config["sales_report_url"])
        inventory_ready = bool(config["inventory_report_url"])
        master_ready = bool(config["master_report_url"])
        latest_files = {}
        for job in jobs:
            if job["kind"] in {"sales", "inventory", "master"} and job["archive_file"] and job["kind"] not in latest_files:
                latest_files[job["kind"]] = job
        import_ready = all(kind in latest_files for kind in ("sales", "inventory", "master"))
        already_imported = import_ready and all(latest_files[kind]["status"] == "imported" for kind in ("sales", "inventory", "master"))
        content = f"""{self.flash(query.get('message',''))}
        <header class="page-heading"><div><a class="back-link" href="/data">返回数据中心</a><span class="eyebrow">ASSISTED REPORT CAPTURE</span><h1>唯品浏览器报表采集</h1><p>使用独立 Chrome 会话接收官方销售和库存报表，不保存唯品账号密码。</p></div></header>
        <section class="environment-banner"><div><span>专用浏览器状态</span><strong>{esc(status_labels.get(config['session_status'], config['session_status']))}</strong><p>{esc(config['last_check_message'] or '尚未启动唯品专用 Chrome。')}</p></div><div class="heading-actions"><form method="post" action="/data/browser/open"><button class="button primary" type="submit">打开唯品后台登录</button></form><form method="post" action="/data/browser/check"><button class="button ghost" type="submit">检查登录状态</button></form></div></section>
        <section class="schedule-line capture-step-strip"><div><span>后台入口</span><strong>VIS / 魔方罗盘</strong></div><div><span>账号密码</span><strong>仅在唯品页面输入</strong></div><div><span>下载目录</span><strong class="url-value">{esc(config['download_dir'])}</strong></div></section>
        <div class="capture-source-grid api-actions-grid">
          <section class="content-section"><div class="section-heading"><div><h2>销售报表页</h2><p>{'已记录页面地址' if sales_ready else '登录后进入销售明细或订单报表页'}</p></div><span class="status {'status-completed' if sales_ready else 'status-test'}">{'已就绪' if sales_ready else '待记录'}</span></div><p class="url-value">{esc(config['sales_report_url'] or '尚未记录')}</p><div class="heading-actions"><form method="post" action="/data/browser/save-page"><input type="hidden" name="kind" value="sales"><button class="button ghost" type="submit">记录当前页为销售报表</button></form><form method="post" action="/data/browser/capture"><input type="hidden" name="kind" value="sales"><button class="button primary" type="submit"{('' if sales_ready else ' disabled')}>开始销售报表采集</button></form></div></section>
          <section class="content-section"><div class="section-heading"><div><h2>库存报表页</h2><p>{'已记录页面地址' if inventory_ready else '登录后进入库存明细报表页'}</p></div><span class="status {'status-completed' if inventory_ready else 'status-test'}">{'已就绪' if inventory_ready else '待记录'}</span></div><p class="url-value">{esc(config['inventory_report_url'] or '尚未记录')}</p><div class="heading-actions"><form method="post" action="/data/browser/save-page"><input type="hidden" name="kind" value="inventory"><button class="button ghost" type="submit">记录当前页为库存报表</button></form><form method="post" action="/data/browser/capture"><input type="hidden" name="kind" value="inventory"><button class="button primary" type="submit"{('' if inventory_ready else ' disabled')}>开始库存报表采集</button></form></div></section>
          <section class="content-section"><div class="section-heading"><div><h2>商品主数据</h2><p>{'已记录商品资料页' if master_ready else 'VIS商品资料用于补齐颜色和尺码'}</p></div><span class="status {'status-completed' if master_ready else 'status-test'}">{'已就绪' if master_ready else '待记录'}</span></div><p class="url-value">{esc(config['master_report_url'] or '尚未记录')}</p><div class="heading-actions"><form method="post" action="/data/browser/save-page"><input type="hidden" name="kind" value="master"><button class="button ghost" type="submit">记录当前页为商品主数据</button></form><form method="post" action="/data/browser/capture"><input type="hidden" name="kind" value="master"><button class="button primary" type="submit"{('' if master_ready else ' disabled')}>更新商品主数据</button></form></div></section>
        </div>
        <section class="content-section capture-history"><div class="section-heading"><div><h2>采集记录</h2><p>下载完成后约 30 秒自动接收，也可立即扫描</p></div><form method="post" action="/data/browser/scan"><button class="button ghost" type="submit">扫描新下载文件</button></form></div><div class="table-wrap"><table><thead><tr><th>任务</th><th>类型</th><th>状态</th><th>文件</th><th>行数</th><th>待确认字段</th><th>开始时间</th></tr></thead><tbody>{rows}</tbody></table></div></section>
        <section class="generate-section"><div><h2>生成真实数据补货计划</h2><p>{'最近报表已导入，可在补货计划中查看' if already_imported else '关联商品主数据补齐颜色，仅在14天和条码校验通过后写入数据'}</p></div><form method="post" action="/data/browser/import"><button class="button primary" type="submit"{('' if import_ready and not already_imported else ' disabled')}>关联报表并生成计划</button></form></section>
        <section class="formula-line api-scope-note"><span>专用 Chrome 配置与个人浏览器隔离</span><span>原始文件按日期留档并计算 SHA-256</span><span>字段校验通过前不会写入补货数据</span></section>
        """
        return self.base_page(user, "data", content, "唯品浏览器采集")

    def render_api_config(self, user: dict, query: dict) -> str:
        store = self.selected_store(query)
        if store["platform_code"] != "vip":
            raise ValueError("当前店铺不是唯品会店铺。")
        config = db.get_api_config(self.db_path, store["id"])
        store_query = f"store={quote(store['store_code'])}"
        is_default_store = store["store_code"] == "VIP-MTN"
        back_href = "/data" if is_default_store else f"/dashboard?{store_query}"
        back_label = "返回数据中心" if is_default_store else f"返回{store['store_name']}监控台"
        environment_label = "正式环境" if config["environment"] == "production" else "沙箱环境"
        status_labels = {
            "not_tested": "尚未测试",
            "credentials_missing": "缺少凭证",
            "configured": "待测试",
            "connected": "鉴权成功",
            "failed": "连接失败",
        }
        secret_placeholder = "已加密保存，留空则不修改" if config["has_app_secret"] else "请输入 VOP AppSecret"
        token_placeholder = "已加密保存，留空则不修改" if config["has_access_token"] else "请输入店铺主账号 OAuth AccessToken"
        content = f"""{self.flash(query.get('message',''))}
        <header class="page-heading"><div><a class="back-link" href="{back_href}">{esc(back_label)}</a><span class="eyebrow">VIPSHOP VOP CONNECTION</span><h1>{esc(store['store_name'])} API 配置</h1><p>只读对接{esc(store['store_name'])}的商品、订单和库存接口，凭证与其他店铺隔离。</p></div><div class="heading-actions"><a class="button ghost" href="https://vop.vip.com/home#/console/app/overview" target="_blank" rel="noreferrer">打开 VOP 控制台</a></div></header>
        <section class="environment-banner"><div><span>最近一次试连</span><strong>{esc(status_labels.get(config['last_test_status'], config['last_test_status']))}</strong><p>{esc(config['last_test_message'] or '已确认官方网关可访问，等待商家应用凭证。')}</p></div><span class="status status-{esc(config['last_test_status'])}">{esc(environment_label)}</span></section>
        <form method="post" action="/data/api/config"><input type="hidden" name="store_code" value="{esc(store['store_code'])}">
          <section class="settings-section"><div class="settings-intro"><h2>连接环境</h2><p>正式店铺数据使用生产网关；唯品会提供沙箱 AppKey 时才选择沙箱。</p></div><div class="settings-fields field-row"><label>接口环境<select name="environment"><option value="production"{(' selected' if config['environment']=='production' else '')}>正式环境 · vop.vipapis.com</option><option value="sandbox"{(' selected' if config['environment']=='sandbox' else '')}>沙箱环境 · sandbox.vipapis.com</option></select></label><label>预期店铺名称<input name="expected_store_name" value="{esc(config['expected_store_name'])}" required></label></div></section>
          <section class="settings-section"><div class="settings-intro"><h2>应用凭证</h2><p>AppSecret 和 AccessToken 使用本机独立密钥加密后存储，不会显示在页面或日志中。</p></div><div class="settings-fields"><label>AppKey<input name="app_key" value="{esc(config['app_key'])}" autocomplete="off" placeholder="VOP 应用 ID"></label><div class="field-row settings-fields"><label>AppSecret<input type="password" name="app_secret" autocomplete="new-password" placeholder="{esc(secret_placeholder)}"></label><label>AccessToken<input type="password" name="access_token" autocomplete="new-password" placeholder="{esc(token_placeholder)}"></label></div><div class="credential-flags"><span>AppSecret：{'已保存' if config['has_app_secret'] else '未配置'}</span><span>AccessToken：{'已保存' if config['has_access_token'] else '未配置'}</span><span>来源：{'环境变量' if config['credential_source']=='environment' else '加密数据库'}</span></div><div class="weekday-picker"><label class="check-pill"><input type="checkbox" name="clear_app_secret" value="1"><span>清除 AppSecret</span></label><label class="check-pill"><input type="checkbox" name="clear_access_token" value="1"><span>清除 AccessToken</span></label></div></div></section>
          <div class="form-footer"><a class="button ghost" href="{back_href}">取消</a><button class="button primary" type="submit">加密保存配置</button></div>
        </form>
        <div class="two-column api-actions-grid">
          <section class="content-section"><div class="section-heading"><div><h2>1. 测试连接</h2><p>只调用店铺信息接口，不读取订单或库存</p></div></div><dl class="detail-list"><div><dt>官方生产网关</dt><dd>https://vop.vipapis.com</dd></div><div><dt>绑定店铺</dt><dd>{esc(config['verified_store_name'] or '待验证')}</dd></div><div><dt>店铺 ID</dt><dd>{esc(config['external_store_id'] or '-')}</dd></div><div><dt>最近测试</dt><dd>{esc(local_time(config['last_test_at']))}</dd></div></dl><form method="post" action="/data/api/test?{store_query}"><button class="button ghost" type="submit">测试网关与店铺鉴权</button></form></section>
          <section class="content-section"><div class="section-heading"><div><h2>2. 同步单店数据</h2><p>鉴权通过后读取近 14 天订单、商品 SKU 和库存</p></div></div><dl class="detail-list"><div><dt>订单接口</dt><dd>getOrders / getOrderDetail</dd></div><div><dt>库存接口</dt><dd>getSkuStock</dd></div><div><dt>最近同步</dt><dd>{esc(local_time(config['last_sync_at']))}</dd></div></dl><form method="post" action="/data/api/sync?{store_query}"><button class="button primary" type="submit"{('' if config['last_test_status']=='connected' else ' disabled')}>同步并生成补货批次</button></form></section>
        </div>
        <section class="formula-line api-scope-note"><span>当前同步不保存收件人、地址、电话等订单隐私字段</span><span>取消单和拒收单不计入销量</span><span>首期退货量仍需后续接入售后单接口复核</span></section>
        """
        return self.base_page(user, "data", content, f"{store['store_name']} API 配置", store=store)

    def render_tmall_api_config(self, user: dict, query: dict) -> str:
        store = db.get_store(self.db_path, "TMALL-MTN-FLAGSHIP")
        config = db.get_tmall_api_config(self.db_path, store["id"])
        environment_label = "正式环境" if config["environment"] == "production" else "沙箱环境"
        status_labels = {
            "not_tested": "尚未测试",
            "credentials_missing": "凭证待配置",
            "connected": "店铺鉴权成功",
            "failed": "店铺鉴权失败",
        }
        secret_placeholder = "已加密保存，留空则不修改" if config["has_app_secret"] else "请输入应用 AppSecret"
        session_placeholder = "已加密保存，留空则不修改" if config["has_session_key"] else "请输入店铺授权 SessionKey"
        content = f"""{self.flash(query.get('message',''))}
        <header class="page-heading"><div><a class="back-link" href="/dashboard?store=TMALL-MTN-FLAGSHIP">返回天猫监控台</a><span class="eyebrow">TAOBAO OPEN PLATFORM CONNECTION</span><h1>天猫 API 配置与试连</h1><p>先验证{esc(store['store_name'])}的应用凭证和店铺授权，暂不读取或生成补货数据。</p></div><div class="heading-actions"><a class="button ghost" href="https://open.taobao.com/" target="_blank" rel="noreferrer">打开淘宝开放平台</a></div></header>
        <section class="environment-banner"><div><span>最近一次试连</span><strong>{esc(status_labels.get(config['last_test_status'], config['last_test_status']))}</strong><p>{esc(config['last_test_message'] or '尚未执行网关与店铺鉴权测试。')}</p></div><span class="status status-{esc(config['last_test_status'])}">{esc(environment_label)}</span></section>
        <form method="post" action="/data/tmall-api/config">
          <section class="settings-section"><div class="settings-intro"><h2>连接环境</h2><p>正式店铺使用淘宝开放平台生产网关；仅在已获得沙箱应用凭证时选择沙箱。</p></div><div class="settings-fields field-row"><label>接口环境<select name="environment"><option value="production"{(' selected' if config['environment']=='production' else '')}>正式环境 · eco.taobao.com</option><option value="sandbox"{(' selected' if config['environment']=='sandbox' else '')}>沙箱环境 · gw.api.tbsandbox.com</option></select></label><label>预期店铺名称<input name="expected_store_name" value="{esc(config['expected_store_name'])}" required></label></div></section>
          <section class="settings-section"><div class="settings-intro"><h2>应用与店铺授权</h2><p>AppSecret 和 SessionKey 只使用本机密钥加密保存，页面、运行日志和接口测试结果均不显示明文。</p></div><div class="settings-fields"><label>AppKey<input name="app_key" value="{esc(config['app_key'])}" autocomplete="off" placeholder="淘宝开放平台应用 AppKey"></label><div class="field-row settings-fields"><label>AppSecret<input type="password" name="app_secret" autocomplete="new-password" placeholder="{esc(secret_placeholder)}"></label><label>SessionKey<input type="password" name="session_key" autocomplete="new-password" placeholder="{esc(session_placeholder)}"></label></div><div class="credential-flags"><span>AppKey：{esc(config['app_key_masked'] or '未配置')}</span><span>AppSecret：{'已保存' if config['has_app_secret'] else '未配置'}</span><span>SessionKey：{'已保存' if config['has_session_key'] else '未配置'}</span><span>来源：{'环境变量' if config['credential_source']=='environment' else '加密数据库'}</span></div><div class="weekday-picker"><label class="check-pill"><input type="checkbox" name="clear_app_secret" value="1"><span>清除 AppSecret</span></label><label class="check-pill"><input type="checkbox" name="clear_session_key" value="1"><span>清除 SessionKey</span></label></div></div></section>
          <div class="form-footer"><a class="button ghost" href="/dashboard?store=TMALL-MTN-FLAGSHIP">取消</a><button class="button primary" type="submit">加密保存配置</button></div>
        </form>
        <div class="two-column api-actions-grid">
          <section class="content-section"><div class="section-heading"><div><h2>1. 测试网关与店铺</h2><p>先探测官方网关；凭证齐全时只调用店铺信息接口</p></div></div><dl class="detail-list"><div><dt>生产网关</dt><dd>https://eco.taobao.com/router/rest</dd></div><div><dt>店铺接口</dt><dd>taobao.shop.seller.get</dd></div><div><dt>绑定店铺</dt><dd>{esc(config['verified_store_name'] or '待验证')}</dd></div><div><dt>卖家昵称</dt><dd>{esc(config['seller_nick'] or '-')}</dd></div><div><dt>店铺 ID</dt><dd>{esc(config['external_shop_id'] or '-')}</dd></div></dl><form method="post" action="/data/tmall-api/test"><button class="button ghost" type="submit">测试网关与店铺鉴权</button></form></section>
          <section class="content-section"><div class="section-heading"><div><h2>2. 验证只读取数范围</h2><p>店铺鉴权后再逐项申请和验证接口权限</p></div></div><dl class="detail-list"><div><dt>商品与 SKU</dt><dd>款号、货号、颜色、尺码</dd></div><div><dt>近 14 天销售</dt><dd>订单明细汇总至 SKU / 自然日</dd></div><div><dt>在售库存</dt><dd>可售数量与同批次快照</dd></div><div><dt>最近同步</dt><dd>{esc(local_time(config['last_sync_at']))}</dd></div></dl><button class="button primary" type="button" disabled>同步并生成补货批次</button></section>
        </div>
        <section class="formula-line api-scope-note"><span>OAuth 授权入口：oauth.taobao.com/authorize</span><span>不保存买家姓名、电话或地址</span><span>四类数据口径全部通过前不生成补货建议</span></section>
        """
        return self.base_page(
            user, "data", content, "天猫 API 配置与试连", store=store
        )

    def render_settings(self, user: dict, query: dict) -> str:
        settings = db.get_settings(self.db_path)
        weekday_controls = "".join(
            f'<label class="check-pill"><input type="checkbox" name="schedule_weekdays" value="{day}"{(" checked" if day in settings["schedule_weekdays"] else "")}><span>{label}</span></label>'
            for day, label in WEEKDAY_LABELS.items()
        )
        target_options = "".join(f'<option value="{day}"{(" selected" if day == settings["target_days"] else "")}>{day} 天</option>' for day in (30, 45, 60))
        content = f"""{self.flash(query.get('message',''))}<header class="page-heading"><div><span class="eyebrow">AUTOMATION SETTINGS</span><h1>补货频率与参数</h1><p>修改后用于下一次自动或手动生成的补货计划，历史批次不受影响。</p></div></header>
        <form method="post" action="/settings">
          <section class="settings-section"><div class="settings-intro"><h2>自动生成频率</h2><p>默认每周二、周五各生成一次，商品部收到站内待办提醒。</p></div><div class="settings-fields"><label class="toggle-line"><input type="checkbox" name="auto_generate" value="1"{(' checked' if settings['auto_generate'] else '')}><span>启用自动生成</span></label><div class="weekday-picker">{weekday_controls}</div><label>生成时间<input type="time" name="schedule_time" value="{esc(settings['schedule_time'])}" required></label></div></section>
          <section class="settings-section"><div class="settings-intro"><h2>库存覆盖参数</h2><p>建议量按目标覆盖需求、安全库存、现有可售和有效在途计算。</p></div><div class="settings-fields field-row"><label>目标覆盖<select name="target_days">{target_options}</select></label><label>安全库存天数<input type="number" name="safety_days" min="0" max="30" value="{settings['safety_days']}"></label></div></section>
          <section class="settings-section"><div class="settings-intro"><h2>补货货号筛选</h2><p>按货号汇总全部尺码。命中条件1或条件2之一，并满足库存支撑门槛，即进入补货计划。</p></div><div class="settings-fields condition-builder"><div class="condition-rule"><span>条件1</span><strong>销量累计</strong></div><div class="field-row settings-fields"><label>近7天最低销量<input type="number" name="min_sales_7" min="0" value="{settings['min_sales_7']}"></label><label>近14天最低销量<input type="number" name="min_sales_14" min="0" value="{settings['min_sales_14']}"></label></div><div class="condition-or">或者</div><div class="condition-rule"><span>条件2</span><strong>连续销售</strong></div><label>连续有销量天数<input type="number" name="min_consecutive_sales_days" min="1" max="14" value="{settings['min_consecutive_sales_days']}"></label><div class="condition-common"><span>共同条件</span><label>最大库存支撑天数<input type="number" name="max_coverage_days" min="1" max="90" step="0.5" value="{settings['max_coverage_days']:g}"></label></div></div></section>
          <section class="settings-section locked-setting"><div class="settings-intro"><h2>预测权重</h2><p>首期采用透明规则，累计更多历史数据后再评估升级。</p></div><div class="weight-display"><span>近 7 天<strong>60%</strong></span><i></i><span>近 14 天<strong>40%</strong></span></div></section>
          <div class="form-footer"><a class="button ghost" href="/dashboard">取消</a><button class="button primary" type="submit">保存设置</button></div>
        </form>"""
        return self.base_page(user, "settings", content, "频率设置")

    def base_page(
        self, user: dict, active: str, content: str, title: str, store: dict | None = None
    ) -> str:
        store = store or self.selected_store()
        unread = db.unread_notifications(
            self.db_path, user["id"], store_id=store["id"]
        )
        store_code = store["store_code"]
        store_query = f"?store={quote(store_code)}"
        nav_items = [
            ("dashboard", f"/dashboard{store_query}", "监控台"),
            ("plans", f"/plans{store_query}", "补货计划"),
        ]
        if user["role"] in {"followup", "admin"}:
            nav_items.append(("followup", "/followup", "跟单任务"))
        if user["role"] in {"merchandise", "admin"}:
            if store["platform_code"] == "tmall":
                nav_items.append(
                    ("data", f"/data/tmall-api{store_query}", "API 配置与试连")
                )
            elif store["store_code"] != "VIP-MTN":
                nav_items.append(
                    ("data", f"/data/api{store_query}", "API 配置与试连")
                )
            else:
                nav_items.extend(
                    [("data", "/data", "数据中心"), ("settings", "/settings", "频率设置")]
                )
        nav_html = "".join(f'<a class="{("active" if key == active else "")}" href="{href}">{label}</a>' for key, href, label in nav_items)
        store_tabs = "".join(
            f'<a class="{("active" if item["id"] == store["id"] else "")}" href="/dashboard?store={quote(item["store_code"])}"><span>{esc(item["platform_name"])}</span><strong>{esc(item["store_name"])}</strong></a>'
            for item in db.list_stores(self.db_path)
        )
        toast = ""
        if unread:
            latest = unread[0]
            toast = f"""<aside class="notice-toast" id="notice-toast"><div><span>新待办</span><strong>{esc(latest['title'])}</strong><p>{esc(latest['body'])}</p><a href="{esc(latest['link'] or f'/dashboard{store_query}')}">立即查看</a></div><form method="post" action="/notifications/read{store_query}"><button type="submit" title="关闭提醒">×</button></form></aside>"""
        if store["platform_code"] == "tmall":
            api_config = db.get_tmall_api_config(self.db_path, store["id"])
            sidebar_title = "API 已鉴权" if api_config["last_test_status"] == "connected" else "API 联调中"
            sidebar_note = api_config["verified_store_name"] or {
                "failed": "店铺鉴权失败",
                "credentials_missing": "凭证待配置",
            }.get(api_config["last_test_status"], "等待店铺授权")
        else:
            api_config = db.get_api_config(self.db_path, store["id"])
            if store["store_code"] == "VIP-MTN":
                sidebar_title = "API 已鉴权" if api_config["last_test_status"] == "connected" else "正式运行"
                sidebar_note = api_config["verified_store_name"] if api_config["last_test_status"] == "connected" else "唯品浏览器报表取数"
            else:
                sidebar_title = "API 已鉴权" if api_config["last_test_status"] == "connected" else "API 联调中"
                sidebar_note = api_config["verified_store_name"] or {
                    "failed": "店铺鉴权失败",
                    "credentials_missing": "凭证待配置",
                }.get(api_config["last_test_status"], "等待 VOP 应用许可")
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(title)} · 补货监控中心</title><style>{self.css()}</style></head>
        <body><div class="app-shell"><aside class="sidebar"><a class="app-brand" href="/dashboard{store_query}"><span>R</span><div><strong>补货监控中心</strong><small>{esc(store['brand_name'])} · {esc(store['platform_name'])}</small></div></a><nav>{nav_html}</nav><div class="sidebar-foot"><span class="live-dot"></span><div><strong>{esc(sidebar_title)}</strong><small>{esc(sidebar_note)}</small></div></div></aside>
        <div class="main-shell"><header class="topbar"><div class="mobile-brand">补货监控中心</div><nav class="mobile-nav">{nav_html}</nav><div class="user-area"><div><strong>{esc(user['display_name'])}</strong><span>{esc(ROLE_LABELS.get(user['role'], user['role']))}</span></div><span class="notification-count">{len(unread)}</span><form method="post" action="/logout"><button class="text-button" type="submit">退出</button></form></div></header><nav class="store-tabs" aria-label="店铺切换">{store_tabs}</nav><main class="page-content">{content}</main></div></div>{toast}<script>(()=>{{const tabs=document.querySelector('.store-tabs');const active=tabs?.querySelector('a.active');if(tabs&&active&&tabs.scrollWidth>tabs.clientWidth){{tabs.scrollLeft=Math.max(0,active.offsetLeft+active.offsetWidth-tabs.clientWidth)}}}})()</script></body></html>"""

    def flash(self, message: str) -> str:
        return f'<div class="flash">{esc(message)}</div>' if message else ""

    def css(self) -> str:
        return """
:root{--ink:#1f2926;--muted:#68736e;--line:#dce2df;--surface:#fff;--canvas:#f4f6f4;--green:#1f6652;--green-dark:#164a3d;--green-soft:#e8f2ee;--amber:#b66d12;--amber-soft:#fff3df;--red:#b34237;--red-soft:#fbeae7;--blue:#326b91;--sidebar:#202825;--radius:6px;--shadow:0 10px 30px rgba(31,41,38,.08)}
.store-tabs{display:flex;gap:0;padding:0 32px;background:#fff;border-bottom:1px solid var(--line);overflow-x:auto}.store-tabs a{display:flex;flex-direction:column;justify-content:center;min-width:210px;height:64px;padding:9px 18px;border-bottom:3px solid transparent;color:var(--muted);white-space:nowrap}.store-tabs a+a{border-left:1px solid #edf0ee}.store-tabs span{font-size:10px}.store-tabs strong{margin-top:3px;color:var(--ink);font-size:13px}.store-tabs a.active{border-bottom-color:var(--green);background:#f8faf9}.store-tabs a.active span,.store-tabs a.active strong{color:var(--green)}
@media(max-width:640px){.store-tabs{padding:0}.store-tabs a{min-width:50%;padding:9px 10px}.store-tabs strong{font-size:12px}}
.dashboard-grid>*{min-width:0}.content-section,.style-group{min-width:0}.table-wrap{max-width:100%}
.credential-flags{display:flex;gap:8px;flex-wrap:wrap}.credential-flags span{padding:6px 9px;background:#f0f3f1;color:var(--muted);font-size:11px}.api-actions-grid{margin-top:22px}.api-scope-note{margin-top:22px}.status-connected{background:var(--green-soft);color:var(--green)}.status-failed,.status-credentials_missing{background:var(--red-soft);color:var(--red)}.button:disabled{cursor:not-allowed;opacity:.45;filter:none!important}.button:disabled:hover{background:var(--green);border-color:var(--green)}
.url-value{display:block;max-width:100%;overflow-wrap:anywhere;word-break:break-word;color:var(--muted);font-size:12px;line-height:1.55}.capture-step-strip{margin-bottom:22px}.capture-step-strip .url-value{font-weight:600;color:var(--ink)}.capture-source-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:22px}.capture-history{margin-top:22px}.status-ready_for_import,.status-imported{background:var(--green-soft);color:var(--green)}.status-needs_mapping,.status-needs_conversion{background:var(--red-soft);color:var(--red)}.status-waiting_download{background:var(--amber-soft);color:var(--amber)}
.condition-tags{display:inline-flex;gap:5px;flex-wrap:wrap;margin-top:7px}.condition-tag{display:inline-flex;padding:3px 7px;border-radius:3px;background:#e8f0f7;color:var(--blue);font-size:10px;font-weight:700}.condition-tag.condition_2{background:var(--amber-soft);color:var(--amber)}.condition-builder{gap:14px}.condition-rule{display:flex;align-items:center;gap:10px;padding-left:12px;border-left:3px solid var(--green)}.condition-rule span,.condition-common>span{font-size:11px;font-weight:750;color:var(--green)}.condition-or{font-size:11px;font-weight:750;color:var(--muted);text-align:center}.condition-common{display:grid;grid-template-columns:auto minmax(220px,1fr);align-items:center;gap:16px;padding-top:14px;border-top:1px solid var(--line)}.condition-common label{display:grid;gap:7px;font-weight:600}
*{box-sizing:border-box}html{background:var(--canvas)}body{margin:0;color:var(--ink);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;font-size:14px;letter-spacing:0}a{color:var(--green);text-decoration:none}button,input,select{font:inherit;letter-spacing:0}button{cursor:pointer}.app-shell{min-height:100vh;display:grid;grid-template-columns:220px minmax(0,1fr)}.sidebar{position:sticky;top:0;height:100vh;background:var(--sidebar);color:#fff;padding:22px 16px;display:flex;flex-direction:column}.app-brand{display:flex;align-items:center;gap:11px;color:#fff;padding:0 7px 24px;border-bottom:1px solid rgba(255,255,255,.1)}.app-brand>span{display:grid;place-items:center;width:34px;height:34px;border-radius:4px;background:#e6b75e;color:#202825;font-weight:800}.app-brand div{display:flex;flex-direction:column;min-width:0}.app-brand strong{font-size:15px;white-space:nowrap}.app-brand small{color:#aebbb5;margin-top:3px}.sidebar nav{display:grid;gap:4px;margin-top:24px}.sidebar nav a{color:#c7d0cc;padding:10px 12px;border-radius:5px}.sidebar nav a:hover,.sidebar nav a.active{background:rgba(255,255,255,.1);color:#fff}.sidebar nav a.active{box-shadow:inset 3px 0 #e6b75e}.sidebar-foot{margin-top:auto;border-top:1px solid rgba(255,255,255,.1);padding:18px 8px 0;display:flex;align-items:center;gap:9px}.sidebar-foot div{display:flex;flex-direction:column}.sidebar-foot small{color:#aebbb5;margin-top:2px}.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#61b78e;box-shadow:0 0 0 4px rgba(97,183,142,.14)}.main-shell{min-width:0}.topbar{height:66px;padding:0 28px;background:rgba(255,255,255,.95);border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:flex-end;position:sticky;top:0;z-index:20}.user-area{display:flex;align-items:center;gap:14px}.user-area>div{display:flex;flex-direction:column;text-align:right}.user-area span{color:var(--muted);font-size:12px}.notification-count{display:grid!important;place-items:center;width:25px;height:25px;border-radius:50%;background:var(--amber-soft);color:var(--amber)!important;font-weight:700}.text-button{border:0;background:transparent;color:var(--muted);padding:7px}.mobile-brand,.mobile-nav{display:none}.page-content{max-width:1500px;margin:0 auto;padding:30px 32px 70px}.page-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;margin-bottom:26px}.page-heading h1{font-size:28px;line-height:1.2;margin:5px 0 7px;letter-spacing:0}.page-heading p{margin:0;color:var(--muted)}.eyebrow{font-size:11px;font-weight:700;letter-spacing:.08em;color:var(--green)}.heading-actions,.task-actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.button{display:inline-flex;justify-content:center;align-items:center;min-height:38px;padding:8px 15px;border:1px solid var(--line);border-radius:5px;background:#fff;color:var(--ink);font-weight:650}.button:hover{border-color:#9eaaa5}.button.primary{background:var(--green);border-color:var(--green);color:#fff}.button.primary:hover{background:var(--green-dark)}.button.ghost{background:#fff}.button.wide{width:100%}.task-band{display:flex;align-items:center;justify-content:space-between;gap:24px;background:#26322e;color:#fff;padding:22px 24px;border-left:5px solid #e6b75e}.task-band h2{margin:4px 0;font-size:20px}.task-band p{margin:0;color:#bfc9c4}.task-band .eyebrow{color:#e6c881}.empty-band{padding:20px 24px;background:#fff;border:1px solid var(--line);display:flex;gap:12px}.metric-strip{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));margin:22px 0;background:#fff;border:1px solid var(--line)}.metric-strip>div{padding:19px 22px;border-right:1px solid var(--line)}.metric-strip>div:last-child{border:0}.metric-strip span{display:block;color:var(--muted);font-size:12px}.metric-strip strong{display:inline-block;font-size:29px;margin-top:4px}.metric-strip small{margin-left:6px;color:var(--muted)}.danger-text{color:var(--red)!important}.warning-text{color:var(--amber)!important}.dashboard-grid{display:grid;grid-template-columns:minmax(0,2fr) minmax(270px,1fr);gap:22px}.content-section{background:#fff;border:1px solid var(--line);padding:22px}.section-heading{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:18px}.section-heading h2{font-size:17px;margin:0 0 4px}.section-heading p{margin:0;color:var(--muted);font-size:12px}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;min-width:720px}th{text-align:left;font-size:12px;color:var(--muted);font-weight:600;padding:10px 11px;border-bottom:1px solid var(--line);white-space:nowrap}td{padding:13px 11px;border-bottom:1px solid #edf0ee;vertical-align:middle}tbody tr:last-child td{border-bottom:0}.item-link{display:flex;flex-direction:column;gap:2px;color:var(--ink)}.item-link span{font-size:12px;color:var(--muted)}.risk,.status{display:inline-flex;align-items:center;white-space:nowrap;border-radius:999px;padding:4px 8px;font-size:11px;font-weight:700}.risk.critical{background:var(--red-soft);color:var(--red)}.risk.warning{background:var(--amber-soft);color:var(--amber)}.risk.watch{background:#edf1f5;color:var(--blue)}.risk.healthy,.risk.no_sales{background:var(--green-soft);color:var(--green)}.status{background:#edf1ef;color:#52605a}.status-merchandise_pending,.status-followup_pending{background:var(--amber-soft);color:var(--amber)}.status-merchandise_editing,.status-followup_processing{background:#e8f0f7;color:var(--blue)}.status-completed{background:var(--green-soft);color:var(--green)}.status-superseded{background:#edf0ee;color:var(--muted)}.status-test{background:var(--amber-soft);color:var(--amber)}.task-band .status{background:rgba(255,255,255,.12);color:#fff}.health-bar{height:14px;background:#edf0ee;display:flex;overflow:hidden;border-radius:3px}.health-bar span{height:100%}.bar-critical{background:var(--red)}.bar-warning{background:#d49a42}.bar-healthy{background:#5b9d81}.health-legend{margin:18px 0}.health-legend>div,.detail-list>div{display:flex;justify-content:space-between;gap:20px;padding:10px 0;border-bottom:1px solid #edf0ee}.health-legend dt,.detail-list dt{color:var(--muted)}.health-legend dd,.detail-list dd{margin:0;font-weight:650}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px}.dot.critical{background:var(--red)}.dot.warning{background:#d49a42}.dot.healthy{background:#5b9d81}.sync-panel{margin-top:18px;padding-top:18px;border-top:1px solid var(--line)}.sync-panel span{display:block;color:var(--muted);font-size:12px}.sync-panel strong{display:block;margin:5px 0}.sync-panel p{color:var(--muted);line-height:1.5}.schedule-line{display:grid;grid-template-columns:repeat(3,1fr);margin-top:22px;background:#fff;border:1px solid var(--line)}.schedule-line>div{padding:16px 20px;border-right:1px solid var(--line)}.schedule-line>div:last-child{border:0}.schedule-line span{display:block;color:var(--muted);font-size:12px;margin-bottom:5px}.plan-heading{margin-bottom:18px}.back-link{display:block;margin-bottom:12px;font-size:12px}.formula-line{display:flex;gap:24px;align-items:center;padding:12px 16px;background:#edf3f0;border-left:3px solid var(--green);color:#496059;margin-bottom:20px}.formula-line .status{margin-left:auto}.plan-groups{display:grid;gap:18px}.style-group{background:#fff;border:1px solid var(--line);scroll-margin-top:85px}.style-heading{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:17px 20px;border-bottom:1px solid var(--line)}.style-heading h2{display:inline;margin:0 0 0 9px;font-size:16px}.style-heading div>h2:first-child{margin-left:0}.style-heading p{margin:5px 0 0;color:var(--muted);font-size:12px}.style-totals{display:flex;gap:22px}.style-totals span{display:flex;flex-direction:column;align-items:flex-end;color:var(--muted);font-size:11px}.style-totals strong{color:var(--ink);font-size:18px;margin-top:2px}.goods-group{border-bottom:1px solid var(--line)}.goods-group:last-child{border-bottom:0}.goods-heading{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:15px 20px;background:#f8faf9}.goods-heading h3{margin:0 0 3px;font-size:14px}.goods-heading p{margin:0;color:var(--muted);font-size:12px}.goods-totals{display:flex;gap:22px}.goods-totals span{color:var(--muted);font-size:11px}.goods-totals strong{display:block;color:var(--ink);font-size:15px;margin-top:2px}.dense-table td{padding:10px}.dense-table input,.dense-table select{min-width:92px}.qty-input{width:74px!important;min-width:74px!important;font-weight:700}.size-label small{display:block;color:var(--green);font-size:10px;margin-top:2px}.cell-note{display:block;color:var(--muted);font-size:10px}.risk-row-critical{box-shadow:inset 3px 0 var(--red)}.risk-row-warning{box-shadow:inset 3px 0 #d49a42}input,select{min-height:36px;border:1px solid #cfd7d3;border-radius:4px;padding:7px 9px;background:#fff;color:var(--ink)}input:focus,select:focus{outline:2px solid rgba(31,102,82,.17);border-color:var(--green)}.sticky-actions{position:sticky;bottom:14px;z-index:10;margin:20px auto 0;max-width:820px;padding:13px 16px;background:#202825;color:#fff;box-shadow:var(--shadow);display:flex;justify-content:space-between;align-items:center;gap:20px}.sticky-actions>div{display:flex;align-items:center;gap:9px}.sticky-actions span{color:#bfc9c4;font-size:12px}.sticky-actions .button.ghost{background:transparent;color:#fff;border-color:#65716c}.empty-cell{text-align:center;color:var(--muted);padding:40px}.two-column{display:grid;grid-template-columns:1fr 1fr;gap:22px}.environment-banner,.generate-section{background:#fff;border:1px solid var(--line);padding:20px 22px;margin-bottom:22px;display:flex;justify-content:space-between;align-items:center;gap:20px}.environment-banner span,.environment-banner p{color:var(--muted)}.environment-banner strong{display:block;font-size:18px;margin:4px 0}.environment-banner p{margin:0}.subtle-note{color:var(--muted);line-height:1.6}.upload-form{display:flex;gap:10px;align-items:center}.file-field{flex:1;border:1px dashed #aab6b1;padding:13px}.file-field span{display:block;margin-bottom:8px;font-weight:650}.file-field input{border:0;padding:0;width:100%}.generate-section{margin-top:22px}.generate-section h2{margin:0 0 5px;font-size:17px}.generate-section p{margin:0;color:var(--muted)}.settings-section{display:grid;grid-template-columns:minmax(250px,1fr) minmax(420px,2fr);gap:40px;background:#fff;border:1px solid var(--line);border-bottom:0;padding:26px}.settings-section:last-of-type{border-bottom:1px solid var(--line)}.settings-intro h2{margin:0 0 7px;font-size:17px}.settings-intro p{margin:0;color:var(--muted);line-height:1.55}.settings-fields{display:grid;gap:18px}.settings-fields label{display:grid;gap:7px;font-weight:600}.toggle-line{display:flex!important;align-items:center;gap:9px}.toggle-line input{min-height:auto}.weekday-picker{display:flex;gap:7px;flex-wrap:wrap}.check-pill input{position:absolute;opacity:0;pointer-events:none}.check-pill span{display:block;border:1px solid #cfd7d3;padding:8px 12px;border-radius:4px;font-weight:500}.check-pill input:checked+span{background:var(--green);border-color:var(--green);color:#fff}.field-row{grid-template-columns:1fr 1fr}.weight-display{display:flex;align-items:center;gap:18px}.weight-display span{display:flex;flex-direction:column;color:var(--muted)}.weight-display strong{font-size:22px;color:var(--ink);margin-top:4px}.weight-display i{width:1px;height:38px;background:var(--line)}.form-footer{display:flex;justify-content:flex-end;gap:10px;padding:20px 0}.flash{padding:11px 14px;background:var(--green-soft);color:var(--green-dark);border-left:3px solid var(--green);margin-bottom:18px}.notice-toast{position:fixed;right:22px;bottom:22px;width:min(370px,calc(100vw - 44px));background:#fff;border:1px solid var(--line);box-shadow:0 18px 50px rgba(31,41,38,.18);z-index:50;padding:17px 42px 17px 18px}.notice-toast div>span{font-size:10px;color:var(--amber);font-weight:700}.notice-toast strong{display:block;margin:4px 0}.notice-toast p{color:var(--muted);line-height:1.5;margin:5px 0 9px}.notice-toast form{position:absolute;right:8px;top:8px}.notice-toast button{border:0;background:transparent;font-size:22px;color:var(--muted)}.login-body{min-height:100vh;background:#edf1ee;display:grid;place-items:center;padding:24px}.login-shell{width:min(900px,100%);min-height:500px;background:#fff;display:grid;grid-template-columns:1.1fr .9fr;box-shadow:var(--shadow)}.login-brand{background:#202825;color:#fff;padding:58px 54px;display:flex;flex-direction:column;justify-content:center}.brand-kicker{color:#e6c881;font-size:11px;letter-spacing:.12em;font-weight:700}.login-brand h1{font-size:38px;margin:13px 0 10px}.login-brand p{color:#bfc9c4;font-size:17px}.login-status{margin-top:90px;color:#cdd6d2;display:flex;align-items:center;gap:9px}.login-form-wrap{padding:55px 46px;display:flex;flex-direction:column;justify-content:center}.login-form-head{display:flex;flex-direction:column;color:var(--muted);margin-bottom:25px}.login-form-head strong{font-size:25px;color:var(--ink);margin-top:3px}.stack-form{display:grid;gap:17px}.stack-form label{display:grid;gap:7px;font-weight:600}.stack-form input{width:100%;height:42px}.demo-accounts{display:flex;gap:8px;flex-wrap:wrap;margin-top:25px}.demo-accounts span{background:#f0f3f1;color:var(--muted);padding:5px 8px;border-radius:3px;font-size:11px}.login-error{background:var(--red-soft);color:var(--red);padding:10px;margin-bottom:14px}
@media(max-width:980px){.app-shell{display:block}.sidebar{display:none}.topbar{height:auto;min-height:62px;padding:10px 18px;justify-content:space-between;gap:14px;flex-wrap:wrap}.mobile-brand{display:block;font-weight:750}.mobile-nav{display:flex;order:3;width:100%;gap:4px;overflow:auto}.mobile-nav a{padding:8px 10px;color:var(--muted);white-space:nowrap}.mobile-nav a.active{color:var(--green);border-bottom:2px solid var(--green)}.page-content{padding:24px 18px 70px}.dashboard-grid,.two-column,.capture-source-grid{grid-template-columns:1fr}.metric-strip{grid-template-columns:repeat(3,1fr)}.metric-strip>div{border-right:1px solid var(--line);border-bottom:1px solid var(--line)}.metric-strip>div:nth-child(3n){border-right:0}.metric-strip>div:nth-child(n+4){border-bottom:0}.metric-strip>div:last-child{border-right:0}.schedule-line{grid-template-columns:1fr}.schedule-line>div{border-right:0;border-bottom:1px solid var(--line)}.settings-section{grid-template-columns:1fr}.formula-line{align-items:flex-start;flex-direction:column;gap:7px}.formula-line .status{margin-left:0}}
@media(max-width:640px){.page-heading,.task-band,.style-heading,.goods-heading,.environment-banner,.generate-section,.capture-history .section-heading{align-items:flex-start;flex-direction:column}.page-heading h1{font-size:24px}.task-actions,.heading-actions{width:100%}.metric-strip{grid-template-columns:1fr 1fr}.metric-strip>div{padding:15px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}.metric-strip>div:nth-child(3n){border-right:1px solid var(--line)}.metric-strip>div:nth-child(n+4){border-bottom:1px solid var(--line)}.metric-strip>div:nth-child(even){border-right:0}.metric-strip>div:nth-child(n+5){border-bottom:0}.metric-strip>div:last-child{border-right:0}.metric-strip strong{font-size:24px}.schedule-line{grid-template-columns:1fr}.style-totals,.goods-totals{width:100%;justify-content:flex-start;flex-wrap:wrap}.style-totals span{align-items:flex-start}.sticky-actions{bottom:6px;align-items:flex-start;flex-direction:column}.sticky-actions>div{width:100%;flex-wrap:wrap}.sticky-actions .button{flex:1}.settings-section{padding:20px;gap:20px}.field-row,.condition-common{grid-template-columns:1fr}.login-shell{grid-template-columns:1fr}.login-brand{padding:30px}.login-brand h1{font-size:29px}.login-status{margin-top:20px}.login-form-wrap{padding:32px 28px}.user-area>div{display:none}}
"""

    def redirect(self, start_response, location: str):
        start_response("302 Found", [("Location", location)])
        return [b""]

    def html_response(self, start_response, content: str, status: str = "200 OK"):
        body = content.encode("utf-8")
        start_response(status, [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body)))])
        return [body]

    def text_response(self, start_response, content: str, status: str = "200 OK"):
        body = content.encode("utf-8")
        start_response(status, [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))])
        return [body]

    def file_response(self, start_response, content: bytes, filename: str):
        encoded = quote(filename)
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                ("Content-Disposition", f"attachment; filename*=UTF-8''{encoded}"),
                ("Content-Length", str(len(content))),
            ],
        )
        return [content]

    def error_response(self, start_response, status: str, message: str):
        return self.html_response(
            start_response,
            f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>处理失败</title><style>{self.css()}</style></head><body class="login-body"><section class="login-form-wrap" style="background:#fff;max-width:520px"><h1>处理失败</h1><p>{esc(message)}</p><a class="button primary" href="javascript:history.back()">返回上一页</a></section></body></html>',
            status,
        )
