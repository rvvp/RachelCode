from __future__ import annotations

import html
import json
import secrets
from http import cookies
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from planning_center import db

SESSIONS: dict[str, int] = {}


class PlanningApplication:
    def __init__(self, db_path: str | Path, catalog_api_url: str, catalog_api_token: str = ""):
        self.db_path = str(db_path)
        self.catalog_api_url = str(catalog_api_url or "http://127.0.0.1:8765").rstrip("/")
        self.catalog_api_token = str(catalog_api_token or "").strip()

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        query = {key: values[0] for key, values in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()}
        user = self.current_user(environ)
        try:
            if path == "/healthz" and method == "GET":
                return self.json_response(start_response, {"status": "ok", "db_exists": Path(self.db_path).exists()})
            if path == "/login":
                if method == "GET":
                    return self.html_response(start_response, self.render_login(query.get("error", "")))
                return self.handle_login(environ, start_response)
            if path == "/logout" and method == "POST":
                return self.handle_logout(environ, start_response)
            if not user:
                return self.redirect(start_response, "/login")
            if path == "/":
                return self.redirect(start_response, "/dashboard")
            if path == "/dashboard" and method == "GET":
                return self.html_response(start_response, self.render_dashboard(user))
            if path == "/category-planning" and method == "GET":
                return self.html_response(start_response, self.render_category_planning(user))
            if path == "/sync" and method == "POST":
                return self.handle_sync(start_response, user)
            if path == "/workbench" and method == "GET":
                return self.html_response(start_response, self.render_workbench(user, query))
            if path == "/pricing/suggest" and method == "POST":
                return self.handle_suggest(environ, start_response, user)
            if path == "/pricing/batch" and method == "POST":
                return self.handle_pricing_batch(environ, start_response, user)
            if path.startswith("/pricing/") and path.endswith("/confirm") and method == "POST":
                return self.handle_confirm(start_response, user, self.path_id(path, "/pricing/", "/confirm"))
            if path.startswith("/pricing/") and path.endswith("/recalculate") and method == "POST":
                return self.handle_recalculate(environ, start_response, user, self.path_id(path, "/pricing/", "/recalculate"))
            if path.startswith("/pricing/") and path.endswith("/submit-review") and method == "POST":
                return self.handle_submit_review(environ, start_response, user, self.path_id(path, "/pricing/", "/submit-review"))
            if path.startswith("/pricing/") and path.endswith("/review-save") and method == "POST":
                return self.handle_review_save(environ, start_response, user, self.path_id(path, "/pricing/", "/review-save"))
            if path.startswith("/pricing/") and path.endswith("/approve") and method == "POST":
                return self.handle_approve(environ, start_response, user, self.path_id(path, "/pricing/", "/approve"))
            if path.startswith("/pricing/") and path.endswith("/publish") and method == "POST":
                return self.handle_publish(start_response, user, self.path_id(path, "/pricing/", "/publish"))
            if path == "/rules" and method == "GET":
                return self.html_response(start_response, self.render_rules(user, query))
            if path == "/rules/category-option" and method == "POST":
                return self.handle_category_option(environ, start_response, user)
            if path.startswith("/rules/category-option/") and path.endswith("/delete") and method == "POST":
                return self.handle_category_option_delete(
                    start_response,
                    user,
                    self.path_id(path, "/rules/category-option/", "/delete"),
                )
            if path == "/rules/channel-option" and method == "POST":
                return self.handle_channel_option(environ, start_response, user)
            if path.startswith("/rules/channel-option/") and path.endswith("/delete") and method == "POST":
                return self.handle_channel_option_delete(
                    start_response,
                    user,
                    self.path_id(path, "/rules/channel-option/", "/delete"),
                )
            if path == "/rules/category" and method == "POST":
                return self.handle_category_rule(environ, start_response, user)
            if path.startswith("/rules/category/") and path.endswith("/delete") and method == "POST":
                return self.handle_category_rule_delete(
                    start_response,
                    user,
                    self.path_id(path, "/rules/category/", "/delete"),
                )
            if path == "/rules/category-cost" and method == "POST":
                return self.handle_category_cost_rule(environ, start_response, user)
            if path.startswith("/rules/category-cost/") and path.endswith("/delete") and method == "POST":
                return self.handle_category_cost_rule_delete(
                    start_response,
                    user,
                    self.path_id(path, "/rules/category-cost/", "/delete"),
                )
            if path == "/rules/supplier" and method == "POST":
                return self.handle_supplier_rule(environ, start_response, user)
            if path.startswith("/rules/supplier/") and path.endswith("/delete") and method == "POST":
                return self.handle_supplier_rule_delete(
                    start_response,
                    user,
                    self.path_id(path, "/rules/supplier/", "/delete"),
                )
            if path == "/stats" and method == "GET":
                return self.html_response(start_response, self.render_stats(user, query))
            if path == "/settings" and method == "GET":
                return self.html_response(start_response, self.render_settings(user))
            return self.html_response(start_response, self.render_message("页面不存在", "没有找到你要访问的页面。"), status="404 Not Found")
        except (ValueError, LookupError) as error:
            return self.html_response(start_response, self.render_message("操作失败", str(error)), status="400 Bad Request")
        except PermissionError as error:
            return self.html_response(start_response, self.render_message("权限不足", str(error)), status="403 Forbidden")
        except Exception as error:
            return self.html_response(start_response, self.render_message("服务暂时不可用", str(error)), status="500 Internal Server Error")

    def path_id(self, path: str, prefix: str, suffix: str) -> int:
        value = path[len(prefix) : -len(suffix)].strip("/")
        if not value.isdigit():
            raise ValueError("记录编号不正确。")
        return int(value)

    def current_user(self, environ):
        parsed = cookies.SimpleCookie(environ.get("HTTP_COOKIE", ""))
        token = parsed.get("planning_session")
        if not token or token.value not in SESSIONS:
            return None
        with db.get_connection(self.db_path) as connection:
            row = connection.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (SESSIONS[token.value],)).fetchone()
        return dict(row) if row else None

    def parse_form(self, environ) -> dict:
        return {key: values[0] for key, values in self.parse_form_values(environ).items()}

    def parse_form_values(self, environ) -> dict[str, list[str]]:
        length = int(environ.get("CONTENT_LENGTH") or "0")
        raw = environ["wsgi.input"].read(length).decode("utf-8")
        return parse_qs(raw, keep_blank_values=True)

    def handle_login(self, environ, start_response):
        form = self.parse_form(environ)
        user = db.authenticate_user(self.db_path, form.get("username", ""), form.get("password", ""))
        if not user:
            return self.html_response(start_response, self.render_login("账号或密码不正确。"), status="401 Unauthorized")
        token = secrets.token_urlsafe(24)
        SESSIONS[token] = user["id"]
        start_response("302 Found", [("Location", "/dashboard"), ("Set-Cookie", f"planning_session={token}; Path=/; HttpOnly; SameSite=Lax")])
        return [b""]

    def handle_logout(self, environ, start_response):
        parsed = cookies.SimpleCookie(environ.get("HTTP_COOKIE", ""))
        token = parsed.get("planning_session")
        if token:
            SESSIONS.pop(token.value, None)
        start_response("302 Found", [("Location", "/login"), ("Set-Cookie", "planning_session=deleted; Path=/; Max-Age=0")])
        return [b""]

    def handle_sync(self, start_response, user):
        self.require_catalog_operator(user)
        items = self.fetch_catalog_products()
        count = db.upsert_source_products(self.db_path, items)
        return self.redirect(start_response, "/workbench?notice=" + self.q(f"已同步 {count} 条待定价商品资料。"))

    def fetch_catalog_products(self) -> list[dict]:
        if not self.catalog_api_token:
            raise ValueError("尚未配置藏宝阁内部 Token，请在启动环境变量中设置 PLANNING_CATALOG_API_TOKEN。")
        request = Request(
            f"{self.catalog_api_url}/api/internal/planning/products",
            headers={"Authorization": f"Bearer {self.catalog_api_token}", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as error:
            raise ValueError(f"读取藏宝阁失败：{error}")
        if not isinstance(payload, dict) or payload.get("error"):
            raise ValueError(payload.get("message") if isinstance(payload, dict) else "藏宝阁返回内容异常。")
        return payload.get("items") or []

    def handle_suggest(self, environ, start_response, user):
        self.require_catalog_operator(user)
        form = self.parse_form(environ)
        product_id = int(form.get("product_id") or 0)
        product = db.get_source_product(self.db_path, product_id)
        if not product:
            raise LookupError("同步商品不存在，请先同步藏宝阁。")
        category = str(form.get("category") or product.get("category_suggestion") or product.get("category") or "").strip()
        category = db.validate_category_option(self.db_path, category)
        with db.get_connection(self.db_path) as connection:
            connection.execute("UPDATE source_products SET category = ? WHERE id = ?", (category, product_id))
        product["category"] = category
        record = db.create_pricing_record(self.db_path, product, user.get("display_name", "商品企划中心"))
        return self.redirect(
            start_response,
            "/workbench?notice="
            + self.q(f"已生成 {record['style_code'] or record['product_name']} 的测算上新价 {record['calculated_price']:g}。")
            + f"#pricing-row-{int(record['source_product_id'])}",
        )

    def handle_confirm(self, start_response, user, record_id: int):
        record = db.get_pricing_record(self.db_path, record_id)
        if not record:
            raise LookupError("定价记录不存在。")
        if user.get("role") == "admin":
            record = db.approve_pricing_record(
                self.db_path,
                record_id,
                record["launch_price"],
                record["channel"],
                user.get("display_name", "企划管理员"),
            )
            message = f"定价记录 {record['publication_id']} 已复核通过。"
        else:
            record = db.submit_pricing_for_review(self.db_path, record_id, record["launch_price"], user.get("display_name", "商品部企划员"))
            message = f"定价记录 {record['publication_id']} 已提交复核。"
        return self.redirect(start_response, "/workbench?notice=" + self.q(message))

    def handle_submit_review(self, environ, start_response, user, record_id: int):
        self.require_catalog_operator(user)
        form = self.parse_form(environ)
        existing = db.get_pricing_record(self.db_path, record_id)
        if not existing:
            raise LookupError("定价记录不存在。")
        record = db.submit_pricing_for_review(
            self.db_path,
            record_id,
            form.get("launch_price") or existing.get("calculated_price") or existing.get("launch_price"),
            user.get("display_name", "商品部企划员"),
            form.get("category"),
            form.get("channel"),
        )
        return self.redirect(
            start_response,
            "/workbench?notice="
            + self.q(f"{record['style_code'] or record['product_name']} 已提交企划管理员复核。")
            + f"#pricing-row-{int(record['source_product_id'])}",
        )

    def handle_recalculate(self, environ, start_response, user, record_id: int):
        self.require_catalog_operator(user)
        form = self.parse_form(environ)
        record = db.recalculate_pricing_record(
            self.db_path,
            record_id,
            form.get("category", ""),
            user.get("display_name", "商品部企划员"),
        )
        return self.redirect(
            start_response,
            "/workbench?notice="
            + self.q(f"{record['style_code'] or record['product_name']} 已按所选品类重新测算，上新价为 {record['calculated_price']:g}。")
            + f"#pricing-row-{int(record['source_product_id'])}",
        )

    def handle_review_save(self, environ, start_response, user, record_id: int):
        self.require_rule_manager(user)
        form = self.parse_form(environ)
        record = db.save_review_price(
            self.db_path,
            record_id,
            form.get("launch_price", ""),
            form.get("channel", ""),
            user.get("display_name", "企划管理员"),
        )
        return self.redirect(start_response, "/workbench?notice=" + self.q(f"{record['style_code'] or record['product_name']} 的复核上新价与渠道已保存。"))

    def handle_approve(self, environ, start_response, user, record_id: int):
        self.require_rule_manager(user)
        form = self.parse_form(environ)
        record = db.approve_pricing_record(
            self.db_path,
            record_id,
            form.get("launch_price", ""),
            form.get("channel", ""),
            user.get("display_name", "企划管理员"),
        )
        return self.redirect(start_response, "/workbench?notice=" + self.q(f"{record['style_code'] or record['product_name']} 已复核通过，可回传藏宝阁。"))

    def handle_pricing_batch(self, environ, start_response, user):
        form = self.parse_form_values(environ)
        action = (form.get("batch_action") or [""])[0]
        field_names = {
            "submit-review": "submit_review_ids",
            "approve": "approve_ids",
            "publish": "publish_ids",
        }
        field_name = field_names.get(action)
        if not field_name:
            raise ValueError("请选择要执行的批量操作。")
        if action == "approve":
            self.require_rule_manager(user)
        else:
            self.require_catalog_operator(user)
        record_ids = self.batch_record_ids(form.get(field_name, []))
        if not record_ids:
            raise ValueError("请先勾选要处理的款式。")
        records = []
        for record_id in record_ids:
            record = db.get_pricing_record(self.db_path, record_id)
            if not record:
                raise LookupError(f"定价记录 {record_id} 不存在。")
            records.append(record)

        if action == "submit-review":
            pending = []
            for record in records:
                if record["status"] not in {"suggested", "conflict"}:
                    raise ValueError(f"{record['style_code'] or record['product_name']} 不在初审阶段。")
                price = (form.get(f"launch_price_{record['id']}") or [record.get("launch_price")])[0]
                category = (form.get(f"category_{record['id']}") or [record.get("category")])[0]
                channel = (form.get(f"channel_{record['id']}") or [record.get("channel")])[0]
                validated_category = db.validate_category_option(self.db_path, category)
                validated_channel = db.validate_channel_option(self.db_path, channel)
                db.calculate_pricing(
                    self.db_path,
                    record["season_year"],
                    validated_category,
                    record["supplier"],
                    record["cost"],
                )
                pending.append(
                    (
                        record,
                        db.validated_launch_price(price),
                        validated_category,
                        validated_channel,
                    )
                )
            for record, price, category, channel in pending:
                db.submit_pricing_for_review(
                    self.db_path,
                    record["id"],
                    price,
                    user.get("display_name", "商品部企划员"),
                    category,
                    channel,
                )
            return self.redirect(start_response, "/workbench?status=review_pending&notice=" + self.q(f"已批量提交 {len(pending)} 款上新定价进入复核。"))

        if action == "approve":
            for record in records:
                if record["status"] != "review_pending":
                    raise ValueError(f"{record['style_code'] or record['product_name']} 不在复核阶段。")
                submitted_price = (form.get(f"review_price_{record['id']}") or [""])[0]
                submitted_channel = (form.get(f"review_channel_{record['id']}") or [""])[0]
                clean_channel = db.validate_channel_option(self.db_path, submitted_channel)
                if (
                    db.validated_launch_price(submitted_price) != db.validated_launch_price(record["launch_price"])
                    or clean_channel != record["channel"]
                ):
                    raise ValueError(f"{record['style_code'] or record['product_name']} 的复核上新价或渠道已修改，请先点击“修改保存”。")
            for record in records:
                db.approve_pricing_record(
                    self.db_path,
                    record["id"],
                    record["launch_price"],
                    record["channel"],
                    user.get("display_name", "企划管理员"),
                )
            return self.redirect(start_response, "/workbench?status=confirmed&notice=" + self.q(f"已批量复核通过 {len(records)} 款上新定价。"))

        for record in records:
            if record["status"] not in {"confirmed", "conflict"}:
                raise ValueError(f"{record['style_code'] or record['product_name']} 尚未完成复核。")
        published = []
        failed = []
        for record in records:
            updated = self.publish_pricing_record(record, user)
            if updated["status"] == "published":
                published.append(updated)
            else:
                failed.append(updated)
        query = {"notice": f"已批量回传 {len(published)} 款上新定价。"}
        if failed:
            query["error"] = f"有 {len(failed)} 款回传失败，请查看版本冲突状态后重新处理。"
        return self.redirect(start_response, "/workbench?" + urlencode(query))

    def handle_publish(self, start_response, user, record_id: int):
        self.require_catalog_operator(user)
        record = db.get_pricing_record(self.db_path, record_id)
        if not record:
            raise LookupError("定价记录不存在。")
        if record["status"] not in {"confirmed", "conflict"}:
            raise ValueError("请先完成复核后再回传。")
        updated = self.publish_pricing_record(record, user)
        if updated["status"] == "published":
            return self.redirect(start_response, "/workbench?notice=" + self.q("上新价格已发布回藏宝阁。"))
        return self.redirect(start_response, "/workbench?error=" + self.q(updated.get("error_message") or "回传失败，请重新同步后处理。"))

    def publish_pricing_record(self, record: dict, user: dict) -> dict:
        if not self.catalog_api_token:
            raise ValueError("尚未配置藏宝阁内部 Token。")
        payload = {
            "publication_id": record["publication_id"],
            "source_version_no": record["source_version_no"],
            "category": record["category"],
            "launch_channel": record.get("channel", ""),
            "launch_price": record["launch_price"],
            "fixed_multiplier": record["fixed_multiplier"],
            "supplier_coefficient": record["supplier_coefficient"],
            "raw_price": record["raw_price"],
            "operator_name": user.get("display_name", "商品企划中心"),
        }
        request = Request(
            f"{self.catalog_api_url}/api/internal/planning/products/{record['source_product_id']}/price-publication",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.catalog_api_token}", "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                error_payload = json.loads(error.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_payload = {}
            result = {
                "status": "conflict" if error.code == 409 else "failed",
                "message": error_payload.get("message") or f"回传藏宝阁失败：HTTP {error.code}",
            }
        except Exception as error:
            result = {"status": "failed", "message": f"回传藏宝阁失败：{error}"}
        if isinstance(result, dict) and result.get("error"):
            result = {"status": "conflict" if result.get("error") == "version_conflict" else "failed", "message": result.get("message", "回传失败")}
        return db.mark_record_published(
            self.db_path,
            record["id"],
            result if isinstance(result, dict) else {"status": "failed", "message": "返回内容异常"},
        )

    def handle_category_option(self, environ, start_response, user):
        self.require_rule_manager(user)
        form = self.parse_form(environ)
        option_id = self.optional_id(form.get("option_id"))
        db.save_category_option(
            self.db_path,
            form.get("name", ""),
            form.get("keywords", ""),
            form.get("sort_order", "0"),
            form.get("note", ""),
            option_id,
        )
        message = "品类选项已修改。" if option_id else "品类选项已新增。"
        return self.redirect(start_response, "/rules?notice=" + self.q(message) + "#category-options")

    def handle_category_option_delete(self, start_response, user, option_id: int):
        self.require_rule_manager(user)
        db.delete_category_option(self.db_path, option_id)
        return self.redirect(start_response, "/rules?notice=" + self.q("品类选项已删除。") + "#category-options")

    def handle_channel_option(self, environ, start_response, user):
        self.require_rule_manager(user)
        form = self.parse_form(environ)
        option_id = self.optional_id(form.get("option_id"))
        db.save_channel_option(
            self.db_path,
            form.get("name", ""),
            form.get("sort_order", "0"),
            form.get("note", ""),
            option_id,
        )
        message = "渠道选项已修改。" if option_id else "渠道选项已新增。"
        return self.redirect(start_response, "/rules?notice=" + self.q(message) + "#channel-options")

    def handle_channel_option_delete(self, start_response, user, option_id: int):
        self.require_rule_manager(user)
        db.delete_channel_option(self.db_path, option_id)
        return self.redirect(start_response, "/rules?notice=" + self.q("渠道选项已删除。") + "#channel-options")

    def handle_category_rule(self, environ, start_response, user):
        self.require_rule_manager(user)
        form = self.parse_form(environ)
        rule_id = self.optional_id(form.get("rule_id"))
        db.save_category_rule(
            self.db_path,
            form.get("season_year", ""),
            "连衣裙",
            float(form.get("multiplier") or 0),
            form.get("note", ""),
            rule_id,
        )
        message = "连衣裙固定倍率已修改。" if rule_id else "连衣裙固定倍率已新增。"
        return self.redirect(start_response, "/rules?notice=" + self.q(message) + "#dress-rules")

    def handle_category_rule_delete(self, start_response, user, rule_id: int):
        self.require_rule_manager(user)
        db.delete_category_rule(self.db_path, rule_id)
        return self.redirect(start_response, "/rules?notice=" + self.q("连衣裙固定倍率已删除。") + "#dress-rules")

    def handle_category_cost_rule(self, environ, start_response, user):
        self.require_rule_manager(user)
        form = self.parse_form(environ)
        rule_id = self.optional_id(form.get("rule_id"))
        db.save_category_cost_rule(
            self.db_path,
            form.get("season_year", ""),
            form.get("lower_cost", ""),
            form.get("upper_cost", ""),
            float(form.get("multiplier") or 0),
            form.get("note", ""),
            rule_id,
        )
        message = "其他品类成本区间倍率已修改。" if rule_id else "其他品类成本区间倍率已新增。"
        return self.redirect(start_response, "/rules?notice=" + self.q(message) + "#cost-rules")

    def handle_category_cost_rule_delete(self, start_response, user, rule_id: int):
        self.require_rule_manager(user)
        db.delete_category_cost_rule(self.db_path, rule_id)
        return self.redirect(start_response, "/rules?notice=" + self.q("成本区间规则已删除。") + "#cost-rules")

    def handle_supplier_rule(self, environ, start_response, user):
        self.require_rule_manager(user)
        form = self.parse_form(environ)
        rule_id = self.optional_id(form.get("rule_id"))
        db.save_supplier_coefficient(
            self.db_path,
            form.get("season_year", ""),
            form.get("supplier", ""),
            float(form.get("coefficient") or 0),
            form.get("note", ""),
            rule_id,
        )
        message = "供应商浮动系数已修改。" if rule_id else "供应商浮动系数已新增。"
        return self.redirect(start_response, "/rules?notice=" + self.q(message) + "#supplier-rules")

    def handle_supplier_rule_delete(self, start_response, user, rule_id: int):
        self.require_rule_manager(user)
        db.delete_supplier_coefficient(self.db_path, rule_id)
        return self.redirect(start_response, "/rules?notice=" + self.q("供应商浮动系数已删除。") + "#supplier-rules")

    def require_rule_manager(self, user: dict) -> None:
        if user.get("role") != "admin":
            raise PermissionError("只有企划管理员可以维护规则。")

    def require_catalog_operator(self, user: dict) -> None:
        if user.get("role") != "planner":
            raise PermissionError("藏宝阁同步、初审提交和回传由商品部初审人员执行。")

    def optional_id(self, value) -> int | None:
        if value in (None, ""):
            return None
        text = str(value).strip()
        if not text.isdigit():
            raise ValueError("规则编号不正确。")
        return int(text)

    def batch_record_ids(self, values: list[str]) -> list[int]:
        record_ids = []
        for value in values:
            text = str(value).strip()
            if not text.isdigit() or int(text) <= 0:
                raise ValueError("批量操作中包含无效的定价记录。")
            record_id = int(text)
            if record_id not in record_ids:
                record_ids.append(record_id)
        if len(record_ids) > 200:
            raise ValueError("单次批量操作最多处理 200 款。")
        return record_ids

    def render_login(self, error: str = "") -> str:
        content = f"""
        <main class='login'><div class='login-mark'>PC / MERCHANDISE PLANNING</div><h1>商品企划中心</h1><p class='muted'>面向新季品类结构与上新价格决策的商品部企划工作台。</p>
        {self.alert(error, 'error') if error else ''}
        <form method='post' action='/login'><label>账号<input name='username' autofocus required></label><label>密码<input name='password' type='password' required></label><button class='primary' type='submit'>进入企划中心</button></form>
        </main>"""
        return self.page("登录 - 商品企划中心", content, None)

    def render_dashboard(self, user: dict) -> str:
        sources = db.list_source_products(self.db_path)
        records = db.list_pricing_records(self.db_path)
        pending = sum(1 for item in sources if item.get("status") == "pending")
        confirmed = sum(1 for item in records if item.get("status") == "confirmed")
        published = sum(1 for item in records if item.get("status") == "published")
        catalog_sync_action = (
            "<form method='post' action='/sync'><button class='primary' type='submit'>立即同步藏宝阁</button></form>"
            if user.get("role") == "planner"
            else "<small class='review-note'>同步与回传由商品部初审人员执行</small>"
        )
        content = f"""
        <section class='hero'><div><div class='eyebrow'>MERCHANDISE PLANNING</div><h1>商品企划中心</h1><p>围绕新季商品结构与上新价格，沉淀商品部从品类计划到定价确认的企划工作。</p></div><div class='hero-note'><span>当前操作人</span><strong>{html.escape(user.get('display_name',''))}</strong><small>{'管理员' if user.get('role') == 'admin' else '商品部企划员'}</small></div></section>
        <section class='module-grid' aria-label='企划板块'>
          <article class='module-entry module-entry-planned'><div class='module-entry-top'><span class='module-index'>01</span><span class='phase-tag'>第二阶段</span></div><div><div class='eyebrow'>CATEGORY PLANNING</div><h2>品类企划</h2><p>新季开始前规划品类结构与 SKU 数计划。</p></div><a class='button' href='/category-planning'>查看板块</a></article>
          <article class='module-entry'><div class='module-entry-top'><span class='module-index'>02</span><span class='phase-tag phase-tag-live'>当前可用</span></div><div><div class='eyebrow'>NEW ARRIVAL PRICING</div><h2>上新定价</h2><p>同步藏宝阁新款，完成价格计算、确认、统计与回传。</p></div><a class='button primary' href='/workbench'>进入工作台</a></article>
        </section>
        <div class='section-label'><div><div class='eyebrow'>PRICING OVERVIEW</div><h2>上新定价概况</h2></div><a href='/stats'>查看价格带统计</a></div>
        <section class='metrics'><a href='/workbench'><span>待定价商品</span><strong>{pending}</strong><small>来源：藏宝阁已提交资料</small></a><a href='/workbench?status=confirmed'><span>待回传定价</span><strong>{confirmed}</strong><small>复核通过，等待回传</small></a><a href='/workbench?status=published'><span>已发布</span><strong>{published}</strong><small>已写回藏宝阁</small></a></section>
        <section class='split'><div class='panel'><div class='panel-head'><div><div class='eyebrow'>QUICK START</div><h2>今天从这里开始</h2></div></div><div class='quick-grid'><a href='/workbench'><b>01</b><span>打开上新审核工作台</span><small>同步新款、确认品类、生成测算上新价</small></a><a href='/rules'><b>02</b><span>检查规则</span><small>品类、渠道、倍率与供应商系数</small></a><a href='/stats'><b>03</b><span>查看价格带分布</span><small>用当前定价结果校验结构</small></a></div></div><div class='panel notice-panel'><div class='eyebrow'>DATA BOUNDARY</div><h2>成本以藏宝阁为准</h2><p>商品企划中心不录入或估算采购成本。所有成本来自藏宝阁跟单部提交的含税价，回传时会核对资料版本，避免旧成本覆盖新资料。</p>{catalog_sync_action}</div></section>
        """
        return self.shell("企划总览", content, user, "dashboard")

    def render_category_planning(self, user: dict) -> str:
        content = """
        <section class='page-heading category-heading'><div><div class='eyebrow'>CATEGORY PLANNING</div><h1>品类企划</h1><p>新季开始前，由商品部规划品类结构与 SKU 数计划。</p></div><span class='phase-badge'>第二阶段</span></section>
        <section class='planning-scope'>
          <div class='scope-primary'><span class='scope-number'>01</span><div><span>企划维度</span><strong>年份季节</strong><p>每个新季建立一份独立的品类企划。</p></div></div>
          <div><span class='scope-number'>02</span><div><span>结构计划</span><strong>品类组合</strong><p>明确当季计划经营的品类范围。</p></div></div>
          <div><span class='scope-number'>03</span><div><span>数量计划</span><strong>SKU 数</strong><p>按品类规划目标 SKU 数及整体规模。</p></div></div>
        </section>
        <section class='panel phase-panel'><div><div class='eyebrow'>PHASE 2</div><h2>板块已预留</h2><p>第二阶段将在此完善季节企划创建、品类明细、SKU 数计划、计划合计与执行对照。当前阶段不产生企划数据，也不影响上新定价流程。</p></div><a class='button primary' href='/workbench'>进入上新定价</a></section>
        """
        return self.shell("品类企划", content, user, "category-planning")

    def render_workbench(self, user: dict, query: dict) -> str:
        notice, error = query.get("notice", ""), query.get("error", "")
        season = query.get("season_year", "")
        status = query.get("status", "")
        if self.catalog_api_token and user.get("role") == "planner":
            try:
                count = db.upsert_source_products(self.db_path, self.fetch_catalog_products())
                if count and not notice:
                    notice = f"已自动同步 {count} 条藏宝阁资料。"
            except ValueError as sync_error:
                if not error:
                    error = str(sync_error)
        products = db.list_source_products(self.db_path, season_year=season)
        records = db.list_pricing_records(self.db_path, season_year=season)
        category_options = db.list_category_options(self.db_path, enabled_only=True)
        channel_options = db.list_channel_options(self.db_path, enabled_only=True)
        latest_records = {}
        for record in records:
            latest_records.setdefault(int(record["source_product_id"]), record)
        workflow_labels = {
            "waiting": "待计算",
            "suggested": "待初审",
            "review_pending": "待复核",
            "confirmed": "复核通过，待回传",
            "published": "已回传",
            "conflict": "版本冲突",
        }
        workflow_classes = {
            "waiting": "waiting",
            "suggested": "suggested",
            "review_pending": "review-pending",
            "confirmed": "confirmed",
            "published": "published",
            "conflict": "conflict",
        }
        filtered_products = []
        for item in products:
            record = latest_records.get(int(item["id"]))
            workflow_status = record.get("status") if record else "waiting"
            if status and workflow_status != status:
                continue
            filtered_products.append((item, record, workflow_status))
        seasons = sorted({item.get("season_year", "") for item in db.list_source_products(self.db_path) if item.get("season_year")}, reverse=True)
        catalog_sync_action = (
            "<form method='post' action='/sync'><button class='primary' type='submit'>同步藏宝阁</button></form>"
            if user.get("role") == "planner"
            else "<span class='review-note'>同步与回传由商品部初审人员执行</span>"
        )
        batch_buttons = (
            "<button type='submit' name='batch_action' value='submit-review' data-label='批量初审提交' disabled>批量初审提交</button>"
            "<button class='primary' type='submit' name='batch_action' value='publish' data-label='批量回传藏宝阁' disabled>批量回传藏宝阁</button>"
            if user.get("role") == "planner"
            else "<button class='primary' type='submit' name='batch_action' value='approve' data-label='批量复核通过' disabled>批量复核通过</button>"
        )
        batch_toolbar = f"""
        <form id='pricing-batch-form' class='pricing-batch-toolbar' method='post' action='/pricing/batch'>
          <label><input id='pricing-select-all' type='checkbox'>全选当前可操作款式</label>
          <span id='pricing-selected-count'>已选 0 款</span>
          <div class='pricing-batch-actions'>{batch_buttons}</div>
        </form>"""
        def select_options(options: list[dict], selected_value: str, placeholder: str) -> str:
            return (
                f"<option value='' {'selected' if not selected_value else ''} disabled>{html.escape(placeholder)}</option>"
                + "".join(
                    f"<option value='{html.escape(option['name'], quote=True)}' {'selected' if option['name'] == selected_value else ''}>{html.escape(option['name'])}</option>"
                    for option in options
                )
            )

        pricing_columns = [
            ("select", "选择", "pricing-select-cell"),
            ("image", "图片", ""),
            ("season", "年份季节", ""),
            ("style", "款号", ""),
            ("color", "款色", ""),
            ("product", "商品名称", ""),
            ("supplier", "供应商", ""),
            ("cost", "含税成本", ""),
            ("source-status", "来源状态", ""),
            ("category", "品类", ""),
            ("rule", "规则计算", ""),
            ("price", "测算上新价", ""),
            ("channel", "渠道划分", ""),
            ("workflow", "流程状态与操作", ""),
        ]
        pricing_colgroup = "<colgroup>" + "".join(
            f"<col data-column-key='{key}'>" for key, _, _ in pricing_columns
        ) + "</colgroup>"
        pricing_header_cells = []
        for key, label, classes in pricing_columns:
            class_attr = f" class='{html.escape(classes, quote=True)}'" if classes else ""
            label_markup = "<span class='visually-hidden'>选择</span>" if key == "select" else html.escape(label)
            pricing_header_cells.append(
                f"<th{class_attr} data-column-key='{key}'>{label_markup}"
                f"<span class='column-resize-handle' title='拖动调整列宽' role='separator' "
                f"aria-label='调整{html.escape(label, quote=True)}列宽' aria-orientation='vertical' tabindex='0'></span></th>"
            )
        pricing_header = "".join(pricing_header_cells)

        rows = []
        for item, record, workflow_status in filtered_products:
            cost = item.get("actual_cost")
            can_price = user.get("role") == "planner" and cost is not None and float(cost or 0) > 0
            source_status_label = {"pending": "已提交商品部", "published": "已完成", "received": "已接收"}.get(item.get("status"), item.get("status") or "未知")
            image_url = str(item.get("image_url") or "").strip()
            image = f"<img src='{html.escape(image_url, quote=True)}' alt='{html.escape(item.get('style_color') or item.get('style_code') or '商品图片', quote=True)}'>" if image_url else "<div class='product-image-empty'>暂无图片</div>"
            source_version = f"<small>来源 V{int(item.get('source_version_no') or 1)}</small>"
            category_cell = ""
            rule_cell = ""
            channel_cell = "<span class='muted'>初审时选择</span>"
            price_cell = "<span class='muted'>—</span>"
            status_cell = "<span class='status status-waiting'>待计算</span>"
            action_cell = "<span class='review-note'>先匹配品类并生成测算上新价</span>"
            selection_cell = "<span class='muted'>—</span>"
            if not record:
                category_value = str(item.get("category_suggestion") or item.get("category") or "")
                if user.get("role") == "planner":
                    category_cell = f"""
                      <form id='pricing-calc-{item['id']}' class='table-action-form pricing-calc-form' method='post' action='/pricing/suggest'>
                        <input type='hidden' name='product_id' value='{item['id']}'>
                        <strong>{html.escape(category_value or '品类待匹配')}</strong>
                      </form>
                      <small>根据商品名称自动判定，初审阶段可确认或修正。</small>
                    """
                    rule_cell = f"<button class='primary' type='submit' form='pricing-calc-{item['id']}' {' ' if can_price else 'disabled'}>生成测算上新价</button><small>连衣裙用固定倍率，其余品类按成本区间。</small>"
                else:
                    category_cell = f"<strong>{html.escape(category_value or '品类待匹配')}</strong><small>系统按商品名称自动判定</small>"
                    rule_cell = "<span class='review-note'>由商品部初审人员生成测算上新价</span>"
            else:
                record_status = str(record.get("status") or "")
                status_label = workflow_labels.get(record_status, record_status)
                status_class = workflow_classes.get(record_status, "waiting")
                record_error = f"<small class='error-text'>{html.escape(record['error_message'])}</small>" if record.get("error_message") else ""
                calculated_price = float(record.get("calculated_price") or record["launch_price"])
                calculated_price_value = html.escape(f"{calculated_price:g}", quote=True)
                price_value = html.escape(f"{float(record['launch_price']):g}", quote=True)
                rule_expression = f"{record['fixed_multiplier']:g} × {record['supplier_coefficient']:g}"
                rule_raw_price = f"= {record['raw_price']:.1f} 原始"
                category_cell = f"<strong>{html.escape(record['category'])}</strong>"
                rule_cell = f"<span class='rule-summary'><span class='rule-expression'>{html.escape(rule_expression)}</span><span class='rule-raw-price'>{html.escape(rule_raw_price)}</span></span>{record_error}"
                channel_label = record.get("channel") or ("待初审选择" if record_status in {"suggested", "conflict"} else "历史记录未划分")
                channel_cell = f"<strong>{html.escape(channel_label)}</strong>"
                price_cell = f"<span class='price calculated-price'>{calculated_price_value}</span><small>当前执行价：{price_value}</small><small>{html.escape(record['publication_id'])}</small>"
                status_cell = f"<span class='status status-{status_class}'>{html.escape(status_label)}</span>"
                controls = ""
                if record_status in {"suggested", "conflict"} and user.get("role") == "planner":
                    selection_cell = f"<input class='pricing-batch-checkbox' type='checkbox' name='submit_review_ids' value='{record['id']}' form='pricing-batch-form' aria-label='选择 {html.escape(record['style_code'] or record['product_name'], quote=True)} 进行批量初审'>"
                    category_cell = f"""
                    <div class='category-review-control'>
                      <strong class='category-current' data-category-current='{record['id']}'>{html.escape(record['category'] or '品类待匹配')}</strong>
                      <button type='button' class='compact-button category-edit-button' data-category-target='initial-category-{record['id']}' aria-controls='initial-category-{record['id']}' aria-expanded='false'>修改</button>
                      <select id='initial-category-{record['id']}' class='initial-review-category' name='category' form='initial-review-form-{record['id']}' required hidden>{select_options(category_options, record['category'], '请选择品类')}</select>
                    </div>
                    <small>系统按商品名称自动判定；需要调整时点击“修改”，选项来自规则中的品类。</small>"""
                    rule_cell += f"<button class='compact-button recalculate-button' type='submit' form='initial-review-form-{record['id']}' formaction='/pricing/{record['id']}/recalculate' formnovalidate>按所选品类重新测算</button>"
                    channel_cell = f"<label class='cell-field'>渠道<select id='initial-channel-{record['id']}' class='initial-review-channel' name='channel' form='initial-review-form-{record['id']}' required>{select_options(channel_options, record.get('channel') or '', '请选择渠道')}</select></label>"
                    controls = f"""
                    <form id='initial-review-form-{record['id']}' class='table-action-form price-review-form initial-review-form' method='post' action='/pricing/{record['id']}/submit-review'>
                      <label>初审上新价<input id='initial-price-{record['id']}' class='initial-review-price' name='launch_price' type='number' min='1' step='1' inputmode='numeric' value='{price_value}' data-calculated-value='{calculated_price_value}' required></label>
                      <button class='primary' type='submit'>确认并提交复核</button>
                    </form>
                    <small>默认使用测算上新价；如需调整，可直接修改初审上新价。</small>"""
                elif record_status == "review_pending" and user.get("role") == "admin":
                    selection_cell = f"<input class='pricing-batch-checkbox review-batch-checkbox' type='checkbox' name='approve_ids' value='{record['id']}' form='pricing-batch-form' aria-label='选择 {html.escape(record['style_code'] or record['product_name'], quote=True)} 进行批量复核'>"
                    controls = f"""
                    <div class='review-controls'><span class='review-note'>商品部初审已提交，请进行复核</span>
                      <form class='table-action-form price-review-form review-approval-form' method='post' action='/pricing/{record['id']}/review-save'>
                        <label>复核上新价<input name='launch_price' type='number' min='1' step='1' inputmode='numeric' value='{price_value}' data-saved-value='{price_value}' required></label>
                        <button type='submit'>修改保存</button>
                        <label>复核渠道<select name='channel' data-saved-value='{html.escape(record.get('channel') or '', quote=True)}' required>{select_options(channel_options, record.get('channel') or '', '请选择渠道')}</select></label>
                        <button class='primary review-approve-button' type='submit' formaction='/pricing/{record['id']}/approve'>复核通过</button>
                      </form>
                    </div>"""
                elif record_status == "confirmed" and user.get("role") == "planner":
                    selection_cell = f"<input class='pricing-batch-checkbox' type='checkbox' name='publish_ids' value='{record['id']}' form='pricing-batch-form' aria-label='选择 {html.escape(record['style_code'] or record['product_name'], quote=True)} 批量回传藏宝阁'>"
                    controls = f"<form class='table-action-form' method='post' action='/pricing/{record['id']}/publish'><button class='primary' type='submit'>回传藏宝阁</button></form>"
                elif record_status == "confirmed" and user.get("role") == "admin":
                    controls = "<span class='review-note'>复核已通过，待商品部回传</span>"
                elif record_status == "published":
                    controls = "<span class='review-note'>已完成回传</span>"
                else:
                    controls = "<span class='review-note'>等待下一处理环节</span>"
                action_cell = controls
            workflow_cell = f"<div class='workflow-status'>{status_cell}</div><div class='workflow-actions'>{action_cell}</div>"
            rows.append(f"""
              <tr id='pricing-row-{int(item['id'])}'>
                <td class='pricing-select-cell'>{selection_cell}</td>
                <td class='pricing-image-cell image-cell'>{image}</td>
                <td><strong>{html.escape(item.get('season_year') or '未提供')}</strong>{source_version}</td>
                <td><strong>{html.escape(item.get('style_code') or '未提供')}</strong></td>
                <td>{html.escape(item.get('style_color') or item.get('color_name') or '未提供')}</td>
                <td>{html.escape(item.get('product_name') or '未提供')}</td>
                <td>{html.escape(item.get('supplier') or '未提供')}</td>
                <td><strong class='cost-value'>{html.escape(f"{float(cost):g}" if cost is not None else '未提供')}</strong></td>
                <td><span class='status status-source'>{html.escape(source_status_label)}</span></td>
                <td class='pricing-category-cell'>{category_cell}</td>
                <td class='pricing-rule-cell'>{rule_cell}</td>
                <td class='price-cell'>{price_cell}</td>
                <td class='pricing-channel-cell'>{channel_cell}</td>
                <td class='pricing-workflow-cell'>{workflow_cell}</td>
              </tr>""")
        content = f"""
        <section class='page-heading'><div><div class='eyebrow'>NEW ARRIVAL PRICING</div><h1>上新审核工作台</h1><p>所有款色集中在一张定价资料与审核卡片中，按条目完成规则匹配、初审、复核和回传。</p></div>{catalog_sync_action}</section>
        {self.alert(notice, 'success') if notice else ''}{self.alert(error, 'error') if error else ''}
        <section class='filter-bar'><form method='get' action='/workbench'><label>年份季节<select name='season_year'><option value=''>全部季节</option>{''.join(f"<option value='{html.escape(value, quote=True)}' {'selected' if value == season else ''}>{html.escape(value)}</option>" for value in seasons)}</select></label><label>定价状态<select name='status'><option value=''>全部状态</option><option value='waiting' {'selected' if status == 'waiting' else ''}>待计算</option><option value='suggested' {'selected' if status == 'suggested' else ''}>待初审</option><option value='review_pending' {'selected' if status == 'review_pending' else ''}>待复核</option><option value='confirmed' {'selected' if status == 'confirmed' else ''}>复核通过，待回传</option><option value='published' {'selected' if status == 'published' else ''}>已回传</option><option value='conflict' {'selected' if status == 'conflict' else ''}>版本冲突</option></select></label><button type='submit'>筛选</button></form></section>
        <section class='workbench-summary'><span>当前显示</span><strong>{len(filtered_products)} 款</strong><small>左侧固定：勾选、图片；资料字段：年份季节、款号、款色、商品名称、供应商、含税成本、来源状态</small></section>
        <section class='panel pricing-board'><div class='panel-head'><div><div class='eyebrow'>PRICING BOARD</div><h2>初审与复核</h2><p class='hint'>来源资料、测算结果和流程操作在同一行展示，状态与操作合并在最后一列。</p></div><div class='pricing-board-tools'><button id='pricing-reset-columns' class='compact-button' type='button' title='恢复默认列宽'>恢复默认列宽</button><span class='count'>{len(filtered_products)} 款</span></div></div>{batch_toolbar}<div class='table-wrap pricing-table-wrap'><table class='pricing-table' data-resizable-columns='pricing-v1'>{pricing_colgroup}<thead><tr>{pricing_header}</tr></thead><tbody>{''.join(rows) if rows else f'<tr><td colspan="14" class="empty">暂无符合条件的款色。请同步藏宝阁或调整筛选条件。</td></tr>'}</tbody></table></div></section>
        <script>
        (() => {{
          const storageKey = 'planning-workbench-scroll';
          const tableWrap = document.querySelector('.pricing-table-wrap');
          const pricingTable = document.querySelector('.pricing-table[data-resizable-columns]');
          const resizeStorageKey = 'planning-workbench-column-widths-v1';
          const resizeHeaders = pricingTable ? Array.from(pricingTable.querySelectorAll('thead th[data-column-key]')) : [];
          const resizeColumns = pricingTable ? Array.from(pricingTable.querySelectorAll('col[data-column-key]')) : [];
          const defaultColumnWidths = {{
            select: 44, image: 78, season: 105, style: 88, color: 92, product: 130,
            supplier: 110, cost: 88, 'source-status': 112, category: 170, rule: 180,
            price: 112, channel: 135, workflow: 270
          }};
          const minimumColumnWidths = {{
            select: 44, image: 68, season: 84, style: 72, color: 72, product: 100,
            supplier: 90, cost: 72, 'source-status': 92, category: 130, rule: 150,
            price: 96, channel: 110, workflow: 220
          }};
          const maximumColumnWidth = 640;
          const readColumnWidths = () => {{
            const widths = {{...defaultColumnWidths}};
            try {{
              const saved = JSON.parse(localStorage.getItem(resizeStorageKey) || 'null');
              if (saved && typeof saved === 'object') {{
                for (const key of Object.keys(widths)) {{
                  const value = Number(saved[key]);
                  if (Number.isFinite(value)) widths[key] = value;
                }}
              }}
            }} catch (error) {{}}
            return widths;
          }};
          const clampColumnWidth = (key, value) => Math.min(
            maximumColumnWidth,
            Math.max(minimumColumnWidths[key] || 72, Math.round(Number(value) || defaultColumnWidths[key] || 72))
          );
          const applyColumnWidth = (header, width) => {{
            const key = header.dataset.columnKey;
            const safeWidth = clampColumnWidth(key, width);
            const column = resizeColumns.find((item) => item.dataset.columnKey === key);
            if (column) column.style.width = `${{safeWidth}}px`;
            header.style.width = `${{safeWidth}}px`;
            if (key === 'select') pricingTable.style.setProperty('--select-column-width', `${{safeWidth}}px`);
            if (key === 'image') pricingTable.style.setProperty('--image-column-width', `${{safeWidth}}px`);
            return safeWidth;
          }};
          const saveColumnWidths = () => {{
            const widths = {{}};
            resizeHeaders.forEach((header) => {{
              widths[header.dataset.columnKey] = Math.round(header.getBoundingClientRect().width);
            }});
            try {{ localStorage.setItem(resizeStorageKey, JSON.stringify(widths)); }} catch (error) {{}}
          }};
          if (pricingTable) {{
            const widths = readColumnWidths();
            resizeHeaders.forEach((header) => applyColumnWidth(header, widths[header.dataset.columnKey]));
            const resetButton = document.querySelector('#pricing-reset-columns');
            resetButton?.addEventListener('click', () => {{
              try {{ localStorage.removeItem(resizeStorageKey); }} catch (error) {{}}
              resizeHeaders.forEach((header) => applyColumnWidth(header, defaultColumnWidths[header.dataset.columnKey]));
            }});
            let resizeState = null;
            const stopResize = () => {{
              if (!resizeState) return;
              resizeState = null;
              document.body.classList.remove('column-resizing');
              saveColumnWidths();
            }};
            resizeHeaders.forEach((header) => {{
              const handle = header.querySelector('.column-resize-handle');
              if (!handle) return;
              handle.addEventListener('pointerdown', (event) => {{
                if (event.button !== 0) return;
                event.preventDefault();
                resizeState = {{
                  header,
                  startX: event.clientX,
                  startWidth: header.getBoundingClientRect().width
                }};
                document.body.classList.add('column-resizing');
                handle.setPointerCapture?.(event.pointerId);
              }});
              handle.addEventListener('pointerup', stopResize);
              handle.addEventListener('pointercancel', stopResize);
              handle.addEventListener('keydown', (event) => {{
                if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
                event.preventDefault();
                const direction = event.key === 'ArrowRight' ? 1 : -1;
                applyColumnWidth(header, header.getBoundingClientRect().width + direction * 10);
                saveColumnWidths();
              }});
            }});
            document.addEventListener('pointermove', (event) => {{
              if (!resizeState) return;
              applyColumnWidth(
                resizeState.header,
                resizeState.startWidth + event.clientX - resizeState.startX
              );
            }});
            document.addEventListener('pointerup', stopResize);
            document.addEventListener('pointercancel', stopResize);
          }}
          const batchForm = document.querySelector('#pricing-batch-form');
          const selectAll = document.querySelector('#pricing-select-all');
          const selectedCount = document.querySelector('#pricing-selected-count');
          const batchChecks = Array.from(document.querySelectorAll('.pricing-batch-checkbox'));
          const batchButtons = Array.from(document.querySelectorAll("#pricing-batch-form button[name='batch_action']"));
          const batchFieldByAction = {{'submit-review': 'submit_review_ids', 'approve': 'approve_ids', 'publish': 'publish_ids'}};
          const updateBatchControls = () => {{
            const enabled = batchChecks.filter((checkbox) => !checkbox.disabled);
            const selected = enabled.filter((checkbox) => checkbox.checked);
            if (selectedCount) selectedCount.textContent = `已选 ${{selected.length}} 款`;
            if (selectAll) {{
              selectAll.disabled = enabled.length === 0;
              selectAll.checked = enabled.length > 0 && selected.length === enabled.length;
              selectAll.indeterminate = selected.length > 0 && selected.length < enabled.length;
            }}
            batchButtons.forEach((button) => {{
              const fieldName = batchFieldByAction[button.value];
              const actionCount = selected.filter((checkbox) => checkbox.name === fieldName).length;
              button.disabled = actionCount === 0;
              button.textContent = actionCount > 0 ? `${{button.dataset.label}}（${{actionCount}}）` : button.dataset.label;
            }});
          }};
          if (selectAll) selectAll.addEventListener('change', () => {{
            batchChecks.forEach((checkbox) => {{ if (!checkbox.disabled) checkbox.checked = selectAll.checked; }});
            updateBatchControls();
          }});
          batchChecks.forEach((checkbox) => checkbox.addEventListener('change', updateBatchControls));
          document.querySelectorAll('.category-edit-button').forEach((button) => {{
            button.addEventListener('click', () => {{
              const select = document.getElementById(button.dataset.categoryTarget);
              if (!select) return;
              select.hidden = !select.hidden;
              button.setAttribute('aria-expanded', String(!select.hidden));
              if (!select.hidden) select.focus();
            }});
          }});
          document.querySelectorAll('.initial-review-category').forEach((select) => {{
            select.addEventListener('change', () => {{
              const current = document.querySelector(`[data-category-current="${{select.id.replace('initial-category-', '')}}"]`);
              if (current && select.selectedOptions[0]) current.textContent = select.selectedOptions[0].textContent;
            }});
          }});
          if (batchForm) batchForm.addEventListener('submit', (event) => {{
            batchForm.querySelectorAll('.batch-row-value').forEach((input) => input.remove());
            const action = event.submitter?.value;
            if (!['submit-review', 'approve'].includes(action)) return;
            const checkboxName = action === 'submit-review' ? 'submit_review_ids' : 'approve_ids';
            const inputSelector = action === 'submit-review' ? '#initial-price-' : '#pricing-row-';
            const selectedForPricing = batchChecks.filter((checkbox) => checkbox.name === checkboxName && checkbox.checked);
            for (const checkbox of selectedForPricing) {{
              const priceInput = action === 'submit-review'
                ? document.querySelector(`${{inputSelector}}${{checkbox.value}}`)
                : checkbox.closest('tr')?.querySelector(".review-approval-form input[name='launch_price']");
              if (!priceInput || !priceInput.reportValidity()) {{
                event.preventDefault();
                return;
              }}
              const hidden = document.createElement('input');
              hidden.type = 'hidden';
              hidden.className = 'batch-row-value';
              hidden.name = `${{action === 'submit-review' ? 'launch_price' : 'review_price'}}_${{checkbox.value}}`;
              hidden.value = priceInput.value;
              batchForm.appendChild(hidden);
              const row = checkbox.closest('tr');
              if (action === 'submit-review') {{
                for (const [field, selector] of [['category', '.initial-review-category'], ['channel', '.initial-review-channel']]) {{
                  const select = row?.querySelector(selector);
                  if (!select || !select.reportValidity()) {{
                    event.preventDefault();
                    return;
                  }}
                  const optionHidden = document.createElement('input');
                  optionHidden.type = 'hidden';
                  optionHidden.className = 'batch-row-value';
                  optionHidden.name = `${{field}}_${{checkbox.value}}`;
                  optionHidden.value = select.value;
                  batchForm.appendChild(optionHidden);
                }}
              }} else {{
                const channelSelect = row?.querySelector(".review-approval-form select[name='channel']");
                if (!channelSelect || !channelSelect.reportValidity()) {{
                  event.preventDefault();
                  return;
                }}
                const channelHidden = document.createElement('input');
                channelHidden.type = 'hidden';
                channelHidden.className = 'batch-row-value';
                channelHidden.name = `review_channel_${{checkbox.value}}`;
                channelHidden.value = channelSelect.value;
                batchForm.appendChild(channelHidden);
              }}
            }}
          }});
          document.querySelectorAll('.review-approval-form').forEach((form) => {{
            const input = form.querySelector("input[name='launch_price']");
            const channel = form.querySelector("select[name='channel']");
            const approveButton = form.querySelector('.review-approve-button');
            const batchCheckbox = form.closest('tr')?.querySelector('.review-batch-checkbox');
            if (!input || !channel || !approveButton) return;
            const syncApprovalState = () => {{
              const savedValue = Number(input.dataset.savedValue);
              const currentValue = Number(input.value);
              const priceChanged = !input.validity.valid || !input.value.trim() || currentValue !== savedValue;
              const channelChanged = !channel.validity.valid || !channel.value || channel.value !== channel.dataset.savedValue;
              const reviewChanged = priceChanged || channelChanged;
              approveButton.disabled = reviewChanged;
              if (batchCheckbox) {{
                batchCheckbox.disabled = reviewChanged;
                if (reviewChanged) batchCheckbox.checked = false;
              }}
              updateBatchControls();
            }};
            input.addEventListener('input', syncApprovalState);
            input.addEventListener('change', syncApprovalState);
            channel.addEventListener('change', syncApprovalState);
            syncApprovalState();
          }});
          updateBatchControls();
          document.querySelectorAll('.pricing-table form, #pricing-batch-form').forEach((form) => {{
            form.addEventListener('submit', () => {{
              sessionStorage.setItem(storageKey, JSON.stringify({{
                pageX: window.scrollX,
                pageY: window.scrollY,
                tableX: tableWrap ? tableWrap.scrollLeft : 0,
                savedAt: Date.now()
              }}));
            }});
          }});
          try {{
            const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
            sessionStorage.removeItem(storageKey);
            if (saved && Date.now() - Number(saved.savedAt || 0) < 30000) {{
              history.replaceState(null, '', window.location.pathname + window.location.search);
              const restoreScroll = () => {{
                if (tableWrap) tableWrap.scrollLeft = Number(saved.tableX || 0);
                window.scrollTo(Number(saved.pageX || 0), Number(saved.pageY || 0));
              }};
              requestAnimationFrame(() => {{
                requestAnimationFrame(restoreScroll);
              }});
              setTimeout(restoreScroll, 150);
            }}
          }} catch (error) {{
            sessionStorage.removeItem(storageKey);
          }}
        }})();
        </script>
        """
        return self.shell("上新审核工作台", content, user, "workbench")

    def render_rules(self, user: dict, query: dict | None = None) -> str:
        query = query or {}
        categories = db.list_category_options(self.db_path)
        channels = db.list_channel_options(self.db_path)
        dress_rules = db.list_category_rules(self.db_path)
        cost_rules = db.list_category_cost_rules(self.db_path)
        suppliers = db.list_supplier_coefficients(self.db_path)
        can_manage = user.get("role") == "admin"

        def selected(items: list[dict], query_key: str) -> dict | None:
            try:
                selected_id = int(query.get(query_key) or 0)
            except (TypeError, ValueError):
                return None
            return next((item for item in items if int(item["id"]) == selected_id), None)

        def field(item: dict | None, key: str) -> str:
            value = item.get(key) if item else ""
            if value is None:
                return ""
            if isinstance(value, float):
                value = f"{value:g}"
            return html.escape(str(value), quote=True)

        category_edit = selected(categories, "edit_category_option") if can_manage else None
        channel_edit = selected(channels, "edit_channel_option") if can_manage else None
        dress_edit = selected(dress_rules, "edit_dress") if can_manage else None
        cost_edit = selected(cost_rules, "edit_cost") if can_manage else None
        supplier_edit = selected(suppliers, "edit_supplier") if can_manage else None

        category_option_rows = "".join(
            f"<tr><td><strong>{html.escape(item['name'])}</strong><small>{'连衣裙固定倍率' if item['pricing_group'] == 'dress' else '其他品类成本区间倍率'}</small></td><td>{html.escape(item['keywords'] or '未配置，作为人工选项')}</td><td>{int(item['sort_order'])}</td><td>{html.escape(item['note'])}</td>"
            + (
                f"<td><div class='rule-actions'><a class='button compact-button' href='/rules?edit_category_option={item['id']}#category-options'>编辑</a><form method='post' action='/rules/category-option/{item['id']}/delete' onsubmit=\"return confirm('确定删除这个品类选项吗？');\"><button class='danger-button' type='submit'>删除</button></form></div></td>"
                if can_manage
                else ""
            )
            + "</tr>"
            for item in categories
        )
        channel_option_rows = "".join(
            f"<tr><td><strong>{html.escape(item['name'])}</strong></td><td>{int(item['sort_order'])}</td><td>{html.escape(item['note'])}</td>"
            + (
                f"<td><div class='rule-actions'><a class='button compact-button' href='/rules?edit_channel_option={item['id']}#channel-options'>编辑</a><form method='post' action='/rules/channel-option/{item['id']}/delete' onsubmit=\"return confirm('确定删除这个渠道选项吗？');\"><button class='danger-button' type='submit'>删除</button></form></div></td>"
                if can_manage
                else ""
            )
            + "</tr>"
            for item in channels
        )
        dress_rows = "".join(
            f"<tr><td>{html.escape(item['season_year'] or '默认')}</td><td>{item['multiplier']:g}</td><td>{html.escape(item['note'])}</td>"
            + (
                f"<td><div class='rule-actions'><a class='button compact-button' href='/rules?edit_dress={item['id']}#dress-rules'>编辑</a><form method='post' action='/rules/category/{item['id']}/delete' onsubmit=\"return confirm('确定删除这条连衣裙倍率规则吗？');\"><button class='danger-button' type='submit'>删除</button></form></div></td>"
                if can_manage
                else ""
            )
            + "</tr>"
            for item in dress_rules
        )
        cost_rows = ''.join(
            f"<tr><td>{html.escape(item['season_year'] or '默认')}</td><td>{html.escape(self.cost_range_label(item.get('lower_cost'), item.get('upper_cost')))}</td><td>{item['multiplier']:g}</td><td>{html.escape(item['note'])}</td>"
            + (
                f"<td><div class='rule-actions'><a class='button compact-button' href='/rules?edit_cost={item['id']}#cost-rules'>编辑</a><form method='post' action='/rules/category-cost/{item['id']}/delete' onsubmit=\"return confirm('确定删除这条成本区间规则吗？');\"><button class='danger-button' type='submit'>删除</button></form></div></td>"
                if can_manage
                else ""
            )
            + "</tr>"
            for item in cost_rules
        )
        supplier_rows = "".join(
            f"<tr><td>{html.escape(item['season_year'] or '默认')}</td><td>{html.escape(item['supplier'])}</td><td>{item['coefficient']:g}</td><td>{html.escape(item['note'])}</td>"
            + (
                f"<td><div class='rule-actions'><a class='button compact-button' href='/rules?edit_supplier={item['id']}#supplier-rules'>编辑</a><form method='post' action='/rules/supplier/{item['id']}/delete' onsubmit=\"return confirm('确定删除这条供应商系数规则吗？');\"><button class='danger-button' type='submit'>删除</button></form></div></td>"
                if can_manage
                else ""
            )
            + "</tr>"
            for item in suppliers
        )

        category_option_form = ""
        channel_option_form = ""
        dress_form = ""
        cost_form = ""
        supplier_form = ""
        if can_manage:
            category_option_form = f"""
            <form class='rule-form option-rule-form' method='post' action='/rules/category-option'>
              <input type='hidden' name='option_id' value='{field(category_edit, 'id')}'>
              <label><span>品类名称</span><input name='name' value='{field(category_edit, 'name')}' placeholder='例如 针织衫' required></label>
              <label><span>商品名匹配词</span><input name='keywords' value='{field(category_edit, 'keywords')}' placeholder='多个词用逗号分隔'></label>
              <label><span>排序</span><input name='sort_order' value='{field(category_edit, 'sort_order') or '0'}' type='number' min='0' max='9999' step='1' required></label>
              <label><span>备注</span><input name='note' value='{field(category_edit, 'note')}' placeholder='选填'></label>
              <div class='form-actions'><button class='primary' type='submit'>{'保存修改' if category_edit else '新增品类'}</button>{"<a class='button' href='/rules#category-options'>取消</a>" if category_edit else ''}</div>
            </form>"""
            channel_option_form = f"""
            <form class='rule-form option-rule-form' method='post' action='/rules/channel-option'>
              <input type='hidden' name='option_id' value='{field(channel_edit, 'id')}'>
              <label><span>渠道名称</span><input name='name' value='{field(channel_edit, 'name')}' placeholder='例如 天猫' required></label>
              <label><span>排序</span><input name='sort_order' value='{field(channel_edit, 'sort_order') or '0'}' type='number' min='0' max='9999' step='1' required></label>
              <label><span>备注</span><input name='note' value='{field(channel_edit, 'note')}' placeholder='选填'></label>
              <div class='form-actions'><button class='primary' type='submit'>{'保存修改' if channel_edit else '新增渠道'}</button>{"<a class='button' href='/rules#channel-options'>取消</a>" if channel_edit else ''}</div>
            </form>"""
            dress_form = f"""
            <form class='rule-form dress-rule-form' method='post' action='/rules/category'>
              <input type='hidden' name='rule_id' value='{field(dress_edit, 'id')}'>
              <label><span>适用季节</span><input name='season_year' value='{field(dress_edit, 'season_year')}' placeholder='留空为默认'></label>
              <label><span>固定倍率</span><input name='multiplier' value='{field(dress_edit, 'multiplier')}' type='number' min='0.01' step='0.01' placeholder='例如 4.20' required></label>
              <label><span>备注</span><input name='note' value='{field(dress_edit, 'note')}' placeholder='选填'></label>
              <div class='form-actions'><button class='primary' type='submit'>{'保存修改' if dress_edit else '新增规则'}</button>{"<a class='button' href='/rules#dress-rules'>取消</a>" if dress_edit else ''}</div>
            </form>"""
            cost_form = f"""
            <form class='rule-form cost-rule-form' method='post' action='/rules/category-cost'>
              <input type='hidden' name='rule_id' value='{field(cost_edit, 'id')}'>
              <label><span>适用季节</span><input name='season_year' value='{field(cost_edit, 'season_year')}' placeholder='留空为默认'></label>
              <label><span>成本下限（包含）</span><input name='lower_cost' value='{field(cost_edit, 'lower_cost')}' type='number' min='0' step='0.01' placeholder='不填表示无下限'></label>
              <label><span>成本上限（不包含）</span><input name='upper_cost' value='{field(cost_edit, 'upper_cost')}' type='number' min='0' step='0.01' placeholder='不填表示无上限'></label>
              <label><span>倍率</span><input name='multiplier' value='{field(cost_edit, 'multiplier')}' type='number' min='0.01' step='0.01' placeholder='例如 3.90' required></label>
              <label><span>备注</span><input name='note' value='{field(cost_edit, 'note')}' placeholder='选填'></label>
              <div class='form-actions'><button class='primary' type='submit'>{'保存修改' if cost_edit else '新增区间'}</button>{"<a class='button' href='/rules#cost-rules'>取消</a>" if cost_edit else ''}</div>
            </form>"""
            supplier_form = f"""
            <form class='rule-form supplier-rule-form' method='post' action='/rules/supplier'>
              <input type='hidden' name='rule_id' value='{field(supplier_edit, 'id')}'>
              <label><span>适用季节</span><input name='season_year' value='{field(supplier_edit, 'season_year')}' placeholder='留空为默认'></label>
              <label><span>供应商</span><input name='supplier' value='{field(supplier_edit, 'supplier')}' placeholder='供应商名称' required></label>
              <label><span>浮动系数</span><input name='coefficient' value='{field(supplier_edit, 'coefficient')}' type='number' min='0.01' step='0.01' placeholder='例如 1.00' required></label>
              <label><span>备注</span><input name='note' value='{field(supplier_edit, 'note')}' placeholder='选填'></label>
              <div class='form-actions'><button class='primary' type='submit'>{'保存修改' if supplier_edit else '新增规则'}</button>{"<a class='button' href='/rules#supplier-rules'>取消</a>" if supplier_edit else ''}</div>
            </form>"""

        access_text = (
            "当前账号具有规则维护权限，可以新增、编辑和删除全部规则及业务选项。"
            if can_manage
            else "当前账号为只读权限，可以查看规则但不能新增、编辑或删除。"
        )
        access_class = "access-write" if can_manage else "access-readonly"
        content = f"""
        <section class='page-heading'><div><div class='eyebrow'>RULES & ASSUMPTIONS</div><h1>规则</h1><p>统一维护品类、渠道和定价计算规则。定价规则调整后只影响新生成或初审时重新测算的价格。</p></div></section>
        {self.alert(query.get('notice', ''), 'success') if query.get('notice') else ''}
        <section class='rule-access {access_class}'><div><strong>{'规则维护账号' if can_manage else '规则只读账号'}</strong><span>{access_text}</span></div><small>维护权限：企划管理员</small></section>
        <section class='rule-logic'><div><span>01</span><strong>商品名判定品类</strong><p>同步时按品类匹配词自动判定，初审人员可以确认或修改。</p></div><div><span>02</span><strong>规则计算</strong><p>连衣裙使用固定倍率，其余品类按照含税成本区间匹配倍率。</p></div><div><span>03</span><strong>渠道划分</strong><p>渠道选项提前维护，由商品部初审人员人工选择。</p></div></section>
        <section class='panel' id='category-options'><div class='panel-head'><div><div class='eyebrow'>CATEGORY OPTIONS</div><h2>品类选项</h2></div><span class='hint'>用于自动判定与初审选择</span></div>{category_option_form}<p class='range-help'>系统按商品名称匹配关键词，多个关键词可用逗号或换行分隔；同时命中时优先采用更长的关键词。“其他品类”可作为未命中时的默认选项。</p><div class='table-wrap'><table><thead><tr><th>品类</th><th>商品名匹配词</th><th>排序</th><th>备注</th>{'<th>操作</th>' if can_manage else ''}</tr></thead><tbody>{category_option_rows or f'<tr><td colspan="{5 if can_manage else 4}" class="empty">尚未配置品类选项。</td></tr>'}</tbody></table></div></section>
        <section class='panel' id='channel-options'><div class='panel-head'><div><div class='eyebrow'>CHANNEL OPTIONS</div><h2>渠道选项</h2></div><span class='hint'>由商品部初审人员人工判断</span></div>{channel_option_form}<div class='table-wrap'><table><thead><tr><th>渠道</th><th>排序</th><th>备注</th>{'<th>操作</th>' if can_manage else ''}</tr></thead><tbody>{channel_option_rows or f'<tr><td colspan="{4 if can_manage else 3}" class="empty">尚未配置渠道选项。</td></tr>'}</tbody></table></div></section>
        <section class='panel' id='dress-rules'><div class='panel-head'><div><div class='eyebrow'>DRESS</div><h2>连衣裙固定倍率</h2></div><span class='hint'>不区分成本金额</span></div>{dress_form}<div class='table-wrap'><table><thead><tr><th>适用季节</th><th>固定倍率</th><th>备注</th>{'<th>操作</th>' if can_manage else ''}</tr></thead><tbody>{dress_rows or f'<tr><td colspan="{4 if can_manage else 3}" class="empty">尚未配置连衣裙固定倍率。</td></tr>'}</tbody></table></div></section>
        <section class='panel' id='cost-rules'><div class='panel-head'><div><div class='eyebrow'>OTHER CATEGORIES</div><h2>其他品类成本区间倍率</h2></div><span class='hint'>下限包含，上限不包含</span></div>{cost_form}<p class='range-help'>可按实际业务新增任意数量的成本区间，也可随时编辑区间边界和倍率。上限填 600 表示成本小于 600；最后一档可不填上限。同一季节的区间不能重叠。</p><div class='table-wrap'><table><thead><tr><th>适用季节</th><th>含税成本区间</th><th>倍率</th><th>备注</th>{'<th>操作</th>' if can_manage else ''}</tr></thead><tbody>{cost_rows or f'<tr><td colspan="{5 if can_manage else 4}" class="empty">尚未配置其他品类成本区间；未命中区间时不能生成测算上新价。</td></tr>'}</tbody></table></div></section>
        <section class='panel' id='supplier-rules'><div class='panel-head'><div><div class='eyebrow'>SUPPLIER ADJUSTMENT</div><h2>供应商浮动系数</h2></div><span class='hint'>未配置时为 1.00</span></div>{supplier_form}<div class='table-wrap'><table><thead><tr><th>适用季节</th><th>供应商</th><th>系数</th><th>备注</th>{'<th>操作</th>' if can_manage else ''}</tr></thead><tbody>{supplier_rows or f'<tr><td colspan="{5 if can_manage else 4}" class="empty">尚未配置，系统默认使用 1.00。</td></tr>'}</tbody></table></div></section>
        """
        return self.shell("规则", content, user, "rules")

    def cost_range_label(self, lower, upper) -> str:
        if lower is None:
            return f"小于 {float(upper):g}"
        if upper is None:
            return f"{float(lower):g} 及以上"
        return f"{float(lower):g} ≤ 成本 < {float(upper):g}"

    def render_stats(self, user: dict, query: dict) -> str:
        season = query.get("season_year", "")
        category = query.get("category", "")
        stats = db.pricing_stats(self.db_path, season, category)
        records = db.list_pricing_records(self.db_path, season_year=season)
        total = sum(item["count"] for item in stats)
        seasons = sorted({item.get("season_year", "") for item in records if item.get("season_year")}, reverse=True)
        bars = ''.join(f"<div class='band-row'><div class='band-label'><span>{html.escape(item['label'])}</span><strong>{item['count']} 款 · {item['share']:.1f}%</strong></div><div class='bar'><i style='width:{min(100, item['share'])}%'></i></div></div>" for item in stats)
        content = f"""
        <section class='page-heading'><div><div class='eyebrow'>PRICE ARCHITECTURE</div><h1>价格带统计</h1><p>统计口径为已确认或已发布的款式数，未定价商品不计入占比。</p></div></section>
        <section class='filter-bar'><form method='get' action='/stats'><label>年份季节<select name='season_year'><option value=''>全部季节</option>{''.join(f"<option {'selected' if value == season else ''}>{html.escape(value)}</option>" for value in seasons)}</select></label><label>品类<input name='category' value='{html.escape(category)}' placeholder='全部品类'></label><button type='submit'>刷新统计</button></form></section>
        <section class='metrics'><div><span>统计款式</span><strong>{total}</strong><small>已确认 / 已发布</small></div><div><span>最低价格带</span><strong>{stats[0]['count'] if stats else 0}</strong><small>300 及以下</small></div><div><span>最高价格带</span><strong>{stats[-1]['count'] if stats else 0}</strong><small>1201 以上</small></div></section>
        <section class='panel'><div class='panel-head'><div><div class='eyebrow'>CURRENT MIX</div><h2>价格带分布</h2></div></div>{bars or '<p class="empty">暂无已确认定价。</p>'}</section>
        """
        return self.shell("价格带统计", content, user, "stats")

    def render_settings(self, user: dict) -> str:
        content = f"<section class='page-heading'><div><div class='eyebrow'>CONNECTION</div><h1>连接设置</h1><p>商品企划中心通过藏宝阁专用内部接口读取资料和发布价格。</p></div></section><section class='panel settings'><dl><dt>藏宝阁接口地址</dt><dd>{html.escape(self.catalog_api_url)}</dd><dt>内部 Token</dt><dd>{'已配置（启动时注入）' if self.catalog_api_token else '未配置'}</dd><dt>本地数据库</dt><dd>{html.escape(self.db_path)}</dd></dl><p class='muted'>Token 不写入数据库或页面。正式部署时请通过服务环境变量注入，并限制接口仅监听公司内网。</p></section>"
        return self.shell("连接设置", content, user, "settings")

    def record_status_label(self, status: str) -> str:
        return {"suggested": "待初审", "review_pending": "待复核", "confirmed": "复核通过，待回传", "published": "已回传", "conflict": "版本冲突", "failed": "回传失败"}.get(status, status)

    def page(self, title: str, content: str, user: dict | None) -> str:
        body_class = "app-body" if user else "login-body"
        return f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'><title>{html.escape(title)}</title><style>{self.css()}</style></head><body class='{body_class}'>{content}</body></html>"

    def shell(self, title: str, content: str, user: dict, current: str) -> str:
        nav = ''.join(f"<a class='{'active' if current == key else ''}' href='{href}'>{label}</a>" for key, href, label in [("dashboard", "/dashboard", "企划总览"), ("category-planning", "/category-planning", "品类企划"), ("workbench", "/workbench", "上新定价"), ("rules", "/rules", "规则"), ("stats", "/stats", "价格带统计"), ("settings", "/settings", "连接设置")])
        content = f"<div class='app-shell'><header><a class='brand' href='/dashboard'><span>PC</span><div><strong>商品企划中心</strong><small>Merchandise Planning</small></div></a><nav>{nav}</nav><div class='user'><span>{html.escape(user.get('display_name',''))}</span><form method='post' action='/logout'><button type='submit' aria-label='退出登录'>退出</button></form></div></header><main class='main'>{content}</main><footer>商品企划中心 · 成本来源：藏宝阁跟单部含税价</footer></div>"
        return self.page(title, content, user)

    def render_message(self, title: str, message: str) -> str:
        return f"<main class='login'><div class='login-mark'>PC / 商品企划中心</div><h1>{html.escape(title)}</h1><p class='muted'>{html.escape(message)}</p><a class='button primary' href='/dashboard'>返回总览</a></main>"

    def alert(self, message: str, kind: str) -> str:
        return f"<div class='alert {kind}'>{html.escape(message)}</div>"

    def q(self, value: str) -> str:
        return urlencode({"notice": value})[7:]

    def redirect(self, start_response, location: str):
        start_response("302 Found", [("Location", location)])
        return [b""]

    def html_response(self, start_response, body: str, status: str = "200 OK"):
        data = body.encode("utf-8")
        start_response(status, [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(data)))])
        return [data]

    def json_response(self, start_response, payload: dict, status: str = "200 OK"):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        start_response(status, [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(data)))])
        return [data]

    def css(self) -> str:
        return """
        .rule-access{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:13px 17px;border:1px solid;margin-bottom:16px}.rule-access div{display:flex;align-items:baseline;gap:12px}.rule-access span,.rule-access small{font-size:12px}.rule-access.access-write{background:#eaf3ed;border-color:#c7d9cc;color:#315447}.rule-access.access-readonly{background:#f3f1ee;border-color:#ded8d1;color:#6f6259}.rule-logic{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));background:#fff;border:1px solid var(--line);margin-bottom:24px}.rule-logic>div{padding:20px 22px;border-right:1px solid var(--line)}.rule-logic>div:last-child{border-right:0}.rule-logic span{color:var(--accent);font:21px Georgia,serif}.rule-logic strong{display:block;font-size:17px;margin:4px 0}.rule-logic p,.range-help{margin:0;color:var(--muted);font-size:12px}.rule-form{align-items:end}.rule-form.dress-rule-form{grid-template-columns:1fr .7fr 1.5fr auto}.rule-form.cost-rule-form{grid-template-columns:1fr 1fr 1fr .7fr 1.2fr auto}.range-help{margin:-5px 0 18px}.form-actions,.rule-actions{display:flex;align-items:center;gap:6px;white-space:nowrap}.form-actions button,.form-actions .button{height:42px}.compact-button,.danger-button{padding:5px 9px;font-size:12px}.danger-button{color:#9b3e32;border-color:#efc9c2}.rule-actions form{margin:0}.workbench-summary{display:flex;align-items:baseline;gap:12px;margin:0 0 18px;color:var(--muted)}.workbench-summary strong{font:24px Georgia,serif;color:var(--deep)}.workbench-summary small{font-size:12px}.product-card-list{display:grid;gap:18px}.product-card{background:#fff;border:1px solid var(--line);padding:22px;min-width:0}.product-card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:18px}.product-card-head h2{font:25px Georgia,serif;font-weight:500;margin:4px 0 0}.source-version{color:var(--muted);font-size:12px}.source-fields{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border-top:1px solid var(--line);border-left:1px solid var(--line);margin-bottom:18px}.source-field{min-width:0;min-height:82px;padding:12px 14px;border-right:1px solid var(--line);border-bottom:1px solid var(--line)}.source-field span{display:block;color:var(--muted);font-size:11px;margin-bottom:5px}.source-field strong{display:block;overflow-wrap:anywhere}.source-field-image{grid-row:span 2}.source-field-image img,.product-image-empty{display:block;width:82px;height:82px;object-fit:cover;background:#f0f3f0;border:1px solid var(--line)}.product-image-empty{display:grid;place-items:center;color:var(--muted);font-size:11px;text-align:center}.cost-value{color:var(--deep)}.pricing-panel{border:1px solid #cfdcd1;background:#f7faf7;padding:18px}.pricing-panel-empty{background:#fbfaf8;border-color:var(--line)}.pricing-panel-head{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:12px}.pricing-panel h3{font-size:18px;margin:4px 0}.pricing-panel-head p{margin:0;color:var(--muted);font-size:12px}.price-result{display:flex;align-items:baseline;gap:14px;margin:12px 0}.price-result span,.price-result small{color:var(--muted);font-size:12px}.price-result strong{font:30px Georgia,serif;color:var(--deep)}.price-result small{margin-left:auto}.pricing-calc-form,.price-review-form{display:flex;align-items:end;gap:9px;flex-wrap:wrap}.pricing-calc-form label,.price-review-form label{display:flex;flex-direction:column;gap:4px;color:var(--muted);font-size:12px}.pricing-calc-form input{min-width:220px}.price-review-form input{width:150px}.pricing-panel small{color:var(--muted);font-size:12px}.pricing-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.review-controls{display:flex;align-items:end;gap:14px;flex-wrap:wrap}.review-note{color:var(--muted);font-size:12px}.status-waiting{background:#f0f1ef;color:#68736a}.status-suggested{background:#fff5e8;color:#9b5a1b}.status-review-pending{background:#fff1e7;color:#98431d}.status-confirmed{background:#e5f2e9;color:#2d6b42}.status-published{background:#dfeee5;color:#276641}.status-conflict{background:#fff0ef;color:#a23f35}.status-source{background:#eef2ee;color:#526257}.error-text{display:block;margin-top:9px;background:#fff0ef;color:#a23f35;padding:5px 7px}@media(max-width:900px){.rule-logic{grid-template-columns:1fr}.rule-logic>div{border-right:0;border-bottom:1px solid var(--line)}.rule-logic>div:last-child{border-bottom:0}.rule-form.dress-rule-form,.rule-form.cost-rule-form{grid-template-columns:1fr 1fr}.rule-form.dress-rule-form .form-actions,.rule-form.cost-rule-form .form-actions{grid-column:span 2}.source-fields{grid-template-columns:repeat(2,minmax(0,1fr))}.source-field-image{grid-row:span 2}}@media(max-width:620px){.rule-access,.rule-access div{align-items:flex-start;flex-direction:column;gap:3px}.rule-form.dress-rule-form,.rule-form.cost-rule-form{grid-template-columns:1fr}.rule-form.dress-rule-form .form-actions,.rule-form.cost-rule-form .form-actions{grid-column:auto}.product-card{padding:16px}.product-card-head{gap:10px}.product-card-head h2{font-size:21px}.source-fields{grid-template-columns:1fr 1fr}.source-field{padding:10px}.source-field-image{grid-row:span 2}.pricing-panel-head,.review-controls{flex-direction:column;align-items:flex-start}.price-result{flex-wrap:wrap}.price-result small{margin-left:0}.pricing-calc-form,.price-review-form{align-items:stretch;flex-direction:column}.pricing-calc-form label,.price-review-form label,.pricing-calc-form input,.price-review-form input,.pricing-calc-form button,.price-review-form button{width:100%;min-width:0}.form-actions{flex-wrap:wrap}}
        .module-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px;margin-bottom:34px}.module-entry{min-width:0;min-height:252px;background:#fff;border:1px solid var(--line);border-top:3px solid var(--deep);padding:25px;display:flex;flex-direction:column;align-items:flex-start;justify-content:space-between;gap:24px}.module-entry-planned{border-top-color:#9a8172;background:#fbfaf8}.module-entry-top{width:100%;display:flex;justify-content:space-between;align-items:center}.module-index{font:29px Georgia,serif;color:#9aa19c}.phase-tag,.phase-badge{display:inline-block;background:#eeeae6;color:#745e51;padding:4px 9px;border-radius:3px;font-size:12px}.phase-tag-live{background:#e5f2e9;color:#2d6b42}.module-entry h2{font:28px Georgia,serif;font-weight:500;margin:5px 0 7px}.module-entry p{color:var(--muted);margin:0}.section-label{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;margin-bottom:12px}.section-label h2{font-size:20px;margin:2px 0 0}.section-label>a{color:var(--deep);font-size:13px}.planning-scope{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));background:#fff;border:1px solid var(--line);margin-bottom:24px}.planning-scope>div{min-width:0;padding:28px;border-right:1px solid var(--line);display:flex;gap:18px}.planning-scope>div:last-child{border-right:0}.scope-primary{background:#f0f5f1}.scope-number{font:25px Georgia,serif;color:var(--accent)}.planning-scope div div>span{display:block;color:var(--muted);font-size:12px}.planning-scope strong{display:block;font:24px Georgia,serif;margin:3px 0 7px}.planning-scope p{margin:0;color:var(--muted)}.phase-panel{display:flex;align-items:center;justify-content:space-between;gap:28px;background:#fbfaf8;border-left:3px solid #9a8172}.phase-panel>div{max-width:760px}.phase-panel p{margin:5px 0 0;color:var(--muted)}.phase-badge{font-size:13px;padding:7px 12px}@media(max-width:900px){.planning-scope{grid-template-columns:1fr}.planning-scope>div{border-right:0;border-bottom:1px solid var(--line)}.planning-scope>div:last-child{border-bottom:0}}@media(max-width:620px){.module-grid{grid-template-columns:1fr;gap:14px}.module-entry{min-height:220px;padding:20px}.section-label,.phase-panel{align-items:flex-start;flex-direction:column}.planning-scope>div{padding:21px}}
        :root{--ink:#202421;--muted:#6c756e;--line:#dde3dc;--paper:#f6f8f5;--card:#fff;--accent:#b5572a;--deep:#315447;--soft:#eaf0eb}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}a{color:inherit;text-decoration:none}button,.button{border:1px solid #cbd5ce;background:#fff;color:var(--ink);border-radius:4px;padding:9px 14px;font:inherit;cursor:pointer}button:hover,.button:hover{border-color:var(--accent);color:var(--accent)}button.primary,.button.primary{background:var(--deep);border-color:var(--deep);color:#fff}button:disabled{cursor:not-allowed;opacity:.45}.app-shell{width:100%;min-width:0;min-height:100vh}header{width:100%;height:72px;background:#fff;border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 34px;gap:30px;position:sticky;top:0;z-index:3}.brand{display:flex;align-items:center;gap:10px;min-width:230px}.brand>span{display:grid;place-items:center;width:34px;height:34px;background:var(--deep);color:#fff;font-weight:700;letter-spacing:.08em;border-radius:3px}.brand strong{display:block;font-size:15px}.brand small{display:block;color:var(--muted);font-size:10px;letter-spacing:.08em;text-transform:uppercase}nav{display:flex;gap:4px;flex:1;min-width:0}nav a{padding:9px 12px;color:var(--muted);border-bottom:2px solid transparent}nav a.active,nav a:hover{color:var(--deep);border-bottom-color:var(--accent)}.user{display:flex;align-items:center;gap:13px;color:var(--muted);white-space:nowrap}.user button{padding:5px 9px}.main{width:100%;min-width:0;max-width:1320px;margin:0 auto;padding:38px 34px 60px}.hero,.page-heading{display:flex;align-items:flex-end;justify-content:space-between;gap:28px;margin-bottom:25px}.hero h1,.page-heading h1{font-family:Georgia,'Times New Roman',serif;font-weight:500;font-size:42px;line-height:1.15;margin:5px 0 10px;letter-spacing:0}.hero p,.page-heading p{margin:0;color:var(--muted);max-width:680px}.eyebrow{color:var(--accent);font-size:11px;letter-spacing:.14em;font-weight:700}.hero-note{background:var(--deep);color:#fff;padding:18px 22px;min-width:190px}.hero-note span,.hero-note small{display:block;opacity:.7;font-size:12px}.hero-note strong{display:block;font-size:21px;margin:4px 0}.metrics{display:grid;grid-template-columns:repeat(3,1fr);background:#fff;border:1px solid var(--line);margin-bottom:24px}.metrics>a,.metrics>div{padding:20px 24px;border-right:1px solid var(--line)}.metrics>*:last-child{border-right:0}.metrics span,.metrics small{display:block;color:var(--muted)}.metrics strong{display:block;font:34px Georgia,serif;margin:4px 0}.split{display:grid;grid-template-columns:1.45fr 1fr;gap:24px}.panel{min-width:0;background:var(--card);border:1px solid var(--line);padding:24px;margin-bottom:24px}.panel-head{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:18px}.panel h2{font-size:20px;font-weight:600;margin:2px 0}.hint,.count,.muted,.meta{color:var(--muted)}.count{font-size:13px}.quick-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.quick-grid a{border:1px solid var(--line);padding:17px;min-height:126px;display:flex;flex-direction:column;gap:3px}.quick-grid a:hover{border-color:var(--accent);background:#fffaf7}.quick-grid b{color:var(--accent);font:23px Georgia,serif}.quick-grid small{color:var(--muted);font-size:12px}.notice-panel{background:#f0f5f1}.notice-panel p{color:#53645a}.page-heading form{margin-bottom:4px}.filter-bar{background:#fff;border:1px solid var(--line);padding:14px 18px;margin-bottom:24px}.filter-bar form{display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap}.filter-bar label,.rule-form label{display:flex;flex-direction:column;gap:4px;color:var(--muted);font-size:12px}.filter-bar input,.filter-bar select{min-width:190px}input,select{border:1px solid #cfd8d1;background:#fff;padding:9px 10px;border-radius:3px;color:var(--ink);font:inherit;min-width:0}table{width:100%;border-collapse:collapse}th{text-align:left;color:var(--muted);font-size:12px;font-weight:500;background:#f7f9f7}th,td{padding:12px 11px;border-bottom:1px solid var(--line);vertical-align:middle}tbody tr:last-child td{border-bottom:0}td strong{display:block}td small{display:block;color:var(--muted);font-size:11px}.table-wrap{width:100%;max-width:100%;overflow:auto}.inline-form{display:flex;gap:6px;min-width:205px}.inline-form input{width:120px}.inline-form button{padding:7px 10px;white-space:nowrap}.status{display:inline-block;border-radius:3px;padding:3px 7px;background:var(--soft);color:var(--deep);font-size:12px}.status-confirmed{background:#fff1e7;color:#98431d}.status-published{background:#e5f2e9;color:#2d6b42}.status-conflict,.error-text{background:#fff0ef;color:#a23f35}.price{font:20px Georgia,serif;color:var(--deep)}.actions{display:flex;gap:5px;white-space:nowrap}.actions button{padding:6px 9px;font-size:12px}.empty{text-align:center;color:var(--muted);padding:30px!important}.alert{padding:11px 14px;border:1px solid;margin:0 0 20px}.alert.success{background:#edf7ef;border-color:#c8e2cd;color:#2f6741}.alert.error{background:#fff1ef;border-color:#efc9c2;color:#9b3e32}.rule-form{display:grid;grid-template-columns:1fr 1fr .7fr 1.2fr auto;gap:7px;margin-bottom:20px}.band-row{margin:19px 0}.band-label{display:flex;justify-content:space-between;gap:15px;margin-bottom:6px}.band-label span{font-weight:600}.band-label strong{color:var(--muted);font-size:13px;font-weight:500}.bar{height:11px;background:#edf1ed}.bar i{display:block;height:100%;background:var(--accent)}.settings dl{display:grid;grid-template-columns:180px 1fr;border-top:1px solid var(--line)}.settings dt,.settings dd{padding:13px 0;margin:0;border-bottom:1px solid var(--line)}.settings dt{color:var(--muted)}footer{max-width:1320px;margin:0 auto;padding:0 34px 25px;color:#909890;font-size:12px}.login-body{min-height:100vh;display:grid;place-items:center;background:#eef2ee}.login{width:min(430px,calc(100% - 36px));background:#fff;border:1px solid var(--line);padding:38px}.login-mark{color:var(--accent);font-size:12px;letter-spacing:.12em;font-weight:700}.login h1{font:36px Georgia,serif;margin:15px 0 8px}.login form{margin-top:25px}.login label{display:block;color:var(--muted);font-size:12px;margin:14px 0}.login input{width:100%;margin-top:5px}.login button{width:100%;margin-top:12px}.button{display:inline-block}.login .button{margin-top:18px} @media(max-width:900px){header{padding:0 18px;gap:16px}.brand{min-width:auto}.brand small,nav a{font-size:12px}nav{overflow:auto}.user>span{display:none}.main{padding:28px 18px 45px}.hero,.page-heading{align-items:flex-start;flex-direction:column}.hero h1,.page-heading h1{font-size:35px}.split{grid-template-columns:1fr}.rule-form{grid-template-columns:1fr 1fr}.rule-form button{grid-column:span 2}.quick-grid{grid-template-columns:1fr 1fr}}@media(max-width:620px){header{height:auto;min-height:66px;flex-wrap:wrap;padding:12px 15px}nav{order:3;flex:0 0 100%;width:100%;max-width:100%;overflow-x:auto}.metrics{grid-template-columns:1fr}.metrics>*{border-right:0;border-bottom:1px solid var(--line)}.metrics>*:last-child{border-bottom:0}.quick-grid{grid-template-columns:1fr}.rule-form{grid-template-columns:1fr}.rule-form button{grid-column:auto}.filter-bar form{align-items:stretch;flex-direction:column}.filter-bar label,.filter-bar input,.filter-bar select,.filter-bar button{width:100%}.filter-bar input,.filter-bar select{min-width:0}.panel{padding:18px}.main{padding-left:13px;padding-right:13px}.hero h1,.page-heading h1{font-size:30px}.actions{flex-direction:column}.settings dl{grid-template-columns:1fr}.settings dt{border-bottom:0;padding-bottom:3px}.settings dd{padding-top:0}.login{padding:28px 22px}}
        .pricing-board{padding:0;overflow:hidden}.pricing-board>.panel-head{padding:22px 24px 0}.pricing-board>.panel-head h2{margin-bottom:3px}.pricing-board>.panel-head p{margin:0}.pricing-batch-toolbar{display:flex;align-items:center;gap:14px;min-height:58px;padding:10px 24px;border-top:1px solid var(--line);background:#f7f9f7}.pricing-batch-toolbar>label{display:flex;align-items:center;gap:7px;font-weight:600;white-space:nowrap}.pricing-batch-toolbar input,.pricing-select-cell input{width:16px;height:16px;margin:0;accent-color:var(--deep)}.pricing-batch-toolbar>span{color:var(--muted);font-size:12px;white-space:nowrap}.pricing-batch-actions{display:flex;align-items:center;justify-content:flex-end;gap:8px;margin-left:auto}.pricing-batch-actions button{padding:7px 11px;font-size:12px;white-space:nowrap}.visually-hidden{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}.pricing-table-wrap{position:relative;overflow-x:auto}.pricing-table{min-width:1454px}.pricing-table tr[id]{scroll-margin-top:92px}.pricing-table th{white-space:nowrap}.pricing-table th,.pricing-table td{padding:12px 10px}.pricing-table tbody tr:hover{background:#fbfcfb}.pricing-table tbody tr:hover .pricing-select-cell{background:#fbfcfb}.pricing-table td{min-width:92px}.pricing-table .pricing-select-cell{position:sticky;left:0;z-index:1;min-width:44px;width:44px;text-align:center;padding-left:8px;padding-right:8px;background:#fff;box-shadow:1px 0 0 var(--line)}.pricing-table thead .pricing-select-cell{z-index:2;background:#f7f9f7}.pricing-table td:nth-child(2){min-width:105px}.pricing-table td:nth-child(3){min-width:88px}.pricing-table td:nth-child(4){min-width:92px}.pricing-table td:nth-child(5){min-width:78px}.pricing-table td:nth-child(6){min-width:130px}.pricing-table td:nth-child(7){min-width:110px}.pricing-table td:nth-child(8){min-width:88px}.pricing-table td:nth-child(9){min-width:112px}.pricing-table td:nth-child(10){min-width:235px}.pricing-table td:nth-child(11){min-width:112px}.pricing-table td:nth-child(12){min-width:130px}.pricing-table td:nth-child(13){min-width:270px}.pricing-table .image-cell img,.pricing-table .product-image-empty{width:56px;height:56px}.pricing-table .pricing-match-cell>small{margin-top:3px}.pricing-table .price-cell small{margin-top:3px}.pricing-table .pricing-action-cell{vertical-align:middle}.pricing-table .pricing-action-cell form{margin:0 0 7px}.pricing-table .pricing-action-cell>form:only-child{margin-bottom:0}.pricing-table .pricing-action-cell label{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px;white-space:nowrap}.pricing-table .pricing-action-cell input{width:110px;padding:7px 8px}.pricing-table .pricing-action-cell button{padding:7px 9px;font-size:12px;white-space:nowrap}.pricing-table .pricing-action-cell small{max-width:245px}.pricing-table .pricing-calc-form{display:flex;align-items:end;gap:6px;flex-wrap:wrap}.pricing-table .pricing-calc-form label{display:flex;flex-direction:column;align-items:stretch;gap:3px;color:var(--muted);font-size:12px;white-space:normal}.pricing-table .pricing-calc-form input{width:116px}.pricing-table .review-controls{display:flex;align-items:flex-start;gap:8px;flex-wrap:wrap}.pricing-table .review-controls>span{width:100%}.pricing-table .review-approval-form{display:grid;grid-template-columns:max-content max-content;align-items:end;gap:7px 9px}.pricing-table .review-approval-form select{width:110px;padding:7px 8px}.pricing-table .review-approval-form button{width:auto;min-width:0}.pricing-table .review-approve-button{grid-column:2;justify-self:end}
        .rule-form.option-rule-form{grid-template-columns:1fr 1.4fr .55fr 1.2fr auto}.pricing-table{min-width:1690px}.pricing-table td:nth-child(10){min-width:170px}.pricing-table td:nth-child(11){min-width:180px}.pricing-table td:nth-child(12){min-width:135px}.pricing-table td:nth-child(13){min-width:112px}.pricing-table td:nth-child(14){min-width:130px}.pricing-table td:nth-child(15){min-width:270px}.pricing-table .pricing-category-cell select,.pricing-table .pricing-channel-cell select,.pricing-table .pricing-calc-form select{width:145px;padding:7px 8px}.pricing-table .category-review-control{display:flex;align-items:center;gap:7px;flex-wrap:wrap}.pricing-table .category-review-control select[hidden]{display:none}.pricing-table .category-edit-button{padding:5px 8px;font-size:12px}.pricing-table .cell-field{display:flex;flex-direction:column;gap:3px;color:var(--muted);font-size:12px}.pricing-table .pricing-rule-cell .rule-summary{display:block;white-space:nowrap}.pricing-table .pricing-rule-cell .recalculate-button{display:block;margin-top:8px;white-space:nowrap}.pricing-table .pricing-category-cell small,.pricing-table .pricing-rule-cell small{margin-top:4px}
        @media(max-width:900px){.rule-form.option-rule-form{grid-template-columns:1fr 1fr}.rule-form.option-rule-form .form-actions{grid-column:span 2}}
        @media(max-width:620px){.rule-form.option-rule-form{grid-template-columns:1fr}.rule-form.option-rule-form .form-actions{grid-column:auto}.pricing-board>.panel-head{padding:18px 18px 0;align-items:flex-start}.pricing-batch-toolbar{align-items:flex-start;flex-wrap:wrap;padding:10px 18px}.pricing-batch-actions{width:100%;justify-content:flex-start;margin-left:0;overflow-x:auto}.pricing-table{min-width:1690px}.pricing-table th,.pricing-table td{padding:10px 9px}.pricing-table .pricing-action-cell input{width:102px}.pricing-table .pricing-action-cell button{font-size:11px;padding:6px 8px}}
        .pricing-table .pricing-image-cell{position:sticky;left:44px;z-index:1;min-width:78px;width:78px;padding-left:10px;padding-right:10px;background:#fff;box-shadow:1px 0 0 var(--line)}.pricing-table thead th:nth-child(2){position:sticky;left:44px;z-index:3;min-width:78px;width:78px;background:#f7f9f7;box-shadow:1px 0 0 var(--line)}.pricing-table tbody tr:hover .pricing-image-cell{background:#fbfcfb}.pricing-table td:nth-child(2){min-width:78px;width:78px}.pricing-table td:nth-child(3){min-width:105px}.pricing-table td:nth-child(4){min-width:88px}.pricing-table td:nth-child(5){min-width:92px}.pricing-table td:nth-child(6){min-width:130px}.pricing-table td:nth-child(7){min-width:110px}.pricing-table td:nth-child(8){min-width:88px}.pricing-table td:nth-child(9){min-width:112px}.pricing-table td:nth-child(10){min-width:170px}.pricing-table td:nth-child(11){min-width:180px}.pricing-table td:nth-child(12){min-width:112px}.pricing-table td:nth-child(13){min-width:135px}.pricing-table td:nth-child(14){min-width:270px}.pricing-table .pricing-workflow-cell{vertical-align:middle}.pricing-table .workflow-status{margin-bottom:8px}.pricing-table .workflow-actions form{margin:0 0 7px}.pricing-table .workflow-actions>form:only-child{margin-bottom:0}.pricing-table .workflow-actions label{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px;white-space:nowrap}.pricing-table .workflow-actions input{width:110px;padding:7px 8px}.pricing-table .workflow-actions button{padding:7px 9px;font-size:12px;white-space:nowrap}.pricing-table .workflow-actions small{max-width:245px}
        @media(max-width:620px){.pricing-table .workflow-actions input{width:102px}.pricing-table .workflow-actions button{font-size:11px;padding:6px 8px}}
        .pricing-board-tools{display:flex;align-items:center;gap:12px}.pricing-board-tools .compact-button{background:#fff}.pricing-table[data-resizable-columns]{table-layout:fixed;width:max-content;min-width:0;--select-column-width:44px;--image-column-width:78px}.pricing-table[data-resizable-columns] col{width:auto}.pricing-table[data-resizable-columns] th,.pricing-table[data-resizable-columns] td{width:auto;min-width:0!important;overflow-wrap:anywhere}.pricing-table[data-resizable-columns] .pricing-select-cell{width:var(--select-column-width)}.pricing-table[data-resizable-columns] .pricing-image-cell{left:var(--select-column-width);width:var(--image-column-width)}.pricing-table[data-resizable-columns] thead th:nth-child(2){left:var(--select-column-width);width:var(--image-column-width)}.pricing-table[data-resizable-columns] thead th:not(:first-child):not(:nth-child(2)){position:relative}.pricing-table .rule-summary{display:flex;flex-direction:column;gap:2px;white-space:normal}.pricing-table .rule-expression,.pricing-table .rule-raw-price{display:block;white-space:nowrap}.column-resize-handle{position:absolute;top:0;right:-4px;width:8px;height:100%;cursor:col-resize;touch-action:none;z-index:4}.column-resize-handle:hover,.column-resize-handle:focus-visible,.column-resizing .column-resize-handle{background:var(--accent);opacity:.6;outline:0}.column-resizing{cursor:col-resize!important;user-select:none}.column-resizing *{cursor:col-resize!important;user-select:none}
        @media(max-width:620px){.pricing-board-tools{align-items:flex-start;flex-direction:column;gap:6px}.pricing-table[data-resizable-columns]{min-width:0}}
        """
