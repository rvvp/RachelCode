from __future__ import annotations

import cgi
import html
import io
import json
import math
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from http import cookies
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

from catalog_backend import db
from catalog_backend.excel import (
    brand_bill_dashboard_workbook_bytes,
    brand_bill_template_bytes,
    dashboard_rows_from_brand_bill_summary,
    parse_brand_bill_workbook,
    parse_image_mapping_workbook,
    parse_supplier_bill_workbook,
    parse_supplier_master_workbook,
    parse_supplier_settlement_workbook,
    parse_workbook,
    supplier_settlement_template_bytes,
    supplier_settlement_workbook_bytes,
    supplier_bill_template_bytes,
    supplier_bill_workbook_bytes,
    supplier_master_template_bytes,
    workbook_bytes,
)
from catalog_backend.fields import CATALOG_EXPORT_FIELD_ORDER, FIELDS_BY_GROUP, FieldDef, PRODUCT_FIELDS, PRODUCT_FIELD_MAP
from catalog_backend.policies import (
    B_STAGE_FIELD_KEYS,
    BILLING_PLATFORM_OPTIONS,
    C_OPERATING_CHANNELS,
    available_lifecycle_actions,
    available_status_actions,
    can_access_billing_module,
    can_access_brand_bills,
    can_access_platform_bills,
    can_access_supplier_settlements,
    can_create_product,
    can_edit_product,
    can_import_product_excel,
    can_import_product_images,
    c_user_can_manage_platform_bill,
    can_manage_supplier_settlements,
    can_manage_users,
    can_manage_lifecycle,
    can_process_brand_bills,
    can_review_product,
    can_see_product,
    can_upload_platform_bills,
    billing_platform_label,
    can_view_logs,
    department_label,
    editable_field_keys_for_user,
    is_admin,
    is_department_monitor,
    is_executive_read_only,
    lifecycle_label,
    MANAGEABLE_DEPARTMENTS,
    normalize_launch_channel,
    normalize_billing_platform_codes,
    operating_channel_label,
    platform_bill_platform_codes_for_user,
    status_label,
    visible_fields_for_department,
    visible_fields_from_keys,
)
from catalog_backend.uploads import (
    generic_content_type,
    MEDIA_URL_PREFIX,
    MAX_IMAGE_BYTES,
    delete_generic_upload,
    delete_local_media,
    media_content_type,
    media_file_path,
    read_validated_file_upload,
    read_validated_file_uploads,
    read_validated_image_upload,
    read_validated_image_uploads,
    save_image_upload,
    save_generic_upload,
    upload_file_path,
)


SESSIONS: dict[str, int] = {}
LIST_LAYOUT_VIRTUAL_FIELDS: tuple[FieldDef, ...] = ()
LIST_LAYOUT_VIRTUAL_FIELD_MAP = {}
LIST_LAYOUT_HIDDEN_FIELD_KEYS = {
    "size_chart",
    "size_f",
    "size_s",
    "size_m",
    "size_l",
    "size_xl",
    "size_2xl",
    "size_3xl",
    "total_quantity",
}
BILLING_MONTH_STATUS_COLORS = {
    "draft": "#9f6b30",
    "partial_to_b": "#7f5d2a",
    "submitted_to_b": "#2f6f4f",
}


class CatalogApplication:
    def __init__(self, db_path: str | Path, upload_dir: str | Path, brand_config: dict | None = None):
        self.db_path = str(db_path)
        self.upload_dir = str(upload_dir)
        self.brand_config = self.build_brand_config(brand_config or {})

    def build_brand_config(self, overrides: dict) -> dict:
        config = {
            "brand_name": "思安娜的\n藏寶閣",
            "brand_mark": "Sianna",
            "brand_tagline": "让商品资料从分散表格进入统一底库",
            "brand_subtitle": "面向跟单部、商品部与运营部协作的内部资料后台，支持跟单部主体填写、商品部补充品类、图片、上新价格、上新渠道和资料完成，运营部只读开放与结构化调用。",
            "brand_eyebrow": "Siana Treasure Pavilion",
            "brand_console_eyebrow": "Siana Treasure Workspace",
            "accent": "#bc6c25",
            "accent_strong": "#7f3b08",
            "accent_deep": "#355f52",
        }
        for key, value in overrides.items():
            clean_value = str(value).strip().replace("\\n", "\n")
            if clean_value:
                config[key] = clean_value
        return config

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        query = {
            key: values[0]
            for key, values in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()
        }
        user = self.current_user(environ)
        monitor_department = str(query.get("monitor_department", "")).strip().upper()
        if user and is_admin(user):
            user = dict(user)
            user["monitor_path"] = path
            user["monitor_query"] = {
                key: value for key, value in query.items() if key != "monitor_department"
            }
            if method == "GET" and monitor_department in {"A", "B", "C"}:
                user = self.department_monitor_user(user, monitor_department, path, query)
        try:
            if path == "/":
                return self.redirect(start_response, "/modules" if user else "/login")
            if path == "/healthz" and method == "GET":
                return self.handle_healthz(start_response)
            if path == "/login":
                if method == "GET":
                    return self.html_response(start_response, self.render_login())
                form, _ = self.parse_form(environ)
                return self.handle_login(start_response, form)
            if path == "/logout" and method == "POST":
                return self.handle_logout(environ, start_response)
            if path == "/api/products" and method == "GET":
                return self.handle_api(environ, start_response, user, query)
            if not user:
                return self.redirect(start_response, "/login")
            if path == "/profile/password":
                if method == "GET":
                    return self.html_response(start_response, self.render_password_change_page(user))
                return self.handle_password_change(environ, start_response, user)
            if user.get("must_change_password"):
                return self.redirect(start_response, "/profile/password")
            if path == "/modules" and method == "GET":
                return self.html_response(
                    start_response,
                    self.render_modules_home(user),
                )
            if path == "/products" and method == "GET":
                direct_target = self.style_code_detail_redirect_target(user, query)
                if direct_target:
                    return self.redirect(start_response, direct_target)
                return self.html_response(
                    start_response,
                    self.render_products(user, query),
                )
            if path == "/billing" and method == "GET":
                return self.handle_billing_home(start_response, user, query)
            if path == "/billing/monthly-board" and method == "POST":
                return self.handle_supplier_monthly_board_save(environ, start_response, user)
            if path == "/billing/platform-bills" and method == "GET":
                return self.handle_platform_bills_page(start_response, user, query)
            if path == "/billing/platform-bills/upload" and method == "POST":
                return self.handle_platform_bill_upload(environ, start_response, user)
            if path == "/billing/platform-bills/delete" and method == "POST":
                return self.handle_platform_bill_delete(environ, start_response, user)
            if path == "/billing/platform-bills/submit" and method == "POST":
                return self.handle_platform_bill_submit(environ, start_response, user)
            if path == "/billing/platform-bills/return-request" and method == "POST":
                return self.handle_platform_bill_return_request(environ, start_response, user)
            if path == "/billing/platform-bills/return-decision" and method == "POST":
                return self.handle_platform_bill_return_decision(environ, start_response, user)
            if path == "/billing/platform-bills/platforms" and method == "POST":
                return self.handle_platform_bill_platforms_update(environ, start_response, user)
            if path == "/billing/brand-bills" and method == "GET":
                return self.handle_brand_bills_page(start_response, user, query)
            if path == "/billing/brand-bills/upload" and method == "POST":
                return self.handle_brand_bill_upload(environ, start_response, user)
            if path == "/billing/brand-bills/delete" and method == "POST":
                return self.handle_brand_bill_delete(environ, start_response, user)
            if path == "/billing/brand-bills/dashboard" and method == "POST":
                return self.handle_brand_bill_dashboard_update(environ, start_response, user)
            if path == "/billing/brand-bills/submit" and method == "POST":
                return self.handle_brand_bill_submit(environ, start_response, user)
            if path == "/billing/brand-bills/return-request" and method == "POST":
                return self.handle_brand_bill_return_request(environ, start_response, user)
            if path == "/billing/brand-bills/return-decision" and method == "POST":
                return self.handle_brand_bill_return_decision(environ, start_response, user)
            if path == "/billing/brand-bills/dashboard.xlsx" and method == "GET":
                return self.handle_brand_bill_dashboard_export(start_response, user, query)
            if path == "/billing/brand-bills/template.xlsx" and method == "GET":
                return self.handle_brand_bill_template(start_response, user)
            if path == "/billing/supplier-settlements" and method == "GET":
                return self.handle_supplier_settlements_page(start_response, user, query)
            if path == "/billing/supplier-settlements/suppliers" and method == "POST":
                return self.handle_supplier_master_upsert(environ, start_response, user)
            if path == "/billing/supplier-settlements/master/edit":
                if method == "GET":
                    return self.handle_supplier_master_edit_page(start_response, user, query)
                return self.handle_supplier_master_edit_save(environ, start_response, user)
            if path == "/billing/supplier-settlements/master/new" and method == "GET":
                return self.handle_supplier_master_form_page(start_response, user, query)
            if path == "/billing/supplier-settlements/master" and method == "POST":
                return self.handle_supplier_bill_master_save(environ, start_response, user)
            if path == "/billing/supplier-settlements/master/import" and method == "POST":
                return self.handle_supplier_master_import(environ, start_response, user)
            if path == "/billing/supplier-settlements/master/template.xlsx" and method == "GET":
                return self.handle_supplier_master_template(start_response, user)
            if path == "/billing/supplier-settlements/bills/import" and method == "POST":
                return self.handle_supplier_bill_import(environ, start_response, user)
            if path == "/billing/supplier-settlements/bills/delete" and method == "POST":
                return self.handle_supplier_bill_delete(environ, start_response, user)
            if path == "/billing/supplier-settlements/bills/export.xlsx" and method == "GET":
                return self.handle_supplier_bill_export(start_response, user, query)
            if path == "/billing/supplier-settlements/bills/template.xlsx" and method == "GET":
                return self.handle_supplier_bill_template(start_response, user)
            if path == "/billing/supplier-settlements/records" and method == "POST":
                return self.handle_supplier_settlement_upsert(environ, start_response, user)
            if path == "/billing/supplier-settlements/import" and method == "POST":
                return self.handle_supplier_settlement_import(environ, start_response, user)
            if path == "/billing/supplier-settlements/export.xlsx" and method == "GET":
                return self.handle_supplier_settlement_export(start_response, user, query)
            if path == "/billing/supplier-settlements/template.xlsx" and method == "GET":
                return self.handle_supplier_settlement_template(start_response, user)
            if path.startswith("/billing/platform-bills/files/") and method == "GET":
                return self.handle_platform_bill_download(start_response, user, path)
            if path.startswith("/billing/brand-bills/files/") and method == "GET":
                return self.handle_brand_bill_download(start_response, user, path)
            if path == "/products/bulk" and method == "POST":
                return self.handle_products_bulk(environ, start_response, user)
            if path == "/logs" and method == "GET":
                return self.handle_logs_center(start_response, user, query)
            if path == "/logs/export.csv" and method == "GET":
                return self.handle_logs_center_export(start_response, user, query)
            if path == "/products/new":
                if method == "GET":
                    return self.require_editor(start_response, user) or self.html_response(
                        start_response,
                        self.render_product_form(user, "/products/new", "新建商品资料", {}),
                    )
                return self.require_editor(start_response, user) or self.handle_product_create(
                    environ,
                    start_response,
                    user,
                )
            if path == "/import":
                if method == "GET":
                    return self.require_product_importer(start_response, user) or self.html_response(
                        start_response,
                        self.render_import_page(user),
                    )
                return self.require_product_importer(start_response, user) or self.handle_import(
                    environ,
                    start_response,
                    user,
                )
            if path == "/import-images":
                if method == "GET":
                    return self.require_image_importer(start_response, user) or self.html_response(
                        start_response,
                        self.render_image_import_page(user),
                    )
                return self.require_image_importer(start_response, user) or self.handle_image_import(
                    environ,
                    start_response,
                    user,
                )
            if path == "/export.xlsx" and method == "GET":
                return self.handle_export(start_response, user, query)
            if path == "/products/review" and method == "GET":
                return self.html_response(start_response, self.render_review_queue(user, query))
            if path == "/products/review/bulk" and method == "POST":
                return self.handle_review_bulk(environ, start_response, user)
            if path == "/users":
                if method == "GET":
                    return self.handle_users_page(start_response, user, query)
                return self.handle_user_create(environ, start_response, user)
            if path == "/settings/c-fields":
                if method == "GET":
                    return self.handle_c_field_settings_page(start_response, user, query)
                return self.handle_c_field_settings_update(environ, start_response, user)
            if path == "/settings/list-layout":
                if method == "GET":
                    return self.handle_list_layout_settings_page(start_response, user, query)
                return self.handle_list_layout_settings_update(environ, start_response, user)
            if path.startswith(MEDIA_URL_PREFIX) and method == "GET":
                return self.handle_media(start_response, user, path)

            parts = [segment for segment in path.strip("/").split("/") if segment]
            if len(parts) >= 2 and parts[0] == "products" and parts[1].isdigit():
                product_id = int(parts[1])
                if len(parts) == 2 and method == "GET":
                    return self.handle_product_detail(start_response, user, product_id, query)
                if len(parts) == 3 and parts[2] == "edit":
                    return self.handle_product_edit(environ, start_response, user, product_id)
                if len(parts) == 3 and parts[2] == "status" and method == "POST":
                    return self.handle_status_change(environ, start_response, user, product_id)
                if len(parts) == 3 and parts[2] == "lifecycle" and method == "POST":
                    return self.handle_lifecycle_change(environ, start_response, user, product_id)
                if len(parts) == 3 and parts[2] == "logs" and method == "GET":
                    return self.handle_product_logs(start_response, user, product_id, query)
                if len(parts) == 3 and parts[2] == "versions" and method == "GET":
                    return self.handle_product_versions(start_response, user, product_id, query)
                if len(parts) == 5 and parts[2] == "versions" and parts[3].isdigit() and parts[4] == "restore" and method == "POST":
                    return self.handle_product_version_restore(environ, start_response, user, product_id, int(parts[3]))
                if len(parts) == 4 and parts[2] == "logs" and parts[3] == "export.csv" and method == "GET":
                    return self.handle_product_logs_export(start_response, user, product_id, query)
            if len(parts) >= 2 and parts[0] == "users" and parts[1].isdigit():
                managed_user_id = int(parts[1])
                if len(parts) == 3 and parts[2] == "edit":
                    if method == "GET":
                        return self.handle_user_edit_page(start_response, user, managed_user_id)
                    return self.handle_user_update(environ, start_response, user, managed_user_id)
                if len(parts) == 3 and parts[2] == "toggle" and method == "POST":
                    return self.handle_user_toggle(environ, start_response, user, managed_user_id)
                if len(parts) == 3 and parts[2] == "reset-password" and method == "POST":
                    return self.handle_user_reset_password(environ, start_response, user, managed_user_id)

            return self.html_response(
                start_response,
                self.render_message_page("页面不存在", "没有找到你要访问的页面。", user),
                status="404 Not Found",
            )
        except ValueError as error:
            return self.html_response(
                start_response,
                self.render_message_page("操作失败", str(error), user),
                status="400 Bad Request",
            )

    def current_user(self, environ):
        raw_cookie = environ.get("HTTP_COOKIE", "")
        parsed_cookie = cookies.SimpleCookie(raw_cookie)
        session_cookie = parsed_cookie.get("session")
        if not session_cookie:
            return None
        user_id = SESSIONS.get(session_cookie.value)
        if not user_id:
            return None
        return db.get_user_by_id(self.db_path, user_id)

    def department_monitor_user(self, user: dict, department: str, path: str, query: dict) -> dict:
        """Build a read-only departmental view for an administrator without impersonation."""
        monitored_user = dict(user)
        monitored_user["department"] = department
        monitored_user["monitor_department"] = department
        monitored_user["monitor_path"] = path
        monitored_user["monitor_query"] = {
            key: value for key, value in query.items() if key != "monitor_department"
        }
        if department == "C":
            monitored_user["operating_channel"] = "all"
            monitored_user["billing_platforms_json"] = json.dumps(
                [code for code, _label in BILLING_PLATFORM_OPTIONS], ensure_ascii=False
            )
        return monitored_user

    def parse_form(self, environ):
        content_type = environ.get("CONTENT_TYPE", "")
        if content_type.startswith("multipart/form-data"):
            storage = cgi.FieldStorage(
                fp=environ["wsgi.input"],
                environ=environ,
                keep_blank_values=True,
            )
            form_data = {}
            files = {}
            for key in storage.keys():
                item = storage[key]
                items = item if isinstance(item, list) else [item]
                file_items = [entry for entry in items if getattr(entry, "filename", "")]
                if file_items:
                    files[key] = file_items if len(file_items) > 1 else file_items[0]
                    continue
                values = [entry.value for entry in items]
                form_data[key] = values if len(values) > 1 else values[0]
            return form_data, files
        content_length = int(environ.get("CONTENT_LENGTH") or "0")
        raw_body = environ["wsgi.input"].read(content_length).decode("utf-8")
        form_data = {}
        for key, values in parse_qs(raw_body, keep_blank_values=True).items():
            form_data[key] = values if len(values) > 1 else values[0]
        return form_data, {}

    def require_editor(self, start_response, user):
        if can_create_product(user):
            return None
        return self.html_response(
            start_response,
            self.render_message_page("权限不足", "当前账号只有查看和调用权限，不能录入或修改资料。", user),
            status="403 Forbidden",
        )

    def require_product_importer(self, start_response, user):
        if can_import_product_excel(user):
            return None
        return self.html_response(
            start_response,
            self.render_message_page("权限不足", "当前账号不能导入商品资料 Excel。", user),
            status="403 Forbidden",
        )

    def require_image_importer(self, start_response, user):
        if can_import_product_images(user):
            return None
        return self.html_response(
            start_response,
            self.render_message_page("权限不足", "当前账号不能导入商品图片。", user),
            status="403 Forbidden",
        )

    def require_admin(self, start_response, user):
        if can_manage_users(user):
            return None
        return self.html_response(
            start_response,
            self.render_message_page("权限不足", "只有管理员可以管理账号。", user),
            status="403 Forbidden",
        )

    def handle_login(self, start_response, form):
        username = form.get("username", "").strip()
        password = form.get("password", "")
        login_status = db.get_login_attempt_status(self.db_path, username)
        if login_status["is_locked"]:
            minutes_left = max(1, (login_status["remaining_seconds"] + 59) // 60)
            return self.html_response(
                start_response,
                self.render_login(f"登录失败次数过多，账号已临时锁定。请约 {minutes_left} 分钟后再试。"),
                status="423 Locked",
            )
        user = db.authenticate_user(self.db_path, username, password)
        if not user:
            failure_status = db.register_login_failure(self.db_path, username)
            if failure_status["is_locked"]:
                return self.html_response(
                    start_response,
                    self.render_login(f"登录失败次数过多，账号已临时锁定 {db.LOGIN_LOCK_MINUTES} 分钟。"),
                    status="423 Locked",
                )
            return self.html_response(
                start_response,
                self.render_login("账号或密码不正确，请重试。连续失败过多会临时锁定登录。"),
                status="401 Unauthorized",
            )
        db.clear_login_failures(self.db_path, username)
        token = secrets.token_urlsafe(24)
        SESSIONS[token] = user["id"]
        headers = [
            ("Location", "/profile/password" if user.get("must_change_password") else "/modules"),
            ("Set-Cookie", f"session={token}; Path=/; HttpOnly; SameSite=Lax"),
        ]
        start_response("302 Found", headers)
        return [b""]

    def handle_logout(self, environ, start_response):
        raw_cookie = environ.get("HTTP_COOKIE", "")
        parsed_cookie = cookies.SimpleCookie(raw_cookie)
        session_cookie = parsed_cookie.get("session")
        if session_cookie and session_cookie.value in SESSIONS:
            del SESSIONS[session_cookie.value]
        headers = [
            ("Location", "/login"),
            ("Set-Cookie", "session=deleted; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT"),
        ]
        start_response("302 Found", headers)
        return [b""]

    def handle_product_create(self, environ, start_response, user):
        form, files = self.parse_form(environ)
        self.apply_image_upload(form, files, existing_image_url=None)
        form = self.normalized_form_for_stage(user, None, form)
        errors = self.validate_product_form(form)
        if errors:
            return self.html_response(
                start_response,
                self.render_product_form(user, "/products/new", "新建商品资料", form, errors),
                status="400 Bad Request",
            )
        with db.get_connection(self.db_path) as connection:
            product_id = db.create_product(connection, form, user["id"], "A")
        return self.redirect(
            start_response,
            f"/products/{product_id}?notice=" + self.urlencode_message("商品资料已创建。"),
        )

    def handle_product_edit(self, environ, start_response, user, product_id: int):
        product = db.get_product(self.db_path, product_id)
        if not product:
            return self.html_response(
                start_response,
                self.render_message_page("记录不存在", "这条商品资料可能已被删除。", user),
                status="404 Not Found",
            )
        if not can_edit_product(user, product):
            return self.html_response(
                start_response,
                self.render_message_page("不可编辑", "你只能修改自己录入的商品资料。", user),
                status="403 Forbidden",
            )
        if environ.get("REQUEST_METHOD", "GET").upper() == "GET":
            return self.html_response(
                start_response,
                self.render_product_form(
                    user,
                    f"/products/{product_id}/edit",
                    f"编辑资料 #{product_id}",
                    product,
                ),
            )
        form, files = self.parse_form(environ)
        self.apply_image_upload(form, files, existing_image_url=product.get("image_url"))
        form = self.normalized_form_for_stage(user, product, form)
        errors = self.validate_product_form(form)
        if errors:
            merged = {**product, **form}
            return self.html_response(
                start_response,
                self.render_product_form(
                    user,
                    f"/products/{product_id}/edit",
                    f"编辑资料 #{product_id}",
                    merged,
                    errors,
                ),
                status="400 Bad Request",
            )
        with db.get_connection(self.db_path) as connection:
            db.update_product(connection, product_id, form, user["id"])
        return self.redirect(
            start_response,
            f"/products/{product_id}?notice=" + self.urlencode_message("商品资料已更新。"),
        )

    def handle_product_detail(self, start_response, user, product_id: int, query: dict):
        product = db.get_product(self.db_path, product_id)
        if not product:
            return self.html_response(
                start_response,
                self.render_message_page("记录不存在", "没有找到这条商品资料。", user),
                status="404 Not Found",
            )
        if not can_see_product(user, product):
            return self.html_response(
                start_response,
                self.render_message_page("不可查看", "当前账号只能查看已完成且对本角色开放的资料。", user),
                status="403 Forbidden",
            )
        if user.get("department") == "C":
            product = self.visible_products_for_user([product], user)[0]
        notice = query.get("notice", "").strip()
        return self.html_response(start_response, self.render_product_detail(user, product, notice))

    def handle_import(self, environ, start_response, user):
        _, files = self.parse_form(environ)
        workbook_field = files.get("workbook")
        if workbook_field is None or getattr(workbook_field, "file", None) is None:
            return self.html_response(
                start_response,
                self.render_import_page(user, error="请选择一个 Excel 模板文件后再导入。"),
                status="400 Bad Request",
            )
        workbook_field.file.seek(0)
        products = parse_workbook(workbook_field.file)
        if user.get("department") == "B":
            return self.handle_b_stage_import(start_response, user, products)
        created = 0
        updated = 0
        for product in products:
            action, _ = db.save_or_update_owned_product(
                self.db_path,
                product,
                user["id"],
                user["department"],
            )
            if action == "created":
                created += 1
            else:
                updated += 1
        report = f"导入完成：新增 {created} 条，更新 {updated} 条。"
        return self.html_response(start_response, self.render_import_page(user, report=report))

    def handle_b_stage_import(self, start_response, user, products: list[dict]):
        updated = 0
        unmatched: list[str] = []
        ambiguous: list[str] = []
        skipped: list[str] = []
        invalid_channels: list[str] = []
        with db.get_connection(self.db_path) as connection:
            for row in products:
                row_label = str(row.get("style_code") or row.get("product_name") or "未命名资料").strip()
                raw_launch_channel = row.get("launch_channel")
                if raw_launch_channel not in (None, ""):
                    normalized_launch_channel = normalize_launch_channel(raw_launch_channel)
                    if not normalized_launch_channel:
                        invalid_channels.append(row_label)
                        continue
                    row["launch_channel"] = normalized_launch_channel
                candidates = db.find_matching_products_for_import(
                    connection,
                    row.get("style_code"),
                    row.get("style_color"),
                    row.get("color_name"),
                    row.get("product_name"),
                )
                editable_candidates = [item for item in candidates if can_edit_product(user, item)]
                if not editable_candidates:
                    unmatched.append(row_label)
                    continue
                if len(editable_candidates) > 1:
                    ambiguous.append(row_label)
                    continue
                product = editable_candidates[0]
                updated_payload = {field.key: product.get(field.key) for field in PRODUCT_FIELDS}
                changed = False
                for field_key in ("category", "image_url", "launch_price", "launch_channel", "completion_flag"):
                    value = row.get(field_key)
                    if value in (None, ""):
                        continue
                    updated_payload[field_key] = value
                    if field_key == "image_url":
                        updated_payload["image_gallery_json"] = json.dumps([str(value).strip()], ensure_ascii=False)
                    changed = True
                if not changed:
                    skipped.append(row_label)
                    continue
                db.update_product(connection, product["id"], updated_payload, user["id"])
                updated += 1
        report = f"导入完成：已回填 {updated} 条商品部资料。"
        detail_parts = []
        if unmatched:
            detail_parts.append(f"未匹配 {len(unmatched)} 条")
        if ambiguous:
            detail_parts.append(f"重复匹配 {len(ambiguous)} 条")
        if skipped:
            detail_parts.append(f"未填写商品部字段而跳过 {len(skipped)} 条")
        if invalid_channels:
            detail_parts.append(f"上新渠道不合法 {len(invalid_channels)} 条（仅支持天猫、唯品、同款）")
        if detail_parts:
            report = f"{report} 另外：{'，'.join(detail_parts)}。"
        return self.html_response(start_response, self.render_import_page(user, report=report))

    def handle_image_import(self, environ, start_response, user):
        _, files = self.parse_form(environ)
        try:
            mapping_workbook = read_validated_file_upload(
                files.get("mapping_workbook"),
                allowed_extensions={
                    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    ".xls": "application/vnd.ms-excel",
                },
            )
            upload_payloads = read_validated_image_uploads(files.get("image_files"))
        except ValueError as error:
            return self.html_response(
                start_response,
                self.render_image_import_page(user, error=str(error)),
                status="400 Bad Request",
            )

        if mapping_workbook:
            try:
                mapping_rows = parse_image_mapping_workbook(io.BytesIO(mapping_workbook["content"]))
            except ValueError as error:
                return self.html_response(
                    start_response,
                    self.render_image_import_page(user, error=str(error)),
                    status="400 Bad Request",
                )
            except Exception:
                return self.html_response(
                    start_response,
                    self.render_image_import_page(user, error="图片映射 Excel 解析失败，请确认文件可正常打开，并优先使用 xlsx 格式。"),
                    status="400 Bad Request",
                )
            if not upload_payloads:
                return self.html_response(
                    start_response,
                    self.render_image_import_page(user, error="使用 Excel 映射导入时，请同时上传对应的 JPG 或 PNG 图片文件。"),
                    status="400 Bad Request",
                )
            return self.handle_image_import_by_workbook(
                start_response,
                user,
                mapping_rows,
                upload_payloads,
                workbook_name=str(mapping_workbook.get("original_filename") or "").strip(),
            )

        if not upload_payloads:
            return self.html_response(
                start_response,
                self.render_image_import_page(user, error="请选择至少一张图片后再导入。"),
                status="400 Bad Request",
            )
        return self.handle_image_import_by_filename(start_response, user, upload_payloads)

    def handle_image_import_by_filename(self, start_response, user, upload_payloads: list[dict]):
        uploads_by_style_color: dict[str, list[tuple[str, dict]]] = {}
        invalid_names: list[str] = []
        for payload in upload_payloads:
            original_filename = str(payload.get("original_filename") or "").strip()
            style_color_name = self.style_color_name_from_filename(original_filename)
            if not style_color_name:
                invalid_names.append(original_filename or "未命名图片")
                continue
            style_key = self.style_color_match_key(style_color_name)
            uploads_by_style_color.setdefault(style_key, []).append((style_color_name, payload))

        matched: list[str] = []
        unmatched: list[str] = []
        duplicate_uploads: list[str] = []
        ambiguous_matches: list[str] = []
        updated_count = 0

        with db.get_connection(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM products
                WHERE lifecycle_status = 'active'
                ORDER BY updated_at DESC, id DESC
                """
            ).fetchall()
            products_by_style_color: dict[str, list[dict]] = {}
            for row in rows:
                product = db.row_to_dict(row) or {}
                if not can_edit_product(user, product):
                    continue
                style_key = self.style_color_match_key(product.get("style_color"))
                if not style_key:
                    continue
                products_by_style_color.setdefault(style_key, []).append(product)

            for style_key, upload_items in uploads_by_style_color.items():
                style_color_name = upload_items[0][0]
                if len(upload_items) > 1:
                    duplicate_uploads.append(f"{style_color_name}（检测到 {len(upload_items)} 张同名图片）")
                    continue
                target_products = products_by_style_color.get(style_key, [])
                if not target_products:
                    unmatched.append(style_color_name)
                    continue
                if len(target_products) > 1:
                    ambiguous_matches.append(f"{style_color_name}（匹配到 {len(target_products)} 条资料）")
                    continue

                payload = upload_items[0][1]
                product = target_products[0]
                self.apply_imported_image_to_product(connection, user, product, payload)
                updated_count += 1
                matched.append(f"{style_color_name} -> 资料 #{product['id']}")

        report = {
            "mode": "filename",
            "selected_count": len(upload_payloads),
            "updated_count": updated_count,
            "matched": matched,
            "unmatched": unmatched,
            "duplicate_uploads": duplicate_uploads,
            "ambiguous_matches": ambiguous_matches,
            "invalid_names": invalid_names,
        }
        return self.html_response(start_response, self.render_image_import_page(user, report=report))

    def handle_image_import_by_workbook(
        self,
        start_response,
        user,
        mapping_rows: list[dict],
        upload_payloads: list[dict],
        *,
        workbook_name: str = "",
    ):
        uploads_by_filename: dict[str, list[tuple[str, dict]]] = {}
        uploads_by_stem: dict[str, list[tuple[str, dict]]] = {}
        for payload in upload_payloads:
            original_filename = self.normalized_upload_filename(payload.get("original_filename"))
            filename_key = self.style_color_match_key(original_filename)
            stem_name = self.style_color_name_from_filename(original_filename)
            stem_key = self.style_color_match_key(stem_name)
            uploads_by_filename.setdefault(filename_key, []).append((original_filename, payload))
            uploads_by_stem.setdefault(stem_key, []).append((original_filename, payload))

        matched: list[str] = []
        unmatched_products: list[str] = []
        unmatched_images: list[str] = []
        duplicate_uploads: list[str] = []
        ambiguous_matches: list[str] = []
        duplicate_mapping_rows: list[str] = []
        updated_count = 0
        used_upload_payload_ids: set[int] = set()
        seen_style_keys: set[str] = set()

        with db.get_connection(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM products
                WHERE lifecycle_status = 'active'
                ORDER BY updated_at DESC, id DESC
                """
            ).fetchall()
            products_by_style_color: dict[str, list[dict]] = {}
            for row in rows:
                product = db.row_to_dict(row) or {}
                if not can_edit_product(user, product):
                    continue
                style_key = self.style_color_match_key(product.get("style_color"))
                if not style_key:
                    continue
                products_by_style_color.setdefault(style_key, []).append(product)

            for row in mapping_rows:
                style_color_name = str(row.get("style_color") or "").strip()
                image_reference = self.normalized_upload_filename(row.get("image_filename"))
                style_key = self.style_color_match_key(style_color_name)
                if style_key in seen_style_keys:
                    duplicate_mapping_rows.append(f"{style_color_name}（Excel 中重复出现）")
                    continue
                seen_style_keys.add(style_key)

                upload_items = uploads_by_filename.get(self.style_color_match_key(image_reference), [])
                if not upload_items:
                    upload_items = uploads_by_stem.get(self.style_color_match_key(self.style_color_name_from_filename(image_reference)), [])
                if not upload_items:
                    unmatched_images.append(f"{style_color_name} -> {image_reference}")
                    continue
                if len(upload_items) > 1:
                    duplicate_uploads.append(f"{image_reference}（检测到 {len(upload_items)} 个同名图片）")
                    continue

                target_products = products_by_style_color.get(style_key, [])
                if not target_products:
                    unmatched_products.append(style_color_name)
                    continue
                if len(target_products) > 1:
                    ambiguous_matches.append(f"{style_color_name}（匹配到 {len(target_products)} 条资料）")
                    continue

                payload = upload_items[0][1]
                payload_id = id(payload)
                if payload_id in used_upload_payload_ids:
                    duplicate_mapping_rows.append(f"{style_color_name} -> {image_reference}（同一张图片被重复引用）")
                    continue
                used_upload_payload_ids.add(payload_id)

                product = target_products[0]
                self.apply_imported_image_to_product(connection, user, product, payload)
                updated_count += 1
                matched.append(f"{style_color_name} <- {image_reference} -> 资料 #{product['id']}")

        report = {
            "mode": "workbook",
            "workbook_name": workbook_name,
            "mapping_row_count": len(mapping_rows),
            "selected_count": len(upload_payloads),
            "updated_count": updated_count,
            "matched": matched,
            "unmatched": unmatched_products,
            "unmatched_images": unmatched_images,
            "duplicate_uploads": duplicate_uploads,
            "ambiguous_matches": ambiguous_matches,
            "duplicate_mapping_rows": duplicate_mapping_rows,
            "invalid_names": [],
        }
        return self.html_response(start_response, self.render_image_import_page(user, report=report))

    def apply_imported_image_to_product(self, connection, user, product: dict, payload: dict) -> None:
        existing_gallery = self.image_gallery_values(product)
        media_path = save_image_upload(self.upload_dir, payload)
        updated_payload = {field.key: product.get(field.key) for field in PRODUCT_FIELDS}
        updated_payload["image_url"] = media_path
        updated_payload["image_gallery_json"] = json.dumps([media_path], ensure_ascii=False)
        try:
            db.update_product(connection, product["id"], updated_payload, user["id"])
            connection.commit()
        except Exception:
            connection.rollback()
            delete_local_media(self.upload_dir, media_path)
            raise
        for old_media_path in existing_gallery:
            if old_media_path != media_path:
                delete_local_media(self.upload_dir, old_media_path)

    def handle_export(self, start_response, user, query):
        selected_product_ids = {
            product_id for product_id in self.parse_numeric_csv(query.get("selected", ""))
        }
        export_mode = query.get("mode", "").strip()
        if export_mode == "selected" and not selected_product_ids:
            return self.redirect(
                start_response,
                "/products?notice=" + self.urlencode_message("请先勾选至少一条资料，再导出勾选资料。"),
            )
        source_status_filter = "" if user.get("department") == "C" else query.get("status", "")
        visible_products = self.visible_products_for_user(
            db.list_products(
                self.db_path,
                query=query.get("q", ""),
                department=query.get("department", ""),
                status=source_status_filter,
            ),
            user,
        )
        if user.get("department") == "C" and query.get("status", ""):
            visible_products = [
                product for product in visible_products
                if self.c_effective_status(product, user) == query.get("status", "")
            ]
        products = [
            self.product_payload_for_user(product, user)
            for product in visible_products
            if not selected_product_ids or int(product.get("id") or 0) in selected_product_ids
        ]
        visible_fields = self.visible_fields_for_user(user)
        include_images = query.get("include_images", "").strip() == "1"
        body = workbook_bytes(
            products,
            visible_fields,
            image_fetcher=self.fetch_export_image if include_images else None,
        )
        filename = "catalog-export-with-images.xlsx" if include_images else "catalog-export.xlsx"
        headers = [
            ("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            ("Content-Disposition", f'attachment; filename="{filename}"'),
            ("Content-Length", str(len(body))),
        ]
        start_response("200 OK", headers)
        return [body]

    def fetch_export_image(self, image_url: str) -> bytes:
        """Read a local media file or a bounded external image for Excel export."""
        source = str(image_url or "").strip()
        if source.startswith(MEDIA_URL_PREFIX):
            file_path = media_file_path(self.upload_dir, source)
            if not file_path.exists():
                raise ValueError("图片文件不存在。")
            content = file_path.read_bytes()
        else:
            parsed = urlparse(source)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("图片链接必须是有效的 HTTP 或 HTTPS 地址。")
            request = Request(
                source,
                headers={"User-Agent": "Sianna-Catalog-Export/1.0", "Accept": "image/*"},
            )
            with urlopen(request, timeout=8) as response:
                content = response.read(MAX_IMAGE_BYTES + 1)
        if not content or len(content) > MAX_IMAGE_BYTES:
            raise ValueError("图片文件为空或超过 5MB。")
        return content

    def handle_api(self, environ, start_response, user, query):
        if user and user.get("must_change_password"):
            payload = json.dumps(
                {"error": "password_change_required", "message": "当前账号需要先修改密码后才能继续调用接口。"},
                ensure_ascii=False,
            ).encode("utf-8")
            start_response(
                "403 Forbidden",
                [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(payload)))],
            )
            return [payload]
        api_user = user or self.api_token_user(environ, query)
        if not api_user:
            payload = json.dumps(
                {"error": "unauthorized", "message": "请先登录，或使用有效的 C 部门 API 访问令牌。"},
                ensure_ascii=False,
            ).encode("utf-8")
            start_response(
                "401 Unauthorized",
                [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(payload)))],
            )
            return [payload]
        source_status_filter = "" if api_user.get("department") == "C" else query.get("status", "")
        visible_products = self.visible_products_for_user(
            db.list_products(
                self.db_path,
                query=query.get("q", ""),
                department=query.get("department", ""),
                status=source_status_filter,
            ),
            api_user,
        )
        if api_user.get("department") == "C" and query.get("status", ""):
            visible_products = [
                product for product in visible_products
                if self.c_effective_status(product, api_user) == query.get("status", "")
            ]
        products = [self.product_payload_for_user(product, api_user) for product in visible_products]
        payload = json.dumps(
            {
                "role": api_user["department"],
                "count": len(products),
                "items": products,
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        start_response(
            "200 OK",
            [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(payload)))],
        )
        return [payload]

    def handle_healthz(self, start_response):
        db_exists = Path(self.db_path).exists()
        uploads_exists = Path(self.upload_dir).exists()
        user_count = len(db.list_users(self.db_path)) if db_exists else 0
        payload = json.dumps(
            {
                "status": "ok",
                "db_path": self.db_path,
                "db_exists": db_exists,
                "uploads_path": self.upload_dir,
                "uploads_exists": uploads_exists,
                "user_count": user_count,
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        start_response(
            "200 OK",
            [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(payload)))],
        )
        return [payload]

    def handle_status_change(self, environ, start_response, user, product_id: int):
        product = db.get_product(self.db_path, product_id)
        if not product:
            return self.html_response(
                start_response,
                self.render_message_page("记录不存在", "没有找到这条商品资料。", user),
                status="404 Not Found",
            )
        if not can_see_product(user, product):
            return self.html_response(
                start_response,
                self.render_message_page("不可操作", "当前账号不能处理其他运营归属的资料。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        target_status = form.get("status", "").strip()
        review_note = " ".join(str(form.get("review_note", "")).split())
        if user.get("department") == "C" and target_status == "received":
            if product.get("status") not in {"published", "received"}:
                return self.html_response(
                    start_response,
                    self.render_message_page("不可操作", "当前资料尚未完成，不能接收。", user),
                    status="403 Forbidden",
                )
            release_no = int(product.get("c_release_no") or 0)
            if release_no <= 0:
                return self.html_response(
                    start_response,
                    self.render_message_page("不可操作", "当前资料没有可接收的运营版本。", user),
                    status="403 Forbidden",
                )
            with db.get_connection(self.db_path) as connection:
                recorded = db.record_c_product_receipt(connection, product_id, user["id"], release_no)
                if recorded:
                    details = self.status_change_details(user, product, "received", review_note)
                    if product.get("status") != "received":
                        db.change_product_status(
                            connection,
                            product_id,
                            "received",
                            user["id"],
                            "接收资料",
                            details,
                        )
                    else:
                        db.log_product_action(
                            connection,
                            product_id,
                            user["id"],
                            "c:received",
                            "接收资料",
                            details,
                        )
            notice = "资料已接收。" if recorded else "该资料当前版本已接收，无需重复操作。"
            return self.redirect(
                start_response,
                f"/products/{product_id}?notice=" + self.urlencode_message(notice),
            )
        allowed_actions = dict(available_status_actions(user, product))
        if target_status not in allowed_actions:
            return self.html_response(
                start_response,
                self.render_message_page("不可操作", "当前账号不能执行这个状态变更。", user),
                status="403 Forbidden",
            )
        validation_error = self.status_transition_validation_error(product, target_status)
        if validation_error:
            return self.html_response(
                start_response,
                self.render_message_page("无法流转", validation_error, user),
                status="400 Bad Request",
            )
        action_label = allowed_actions[target_status]
        details = self.status_change_details(user, product, target_status, review_note)
        with db.get_connection(self.db_path) as connection:
            db.change_product_status(
                connection,
                product_id,
                target_status,
                user["id"],
                action_label,
                details,
                revision_flag_override=self.status_change_revision_flag(user, product, target_status),
            )
        return self.redirect(
            start_response,
            f"/products/{product_id}?notice=" + self.urlencode_message(f"状态已更新为{status_label(target_status)}。"),
        )

    def handle_lifecycle_change(self, environ, start_response, user, product_id: int):
        product = db.get_product(self.db_path, product_id)
        if not product:
            return self.html_response(
                start_response,
                self.render_message_page("记录不存在", "没有找到这条商品资料。", user),
                status="404 Not Found",
            )
        form, _ = self.parse_form(environ)
        target_status = form.get("lifecycle_status", "").strip()
        allowed_actions = dict(available_lifecycle_actions(user, product))
        if target_status not in allowed_actions:
            return self.html_response(
                start_response,
                self.render_message_page("不可操作", "当前账号不能执行这个资料生命周期操作。", user),
                status="403 Forbidden",
            )
        if target_status == "deleted":
            confirmation = str(form.get("confirm_text", "")).strip()
            if confirmation != "DELETE":
                return self.html_response(
                    start_response,
                    self.render_message_page("需要确认", "删除资料前请输入 DELETE 进行确认。", user),
                    status="400 Bad Request",
                )
        with db.get_connection(self.db_path) as connection:
            db.change_product_lifecycle(
                connection,
                product_id,
                target_status,
                user["id"],
                allowed_actions[target_status],
                self.lifecycle_change_details(user, target_status),
            )
        if target_status == "deleted":
            return_to = self.safe_internal_path(form.get("return_to"))
            if not return_to:
                return_to = "/products"
            separator = "&" if "?" in return_to else "?"
            return self.redirect(
                start_response,
                f"{return_to}{separator}notice=" + self.urlencode_message("资料已删除。"),
            )
        return self.redirect(
            start_response,
            f"/products/{product_id}?notice=" + self.urlencode_message(f"资料生命周期已更新为{lifecycle_label(target_status)}。"),
        )

    def handle_product_logs(self, start_response, user, product_id: int, query: dict):
        product = db.get_product(self.db_path, product_id)
        if not product:
            return self.html_response(
                start_response,
                self.render_message_page("记录不存在", "没有找到这条商品资料。", user),
                status="404 Not Found",
            )
        if not can_view_logs(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能查看操作日志。", user),
                status="403 Forbidden",
            )
        if not can_see_product(user, product):
            return self.html_response(
                start_response,
                self.render_message_page("不可查看", "当前账号不能查看这条资料的日志。", user),
                status="403 Forbidden",
            )
        return self.html_response(start_response, self.render_product_logs(user, product, query))

    def handle_product_versions(self, start_response, user, product_id: int, query: dict):
        product = db.get_product(self.db_path, product_id)
        if not product:
            return self.html_response(
                start_response,
                self.render_message_page("记录不存在", "没有找到这条商品资料。", user),
                status="404 Not Found",
            )
        if not is_admin(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "只有管理员可以查看完整版本记录。", user),
                status="403 Forbidden",
            )
        return self.html_response(start_response, self.render_product_versions(user, product, query))

    def handle_product_version_restore(self, environ, start_response, user, product_id: int, version_no: int):
        product = db.get_product(self.db_path, product_id)
        if not product:
            return self.html_response(
                start_response,
                self.render_message_page("记录不存在", "没有找到这条商品资料。", user),
                status="404 Not Found",
            )
        if not is_admin(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "只有管理员可以恢复资料版本。", user),
                status="403 Forbidden",
            )
        with db.get_connection(self.db_path) as connection:
            new_version_no = db.restore_product_version(connection, product_id, version_no, user["id"])
        return self.redirect(
            start_response,
            f"/products/{product_id}?notice=" + self.urlencode_message(f"已恢复到 V{version_no}，并生成当前版本 V{new_version_no}。"),
        )

    def handle_product_logs_export(self, start_response, user, product_id: int, query: dict):
        product = db.get_product(self.db_path, product_id)
        if not product:
            return self.html_response(
                start_response,
                self.render_message_page("记录不存在", "没有找到这条商品资料。", user),
                status="404 Not Found",
            )
        if not can_view_logs(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能导出操作日志。", user),
                status="403 Forbidden",
            )
        if not can_see_product(user, product):
            return self.html_response(
                start_response,
                self.render_message_page("不可查看", "当前账号不能导出这条资料的日志。", user),
                status="403 Forbidden",
            )
        logs = self.filtered_product_logs(product_id, query)
        body = db.product_logs_csv_bytes(logs)
        start_response(
            "200 OK",
            [
                ("Content-Type", "text/csv; charset=utf-8"),
                ("Content-Disposition", f'attachment; filename="product-{product_id}-logs.csv"'),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    def handle_logs_center(self, start_response, user, query: dict):
        if not can_view_logs(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能查看全局操作日志。", user),
                status="403 Forbidden",
            )
        return self.html_response(start_response, self.render_logs_center(user, query))

    def handle_logs_center_export(self, start_response, user, query: dict):
        if not can_view_logs(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能导出全局操作日志。", user),
                status="403 Forbidden",
            )
        logs = self.filtered_global_logs(user, query)
        body = db.product_logs_csv_bytes(logs)
        start_response(
            "200 OK",
            [
                ("Content-Type", "text/csv; charset=utf-8"),
                ("Content-Disposition", 'attachment; filename="catalog-logs.csv"'),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    def handle_products_bulk(self, environ, start_response, user):
        if not is_admin(user) and user.get("department") not in {"A", "B", "C"}:
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能执行批量操作。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        product_ids = self.collect_numeric_values(form, "product_ids")
        action = form.get("bulk_action", "").strip()
        if not product_ids:
            return self.redirect(
                start_response,
                "/products?notice=" + self.urlencode_message("请先勾选至少一条资料。"),
            )
        updated = 0
        skipped = 0
        skip_reasons: list[str] = []
        with db.get_connection(self.db_path) as connection:
            for product_id in product_ids:
                product = db.get_product(self.db_path, product_id)
                if not product:
                    skipped += 1
                    self.append_bulk_skip_reason(skip_reasons, None, "资料不存在或已被删除。")
                    continue
                if action == "submit_to_b_selected":
                    allowed = dict(available_status_actions(user, product))
                    if "pending" not in allowed:
                        skipped += 1
                        self.append_bulk_skip_reason(skip_reasons, product, "当前状态不能提交给商品部。")
                        continue
                    validation_error = self.status_transition_validation_error(product, "pending")
                    if validation_error:
                        skipped += 1
                        self.append_bulk_skip_reason(skip_reasons, product, validation_error)
                        continue
                    db.change_product_status(
                        connection,
                        product_id,
                        "pending",
                        user["id"],
                        allowed["pending"],
                        self.status_change_details(user, product, "pending"),
                        revision_flag_override=self.status_change_revision_flag(user, product, "pending"),
                    )
                    updated += 1
                    continue
                if action == "complete_to_c_selected":
                    allowed = dict(available_status_actions(user, product))
                    if "published" not in allowed:
                        skipped += 1
                        self.append_bulk_skip_reason(skip_reasons, product, "当前状态不能开放给运营部。")
                        continue
                    validation_error = self.status_transition_validation_error(product, "published")
                    if validation_error:
                        skipped += 1
                        self.append_bulk_skip_reason(skip_reasons, product, validation_error)
                        continue
                    db.change_product_status(
                        connection,
                        product_id,
                        "published",
                        user["id"],
                        allowed["published"],
                        "商品部批量补齐品类、图片、上新价格、上新渠道和资料完成，并开放给运营部读取。",
                    )
                    updated += 1
                    continue
                if action == "receive_selected":
                    if user.get("department") != "C" or not can_see_product(user, product):
                        skipped += 1
                        self.append_bulk_skip_reason(skip_reasons, product, "当前账号不能接收其他运营归属的资料。")
                        continue
                    if product.get("status") not in {"published", "received"} or int(product.get("c_release_no") or 0) <= 0:
                        skipped += 1
                        self.append_bulk_skip_reason(skip_reasons, product, "当前资料不是待接收状态。")
                        continue
                    if db.record_c_product_receipt(connection, product_id, user["id"], int(product.get("c_release_no") or 0)):
                        details = self.status_change_details(user, product, "received")
                        if product.get("status") != "received":
                            db.change_product_status(
                                connection,
                                product_id,
                                "received",
                                user["id"],
                                "接收资料",
                                details,
                            )
                        else:
                            db.log_product_action(
                                connection,
                                product_id,
                                user["id"],
                                "c:received",
                                "接收资料",
                                details,
                            )
                        updated += 1
                    else:
                        skipped += 1
                        self.append_bulk_skip_reason(skip_reasons, product, "当前版本已接收。")
                    continue
                if action == "return_to_a_selected":
                    allowed = dict(available_status_actions(user, product))
                    if "draft" not in allowed:
                        skipped += 1
                        self.append_bulk_skip_reason(skip_reasons, product, "当前状态不能退回给跟单部。")
                        continue
                    db.change_product_status(
                        connection,
                        product_id,
                        "draft",
                        user["id"],
                        allowed["draft"],
                        self.status_change_details(user, product, "draft"),
                    )
                    updated += 1
                    continue
                if action == "publish_selected":
                    allowed = dict(available_status_actions(user, product))
                    if "published" not in allowed:
                        skipped += 1
                        self.append_bulk_skip_reason(skip_reasons, product, "当前状态不能执行批量完成。")
                        continue
                    validation_error = self.status_transition_validation_error(product, "published")
                    if validation_error:
                        skipped += 1
                        self.append_bulk_skip_reason(skip_reasons, product, validation_error)
                        continue
                    db.change_product_status(
                        connection,
                        product_id,
                        "published",
                        user["id"],
                        allowed["published"],
                        "系统管理员批量将资料标记为已完成并开放给 C。",
                    )
                    updated += 1
                    continue
                if action == "archive_selected":
                    allowed = dict(available_lifecycle_actions(user, product))
                    if "archived" not in allowed:
                        skipped += 1
                        self.append_bulk_skip_reason(skip_reasons, product, "当前状态不能批量归档。")
                        continue
                    db.change_product_lifecycle(
                        connection,
                        product_id,
                        "archived",
                        user["id"],
                        allowed["archived"],
                        "系统管理员批量归档资料。",
                    )
                    updated += 1
                    continue
                skipped += 1
        if action == "submit_to_b_selected":
            action_label = "批量提交给商品部填写"
        elif action == "complete_to_c_selected":
            action_label = "批量完成并开放给运营部"
        elif action == "receive_selected":
            action_label = "批量接收资料"
        elif action == "return_to_a_selected":
            action_label = "批量退回跟单部修改"
        elif action == "publish_selected":
            action_label = "批量完成并开放给运营部"
        elif action == "archive_selected":
            action_label = "批量归档"
        else:
            action_label = "批量操作"
        notice = f"{action_label}完成：成功 {updated} 条，跳过 {skipped} 条。"
        if skip_reasons:
            notice = f"{notice} 主要原因：{'；'.join(skip_reasons)}"
        return self.redirect(
            start_response,
            "/products?notice=" + self.urlencode_message(notice),
        )

    def handle_review_bulk(self, environ, start_response, user):
        if not is_admin(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "只有系统管理员可以执行批量流转。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        product_ids = self.collect_numeric_values(form, "product_ids")
        if not product_ids:
            return self.redirect(
                start_response,
                "/products/review?notice=" + self.urlencode_message("请先勾选至少一条待商品部填写资料。"),
            )
        updated = 0
        skipped = 0
        with db.get_connection(self.db_path) as connection:
            for product_id in product_ids:
                product = db.get_product(self.db_path, product_id)
                if not product:
                    skipped += 1
                    continue
                allowed = dict(available_status_actions(user, product))
                if "published" not in allowed:
                    skipped += 1
                    continue
                if self.status_transition_validation_error(product, "published"):
                    skipped += 1
                    continue
                db.change_product_status(
                    connection,
                    product_id,
                    "published",
                    user["id"],
                    allowed["published"],
                    "系统管理员在流转看板中批量将资料标记为已完成并开放给 C。",
                )
                updated += 1
        notice = f"批量完成并开放给运营部：成功 {updated} 条，跳过 {skipped} 条。"
        return self.redirect(
            start_response,
            "/products/review?notice=" + self.urlencode_message(notice),
        )

    def handle_users_page(self, start_response, user, query):
        denied = self.require_admin(start_response, user)
        if denied:
            return denied
        notice = query.get("notice", "").strip()
        return self.html_response(start_response, self.render_users_page(user, notice=notice))

    def handle_user_create(self, environ, start_response, user):
        denied = self.require_admin(start_response, user)
        if denied:
            return denied
        form, _ = self.parse_form(environ)
        if not str(form.get("display_name", "")).strip():
            form["display_name"] = str(form.get("username", "")).strip()
        errors = self.validate_user_form(form, creating=True)
        if errors:
            return self.html_response(
                start_response,
                self.render_users_page(user, form_values=form, errors=errors),
                status="400 Bad Request",
            )
        try:
            db.create_user(
                self.db_path,
                form.get("username", ""),
                form.get("display_name", ""),
                form.get("department", ""),
                form.get("password", ""),
                must_change_password=form.get("must_change_password") == "on",
                operating_channel=form.get("operating_channel", ""),
                billing_platform_codes=self.billing_platform_codes_from_form(form),
            )
        except Exception as error:
            return self.html_response(
                start_response,
                self.render_users_page(user, form_values=form, errors=[f"创建失败：{error}"]),
                status="400 Bad Request",
            )
        return self.redirect(
            start_response,
            "/users?notice=" + self.urlencode_message("账号已创建。"),
        )

    def handle_user_edit_page(self, start_response, user, managed_user_id: int):
        denied = self.require_admin(start_response, user)
        if denied:
            return denied
        managed_user = db.get_user_record(self.db_path, managed_user_id)
        if not managed_user:
            return self.html_response(
                start_response,
                self.render_message_page("账号不存在", "没有找到要编辑的账号。", user),
                status="404 Not Found",
            )
        return self.html_response(start_response, self.render_user_edit_page(user, managed_user))

    def handle_user_update(self, environ, start_response, user, managed_user_id: int):
        denied = self.require_admin(start_response, user)
        if denied:
            return denied
        managed_user = db.get_user_record(self.db_path, managed_user_id)
        if not managed_user:
            return self.html_response(
                start_response,
                self.render_message_page("账号不存在", "没有找到要编辑的账号。", user),
                status="404 Not Found",
            )
        form, _ = self.parse_form(environ)
        errors = self.validate_user_form(form, creating=False)
        if errors:
            merged = {**managed_user, **form}
            return self.html_response(
                start_response,
                self.render_user_edit_page(user, merged, errors=errors),
                status="400 Bad Request",
            )
        db.update_user_profile(
            self.db_path,
            managed_user_id,
            form.get("display_name", ""),
            form.get("department", ""),
            form.get("operating_channel", ""),
            self.billing_platform_codes_from_form(form),
        )
        return self.redirect(
            start_response,
            "/users?notice=" + self.urlencode_message("账号资料已更新。"),
        )

    def handle_user_toggle(self, environ, start_response, user, managed_user_id: int):
        denied = self.require_admin(start_response, user)
        if denied:
            return denied
        managed_user = db.get_user_record(self.db_path, managed_user_id)
        if not managed_user:
            return self.html_response(
                start_response,
                self.render_message_page("账号不存在", "没有找到这个账号。", user),
                status="404 Not Found",
            )
        if managed_user["id"] == user["id"] and managed_user.get("is_active"):
            return self.html_response(
                start_response,
                self.render_message_page("不可停用", "不能停用当前登录的管理员账号。", user),
                status="400 Bad Request",
            )
        target_active = not bool(managed_user.get("is_active"))
        form, _ = self.parse_form(environ)
        if not target_active:
            confirmation = str(form.get("confirm_text", "")).strip()
            if confirmation != "DISABLE":
                return self.html_response(
                    start_response,
                    self.render_message_page("需要确认", "停用账号前请输入 DISABLE 进行确认。", user),
                    status="400 Bad Request",
                )
        db.set_user_active(self.db_path, managed_user_id, target_active)
        db.log_admin_audit_action(
            self.db_path,
            user["id"],
            "user:enable" if target_active else "user:disable",
            "启用账号" if target_active else "停用账号",
            "user",
            str(managed_user_id),
            managed_user.get("username", ""),
            f"{'启用' if target_active else '停用'}了账号 {managed_user.get('username', '')}。",
        )
        notice = "账号已启用。" if target_active else "账号已停用。"
        return self.redirect(start_response, "/users?notice=" + self.urlencode_message(notice))

    def handle_user_reset_password(self, environ, start_response, user, managed_user_id: int):
        denied = self.require_admin(start_response, user)
        if denied:
            return denied
        managed_user = db.get_user_record(self.db_path, managed_user_id)
        if not managed_user:
            return self.html_response(
                start_response,
                self.render_message_page("账号不存在", "没有找到这个账号。", user),
                status="404 Not Found",
            )
        form, _ = self.parse_form(environ)
        password = form.get("new_password", "").strip()
        confirmation = str(form.get("confirm_text", "")).strip()
        if len(password) < 6:
            return self.html_response(
                start_response,
                self.render_message_page("密码过短", "重置密码至少需要 6 位。", user),
                status="400 Bad Request",
            )
        if confirmation != "RESET":
            return self.html_response(
                start_response,
                self.render_message_page("需要确认", "重置密码前请输入 RESET 进行确认。", user),
                status="400 Bad Request",
            )
        db.reset_user_password(self.db_path, managed_user_id, password, must_change_password=True)
        db.log_admin_audit_action(
            self.db_path,
            user["id"],
            "user:reset_password",
            "重置密码",
            "user",
            str(managed_user_id),
            managed_user.get("username", ""),
            f"重置了账号 {managed_user.get('username', '')} 的密码，并要求下次登录修改密码。",
        )
        return self.redirect(
            start_response,
            "/users?notice=" + self.urlencode_message("密码已重置，用户下次登录后应立即修改。"),
        )

    def handle_media(self, start_response, user, path: str):
        file_path = media_file_path(self.upload_dir, path)
        if not file_path.exists():
            return self.html_response(
                start_response,
                self.render_message_page("文件不存在", "没有找到这张图片。"),
                status="404 Not Found",
            )
        media_is_visible = any(
            path in self.image_gallery_values(product) and can_see_product(user, product)
            for product in db.list_products(self.db_path)
        )
        if not media_is_visible:
            return self.html_response(
                start_response,
                self.render_message_page("不可查看", "当前账号不能查看这张图片。", user),
                status="403 Forbidden",
            )
        body = file_path.read_bytes()
        start_response(
            "200 OK",
            [
                ("Content-Type", media_content_type(path)),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    def handle_password_change(self, environ, start_response, user):
        form, _ = self.parse_form(environ)
        current_password = form.get("current_password", "")
        new_password = form.get("new_password", "")
        confirm_password = form.get("confirm_password", "")
        errors = []
        if not db.verify_password(current_password, user.get("password_hash", "")):
            errors.append("当前密码不正确。")
        if len(new_password) < 6:
            errors.append("新密码至少需要 6 位。")
        if new_password != confirm_password:
            errors.append("两次输入的新密码不一致。")
        if current_password and new_password and current_password == new_password:
            errors.append("新密码不能与当前密码相同。")
        if errors:
            return self.html_response(
                start_response,
                self.render_password_change_page(user, errors=errors),
                status="400 Bad Request",
            )
        db.update_user_password(self.db_path, user["id"], new_password, must_change_password=False)
        notice = "密码已更新。"
        if user.get("must_change_password"):
            notice = "密码已更新，已解除首次登录改密要求。"
        return self.redirect(
            start_response,
            "/products?notice=" + self.urlencode_message(notice),
        )

    def handle_c_field_settings_page(self, start_response, user, query):
        denied = self.require_admin(start_response, user)
        if denied:
            return denied
        notice = query.get("notice", "").strip()
        return self.html_response(
            start_response,
            self.render_c_field_settings_page(user, notice=notice),
        )

    def handle_list_layout_settings_page(self, start_response, user, query):
        if is_executive_read_only(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "总经办账号可查看和下载资料，但不能修改部门列表字段设置。", user),
                status="403 Forbidden",
            )
        notice = query.get("notice", "").strip()
        return self.html_response(
            start_response,
            self.render_list_layout_settings_page(user, notice=notice),
        )

    def handle_list_layout_settings_update(self, environ, start_response, user):
        if is_executive_read_only(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "总经办账号不能修改部门列表字段设置。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        ordered_keys = self.normalize_list_layout_order(form, user)
        selected_key_set = set(self.normalize_list_layout_keys(self.collect_checkbox_values(form, "field_keys"), user))
        selected_keys = [key for key in ordered_keys if key in selected_key_set]
        if not selected_keys:
            return self.html_response(
                start_response,
                self.render_list_layout_settings_page(
                    user,
                    selected_keys=selected_keys,
                    errors=["至少需要选择一个列表字段。"],
                ),
                status="400 Bad Request",
            )
        db.set_setting(
            self.db_path,
            self.list_layout_setting_key(user),
            ",".join(selected_keys),
        )
        db.set_setting(
            self.db_path,
            self.list_layout_customized_setting_key(user),
            "1",
        )
        notice = f"{department_label(user.get('department'))}的资料列表字段已更新。"
        return self.redirect(
            start_response,
            "/settings/list-layout?notice=" + self.urlencode_message(notice),
        )

    def handle_c_field_settings_update(self, environ, start_response, user):
        denied = self.require_admin(start_response, user)
        if denied:
            return denied
        form, _ = self.parse_form(environ)
        template_name = str(form.get("template_name", "")).strip()
        templates = self.c_field_templates()
        if "apply_template" in form:
            template = templates.get(template_name)
            selected_keys = template["field_keys"] if template else []
            if not selected_keys:
                return self.html_response(
                    start_response,
                    self.render_c_field_settings_page(
                        user,
                        template_name=template_name,
                        errors=["要套用的字段模板不存在，或模板里没有有效字段。"],
                    ),
                    status="400 Bad Request",
                )
            db.set_setting(self.db_path, "c_visible_field_keys", ",".join(selected_keys))
            return self.redirect(
                start_response,
                "/settings/c-fields?notice=" + self.urlencode_message(f"已套用字段模板：{template_name}。"),
            )
        if "delete_template" in form:
            if template_name not in templates:
                return self.html_response(
                    start_response,
                    self.render_c_field_settings_page(
                        user,
                        template_name=template_name,
                        errors=["要删除的字段模板不存在。"],
                    ),
                    status="400 Bad Request",
                )
            del templates[template_name]
            self.save_c_field_templates(templates)
            return self.redirect(
                start_response,
                "/settings/c-fields?notice=" + self.urlencode_message(f"已删除字段模板：{template_name}。当前开放字段保持不变。"),
            )
        selected_keys = self.normalize_field_keys(self.collect_checkbox_values(form, "field_keys"))
        if not selected_keys:
            return self.html_response(
                start_response,
                    self.render_c_field_settings_page(
                        user,
                        selected_keys=selected_keys,
                        template_name=template_name,
                        errors=["至少需要开放一个字段给运营部。"],
                    ),
                status="400 Bad Request",
            )
        if "save_template" in form:
            if not template_name:
                return self.html_response(
                    start_response,
                    self.render_c_field_settings_page(
                        user,
                        selected_keys=selected_keys,
                        template_name=template_name,
                        errors=["请先填写模板名称，再保存字段模板。"],
                    ),
                    status="400 Bad Request",
                )
            if len(template_name) > 40:
                return self.html_response(
                    start_response,
                    self.render_c_field_settings_page(
                        user,
                        selected_keys=selected_keys,
                        template_name=template_name,
                        errors=["模板名称请控制在 40 个字符以内。"],
                    ),
                    status="400 Bad Request",
                )
            templates[template_name] = {
                "field_keys": selected_keys,
            }
            self.save_c_field_templates(templates)
            db.set_setting(self.db_path, "c_visible_field_keys", ",".join(selected_keys))
            db.log_admin_audit_action(
                self.db_path,
                user["id"],
                "settings:c_template_save",
                "保存字段模板",
                "c_field_template",
                template_name,
                template_name,
                f"保存了字段模板，共 {len(selected_keys)} 个字段。",
            )
            return self.redirect(
                start_response,
                "/settings/c-fields?notice=" + self.urlencode_message(f"已保存字段模板：{template_name}，并同步更新当前开放字段。"),
            )
        db.set_setting(self.db_path, "c_visible_field_keys", ",".join(selected_keys))
        if "rotate_token" in form:
            db.set_setting(self.db_path, "c_api_token", secrets.token_urlsafe(24))
            db.log_admin_audit_action(
                self.db_path,
                user["id"],
                "settings:c_api_token_rotate",
                "生成或轮换 API 令牌",
                "c_api_token",
                "c_api_token",
                "C 部门 API 令牌",
                "生成或轮换了 C 部门只读 API 令牌。",
            )
            notice = "C 部门可见字段已更新，并已生成新的 API 访问令牌。"
        elif "disable_token" in form:
            confirmation = str(form.get("confirm_text", "")).strip()
            if confirmation != "DISABLE":
                return self.html_response(
                    start_response,
                    self.render_c_field_settings_page(
                        user,
                        selected_keys=selected_keys,
                        template_name=template_name,
                        errors=["停用 API 令牌前请输入 DISABLE 进行确认。"],
                    ),
                    status="400 Bad Request",
                )
            db.set_setting(self.db_path, "c_api_token", "")
            db.log_admin_audit_action(
                self.db_path,
                user["id"],
                "settings:c_api_token_disable",
                "停用 API 令牌",
                "c_api_token",
                "c_api_token",
                "C 部门 API 令牌",
                "停用了 C 部门只读 API 令牌。",
            )
            notice = "C 部门可见字段已更新，并已停用 API 访问令牌。"
        else:
            db.log_admin_audit_action(
                self.db_path,
                user["id"],
                "settings:c_visible_fields_update",
                "更新 C 字段开放范围",
                "c_visible_fields",
                "c_visible_field_keys",
                "C 部门字段开放设置",
                f"更新了 C 部门开放字段，共 {len(selected_keys)} 个字段。",
            )
            notice = "C 部门可见字段已更新。"
        return self.redirect(
            start_response,
            "/settings/c-fields?notice=" + self.urlencode_message(notice),
        )

    def validate_product_form(self, form):
        errors = []
        if not form.get("product_name", "").strip():
            errors.append("商品名称不能为空。")
        if not form.get("style_code", "").strip():
            errors.append("款号不能为空。")
        launch_channel = str(form.get("launch_channel", "")).strip()
        if launch_channel and not normalize_launch_channel(launch_channel):
            errors.append("上新渠道仅支持天猫、唯品、同款。")
        for numeric_field in (
            "tax_included_price",
            "tag_price",
            "launch_price",
            "size_69",
            "size_f",
            "size_s",
            "size_m",
            "size_l",
            "size_xl",
            "size_2xl",
            "size_3xl",
            "total_quantity",
        ):
            raw_value = form.get(numeric_field, "")
            if raw_value is None:
                continue
            value = str(raw_value).strip()
            if not value:
                continue
            try:
                if PRODUCT_FIELD_MAP[numeric_field].storage_type == "INTEGER":
                    int(float(value))
                else:
                    float(value)
            except ValueError:
                errors.append(f"{PRODUCT_FIELD_MAP[numeric_field].label} 必须是数字。")
        return errors

    def apply_image_upload(self, form: dict, files: dict, existing_image_url: str | None):
        existing_gallery = self.image_gallery_values({"image_gallery_json": form.get("image_gallery_json"), "image_url": existing_image_url})
        retained_gallery = self.collect_gallery_values(form, "image_gallery_existing")
        manual_gallery = self.collect_gallery_values(form, "image_gallery_manual")
        upload_payloads = read_validated_image_uploads(files.get("image_uploads"))
        upload_payload = read_validated_image_upload(files.get("image_upload"))
        manual_value = str(form.get("image_url", "")).strip()
        gallery_values = []
        if retained_gallery:
            gallery_values.extend(retained_gallery)
        elif existing_gallery and not manual_value and not upload_payload and not upload_payloads and "image_gallery_existing__0" not in form:
            gallery_values.extend(existing_gallery)
        for value in manual_gallery:
            if value not in gallery_values:
                gallery_values.append(value)
        for payload in upload_payloads:
            media_path = save_image_upload(self.upload_dir, payload)
            if media_path not in gallery_values:
                gallery_values.append(media_path)
        if upload_payload:
            new_media_path = save_image_upload(self.upload_dir, upload_payload)
            if new_media_path not in gallery_values:
                gallery_values.insert(0, new_media_path)
        elif manual_value and manual_value not in gallery_values:
            gallery_values.insert(0, manual_value)
        gallery_values = self.normalize_gallery_values(gallery_values)
        removed_local_media = {
            value for value in existing_gallery
            if value not in gallery_values
        }
        for media_path in removed_local_media:
            delete_local_media(self.upload_dir, media_path)
        form["image_gallery_json"] = json.dumps(gallery_values, ensure_ascii=False)
        if gallery_values:
            form["image_url"] = gallery_values[0]
            return
        form["image_url"] = ""

    def editable_field_keys_for_request(self, user, product: dict | None = None) -> set[str]:
        return set(editable_field_keys_for_user(user, product))

    def normalized_form_for_stage(self, user, product: dict | None, form: dict) -> dict:
        editable_keys = self.editable_field_keys_for_request(user, product)
        normalized = {}
        source = product or {}
        for field in PRODUCT_FIELDS:
            if field.key in editable_keys:
                if field.key in form:
                    normalized[field.key] = form.get(field.key, "")
                else:
                    normalized[field.key] = source.get(field.key, "")
            else:
                normalized[field.key] = source.get(field.key, "")
        normalized["image_gallery_json"] = form.get("image_gallery_json", source.get("image_gallery_json", "[]"))
        if "launch_channel" in editable_keys:
            clean_channel = normalize_launch_channel(normalized.get("launch_channel"))
            if clean_channel:
                normalized["launch_channel"] = clean_channel
        return normalized

    def validate_user_form(self, form, creating: bool):
        errors = []
        if creating and not form.get("username", "").strip():
            errors.append("用户名不能为空。")
        if not form.get("display_name", "").strip():
            errors.append("显示名称不能为空。")
        if form.get("department", "").strip() not in MANAGEABLE_DEPARTMENTS:
            errors.append("角色或部门不合法。")
        if form.get("department", "").strip() == "C" and form.get("operating_channel", "").strip() not in C_OPERATING_CHANNELS:
            errors.append("运营部账号必须选择天猫类或唯品类运营归属。")
        if form.get("department", "").strip() == "C":
            raw_billing_codes = self.collect_checkbox_values(form, "billing_platform_codes")
            billing_codes = normalize_billing_platform_codes(raw_billing_codes)
            if not billing_codes:
                errors.append("运营部账号至少需要选择一个账单属性。")
            elif len(billing_codes) != len({str(code or "").strip().lower() for code in raw_billing_codes}):
                errors.append("账单属性包含不支持的平台。")
        if creating:
            password = form.get("password", "")
            if len(password) < 6:
                errors.append("初始密码至少需要 6 位。")
        return errors

    def billing_platform_codes_from_form(self, form: dict) -> tuple[str, ...]:
        return normalize_billing_platform_codes(
            self.collect_checkbox_values(form, "billing_platform_codes")
        )

    def billing_platform_codes_for_user_form(self, values: dict) -> tuple[str, ...]:
        submitted_codes = self.collect_checkbox_values(values, "billing_platform_codes")
        if submitted_codes:
            return normalize_billing_platform_codes(submitted_codes)
        return normalize_billing_platform_codes(values.get("billing_platforms_json"))

    def html_response(self, start_response, body: str, status: str = "200 OK"):
        payload = body.encode("utf-8")
        headers = [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(payload)))]
        start_response(status, headers)
        return [payload]

    def redirect(self, start_response, location: str):
        start_response("302 Found", [("Location", location)])
        return [b""]

    def urlencode_message(self, message: str) -> str:
        return urlencode({"notice": message}).split("=", 1)[1]

    def platform_bills_page_url(self, month_key: str, notice: str = "", platform_code: str = "") -> str:
        params = {"month": str(month_key or "").strip()}
        if str(platform_code or "").strip():
            params["platform"] = str(platform_code).strip()
        if notice:
            params["notice"] = notice
        return "/billing/platform-bills?" + urlencode(params)

    def safe_internal_path(self, value) -> str:
        path = str(value or "").strip()
        if not path.startswith("/") or path.startswith("//"):
            return ""
        return path

    def products_return_path(self, query: dict) -> str:
        params = {}
        for key in ("q", "department", "status", "lifecycle_status"):
            value = str(query.get(key, "")).strip()
            if value:
                params[key] = value
        if not params:
            return "/products"
        return "/products?" + urlencode(params)

    def style_code_detail_redirect_target(self, user, query: dict) -> str:
        keyword = str(query.get("q", "")).strip()
        if not keyword:
            return ""
        exact_matches = []
        for product in db.list_products(
            self.db_path,
            keyword,
            str(query.get("department", "")).strip(),
            str(query.get("status", "")).strip(),
            str(query.get("lifecycle_status", "")).strip(),
        ):
            if not can_see_product(user, product):
                continue
            style_code = str(product.get("style_code") or "").strip()
            if style_code.casefold() != keyword.casefold():
                continue
            exact_matches.append(product)
        if len(exact_matches) != 1:
            return ""
        product = exact_matches[0]
        params = {"notice": f"已为你打开款号 {keyword} 的资料详情。"}
        if is_department_monitor(user):
            params["monitor_department"] = str(user.get("monitor_department") or "")
        return f"/products/{product['id']}?" + urlencode(params)

    def style_color_match_key(self, value) -> str:
        normalized = " ".join(str(value or "").replace("\u3000", " ").split())
        return normalized.casefold()

    def style_color_name_from_filename(self, filename: str | None) -> str:
        clean_filename = Path(str(filename or "")).name
        return " ".join(Path(clean_filename).stem.replace("\u3000", " ").split())

    def normalized_upload_filename(self, filename: str | None) -> str:
        clean_filename = str(filename or "").strip().replace("\\", "/")
        return " ".join(Path(clean_filename).name.replace("\u3000", " ").split())

    def empty_platform_bill_rows(self) -> list[dict]:
        return [
            {
                "platform_code": platform_code,
                "platform_label": db.platform_bill_platform_label_for_db(self.db_path, platform_code),
                "main_file": None,
                "attachments": [],
                "main_ready": False,
                "submitted": False,
                "submitted_at": None,
                "submitted_by": None,
                "submitted_by_name": "",
            }
            for platform_code in db.platform_bill_platform_codes_for_db(self.db_path)
        ]

    def platform_bill_file_link(self, file_item: dict | None, user: dict) -> str:
        if not file_item:
            return '<span class="meta">未上传</span>'
        if user.get("department") == "C" and int(file_item.get("uploaded_by") or 0) != int(user.get("id") or 0):
            return '<span class="meta">已由其他同事上传</span>'
        file_name = html.escape(str(file_item.get("original_filename") or "未命名文件"))
        uploaded_at = html.escape(self.format_list_timestamp(file_item.get("created_at")))
        if user.get("department") == "C":
            return f"{file_name}<br><span class=\"meta\">{uploaded_at}</span>"
        download_url = f"/billing/platform-bills/files/{int(file_item.get('id') or 0)}"
        return (
            f'<a href="{download_url}">{file_name}</a>'
            f'<br><span class="meta">{uploaded_at}</span>'
        )

    def can_manage_platform_bill_file(self, user: dict, file_item: dict | None, is_locked: bool) -> bool:
        if not user or not file_item or is_locked:
            return False
        if user.get("department") != "C":
            return False
        if not c_user_can_manage_platform_bill(user, file_item.get("platform_code")):
            return False
        return int(file_item.get("uploaded_by") or 0) == int(user.get("id") or 0)

    def platform_item_locked(self, platform_item: dict | None, month_status: str | None = None) -> bool:
        if not platform_item:
            return bool(month_status == "submitted_to_b")
        return bool(platform_item.get("submitted")) or month_status == "submitted_to_b"

    def brand_bill_file_link(self, version_item: dict | None) -> str:
        if not version_item:
            return "暂无上传文件"
        file_name = html.escape(str(version_item.get("original_filename") or "未命名文件"))
        uploaded_at = html.escape(self.format_list_timestamp(version_item.get("created_at")))
        version_no = int(version_item.get("version_no") or 0)
        download_url = f"/billing/brand-bills/files/{int(version_item.get('id') or 0)}"
        return (
            f'<a href="{download_url}">{file_name}</a>'
            f'<br><span class="meta">V{version_no} · {uploaded_at}</span>'
        )

    def brand_bill_dashboard_summary(self, latest_version: dict | None) -> dict:
        empty_summary = {
            "month_label": "",
            "channel_count": 0,
            "shop_count": 0,
            "total_qty": 0.0,
            "total_amount": 0.0,
            "gz_qty": 0.0,
            "gz_amount": 0.0,
            "wh_qty": 0.0,
            "wh_amount": 0.0,
            "channels": [],
            "table_rows": [],
        }
        if not latest_version or not latest_version.get("stored_path"):
            return empty_summary
        file_path = upload_file_path(self.upload_dir, latest_version["stored_path"])
        if not file_path.exists():
            return empty_summary
        try:
            with file_path.open("rb") as file_obj:
                return parse_brand_bill_workbook(file_obj)
        except Exception:
            return empty_summary

    def brand_bill_dashboard_summary_from_rows(self, rows: list[dict], latest_version: dict | None = None) -> dict:
        if not rows:
            return self.brand_bill_dashboard_summary(latest_version)
        summary = {
            "month_label": "",
            "channel_count": 0,
            "shop_count": 0,
            "total_qty": 0.0,
            "total_amount": 0.0,
            "gz_qty": 0.0,
            "gz_amount": 0.0,
            "wh_qty": 0.0,
            "wh_amount": 0.0,
            "channels": [],
            "table_rows": [],
        }
        channels = {}
        for row in rows:
            month_label = str(row.get("month_label") or "").strip()
            platform_name = str(row.get("platform_name") or "").strip()
            shop_name = str(row.get("shop_name") or "").strip()
            total_qty = float(row.get("total_qty") or 0)
            total_amount = float(row.get("total_amount") or 0)
            gz_qty = float(row.get("gz_qty") or 0)
            gz_amount = float(row.get("gz_amount") or 0)
            wh_qty = float(row.get("wh_qty") or 0)
            wh_amount = float(row.get("wh_amount") or 0)
            summary["table_rows"].append(
                {
                    "month_label": month_label,
                    "platform_name": platform_name,
                    "shop_name": shop_name,
                    "total_qty": total_qty,
                    "total_amount": total_amount,
                    "gz_qty": gz_qty,
                    "gz_amount": gz_amount,
                    "wh_qty": wh_qty,
                    "wh_amount": wh_amount,
                }
            )
            if month_label and not summary["month_label"]:
                summary["month_label"] = month_label
            if not platform_name:
                continue
            channel_key = platform_name if platform_name != "合计" else "合计"
            bucket = channels.setdefault(
                channel_key,
                {
                    "channel_name": channel_key,
                    "shop_count": 0,
                    "shops": [],
                    "total_qty": 0.0,
                    "total_amount": 0.0,
                    "gz_qty": 0.0,
                    "gz_amount": 0.0,
                    "wh_qty": 0.0,
                    "wh_amount": 0.0,
                },
            )
            if shop_name and "合计" not in shop_name:
                bucket["shop_count"] += 1
                bucket["shops"].append(shop_name)
            bucket["total_qty"] += total_qty
            bucket["total_amount"] += total_amount
            bucket["gz_qty"] += gz_qty
            bucket["gz_amount"] += gz_amount
            bucket["wh_qty"] += wh_qty
            bucket["wh_amount"] += wh_amount
        if "合计" in channels:
            total_row = channels["合计"]
            summary["total_qty"] = total_row["total_qty"]
            summary["total_amount"] = total_row["total_amount"]
            summary["gz_qty"] = total_row["gz_qty"]
            summary["gz_amount"] = total_row["gz_amount"]
            summary["wh_qty"] = total_row["wh_qty"]
            summary["wh_amount"] = total_row["wh_amount"]
        filtered_channels = [value for key, value in channels.items() if key != "合计"]
        if not summary["total_qty"] and not summary["total_amount"]:
            summary["total_qty"] = sum(item["total_qty"] for item in filtered_channels)
            summary["total_amount"] = sum(item["total_amount"] for item in filtered_channels)
            summary["gz_qty"] = sum(item["gz_qty"] for item in filtered_channels)
            summary["gz_amount"] = sum(item["gz_amount"] for item in filtered_channels)
            summary["wh_qty"] = sum(item["wh_qty"] for item in filtered_channels)
            summary["wh_amount"] = sum(item["wh_amount"] for item in filtered_channels)
        summary["channels"] = filtered_channels
        summary["channel_count"] = len(filtered_channels)
        summary["shop_count"] = sum(item["shop_count"] for item in filtered_channels)
        return summary

    def format_money(self, value) -> str:
        return f"{float(value or 0):,.2f}"

    def format_table_metric(self, value, money: bool = False) -> str:
        if value in (None, ""):
            return ""
        if money:
            return self.format_money(value)
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return html.escape(str(value))
        if numeric.is_integer():
            return str(int(numeric))
        return f"{numeric:g}"

    def format_table_ratio(self, value, total) -> str:
        try:
            denominator = float(total or 0)
            numerator = float(value or 0)
        except (TypeError, ValueError):
            return ""
        if not denominator:
            return ""
        return f"{numerator / denominator:.1%}"

    def split_multiline_values(self, raw_value) -> list[str]:
        values = []
        for line in str(raw_value or "").replace("\r", "\n").split("\n"):
            clean = line.strip()
            if clean:
                values.append(clean)
        return values

    def configured_c_field_keys(self) -> list[str]:
        raw_value = db.get_setting(self.db_path, "c_visible_field_keys") or ""
        keys = self.normalize_field_keys(raw_value.split(","))
        if not keys:
            keys = [field.key for field in self.c_field_available_fields() if field.visible_to_c]
        return keys

    def c_field_available_fields(self):
        source_user = {"department": "A"}
        fields = self.configured_list_layout_fields(source_user)
        available_keys = {field.key for field in fields}
        for field_key in CATALOG_EXPORT_FIELD_ORDER:
            if field_key not in B_STAGE_FIELD_KEYS or field_key in available_keys:
                continue
            field = PRODUCT_FIELD_MAP.get(field_key)
            if not field or field.key in LIST_LAYOUT_HIDDEN_FIELD_KEYS:
                continue
            fields.append(field)
            available_keys.add(field.key)
        return fields

    def normalize_field_keys(self, values) -> list[str]:
        valid_keys = {field.key for field in self.c_field_available_fields()}
        normalized = []
        seen = set()
        for value in values:
            key = str(value).strip()
            if not key or key not in valid_keys or key in seen:
                continue
            normalized.append(key)
            seen.add(key)
        return normalized

    def list_layout_setting_key(self, user: dict) -> str:
        department = (user.get("department") or "").strip().upper()
        if department not in {"A", "B", "C"}:
            department = "A"
        return f"list_layout_fields_{department}"

    def list_layout_customized_setting_key(self, user: dict) -> str:
        department = (user.get("department") or "").strip().upper()
        if department not in {"A", "B", "C"}:
            department = "A"
        return f"list_layout_customized_{department}"

    def list_layout_available_fields(self, user: dict):
        fields = [field for field in self.visible_fields_for_user(user) if field.key not in LIST_LAYOUT_HIDDEN_FIELD_KEYS]
        existing_keys = {field.key for field in fields}
        for field in LIST_LAYOUT_VIRTUAL_FIELDS:
            if field.key in existing_keys:
                continue
            fields.append(field)
        return fields

    def list_layout_field_by_key(self, field_key: str):
        return PRODUCT_FIELD_MAP.get(field_key) or LIST_LAYOUT_VIRTUAL_FIELD_MAP.get(field_key)

    def normalize_list_layout_keys(self, values, user: dict) -> list[str]:
        valid_keys = {field.key for field in self.list_layout_available_fields(user)}
        normalized = []
        seen = set()
        for value in values:
            key = str(value).strip()
            if not key or key not in valid_keys or key in seen:
                continue
            normalized.append(key)
            seen.add(key)
        return normalized

    def normalize_list_layout_order(self, form: dict, user: dict) -> list[str]:
        rank_rows = []
        for key, value in form.items():
            if not key.startswith("field_rank__"):
                continue
            field_key = key.split("__", 1)[1]
            rank_value = str(value).strip()
            if not rank_value.isdigit():
                continue
            rank_rows.append((int(rank_value), field_key))
        if rank_rows:
            rank_rows.sort(key=lambda item: (item[0], item[1]))
            return self.normalize_list_layout_keys([field_key for _, field_key in rank_rows], user)
        return self.normalize_list_layout_keys(self.collect_checkbox_values(form, "field_order"), user)

    def default_list_layout_keys(self, user: dict) -> list[str]:
        available_fields = self.list_layout_available_fields(user)
        available_keys = [field.key for field in available_fields]
        department = (user.get("department") or "").strip().upper()
        if is_executive_read_only(user):
            department = "A"
        default_keys = db.ordered_list_layout_keys(available_keys, department)
        return default_keys or available_keys[:4]

    def configured_list_layout_keys(self, user: dict) -> list[str]:
        raw_value = db.get_setting(self.db_path, self.list_layout_setting_key(user)) or ""
        keys = self.normalize_list_layout_keys(raw_value.split(","), user)
        is_customized = (db.get_setting(self.db_path, self.list_layout_customized_setting_key(user)) or "0") == "1"
        if is_customized and keys:
            return keys
        default_keys = self.default_list_layout_keys(user)
        if not is_customized:
            return default_keys
        return keys or default_keys

    def configured_list_layout_fields(self, user: dict):
        fields = []
        for key in self.configured_list_layout_keys(user):
            field = self.list_layout_field_by_key(key)
            if not field:
                continue
            fields.append(field)
        return fields

    def c_field_templates(self) -> dict[str, dict]:
        raw_value = db.get_setting(self.db_path, "c_field_templates_json") or "{}"
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        templates = {}
        for raw_name, raw_value in parsed.items():
            name = str(raw_name).strip()
            normalized_keys = []
            rules = {}
            if isinstance(raw_value, list):
                normalized_keys = self.normalize_field_keys(raw_value)
            elif isinstance(raw_value, dict):
                normalized_keys = self.normalize_field_keys(raw_value.get("field_keys", []))
            if not name:
                continue
            if normalized_keys:
                templates[name] = {
                    "field_keys": normalized_keys,
                }
        return templates

    def save_c_field_templates(self, templates: dict[str, dict]) -> None:
        cleaned_templates = {}
        for raw_name, raw_template in templates.items():
            name = str(raw_name).strip()
            if isinstance(raw_template, dict):
                normalized_keys = self.normalize_field_keys(raw_template.get("field_keys", []))
            else:
                normalized_keys = self.normalize_field_keys(raw_template)
            if not name or not normalized_keys:
                continue
            cleaned_templates[name] = {
                "field_keys": normalized_keys,
            }
        db.set_setting(
            self.db_path,
            "c_field_templates_json",
            json.dumps(cleaned_templates, ensure_ascii=False),
        )

    def visible_fields_for_c_product(self, product: dict):
        return visible_fields_from_keys(self.configured_c_field_keys())

    def filtered_product_logs(self, product_id: int, query: dict) -> list[dict]:
        logs = db.get_product_logs(self.db_path, product_id)
        return db.filter_product_logs(
            logs,
            action_query=query.get("action", ""),
            actor_query=query.get("actor", ""),
            product_query=query.get("product", ""),
        )

    def filtered_global_logs(self, user: dict, query: dict) -> list[dict]:
        logs = db.list_product_logs(self.db_path)
        visible_logs = []
        for item in logs:
            if is_admin(user):
                visible_logs.append(item)
                continue
            if item.get("created_by") == user.get("id"):
                visible_logs.append(item)
        filtered_logs = db.filter_product_logs(
            visible_logs,
            action_query=query.get("action", ""),
            actor_query=query.get("actor", ""),
            product_query=query.get("product", ""),
        )
        if is_admin(user):
            filtered_logs.extend(
                db.filter_admin_logs(
                    db.list_admin_audit_logs(self.db_path),
                    action_query=query.get("action", ""),
                    actor_query=query.get("actor", ""),
                    product_query=query.get("product", ""),
                )
            )
            filtered_logs.sort(key=lambda item: (item.get("created_at", ""), item.get("id", 0)), reverse=True)
        return filtered_logs

    def configured_c_api_token(self) -> str:
        return (db.get_setting(self.db_path, "c_api_token") or "").strip()

    def api_token_user(self, environ, query: dict) -> dict | None:
        token = self.api_token_from_request(environ, query)
        configured = self.configured_c_api_token()
        if not token or not configured or not secrets.compare_digest(token, configured):
            return None
        return {
            "id": 0,
            "username": "c_api_token",
            "display_name": "C 部门接口令牌",
            "department": "C",
        }

    def api_token_from_request(self, environ, query: dict) -> str:
        authorization = (environ.get("HTTP_AUTHORIZATION") or "").strip()
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return (query.get("access_token") or "").strip()

    def visible_fields_for_user(self, user):
        if user.get("department") == "C":
            return visible_fields_from_keys(self.configured_c_field_keys())
        return visible_fields_for_department(user["department"])

    def visible_products_for_user(self, products: list[dict], user: dict) -> list[dict]:
        visible_products = [product for product in products if can_see_product(user, product)]
        if user.get("department") != "C" or not visible_products:
            return visible_products
        if is_department_monitor(user):
            for product in visible_products:
                product["c_received"] = product.get("status") == "received"
            return visible_products
        receipt_releases = db.c_receipt_release_numbers(self.db_path, int(user["id"]))
        for product in visible_products:
            release_no = int(product.get("c_release_no") or 0)
            product["c_received"] = release_no in receipt_releases.get(int(product["id"]), set())
        return visible_products

    def c_effective_status(self, product: dict, user: dict) -> str:
        status = str(product.get("status") or "")
        if is_department_monitor(user):
            return status
        if user.get("department") == "C" and status in {"published", "received"}:
            return "received" if product.get("c_received") else "published"
        return status

    def product_payload_for_user(self, product: dict, user: dict) -> dict:
        visible_fields = self.visible_fields_for_c_product(product) if user.get("department") == "C" else self.visible_fields_for_user(user)
        effective_status = self.c_effective_status(product, user)
        payload = {
            "id": product.get("id"),
            "owner_department": product.get("owner_department"),
            "creator_name": product.get("creator_name"),
            "creator_username": product.get("creator_username"),
            "created_at": product.get("created_at"),
            "updated_at": product.get("updated_at"),
            "status": effective_status,
            "status_label": status_label(effective_status),
            "revision_flag": int(product.get("revision_flag") or 0),
            "revision_label": "已更新" if int(product.get("revision_flag") or 0) else "",
            "elapsed_days": self.elapsed_days_value(product.get("created_at"), product.get("completed_to_c_at")),
            "elapsed_days_label": self.elapsed_days_label(product.get("created_at"), product.get("completed_to_c_at")),
            "last_reviewed_at": product.get("last_reviewed_at"),
            "reviewer_name": product.get("reviewer_name"),
            "image_gallery": self.image_gallery_values(product),
        }
        for field in visible_fields:
            field_value = product.get(field.key)
            if field.key == "completion_flag" and field_value is None:
                field_value = ""
            payload[field.key] = field_value
        return payload

    def elapsed_days_value(self, created_at, completed_to_c_at=None) -> int | None:
        return db.compute_elapsed_days(created_at, completed_to_c_at)

    def elapsed_days_label(self, created_at, completed_to_c_at=None) -> str:
        elapsed_days = self.elapsed_days_value(created_at, completed_to_c_at)
        if elapsed_days is None:
            return "未开始"
        return f"{elapsed_days} 天"

    def format_list_timestamp(self, value) -> str:
        clean_value = str(value or "").strip()
        if not clean_value:
            return ""
        parsed_value = db.parse_utc(clean_value)
        if not parsed_value:
            return clean_value
        return parsed_value.astimezone().strftime("%Y-%m-%d %H:%M")

    def format_list_date(self, value) -> str:
        clean_value = db.normalize_optional_date_text(value) or ""
        if not clean_value:
            return ""
        if len(clean_value) >= 10 and clean_value[:10].count("-") == 2:
            return clean_value[:10]
        parsed_value = db.parse_utc(clean_value)
        if not parsed_value:
            return clean_value
        return parsed_value.astimezone().strftime("%Y-%m-%d")

    def format_list_decimal(self, value) -> str:
        clean_value = str(value or "").strip()
        if not clean_value:
            return ""
        try:
            return f"{float(clean_value):.2f}"
        except ValueError:
            return clean_value

    def list_value_markup(
        self,
        value,
        *,
        link_href: str | None = None,
        link_label: str | None = None,
        mono: bool = False,
        extra_class: str = "",
    ) -> str:
        clean_value = str(value).strip() if value is not None else ""
        if clean_value == "":
            return '<span class="table-cell-empty" aria-hidden="true"></span>'
        title = html.escape(clean_value, quote=True)
        text = html.escape(link_label or clean_value)
        class_name = "table-cell-text"
        if mono:
            class_name = f"{class_name} table-cell-mono"
        if extra_class:
            class_name = f"{class_name} {extra_class}"
        if link_href:
            href = html.escape(str(link_href), quote=True)
            return f'<a class="table-cell-link" href="{href}" target="_blank" rel="noreferrer" title="{title}">{text}</a>'
        return f'<span class="{class_name}" title="{title}">{text}</span>'

    def list_layout_cell_markup(self, field, payload: dict) -> str:
        if field.key == "completion_flag":
            return self.list_value_markup(payload.get("completion_flag"), mono=True)
        if field.key in {"shooting_date", "inspection_date"}:
            return self.list_value_markup(
                self.format_list_date(payload.get(field.key)),
                mono=True,
                extra_class="table-cell-date",
            )
        if field.key == "tax_included_price":
            return self.list_value_markup(
                self.format_list_decimal(payload.get(field.key)),
                mono=True,
            )
        if field.key == "image_url":
            return self.list_value_markup(
                payload.get(field.key),
                link_href=payload.get(field.key),
                link_label="查看图片",
            )
        return self.list_value_markup(payload.get(field.key), mono=field.input_type == "number")

    def status_transition_validation_error(self, product: dict, target_status: str) -> str:
        b_stage_field_keys = set(B_STAGE_FIELD_KEYS)
        b_stage_field_order = ("category", "image_url", "launch_price", "launch_channel", "completion_flag")
        if target_status == "pending":
            missing_keys = db.completion_missing_field_keys(product, excluded_keys=b_stage_field_keys)
            if not missing_keys:
                return ""
            missing_labels = [PRODUCT_FIELD_MAP[key].label for key in missing_keys if key in PRODUCT_FIELD_MAP]
            preview = "、".join(missing_labels[:6])
            if len(missing_labels) > 6:
                preview = f"{preview} 等 {len(missing_labels)} 项"
            return f"跟单部资料还未完成，暂不能提交给商品部。请先补齐这些字段：{preview}。"
        if target_status == "published":
            missing_keys = db.completion_missing_field_keys(product, excluded_keys=b_stage_field_keys)
            if missing_keys:
                missing_labels = [PRODUCT_FIELD_MAP[key].label for key in missing_keys if key in PRODUCT_FIELD_MAP]
                preview = "、".join(missing_labels[:6])
                if len(missing_labels) > 6:
                    preview = f"{preview} 等 {len(missing_labels)} 项"
                return f"主体资料还未完成，暂不能流转给运营部。请先补齐这些字段：{preview}。"
            missing_fields = []
            for field_key in b_stage_field_order:
                if field_key == "image_url":
                    if db.product_has_image(product):
                        continue
                elif db.has_meaningful_value(product.get(field_key)):
                    continue
                if field_key in PRODUCT_FIELD_MAP:
                    missing_fields.append(PRODUCT_FIELD_MAP[field_key].label)
            if missing_fields:
                return f"商品部还未补齐 { '、'.join(missing_fields) }，暂不能流转给运营部。"
            if not normalize_launch_channel(product.get("launch_channel")):
                return "上新渠道仅支持天猫、唯品、同款，确认后才能流转给运营部。"
        return ""

    def revision_badge(self, product: dict | None) -> str:
        if not product or not int(product.get("revision_flag") or 0):
            return ""
        return '<span class="pill" style="background:linear-gradient(180deg, rgba(191,87,0,0.18), rgba(191,87,0,0.1)); color:#8a3e00;">已更新</span>'

    def version_label(self, product: dict | None) -> str:
        version_no = int((product or {}).get("current_version_no") or 1)
        return f"V{version_no}"

    def version_badges(self, version: dict, current_version_no: int) -> str:
        badges = []
        version_no = int(version.get("version_no") or 0)
        if version_no == current_version_no:
            badges.append('<span class="pill" style="background:linear-gradient(180deg, rgba(50,111,85,0.16), rgba(50,111,85,0.1)); color:#2f6f55;">当前版本</span>')
        if version_no == 1:
            badges.append('<span class="pill" style="background:linear-gradient(180deg, rgba(56,111,100,0.12), rgba(56,111,100,0.07)); color:#315f58;">初始版本</span>')
        source_version_no = version.get("source_version_no")
        if source_version_no:
            badges.append(f'<span class="pill" style="background:linear-gradient(180deg, rgba(181,106,45,0.16), rgba(181,106,45,0.08)); color:#8c562a;">恢复自 V{int(source_version_no)}</span>')
        return "".join(badges)

    def collect_checkbox_values(self, form: dict, field_name: str) -> list[str]:
        values = []
        for key, value in form.items():
            if key == field_name:
                if isinstance(value, list):
                    values.extend(str(item) for item in value)
                else:
                    values.append(str(value))
            elif key.startswith(field_name + "__"):
                values.append(str(value))
        return values

    def collect_numeric_values(self, form: dict, field_name: str) -> list[int]:
        values = []
        for value in self.collect_checkbox_values(form, field_name):
            if not str(value).isdigit():
                continue
            values.append(int(value))
        return values

    def parse_numeric_csv(self, raw_value: str) -> list[int]:
        values = []
        for item in str(raw_value or "").split(","):
            value = item.strip()
            if not value or not value.isdigit():
                continue
            values.append(int(value))
        return values

    def product_brief_label(self, product: dict | None) -> str:
        if not product:
            return "当前资料"
        style_code = str(product.get("style_code") or "").strip()
        product_name = str(product.get("product_name") or "").strip()
        if style_code and product_name:
            return f"{style_code} / {product_name}"
        if style_code:
            return style_code
        if product_name:
            return product_name
        product_id = product.get("id")
        if product_id:
            return f"资料 #{product_id}"
        return "当前资料"

    def append_bulk_skip_reason(self, bucket: list[str], product: dict | None, reason: str, *, limit: int = 3) -> None:
        clean_reason = str(reason or "").strip()
        if not clean_reason or len(bucket) >= limit:
            return
        message = f"{self.product_brief_label(product)}：{clean_reason}"
        if message in bucket:
            return
        bucket.append(message)

    def export_menu(self, user, compact: bool = False) -> str:
        summary_class = "export-menu-summary export-menu-summary-compact" if compact else "export-menu-summary"
        menu_class = "export-menu export-menu-compact" if compact else "export-menu"
        return f"""
        <details class="{menu_class}">
          <summary class="{summary_class}">导出 Excel</summary>
          <div class="export-menu-panel">
            <a class="export-menu-item" href="/export.xlsx">导出全部资料</a>
            <a class="export-menu-item" href="/export.xlsx?mode=selected" data-base-href="/export.xlsx?mode=selected" data-export-selected="1">导出勾选资料</a>
            <a class="export-menu-item" href="/export.xlsx?mode=selected&amp;include_images=1" data-base-href="/export.xlsx?mode=selected&amp;include_images=1" data-export-selected="1">导出勾选含图片</a>
          </div>
        </details>
        """

    def collect_gallery_values(self, form: dict, field_name: str) -> list[str]:
        indexed_values = []
        for key, value in form.items():
            if not key.startswith(field_name + "__"):
                continue
            suffix = key.split("__", 1)[1]
            sort_key = float("inf")
            if suffix.isdigit():
                sort_key = int(suffix)
            indexed_values.append((sort_key, str(value).strip()))
        indexed_values.sort(key=lambda item: item[0])
        values = []
        seen = set()
        for _, value in indexed_values:
            if not value or value in seen:
                continue
            values.append(value)
            seen.add(value)
        return values

    def normalize_gallery_values(self, values) -> list[str]:
        normalized = []
        seen = set()
        for value in values:
            item = str(value).strip()
            if not item or item in seen:
                continue
            normalized.append(item)
            seen.add(item)
        return normalized

    def image_gallery_values(self, source: dict) -> list[str]:
        raw_gallery = source.get("image_gallery_json")
        parsed = []
        if isinstance(raw_gallery, str) and raw_gallery.strip():
            try:
                parsed = json.loads(raw_gallery)
            except json.JSONDecodeError:
                parsed = []
        elif isinstance(raw_gallery, list):
            parsed = raw_gallery
        gallery_values = self.normalize_gallery_values(parsed)
        fallback = str(source.get("image_url") or "").strip()
        if fallback and fallback not in gallery_values:
            gallery_values.insert(0, fallback)
        return gallery_values

    def nav(self, user, current_page: str | None = None):
        brand_name = self.brand_config["brand_name"]
        brand_mark = self.brand_config["brand_mark"]
        brand_lines = [line.strip() for line in brand_name.splitlines() if line.strip()]
        if not brand_lines:
            brand_lines = [brand_name]
        brand_primary = brand_lines[0]
        brand_secondary_markup = ""
        if len(brand_lines) > 1:
            brand_secondary_markup = "".join(
                f'<div class="brand-subline brand-subline-seal">{html.escape(line)}</div>'
                for line in brand_lines[1:]
            )
        brand_mark_text = brand_mark
        module_entries = [
            """
            <a href="/products">
              <span class="nav-dropdown-title">板块一</span>
              <span class="nav-dropdown-note">商品资料后台</span>
            </a>
            """
        ]
        if can_access_billing_module(user):
            module_entries.append(
                """
                <a href="/billing">
                  <span class="nav-dropdown-title">板块二</span>
                  <span class="nav-dropdown-note">账单与结算</span>
                </a>
                """
            )
        else:
            module_entries.append(
                """
                <span class="nav-dropdown-link nav-dropdown-link-disabled">
                  <span class="nav-dropdown-title">板块二</span>
                  <span class="nav-dropdown-note">当前账号暂不可访问</span>
                </span>
                """
            )
        board_guide = f"""
        <details class="nav-menu">
          <summary>板块导览</summary>
          <div class="nav-dropdown">
            {''.join(module_entries)}
          </div>
        </details>
        """
        action_links = [
            '<li class="nav-chip"><a href="/modules">首页</a></li>',
            f'<li class="nav-chip nav-chip-dropdown">{board_guide}</li>',
        ]
        if is_admin(user):
            action_links.append('<li class="nav-chip"><a href="/users">账号管理</a></li>')
            action_links.append('<li class="nav-chip"><a href="/settings/c-fields">字段开放</a></li>')
        if can_view_logs(user):
            action_links.append('<li class="nav-chip"><a href="/logs">日志中心</a></li>')
        if not is_department_monitor(user):
            action_links.append('<li class="nav-chip"><a href="/profile/password">修改密码</a></li>')
        links = "".join(action_links)
        department_badge = department_label(user["department"])
        return f"""
        <nav class="nav-shell">
          <div class="nav-brand">
            <div class="brand-mark brand-mark-seal"><span class="brand-mark-label">{html.escape(brand_mark_text)}</span></div>
            <div>
              <div class="brand brand-hidden-label">{html.escape(brand_primary)}</div>
              {brand_secondary_markup}
            </div>
          </div>
          <div class="nav-actions">
            <ul class="nav-links">{links}</ul>
            <div class="nav-session">
              <div class="nav-role">
                <span class="meta-label">当前身份</span>
                <strong>{html.escape(user.get('username') or '')}</strong>
                <div class="nav-role-note">{html.escape(department_badge)}</div>
              </div>
              <form method="post" action="/logout"><button class="ghost-button ghost-danger" type="submit">退出登录</button></form>
            </div>
          </div>
        </nav>
        """

    @staticmethod
    def monitor_department_available_for_path(department: str, path: str) -> bool:
        if path.startswith("/billing/platform-bills"):
            return department in {"B", "C"}
        if path.startswith("/billing/brand-bills"):
            return department in {"A", "B"}
        if path.startswith("/billing/supplier-settlements"):
            return department == "A"
        if path.endswith("/logs") or path == "/logs":
            return department in {"A", "B"}
        return department in {"A", "B", "C"}

    def department_monitor_toolbar(self, user, current_page: str | None) -> str:
        if current_page not in {"modules", "products", "billing"}:
            return ""
        if not user or not (is_admin(user) or is_department_monitor(user)):
            return ""
        path = str(user.get("monitor_path") or "/modules")
        base_query = {
            str(key): str(value)
            for key, value in (user.get("monitor_query") or {}).items()
            if str(key) and str(value) != ""
        }
        active_department = str(user.get("monitor_department") or "")

        def target_url(department: str | None = None) -> str:
            params = dict(base_query)
            if department:
                params["monitor_department"] = department
            return path + (f"?{urlencode(params)}" if params else "")

        buttons = []
        for department in ("A", "B", "C"):
            label = department_label(department)
            if self.monitor_department_available_for_path(department, path):
                active_class = " department-monitor-button-active" if department == active_department else ""
                buttons.append(
                    f'<a class="department-monitor-button{active_class}" href="{html.escape(target_url(department), quote=True)}">{html.escape(label)}</a>'
                )
            else:
                buttons.append(
                    f'<span class="department-monitor-button department-monitor-button-disabled">{html.escape(label)}</span>'
                )
        mode_note = (
            f"正在查看{department_label(active_department)}工作区，只读模式"
            if active_department
            else "选择部门查看对应工作区与进度"
        )
        exit_markup = (
            f'<a class="department-monitor-exit" data-monitor-exit="1" href="{html.escape(target_url(), quote=True)}">返回管理员视图</a>'
            if active_department
            else ""
        )
        return f"""
        <section class="department-monitor-toolbar" aria-label="部门监控">
          <div class="department-monitor-title"><strong>部门监控</strong><span>{html.escape(mode_note)}</span></div>
          <div class="department-monitor-actions">{''.join(buttons)}{exit_markup}</div>
        </section>
        """

    def render_page_back_link(self, href: str, label: str = "返回上一层", note: str = "") -> str:
        note_markup = f'<span class="page-back-note">{html.escape(note)}</span>' if note else ""
        return f"""
        <div class="page-back-row">
          <a class="page-back-link" href="{html.escape(href, quote=True)}">&larr; {html.escape(label)}</a>
          {note_markup}
        </div>
        """

    def page(
        self,
        title: str,
        content: str,
        user=None,
        current_page: str | None = None,
        back_href: str | None = None,
        back_label: str = "返回上一层",
        back_note: str = "",
    ):
        nav = self.nav(user, current_page=current_page) if user else ""
        monitor_toolbar = self.department_monitor_toolbar(user, current_page) if user else ""
        back_link = self.render_page_back_link(back_href, back_label, back_note) if back_href else ""
        monitor_script = ""
        if is_department_monitor(user):
            monitor_department = json.dumps(str(user.get("monitor_department") or ""), ensure_ascii=False)
            monitor_script = f"""
            (() => {{
              const monitorDepartment = {monitor_department};
              document.querySelectorAll('a[href]').forEach((link) => {{
                const href = link.getAttribute('href') || '';
                if (!href || href.startsWith('#') || link.dataset.monitorExit === '1') return;
                const target = new URL(href, window.location.origin);
                if (target.origin !== window.location.origin || target.pathname === '/logout') return;
                if (!target.searchParams.has('monitor_department')) {{
                  target.searchParams.set('monitor_department', monitorDepartment);
                  link.setAttribute('href', target.pathname + target.search + target.hash);
                }}
              }});
              document.querySelectorAll('form[method="get"]').forEach((form) => {{
                form.addEventListener('submit', () => {{
                  if (form.querySelector('input[name="monitor_department"]')) return;
                  const field = document.createElement('input');
                  field.type = 'hidden';
                  field.name = 'monitor_department';
                  field.value = monitorDepartment;
                  form.appendChild(field);
                }});
              }});
            }})();
            """
        brand_name = self.brand_config["brand_name"]
        accent = self.brand_config["accent"]
        accent_strong = self.brand_config["accent_strong"]
        accent_deep = self.brand_config["accent_deep"]
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title.replace('商品资料后台', brand_name))}</title>
  <style>
    :root {{
      --bg: #f6f1e8;
      --paper: rgba(255, 251, 245, 0.9);
      --paper-strong: rgba(255, 248, 238, 0.98);
      --paper-soft: rgba(255, 255, 255, 0.74);
      --ink: #241b14;
      --muted: #786d62;
      --line: rgba(94, 67, 40, 0.14);
      --accent: {html.escape(accent)};
      --accent-strong: {html.escape(accent_strong)};
      --accent-deep: {html.escape(accent_deep)};
      --success: #2f6f55;
      --warning: #9d612d;
      --shadow: 0 28px 70px rgba(88, 56, 31, 0.12);
      --shadow-soft: 0 12px 32px rgba(102, 72, 44, 0.08);
      --radius: 28px;
      --radius-sm: 18px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at 12% 10%, rgba(228, 177, 108, 0.28), transparent 24%),
        radial-gradient(circle at 88% 16%, rgba(56, 111, 100, 0.14), transparent 20%),
        radial-gradient(circle at 78% 88%, rgba(181, 106, 45, 0.16), transparent 22%),
        linear-gradient(180deg, #fbf7f1 0%, var(--bg) 54%, #f4ede3 100%);
      font-family: "Avenir Next", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      min-height: 100vh;
    }}
    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      background-image:
        linear-gradient(rgba(255,255,255,0.14) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.14) 1px, transparent 1px);
      background-size: 32px 32px;
      mask-image: linear-gradient(180deg, rgba(0,0,0,0.18), transparent 75%);
    }}
    a {{ color: var(--accent-strong); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .shell {{ max-width: 1440px; margin: 0 auto; padding: 24px 24px 64px; }}
    .page-back-row {{
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
      margin: -6px 0 18px;
    }}
    .page-back-link {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 16px;
      border-radius: 999px;
      background: rgba(255,255,255,0.82);
      border: 1px solid rgba(94, 67, 40, 0.12);
      color: var(--accent-strong);
      font-size: 13px;
      font-weight: 700;
      text-decoration: none;
      box-shadow: var(--shadow-soft);
      transition: transform 160ms ease, background 160ms ease, box-shadow 160ms ease;
    }}
    .page-back-link:hover {{
      text-decoration: none;
      background: rgba(255,255,255,0.96);
      transform: translateY(-1px);
      box-shadow: 0 16px 28px rgba(91, 58, 29, 0.10);
    }}
    .page-back-note {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
    .department-monitor-toolbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
      margin: -10px 0 18px;
      padding: 13px 16px;
      border: 1px solid rgba(181, 106, 45, 0.16);
      border-radius: 18px;
      background: rgba(255, 249, 240, 0.74);
      box-shadow: var(--shadow-soft);
    }}
    .department-monitor-title {{
      display: flex;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 8px;
    }}
    .department-monitor-title strong {{
      font-size: 14px;
      color: var(--accent-strong);
    }}
    .department-monitor-title span {{
      color: var(--muted);
      font-size: 13px;
    }}
    .department-monitor-actions {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 8px;
      flex-wrap: wrap;
    }}
    .department-monitor-button,
    .department-monitor-exit {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 34px;
      padding: 7px 13px;
      border-radius: 11px;
      border: 1px solid rgba(94, 67, 40, 0.12);
      background: rgba(255,255,255,0.78);
      color: var(--ink);
      font-size: 13px;
      font-weight: 700;
      text-decoration: none;
    }}
    .department-monitor-button:hover,
    .department-monitor-exit:hover {{
      text-decoration: none;
      background: rgba(255,255,255,0.98);
    }}
    .department-monitor-button-active {{
      border-color: rgba(181, 106, 45, 0.38);
      background: rgba(226, 187, 134, 0.28);
      color: var(--accent-strong);
    }}
    .department-monitor-button-disabled {{
      color: rgba(120, 109, 98, 0.64);
      background: rgba(255,255,255,0.44);
      cursor: not-allowed;
    }}
    .department-monitor-exit {{
      color: var(--accent-strong);
      border-color: rgba(181, 106, 45, 0.22);
    }}
    .nav-shell {{
      display: grid;
      grid-template-columns: minmax(220px, 280px) 1fr;
      gap: 20px;
      align-items: center;
      padding: 24px 26px;
      background: linear-gradient(180deg, rgba(255,251,245,0.9), rgba(255,248,239,0.82));
      border: 1px solid rgba(94, 67, 40, 0.1);
      border-radius: calc(var(--radius) + 8px);
      box-shadow: var(--shadow);
      backdrop-filter: blur(16px);
      margin-bottom: 26px;
      position: sticky;
      top: 12px;
      z-index: 20;
      overflow: visible;
      isolation: isolate;
    }}
    .nav-shell::before {{
      content: "远山含黛色，玲珑晓楼阁";
      position: absolute;
      left: 50%;
      top: 52%;
      z-index: 0;
      color: rgba(91, 62, 35, 0.008);
      font-family: "STXingkaiSC-Light", "行楷-简 细体", "STXingkai", "WangXizhi", "王羲之行书", "FZXingKai-S04S", "STKaiti", cursive;
      font-size: 30px;
      font-weight: 400;
      font-synthesis: none;
      letter-spacing: 0;
      line-height: 1;
      white-space: nowrap;
      transform: translate(-50%, -50%);
      pointer-events: none;
      user-select: none;
    }}
    .nav-shell > * {{
      position: relative;
      z-index: 1;
    }}
    .nav-brand {{
      display: flex;
      align-items: center;
      gap: 16px;
    }}
    .brand-mark {{
      width: 58px;
      height: 58px;
      border-radius: 20px;
      display: grid;
      place-items: center;
      font-weight: 800;
      letter-spacing: 0.08em;
      color: #fff8ee;
      background:
        linear-gradient(135deg, var(--accent) 0%, #d59647 50%, var(--accent-deep) 100%);
      box-shadow: 0 14px 30px rgba(96, 64, 35, 0.18);
    }}
    .brand-mark-seal {{
      width: 76px;
      height: 76px;
      border-radius: 22px;
      padding: 8px;
      font-family: "Avenir Next", "Helvetica Neue", sans-serif;
      font-size: 18px;
      font-weight: 700;
      letter-spacing: 0.04em;
      line-height: 1;
      text-align: center;
      color: #92541e;
      white-space: normal;
      background: rgba(255, 251, 245, 0.78);
      border: 1px solid rgba(188, 108, 37, 0.2);
      box-shadow:
        0 8px 18px rgba(102, 72, 44, 0.08),
        inset 0 1px 0 rgba(255,255,255,0.64);
    }}
    .brand-mark-seal .brand-mark-label {{
      display: inline-block;
      transform: translateX(-2px);
    }}
    .brand {{
      font-size: 23px;
      font-weight: 700;
      letter-spacing: 0.08em;
      line-height: 1;
      color: rgba(36, 27, 20, 0.76);
    }}
    .brand-hidden-label {{
      display: none;
    }}
    .brand-subline {{
      margin-top: 2px;
      font-size: 44px;
      font-weight: 780;
      letter-spacing: 0.02em;
      line-height: 1.02;
      color: #6d3410;
      text-shadow: 0 1px 2px rgba(82, 41, 16, 0.08);
    }}
    .brand-subline-seal {{
      font-family: "WeibeiSC-Bold", "魏碑-简", "WeibeiTC-Bold", "魏碑-繁", serif;
      font-variant-east-asian: traditional;
    }}
    .meta {{
      color: var(--muted);
      margin-top: 6px;
      font-size: 14px;
      line-height: 1.6;
    }}
    .meta-label {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      margin-bottom: 6px;
    }}
    .nav-actions {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 14px;
      flex-wrap: wrap;
      width: 100%;
    }}
    .nav-session {{
      display: inline-flex;
      align-items: stretch;
      gap: 10px;
      margin-left: auto;
    }}
    .nav-role {{
      display: flex;
      flex-direction: column;
      justify-content: center;
      padding: 12px 16px;
      min-width: 170px;
      border-radius: 18px;
      background: rgba(255,255,255,0.76);
      border: 1px solid rgba(94, 67, 40, 0.12);
      box-shadow: var(--shadow-soft);
    }}
    .nav-role strong {{
      display: block;
      font-size: 15px;
      line-height: 1.25;
    }}
    .nav-role-note {{
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
    .nav-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      list-style: none;
      margin: 0;
      padding: 0;
      justify-content: flex-end;
      flex: 1 1 480px;
    }}
    .nav-chip {{
      position: relative;
      padding: 0;
      border-radius: 999px;
      background: rgba(255,255,255,0.7);
      border: 1px solid rgba(140, 90, 43, 0.12);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.45);
    }}
    .nav-chip a {{
      display: inline-flex;
      align-items: center;
      padding: 11px 16px;
      font-weight: 600;
    }}
    .nav-chip-dropdown {{
      overflow: visible;
    }}
    .nav-menu {{
      position: relative;
    }}
    .nav-menu summary {{
      list-style: none;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 11px 16px;
      font-weight: 600;
      color: var(--accent-strong);
      cursor: pointer;
      user-select: none;
    }}
    .nav-menu summary::-webkit-details-marker {{
      display: none;
    }}
    .nav-menu summary::after {{
      content: "▾";
      font-size: 12px;
      color: var(--muted);
      transition: transform 160ms ease;
    }}
    .nav-menu[open] summary::after {{
      transform: rotate(-180deg);
    }}
    .nav-dropdown {{
      position: absolute;
      top: calc(100% + 10px);
      right: 0;
      min-width: 230px;
      display: grid;
      gap: 8px;
      padding: 12px;
      border-radius: 22px;
      background: rgba(255, 250, 244, 0.98);
      border: 1px solid rgba(94, 67, 40, 0.12);
      box-shadow: var(--shadow-soft);
      backdrop-filter: blur(14px);
      z-index: 20;
    }}
    .nav-dropdown::before {{
      content: "";
      position: absolute;
      left: 0;
      right: 0;
      bottom: 100%;
      height: 10px;
    }}
    .nav-dropdown a,
    .nav-dropdown-link {{
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 4px;
      padding: 12px 14px;
      border-radius: 16px;
      background: rgba(255,255,255,0.78);
      border: 1px solid rgba(94, 67, 40, 0.1);
    }}
    .nav-dropdown a:hover {{
      text-decoration: none;
      background: rgba(255,255,255,0.98);
      transform: translateY(-1px);
    }}
    .nav-dropdown-title {{
      font-size: 14px;
      font-weight: 700;
      color: var(--ink);
    }}
    .nav-dropdown-note {{
      font-size: 12px;
      color: var(--muted);
      line-height: 1.4;
    }}
    .nav-dropdown-link-disabled {{
      opacity: 0.72;
      cursor: not-allowed;
    }}
    .panel {{
      background: linear-gradient(180deg, var(--paper) 0%, rgba(255, 249, 242, 0.94) 100%);
      border: 1px solid rgba(94, 67, 40, 0.1);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 26px;
      backdrop-filter: blur(14px);
      position: relative;
      overflow: hidden;
    }}
    .panel::before {{
      content: "";
      position: absolute;
      inset: 0 auto auto 0;
      width: 120px;
      height: 120px;
      background: radial-gradient(circle, rgba(232, 183, 126, 0.18), transparent 72%);
      pointer-events: none;
    }}
    .hero {{
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(340px, 0.85fr);
      gap: 20px;
      margin-bottom: 22px;
    }}
    .billing-admin-hero {{
      grid-template-columns: minmax(0, 1fr);
      margin-bottom: 16px;
    }}
    .billing-admin-hero .panel > p {{
      max-width: 920px;
      margin-bottom: 0;
    }}
    .hero h1, .panel h1, .panel h2, .panel h3 {{ margin-top: 0; }}
    .eyebrow {{
      display: none;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
    }}
    .stat-card {{
      padding: 18px 18px 16px;
      border-radius: 22px;
      background:
        linear-gradient(180deg, rgba(255,255,255,0.92), rgba(255,247,236,0.96)),
        linear-gradient(135deg, rgba(181,106,45,0.08), rgba(46,95,88,0.05));
      border: 1px solid rgba(94, 67, 40, 0.1);
      box-shadow: var(--shadow-soft);
      position: relative;
      overflow: hidden;
    }}
    .stat-card::after {{
      content: "";
      position: absolute;
      right: -14px;
      top: -14px;
      width: 56px;
      height: 56px;
      border-radius: 18px;
      background: linear-gradient(135deg, rgba(181,106,45,0.12), rgba(46,95,88,0.02));
    }}
    .stat-card strong {{
      display: block;
      font-size: 32px;
      margin-top: 12px;
      line-height: 1;
    }}
    .stat-card span {{
      position: relative;
      z-index: 1;
      color: var(--muted);
      font-size: 13px;
    }}
    .tools {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 18px;
      position: relative;
      z-index: 1;
    }}
    .tools form {{
      display: grid;
      grid-template-columns: minmax(220px, 1.3fr) repeat(3, minmax(150px, 0.8fr)) minmax(130px, 0.7fr);
      gap: 10px;
      width: 100%;
    }}
    input, select, textarea, button {{
      width: 100%;
      font: inherit;
      border-radius: 16px;
      border: 1px solid rgba(91, 58, 29, 0.14);
      padding: 13px 15px;
      background: rgba(255, 255, 255, 0.95);
      color: var(--ink);
    }}
    input:focus, select:focus, textarea:focus {{
      outline: 3px solid rgba(181, 106, 45, 0.12);
      border-color: rgba(181, 106, 45, 0.46);
    }}
    textarea {{ min-height: 112px; resize: vertical; }}
    button {{
      background: var(--accent);
      color: white;
      font-weight: 700;
      cursor: pointer;
      transition: transform 160ms ease, box-shadow 160ms ease, filter 160ms ease;
      box-shadow: 0 16px 28px rgba(91, 58, 29, 0.18);
    }}
    button:hover {{
      transform: translateY(-1px);
      filter: saturate(1.05);
    }}
    .ghost-button {{
      background: rgba(255,255,255,0.78);
      color: var(--ink);
      box-shadow: none;
      border-color: rgba(91, 58, 29, 0.12);
    }}
    .ghost-danger {{
      min-width: 108px;
      height: 100%;
      padding-inline: 18px;
      background: rgba(255,255,255,0.84);
    }}
    .export-menu {{
      position: relative;
    }}
    .export-menu[open] {{
      z-index: 3;
    }}
    .export-menu-compact {{
      display: inline-flex;
    }}
    .export-menu-summary {{
      list-style: none;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 11px 16px;
      min-height: 48px;
      border-radius: 999px;
      background: var(--accent);
      color: #fff;
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 16px 28px rgba(91, 58, 29, 0.18);
      user-select: none;
    }}
    .export-menu-summary-compact {{
      padding: 11px 16px;
      min-height: 0;
      box-shadow: none;
      background: rgba(255,255,255,0.78);
      color: var(--ink);
      border: 1px solid rgba(91, 58, 29, 0.12);
    }}
    .export-menu-summary-compact::after {{
      color: var(--muted);
    }}
    .export-menu-summary::-webkit-details-marker {{
      display: none;
    }}
    .export-menu-summary::after {{
      content: "▾";
      font-size: 12px;
      opacity: 0.9;
    }}
    .export-menu-panel {{
      position: absolute;
      top: calc(100% + 10px);
      left: 0;
      min-width: 190px;
      display: grid;
      gap: 8px;
      padding: 10px;
      border-radius: 18px;
      background: rgba(255, 251, 245, 0.98);
      border: 1px solid rgba(94, 67, 40, 0.12);
      box-shadow: var(--shadow-soft);
    }}
    .export-menu-panel::before {{
      content: "";
      position: absolute;
      left: 0;
      right: 0;
      bottom: 100%;
      height: 10px;
    }}
    .export-menu-item {{
      display: inline-flex;
      align-items: center;
      justify-content: flex-start;
      width: 100%;
      padding: 11px 14px;
      border-radius: 14px;
      border: 1px solid rgba(91, 58, 29, 0.1);
      background: rgba(255,255,255,0.86);
      color: var(--ink);
      text-decoration: none;
      font-weight: 600;
      box-shadow: none;
    }}
    .export-menu-item:hover {{
      text-decoration: none;
      background: rgba(255,247,236,0.96);
      transform: none;
      filter: none;
    }}
    .form-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
    }}
    .field {{
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .field-wide {{
      grid-column: 1 / -1;
    }}
    .table-wrap {{
      overflow-x: auto;
      max-width: 100%;
      border-radius: 22px;
      border: 1px solid rgba(94, 67, 40, 0.12);
      background: rgba(255,255,255,0.72);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.54);
    }}
    .products-list-scroll-wrap {{
      position: relative;
      max-height: clamp(360px, 56vh, 600px);
      overflow-x: auto;
      overflow-y: scroll;
      scrollbar-gutter: stable;
      overscroll-behavior: contain;
      scrollbar-width: thin;
      scrollbar-color: rgba(127, 59, 8, 0.55) rgba(188, 108, 37, 0.08);
    }}
    .products-list-scroll-wrap::-webkit-scrollbar {{
      width: 12px;
      height: 12px;
    }}
    .products-list-scroll-wrap::-webkit-scrollbar-track {{
      background: rgba(188, 108, 37, 0.08);
      border-radius: 999px;
    }}
    .products-list-scroll-wrap::-webkit-scrollbar-thumb {{
      background: linear-gradient(180deg, rgba(127, 59, 8, 0.78), rgba(188, 108, 37, 0.54));
      border-radius: 999px;
      border: 2px solid rgba(255,255,255,0.72);
    }}
    .products-list-scroll-wrap::-webkit-scrollbar-thumb:hover {{
      background: linear-gradient(180deg, rgba(127, 59, 8, 0.88), rgba(188, 108, 37, 0.68));
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 980px;
      background: rgba(255,255,255,0.56);
    }}
    th, td {{
      padding: 15px 14px;
      border-bottom: 1px solid rgba(94, 67, 40, 0.1);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      position: sticky;
      top: 0;
      background: rgba(247, 241, 233, 0.96);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}
    .catalog-table {{
      width: max-content;
      min-width: 100%;
      table-layout: fixed;
      min-width: 1180px;
      background: rgba(255,255,255,0.9);
    }}
    .catalog-table thead th {{
      position: sticky;
      top: 0;
      z-index: 3;
      white-space: nowrap;
      font-size: 13px;
      letter-spacing: 0.04em;
      padding: 10px 18px 10px 10px;
      text-align: center;
      vertical-align: middle;
      overflow: visible;
      box-shadow: inset 0 -1px 0 rgba(94, 67, 40, 0.08);
    }}
    .catalog-table tbody tr {{
      transition: background 160ms ease;
    }}
    .catalog-table tbody tr:nth-child(even) {{
      background: rgba(248, 243, 236, 0.48);
    }}
    .catalog-table tbody tr:hover {{
      background: rgba(255, 248, 238, 0.92);
    }}
    .catalog-table td {{
      padding: 8px 10px;
      vertical-align: middle;
      font-size: 13px;
      line-height: 1.32;
    }}
    .catalog-table .table-id-cell,
    .catalog-table .table-status-cell,
    .catalog-table .table-version-cell,
    .catalog-table .table-days-cell,
    .catalog-table .table-initiator-cell,
    .catalog-table .table-owner-cell,
    .catalog-table .table-user-cell,
    .catalog-table .table-updated-cell,
    .catalog-table .table-actions-cell {{
      white-space: nowrap;
    }}
    .catalog-table .table-id-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 48px;
      padding: 4px 8px;
      border-radius: 12px;
      background: rgba(53, 95, 82, 0.08);
      color: var(--accent-deep);
      font-weight: 700;
    }}
    .catalog-table .table-cell-text,
    .catalog-table .table-cell-link {{
      display: -webkit-box;
      max-width: 168px;
      overflow: hidden;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      word-break: break-word;
      line-height: 1.32;
    }}
    .catalog-table .table-cell-link {{
      color: var(--accent-deep);
      text-decoration: none;
      font-weight: 600;
    }}
    .catalog-table .table-cell-link:hover {{
      text-decoration: underline;
    }}
    .catalog-table .table-cell-empty {{
      display: block;
      min-height: 14px;
    }}
    .catalog-table .table-cell-mono {{
      font-variant-numeric: tabular-nums;
    }}
    .catalog-table .table-cell-date {{
      max-width: 96px;
    }}
    .catalog-table .table-version-stack {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .catalog-table .table-version-label {{
      display: inline-flex;
      align-items: center;
      padding: 4px 8px;
      border-radius: 12px;
      background: rgba(91, 58, 29, 0.06);
      color: var(--ink);
      font-weight: 700;
    }}
    .catalog-table .table-action-links {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      flex-wrap: nowrap;
    }}
    .catalog-table .table-action-links a,
    .catalog-table .table-action-links button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 3px 7px;
      border-radius: 12px;
      border: 1px solid rgba(91, 58, 29, 0.1);
      background: rgba(255,255,255,0.86);
      color: var(--accent-deep);
      text-decoration: none;
      font-weight: 600;
      cursor: pointer;
      font: inherit;
      font-size: 12px;
    }}
    .catalog-table .table-action-links a:hover,
    .catalog-table .table-action-links button:hover {{
      background: rgba(255,247,236,0.96);
      text-decoration: none;
    }}
    .catalog-table .table-action-links .table-action-danger {{
      color: #a63f1a;
      border-color: rgba(166, 63, 26, 0.18);
      background: rgba(255, 245, 241, 0.96);
    }}
    .catalog-table .table-action-links .table-action-danger:hover {{
      background: rgba(255, 234, 226, 0.98);
    }}
    .catalog-table .table-action-links .table-action-receive {{
      color: var(--success);
      border-color: rgba(47, 111, 85, 0.2);
      background: rgba(230, 244, 235, 0.96);
    }}
    .catalog-table .table-action-links .table-action-receive:hover {{
      background: rgba(214, 237, 222, 0.98);
    }}
    .catalog-table .pill {{
      padding: 5px 9px;
      font-size: 11px;
    }}
    .catalog-table .catalog-resizable-head {{
      user-select: none;
    }}
    .catalog-table .catalog-resize-handle {{
      position: absolute;
      top: 3px;
      right: -2px;
      bottom: 3px;
      width: 10px;
      padding: 0;
      border: none;
      border-radius: 999px;
      background: transparent;
      box-shadow: none;
      cursor: col-resize;
      opacity: 0;
      transition: opacity 120ms ease, background 120ms ease;
      z-index: 2;
    }}
    .catalog-table .catalog-resize-handle::before {{
      content: "";
      position: absolute;
      top: 0;
      bottom: 0;
      left: 4px;
      width: 2px;
      border-radius: 999px;
      background: rgba(127, 59, 8, 0.22);
    }}
    .catalog-table thead th:hover .catalog-resize-handle,
    .catalog-table .catalog-resize-handle:focus-visible {{
      opacity: 1;
      background: rgba(188, 108, 37, 0.08);
      outline: none;
      filter: none;
      transform: none;
    }}
    body.catalog-resizing,
    body.catalog-resizing * {{
      cursor: col-resize !important;
      user-select: none !important;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 11px;
      border-radius: 999px;
      background: linear-gradient(180deg, rgba(181,106,45,0.1), rgba(181,106,45,0.06));
      color: var(--accent-strong);
      font-size: 12px;
      font-weight: 700;
    }}
    .notice {{
      padding: 15px 18px;
      border-radius: 18px;
      background: rgba(50, 111, 85, 0.1);
      color: var(--success);
      margin-bottom: 18px;
      border: 1px solid rgba(50, 111, 85, 0.16);
    }}
    .warning {{
      padding: 15px 18px;
      border-radius: 18px;
      background: rgba(157, 97, 45, 0.08);
      color: var(--warning);
      margin-bottom: 18px;
      border: 1px solid rgba(157, 97, 45, 0.14);
    }}
    .board-row-warning {{
      background: rgba(191, 124, 48, 0.10) !important;
    }}
    .board-row-danger {{
      background: rgba(171, 74, 49, 0.10) !important;
    }}
    .board-row-done {{
      background: rgba(50, 111, 85, 0.08) !important;
    }}
    .board-risk-pill {{
      display: inline-flex;
      align-items: center;
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .board-risk-normal {{
      background: rgba(53, 95, 82, 0.08);
      color: var(--accent-deep);
    }}
    .board-risk-warning {{
      background: rgba(191, 124, 48, 0.14);
      color: #8a5208;
    }}
    .board-risk-danger {{
      background: rgba(171, 74, 49, 0.14);
      color: #9a2f23;
    }}
    .board-risk-done {{
      background: rgba(50, 111, 85, 0.14);
      color: var(--success);
    }}
    .error-list {{
      margin: 0 0 18px;
      padding-left: 20px;
      color: #8b2d2d;
    }}
    .detail-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
    }}
    .modules-home {{
      display: flex;
      flex-direction: column;
      min-height: calc(100vh - 190px);
    }}
    .modules-home-watermark {{
      position: relative;
      display: flex;
      align-items: flex-end;
      justify-content: center;
      min-height: 156px;
      margin-top: auto;
      padding: 38px 0 8px;
      overflow: hidden;
      pointer-events: none;
      user-select: none;
    }}
    .modules-home-watermark::before {{
      content: "";
      position: absolute;
      left: 16%;
      right: 16%;
      bottom: 74px;
      border-top: 1px solid rgba(113, 78, 43, 0.07);
    }}
    /* V1 行书版：行楷细体、水印低墨色、底部居中。 */
    .modules-home-watermark span {{
      position: relative;
      z-index: 1;
      color: rgba(91, 62, 35, 0.05);
      font-family: "STXingkaiSC-Light", "行楷-简 细体", "STXingkai", "WangXizhi", "王羲之行书", "FZXingKai-S04S", "STKaiti", cursive;
      font-size: 42px;
      font-weight: 400;
      font-synthesis: none;
      -webkit-text-stroke: 0.52px rgba(91, 62, 35, 0.06);
      letter-spacing: 0;
      line-height: 1.2;
      white-space: nowrap;
      text-shadow: 0 1px 1px rgba(255, 251, 244, 0.62);
      transform: none;
    }}
    .billing-b-home {{
      width: 100%;
    }}
    .billing-b-home-back {{
      margin: -6px 0 18px;
    }}
    .billing-b-workboard .catalog-table {{
      width: 100%;
      min-width: 0;
      table-layout: fixed;
    }}
    .billing-b-workboard .catalog-table th,
    .billing-b-workboard .catalog-table td {{
      width: 25%;
      text-align: center;
      vertical-align: middle;
      font-size: 14px;
    }}
    .billing-b-workboard .board-risk-pill {{
      font-size: 12px;
    }}
    .billing-b-workboard .catalog-table .meta {{
      font-size: 14px;
    }}
    .billing-b-entry-grid {{
      width: 100%;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .billing-a-entry-grid {{
      width: 100%;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .billing-a-home {{
      width: 100%;
    }}
    .supplier-settlement-layout {{
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(440px, 0.95fr);
      grid-template-areas:
        "main query"
        "master query";
      gap: 18px;
      align-items: stretch;
      width: 100%;
    }}
    .supplier-settlement-columns {{
      display: contents;
    }}
    .supplier-settlement-main-panel {{
      grid-area: main;
    }}
    .supplier-bill-query-panel {{
      grid-area: query;
      height: 100%;
    }}
    .supplier-master-panel {{
      grid-area: master;
    }}
    .supplier-settlement-layout .panel {{
      margin-top: 0 !important;
    }}
    .query-anchor {{
      scroll-margin-top: 116px;
    }}
    .supplier-card-head,
    .supplier-summary-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      flex-wrap: wrap;
    }}
    .supplier-card-head {{
      margin-bottom: 18px;
    }}
    .supplier-card-head h1,
    .supplier-card-head h2,
    .supplier-summary-head h3 {{
      margin: 0;
    }}
    .supplier-import-block {{
      padding-top: 18px;
      border-top: 1px solid rgba(94, 67, 40, 0.1);
    }}
    .supplier-bill-import-actions,
    .supplier-bill-query-actions {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      margin-top: 14px;
    }}
    .supplier-bill-import-actions button,
    .supplier-bill-query-actions button {{
      grid-column: 2;
      width: 100%;
    }}
    .supplier-bill-import-action-pair {{
      grid-column: 2;
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }}
    .supplier-bill-import-action-pair button {{
      grid-column: auto;
    }}
    .supplier-bill-import-action-pair-single {{
      grid-template-columns: 1fr;
    }}
    .supplier-bill-delete-note {{
      margin: 10px 0 0;
    }}
    .supplier-summary-block {{
      margin-top: 20px;
      padding-top: 20px;
      border-top: 1px solid rgba(94, 67, 40, 0.1);
    }}
    .supplier-summary-head {{
      margin-bottom: 14px;
    }}
    .supplier-detail-export {{
      padding: 9px 14px;
      font-size: 14px;
    }}
    .supplier-master-section + .supplier-master-section {{
      margin-top: 22px;
      padding-top: 22px;
      border-top: 1px solid rgba(94, 67, 40, 0.1);
    }}
    .supplier-master-section h3 {{
      margin: 0 0 14px;
      font-size: 18px;
    }}
    .supplier-master-section-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }}
    .supplier-master-section-head h3 {{
      margin: 0;
    }}
    .supplier-master-import-form {{
      display: grid;
      gap: 12px;
    }}
    .supplier-master-create-row {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      align-items: end;
    }}
    .supplier-master-file-field {{
      height: 52px;
      justify-content: center;
    }}
    .supplier-master-file-field input {{
      width: 100%;
      height: 52px;
      min-height: 52px;
      padding: 10px 12px;
    }}
    .supplier-master-import-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      justify-content: flex-end;
    }}
    .supplier-master-import-actions button {{
      width: auto;
      min-width: 112px;
    }}
    .supplier-master-import-button {{
      min-height: 48px;
    }}
    .supplier-master-single-link {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 100%;
      height: 52px;
      min-height: 52px;
      padding: 11px 16px;
      border-radius: 16px;
      color: var(--ink);
      background: rgba(255,255,255,0.82);
      border: 1px solid rgba(94, 67, 40, 0.14);
      box-shadow: none;
      font-size: 14px;
      font-weight: 800;
    }}
    .supplier-master-single-link:hover {{
      color: var(--ink);
      text-decoration: none;
      background: rgba(255,255,255,0.96);
    }}
    .supplier-master-query-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }}
    .supplier-master-query-form {{
      display: grid;
      align-content: start;
      gap: 10px;
    }}
    .supplier-master-query-form + .supplier-master-query-form {{
      padding-left: 16px;
      border-left: 1px solid rgba(94, 67, 40, 0.1);
    }}
    .supplier-master-query-form h4 {{
      margin: 0;
      font-size: 14px;
    }}
    .supplier-master-query-form button {{
      width: 100%;
    }}
    .supplier-master-results {{
      margin-top: 18px;
    }}
    .supplier-query-summary {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}
    .supplier-query-summary .stat-card {{
      min-height: 112px;
      justify-content: center;
    }}
    .supplier-query-summary .stat-card strong {{
      font-variant-numeric: tabular-nums;
      font-size: 30px;
    }}
    .supplier-name-options {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      min-height: 44px;
      padding: 8px;
      border: 1px solid rgba(94, 67, 40, 0.12);
      border-radius: 16px;
      background: rgba(255,255,255,0.64);
    }}
    .supplier-name-option {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 7px 10px;
      border-radius: 10px;
      background: rgba(255,250,244,0.9);
      border: 1px solid rgba(94, 67, 40, 0.1);
      color: var(--ink);
      font-size: 13px;
      font-weight: 600;
    }}
    .supplier-name-option[hidden],
    .supplier-name-empty[hidden] {{
      display: none !important;
    }}
    .supplier-name-option input {{
      width: auto;
      margin: 0;
    }}
    .supplier-name-empty {{
      display: flex;
      align-items: center;
      color: var(--muted);
      font-size: 13px;
      padding: 4px 6px;
    }}
    .supplier-master-table {{
      width: 100%;
      min-width: 0;
      table-layout: fixed;
    }}
    .supplier-master-table td {{
      text-align: center;
      vertical-align: middle;
    }}
    .supplier-master-table th,
    .supplier-master-table td {{
      width: 33.333%;
    }}
    .a-monthly-board-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      flex-wrap: wrap;
      margin-bottom: 20px;
    }}
    .a-monthly-board-head h1 {{
      margin: 0;
    }}
    .a-monthly-board-query {{
      display: flex;
      align-items: end;
      gap: 10px;
    }}
    .a-monthly-board-query label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .a-monthly-board-query input {{
      width: 112px;
    }}
    .a-monthly-board-query button {{
      width: auto;
      min-width: 78px;
      padding: 10px 16px;
    }}
    .a-monthly-board-table {{
      width: 100%;
      min-width: 1120px;
      table-layout: fixed;
    }}
    .a-monthly-board-table th,
    .a-monthly-board-table td {{
      text-align: center;
      vertical-align: middle;
      font-size: 14px;
    }}
    .a-monthly-board-table th:first-child {{
      width: 140px;
    }}
    .a-monthly-board-table tbody th {{
      text-align: left;
      white-space: nowrap;
    }}
    .a-monthly-board-table input {{
      width: 100%;
      min-width: 62px;
      padding: 8px 9px;
      border: 1px solid rgba(94, 67, 40, 0.14);
      border-radius: 8px;
      background: rgba(255,255,255,0.78);
      color: var(--ink);
      font: inherit;
      font-variant-numeric: tabular-nums;
      text-align: right;
    }}
    .a-monthly-board-table input:focus {{
      outline: 2px solid rgba(188, 108, 37, 0.22);
      border-color: var(--accent);
    }}
    .a-monthly-board-table .a-monthly-board-total {{
      background: rgba(238, 220, 191, 0.32);
      color: var(--accent-deep);
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }}
    .a-monthly-board-save {{
      display: flex;
      justify-content: flex-end;
      margin-top: 16px;
    }}
    .a-monthly-board-save button {{
      width: 200px;
      min-width: 200px;
      padding: 10px 18px;
    }}
    .billing-c-home-grid {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .detail-panel-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      flex-wrap: wrap;
      margin-bottom: 18px;
    }}
    .detail-panel-main {{
      flex: 1 1 420px;
      min-width: 280px;
    }}
    .detail-panel-main h2 {{
      margin-bottom: 8px;
    }}
    .detail-panel-tools {{
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 10px;
      margin-left: auto;
      flex: 0 1 auto;
    }}
    .detail-panel-tools .tools {{
      justify-content: flex-end;
      margin-bottom: 0;
    }}
    .detail-workflow-note {{
      margin-top: 6px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.65;
    }}
    .detail-summary-inline {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin: 14px 0 18px;
    }}
    .detail-summary-chip {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(255,255,255,0.78);
      border: 1px solid rgba(94, 67, 40, 0.12);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.52);
      font-size: 12px;
      line-height: 1.4;
      color: var(--muted);
    }}
    .detail-summary-chip strong {{
      color: var(--ink);
      font-size: 12px;
    }}
    .detail-card {{
      padding: 20px;
      border-radius: 22px;
      border: 1px solid rgba(94, 67, 40, 0.1);
      background: rgba(255,255,255,0.74);
      box-shadow: var(--shadow-soft);
    }}
    .detail-row {{
      padding: 10px 0;
      border-bottom: 1px dashed rgba(91, 58, 29, 0.14);
    }}
    .detail-row:last-child {{ border-bottom: none; }}
    .detail-label {{
      display: block;
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 4px;
    }}
    .login-shell {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 52px 20px;
      display: grid;
      grid-template-columns: minmax(0, 1.15fr) minmax(360px, 0.85fr);
      gap: 24px;
      align-items: stretch;
    }}
    .login-showcase {{
      padding: 34px;
      border-radius: 34px;
      color: #fff9f1;
      background:
        radial-gradient(circle at top right, rgba(255,255,255,0.18), transparent 26%),
        radial-gradient(circle at 20% 20%, rgba(255,206,145,0.16), transparent 24%),
        linear-gradient(135deg, #74411f 0%, #ab6a32 46%, #284e49 100%);
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }}
    .login-brand-lockup {{
      display: inline-flex;
      align-items: center;
      gap: 18px;
      margin: 12px 0 20px;
    }}
    .login-brand-kicker {{
      width: 104px;
      min-height: 104px;
      display: grid;
      place-items: center;
      padding: 10px;
      border-radius: 30px;
      font-family: "HanYi LiShu Fan", "Baoli SC", "STLiti", "LiSong Pro", "STKaiti", serif;
      font-size: 28px;
      font-weight: 900;
      letter-spacing: 0.18em;
      line-height: 1.1;
      text-align: center;
      color: #fff5e9;
      background:
        linear-gradient(135deg, rgba(120, 57, 25, 0.92) 0%, rgba(166, 92, 42, 0.94) 52%, rgba(46, 78, 70, 0.9) 100%);
      border: 1px solid rgba(255,255,255,0.2);
      box-shadow:
        0 16px 36px rgba(48, 22, 8, 0.24),
        inset 0 1px 0 rgba(255,255,255,0.18);
      white-space: pre-line;
    }}
    .login-brand-main {{
      font-family: "WeibeiSC-Bold", "魏碑-简", "WeibeiTC-Bold", "魏碑-繁", serif;
      font-size: 76px;
      font-weight: 780;
      line-height: 0.98;
      letter-spacing: 0.05em;
      color: #f7ebd7;
      text-shadow: 0 2px 4px rgba(46, 20, 8, 0.12);
      transform: translateY(1px);
    }}
    .login-showcase::after {{
      content: "";
      position: absolute;
      width: 260px;
      height: 260px;
      right: -90px;
      bottom: -120px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(255,255,255,0.16), transparent 72%);
    }}
    .credential {{
      margin-top: 18px;
      padding: 18px 18px 16px;
      border-radius: 22px;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.16);
      backdrop-filter: blur(8px);
    }}
    .hero-grid {{
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 18px;
      align-items: stretch;
    }}
    .signal-board {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
      margin-top: 22px;
    }}
    .signal-tile {{
      padding: 18px;
      border-radius: 22px;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.16);
    }}
    .signal-tile strong {{
      display: block;
      font-size: 28px;
      margin-top: 10px;
    }}
    .hero-banner {{
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      margin-top: 22px;
    }}
    .hero-tag {{
      display: inline-flex;
      align-items: center;
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(255,255,255,0.12);
      border: 1px solid rgba(255,255,255,0.18);
      font-size: 13px;
      font-weight: 700;
    }}
    .ops-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(300px, 0.92fr);
      gap: 18px;
      margin-bottom: 18px;
    }}
    .products-top-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.16fr) minmax(320px, 0.84fr);
      gap: 16px;
      margin-bottom: 18px;
      align-items: stretch;
    }}
    .products-top-grid .panel {{
      height: 100%;
      padding: 22px 22px 20px;
      border-radius: 24px;
    }}
    .products-overview-card h1,
    .products-filter-card h2 {{
      margin-bottom: 8px;
    }}
    .products-overview-card {{
      overflow: visible;
      z-index: 1;
    }}
    .products-overview-card p,
    .products-filter-card p {{
      margin-bottom: 14px;
    }}
    .products-overview-card .tools {{
      margin-bottom: 14px;
    }}
    .products-overview-card .spotlight {{
      margin-top: 14px;
      padding: 14px 16px;
      border-radius: 20px;
    }}
    .products-overview-card .spotlight-value {{
      min-width: 70px;
      padding: 12px 14px;
      border-radius: 16px;
      font-size: 24px;
    }}
    .products-filter-form {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .products-filter-form .products-search-field {{
      grid-column: 1 / -1;
    }}
    .products-filter-form .filter-department-context {{
      display: flex;
      align-items: center;
      min-height: 48px;
      padding: 13px 15px;
      border: 1px solid rgba(91, 58, 29, 0.14);
      border-radius: 16px;
      background: rgba(181, 106, 45, 0.07);
      color: var(--accent-strong);
      font-weight: 700;
    }}
    .products-filter-form .products-filter-submit {{
      background: linear-gradient(180deg, rgba(181,106,45,0.1), rgba(181,106,45,0.06));
      border-color: rgba(181,106,45,0.16);
      box-shadow: none;
      color: var(--accent-strong);
    }}
    .products-filter-form .products-filter-submit:hover {{
      background: linear-gradient(180deg, rgba(181,106,45,0.14), rgba(181,106,45,0.09));
      filter: none;
    }}
    .products-main-stack {{
      display: flex;
      flex-direction: column;
      gap: 18px;
      min-width: 0;
      margin-bottom: 18px;
    }}
    .products-c-dashboard {{
      display: grid;
      grid-template-rows: minmax(0, 2fr) minmax(0, 8fr);
      gap: 18px;
      min-height: 1520px;
    }}
    .products-c-overview-stack {{
      display: grid;
      grid-template-rows: minmax(160px, 6fr) minmax(104px, 4fr);
      gap: 16px;
      min-height: 0;
    }}
    .products-c-overview-stack .products-top-grid {{
      min-height: 0;
      margin-bottom: 0;
    }}
    .products-c-overview-stack .products-insights-grid {{
      min-height: 0;
    }}
    .products-c-overview-stack .products-insights-grid > .panel {{
      height: 100%;
    }}
    .products-c-overview-stack .products-top-grid .panel {{
      padding: 16px 18px;
    }}
    .products-c-overview-stack .products-overview-card p,
    .products-c-overview-stack .products-filter-card p {{
      margin-bottom: 10px;
      line-height: 1.5;
    }}
    .products-c-overview-stack .products-overview-card .tools {{
      margin-bottom: 8px;
    }}
    .products-c-overview-stack .products-overview-card .spotlight {{
      margin-top: 8px;
      padding: 10px 12px;
      border-radius: 16px;
    }}
    .products-c-overview-stack .products-overview-card .spotlight-value {{
      min-width: 54px;
      padding: 8px 10px;
      border-radius: 13px;
      font-size: 17px;
    }}
    .products-c-overview-stack .products-stats-panel {{
      padding: 14px 18px;
    }}
    .products-c-overview-stack .products-stats-panel .table-note {{
      display: none;
    }}
    .products-c-overview-stack .products-stats-panel .stats {{
      margin-top: 8px;
    }}
    .products-c-overview-stack .products-stats-panel .stat-card {{
      padding: 10px 12px;
    }}
    .products-c-overview-stack .products-stats-panel .stat-card strong {{
      margin-top: 7px;
      font-size: 24px;
    }}
    .products-c-dashboard .products-main-stack {{
      min-height: 0;
      margin-bottom: 0;
    }}
    .products-c-dashboard .products-main-stack > .panel {{
      display: flex;
      flex-direction: column;
      min-height: 0;
      height: 100%;
    }}
    .products-c-dashboard #products-bulk-form {{
      display: flex;
      flex: 1 1 auto;
      flex-direction: column;
      min-height: 0;
    }}
    .products-c-dashboard .products-list-scroll-wrap {{
      flex: 1 1 auto;
      min-height: 0;
      max-height: none;
    }}
    .products-editor-dashboard {{
      display: grid;
      grid-template-rows: minmax(0, 6fr) minmax(0, 14fr);
      gap: 18px;
      min-height: 1620px;
    }}
    .products-editor-overview-stack {{
      display: grid;
      grid-template-rows: minmax(250px, 17fr) minmax(150px, 13fr);
      gap: 16px;
      min-height: 0;
    }}
    .products-editor-dashboard .products-top-grid {{
      min-height: 0;
      margin-bottom: 0;
    }}
    .products-editor-dashboard .products-main-stack {{
      min-height: 0;
      margin-bottom: 0;
    }}
    .products-editor-dashboard .products-main-stack > .panel {{
      display: flex;
      flex-direction: column;
      min-height: 0;
      height: 100%;
    }}
    .products-editor-dashboard #products-bulk-form {{
      display: flex;
      flex: 1 1 auto;
      flex-direction: column;
      min-height: 0;
    }}
    .products-editor-dashboard .products-list-scroll-wrap {{
      flex: 1 1 auto;
      min-height: 0;
      max-height: none;
    }}
    .products-editor-dashboard .products-insights-grid {{
      min-height: 0;
    }}
    .products-editor-dashboard .products-insights-grid > .panel {{
      height: 100%;
      padding: 16px 18px;
    }}
    .products-editor-dashboard .products-stats-panel .table-note {{
      display: none;
    }}
    .products-editor-dashboard .products-stats-panel .stats {{
      margin-top: 10px;
    }}
    .products-editor-dashboard .products-stats-panel .stat-card {{
      padding: 12px 14px;
    }}
    .products-editor-dashboard .products-stats-panel .stat-card strong {{
      margin-top: 8px;
      font-size: 27px;
    }}
    .products-main-stack > .panel,
    .products-insights-grid > .panel {{
      width: 100%;
      max-width: 100%;
      min-width: 0;
      padding: 22px 22px 20px;
      border-radius: 24px;
    }}
    .products-insights-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.16fr) minmax(320px, 0.84fr);
      gap: 16px;
      width: 100%;
      min-width: 0;
      align-items: start;
    }}
    .products-insights-grid.products-insights-single {{
      grid-template-columns: minmax(0, 1fr);
    }}
    .products-stats-panel .stats {{
      margin-top: 14px;
    }}
    .spotlight {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      align-items: center;
      margin-top: 18px;
      padding: 18px 20px;
      border-radius: 24px;
      background: linear-gradient(135deg, rgba(181,106,45,0.12), rgba(46,95,88,0.08));
      border: 1px solid rgba(94, 67, 40, 0.1);
    }}
    .spotlight strong {{
      display: block;
      font-size: 15px;
      margin-bottom: 4px;
    }}
    .spotlight-value {{
      min-width: 84px;
      text-align: center;
      padding: 16px;
      border-radius: 20px;
      background: rgba(255,255,255,0.7);
      font-size: 28px;
      font-weight: 800;
      box-shadow: var(--shadow-soft);
    }}
    .section-stack {{
      display: flex;
      flex-direction: column;
      gap: 18px;
    }}
    .list-intro {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      flex-wrap: wrap;
      margin-bottom: 18px;
    }}
    .list-intro-main {{
      flex: 1 1 460px;
      min-width: 280px;
    }}
    .list-intro-actions {{
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 10px;
      margin-left: auto;
      flex: 0 1 auto;
    }}
    .list-intro-actions .tools {{
      justify-content: flex-end;
      margin-bottom: 0;
    }}
    .list-intro-actions .tools > button {{
      width: auto;
      min-width: 0;
      padding-inline: 16px;
      white-space: nowrap;
    }}
    .list-intro-actions .tools.bulk-tools-vertical {{
      flex-direction: column;
      align-items: flex-end;
      gap: 8px;
    }}
    .list-intro-actions .meta {{
      color: var(--muted);
      font-size: 12px;
      text-align: right;
      line-height: 1.55;
    }}
    .table-note {{
      color: var(--muted);
      font-size: 13px;
      margin-top: 6px;
    }}
    .empty-state {{
      padding: 28px 24px;
      text-align: center;
      color: var(--muted);
    }}
    .login-panel {{
      padding: 30px;
    }}
    .login-panel h2 {{
      font-size: 34px;
      margin-bottom: 8px;
    }}
    .login-action {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 14px;
      align-items: center;
      margin-top: 18px;
    }}
    .subtle-note {{
      font-size: 13px;
      color: var(--muted);
      line-height: 1.7;
    }}
    .login-page {{
      position: relative;
      isolation: isolate;
      min-height: calc(100vh - 48px);
      overflow: hidden;
      background: #f8faf7;
    }}
    .login-page::before {{
      content: "";
      position: absolute;
      inset: 56% 0 0;
      z-index: -2;
      background: #e6eee6;
      clip-path: polygon(0 13%, 100% 0, 100% 100%, 0 100%);
    }}
    .login-mytteno {{
      margin-top: 18px;
      color: rgba(54, 94, 74, 0.055);
      font-family: "Snell Roundhand", "Apple Chancery", "URW Chancery L", cursive;
      font-size: clamp(72px, 7.2vw, 120px);
      font-style: italic;
      font-weight: 600;
      line-height: 0.9;
      letter-spacing: 0.04em;
      white-space: nowrap;
      pointer-events: none;
    }}
    .login-page .login-shell {{
      position: relative;
      z-index: 1;
      max-width: 1080px;
      padding: 64px 28px 118px;
      grid-template-columns: minmax(0, 1fr) minmax(340px, 0.78fr);
      gap: clamp(42px, 7vw, 90px);
      align-items: center;
    }}
    .login-page .login-showcase {{
      padding: 18px 0;
      color: var(--ink);
      background: transparent;
      box-shadow: none;
      overflow: visible;
    }}
    .login-page .login-showcase::after {{ display: none; }}
    .login-page .login-brand-lockup {{ margin: 0; }}
    .login-page .login-brand-kicker {{
      color: #92541e;
      background: rgba(255, 251, 245, 0.78);
      border-color: rgba(188, 108, 37, 0.2);
      box-shadow: 0 8px 18px rgba(102, 72, 44, 0.08), inset 0 1px 0 rgba(255,255,255,0.64);
      font-family: "Avenir Next", "Helvetica Neue", sans-serif;
      font-size: 23px;
      font-weight: 700;
      letter-spacing: 0.07em;
    }}
    .login-page .login-brand-main {{
      color: #7f3b08;
      text-shadow: none;
    }}
    .login-brand-intro {{
      max-width: 390px;
      margin: 12px 0 0;
      color: rgba(56, 82, 69, 0.72);
      font-size: 14px;
      line-height: 1.8;
    }}
    .login-page .login-panel {{
      padding: 34px;
      border-radius: 18px;
      border-color: rgba(94, 67, 40, 0.13);
      background: rgba(255, 251, 245, 0.92);
      box-shadow: 0 20px 44px rgba(88, 56, 31, 0.1);
    }}
    .login-page .login-panel::before {{ display: none; }}
    .login-page .login-panel h2 {{
      color: var(--ink);
      font-size: 25px;
      font-weight: 750;
      margin-bottom: 26px;
      letter-spacing: 0;
    }}
    .login-page .login-panel .meta {{ margin: 0 0 24px; }}
    .login-page .login-panel input {{
      border-radius: 10px;
      border-color: rgba(94, 67, 40, 0.16);
      background: rgba(255, 253, 249, 0.96);
    }}
    .login-page .login-action {{ display: block; margin-top: 22px; }}
    .login-page .login-action button {{
      width: 100%;
      min-height: 48px;
      border-radius: 10px;
      background: var(--accent);
      box-shadow: 0 12px 24px rgba(143, 82, 29, 0.18);
    }}
    .login-page .subtle-note {{ display: none; }}
    .login-page {{
      background: linear-gradient(110deg, #fcf9f3 0%, #faf4ea 39%, #f3e6d4 67%, #ead6bd 100%);
    }}
    .login-page::before {{
      display: block;
      inset: 0 0 0 50%;
      z-index: -1;
      background: linear-gradient(180deg, rgba(226, 185, 133, 0.28) 0%, rgba(248, 238, 222, 0.12) 58%, rgba(210, 167, 116, 0.2) 100%);
      clip-path: polygon(14% 0, 100% 0, 100% 100%, 0 100%);
    }}
    .login-page .login-shell {{
      max-width: 1120px;
      padding: 72px 28px 92px;
      grid-template-columns: minmax(0, 1fr) minmax(360px, 380px);
      gap: clamp(56px, 9vw, 112px);
    }}
    .login-page .login-showcase {{
      min-height: 410px;
      padding: 26px 0;
      display: flex;
      flex-direction: column;
      justify-content: center;
      overflow: hidden;
      border-left: 2px solid rgba(188, 108, 37, 0.42);
      padding-left: 26px;
    }}
    .login-page .login-brand-lockup {{
      position: relative;
      z-index: 1;
      display: grid;
      grid-template-columns: 104px minmax(0, 1fr);
      column-gap: 18px;
      align-items: center;
      width: min(100%, 510px);
    }}
    .login-page .login-brand-kicker {{
      grid-column: 1;
      grid-row: 1 / span 2;
    }}
    .login-page .login-brand-main {{
      grid-column: 2;
      grid-row: 1;
      align-self: center;
    }}
    .login-page .login-mytteno {{
      position: absolute;
      left: calc(-36px - 1cm);
      bottom: calc(42px - 0.8cm);
      z-index: 0;
      margin: 0;
      color: rgba(127, 59, 8, 0.045);
      font-size: clamp(88px, 9vw, 150px);
      letter-spacing: 0.06em;
    }}
    .login-brand-intro {{
      position: relative;
      z-index: 1;
      grid-column: 2;
      grid-row: 2;
      display: grid;
      gap: 10px;
      max-width: 390px;
      margin: 16px 0 0;
      padding-top: 16px;
      border-top: 1px solid rgba(127, 59, 8, 0.16);
      color: #945b29;
      font-size: 17px;
      font-weight: 750;
      line-height: 1.75;
    }}
    .login-brand-intro span {{ display: block; color: #7f3b08; font-weight: 750; }}
    .login-brand-intro strong {{
      color: #7f3b08;
      font-size: 17px;
      font-weight: 750;
    }}
    .login-page .login-panel {{
      width: 100%;
      margin-left: auto;
      padding: 38px 34px;
      border-radius: 8px;
      border-top: 3px solid rgba(188, 108, 37, 0.7);
      border-color: rgba(94, 67, 40, 0.13);
      background: rgba(255, 251, 245, 0.94);
      box-shadow: 0 16px 32px rgba(88, 56, 31, 0.08);
    }}
    .billing-compact-table {{
      min-width: 1120px;
    }}
    .billing-compact-table td {{
      vertical-align: top;
      padding-top: 14px;
      padding-bottom: 14px;
    }}
    .billing-cell-title {{
      font-weight: 700;
      color: var(--ink);
    }}
    .billing-platform-cell {{
      display: grid;
      gap: 8px;
      min-width: 180px;
    }}
    .billing-platform-picker-label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }}
    .billing-platform-picker-label select {{
      min-height: 42px;
      padding: 10px 12px;
      border-radius: 14px;
      color: var(--ink);
      background: rgba(255, 255, 255, 0.95);
      font-size: 14px;
    }}
    .billing-platform-label {{
      font-size: 15px;
      font-weight: 700;
      color: var(--ink);
    }}
    .billing-platform-meta {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: fit-content;
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 12px;
      color: #7a5a3c;
      background: rgba(188, 108, 37, 0.1);
      border: 1px solid rgba(188, 108, 37, 0.12);
      white-space: nowrap;
    }}
    .billing-file-list {{
      margin: 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 8px;
    }}
    .billing-month-section {{
      display: grid;
      gap: 16px;
      align-content: start;
    }}
    .billing-month-status {{
      grid-template-columns: minmax(0, 1fr);
      gap: 10px;
      align-items: stretch;
    }}
    .billing-month-status-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }}
    .billing-month-status-head strong {{
      margin: 0;
    }}
    .billing-month-status-value {{
      min-width: 0;
      padding: 8px 12px;
      border-radius: 14px;
      font-size: 18px;
      line-height: 1.25;
      white-space: nowrap;
    }}
    .billing-month-status .table-note {{
      width: 100%;
      max-width: none;
      margin: 0;
    }}
    .billing-month-picker {{
      display: grid;
      grid-template-columns: minmax(220px, 280px) auto;
      gap: 10px 12px;
      align-items: end;
    }}
    .billing-month-picker .field {{
      gap: 5px;
    }}
    .billing-month-picker input {{
      min-height: 42px;
      padding: 10px 12px;
      border-radius: 14px;
    }}
    .billing-month-picker select {{
      min-height: 42px;
      padding: 10px 12px;
      border-radius: 14px;
    }}
    .billing-platform-delete-option {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 42px;
      padding: 10px 12px;
      border: 1px solid rgba(154, 47, 35, 0.16);
      border-radius: 14px;
      color: #8b3d2f;
      background: rgba(171, 74, 49, 0.06);
      white-space: nowrap;
    }}
    .billing-platform-delete-option input {{
      width: auto;
      min-height: 0;
      margin: 0;
    }}
    .account-platform-options {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .account-platform-option {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 42px;
      padding: 10px 13px;
      border: 1px solid rgba(181, 106, 45, 0.18);
      border-radius: 14px;
      color: var(--ink);
      background: rgba(255, 251, 245, 0.76);
      font-size: 14px;
      font-weight: 650;
      cursor: pointer;
    }}
    .account-platform-option input {{
      width: auto;
      min-height: 0;
      margin: 0;
      accent-color: var(--accent);
    }}
    .account-platform-option:has(input:checked) {{
      color: var(--accent-strong);
      border-color: rgba(181, 106, 45, 0.42);
      background: rgba(248, 230, 205, 0.72);
    }}
    .account-create-grid {{
      grid-template-columns: repeat(4, minmax(0, 1fr));
    }}
    .account-create-grid .account-create-billing {{
      grid-column: span 2;
    }}
    .account-create-grid .account-create-submit {{
      padding-top: 28px;
    }}
    .user-account-actions-cell {{
      min-width: 380px;
    }}
    .user-account-primary-action,
    .user-account-primary-action form,
    .user-account-reset-action {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .user-account-primary-action {{
      width: 100%;
      white-space: nowrap;
      justify-content: flex-end;
    }}
    .user-account-primary-action form {{
      flex: 1 1 auto;
      min-width: 0;
    }}
    .user-account-primary-action input {{
      width: 100%;
      min-width: 0;
      flex: 1 1 auto;
      padding: 9px 11px;
    }}
    .user-account-primary-action button {{
      width: 132px;
      min-width: 132px;
      padding: 9px 12px;
    }}
    .user-account-reset-action {{
      margin-top: 8px;
      flex-wrap: nowrap;
      white-space: nowrap;
      width: 100%;
      justify-content: flex-end;
    }}
    .user-account-reset-action input[type="password"] {{
      width: 128px;
      min-width: 128px;
      flex: 0 0 128px;
      padding: 9px 11px;
    }}
    .user-account-reset-action input[name="confirm_text"] {{
      width: 104px;
      min-width: 104px;
      flex: 0 0 104px;
      padding: 9px 11px;
    }}
    .user-account-reset-action button {{
      width: 132px;
      min-width: 132px;
      padding: 9px 12px;
    }}
    @media (max-width: 980px) {{
      .account-create-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .account-create-grid .account-create-billing {{ grid-column: 1 / -1; }}
    }}
    @media (max-width: 640px) {{
      .account-create-grid {{ grid-template-columns: 1fr; }}
      .account-create-grid .account-create-billing {{ grid-column: auto; }}
    }}
    .billing-month-picker button {{
      width: auto;
      min-width: 108px;
      padding: 10px 16px;
      border-radius: 14px;
      font-size: 14px;
      box-shadow: 0 10px 20px rgba(91, 58, 29, 0.14);
    }}
    .billing-month-section .billing-month-picker button {{
      border-color: rgba(181, 106, 45, 0.16);
      background: linear-gradient(180deg, rgba(181,106,45,0.1), rgba(181,106,45,0.06));
      color: var(--accent-strong);
      box-shadow: none;
    }}
    .billing-month-section .billing-month-picker button:hover {{
      background: linear-gradient(180deg, rgba(181,106,45,0.14), rgba(181,106,45,0.09));
      filter: none;
    }}
    .billing-month-grid {{
      gap: 12px;
    }}
    .billing-month-grid .detail-card {{
      padding: 16px 18px;
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(255,255,255,0.92), rgba(249,243,235,0.9));
    }}
    .billing-month-grid .detail-card strong {{
      font-size: 18px;
    }}
    .billing-month-card {{
      position: relative;
      overflow: hidden;
      border-width: 1px;
      transition: transform 160ms ease, box-shadow 160ms ease, border-color 160ms ease;
    }}
    .billing-month-card:hover {{
      transform: translateY(-2px);
      text-decoration: none;
      box-shadow: 0 18px 34px rgba(91, 58, 29, 0.12);
    }}
    .billing-month-card::before {{
      content: "";
      position: absolute;
      inset: 0 0 auto 0;
      height: 4px;
      background: rgba(188, 108, 37, 0.18);
    }}
    .billing-month-card-draft {{
      border-color: rgba(157, 97, 45, 0.16);
      background: linear-gradient(180deg, rgba(255,252,248,0.94), rgba(249,241,232,0.92));
    }}
    .billing-month-card-draft::before {{
      background: linear-gradient(90deg, rgba(157, 97, 45, 0.95), rgba(205, 149, 82, 0.8));
    }}
    .billing-month-card-partial_to_b {{
      border-color: rgba(127, 93, 42, 0.18);
      background: linear-gradient(180deg, rgba(255,251,244,0.95), rgba(245,237,224,0.94));
    }}
    .billing-month-card-partial_to_b::before {{
      background: linear-gradient(90deg, rgba(127, 93, 42, 0.95), rgba(195, 154, 81, 0.78));
    }}
    .billing-month-card-submitted_to_b {{
      border-color: rgba(47, 111, 85, 0.18);
      background: linear-gradient(180deg, rgba(246,252,249,0.96), rgba(233,245,240,0.94));
    }}
    .billing-month-card-submitted_to_b::before {{
      background: linear-gradient(90deg, rgba(47, 111, 85, 0.95), rgba(88, 160, 128, 0.78));
    }}
    .billing-month-card-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 12px;
    }}
    .billing-progress-overview {{
      display: grid;
      grid-template-columns: minmax(320px, 1.2fr) minmax(0, 1fr);
      gap: 16px;
      align-items: stretch;
      margin-top: 2px;
    }}
    .billing-progress-overview .spotlight {{
      margin: 0;
      min-height: 100%;
    }}
    .billing-progress-overview .stats {{
      margin: 0;
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }}
    .billing-progress-overview-compact {{
      grid-template-columns: minmax(0, 1fr);
    }}
    .billing-progress-overview-compact .stats {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .billing-progress-overview .stat-card {{
      min-height: 124px;
      justify-content: space-between;
    }}
    .billing-progress-overview .stat-card strong {{
      font-size: 28px;
    }}
    .brand-dashboard-grid {{
      display: grid;
      gap: 16px;
    }}
    .brand-dashboard-panel {{
      display: grid;
      gap: 14px;
    }}
    .brand-bills-primary-panel {{
      padding-top: 22px;
      padding-bottom: 22px;
    }}
    .brand-bills-primary-panel .brand-bill-card-head {{
      margin-bottom: 16px;
    }}
    .brand-bills-primary-panel .brand-bill-current-section {{
      gap: 16px;
    }}
    .brand-bills-primary-panel .brand-bill-current-file-section {{
      padding-top: 16px;
    }}
    .brand-dashboard-toolbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }}
    .brand-dashboard-toolbar h2 {{
      margin: 0;
    }}
    .brand-bill-card-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      flex-wrap: wrap;
      margin-bottom: 22px;
    }}
    .brand-bill-card-head h1 {{
      margin: 0;
    }}
    .brand-bill-flow-chip {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 34px;
      padding: 7px 12px;
      border-radius: 999px;
      font-size: 13px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .brand-bill-flow-draft {{
      color: var(--accent-strong);
      background: rgba(188, 108, 37, 0.10);
    }}
    .brand-bill-flow-submitted {{
      color: var(--success);
      background: rgba(50, 111, 85, 0.13);
    }}
    .brand-bill-flow-return {{
      color: #8a5208;
      background: rgba(191, 124, 48, 0.14);
    }}
    .brand-bill-management-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 26px;
      align-items: start;
    }}
    .brand-bill-current-section {{
      display: grid;
      gap: 20px;
      min-width: 0;
    }}
    .brand-bill-current-section h2,
    .brand-bill-side h2 {{
      margin-bottom: 14px;
    }}
    .brand-bill-status-heading {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 12px;
      flex-wrap: wrap;
      margin-bottom: 14px;
    }}
    .brand-bill-status-heading h2 {{
      margin: 0;
    }}
    .brand-bill-current-file-section {{
      padding-top: 20px;
      border-top: 1px solid rgba(94, 67, 40, 0.12);
    }}
    .brand-bill-file-summary {{
      display: grid;
      gap: 6px;
      padding: 0 0 18px;
      border-bottom: 1px solid rgba(94, 67, 40, 0.12);
    }}
    .brand-bill-file-label {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }}
    .brand-bill-file-name {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      font-weight: 700;
    }}
    .brand-bill-file-name a {{
      overflow-wrap: anywhere;
    }}
    .brand-bill-download-link {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 5px 10px;
      border: 1px solid rgba(142, 87, 43, 0.20);
      border-radius: 6px;
      color: var(--accent-strong);
      background: rgba(255, 252, 247, 0.76);
      font-size: 12px;
      text-decoration: none;
      white-space: nowrap;
    }}
    .brand-bill-download-link:hover {{
      background: rgba(188, 108, 37, 0.10);
    }}
    .brand-bill-file-actions {{
      display: flex;
      align-items: end;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 18px;
    }}
    .brand-bill-upload-form {{
      display: grid;
      grid-template-columns: minmax(150px, 220px) auto;
      gap: 10px;
      align-items: end;
      margin: 0;
      width: auto;
    }}
    .brand-bill-upload-form .field {{
      min-width: 0;
      width: clamp(150px, 16vw, 220px);
    }}
    .brand-bill-upload-form button {{
      width: auto;
      min-width: 78px;
    }}
    .brand-bill-submit-form button {{
      width: auto;
      min-width: 128px;
    }}
    .brand-bill-submit-form {{
      margin: 0 0 0 auto;
    }}
    .brand-bill-file-actions button:disabled {{
      cursor: not-allowed;
      filter: none;
      opacity: 0.48;
      box-shadow: none;
    }}
    .brand-bill-delete-form {{
      align-self: end;
      margin: 0;
    }}
    .brand-bill-delete-form button {{
      width: auto;
      min-width: 90px;
    }}
    .brand-bill-side {{
      display: grid;
      gap: 20px;
      min-width: 0;
      padding-left: 26px;
      border-left: 1px solid rgba(94, 67, 40, 0.12);
    }}
    .brand-bill-status-section,
    .brand-bill-history-section {{
      min-width: 0;
    }}
    .brand-bill-history-section {{
      padding-top: 0;
    }}
    .brand-bill-return-detail {{
      margin-top: 10px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.65;
    }}
    .brand-bill-return-form {{
      display: grid;
      gap: 10px;
      margin-top: 16px;
    }}
    .brand-bill-return-form textarea {{
      min-height: 82px;
    }}
    .brand-bill-return-form button {{
      width: fit-content;
      min-width: 128px;
    }}
    .brand-bill-return-decisions {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 14px;
    }}
    .brand-bill-return-decisions form {{
      margin: 0;
    }}
    .brand-bill-return-decisions button {{
      width: auto;
      min-width: 116px;
    }}
    .brand-bill-history-query {{
      display: flex;
      align-items: end;
      gap: 10px;
      width: 100%;
    }}
    .brand-bill-history-query input {{
      flex: 0 1 66.666%;
      min-width: 0;
    }}
    .brand-bill-history-query button {{
      width: auto;
      min-width: 90px;
      margin-left: auto;
    }}
    .brand-bill-history-list {{
      display: grid;
      gap: 0;
      margin: 14px 0 0;
      padding: 0;
      list-style: none;
      border-top: 1px solid rgba(94, 67, 40, 0.10);
    }}
    .brand-bill-history-list li {{
      display: grid;
      grid-template-columns: auto minmax(0, 1fr);
      gap: 8px 10px;
      padding: 12px 0;
      border-bottom: 1px solid rgba(94, 67, 40, 0.10);
    }}
    .brand-bill-history-version {{
      font-size: 12px;
      font-weight: 700;
      color: var(--accent-deep);
    }}
    .brand-bill-history-list .meta {{
      grid-column: 2;
      margin: -4px 0 0;
      font-size: 12px;
    }}
    .brand-dashboard-controls {{
      display: grid;
      justify-items: end;
      gap: 10px;
      flex: 1 1 auto;
      margin-left: auto;
    }}
    .brand-dashboard-primary-controls {{
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .brand-dashboard-secondary-controls {{
      display: flex;
      justify-content: flex-end;
      width: 100%;
    }}
    .brand-dashboard-import-form {{
      margin: 0;
    }}
    .brand-dashboard-import-input {{
      position: absolute;
      width: 1px;
      height: 1px;
      overflow: hidden;
      clip: rect(0 0 0 0);
      clip-path: inset(50%);
      white-space: nowrap;
    }}
    .brand-dashboard-control {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: auto;
      min-width: 112px;
      min-height: 42px;
      padding: 9px 14px;
      border: 1px solid rgba(91, 58, 29, 0.12);
      border-radius: 13px;
      background: rgba(255,255,255,0.78);
      color: var(--ink);
      font-weight: 700;
      text-decoration: none;
      white-space: nowrap;
      cursor: pointer;
      box-shadow: none;
    }}
    .brand-dashboard-control:hover {{
      background: rgba(255,247,236,0.96);
      text-decoration: none;
    }}
    .brand-dashboard-import-control {{
      border-color: rgba(181, 106, 45, 0.16);
      background: linear-gradient(180deg, rgba(181,106,45,0.1), rgba(181,106,45,0.06));
      color: var(--accent-strong);
    }}
    .brand-dashboard-query {{
      position: relative;
    }}
    .brand-dashboard-query summary {{
      list-style: none;
    }}
    .brand-dashboard-query summary::-webkit-details-marker {{
      display: none;
    }}
    .brand-dashboard-query-panel {{
      position: absolute;
      top: calc(100% + 8px);
      right: 0;
      z-index: 8;
      min-width: 236px;
      padding: 12px;
      border: 1px solid rgba(94, 67, 40, 0.12);
      border-radius: 16px;
      background: rgba(255, 251, 245, 0.98);
      box-shadow: var(--shadow-soft);
    }}
    .brand-dashboard-query-panel::before {{
      content: "";
      position: absolute;
      left: 0;
      right: 0;
      bottom: 100%;
      height: 8px;
    }}
    .brand-dashboard-query-panel form {{
      display: grid;
      gap: 10px;
    }}
    .brand-dashboard-query-panel label {{
      display: grid;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }}
    .brand-dashboard-summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 2px;
    }}
    .brand-dashboard-pill {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 12px;
      border-radius: 999px;
      background: rgba(188, 108, 37, 0.08);
      border: 1px solid rgba(188, 108, 37, 0.12);
      color: var(--ink);
      font-size: 12px;
    }}
    .brand-dashboard-pill strong {{
      font-size: 13px;
      color: var(--accent-strong);
    }}
    .brand-dashboard-table th,
    .brand-dashboard-table td {{
      white-space: nowrap;
      font-size: 14px;
    }}
    .brand-dashboard-table {{
      width: 100%;
      min-width: 100%;
      table-layout: fixed;
    }}
    .brand-dashboard-table thead tr:first-child th {{
      text-align: center;
    }}
    .brand-dashboard-table thead tr:nth-child(2) th:first-child,
    .brand-dashboard-table thead tr:nth-child(2) th:nth-child(2),
    .brand-dashboard-table thead tr:nth-child(2) th:nth-child(3) {{
      text-align: left;
    }}
    .brand-dashboard-table td {{
      vertical-align: middle;
    }}
    .brand-dashboard-table .brand-col-month,
    .brand-dashboard-table .brand-col-channel,
    .brand-dashboard-table .brand-col-shop {{
      white-space: normal;
      min-width: 120px;
      max-width: 220px;
      word-break: break-word;
      line-height: 1.4;
    }}
    .brand-dashboard-table .brand-col-month {{
      min-width: 110px;
      max-width: 140px;
    }}
    .brand-dashboard-table .brand-col-number {{
      text-align: right;
      font-variant-numeric: tabular-nums;
      min-width: 86px;
    }}
    .brand-dashboard-table .brand-col-ratio {{
      color: var(--accent-deep);
    }}
    .brand-dashboard-table .brand-group-divider {{
      border-right: 1px solid rgba(127, 59, 8, 0.22);
    }}
    .brand-dashboard-table .brand-gz-divider {{
      border-right-color: rgba(127, 59, 8, 0.12);
    }}
    .brand-dashboard-table .brand-dashboard-resizable-head {{
      position: relative;
      padding-right: 22px;
    }}
    .brand-dashboard-resize-handle {{
      position: absolute;
      top: 0;
      right: -6px;
      width: 12px;
      height: 100%;
      min-width: 0;
      padding: 0;
      border: 0;
      border-radius: 0;
      background: transparent;
      box-shadow: none;
      cursor: col-resize;
      touch-action: none;
    }}
    .brand-dashboard-resize-handle::before {{
      content: "";
      position: absolute;
      top: 26%;
      bottom: 26%;
      left: 5px;
      border-left: 1px solid rgba(127, 59, 8, 0.22);
    }}
    .brand-dashboard-resize-handle:hover::before {{
      border-color: var(--accent-strong);
    }}
    body.brand-dashboard-resizing {{
      cursor: col-resize;
      user-select: none;
    }}
    .brand-dashboard-table .brand-empty-cell {{
      color: var(--muted);
    }}
    .brand-bill-workspace {{
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(340px, 0.9fr);
      gap: 18px;
      align-items: start;
    }}
    .brand-upload-panel {{
      display: grid;
      gap: 12px;
      padding: 20px 20px 18px;
      border-radius: 22px;
      background: linear-gradient(180deg, rgba(255,255,255,0.95), rgba(247,239,231,0.9));
      border: 1px solid rgba(94, 67, 40, 0.08);
      box-shadow: var(--shadow-soft);
    }}
    .brand-upload-panel .tools {{
      margin-bottom: 0;
    }}
    .brand-action-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }}
    .brand-action-card {{
      display: grid;
      gap: 8px;
      padding: 14px 16px;
      border-radius: 18px;
      background: rgba(255,255,255,0.86);
      border: 1px solid rgba(94, 67, 40, 0.08);
      box-shadow: var(--shadow-soft);
    }}
    .brand-action-card strong {{
      font-size: 15px;
      color: var(--ink);
    }}
    .brand-action-card .meta {{
      margin-top: 0;
      font-size: 12px;
      line-height: 1.7;
    }}
    .brand-upload-form {{
      display: grid;
      gap: 12px;
    }}
    .brand-upload-tools {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      padding-top: 2px;
    }}
    .brand-upload-tools button,
    .brand-upload-tools a {{
      width: auto;
      min-width: 138px;
    }}
    .brand-upload-summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }}
    .brand-flow-card {{
      display: grid;
      gap: 10px;
      padding: 20px 18px;
      border-radius: 22px;
      background: linear-gradient(180deg, rgba(255,251,246,0.96), rgba(244,235,224,0.93));
      border: 1px solid rgba(94, 67, 40, 0.1);
      box-shadow: var(--shadow-soft);
    }}
    .brand-version-card {{
      padding: 13px 15px;
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(255,255,255,0.93), rgba(249,242,233,0.9));
      border: 1px solid rgba(94, 67, 40, 0.08);
      display: grid;
      gap: 6px;
    }}
    .billing-status-chip {{
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      line-height: 1;
      white-space: nowrap;
      border: 1px solid transparent;
    }}
    .billing-status-draft {{
      background: rgba(157, 97, 45, 0.1);
      color: #8d5523;
      border-color: rgba(157, 97, 45, 0.14);
    }}
    .billing-status-partial_to_b {{
      background: rgba(127, 93, 42, 0.11);
      color: #6e5328;
      border-color: rgba(127, 93, 42, 0.16);
    }}
    .billing-status-submitted_to_b {{
      background: rgba(47, 111, 85, 0.11);
      color: var(--success);
      border-color: rgba(47, 111, 85, 0.16);
    }}
    .billing-month-metrics {{
      display: grid;
      gap: 10px;
    }}
    .billing-metric-row {{
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 10px;
      align-items: center;
      font-size: 13px;
      color: var(--muted);
    }}
    .billing-progress-track {{
      position: relative;
      height: 8px;
      border-radius: 999px;
      overflow: hidden;
      background: rgba(94, 67, 40, 0.08);
    }}
    .billing-progress-fill {{
      position: absolute;
      inset: 0 auto 0 0;
      border-radius: inherit;
      background: linear-gradient(90deg, var(--accent) 0%, var(--accent-deep) 100%);
    }}
    .billing-progress-fill-submitted {{
      background: linear-gradient(90deg, var(--accent-deep) 0%, #4fa07e 100%);
    }}
    .billing-platform-hero {{
      grid-template-columns: minmax(0, 3fr) minmax(0, 7fr);
      align-items: stretch;
    }}
    .billing-platform-hero > .panel {{
      min-width: 0;
      height: 100%;
    }}
    .billing-workspace-panel {{
      width: 100%;
      margin: 0;
      padding-top: 24px;
      scroll-margin-top: 128px;
    }}
    .billing-workspace-panel .table-note {{
      margin-top: -2px;
      margin-bottom: 14px;
      max-width: 980px;
    }}
    .billing-workspace-panel .table-wrap {{
      overflow-x: hidden;
    }}
    .billing-workspace-panel .billing-compact-table {{
      width: 100%;
      min-width: 0;
      table-layout: fixed;
    }}
    .billing-workspace-panel .billing-compact-table th,
    .billing-workspace-panel .billing-compact-table td {{
      padding: 10px 9px;
      white-space: normal;
      overflow-wrap: anywhere;
      word-break: break-word;
    }}
    .billing-workspace-panel .billing-compact-table thead th {{
      font-size: 14px;
    }}
    .billing-workspace-panel .billing-platform-cell,
    .billing-workspace-panel .billing-file-block,
    .billing-workspace-panel .billing-status-card,
    .billing-workspace-panel .billing-actions-panel {{
      min-width: 0;
    }}
    .billing-workspace-panel .billing-file-block,
    .billing-workspace-panel .billing-status-card,
    .billing-workspace-panel .billing-actions-panel {{
      padding: 10px;
    }}
    .billing-workspace-panel .billing-platform-picker-label select,
    .billing-workspace-panel .billing-actions-panel input[type="file"] {{
      min-width: 0;
      max-width: 100%;
    }}
    .billing-workspace-panel .billing-file-block-head {{
      flex-wrap: wrap;
    }}
    @media (max-width: 1180px) {{
      .billing-platform-hero {{
        grid-template-columns: 1fr;
      }}
    }}
    .billing-inline-form {{
      display: inline-flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .billing-inline-form input[type="file"] {{
      max-width: 240px;
      min-height: 40px;
      padding: 8px 10px;
      border-radius: 13px;
      font-size: 13px;
    }}
    .billing-inline-stack {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      align-items: flex-start;
    }}
    .billing-inline-stack .meta {{
      margin-top: 0;
      font-size: 12px;
    }}
    .billing-actions-compact {{
      gap: 8px;
      align-items: center;
    }}
    .billing-actions-compact button {{
      width: auto;
      min-width: 94px;
      padding: 9px 14px;
      border-radius: 13px;
      font-size: 13px;
      line-height: 1.2;
      box-shadow: 0 8px 18px rgba(91, 58, 29, 0.12);
      white-space: nowrap;
    }}
    .billing-file-block {{
      display: grid;
      gap: 10px;
      padding: 12px 14px;
      border-radius: 16px;
      background: linear-gradient(180deg, rgba(255,255,255,0.88), rgba(248,242,235,0.84));
      border: 1px solid rgba(94, 67, 40, 0.08);
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.45);
    }}
    .billing-file-block-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}
    .billing-file-block strong {{
      font-size: 13px;
      letter-spacing: 0.04em;
      color: var(--muted);
    }}
    .billing-file-block-head strong {{
      margin-bottom: 0;
    }}
    .billing-file-count {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      color: #7a5a3c;
      background: rgba(188, 108, 37, 0.1);
      border: 1px solid rgba(188, 108, 37, 0.12);
      white-space: nowrap;
    }}
    .billing-file-block .meta {{
      margin-top: 0;
    }}
    .billing-file-item {{
      display: grid;
      gap: 6px;
      padding: 10px 12px;
      border-radius: 12px;
      background: rgba(255,255,255,0.9);
      border: 1px solid rgba(94, 67, 40, 0.08);
    }}
    .billing-file-index {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: fit-content;
      padding: 4px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      color: var(--accent-deep);
      background: rgba(188, 108, 37, 0.08);
      border: 1px solid rgba(188, 108, 37, 0.1);
      white-space: nowrap;
    }}
    .billing-file-empty {{
      padding: 12px;
      border-radius: 12px;
      background: rgba(255,255,255,0.82);
      border: 1px dashed rgba(94, 67, 40, 0.14);
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
    }}
    .billing-file-history {{
      display: grid;
      gap: 10px;
      margin-top: 12px;
      padding-top: 12px;
      border-top: 1px dashed rgba(94, 67, 40, 0.16);
    }}
    .billing-file-history summary {{
      color: var(--accent-strong);
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
    }}
    .billing-history-version {{
      display: grid;
      gap: 8px;
      padding-top: 8px;
    }}
    .billing-history-version > strong {{
      color: var(--muted);
      font-size: 12px;
    }}
    .billing-status-card {{
      display: grid;
      gap: 8px;
      padding: 12px 14px;
      border-radius: 16px;
      background: linear-gradient(180deg, rgba(255,255,255,0.86), rgba(247,239,231,0.82));
      border: 1px solid rgba(94, 67, 40, 0.08);
      min-height: 100%;
    }}
    .billing-status-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .billing-status-note {{
      font-size: 12px;
      line-height: 1.65;
      color: var(--muted);
    }}
    .billing-actions-panel {{
      display: grid;
      gap: 10px;
      padding: 12px 14px;
      border-radius: 16px;
      background: linear-gradient(180deg, rgba(255,255,255,0.86), rgba(247,239,231,0.82));
      border: 1px solid rgba(94, 67, 40, 0.08);
      min-width: 280px;
    }}
    .billing-actions-panel .billing-inline-stack {{
      width: 100%;
      align-items: stretch;
    }}
    .billing-actions-panel .billing-inline-form {{
      width: 100%;
    }}
    .billing-actions-panel input[type="file"] {{
      flex: 1 1 220px;
      width: 100%;
      max-width: none;
    }}
    .billing-actions-panel .billing-actions-compact {{
      justify-content: flex-start;
      width: 100%;
    }}
    .billing-upload-form {{
      width: 100%;
    }}
    .billing-upload-form input[type="file"] {{
      width: 100%;
    }}
    .billing-actions-primary-row {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      width: 100%;
    }}
    .billing-actions-primary-row > button,
    .billing-actions-primary-row > form,
    .billing-actions-primary-row > form button {{
      width: 100%;
    }}
    .billing-actions-primary-row .billing-upload-button {{
      border-color: rgba(181, 106, 45, 0.16);
      background: linear-gradient(180deg, rgba(181,106,45,0.1), rgba(181,106,45,0.06));
      color: var(--accent-strong);
      box-shadow: none;
    }}
    .billing-actions-primary-row .billing-upload-button:hover {{
      background: linear-gradient(180deg, rgba(181,106,45,0.14), rgba(181,106,45,0.09));
      filter: none;
    }}
    .billing-actions-primary-row .ghost-button {{
      background: rgba(255,255,255,0.88);
      box-shadow: none;
    }}
    .billing-actions-submit-row,
    .billing-actions-submit-row form,
    .billing-actions-submit-row button {{
      width: 100%;
    }}
    .billing-actions-submit-row button {{
      margin-top: 2px;
    }}
    .billing-return-request,
    .billing-return-decision {{
      display: grid;
      gap: 8px;
      width: 100%;
      padding-top: 10px;
      border-top: 1px dashed rgba(94, 67, 40, 0.16);
    }}
    .billing-return-request input {{
      width: 100%;
      min-width: 0;
    }}
    .billing-return-request button {{
      width: 100%;
    }}
    .billing-return-note {{
      padding: 10px 12px;
      border-radius: 13px;
      color: var(--warning);
      background: rgba(157, 97, 45, 0.08);
      border: 1px solid rgba(157, 97, 45, 0.12);
      font-size: 12px;
      line-height: 1.5;
    }}
    .billing-actions-note {{
      font-size: 12px;
      line-height: 1.65;
      color: var(--muted);
    }}
    .billing-owner-badge {{
      display: inline-flex;
      align-items: center;
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 13px;
      font-weight: 700;
      line-height: 1.2;
      white-space: nowrap;
    }}
    .billing-owner-self {{
      background: rgba(50, 111, 85, 0.14);
      color: var(--success);
    }}
    .billing-owner-other {{
      background: rgba(157, 97, 45, 0.12);
      color: var(--warning);
    }}
    .billing-owner-empty {{
      background: rgba(91, 58, 29, 0.08);
      color: var(--muted);
    }}
    .billing-owner-locked {{
      background: rgba(109, 52, 16, 0.12);
      color: #6d3410;
    }}
    @media (max-width: 900px) {{
      .hero, .login-shell, .hero-grid, .ops-grid, .products-top-grid, .products-insights-grid, .billing-c-home-grid, .billing-b-entry-grid, .billing-a-entry-grid {{ grid-template-columns: 1fr; }}
      .products-c-dashboard {{
        display: block;
        min-height: 0;
      }}
      .products-editor-dashboard {{
        display: block;
        min-height: 0;
      }}
      .products-c-overview-stack {{
        display: block;
      }}
      .products-editor-overview-stack {{
        display: block;
      }}
      .products-c-overview-stack .products-insights-grid {{
        margin-top: 18px;
      }}
      .products-editor-overview-stack .products-insights-grid {{
        margin-top: 18px;
      }}
      .products-c-dashboard .products-main-stack {{
        margin-top: 18px;
      }}
      .products-editor-dashboard .products-main-stack,
      .products-editor-dashboard .products-insights-grid {{
        margin-top: 18px;
      }}
      .products-c-dashboard .products-main-stack > .panel {{
        display: block;
        height: auto;
      }}
      .products-editor-dashboard .products-main-stack > .panel {{
        display: block;
        height: auto;
      }}
      .products-c-dashboard #products-bulk-form {{
        display: block;
      }}
      .products-editor-dashboard #products-bulk-form {{
        display: block;
      }}
      .products-c-dashboard .products-list-scroll-wrap {{
        max-height: 58vh;
      }}
      .products-editor-dashboard .products-list-scroll-wrap {{
        max-height: 58vh;
      }}
      .list-intro-actions {{
        width: 100%;
        align-items: flex-start;
      }}
      .list-intro-actions .tools {{
        justify-content: flex-start;
      }}
      .list-intro-actions .tools.bulk-tools-vertical {{
        align-items: flex-start;
      }}
      .list-intro-actions .meta {{
        text-align: left;
      }}
      .detail-panel-tools {{
        width: 100%;
        align-items: flex-start;
      }}
      .detail-panel-tools .tools {{
        justify-content: flex-start;
      }}
      .products-list-scroll-wrap {{
        max-height: 58vh;
      }}
      .billing-month-picker {{ grid-template-columns: 1fr; }}
      .billing-progress-overview {{
        grid-template-columns: 1fr;
      }}
      .billing-progress-overview .stats {{
        grid-template-columns: 1fr;
      }}
      .brand-dashboard-grid,
      .brand-bill-workspace {{
        grid-template-columns: 1fr;
      }}
      .a-monthly-board-head {{
        align-items: flex-start;
      }}
      .a-monthly-board-query {{
        width: 100%;
      }}
      .supplier-query-summary {{
        grid-template-columns: 1fr;
      }}
      .supplier-settlement-layout {{
        grid-template-columns: 1fr;
        grid-template-areas:
          "main"
          "master"
          "query";
      }}
      .supplier-master-query-grid {{
        grid-template-columns: 1fr;
      }}
      .supplier-master-query-form + .supplier-master-query-form {{
        padding: 16px 0 0;
        border-top: 1px solid rgba(94, 67, 40, 0.1);
        border-left: none;
      }}
      .supplier-master-create-row {{
        grid-template-columns: 1fr;
      }}
      .supplier-master-section-head {{
        align-items: flex-start;
      }}
      .supplier-bill-import-actions,
      .supplier-bill-query-actions {{
        grid-template-columns: 1fr;
      }}
      .supplier-bill-import-actions button,
      .supplier-bill-query-actions button {{
        grid-column: 1;
      }}
      .supplier-bill-import-action-pair {{
        grid-column: 1;
      }}
      .brand-dashboard-toolbar {{
        align-items: flex-start;
      }}
      .brand-bill-management-grid {{
        grid-template-columns: 1fr;
      }}
      .brand-bill-side {{
        padding: 20px 0 0;
        border-top: 1px solid rgba(94, 67, 40, 0.12);
        border-left: none;
      }}
      .brand-bill-upload-form {{
        grid-template-columns: 1fr;
      }}
      .brand-bill-file-actions {{
        display: grid;
        grid-template-columns: 1fr;
      }}
      .brand-bill-upload-form,
      .brand-bill-submit-form,
      .brand-bill-delete-form {{
        width: 100%;
        margin: 0;
      }}
      .brand-bill-upload-form button,
      .brand-bill-submit-form button,
      .brand-bill-delete-form button {{
        width: 100%;
      }}
      .brand-dashboard-controls {{
        width: 100%;
        margin-left: 0;
        justify-items: start;
      }}
      .brand-dashboard-primary-controls,
      .brand-dashboard-secondary-controls {{
        justify-content: flex-start;
      }}
      .brand-dashboard-query-panel {{
        right: auto;
        left: 0;
      }}
      .brand-summary-board .stats {{
        grid-template-columns: 1fr;
      }}
      .billing-actions-panel {{
        min-width: 0;
      }}
      .nav-shell {{
        grid-template-columns: 1fr;
        position: static;
      }}
      .nav-shell::before {{
        top: 34px;
        font-size: 20px;
      }}
      .nav-actions {{
        justify-content: flex-start;
      }}
      .products-filter-form {{
        grid-template-columns: 1fr;
      }}
      .nav-session {{
        margin-left: 0;
      }}
      .nav-links {{
        width: 100%;
        justify-content: flex-start;
        flex-basis: 100%;
      }}
      .nav-chip-dropdown {{
        width: 100%;
      }}
      .nav-menu {{
        width: 100%;
      }}
      .nav-menu summary {{
        width: 100%;
        justify-content: space-between;
      }}
      .nav-dropdown {{
        left: 0;
        right: auto;
        min-width: min(100%, 280px);
      }}
      .tools form {{
        grid-template-columns: 1fr;
      }}
      .login-page {{
        min-height: calc(100vh - 28px);
      }}
      .login-page .login-shell {{
        gap: 34px;
        padding: 42px 24px 74px;
      }}
      .login-page .login-showcase {{
        min-height: 0;
        padding: 0 0 0 20px;
        text-align: center;
        overflow: visible;
      }}
      .login-page .login-brand-lockup {{
        grid-template-columns: 84px minmax(0, 1fr);
        column-gap: 14px;
        width: min(100%, 430px);
        margin: 0 auto;
        text-align: left;
      }}
      .login-page .login-mytteno {{
        position: relative;
        left: auto;
        bottom: auto;
        margin: 18px 0 0;
        font-size: clamp(66px, 18vw, 96px);
      }}
      .login-brand-intro {{
        margin: 16px 0 0;
        text-align: left;
      }}
      .login-page .login-panel {{
        padding: 26px 22px;
      }}
      .login-panel h2 {{
        font-size: 28px;
      }}
      .login-brand-lockup {{
        gap: 14px;
      }}
      .login-brand-kicker {{
        width: 84px;
        min-height: 84px;
        font-size: 23px;
      }}
      .login-brand-main {{
        font-size: 56px;
      }}
      .brand-subline {{
        font-size: 36px;
      }}
      .modules-home {{
        min-height: calc(100vh - 144px);
      }}
      .modules-home-watermark {{
        min-height: 112px;
        padding: 26px 2% 6px;
      }}
      .modules-home-watermark::before {{
        bottom: 50px;
      }}
      .modules-home-watermark span {{
        font-size: 28px;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    {nav}
    {monitor_toolbar}
    {back_link}
    {content}
  </div>
  <script>
    {monitor_script}
    document.querySelectorAll("details").forEach(function(menu) {{
      let closeTimer = null;
      const cancelPendingClose = function() {{
        if (closeTimer !== null) {{
          window.clearTimeout(closeTimer);
          closeTimer = null;
        }}
      }};
      menu.addEventListener("pointerenter", cancelPendingClose);
      menu.addEventListener("pointerleave", function(event) {{
        if (event.pointerType === "mouse") {{
          cancelPendingClose();
          closeTimer = window.setTimeout(function() {{
            menu.open = false;
            closeTimer = null;
          }}, 180);
        }}
      }});
      menu.addEventListener("focusout", function(event) {{
        if (!(event.relatedTarget instanceof Node) || !menu.contains(event.relatedTarget)) {{
          cancelPendingClose();
          menu.open = false;
        }}
      }});
      menu.addEventListener("toggle", function() {{
        if (menu.open) {{
          document.querySelectorAll("details[open]").forEach(function(otherMenu) {{
            if (otherMenu !== menu) {{
              otherMenu.open = false;
            }}
          }});
        }}
        if (!menu.open) {{
          cancelPendingClose();
        }}
      }});
    }});
    document.addEventListener("click", async function(event) {{
      const target = event.target;
      if (!(target instanceof HTMLElement)) {{
        return;
      }}
      const copyText = target.getAttribute("data-copy-text");
      if (!copyText) {{
        return;
      }}
      try {{
        await navigator.clipboard.writeText(copyText);
        const original = target.textContent;
        target.textContent = "已复制";
        setTimeout(function() {{
          target.textContent = original;
        }}, 1200);
      }} catch (error) {{
        console.error(error);
      }}
    }});
  </script>
</body>
</html>
"""

    def require_billing_access(self, start_response, user):
        if can_access_billing_module(user):
            return None
        return self.html_response(
            start_response,
            self.render_message_page("权限不足", "当前账号不能进入账单与结算板块。", user),
            status="403 Forbidden",
        )

    def handle_billing_home(self, start_response, user, query):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        return self.html_response(start_response, self.render_billing_home(user, query))

    @staticmethod
    def parse_optional_monthly_board_count(value, label: str) -> int | None:
        raw_value = str(value or "").strip()
        if not raw_value:
            return None
        try:
            parsed_value = int(raw_value)
        except ValueError as error:
            raise ValueError(f"{label}应填写非负整数。") from error
        if parsed_value < 0:
            raise ValueError(f"{label}不能小于 0。")
        return parsed_value

    @staticmethod
    def parse_optional_monthly_board_amount(value, label: str) -> float | None:
        raw_value = str(value or "").replace(",", "").strip()
        if not raw_value:
            return None
        try:
            parsed_value = float(raw_value)
        except ValueError as error:
            raise ValueError(f"{label}应填写有效金额。") from error
        if not math.isfinite(parsed_value) or parsed_value < 0:
            raise ValueError(f"{label}应填写非负金额。")
        return parsed_value

    def handle_supplier_monthly_board_save(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if user.get("department") != "A":
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "只有跟单部可以填写月度看板。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        raw_year = str(form.get("year", "")).strip()
        try:
            board_year = db.normalize_monthly_board_year(raw_year)
            month_values = {}
            for month_no in range(1, 13):
                month_label = f"{month_no}月"
                month_values[month_no] = {
                    "payable_supplier_count": self.parse_optional_monthly_board_count(
                        form.get(f"supplier_count_{month_no}", ""),
                        f"{month_label}应付供应商",
                    ),
                    "payable_total_amount": self.parse_optional_monthly_board_amount(
                        form.get(f"payable_amount_{month_no}", ""),
                        f"{month_label}应付总金额",
                    ),
                }
            with db.get_connection(self.db_path) as connection:
                db.save_supplier_monthly_board(connection, board_year, month_values, user["id"])
        except ValueError as error:
            return self.redirect(
                start_response,
                "/billing?" + urlencode({"year": raw_year, "notice": str(error)}),
            )
        return self.redirect(
            start_response,
            "/billing?" + urlencode({"year": board_year, "notice": f"{board_year}年月度看板已保存。"}),
        )

    def handle_platform_bills_page(self, start_response, user, query):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_access_platform_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能查看平台账单页面。", user),
                status="403 Forbidden",
            )
        return self.html_response(start_response, self.render_platform_bills_page(user, query))

    def handle_platform_bill_upload(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_upload_platform_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能上传平台账单。", user),
                status="403 Forbidden",
            )
        form, files = self.parse_form(environ)
        month_key = form.get("month_key", "")
        platform_code = str(form.get("platform_code", "")).strip()
        if platform_code not in db.platform_bill_platform_codes_for_db(self.db_path):
            return self.html_response(
                start_response,
                self.render_message_page("平台不合法", "请选择有效的平台后再上传。", user),
                status="400 Bad Request",
            )
        if not c_user_can_manage_platform_bill(user, platform_code):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前运营账号只能处理其归属平台的账单文件。", user),
                status="403 Forbidden",
            )
        batch_uploads = read_validated_file_uploads(files.get("upload_files"))
        if len(batch_uploads) > 3:
            return self.redirect(
                start_response,
                self.platform_bills_page_url(month_key, "单次最多上传 3 个文件，请删减后再试。", platform_code),
            )
        with db.get_connection(self.db_path) as connection:
            billing_month = db.get_or_create_billing_month(connection, month_key, user["id"])
            summary_before = db.platform_bill_month_summary(self.db_path, billing_month["month_key"])
            platform_snapshot = next(
                (
                    item
                    for item in summary_before["platforms"]
                    if item["platform_code"] == platform_code
                ),
                None,
            ) or {}
            if self.platform_item_locked(platform_snapshot, billing_month.get("status")):
                return self.html_response(
                    start_response,
                    self.render_message_page("已锁定", "该平台账单已确认提交给商品部，运营部不能继续修改。", user),
                    status="400 Bad Request",
                )
            existing_main = platform_snapshot.get("main_file")
            existing_attachments = platform_snapshot.get("attachments") or []
            existing_files = ([existing_main] if existing_main else []) + list(existing_attachments)
            if any(int(file_item.get("uploaded_by") or 0) != int(user.get("id") or 0) for file_item in existing_files):
                return self.html_response(
                    start_response,
                    self.render_message_page("权限不足", "该平台文件已由其他运营同事维护，你不能上传、覆盖或追加文件。", user),
                    status="403 Forbidden",
                )
            if not batch_uploads:
                return self.redirect(
                    start_response,
                    self.platform_bills_page_url(month_key, "请先选择要上传的文件。", platform_code),
                )
            if existing_files:
                if len(existing_files) + len(batch_uploads) > 3:
                    return self.redirect(
                        start_response,
                        self.platform_bills_page_url(
                            month_key,
                            "该平台最多保留 3 个账单文件；如需调整，请先删除当前文件后重新上传。",
                            platform_code,
                        ),
                    )
            for index, upload_payload in enumerate(batch_uploads):
                stored_path = save_generic_upload(
                    self.upload_dir,
                    upload_payload,
                    subdir=f"billing/platform-bills/{billing_month['month_key']}/{platform_code}/files",
                )
                if not existing_main and index == 0:
                    db.replace_platform_main_file(
                        connection,
                        billing_month["id"],
                        platform_code,
                        upload_payload["original_filename"],
                        stored_path,
                        user["id"],
                    )
                    existing_main = {"uploaded_by": user["id"]}
                else:
                    db.add_platform_attachment(
                        connection,
                        billing_month["id"],
                        platform_code,
                        upload_payload["original_filename"],
                        stored_path,
                        user["id"],
                    )
        return self.redirect(
            start_response,
            self.platform_bills_page_url(
                month_key,
                f"{db.platform_bill_platform_label_for_db(self.db_path, platform_code)}账单文件已上传。",
                platform_code,
            ),
        )

    def handle_platform_bill_delete(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_upload_platform_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能删除平台账单文件。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        month_key = str(form.get("month_key", "")).strip()
        return_platform_code = str(form.get("return_platform", "")).strip()
        file_id_raw = str(form.get("file_id", "")).strip()
        if not file_id_raw.isdigit():
            return self.html_response(
                start_response,
                self.render_message_page("文件不存在", "没有找到要删除的平台账单文件。", user),
                status="404 Not Found",
            )
        file_record = db.get_platform_bill_file_by_id(self.db_path, int(file_id_raw))
        if not file_record:
            return self.html_response(
                start_response,
                self.render_message_page("文件不存在", "没有找到要删除的平台账单文件。", user),
                status="404 Not Found",
            )
        if not c_user_can_manage_platform_bill(user, file_record.get("platform_code")):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前运营账号只能处理其归属平台的账单文件。", user),
                status="403 Forbidden",
            )
        if file_record.get("submitted_at") or file_record.get("month_status") == "submitted_to_b":
            return self.html_response(
                start_response,
                self.render_message_page("已锁定", "该平台账单已确认提交给商品部，不能继续删除或重传。", user),
                status="400 Bad Request",
            )
        if int(file_record.get("uploaded_by") or 0) != int(user.get("id") or 0):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "你只能删除自己上传的平台账单文件。", user),
                status="403 Forbidden",
            )
        delete_scope = str(form.get("delete_scope", "")).strip().lower()
        deleted_count = 0
        deleted_paths: list[str] = []
        with db.get_connection(self.db_path) as connection:
            if delete_scope == "platform":
                platform_files = [
                    item
                    for item in db.list_platform_bill_files(self.db_path, file_record.get("month_key"))
                    if item.get("platform_code") == file_record.get("platform_code")
                    and int(item.get("uploaded_by") or 0) == int(user.get("id") or 0)
                    and int(item.get("is_current") or 0) == 1
                ]
                for item in platform_files:
                    deleted_item = db.delete_platform_bill_file(connection, int(item["id"]))
                    if deleted_item:
                        deleted_count += 1
                        if deleted_item.get("stored_path"):
                            deleted_paths.append(deleted_item.get("stored_path"))
            else:
                deleted_item = db.delete_platform_bill_file(connection, int(file_id_raw))
                if deleted_item:
                    deleted_count = 1
                    if deleted_item.get("stored_path"):
                        deleted_paths.append(deleted_item.get("stored_path"))
        for stored_path in deleted_paths:
            delete_generic_upload(self.upload_dir, stored_path)
        notice_text = "平台账单文件已删除，可重新上传。"
        if delete_scope == "platform":
            notice_text = "该平台下你上传的文件已全部删除，可重新上传。"
        return self.redirect(
            start_response,
            self.platform_bills_page_url(
                month_key or file_record.get("month_key", ""),
                notice_text,
                return_platform_code or str(file_record.get("platform_code") or ""),
            ),
        )

    def handle_platform_bill_submit(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_upload_platform_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能提交平台账单。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        month_key = form.get("month_key", "")
        platform_code = str(form.get("platform_code", "")).strip()
        if not c_user_can_manage_platform_bill(user, platform_code):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前运营账号只能处理其归属平台的账单文件。", user),
                status="403 Forbidden",
            )
        try:
            with db.get_connection(self.db_path) as connection:
                db.submit_platform_bill_platform(connection, month_key, platform_code, user["id"])
        except ValueError as error:
            return self.redirect(
                start_response,
                self.platform_bills_page_url(month_key, str(error), platform_code),
            )
        return self.redirect(
            start_response,
            self.platform_bills_page_url(
                month_key,
                f"{db.platform_bill_platform_label_for_db(self.db_path, platform_code)}已确认提交给商品部。",
                platform_code,
            ),
        )

    def handle_platform_bill_return_request(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_upload_platform_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "只有运营部可以申请退回平台账单。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        month_key = str(form.get("month_key", "")).strip()
        platform_code = str(form.get("platform_code", "")).strip()
        reason = " ".join(str(form.get("reason", "")).split())
        if not c_user_can_manage_platform_bill(user, platform_code):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前运营账号只能处理其归属平台的账单文件。", user),
                status="403 Forbidden",
            )
        if not reason:
            return self.redirect(
                start_response,
                self.platform_bills_page_url(month_key, "请填写申请退回的原因。", platform_code),
            )
        summary = db.platform_bill_month_summary(self.db_path, month_key)
        platform_item = next(
            (item for item in summary.get("platforms", []) if item.get("platform_code") == platform_code),
            None,
        ) or {}
        main_file = platform_item.get("main_file") or {}
        if not main_file or int(main_file.get("uploaded_by") or 0) != int(user.get("id") or 0):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "你只能申请退回本人提交的平台账单。", user),
                status="403 Forbidden",
            )
        try:
            with db.get_connection(self.db_path) as connection:
                db.create_platform_bill_return_request(connection, month_key, platform_code, int(user["id"]), reason)
        except ValueError as error:
            return self.redirect(
                start_response,
                self.platform_bills_page_url(month_key, str(error), platform_code),
            )
        return self.redirect(
            start_response,
            self.platform_bills_page_url(month_key, "退回申请已提交，等待商品部或管理员处理。", platform_code),
        )

    def handle_platform_bill_return_decision(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if user.get("department") != "B" and not is_admin(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "只有商品部或管理员可以处理退回申请。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        request_id_raw = str(form.get("request_id", "")).strip()
        month_key = str(form.get("month_key", "")).strip()
        platform_code = str(form.get("platform_code", "")).strip()
        decision = str(form.get("decision", "")).strip().lower()
        if not request_id_raw.isdigit() or decision not in {"approve", "reject"}:
            return self.html_response(
                start_response,
                self.render_message_page("请求无效", "没有找到可处理的退回申请。", user),
                status="400 Bad Request",
            )
        try:
            with db.get_connection(self.db_path) as connection:
                db.resolve_platform_bill_return_request(
                    connection,
                    int(request_id_raw),
                    int(user["id"]),
                    approve=decision == "approve",
                )
        except ValueError as error:
            return self.redirect(
                start_response,
                self.platform_bills_page_url(month_key, str(error), platform_code),
            )
        notice = "已退回运营部，请等待重新上传并确认提交。" if decision == "approve" else "已驳回退回申请，原提交版本继续有效。"
        return self.redirect(
            start_response,
            self.platform_bills_page_url(month_key, notice, platform_code),
        )

    def handle_platform_bill_platforms_update(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not is_admin(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "只有管理员可以维护平台清单。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        month_key = str(form.get("month_key", "")).strip()
        selected_platform_code = str(form.get("selected_platform", "")).strip()
        if not month_key:
            return self.redirect(
                start_response,
                "/billing/platform-bills?notice=" + self.urlencode_message("请先选择要维护的平台月份。"),
            )
        remove_codes = {
            str(value).strip().lower()
            for value in self.collect_checkbox_values(form, "remove_platform_code")
            if str(value).strip()
        }
        if remove_codes:
            platform_files = db.list_platform_bill_files(self.db_path, month_key)
            protected_codes = {
                str(item.get("platform_code") or "").strip().lower()
                for item in platform_files
                if str(item.get("platform_code") or "").strip().lower() in remove_codes
            }
            if protected_codes:
                labels = [
                    db.platform_bill_platform_label_for_db(self.db_path, code)
                    for code in sorted(protected_codes)
                ]
                return self.redirect(
                    start_response,
                    self.platform_bills_page_url(
                        month_key,
                        f"{'、'.join(labels)}已有账单文件或提交记录，不能删除。",
                        selected_platform_code,
                    ),
                )
        rows = []
        existing_codes: set[str] = set()
        index = 0
        while True:
            label_key = f"platform_label__{index}"
            code_key = f"platform_code__{index}"
            if label_key not in form and code_key not in form:
                break
            label = str(form.get(label_key, "")).strip()
            code = str(form.get(code_key, "")).strip().lower()
            if label and code not in remove_codes:
                normalized_code = db.normalize_platform_code(code or label, existing_codes)
                rows.append({"code": normalized_code, "label": label})
                existing_codes.add(normalized_code)
            index += 1
        new_labels = [item for item in self.collect_checkbox_values(form, "new_platform_label") if item.strip()]
        for label in new_labels:
            normalized_code = db.normalize_platform_code(label, existing_codes)
            rows.append({"code": normalized_code, "label": label.strip()})
            existing_codes.add(normalized_code)
        if not rows:
            return self.redirect(
                start_response,
                self.platform_bills_page_url(month_key, "至少需要保留一个平台。", selected_platform_code),
            )
        cleaned = db.update_billing_month_platform_configs(self.db_path, month_key, rows, update_default=True)
        db.log_admin_audit_action(
            self.db_path,
            int(user["id"]),
            "update_platform_bill_platforms",
            "更新平台账单平台清单",
            "billing_month",
            month_key,
            month_key,
            f"已更新 {month_key} 的平台清单，共 {len(cleaned)} 个平台。",
        )
        selected_after_update = selected_platform_code if selected_platform_code in {item["code"] for item in cleaned} else ""
        return self.redirect(
            start_response,
            self.platform_bills_page_url(
                month_key,
                f"平台清单已更新，当前共 {len(cleaned)} 个平台。",
                selected_after_update,
            ),
        )

    def handle_platform_bill_download(self, start_response, user, path: str):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if user.get("department") == "C":
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "运营部只能上传和提交平台账单，不能下载已上传文件。", user),
                status="403 Forbidden",
            )
        if not can_access_platform_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能下载平台账单文件。", user),
                status="403 Forbidden",
            )
        file_id_part = path.rsplit("/", 1)[-1].strip()
        if not file_id_part.isdigit():
            return self.html_response(
                start_response,
                self.render_message_page("文件不存在", "没有找到对应的平台账单文件。", user),
                status="404 Not Found",
            )
        file_record = db.get_platform_bill_file_by_id(self.db_path, int(file_id_part))
        if not file_record:
            return self.html_response(
                start_response,
                self.render_message_page("文件不存在", "没有找到对应的平台账单文件。", user),
                status="404 Not Found",
            )
        file_path = upload_file_path(self.upload_dir, file_record["stored_path"])
        if not file_path.exists():
            return self.html_response(
                start_response,
                self.render_message_page("文件不存在", "该文件已不存在，请联系管理员检查。", user),
                status="404 Not Found",
            )
        body = file_path.read_bytes()
        start_response(
            "200 OK",
            [
                ("Content-Type", generic_content_type(file_record["stored_path"])),
                ("Content-Disposition", f"attachment; filename*=UTF-8''{quote(str(file_record['original_filename']))}"),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    def handle_brand_bills_page(self, start_response, user, query):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_access_brand_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能查看品牌月账单页面。", user),
                status="403 Forbidden",
            )
        return self.html_response(start_response, self.render_brand_bills_page(user, query))

    def handle_brand_bill_upload(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_process_brand_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能上传品牌月账单。", user),
                status="403 Forbidden",
            )
        form, files = self.parse_form(environ)
        month_key = form.get("month_key", "")
        note = " ".join(str(form.get("note", "")).split())
        brand_upload = read_validated_file_upload(files.get("brand_bill_file"))
        if not brand_upload:
            return self.redirect(
                start_response,
                "/billing/brand-bills?" + urlencode({"month": month_key, "notice": "请先选择品牌月账单文件。"}),
            )
        with db.get_connection(self.db_path) as connection:
            brand_bill = db.get_or_create_brand_bill(connection, month_key, user["id"])
            if brand_bill.get("status") == "submitted_to_a":
                return self.html_response(
                    start_response,
                    self.render_message_page("已锁定", "该月份品牌月账单已提交给跟单部，商品部不能继续覆盖上传。", user),
                    status="400 Bad Request",
                )
            stored_path = save_generic_upload(
                self.upload_dir,
                brand_upload,
                subdir=f"billing/brand-bills/{db.normalize_month_key(month_key)}",
            )
            db.add_brand_bill_version(
                connection,
                brand_bill["id"],
                brand_upload["original_filename"],
                stored_path,
                user["id"],
                note=note,
            )
        return self.redirect(
            start_response,
            "/billing/brand-bills?" + urlencode({"month": month_key, "notice": "品牌月账单新版本已上传。"}),
        )

    def handle_brand_bill_delete(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_process_brand_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能删除品牌月账单。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        month_key = str(form.get("month_key", "")).strip()
        try:
            with db.get_connection(self.db_path) as connection:
                deleted_version = db.delete_latest_brand_bill_version(connection, month_key)
        except ValueError as error:
            return self.redirect(
                start_response,
                "/billing/brand-bills?" + urlencode({"month": month_key, "notice": str(error)}),
            )
        delete_generic_upload(self.upload_dir, deleted_version.get("stored_path"))
        return self.redirect(
            start_response,
            "/billing/brand-bills?" + urlencode({"month": month_key, "notice": "当前品牌月账单已删除。"}),
        )

    def handle_brand_bill_dashboard_update(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_process_brand_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能维护月销看板数据。", user),
                status="403 Forbidden",
            )
        form, files = self.parse_form(environ)
        month_key = str(form.get("month_key", "")).strip()
        dashboard_upload = read_validated_file_upload(files.get("dashboard_file"))
        if not dashboard_upload:
            return self.redirect(
                start_response,
                "/billing/brand-bills?" + urlencode({"month": month_key, "notice": "请上传看板 Excel 后再更新月销看板。"}),
            )
        parsed = parse_brand_bill_workbook(io.BytesIO(dashboard_upload["content"]))
        dashboard_rows = dashboard_rows_from_brand_bill_summary(parsed)
        source_type = "file"
        source_filename = dashboard_upload["original_filename"]
        source_path = ""
        notice = "月销看板已按上传 Excel 更新。"

        if not dashboard_rows:
            return self.redirect(
                start_response,
                "/billing/brand-bills?" + urlencode({"month": month_key, "notice": "月销看板没有可保存的数据。"}),
            )

        with db.get_connection(self.db_path) as connection:
            brand_bill = db.get_or_create_brand_bill(connection, month_key, user["id"])
            if brand_bill.get("status") == "submitted_to_a":
                return self.html_response(
                    start_response,
                    self.render_message_page("已锁定", "该月份品牌月账单已提交给跟单部，不能继续修改月销看板。", user),
                    status="400 Bad Request",
                )
            source_path = save_generic_upload(
                self.upload_dir,
                dashboard_upload,
                subdir=f"billing/brand-bills/{db.normalize_month_key(month_key)}/dashboard",
            )
            db.replace_brand_bill_dashboard_rows(
                connection,
                int(brand_bill["id"]),
                dashboard_rows,
                int(user["id"]),
                source_type=source_type,
                source_filename=source_filename,
                source_path=source_path,
            )
        return self.redirect(
            start_response,
            "/billing/brand-bills?" + urlencode({"month": month_key, "notice": notice}),
        )

    def handle_brand_bill_submit(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_process_brand_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能提交品牌月账单。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        month_key = form.get("month_key", "")
        with db.get_connection(self.db_path) as connection:
            db.submit_brand_bill(connection, month_key, user["id"])
        return self.redirect(
            start_response,
            "/billing/brand-bills?" + urlencode({"month": month_key, "notice": "该月品牌月账单已提交给跟单部。"}),
        )

    def handle_brand_bill_return_request(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_process_brand_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能申请退回品牌月账单。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        month_key = str(form.get("month_key", "")).strip()
        reason = " ".join(str(form.get("reason", "")).split())
        if not reason:
            return self.redirect(
                start_response,
                "/billing/brand-bills?" + urlencode({"month": month_key, "notice": "请填写申请退回原因。"}),
            )
        try:
            with db.get_connection(self.db_path) as connection:
                db.create_brand_bill_return_request(connection, month_key, user["id"], reason)
        except ValueError as error:
            return self.redirect(
                start_response,
                "/billing/brand-bills?" + urlencode({"month": month_key, "notice": str(error)}),
            )
        return self.redirect(
            start_response,
            "/billing/brand-bills?" + urlencode({"month": month_key, "notice": "退回申请已提交，等待跟单部或管理员处理。"}),
        )

    def handle_brand_bill_return_decision(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if user.get("department") != "A" and not is_admin(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "只有跟单部或管理员可以处理品牌月账单退回申请。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        request_id_raw = str(form.get("request_id", "")).strip()
        month_key = str(form.get("month_key", "")).strip()
        decision = str(form.get("decision", "")).strip().lower()
        if not request_id_raw.isdigit() or decision not in {"approve", "reject"}:
            return self.html_response(
                start_response,
                self.render_message_page("请求无效", "退回申请参数不完整。", user),
                status="400 Bad Request",
            )
        try:
            with db.get_connection(self.db_path) as connection:
                db.resolve_brand_bill_return_request(
                    connection,
                    int(request_id_raw),
                    user["id"],
                    approve=decision == "approve",
                )
        except ValueError as error:
            return self.redirect(
                start_response,
                "/billing/brand-bills?" + urlencode({"month": month_key, "notice": str(error)}),
            )
        notice = "已退回商品部，历史版本已保留。" if decision == "approve" else "已驳回退回申请，原账单继续有效。"
        return self.redirect(
            start_response,
            "/billing/brand-bills?" + urlencode({"month": month_key, "notice": notice}),
        )

    def handle_brand_bill_download(self, start_response, user, path: str):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_access_brand_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能下载品牌月账单文件。", user),
                status="403 Forbidden",
            )
        version_id_part = path.rsplit("/", 1)[-1].strip()
        if not version_id_part.isdigit():
            return self.html_response(
                start_response,
                self.render_message_page("文件不存在", "没有找到对应的品牌月账单文件。", user),
                status="404 Not Found",
            )
        version_record = db.get_brand_bill_version_by_id(self.db_path, int(version_id_part))
        if not version_record:
            return self.html_response(
                start_response,
                self.render_message_page("文件不存在", "没有找到对应的品牌月账单文件。", user),
                status="404 Not Found",
            )
        file_path = upload_file_path(self.upload_dir, version_record["stored_path"])
        if not file_path.exists():
            return self.html_response(
                start_response,
                self.render_message_page("文件不存在", "该文件已不存在，请联系管理员检查。", user),
                status="404 Not Found",
            )
        body = file_path.read_bytes()
        start_response(
            "200 OK",
            [
                ("Content-Type", generic_content_type(version_record["stored_path"])),
                ("Content-Disposition", f"attachment; filename*=UTF-8''{quote(str(version_record['original_filename']))}"),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    def handle_brand_bill_dashboard_export(self, start_response, user, query):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_access_brand_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能导出月销看板。", user),
                status="403 Forbidden",
            )
        month_key = query.get("month", "").strip() or datetime.now(timezone.utc).strftime("%Y-%m")
        try:
            normalized_month_key = db.normalize_month_key(month_key)
        except ValueError as error:
            return self.html_response(
                start_response,
                self.render_message_page("月份格式错误", str(error), user),
                status="400 Bad Request",
            )
        dashboard_rows = db.brand_bill_month_summary(self.db_path, normalized_month_key)["dashboard_rows"]
        body = brand_bill_dashboard_workbook_bytes(dashboard_rows)
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                ("Content-Disposition", f'attachment; filename="brand-dashboard-{normalized_month_key}.xlsx"'),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    def handle_brand_bill_template(self, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_access_brand_bills(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能下载品牌月账单模板。", user),
                status="403 Forbidden",
            )
        body = brand_bill_template_bytes()
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                ("Content-Disposition", 'attachment; filename="brand-bill-template.xlsx"'),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    def handle_supplier_settlements_page(self, start_response, user, query):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_access_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能查看供应商结算页面。", user),
                status="403 Forbidden",
            )
        return self.html_response(start_response, self.render_supplier_settlements_page(user, query))

    def handle_supplier_master_upsert(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_manage_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能维护供应商主档。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        supplier_code = str(form.get("supplier_code", "")).strip()
        supplier_name = str(form.get("supplier_name", "")).strip()
        invoice_names = self.split_multiline_values(form.get("invoice_names", ""))
        supplier_id = str(form.get("supplier_id", "")).strip()
        if supplier_id:
            db.update_supplier(
                self.db_path,
                int(supplier_id),
                supplier_name,
                invoice_names,
                is_active=form.get("is_active") == "on",
            )
            notice = f"供应商 {supplier_code or supplier_name} 已更新。"
        else:
            db.create_supplier(
                self.db_path,
                supplier_code,
                supplier_name,
                invoice_names,
                user["id"],
            )
            notice = f"供应商 {supplier_code} 已创建。"
        return self.redirect(
            start_response,
            "/billing/supplier-settlements?notice=" + self.urlencode_message(notice),
        )

    def handle_supplier_settlement_upsert(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_manage_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能维护供应商结算记录。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        month_key = form.get("month_key", "")
        supplier_id = int(str(form.get("supplier_id", "0")).strip() or "0")
        amount_due_raw = str(form.get("amount_due", "")).strip()
        if not amount_due_raw:
            raise ValueError("应付金额不能为空。")
        try:
            amount_due = float(amount_due_raw)
        except ValueError as error:
            raise ValueError("应付金额必须是数字。") from error
        payment_status = str(form.get("payment_status", "unpaid")).strip()
        payment_date = str(form.get("payment_date", "")).strip()
        invoice_name = str(form.get("invoice_name", "")).strip()
        note = str(form.get("note", "")).strip()
        with db.get_connection(self.db_path) as connection:
            db.upsert_supplier_settlement(
                connection,
                month_key,
                supplier_id,
                invoice_name,
                amount_due,
                payment_status,
                payment_date,
                note,
                user["id"],
            )
        return self.redirect(
            start_response,
            "/billing/supplier-settlements?" + urlencode({"month": month_key, "notice": "供应商结算记录已保存。"}),
        )

    def handle_supplier_settlement_import(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_manage_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能导入供应商结算。", user),
                status="403 Forbidden",
            )
        form, files = self.parse_form(environ)
        month_key = form.get("month_key", "")
        workbook_field = files.get("settlement_workbook")
        if workbook_field is None or getattr(workbook_field, "file", None) is None:
            return self.redirect(
                start_response,
                "/billing/supplier-settlements?" + urlencode({"month": month_key, "notice": "请先选择供应商结算 Excel 文件。"}),
            )
        workbook_field.file.seek(0)
        rows = parse_supplier_settlement_workbook(workbook_field.file)
        if not rows:
            return self.redirect(
                start_response,
                "/billing/supplier-settlements?" + urlencode({"month": month_key, "notice": "导入文件里没有可用的结算记录。"}),
            )
        updated = 0
        with db.get_connection(self.db_path) as connection:
            for row in rows:
                supplier = db.get_supplier_by_code(self.db_path, row["supplier_code"])
                if not supplier:
                    raise ValueError(f"供应商编码 {row['supplier_code']} 在主档中不存在，请先建档。")
                db.upsert_supplier_settlement(
                    connection,
                    month_key,
                    int(supplier["id"]),
                    row["invoice_name"],
                    float(row["amount_due"]),
                    row["payment_status"],
                    row["payment_date"],
                    row["note"],
                    user["id"],
                )
                updated += 1
        return self.redirect(
            start_response,
            "/billing/supplier-settlements?" + urlencode({"month": month_key, "notice": f"已导入 {updated} 条供应商结算记录。"}),
        )

    def handle_supplier_settlement_export(self, start_response, user, query):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_access_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能导出供应商结算。", user),
                status="403 Forbidden",
            )
        month_key = query.get("month", "").strip()
        settlements = db.supplier_settlement_month_summary(self.db_path, month_key)["items"]
        body = supplier_settlement_workbook_bytes(settlements)
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                ("Content-Disposition", f'attachment; filename="supplier-settlements-{month_key or "current"}.xlsx"'),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    def handle_supplier_settlement_template(self, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_access_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能下载供应商结算模板。", user),
                status="403 Forbidden",
            )
        body = supplier_settlement_template_bytes()
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                ("Content-Disposition", 'attachment; filename="supplier-settlement-template.xlsx"'),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    def supplier_bill_query_filters(self, query: dict) -> tuple[str, str, str, list[int]]:
        current_month = datetime.now(timezone.utc).strftime("%Y-%m")
        start_month = str(query.get("start_month", "")).strip() or current_month
        end_month = str(query.get("end_month", "")).strip() or start_month
        supplier_code = str(query.get("supplier_code", "")).strip()
        supplier_name_ids = self.parse_numeric_csv(query.get("supplier_name_ids", ""))
        return start_month, end_month, supplier_code, supplier_name_ids

    def handle_supplier_master_form_page(self, start_response, user, query):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_manage_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能维护供应商信息。", user),
                status="403 Forbidden",
            )
        notice = str(query.get("notice", "")).strip()
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        content = f"""
        <section class="panel supplier-master-form-page">
          <h1>单个供应商录入</h1>
          {notice_block}
          <form method="post" action="/billing/supplier-settlements/master">
            <input type="hidden" name="return_to" value="master_form">
            <div class="form-grid">
              <label class="field"><span>供应商编号</span><input name="supplier_code" placeholder="例如 S001"></label>
              <label class="field"><span>供应商名称</span><input name="supplier_name" placeholder="例如 广美舟"></label>
              <label class="field"><span>供应链经理</span><input name="supply_chain_manager" placeholder="例如 张三"></label>
            </div>
            <div class="tools" style="margin-top:18px; margin-bottom:0;"><button type="submit">保存供应商</button></div>
          </form>
        </section>
        """
        return self.html_response(
            start_response,
            self.page(
                "单个供应商录入 - 商品资料后台",
                content,
                user,
                current_page="billing",
                back_href="/billing/supplier-settlements",
            ),
        )

    def handle_supplier_master_edit_page(self, start_response, user, query):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_manage_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能编辑供应商信息。", user),
                status="403 Forbidden",
            )
        try:
            supplier_master_name_id = int(str(query.get("id", "")).strip())
        except ValueError:
            supplier_master_name_id = 0
        supplier_master = db.get_supplier_master_name(self.db_path, supplier_master_name_id)
        if not supplier_master:
            return self.html_response(
                start_response,
                self.render_message_page("供应商不存在", "没有找到要编辑的供应商信息。", user),
                status="404 Not Found",
            )
        notice = str(query.get("notice", "")).strip()
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        content = f"""
        <section class="panel supplier-master-form-page">
          <h1>编辑供应商</h1>
          <p class="meta">可更正供应商编号、供应商名称和供应链经理。相同供应商编号下的名称共用同一供应链经理。</p>
          {notice_block}
          <form method="post" action="/billing/supplier-settlements/master/edit">
            <input type="hidden" name="supplier_master_name_id" value="{int(supplier_master['id'])}">
            <div class="form-grid">
              <label class="field"><span>供应商编号</span><input name="supplier_code" value="{html.escape(str(supplier_master['supplier_code']), quote=True)}"></label>
              <label class="field"><span>供应商名称</span><input name="supplier_name" value="{html.escape(str(supplier_master['supplier_name']), quote=True)}"></label>
              <label class="field"><span>供应链经理</span><input name="supply_chain_manager" value="{html.escape(str(supplier_master['supply_chain_manager']), quote=True)}"></label>
            </div>
            <div class="tools" style="margin-top:18px; margin-bottom:0;">
              <a class="pill" href="/billing/supplier-settlements?master_code={html.escape(str(supplier_master['supplier_code']), quote=True)}">取消</a>
              <button type="submit">保存修改</button>
            </div>
          </form>
        </section>
        """
        return self.html_response(
            start_response,
            self.page(
                "编辑供应商 - 商品资料后台",
                content,
                user,
                current_page="billing",
                back_href="/billing/supplier-settlements",
            ),
        )

    def handle_supplier_master_edit_save(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_manage_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能编辑供应商信息。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        try:
            supplier_master_name_id = int(str(form.get("supplier_master_name_id", "")).strip())
        except ValueError:
            supplier_master_name_id = 0
        try:
            with db.get_connection(self.db_path) as connection:
                saved = db.update_supplier_master(
                    connection,
                    supplier_master_name_id,
                    form.get("supplier_code", ""),
                    form.get("supplier_name", ""),
                    form.get("supply_chain_manager", ""),
                    user["id"],
                )
        except ValueError as error:
            return self.redirect(
                start_response,
                "/billing/supplier-settlements/master/edit?"
                + urlencode({"id": supplier_master_name_id, "notice": str(error)}),
            )
        return self.redirect(
            start_response,
            "/billing/supplier-settlements?"
            + urlencode(
                {
                    "master_code": saved["supplier_code"],
                    "notice": f"供应商 {saved['supplier_name']} 已更新。",
                }
            ),
        )

    def handle_supplier_master_import(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_manage_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能导入供应商主档。", user),
                status="403 Forbidden",
            )
        _, files = self.parse_form(environ)
        try:
            upload = read_validated_file_upload(
                files.get("supplier_master_workbook"),
                allowed_extensions={".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            )
            if not upload:
                raise ValueError("请先选择供应商主档 Excel 文件。")
            rows = parse_supplier_master_workbook(io.BytesIO(upload["content"]))
            if not rows:
                raise ValueError("导入文件里没有可用的供应商信息。")
            with db.get_connection(self.db_path) as connection:
                for row in rows:
                    db.save_supplier_master(
                        connection,
                        row["supplier_code"],
                        row["supplier_name"],
                        row["supply_chain_manager"],
                        user["id"],
                    )
        except ValueError as error:
            return self.redirect(
                start_response,
                "/billing/supplier-settlements?" + urlencode({"notice": str(error)}),
            )
        return self.redirect(
            start_response,
            "/billing/supplier-settlements?" + urlencode({"notice": f"已导入 {len(rows)} 条供应商信息。"}),
        )

    def handle_supplier_master_template(self, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_access_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能下载供应商主档模板。", user),
                status="403 Forbidden",
            )
        body = supplier_master_template_bytes()
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                ("Content-Disposition", 'attachment; filename="supplier-master-template.xlsx"'),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    def handle_supplier_bill_master_save(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_manage_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能维护供应商信息。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        destination = (
            "/billing/supplier-settlements/master/new"
            if str(form.get("return_to", "")).strip() == "master_form"
            else "/billing/supplier-settlements"
        )
        try:
            saved = None
            with db.get_connection(self.db_path) as connection:
                saved = db.save_supplier_master(
                    connection,
                    form.get("supplier_code", ""),
                    form.get("supplier_name", ""),
                    form.get("supply_chain_manager", ""),
                    user["id"],
                )
        except ValueError as error:
            return self.redirect(
                start_response,
                destination + "?" + urlencode({"notice": str(error)}),
            )
        return self.redirect(
            start_response,
            destination + "?" + urlencode(
                {"notice": f"供应商 {saved['supplier_name']} 已保存。"}
            ),
        )

    def handle_supplier_bill_import(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_manage_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能导入供应商账单。", user),
                status="403 Forbidden",
            )
        form, files = self.parse_form(environ)
        raw_month = str(form.get("period_month", "")).strip()
        try:
            period_month = db.normalize_month_key(raw_month)
            bill_upload = read_validated_file_upload(
                files.get("bill_workbook"),
                allowed_extensions={".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
            )
            if not bill_upload:
                raise ValueError("请先选择账单 Excel 文件。")
            rows = parse_supplier_bill_workbook(io.BytesIO(bill_upload["content"]))
            if not rows:
                raise ValueError("导入文件里没有可用的账单明细。")
            with db.get_connection(self.db_path) as connection:
                change_window = db.supplier_bill_change_window(connection, period_month)
                if change_window["first_imported_at"] and not change_window["within_window"]:
                    raise ValueError(
                        f"{period_month} 账单已超过首次导入后 30 天，不能删除或重新上传。"
                    )
                resolved_rows = db.resolve_supplier_bill_rows(connection, rows)
                stored_path = save_generic_upload(
                    self.upload_dir,
                    bill_upload,
                    subdir=f"billing/supplier-bills/{period_month}",
                )
                batch = db.create_supplier_bill_batch(
                    connection,
                    period_month,
                    bill_upload["original_filename"],
                    stored_path,
                    resolved_rows,
                    user["id"],
                )
        except ValueError as error:
            return self.redirect(
                start_response,
                "/billing/supplier-settlements?" + urlencode(
                    {"start_month": raw_month, "end_month": raw_month, "notice": str(error)}
                ),
            )
        return self.redirect(
            start_response,
            "/billing/supplier-settlements?" + urlencode(
                {
                    "start_month": period_month,
                    "end_month": period_month,
                    "notice": f"{period_month} 账单已导入 V{batch['version_no']}，共 {batch['line_count']} 条明细。",
                }
            ),
        )

    def handle_supplier_bill_delete(self, environ, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if user.get("department") != "A":
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "只有跟单部可以删除并重新上传供应商账单。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        raw_month = str(form.get("period_month", "")).strip()
        try:
            period_month = db.normalize_month_key(raw_month)
            with db.get_connection(self.db_path) as connection:
                deleted = db.deactivate_current_supplier_bill_batch(connection, period_month)
        except ValueError as error:
            return self.redirect(
                start_response,
                "/billing/supplier-settlements?"
                + urlencode({"start_month": raw_month, "end_month": raw_month, "notice": str(error)}),
            )
        return self.redirect(
            start_response,
            "/billing/supplier-settlements?"
            + urlencode(
                {
                    "start_month": period_month,
                    "end_month": period_month,
                    "notice": f"{period_month} 当前账单已删除，可在首次导入后 30 天期限内重新上传。",
                }
            ),
        )

    def handle_supplier_bill_export(self, start_response, user, query):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_access_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能导出供应商账单。", user),
                status="403 Forbidden",
            )
        start_month, end_month, supplier_code, supplier_name_ids = self.supplier_bill_query_filters(query)
        try:
            result = db.query_supplier_bill_lines(
                self.db_path,
                start_month,
                end_month,
                supplier_code,
                supplier_name_ids,
            )
        except ValueError as error:
            return self.html_response(
                start_response,
                self.render_message_page("查询条件错误", str(error), user),
                status="400 Bad Request",
            )
        body = supplier_bill_workbook_bytes(result["items"])
        filename = f"supplier-bills-{result['start_month']}-to-{result['end_month']}.xlsx"
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                ("Content-Disposition", f'attachment; filename="{filename}"'),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    def handle_supplier_bill_template(self, start_response, user):
        denied = self.require_billing_access(start_response, user)
        if denied:
            return denied
        if not can_access_supplier_settlements(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "当前账号不能下载供应商账单模板。", user),
                status="403 Forbidden",
            )
        body = supplier_bill_template_bytes()
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                ("Content-Disposition", 'attachment; filename="supplier-bill-template.xlsx"'),
                ("Content-Length", str(len(body))),
            ],
        )
        return [body]

    def render_login(self, error: str = "") -> str:
        brand_name = self.brand_config["brand_name"]
        brand_mark = self.brand_config["brand_mark"]
        brand_lines = [line.strip() for line in brand_name.splitlines() if line.strip()]
        brand_secondary = brand_lines[1] if len(brand_lines) > 1 else ""
        error_block = f'<div class="warning">{html.escape(error)}</div>' if error else ""
        content = f"""
        <div class="login-page">
          <div class="login-shell">
            <section class="login-showcase">
              <div class="login-brand-lockup">
                <div class="login-brand-kicker">{html.escape(brand_mark)}</div>
                {f'<div class="login-brand-main">{html.escape(brand_secondary)}</div>' if brand_secondary else ""}
                <p class="login-brand-intro"><span>供应链 · 商品 · 运营</span><span>多部门协同管理资料，流程加速，效率加倍</span><strong>开启寻宝之旅</strong></p>
              </div>
              <div class="login-mytteno" aria-hidden="true">MYTENO</div>
            </section>
            <section class="panel login-panel">
              <h2>思安娜的藏宝阁</h2>
              {error_block}
              <form method="post" action="/login">
                <div class="form-grid">
                  <label class="field">
                    <span>用户名</span>
                    <input name="username" placeholder="请输入用户名">
                  </label>
                  <label class="field">
                    <span>密码</span>
                    <input type="password" name="password" placeholder="请输入密码">
                  </label>
                </div>
                <div class="login-action">
                  <button type="submit">进入后台</button>
                </div>
              </form>
            </section>
          </div>
        </div>
        """
        return self.page(f"登录 - {brand_name}", content)

    def render_products(self, user, query) -> str:
        brand_name = self.brand_config["brand_name"]
        console_eyebrow = self.brand_config["brand_console_eyebrow"]
        keyword = query.get("q", "").strip()
        department_filter = query.get("department", "").strip()
        if not is_admin(user):
            department_filter = ""
            query = {**query, "department": ""}
        status_filter = query.get("status", "").strip()
        lifecycle_filter = query.get("lifecycle_status", "").strip()
        return_to_path = self.products_return_path(query)
        bulk_enabled = not is_department_monitor(user) and (
            is_admin(user) or user.get("department") in {"A", "B", "C"}
        )
        source_status_filter = "" if user.get("department") == "C" else status_filter
        products = self.visible_products_for_user(
            db.list_products(self.db_path, keyword, department_filter, source_status_filter, lifecycle_filter),
            user,
        )
        if user.get("department") == "C" and status_filter:
            products = [
                product for product in products
                if self.c_effective_status(product, user) == status_filter
            ]
        configured_list_fields = self.configured_list_layout_fields(user)
        stats = db.department_stats(self.db_path)
        workflow_stats = db.status_stats(self.db_path)
        lifecycle_stats = db.lifecycle_stats(self.db_path)
        recent_stats = db.recent_activity_stats(self.db_path, days=7)
        b_dashboard_stats = (
            db.b_workflow_stats(self.db_path, days=7)
            if user.get("department") == "B" or is_admin(user)
            else {}
        )
        c_receipt_stats = (
            db.c_department_receipt_stats(self.db_path)
            if user.get("department") == "C" and is_department_monitor(user)
            else (db.c_user_receipt_stats(self.db_path, user) if user.get("department") == "C" else {})
        )
        notice = query.get("notice", "")
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        c_note = ""
        if user["department"] == "C":
            if is_department_monitor(user):
                c_note = '<div class="warning">管理员正在查看运营部汇总视图：已合并天猫、唯品及同款资料，仅用于查看接收进度。</div>'
            elif user.get("operating_channel") not in C_OPERATING_CHANNELS:
                c_note = '<div class="warning">当前运营账号尚未设置运营归属，暂不显示商品资料。请由管理员在账号管理中设置为天猫类或唯品类。</div>'
            else:
                c_note = (
                    f'<div class="warning">当前账号归属{html.escape(operating_channel_label(user.get("operating_channel")))}：只能查看本归属渠道及同款资料，页面、Excel 导出和 JSON 接口均不会返回其他渠道内容。</div>'
                )
        rows = []
        for product in products:
            payload = self.product_payload_for_user(product, user)
            revision_badge = self.revision_badge(product)
            actions = [f'<a href="/products/{product["id"]}">查看</a>']
            if can_edit_product(user, product):
                actions.append(f'<a href="/products/{product["id"]}/edit">编辑</a>')
            if (
                user.get("department") == "C"
                and not is_department_monitor(user)
                and self.c_effective_status(product, user) == "published"
            ):
                actions.append(
                    f'<button class="table-action-receive" type="submit" '
                    f'name="status" value="received" '
                    f'formaction="/products/{product["id"]}/status" formmethod="post">接收</button>'
                )
            lifecycle_actions = dict(available_lifecycle_actions(user, product))
            if "deleted" in lifecycle_actions:
                product_name = str(product.get("product_name") or "").strip()
                style_code = str(product.get("style_code") or "").strip()
                action_context = " / ".join(value for value in (product_name, style_code) if value)
                if not action_context:
                    action_context = f"资料 #{product['id']}"
                actions.append(
                    f'<button class="table-action-danger" type="submit" '
                    f'name="lifecycle_status" value="deleted" '
                    f'formaction="/products/{product["id"]}/lifecycle" formmethod="post" '
                    f'data-delete-button="1" '
                    f'data-product-label="{html.escape(action_context, quote=True)}">删除</button>'
                )
            if can_view_logs(user):
                actions.append(f'<a href="/products/{product["id"]}/logs">日志</a>')
            selector_cell = ""
            if bulk_enabled:
                selector_cell = (
                    f'<td class="table-select-cell"><input type="checkbox" name="product_ids" value="{product["id"]}" style="width:auto;"></td>'
                )
            dynamic_cells = [
                f'<td class="catalog-data-cell">{self.list_layout_cell_markup(field, payload)}</td>'
                for field in configured_list_fields
            ]
            version_parts = [f'<span class="table-version-label">{html.escape(self.version_label(product))}</span>']
            if revision_badge:
                version_parts.append(revision_badge)
            rows.append(
                f"""
                <tr class="catalog-row">
                  {selector_cell}
                  <td class="table-id-cell"><a class="table-id-link" href="/products/{product['id']}">#{product['id']}</a></td>
                  {"".join(dynamic_cells)}
                  <td class="table-status-cell"><span class="pill">{html.escape(payload.get('status_label', ''))}</span></td>
                  <td class="table-version-cell"><div class="table-version-stack">{''.join(version_parts)}</div></td>
                  <td class="table-days-cell">{self.list_value_markup(payload.get('elapsed_days_label', '未开始'), mono=True)}</td>
                  <td class="table-initiator-cell">{self.list_value_markup(product.get('creator_name'))}</td>
                  <td class="table-updated-cell">{self.list_value_markup(self.format_list_timestamp(product.get('updated_at')), mono=True)}</td>
                  <td class="table-actions-cell"><div class="table-action-links">{"".join(actions)}</div></td>
                </tr>
                """
            )
        dynamic_headers = [f"<th>{html.escape(field.label)}</th>" for field in configured_list_fields]
        selector_header = '<th class="table-select-head"><input type="checkbox" id="toggle-all-products" style="width:auto;"></th>' if bulk_enabled else ""
        table_column_keys = []
        if bulk_enabled:
            table_column_keys.append("__select__")
        table_column_keys.append("__id__")
        table_column_keys.extend(field.key for field in configured_list_fields)
        table_column_keys.extend(
            [
                "__status__",
                "__version__",
                "__elapsed__",
                "__initiator__",
                "__updated__",
                "__actions__",
            ]
        )
        table_column_count = 7 + len(configured_list_fields) + (1 if bulk_enabled else 0)
        new_button = (
            '<a class="pill" href="/products/new">新建资料</a>'
            if can_create_product(user)
            else ""
        )
        import_button = (
            '<a class="pill" href="/import">导入 Excel</a>'
            if can_import_product_excel(user)
            else ""
        )
        import_image_button = (
            '<a class="pill" href="/import-images">导入图片</a>'
            if can_import_product_images(user)
            else ""
        )
        operations_panel = ""
        insights_grid_class = "products-insights-grid"
        if user["department"] == "C":
            stats_markup = f"""
            <div class="stats">
              <div class="stat-card"><span>总接收</span><strong>{c_receipt_stats.get('received', 0)}</strong></div>
              <div class="stat-card"><span>近7天新增</span><strong>{c_receipt_stats.get('recent_created', 0)}</strong></div>
              <div class="stat-card"><span>待接收</span><strong>{c_receipt_stats.get('pending', 0)}</strong></div>
            </div>
            """
            insights_grid_class += " products-insights-single"
        elif user["department"] in {"A", "EXECUTIVE"}:
            stats_markup = f"""
            <div class="stats">
              <div class="stat-card"><span>总资料数</span><strong>{stats.get('A', 0) + stats.get('B', 0) + stats.get('C', 0)}</strong></div>
              <div class="stat-card"><span>已完成</span><strong>{workflow_stats.get('published', 0) + workflow_stats.get('received', 0)}</strong></div>
              <div class="stat-card"><span>近 7 天新增</span><strong>{recent_stats.get('recent_created', 0)}</strong></div>
              <div class="stat-card"><span>待商品部填写</span><strong>{workflow_stats.get('pending', 0)}</strong></div>
              <div class="stat-card"><span>已删除</span><strong>{lifecycle_stats.get('deleted', 0)}</strong></div>
            </div>
            """
            insights_grid_class += " products-insights-single"
        elif user["department"] == "B" or is_admin(user):
            stats_markup = f"""
            <div class="stats">
              <div class="stat-card"><span>已完成</span><strong>{b_dashboard_stats.get('completed', 0)}</strong></div>
              <div class="stat-card"><span>近7天新增</span><strong>{b_dashboard_stats.get('recent_submitted_to_b', 0)}</strong></div>
              <div class="stat-card"><span>待完成</span><strong>{b_dashboard_stats.get('pending_completion', 0)}</strong></div>
              <div class="stat-card"><span>待接收</span><strong>{b_dashboard_stats.get('awaiting_receipt', 0)}</strong></div>
              <div class="stat-card"><span>近7天退回</span><strong>{b_dashboard_stats.get('recent_returned_to_a', 0)}</strong></div>
            </div>
            """
            insights_grid_class += " products-insights-single"
        else:
            stats_markup = ""
        table_description = (
            (
                "当前为运营部汇总监控视图，已合并天猫、唯品及同款资料，仅展示运营部可读取字段与整体接收状态。"
                if is_department_monitor(user)
                else "当前列表只显示本运营归属可读取字段。状态为已完成的资料可逐条或批量接收，接收状态仅记录在当前账号下。页面、Excel 导出和 JSON 调用会保持同样的渠道范围。"
            )
            if user["department"] == "C"
            else "这里汇总了当前账号可见的商品资料。你可以按部门、协作阶段和资料完成情况快速筛选，再进入详情、日志或批量处理。"
        )
        bulk_tools_markup = self.render_bulk_tools(user)
        lifecycle_filter_markup = f"""
              <select name="lifecycle_status">
                <option value="">全部类型</option>
                <option value="active" {"selected" if lifecycle_filter == "active" else ""}>正常</option>
                <option value="archived" {"selected" if lifecycle_filter == "archived" else ""}>已归档</option>
                <option value="deleted" {"selected" if lifecycle_filter == "deleted" else ""}>已删除</option>
              </select>
        """
        if user["department"] == "C":
            status_filter_markup = f"""
              <select name="status">
                <option value="">全部接收状态</option>
                <option value="published" {"selected" if status_filter == "published" else ""}>待接收</option>
                <option value="received" {"selected" if status_filter == "received" else ""}>已接收</option>
              </select>
            """
        elif user["department"] in {"A", "EXECUTIVE"}:
            status_filter_markup = f"""
              <select name="status">
                <option value="">全部状态</option>
                <option value="draft" {"selected" if status_filter == "draft" else ""}>跟单部填写中</option>
                <option value="pending" {"selected" if status_filter == "pending" else ""}>待商品部填写</option>
                <option value="published" {"selected" if status_filter == "published" else ""}>已完成</option>
              </select>
              {lifecycle_filter_markup}
            """
        elif user["department"] == "B":
            status_filter_markup = f"""
              <select name="status">
                <option value="">全部状态</option>
                <option value="pending" {"selected" if status_filter == "pending" else ""}>待完成</option>
                <option value="published" {"selected" if status_filter == "published" else ""}>已完成</option>
              </select>
              {lifecycle_filter_markup}
            """
        else:
            status_filter_markup = f"""
              <select name="status">
                <option value="">全部状态</option>
                <option value="draft" {"selected" if status_filter == "draft" else ""}>跟单部填写中</option>
                <option value="pending" {"selected" if status_filter == "pending" else ""}>待商品部填写</option>
                <option value="published" {"selected" if status_filter == "published" else ""}>已完成</option>
                <option value="received" {"selected" if status_filter == "received" else ""}>已接收</option>
              </select>
              {lifecycle_filter_markup}
            """
        department_filter_markup = (
            f"""
                <select name="department">
                  <option value="">全部部门</option>
                  <option value="A" {"selected" if department_filter == "A" else ""}>跟单部</option>
                  <option value="B" {"selected" if department_filter == "B" else ""}>商品部</option>
                </select>
            """
            if is_admin(user)
            else f'''
                <input type="hidden" name="department" value="">
                <div class="filter-department-context" aria-label="当前部门">{html.escape(department_label(user.get("department")))}</div>
            '''
        )
        editor_dashboard = user["department"] in {"A", "B", "EXECUTIVE"} or is_admin(user)
        stats_description = (
            "先掌握接收进度，再进入资料列表处理待接收资料。"
            if user["department"] == "C"
            else (
                "先查看资料概览，再进入资料列表处理。"
                if editor_dashboard
                else "把阶段分布、总量和近期节奏统一放在页面最后，浏览主列表后再集中查看更清楚。"
            )
        )
        insights_panel = f"""
        <section class="{insights_grid_class}">
          <section class="panel products-stats-panel">
            <div class="eyebrow">Catalog Summary</div>
            <h2>资料概览</h2>
            <p class="table-note">{stats_description}</p>
            {stats_markup}
          </section>
          {operations_panel}
        </section>
        """
        insights_before_list = insights_panel if user["department"] == "C" or editor_dashboard else ""
        insights_after_list = "" if user["department"] == "C" or editor_dashboard else insights_panel
        dashboard_open = (
            '<div class="products-c-dashboard"><div class="products-c-overview-stack">'
            if user["department"] == "C"
            else ('<div class="products-editor-dashboard"><div class="products-editor-overview-stack">' if editor_dashboard else "")
        )
        overview_close = "</div>" if user["department"] == "C" or editor_dashboard else ""
        dashboard_close = "</div>" if user["department"] == "C" or editor_dashboard else ""
        products_intro = (
            (
                "管理员正在按运营部汇总口径查看已完成资料、待接收资料与已接收资料。"
                if is_department_monitor(user)
                else f"{operating_channel_label(user.get('operating_channel'))}运营账号可读取本归属渠道及同款资料，并对已完成资料进行接收确认。"
            )
            if user["department"] == "C"
            else (
                "当前为总经办只读模式，可查看与跟单部相同范围的商品资料，并下载所需资料。"
                if is_executive_read_only(user)
                else "用一套资料底库承接 Excel 模板、多人协作、权限隔离和后续系统调用。跟单部负责主体资料，商品部补充品类、图片、上新价格、上新渠道和资料完成，运营部只读取管理员开放的完成资料。"
            )
        )
        filter_intro = (
            "按款号、商品名称或品牌筛选资料。"
            if user["department"] == "C"
            else "输入款号会在精确且唯一命中时直接打开资料详情；输入商品名称、品牌或模糊关键词时，仍保留列表筛选结果。"
        )
        if user["department"] == "C" and not is_department_monitor(user):
            c_note = ""
        layout_settings_button = (
            '<a class="pill" href="/settings/list-layout">列表字段设置</a>'
            if not is_executive_read_only(user)
            else ""
        )
        content = f"""
        {dashboard_open}
        <section class="products-top-grid">
          <div class="panel products-overview-card">
            <div class="eyebrow">{html.escape(console_eyebrow)}</div>
            <h1>商品资料后台</h1>
            <p>{html.escape(products_intro)}</p>
            <div class="tools">
              <a class="pill" href="/products">资料列表</a>
              {layout_settings_button}
              {new_button}
              {import_button}
              {import_image_button}
              {self.export_menu(user)}
              {'<a class="pill" href="/products/review">流转看板</a>' if is_admin(user) else ''}
            </div>
          </div>
          <section id="products-search-filter" class="panel products-filter-card query-anchor">
            <div class="eyebrow">List Workspace</div>
            <h2>搜索与筛选</h2>
            <p class="table-note">{html.escape(filter_intro)}</p>
            {notice_block}
            {c_note}
            <form class="products-filter-form" method="get" action="/products#products-search-filter">
                <input class="products-search-field" name="q" value="{html.escape(keyword)}" placeholder="输入款号直达详情，或按名称/品牌搜索">
                {department_filter_markup}
                {status_filter_markup}
                <button class="products-filter-submit" type="submit">筛选</button>
            </form>
          </section>
        </section>
        {insights_before_list}
        {overview_close}
        <div class="products-main-stack">
          <section class="panel">
          <div class="list-intro">
            <div class="list-intro-main">
              <div class="eyebrow">Catalog Table</div>
              <h2>资料列表</h2>
              <p class="table-note">{html.escape(table_description)} 所有账号都可先勾选资料，再导出勾选条目；按住 Shift 再点击勾选框，可连续选中一段资料。</p>
            </div>
            {bulk_tools_markup}
          </div>
          <form id="products-export-form" method="get" action="/export.xlsx">
            <input type="hidden" name="selected" id="export-selected-products" value="">
          </form>
          <form id="products-bulk-form" method="post" action="/products/bulk">
            <input type="hidden" name="confirm_text" id="list-delete-confirm-text" value="">
            <input type="hidden" name="return_to" value="{html.escape(return_to_path)}">
          <div class="table-wrap products-list-scroll-wrap">
            <table class="catalog-table">
              <thead>
                <tr>
                  {selector_header}
                  <th>ID</th>
                  {"".join(dynamic_headers)}
                  <th>状态</th>
                  <th>修改版本</th>
                  <th>历时天数</th>
                  <th>发起人</th>
                  <th>最后更新</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows) if rows else f'<tr><td colspan="{str(table_column_count)}"><div class="empty-state">暂无符合条件的商品资料。</div></td></tr>'}
              </tbody>
            </table>
          </div>
          </form>
          <script>
            (() => {{
              const bulkForm = document.getElementById("products-bulk-form");
              const exportField = document.getElementById("export-selected-products");
              const toggleAll = document.getElementById("toggle-all-products");
              if (!bulkForm) return;
              const rowCheckboxes = () => Array.from(bulkForm.querySelectorAll('input[name="product_ids"]'));
              let lastFocusedCheckbox = null;
              const syncSelected = () => {{
                const selectedValue = rowCheckboxes()
                  .filter((item) => item.checked)
                  .map((item) => item.value)
                  .join(",");
                if (exportField) {{
                  exportField.value = selectedValue;
                }}
                document.querySelectorAll("[data-export-selected='1']").forEach((item) => {{
                  const baseHref = item.getAttribute("data-base-href") || "/export.xlsx?mode=selected";
                  item.setAttribute("href", selectedValue ? `${{baseHref}}&selected=${{encodeURIComponent(selectedValue)}}` : baseHref);
                }});
              }};
              const syncToggleAllState = () => {{
                if (!toggleAll) return;
                const items = rowCheckboxes();
                const checkedCount = items.filter((checkbox) => checkbox.checked).length;
                toggleAll.checked = items.length > 0 && checkedCount === items.length;
                toggleAll.indeterminate = checkedCount > 0 && checkedCount < items.length;
              }};
              const applyRangeSelection = (targetCheckbox) => {{
                if (!lastFocusedCheckbox || lastFocusedCheckbox === targetCheckbox) return;
                const items = rowCheckboxes();
                const startIndex = items.indexOf(lastFocusedCheckbox);
                const endIndex = items.indexOf(targetCheckbox);
                if (startIndex < 0 || endIndex < 0) return;
                const [fromIndex, toIndex] = startIndex < endIndex
                  ? [startIndex, endIndex]
                  : [endIndex, startIndex];
                items.slice(fromIndex, toIndex + 1).forEach((checkbox) => {{
                  checkbox.checked = targetCheckbox.checked;
                }});
              }};
              if (toggleAll) {{
                toggleAll.addEventListener("change", () => {{
                  rowCheckboxes().forEach((item) => {{
                    item.checked = toggleAll.checked;
                    item.dataset.rangeIntent = "0";
                  }});
                  toggleAll.indeterminate = false;
                  syncSelected();
                }});
              }}
              rowCheckboxes().forEach((item) => {{
                item.addEventListener("click", (event) => {{
                  item.dataset.rangeIntent = event.shiftKey ? "1" : "0";
                }});
                item.addEventListener("keydown", (event) => {{
                  if ((event.key === " " || event.key === "Spacebar" || event.key === "Enter") && event.shiftKey) {{
                    item.dataset.rangeIntent = "1";
                  }}
                }});
                item.addEventListener("change", () => {{
                  if (item.dataset.rangeIntent === "1") {{
                    applyRangeSelection(item);
                  }}
                  item.dataset.rangeIntent = "0";
                  lastFocusedCheckbox = item;
                  syncToggleAllState();
                  syncSelected();
                }});
              }});
              syncToggleAllState();
              syncSelected();
            }})();
            (() => {{
              const bulkForm = document.getElementById("products-bulk-form");
              const confirmField = document.getElementById("list-delete-confirm-text");
              if (!bulkForm || !confirmField) return;
              bulkForm.querySelectorAll("[data-delete-button='1']").forEach((button) => {{
                button.addEventListener("click", (event) => {{
                  confirmField.value = "";
                  const productLabel = button.getAttribute("data-product-label") || "当前资料";
                  const typedText = window.prompt(`删除后该条资料会从普通列表隐藏，管理员仍可恢复。\\n请输入 DELETE 确认删除：${{productLabel}}`, "");
                  if (typedText === null) {{
                    event.preventDefault();
                    return;
                  }}
                  const normalized = typedText.trim();
                  if (normalized !== "DELETE") {{
                    event.preventDefault();
                    window.alert("未输入 DELETE，已取消删除。");
                    return;
                  }}
                  confirmField.value = normalized;
                }});
              }});
            }})();
            (() => {{
              const table = document.querySelector(".catalog-table");
              if (!table) return;
              const body = document.body;
              const widthStorageKey = "catalog_column_widths_v1";
              const columnKeys = {json.dumps(table_column_keys)};
              table.querySelectorAll("thead tr").forEach((row) => {{
                Array.from(row.children).forEach((cell, index) => {{
                  if (!(cell instanceof HTMLElement)) return;
                  const key = columnKeys[index];
                  if (!key) return;
                  cell.dataset.columnKey = key;
                }});
              }});
              table.querySelectorAll("tbody tr").forEach((row) => {{
                Array.from(row.children).forEach((cell, index) => {{
                  if (!(cell instanceof HTMLElement)) return;
                  const key = columnKeys[index];
                  if (!key) return;
                  cell.dataset.columnKey = key;
                }});
              }});
              table.querySelectorAll("thead th[data-column-key]").forEach((header) => {{
                if (!(header instanceof HTMLElement)) return;
                if (header.querySelector(".catalog-resize-handle")) return;
                header.classList.add("catalog-resizable-head");
                const labelText = (header.textContent || "").trim() || "当前";
                const handle = document.createElement("button");
                handle.type = "button";
                handle.className = "catalog-resize-handle";
                handle.setAttribute("aria-label", `调整${{labelText}}列宽`);
                header.appendChild(handle);
              }});
              let widthMap = {{}};
              try {{
                widthMap = JSON.parse(window.localStorage.getItem(widthStorageKey) || "{{}}");
              }} catch (error) {{
                widthMap = {{}};
              }}
              const applyWidth = (columnKey, width) => {{
                if (!columnKey || !width) return;
                table.querySelectorAll(`[data-column-key="${{columnKey}}"]`).forEach((cell) => {{
                  cell.style.width = `${{width}}px`;
                  cell.style.minWidth = `${{width}}px`;
                  cell.style.maxWidth = `${{Math.max(width, 48)}}px`;
                }});
              }};
              Object.entries(widthMap).forEach(([columnKey, width]) => {{
                const widthNumber = Number(width);
                if (Number.isFinite(widthNumber) && widthNumber >= 56) {{
                  applyWidth(columnKey, widthNumber);
                }}
              }});
              let activeResize = null;
              const saveWidths = () => {{
                window.localStorage.setItem(widthStorageKey, JSON.stringify(widthMap));
              }};
              const onPointerMove = (event) => {{
                if (!activeResize) return;
                const nextWidth = Math.max(56, Math.round(activeResize.startWidth + (event.clientX - activeResize.startX)));
                widthMap[activeResize.columnKey] = nextWidth;
                applyWidth(activeResize.columnKey, nextWidth);
              }};
              const onPointerUp = () => {{
                if (!activeResize) return;
                body.classList.remove("catalog-resizing");
                saveWidths();
                window.removeEventListener("pointermove", onPointerMove);
                window.removeEventListener("pointerup", onPointerUp);
                activeResize = null;
              }};
              table.querySelectorAll(".catalog-resize-handle").forEach((handle) => {{
                handle.addEventListener("pointerdown", (event) => {{
                  const header = handle.closest("[data-column-key]");
                  if (!(header instanceof HTMLElement)) return;
                  event.preventDefault();
                  const columnKey = header.getAttribute("data-column-key") || "";
                  if (!columnKey) return;
                  activeResize = {{
                    columnKey,
                    startX: event.clientX,
                    startWidth: header.getBoundingClientRect().width,
                  }};
                  body.classList.add("catalog-resizing");
                  window.addEventListener("pointermove", onPointerMove);
                  window.addEventListener("pointerup", onPointerUp);
                }});
              }});
            }})();
          </script>
          </section>
        </div>
        {insights_after_list}
        {dashboard_close}
        """
        return self.page(f"资料列表 - {brand_name}", content, user, current_page="products", back_href="/modules")

    def render_modules_home(self, user) -> str:
        billing_entry = ""
        if can_access_billing_module(user):
            billing_entry = f"""
            <article class="panel">
              <div class="eyebrow">Section 02</div>
              <h2>账单与结算</h2>
              <p>覆盖平台账单上传、品牌月账单整理、供应商结算拆分和年度支付汇总。C 只负责提交平台账单，A/B 负责主要处理流程。</p>
              <div class="tools" style="margin-top:18px; margin-bottom:0;">
                <a class="pill" href="/billing">进入板块二</a>
              </div>
            </article>
            """
        content = f"""
        <div class="modules-home">
          <div class="detail-grid">
            <article class="panel">
              <div class="eyebrow">Section 01</div>
              <h2>商品资料后台</h2>
              <p>继续处理跟单部、商品部、运营部之间的商品资料填写、流转、字段开放、导入导出和版本管理。</p>
              <div class="tools" style="margin-top:18px; margin-bottom:0;">
                <a class="pill" href="/products">进入板块一</a>
              </div>
            </article>
            {billing_entry}
          </div>
          <footer class="modules-home-watermark" aria-hidden="true"><span>远山含黛色，玲珑晓楼阁</span></footer>
        </div>
        """
        return self.page("首页 - 商品资料后台", content, user, current_page="modules")

    def render_b_billing_home(self, user) -> str:
        workboard_rows = []
        for item in db.billing_month_workboard(self.db_path)[:12]:
            month_key_raw = str(item["month_key"])
            month_key = html.escape(month_key_raw)
            platform_total = int(item.get("platform_total") or 0)
            platform_main_count = int(item.get("platform_main_count") or 0)
            platform_submitted_count = int(item.get("platform_submitted_count") or 0)
            brand_version_count = int(item.get("brand_version_count") or 0)
            platform_submitted = bool(platform_total and platform_submitted_count >= platform_total)
            brand_started = bool(item.get("brand_has_version"))
            brand_submitted = str(item.get("brand_status") or "") == "submitted_to_a"
            platform_summary = db.platform_bill_month_summary(self.db_path, month_key_raw)
            pending_return_labels = [
                str(platform.get("platform_label") or "")
                for platform in platform_summary.get("platforms", [])
                if str((platform.get("return_request") or {}).get("status") or "") == "pending"
            ]

            if not platform_submitted:
                stage_label = "平台账单不完整"
                stage_class = "warning"
            elif pending_return_labels:
                stage_label = f"{'、'.join(pending_return_labels)}申请退回"
                stage_class = "warning"
            elif brand_submitted:
                stage_label = "品牌月账单已提交"
                stage_class = "done"
            elif not brand_started:
                stage_label = "待整理品牌月账单"
                stage_class = "normal"
            else:
                stage_label = "品牌月账单制作中"
                stage_class = "warning"

            workboard_rows.append(
                f"""
                <tr>
                  <td>{month_key}</td>
                  <td><a href="/billing/platform-bills?month={month_key}">{html.escape(item["platform_status_label"])}</a><br><span class="meta">账单文件 {platform_main_count} / {platform_total} · 已提交 {platform_submitted_count} / {platform_total}</span></td>
                  <td><a href="/billing/brand-bills?month={month_key}">{html.escape(item["brand_status_label"])}</a><br><span class="meta">版本 {brand_version_count}</span></td>
                  <td><span class="board-risk-pill board-risk-{stage_class}">{html.escape(stage_label)}</span></td>
                </tr>
                """
            )
        content = f"""
        <div class="billing-b-home">
          <div class="page-back-row billing-b-home-back">
            <a class="page-back-link" href="/modules">&larr; 返回上一层</a>
          </div>
          <section class="panel billing-b-workboard">
            <h1>账单与结算-月度看板</h1>
            <div class="table-wrap">
              <table class="catalog-table">
              <thead>
                <tr>
                  <th>月份</th>
                  <th>平台账单</th>
                  <th>品牌月账单</th>
                  <th>当前节点</th>
                </tr>
                </thead>
                <tbody>
                  {''.join(workboard_rows) if workboard_rows else '<tr><td colspan="4"><div class="empty-state">当前还没有形成月度流程记录。运营部提交平台账单后，会在这里显示商品部需要跟进的月份。</div></td></tr>'}
                </tbody>
              </table>
            </div>
          </section>
          <div class="detail-grid billing-b-entry-grid" style="margin-top:18px;">
            <article class="panel">
              <h2>平台账单</h2>
              <p>按月份查看各平台上传与提交状态，下载已提交账单，并处理运营部提出的退回申请。</p>
              <div class="tools" style="margin-top:18px; margin-bottom:0;">
                <a class="pill" href="/billing/platform-bills">进入平台账单</a>
              </div>
            </article>
            <article class="panel">
              <h2>品牌月账单</h2>
              <p>将平台账单整理为品牌完整月账单，填写月销看板与账单明细后，确认提交给跟单部。</p>
              <div class="tools" style="margin-top:18px; margin-bottom:0;">
                <a class="pill" href="/billing/brand-bills">进入品牌月账单</a>
              </div>
            </article>
          </div>
        </div>
        """
        return self.page("账单与结算 - 商品资料后台", content, user, current_page="billing")

    def render_a_billing_home(self, user, query) -> str:
        notice = str(query.get("notice", "")).strip()
        requested_year = str(query.get("year", "")).strip() or str(datetime.now(timezone.utc).year)
        try:
            board_year = db.normalize_monthly_board_year(requested_year)
        except ValueError:
            board_year = datetime.now(timezone.utc).year
            notice = notice or "年度格式不正确，已显示当前年度。"
        board = db.supplier_monthly_board_for_year(self.db_path, board_year)
        read_only_board = is_executive_read_only(user) or is_department_monitor(user)
        board_input_attributes = " readonly" if read_only_board else ""

        def input_value(value, *, amount: bool = False) -> str:
            if value is None:
                return ""
            return f"{float(value):.2f}" if amount else str(int(value))

        month_numbers = list(range(12, 0, -1))
        month_headers = "".join(f"<th>{month_no}月</th>" for month_no in month_numbers)
        supplier_inputs = "".join(
            f'<td><input type="number" name="supplier_count_{month_no}" min="0" step="1" value="{html.escape(input_value((board["months"].get(month_no) or {}).get("payable_supplier_count")), quote=True)}" aria-label="{month_no}月应付供应商"{board_input_attributes}></td>'
            for month_no in month_numbers
        )
        amount_inputs = "".join(
            f'<td><input type="number" name="payable_amount_{month_no}" min="0" step="0.01" value="{html.escape(input_value((board["months"].get(month_no) or {}).get("payable_total_amount"), amount=True), quote=True)}" aria-label="{month_no}月应付总金额"{board_input_attributes}></td>'
            for month_no in month_numbers
        )
        supplier_total = input_value(board.get("supplier_count_total"))
        amount_total = input_value(board.get("payable_amount_total"), amount=True)
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        content = f"""
        <div class="billing-a-home">
          <section id="billing-monthly-board" class="panel query-anchor">
            <div class="a-monthly-board-head">
              <h1>账单与结算-月度看板</h1>
              <form class="a-monthly-board-query" method="get" action="/billing#billing-monthly-board">
                <label>
                  <input type="number" name="year" min="2000" max="2100" value="{board_year}" aria-label="查询年度">
                </label>
                <button type="submit" class="ghost-button">查询</button>
              </form>
            </div>
            {notice_block}
            {'<p class="meta">总经办账号为只读，可查看月度数据与下载相关账单，不能修改或保存。</p>' if read_only_board else ''}
            <form class="a-monthly-board-form" method="post" action="/billing/monthly-board">
              <input type="hidden" name="year" value="{board_year}">
              <div class="table-wrap">
                <table class="catalog-table a-monthly-board-table">
                  <thead>
                    <tr>
                      <th>项目</th>
                      {month_headers}
                      <th class="a-monthly-board-total">总计</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <th scope="row">应付供应商</th>
                      {supplier_inputs}
                      <td id="a-monthly-board-supplier-total" class="a-monthly-board-total">{html.escape(supplier_total)}</td>
                    </tr>
                    <tr>
                      <th scope="row">应付总金额</th>
                      {amount_inputs}
                      <td id="a-monthly-board-amount-total" class="a-monthly-board-total">{html.escape(amount_total)}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              {'' if read_only_board else '<div class="a-monthly-board-save"><button type="submit">保存</button></div>'}
            </form>
            <script>
              (() => {{
                const form = document.querySelector(".a-monthly-board-form");
                if (!form) return;
                const supplierTotal = document.getElementById("a-monthly-board-supplier-total");
                const amountTotal = document.getElementById("a-monthly-board-amount-total");
                const updateTotal = (selector, target, format) => {{
                  const values = Array.from(form.querySelectorAll(selector));
                  let hasValue = false;
                  const total = values.reduce((sum, input) => {{
                    const rawValue = input.value.trim();
                    if (!rawValue) return sum;
                    const value = Number(rawValue);
                    if (!Number.isFinite(value)) return sum;
                    hasValue = true;
                    return sum + value;
                  }}, 0);
                  target.textContent = hasValue ? format(total) : "";
                }};
                const refreshTotals = () => {{
                  updateTotal('input[name^="supplier_count_"]', supplierTotal, (value) => String(value));
                  updateTotal('input[name^="payable_amount_"]', amountTotal, (value) => value.toFixed(2));
                }};
                form.addEventListener("input", refreshTotals);
              }})();
            </script>
          </section>
          <div class="detail-grid billing-a-entry-grid" style="margin-top:18px;">
            <article class="panel">
              <h2>品牌月账单</h2>
              <p>查看商品部已整理并提交的品牌月账单，处理需要退回补充的资料。</p>
              <div class="tools" style="margin-top:18px; margin-bottom:0;">
                <a class="pill" href="/billing/brand-bills">进入品牌月账单</a>
              </div>
            </article>
            <article class="panel">
              <h2>供应商结算</h2>
              <p>按供应商编码拆分应付账单，维护每月结算及本年度已付、待付汇总。</p>
              <div class="tools" style="margin-top:18px; margin-bottom:0;">
                <a class="pill" href="/billing/supplier-settlements">进入供应商结算</a>
              </div>
            </article>
          </div>
        </div>
        """
        return self.page("账单与结算 - 商品资料后台", content, user, current_page="billing", back_href="/modules")

    def render_billing_home(self, user, query) -> str:
        role_label = "系统管理员" if is_admin(user) else department_label(user.get("department"))
        can_upload = can_upload_platform_bills(user)
        can_brand = can_access_brand_bills(user)
        can_supplier = can_access_supplier_settlements(user)
        if user.get("department") == "C":
            content = """
            <div class="detail-grid billing-c-home-grid">
              <article class="panel">
                <div class="eyebrow">Section 02</div>
                <h2>账单与结算</h2>
                <p>当前账号仅处理每月平台账单。完成各平台账单文件上传后，逐个平台确认提交给商品部继续整理。</p>
              </article>
              <article class="panel">
                <div class="eyebrow">Part 01</div>
                <h2>平台账单</h2>
                <p>按月份进入工作台，分别上传各平台的账单文件。每个平台可上传 1 至 3 份文件，确认提交后不可再修改。</p>
                <div class="tools" style="margin-top:18px; margin-bottom:0;">
                  <a class="pill" href="/billing/platform-bills">进入平台账单</a>
                </div>
              </article>
            </div>
            """
            return self.page("账单与结算 - 商品资料后台", content, user, current_page="billing", back_href="/modules")
        if user.get("department") == "B":
            return self.render_b_billing_home(user)
        if user.get("department") in {"A", "EXECUTIVE"}:
            return self.render_a_billing_home(user, query)
        content = f"""
        <section class="hero billing-admin-hero">
          <div class="panel">
            <div class="eyebrow">Section 02</div>
            <h1>账单与结算</h1>
            <p>板块二用于承接平台账单上传、品牌月账单整理和供应商结算汇总。当前先完成板块骨架与权限框架，后续再接入模板导入、文件上传与支付统计。</p>
          </div>
        </section>
        <div class="detail-grid">
          <article class="panel">
            <div class="eyebrow">Part 01</div>
            <h2>平台账单</h2>
            <p>每个月按平台清单逐个平台维护账单文件，每个平台统一维护 1 到 3 份账单文件。C 负责提交，B 负责读取和后续整理；管理员可以维护平台名称与平台数量。</p>
            <div class="tools" style="margin-top:18px; margin-bottom:0;">
              <a class="pill" href="/billing/platform-bills">进入平台账单</a>
            </div>
            <p class="table-note">当前账号权限：{"可提交" if can_upload else ("可查看处理" if can_access_platform_bills(user) else "不可访问")}</p>
          </article>
          <article class="panel">
            <div class="eyebrow">Part 02</div>
            <h2>品牌月账单</h2>
            <p>由商品部把平台账单整理为品牌完整月账单，再流转给跟单部做供应商结算。后续会接入固定 Excel 模板与版本记录。</p>
            <div class="tools" style="margin-top:18px; margin-bottom:0;">
              {'<a class="pill" href="/billing/brand-bills">进入品牌月账单</a>' if can_brand else '<span class="meta">当前账号暂不可访问</span>'}
            </div>
          </article>
          <article class="panel">
            <div class="eyebrow">Part 03</div>
            <h2>供应商结算</h2>
            <p>由跟单部按供应商编码拆分应付账单，并汇总每个供应商历年分月账单、本年度累计已支付和待支付。支持一个编码对应多个开票抬头。</p>
            <div class="tools" style="margin-top:18px; margin-bottom:0;">
              {'<a class="pill" href="/billing/supplier-settlements">进入供应商结算</a>' if can_supplier else '<span class="meta">当前账号暂不可访问</span>'}
            </div>
          </article>
        </div>
        """
        return self.page("账单与结算 - 商品资料后台", content, user, current_page="billing", back_href="/modules")

    def render_platform_bills_page(self, user, query) -> str:
        today = datetime.now(timezone.utc)
        month_key = query.get("month", "").strip() or today.strftime("%Y-%m")
        notice = query.get("notice", "").strip()
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        month_summary = None
        month_record = None
        try:
            month_summary = db.platform_bill_month_summary(self.db_path, month_key)
            month_record = month_summary["month"]
        except ValueError:
            month_summary = None
            month_record = None
        all_months = db.list_billing_months(self.db_path)
        platform_rows = []
        status = month_record.get("status") if month_record else "draft"
        status_label = db.billing_month_status_label(status)
        all_current_platforms = month_summary["platforms"] if month_summary else self.empty_platform_bill_rows()
        platform_configs = db.platform_bill_platform_configs_for_month(self.db_path, month_key)
        allowed_platform_codes = set(
            platform_bill_platform_codes_for_user(
                user,
                (item.get("platform_code") for item in all_current_platforms),
            )
        )
        current_platforms = [
            item for item in all_current_platforms if item.get("platform_code") in allowed_platform_codes
        ]
        requested_platform_code = str(query.get("platform", "")).strip()
        selected_platform_item = next(
            (item for item in current_platforms if item["platform_code"] == requested_platform_code),
            None,
        )
        selected_platform_code = str(selected_platform_item.get("platform_code") or "") if selected_platform_item else ""
        platform_select_options = "".join(
            f'<option value="{html.escape(item["platform_code"])}" {"selected" if item["platform_code"] == selected_platform_code else ""}>{html.escape(item["platform_label"])}</option>'
            for item in current_platforms
        )
        platform_picker_query = {"month": month_key}
        if is_department_monitor(user):
            platform_picker_query["monitor_department"] = str(user.get("monitor_department") or "")
        platform_picker_base_url = "/billing/platform-bills?" + urlencode(platform_picker_query) + "&platform="
        platform_picker_markup = f"""
        <div class="billing-platform-cell">
          <label class="billing-platform-picker-label">
            <span>选择平台</span>
            <select aria-label="选择平台" onchange="window.location.href='{html.escape(platform_picker_base_url, quote=True)}' + encodeURIComponent(this.value) + '#billing-workspace'">
              <option value="">请选择平台</option>
              {platform_select_options}
            </select>
          </label>
        </div>
        """
        if user.get("department") == "C" and not current_platforms:
            platform_picker_markup = """
            <div class=\"billing-platform-cell\">
              <span class=\"meta\">当前账号未配置可处理的平台，请联系管理员检查运营归属。</span>
            </div>
            """
        visible_platform_total = len(current_platforms)
        uploaded_platform_count = sum(1 for item in current_platforms if item.get("main_file"))
        submitted_platform_count = sum(1 for item in current_platforms if item.get("submitted"))
        platform_settings_rows = "".join(
            f"""
            <div class="form-grid" style="grid-template-columns: 1.1fr 2fr auto; margin-bottom:10px;">
              <input type="hidden" name="platform_code__{index}" value="{html.escape(item['code'])}">
              <label class="field">
                <span>平台编码</span>
                <input value="{html.escape(item['code'])}" disabled>
              </label>
              <label class="field">
                <span>平台名称</span>
                <input name="platform_label__{index}" value="{html.escape(item['label'])}" placeholder="请输入平台名称">
              </label>
              <div class="field">
                <span>操作</span>
                <label class="billing-platform-delete-option"><input type="checkbox" name="remove_platform_code" value="{html.escape(item['code'])}"> 删除平台</label>
              </div>
            </div>
            """
            for index, item in enumerate(platform_configs)
        )
        for platform_item in ([selected_platform_item] if selected_platform_item else []):
            main_file = platform_item.get("main_file")
            attachments = platform_item.get("attachments") or []
            return_request = platform_item.get("return_request") or {}
            return_request_status = str(return_request.get("status") or "")
            history_files = platform_item.get("history_files") or []
            history_versions: dict[int, list[dict]] = {}
            for history_file in history_files:
                history_versions.setdefault(int(history_file.get("version_no") or 1), []).append(history_file)
            history_markup = ""
            if history_versions:
                history_version_markup = "".join(
                    f"""
                    <div class="billing-history-version">
                      <strong>历史版本 V{version_no}</strong>
                      <ul class="billing-file-list">
                        {''.join(f'<li class="billing-file-item">{self.platform_bill_file_link(file_item, user)}</li>' for file_item in files)}
                      </ul>
                    </div>
                    """
                    for version_no, files in sorted(history_versions.items(), reverse=True)
                )
                history_markup = f"""
                <details class="billing-file-history">
                  <summary>历史版本（{len(history_versions)}）</summary>
                  {history_version_markup}
                </details>
                """
            is_locked = self.platform_item_locked(platform_item, status)
            all_files = ([main_file] if main_file else []) + list(attachments)
            manageable_files = [item for item in all_files if self.can_manage_platform_bill_file(user, item, is_locked)]
            delete_anchor_file = manageable_files[0] if manageable_files else None
            file_count = len(all_files)
            if all_files:
                file_items_markup = "".join(
                    f'<li class="billing-file-item"><span class="billing-file-index">文件 {index}</span>{self.platform_bill_file_link(file_item, user)}</li>'
                    for index, file_item in enumerate(all_files, start=1)
                )
                file_descriptions = (
                    f'<div class="billing-file-block"><div class="billing-file-block-head"><strong>账单文件</strong>'
                    f'<span class="billing-file-count">{file_count} / 3</span></div>'
                    f'<ul class="billing-file-list">{file_items_markup}</ul>{history_markup}</div>'
                )
            else:
                file_descriptions = (
                    '<div class="billing-file-block"><div class="billing-file-block-head"><strong>账单文件</strong>'
                    '<span class="billing-file-count">0 / 3</span></div>'
                    f'<div class="billing-file-empty">当前还没有上传账单文件。</div>{history_markup}</div>'
                )
            upload_panel = ""
            current_operator = "未上传"
            owner_badge_class = "billing-owner-empty"
            owner_note = "当前还没有运营同事上传这个平台的文件。"
            if return_request_status == "pending":
                current_operator = "退回申请中"
                owner_badge_class = "billing-owner-other"
                owner_note = f"{str(return_request.get('requester_name') or '运营同事')} 已申请退回：{str(return_request.get('reason') or '')}"
            elif return_request_status == "approved" and not main_file:
                current_operator = "待重新提交"
                owner_badge_class = "billing-owner-empty"
                owner_note = "退回申请已通过，原文件已保留为历史版本，请上传新版本后再次确认提交。"
            elif main_file:
                if is_locked:
                    current_operator = "已提交"
                    owner_badge_class = "billing-owner-locked"
                    submitted_actor = str(platform_item.get("submitted_by_name") or main_file.get("uploader_name") or "运营同事")
                    owner_note = (
                        f"退回申请已驳回，原提交版本继续有效。"
                        if return_request_status == "rejected"
                        else f"本平台文件已确认提交，并由 {submitted_actor} 锁定流转给商品部。"
                    )
                elif user.get("department") == "C" and int(main_file.get("uploaded_by") or 0) != int(user.get("id") or 0):
                    current_operator = "已上传"
                    owner_badge_class = "billing-owner-other"
                    owner_note = f"上传人：{str(main_file.get('uploader_name') or '其他运营同事')}。你不能查看文件名、下载、删除或补传。"
                else:
                    current_operator = "已上传"
                    owner_badge_class = "billing-owner-self" if user.get("department") == "C" else "billing-owner-locked"
                    owner_note = (
                        "这是你本人上传的平台文件，如需调整请先删除后重新上传。"
                        if user.get("department") == "C"
                        else f"上传人：{str(main_file.get('uploader_name') or '运营同事')}。"
                    )
            upload_disabled = bool(
                is_locked
                or file_count >= 3
                or (
                    user.get("department") == "C"
                    and main_file
                    and int(main_file.get("uploaded_by") or 0) not in {0, int(user.get("id") or 0)}
                )
            )
            if is_locked:
                upload_mode_label = "该平台已确认提交，账单文件已锁定。"
            elif file_count >= 3:
                upload_mode_label = "该平台最多保留 3 份账单文件；如需调整，请先删除后重新上传。"
            elif file_count:
                upload_mode_label = f"当前已上传 {file_count} / 3 份账单文件，可继续补传，超出前请先删除旧文件。"
            else:
                upload_mode_label = "一次可上传 1 到 3 个账单文件。"
            if can_upload_platform_bills(user):
                upload_form_id = f"billing-upload-{platform_item['platform_code']}"
                can_request_return = bool(
                    main_file
                    and is_locked
                    and int(main_file.get("uploaded_by") or 0) == int(user.get("id") or 0)
                    and return_request_status != "pending"
                )
                delete_button_markup = (
                    f"""
                    <form method="post" action="/billing/platform-bills/delete" class="billing-inline-form" onsubmit="return confirm('确认删除该平台下你上传的所有文件吗？');">
                      <input type="hidden" name="month_key" value="{html.escape(month_key)}">
                      <input type="hidden" name="return_platform" value="{html.escape(platform_item['platform_code'])}">
                      <input type="hidden" name="file_id" value="{int(delete_anchor_file.get('id') or 0)}">
                      <input type="hidden" name="delete_scope" value="platform">
                      <button type="submit" class="ghost-button">删除文件</button>
                    </form>
                    """
                    if delete_anchor_file
                    else '<button type="button" class="ghost-button" disabled>删除文件</button>'
                )
                submit_button_markup = (
                    f"""
                    <form method="post" action="/billing/platform-bills/submit" class="billing-inline-form" onsubmit="return confirm('确认将该平台账单提交给商品部吗？提交后该平台将锁定。');">
                      <input type="hidden" name="month_key" value="{html.escape(month_key)}">
                      <input type="hidden" name="platform_code" value="{html.escape(platform_item['platform_code'])}">
                      <button type="submit" {"disabled" if (not main_file or is_locked) else ""}>确认提交</button>
                    </form>
                    """
                )
                return_request_markup = (
                    f"""
                    <form method="post" action="/billing/platform-bills/return-request" class="billing-return-request" onsubmit="return confirm('确认提交退回申请吗？需等待商品部或管理员处理。');">
                      <input type="hidden" name="month_key" value="{html.escape(month_key)}">
                      <input type="hidden" name="platform_code" value="{html.escape(platform_item['platform_code'])}">
                      <input name="reason" required maxlength="200" placeholder="填写退回原因">
                      <button type="submit" class="ghost-button">申请退回</button>
                    </form>
                    """
                    if can_request_return
                    else (
                        f'<div class="billing-return-note">退回申请已提交，等待商品部或管理员处理。</div>'
                        if return_request_status == "pending"
                        else ""
                    )
                )
                upload_panel = f"""
                <div class="billing-actions-panel">
                  <form id="{html.escape(upload_form_id)}" method="post" action="/billing/platform-bills/upload" enctype="multipart/form-data" class="billing-upload-form">
                    <input type="hidden" name="month_key" value="{html.escape(month_key)}">
                    <input type="hidden" name="platform_code" value="{html.escape(platform_item['platform_code'])}">
                    <input type="file" name="upload_files" multiple {"disabled" if upload_disabled else ""}>
                  </form>
                  <div class="billing-actions-primary-row">
                    <button type="submit" form="{html.escape(upload_form_id)}" class="billing-upload-button" {"disabled" if upload_disabled else ""}>上传文件</button>
                    {delete_button_markup}
                  </div>
                  <div class="billing-actions-submit-row">
                    {submit_button_markup}
                  </div>
                  <div class="billing-actions-note">{html.escape(upload_mode_label)}</div>
                  {return_request_markup}
                </div>
                """
            elif (
                return_request_status == "pending"
                and not is_department_monitor(user)
                and (user.get("department") == "B" or is_admin(user))
            ):
                upload_panel = f"""
                <div class="billing-return-decision">
                  <strong>退回申请</strong>
                  <div class="billing-actions-note">{html.escape(str(return_request.get('requester_name') or '运营同事'))}：{html.escape(str(return_request.get('reason') or ''))}</div>
                  <div class="billing-actions-primary-row">
                    <form method="post" action="/billing/platform-bills/return-decision" onsubmit="return confirm('确认退回运营部并保留当前文件为历史版本吗？');">
                      <input type="hidden" name="request_id" value="{int(return_request.get('id') or 0)}">
                      <input type="hidden" name="month_key" value="{html.escape(month_key)}">
                      <input type="hidden" name="platform_code" value="{html.escape(platform_item['platform_code'])}">
                      <input type="hidden" name="decision" value="approve">
                      <button type="submit">退回运营部</button>
                    </form>
                    <form method="post" action="/billing/platform-bills/return-decision" onsubmit="return confirm('确认驳回本次退回申请吗？');">
                      <input type="hidden" name="request_id" value="{int(return_request.get('id') or 0)}">
                      <input type="hidden" name="month_key" value="{html.escape(month_key)}">
                      <input type="hidden" name="platform_code" value="{html.escape(platform_item['platform_code'])}">
                      <input type="hidden" name="decision" value="reject">
                      <button type="submit" class="ghost-button">驳回申请</button>
                    </form>
                  </div>
                </div>
                """
            else:
                upload_panel = '<span class="meta">当前账号只读</span>'
            platform_rows.append(
                f"""
                <tr>
                  <td class="billing-cell-title">
                    {platform_picker_markup}
                  </td>
                  <td>
                    <div class="billing-status-card">
                      <div class="billing-status-head">
                        <span class="billing-owner-badge {owner_badge_class}">{html.escape(current_operator)}</span>
                        <span class="billing-file-count">{file_count} / 3 份</span>
                      </div>
                      <div class="billing-status-note">{html.escape(owner_note)}</div>
                    </div>
                  </td>
                  <td>{file_descriptions}</td>
                  <td>{upload_panel}</td>
                </tr>
                """
            )
        month_cards = []
        for item in all_months[:12]:
            summary = db.platform_bill_month_overview(self.db_path, item["month_key"])
            month_platform_total = int(summary.get("platform_total") or 5) or 5
            main_ready_count = int(summary.get("main_ready_count") or 0)
            submitted_count = int(summary.get("submitted_count") or 0)
            main_progress = max(0, min(100, round(main_ready_count * 100 / month_platform_total)))
            submitted_progress = max(0, min(100, round(submitted_count * 100 / month_platform_total)))
            status_key = str(summary.get("status") or "draft")
            month_cards.append(
                f"""
                <a class="detail-card billing-month-card billing-month-card-{html.escape(status_key)}" href="/billing/platform-bills?month={html.escape(item['month_key'])}" style="text-decoration:none; color:inherit;">
                  <div class="billing-month-card-head">
                    <strong>{html.escape(item['month_key'])}</strong>
                    <span class="billing-status-chip billing-status-{html.escape(status_key)}">{html.escape(summary.get('status_label') or '')}</span>
                  </div>
                  <div class="billing-month-metrics">
                    <div class="billing-metric-row">
                      <span>已上传</span>
                      <div class="billing-progress-track"><span class="billing-progress-fill" style="width:{main_progress}%;"></span></div>
                      <strong>{main_ready_count} / {month_platform_total}</strong>
                    </div>
                    <div class="billing-metric-row">
                      <span>已提交</span>
                      <div class="billing-progress-track"><span class="billing-progress-fill billing-progress-fill-submitted" style="width:{submitted_progress}%;"></span></div>
                      <strong>{submitted_count} / {month_platform_total}</strong>
                    </div>
                    <div class="meta">全部平台上传后即可逐个平台确认提交。</div>
                  </div>
                </a>
                """
            )
        workspace_title = f"{html.escape(month_key)} 当月工作台"
        platform_workspace_rows = "".join(platform_rows) if platform_rows else f"""
        <tr>
          <td class="billing-cell-title">{platform_picker_markup}</td>
          <td colspan="3"><div class="empty-state">请选择平台后查看状态、账单文件和操作。</div></td>
        </tr>
        """
        platform_workspace_content = f"""
        <div class="table-wrap">
          <table class="catalog-table billing-compact-table">
            <colgroup>
              <col style="width:25%;">
              <col style="width:25%;">
              <col style="width:25%;">
              <col style="width:25%;">
            </colgroup>
            <thead>
              <tr>
                <th>平台</th>
                <th>当前状态</th>
                <th>账单文件</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {platform_workspace_rows}
            </tbody>
          </table>
        </div>
        """
        month_cards_markup = f"""
        <div class="detail-grid billing-month-grid" style="margin-top:2px;">
          {''.join(month_cards) if month_cards else '<div class="detail-card"><strong>暂无历史月份</strong><div class="meta">从本月开始上传后，这里会形成月份档案。</div></div>'}
        </div>
        """
        progress_overview_markup = f"""
        <div class="billing-progress-overview billing-progress-overview-compact">
          <div class="stats">
            <div class="stat-card">
              <span>已上传平台</span>
              <strong>{uploaded_platform_count} / {visible_platform_total}</strong>
            </div>
            <div class="stat-card">
              <span>已提交平台</span>
              <strong>{submitted_platform_count} / {visible_platform_total}</strong>
            </div>
          </div>
        </div>
        """
        show_month_cards_in_summary = user.get("department") in {"B", "C"}
        top_summary_markup = month_cards_markup if show_month_cards_in_summary else progress_overview_markup
        bottom_month_cards_markup = "" if show_month_cards_in_summary else month_cards_markup
        content = f"""
        <section class="hero billing-platform-hero">
          <div id="platform-bill-month-query" class="panel billing-month-section query-anchor">
            <div class="eyebrow">Month Overview</div>
            <h2>平台账单</h2>
            <div class="spotlight billing-month-status">
              <div class="billing-month-status-head">
                <strong>当前月份状态</strong>
                <div class="spotlight-value billing-month-status-value">{html.escape(status_label)}</div>
              </div>
              <div class="table-note">每个平台都需要分别确认提交。单个平台确认后会锁定该平台；全部平台都确认完成后，商品部即可把本月视为完整批次继续整理品牌月账单。</div>
            </div>
            {notice_block}
            {top_summary_markup}
            <form method="get" action="/billing/platform-bills#platform-bill-month-query" class="billing-month-picker">
              <label class="field">
                <span>账单查询</span>
                <input type="month" name="month" value="{html.escape(month_key)}">
              </label>
              <div class="tools" style="margin-top:0; margin-bottom:0;">
                <button type="submit">查询</button>
              </div>
            </form>
            {bottom_month_cards_markup}
          </div>
        <section id="billing-workspace" class="panel billing-workspace-panel">
          <div class="eyebrow">Current Workspace</div>
          <h2>{workspace_title}</h2>
          <p class="table-note">在左侧“平台”列直接选择需要处理的平台。每个平台至少需要 1 个账单文件才能确认提交，同一平台最多保留 3 份文件。运营部不能下载任何文件，只能看到自己上传的文件名。</p>
          {platform_workspace_content}
          {
            f'''
            <section class="panel" style="margin-top:22px; padding:22px;">
              <h3 style="margin-bottom:8px;">平台设置</h3>
              <p class="table-note">仅管理员可维护。这里可以修改、删除当前平台名称，或新增平台；保存后会同步作为后续新月份的默认平台清单。已有账单文件的平台不能删除。</p>
              <form method="post" action="/billing/platform-bills/platforms">
                <input type="hidden" name="month_key" value="{html.escape(month_key)}">
                <input type="hidden" name="selected_platform" value="{html.escape(selected_platform_code)}">
                {platform_settings_rows}
                <div class="form-grid" style="margin-top:10px;">
                  <label class="field">
                    <span>新增平台</span>
                    <textarea name="new_platform_label" placeholder="每行填写一个新平台名称，例如：&#10;得物&#10;视频号"></textarea>
                  </label>
                </div>
                <div class="tools" style="margin-top:16px; margin-bottom:0;">
                  <button type="submit">保存平台设置</button>
                </div>
              </form>
            </section>
            '''
            if is_admin(user)
            else ''
          }
          {
            (
              f'<div class="warning" style="margin-top:22px;">当前账号为{html.escape(department_label(user.get("department")))}：可查看本月平台状态{"" if user.get("department") != "B" else "并下载文件"}，不能代替运营部上传提交。</div>'
              if not can_upload_platform_bills(user)
              else f'<div class="meta" style="margin-top:18px;">当前月份已确认提交平台：{submitted_platform_count} / {visible_platform_total}。</div>'
            )
          }
        </section>
        </section>
        """
        return self.page("平台账单 - 商品资料后台", content, user, current_page="billing", back_href="/billing")

    def render_brand_bills_page(self, user, query) -> str:
        today = datetime.now(timezone.utc)
        month_key = query.get("month", "").strip() or today.strftime("%Y-%m")
        notice = query.get("notice", "").strip()
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        summary = None
        brand_bill = None
        try:
            summary = db.brand_bill_month_summary(self.db_path, month_key)
            brand_bill = summary["brand_bill"]
        except ValueError:
            summary = None
            brand_bill = None
        latest_version = summary["latest_version"] if summary else None
        versions = summary["versions"] if summary else []
        dashboard_rows_saved = summary.get("dashboard_rows") if summary else []
        latest_return_request = summary.get("latest_return_request") if summary else None
        dashboard = self.brand_bill_dashboard_summary_from_rows(dashboard_rows_saved, latest_version)
        status = brand_bill.get("status") if brand_bill else "draft"
        status_label = db.brand_bill_status_label(status)
        is_locked = status == "submitted_to_a"
        pending_return = bool(latest_return_request and latest_return_request.get("status") == "pending")
        can_edit_brand_bill = can_process_brand_bills(user)
        can_resolve_return = not is_department_monitor(user) and (
            user.get("department") == "A" or is_admin(user)
        )
        current_version_has_history = bool(
            latest_version
            and any(
                int(item.get("version_no") or 0) == int(latest_version.get("version_no") or -1)
                for item in (summary.get("return_requests") or [])
            )
        )
        can_delete_current_version = bool(
            can_edit_brand_bill and latest_version and not is_locked and not current_version_has_history
        )
        if pending_return:
            flow_label = "退回申请中"
            flow_class = "return"
        elif is_locked:
            flow_label = "已提交给跟单部"
            flow_class = "submitted"
        elif latest_version:
            flow_label = "商品部整理中"
            flow_class = "draft"
        else:
            flow_label = "待商品部整理"
            flow_class = "draft"
        if latest_version:
            current_file_markup = f"""
            <div class="brand-bill-file-name">
              <a href="/billing/brand-bills/files/{int(latest_version['id'])}">{html.escape(str(latest_version.get('original_filename') or '未命名文件'))}</a>
              <a class="brand-bill-download-link" href="/billing/brand-bills/files/{int(latest_version['id'])}">下载账单</a>
            </div>
            <div class="meta">V{int(latest_version.get('version_no') or 0)} · {html.escape(str(latest_version.get('uploader_name') or '商品部'))} · {html.escape(self.format_list_timestamp(latest_version.get('created_at')))}</div>
            """
        else:
            current_file_markup = '<div class="meta">当前月份尚未上传品牌月账单。</div>'
        history_rows_markup = "".join(
            f"""
            <li>
              <span class="brand-bill-history-version">V{int(item.get('version_no') or 0)}</span>
              <a href="/billing/brand-bills/files/{int(item.get('id') or 0)}">{html.escape(str(item.get('original_filename') or '未命名文件'))}</a>
              <span class="meta">{html.escape(str(item.get('uploader_name') or '商品部'))} · {html.escape(self.format_list_timestamp(item.get('created_at')))}</span>
            </li>
            """
            for item in versions
        ) or '<li><span class="meta" style="grid-column:1 / -1; margin:0;">该月份暂无历史账单。</span></li>'
        upload_markup = (
            f"""
            <form class="brand-bill-upload-form" method="post" action="/billing/brand-bills/upload" enctype="multipart/form-data">
              <input type="hidden" name="month_key" value="{html.escape(month_key)}">
              <label class="field">
                <input type="file" name="brand_bill_file" accept=".xlsx" required>
              </label>
              <button type="submit">上传</button>
            </form>
            """
            if can_edit_brand_bill and not is_locked
            else ""
        )
        if can_edit_brand_bill and not is_locked:
            delete_markup = (
                f"""
                <form class="brand-bill-delete-form" method="post" action="/billing/brand-bills/delete" onsubmit="return confirm('确认删除当前账单吗？删除后可重新上传。');">
                  <input type="hidden" name="month_key" value="{html.escape(month_key)}">
                  <button type="submit" class="ghost-button">删除</button>
                </form>
                """
                if can_delete_current_version
                else '<div class="brand-bill-delete-form"><button type="button" class="ghost-button" disabled>删除</button></div>'
            )
        else:
            delete_markup = ""
        submit_markup = (
            f"""
            <form class="brand-bill-submit-form" method="post" action="/billing/brand-bills/submit" onsubmit="return confirm('确认将本月品牌账单提交给跟单部吗？提交后将锁定，需申请退回后才能继续修改。');">
              <input type="hidden" name="month_key" value="{html.escape(month_key)}">
              <button type="submit" {"disabled" if not latest_version else ""}>提交给跟单部</button>
            </form>
            """
            if can_edit_brand_bill and not is_locked
            else ""
        )
        file_actions_markup = (
            f'<div class="brand-bill-file-actions">{upload_markup}{delete_markup}{submit_markup}</div>'
            if upload_markup or delete_markup or submit_markup
            else ""
        )
        if pending_return and latest_return_request:
            return_detail_markup = f"""
            <div class="brand-bill-return-detail">申请人：{html.escape(str(latest_return_request.get('requester_name') or '商品部'))}<br>原因：{html.escape(str(latest_return_request.get('reason') or ''))}</div>
            """
            if can_resolve_return:
                return_action_markup = f"""
                <div class="brand-bill-return-decisions">
                  <form method="post" action="/billing/brand-bills/return-decision" onsubmit="return confirm('确认退回商品部吗？原账单将保留为历史版本。');">
                    <input type="hidden" name="request_id" value="{int(latest_return_request.get('id') or 0)}">
                    <input type="hidden" name="month_key" value="{html.escape(month_key)}">
                    <input type="hidden" name="decision" value="approve">
                    <button type="submit">退回商品部</button>
                  </form>
                  <form method="post" action="/billing/brand-bills/return-decision" onsubmit="return confirm('确认驳回本次退回申请吗？');">
                    <input type="hidden" name="request_id" value="{int(latest_return_request.get('id') or 0)}">
                    <input type="hidden" name="month_key" value="{html.escape(month_key)}">
                    <input type="hidden" name="decision" value="reject">
                    <button type="submit" class="ghost-button">驳回申请</button>
                  </form>
                </div>
                """
            else:
                return_action_markup = ""
        elif is_locked and can_edit_brand_bill:
            return_detail_markup = ""
            return_action_markup = f"""
            <form class="brand-bill-return-form" method="post" action="/billing/brand-bills/return-request" onsubmit="return confirm('确认提交退回申请吗？需等待跟单部或管理员处理。');">
              <input type="hidden" name="month_key" value="{html.escape(month_key)}">
              <textarea name="reason" maxlength="200" required placeholder="填写申请退回原因"></textarea>
              <button type="submit" class="ghost-button">申请退回</button>
            </form>
            """
        else:
            return_detail_markup = ""
            return_action_markup = ""
        dashboard_rows = []
        table_rows = dashboard.get("table_rows") or []
        for row in table_rows:
            dashboard_rows.append(
                f"""
                <tr>
                  <td class="brand-col-month" data-dashboard-column-key="month_label">{html.escape(str(row.get("month_label") or ""))}</td>
                  <td class="brand-col-channel" data-dashboard-column-key="platform_name">{html.escape(str(row.get("platform_name") or ""))}</td>
                  <td class="brand-col-shop" data-dashboard-column-key="shop_name">{html.escape(str(row.get("shop_name") or ""))}</td>
                  <td class="brand-col-number" data-dashboard-column-key="total_qty">{self.format_table_metric(row.get("total_qty"))}</td>
                  <td class="brand-col-number brand-group-divider brand-gz-divider" data-dashboard-column-key="total_amount">{self.format_table_metric(row.get("total_amount"), money=True)}</td>
                  <td class="brand-col-number" data-dashboard-column-key="gz_qty">{self.format_table_metric(row.get("gz_qty"))}</td>
                  <td class="brand-col-number" data-dashboard-column-key="gz_amount">{self.format_table_metric(row.get("gz_amount"), money=True)}</td>
                  <td class="brand-col-number brand-col-ratio" data-dashboard-column-key="gz_qty_ratio">{self.format_table_ratio(row.get("gz_qty"), row.get("total_qty"))}</td>
                  <td class="brand-col-number brand-col-ratio brand-group-divider brand-gz-divider" data-dashboard-column-key="gz_amount_ratio">{self.format_table_ratio(row.get("gz_amount"), row.get("total_amount"))}</td>
                  <td class="brand-col-number" data-dashboard-column-key="wh_qty">{self.format_table_metric(row.get("wh_qty"))}</td>
                  <td class="brand-col-number" data-dashboard-column-key="wh_amount">{self.format_table_metric(row.get("wh_amount"), money=True)}</td>
                  <td class="brand-col-number brand-col-ratio" data-dashboard-column-key="wh_qty_ratio">{self.format_table_ratio(row.get("wh_qty"), row.get("total_qty"))}</td>
                  <td class="brand-col-number brand-col-ratio" data-dashboard-column-key="wh_amount_ratio">{self.format_table_ratio(row.get("wh_amount"), row.get("total_amount"))}</td>
                </tr>
                """
            )
        dashboard_export_url = "/billing/brand-bills/dashboard.xlsx?" + urlencode({"month": month_key})
        dashboard_query_markup = f"""
        <details id="brand-dashboard-query" class="brand-dashboard-query query-anchor">
          <summary class="brand-dashboard-control">查询</summary>
          <div class="brand-dashboard-query-panel">
            <form method="get" action="/billing/brand-bills#brand-dashboard-query">
              <label>
                <span>月份</span>
                <input type="month" name="month" value="{html.escape(month_key)}">
              </label>
              <button type="submit">查询</button>
            </form>
          </div>
        </details>
        """
        dashboard_import_markup = (
            f"""
            <form class="brand-dashboard-import-form" method="post" action="/billing/brand-bills/dashboard" enctype="multipart/form-data">
              <input type="hidden" name="month_key" value="{html.escape(month_key)}">
              <input class="brand-dashboard-import-input" id="brand-dashboard-file" type="file" name="dashboard_file" accept=".xlsx" onchange="this.form.requestSubmit()" {"disabled" if is_locked else ""}>
              <label class="brand-dashboard-control brand-dashboard-import-control" for="brand-dashboard-file">导入excel</label>
            </form>
            """
            if can_process_brand_bills(user)
            else ""
        )
        content = f"""
        <div class="brand-bills-page">
          <div class="page-back-row brand-bills-page-back">
            <a class="page-back-link" href="/billing">&larr; 返回上一层</a>
          </div>
          <section class="panel brand-bills-primary-panel">
            <div class="brand-bill-card-head">
              <h1>品牌月账单</h1>
            </div>
            <div class="brand-bill-management-grid">
              <section class="brand-bill-current-section">
                <section class="brand-bill-status-section">
                  <div class="brand-bill-status-heading">
                    <h2>当前流程状态</h2>
                    <span class="brand-bill-flow-chip brand-bill-flow-{flow_class}">{html.escape(flow_label)}</span>
                  </div>
                  {return_detail_markup}
                  {return_action_markup}
                  {notice_block}
                </section>
                <section class="brand-bill-current-file-section">
                  <h2>{html.escape(month_key)}当月账单</h2>
                  <div class="brand-bill-file-summary">
                    <span class="brand-bill-file-label">当前账单</span>
                    {current_file_markup}
                  </div>
                  {file_actions_markup}
                </section>
              </section>
              <aside class="brand-bill-side">
                <section id="brand-bill-history-query" class="brand-bill-history-section query-anchor">
                  <h2>历史账单查询</h2>
                  <form class="brand-bill-history-query" method="get" action="/billing/brand-bills#brand-bill-history-query">
                    <input type="month" name="month" value="{html.escape(month_key)}" aria-label="查询月份">
                    <button type="submit" class="ghost-button">查询</button>
                  </form>
                  <ul class="brand-bill-history-list">{history_rows_markup}</ul>
                </section>
              </aside>
            </div>
          </section>
          <section class="panel" style="margin-top:18px;">
            <div class="brand-dashboard-panel">
              <div class="brand-dashboard-toolbar">
                <h2>马天奴月销看板</h2>
                <div class="brand-dashboard-controls">
                  <div class="brand-dashboard-primary-controls">
                    {dashboard_import_markup}
                    <a class="brand-dashboard-control" href="{html.escape(dashboard_export_url, quote=True)}">导出excel</a>
                  </div>
                  <div class="brand-dashboard-secondary-controls">{dashboard_query_markup}</div>
                </div>
              </div>
              <div class="table-wrap">
                <table class="catalog-table brand-dashboard-table">
                  <colgroup>
                    <col data-dashboard-column-key="month_label">
                    <col data-dashboard-column-key="platform_name">
                    <col data-dashboard-column-key="shop_name">
                    <col data-dashboard-column-key="total_qty">
                    <col data-dashboard-column-key="total_amount">
                    <col data-dashboard-column-key="gz_qty">
                    <col data-dashboard-column-key="gz_amount">
                    <col data-dashboard-column-key="gz_qty_ratio">
                    <col data-dashboard-column-key="gz_amount_ratio">
                    <col data-dashboard-column-key="wh_qty">
                    <col data-dashboard-column-key="wh_amount">
                    <col data-dashboard-column-key="wh_qty_ratio">
                    <col data-dashboard-column-key="wh_amount_ratio">
                  </colgroup>
                  <thead>
                    <tr>
                      <th colspan="3"></th>
                      <th colspan="2" class="brand-group-divider brand-gz-divider">合计</th>
                      <th colspan="4" class="brand-group-divider brand-gz-divider">广州仓</th>
                      <th colspan="4">武汉仓</th>
                    </tr>
                    <tr>
                      <th data-dashboard-column-key="month_label">年月</th>
                      <th data-dashboard-column-key="platform_name">平台</th>
                      <th data-dashboard-column-key="shop_name">店铺</th>
                      <th data-dashboard-column-key="total_qty">销售数量</th>
                      <th class="brand-group-divider brand-gz-divider" data-dashboard-column-key="total_amount">销售金额</th>
                      <th data-dashboard-column-key="gz_qty">销售数量</th>
                      <th data-dashboard-column-key="gz_amount">销售金额</th>
                      <th data-dashboard-column-key="gz_qty_ratio">销量占比</th>
                      <th class="brand-group-divider brand-gz-divider" data-dashboard-column-key="gz_amount_ratio">销额占比</th>
                      <th data-dashboard-column-key="wh_qty">销售数量</th>
                      <th data-dashboard-column-key="wh_amount">销售金额</th>
                      <th data-dashboard-column-key="wh_qty_ratio">销量占比</th>
                      <th data-dashboard-column-key="wh_amount_ratio">销额占比</th>
                    </tr>
                  </thead>
                  <tbody>
                    {''.join(dashboard_rows) if dashboard_rows else '<tr><td colspan="13"><div class="empty-state">上传看板 Excel 后，这里会自动生成月销表格。</div></td></tr>'}
                  </tbody>
                </table>
                <script>
                  (() => {{
                    const table = document.querySelector(".brand-dashboard-table");
                    const scrollWrap = table && table.closest(".table-wrap");
                    const headers = table
                      ? Array.from(table.querySelectorAll("thead tr:nth-child(2) th[data-dashboard-column-key]"))
                      : [];
                    if (!table || !scrollWrap || !headers.length) return;
                    const storageKey = "brand_dashboard_column_widths_v1";
                    let widthMap = {{}};
                    try {{
                      widthMap = JSON.parse(window.localStorage.getItem(storageKey) || "{{}}");
                    }} catch (error) {{
                      widthMap = {{}};
                    }}
                    const columnForKey = (key) => table.querySelector(`col[data-dashboard-column-key="${{key}}"]`);
                    const headerForKey = (key) => table.querySelector(`thead tr:nth-child(2) th[data-dashboard-column-key="${{key}}"]`);
                    const applyWidth = (key, width) => {{
                      const column = columnForKey(key);
                      const header = headerForKey(key);
                      if (!column || !header) return;
                      column.style.width = `${{width}}px`;
                      header.style.width = `${{width}}px`;
                    }};
                    const updateTableWidth = () => {{
                      const totalWidth = headers.reduce((total, header) => total + header.getBoundingClientRect().width, 0);
                      const width = Math.max(scrollWrap.clientWidth, Math.ceil(totalWidth));
                      table.style.width = `${{width}}px`;
                      table.style.minWidth = `${{width}}px`;
                    }};
                    Object.entries(widthMap).forEach(([key, value]) => {{
                      const width = Number(value);
                      if (Number.isFinite(width) && width >= 72) applyWidth(key, width);
                    }});
                    if (Object.keys(widthMap).length) updateTableWidth();
                    let activeResize = null;
                    const onPointerMove = (event) => {{
                      if (!activeResize) return;
                      const nextWidth = Math.max(72, Math.round(activeResize.startWidth + event.clientX - activeResize.startX));
                      widthMap[activeResize.key] = nextWidth;
                      applyWidth(activeResize.key, nextWidth);
                      updateTableWidth();
                    }};
                    const onPointerUp = () => {{
                      if (!activeResize) return;
                      document.body.classList.remove("brand-dashboard-resizing");
                      window.localStorage.setItem(storageKey, JSON.stringify(widthMap));
                      window.removeEventListener("pointermove", onPointerMove);
                      window.removeEventListener("pointerup", onPointerUp);
                      activeResize = null;
                    }};
                    headers.forEach((header) => {{
                      const key = header.getAttribute("data-dashboard-column-key") || "";
                      if (!key) return;
                      header.classList.add("brand-dashboard-resizable-head");
                      const handle = document.createElement("button");
                      handle.type = "button";
                      handle.className = "brand-dashboard-resize-handle";
                      handle.title = "拖动调整列宽";
                      handle.setAttribute("aria-label", `调整${{header.textContent.trim()}}列宽`);
                      handle.addEventListener("pointerdown", (event) => {{
                        event.preventDefault();
                        activeResize = {{
                          key,
                          startX: event.clientX,
                          startWidth: header.getBoundingClientRect().width,
                        }};
                        document.body.classList.add("brand-dashboard-resizing");
                        window.addEventListener("pointermove", onPointerMove);
                        window.addEventListener("pointerup", onPointerUp);
                      }});
                      header.appendChild(handle);
                    }});
                  }})();
                </script>
              </div>
            </div>
          </section>
        </div>
        """
        return self.page("品牌月账单 - 商品资料后台", content, user, current_page="billing")

    def render_supplier_settlements_page(self, user, query) -> str:
        notice = str(query.get("notice", "")).strip()
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        master_code = str(query.get("master_code", "")).strip()
        master_name = str(query.get("master_name", "")).strip()
        master_has_query = bool(master_code or master_name)
        can_manage_supplier = can_manage_supplier_settlements(user)
        master_action_header = "<th>操作</th>" if can_manage_supplier else ""
        master_table_colgroup = (
            "<colgroup><col style=\"width:20%\"><col style=\"width:40%\"><col style=\"width:20%\"><col style=\"width:20%\"></colgroup>"
            if can_manage_supplier
            else "<colgroup><col style=\"width:25%\"><col style=\"width:50%\"><col style=\"width:25%\"></colgroup>"
        )
        master_rows = db.list_supplier_master_names(self.db_path, master_code, master_name) if master_has_query else []
        master_table_rows = "".join(
            f"""
            <tr>
              <td>{html.escape(str(item['supplier_code']))}</td>
              <td>{html.escape(str(item['supplier_name']))}</td>
              <td>{html.escape(str(item['supply_chain_manager']))}</td>
              {f'<td><a href="/billing/supplier-settlements/master/edit?id={int(item["id"])}">编辑</a></td>' if can_manage_supplier else ''}
            </tr>
            """
            for item in master_rows
        )
        master_empty_message = "请输入供应商名称或供应商编号查询。" if not master_has_query else "暂无符合条件的供应商信息。"
        master_table_body = master_table_rows or f'<tr><td colspan="{4 if can_manage_supplier else 3}"><div class="empty-state">{master_empty_message}</div></td></tr>'
        start_month, end_month, supplier_code, supplier_name_ids = self.supplier_bill_query_filters(query)
        try:
            bill_result = db.query_supplier_bill_lines(
                self.db_path,
                start_month,
                end_month,
                supplier_code,
                supplier_name_ids,
            )
        except ValueError as error:
            start_month = end_month = datetime.now(timezone.utc).strftime("%Y-%m")
            supplier_code = ""
            supplier_name_ids = []
            bill_result = db.query_supplier_bill_lines(self.db_path, start_month, end_month)
            notice_block = f'<div class="warning">{html.escape(str(error))}</div>'
        code_rows = db.list_supplier_code_masters(self.db_path)
        code_suggestion_parts = []
        for item in code_rows:
            code = str(item["supplier_code"])
            code_suggestion_parts.append(
                f'<option value="{html.escape(code, quote=True)}" '
                f'label="{html.escape(str(item["supply_chain_manager"]), quote=True)}"></option>'
            )
        code_suggestions = "".join(code_suggestion_parts)
        selected_name_ids = {int(item) for item in supplier_name_ids}
        supplier_name_option_parts = []
        for item in db.list_supplier_master_names(self.db_path):
            item_code = str(item["supplier_code"])
            hidden_attr = " hidden" if item_code != supplier_code else ""
            checked_attr = " checked" if int(item["id"]) in selected_name_ids else ""
            supplier_name_option_parts.append(
                f'<label class="supplier-name-option" '
                f'data-supplier-code="{html.escape(item_code, quote=True)}"{hidden_attr}>'
                f'<input type="checkbox" value="{int(item["id"])}"{checked_attr}> '
                f"{html.escape(str(item['supplier_name']))}</label>"
            )
        supplier_name_options = "".join(supplier_name_option_parts)
        export_query = urlencode(
            {
                "start_month": start_month,
                "end_month": end_month,
                "supplier_code": supplier_code,
                "supplier_name_ids": ",".join(str(item) for item in supplier_name_ids),
            }
        )
        supplier_bill_delete_markup = (
            '<button class="ghost-button supplier-bill-delete-button" type="submit" formaction="/billing/supplier-settlements/bills/delete" formmethod="post" onclick="return confirm(\'确认删除该月份当前生效的供应商账单吗？删除后可在期限内重新上传。\');">删除账单</button>'
            if user.get("department") == "A"
            else ""
        )
        supplier_bill_delete_note = (
            '<p class="meta supplier-bill-delete-note">账单首次导入后 30 天内可删除并重新上传；超过期限后将锁定，保留当前资料。</p>'
            if user.get("department") == "A"
            else ""
        )
        supplier_bill_action_pair_class = (
            "supplier-bill-import-action-pair"
            if supplier_bill_delete_markup
            else "supplier-bill-import-action-pair supplier-bill-import-action-pair-single"
        )
        bill_import_markup = (
            f"""
            <form method="post" action="/billing/supplier-settlements/bills/import" enctype="multipart/form-data">
              <div class="form-grid">
                <label class="field"><span>账单所属月份</span><input type="month" name="period_month" value="{html.escape(end_month, quote=True)}"></label>
                <label class="field"><span>账单 Excel</span><input type="file" name="bill_workbook" accept=".xlsx"></label>
              </div>
              <div class="supplier-bill-import-actions">
                <div class="{supplier_bill_action_pair_class}">
                  {supplier_bill_delete_markup}
                  <button class="supplier-bill-import-button" type="submit">导入账单</button>
                </div>
              </div>
              {supplier_bill_delete_note}
            </form>
            """
            if can_manage_supplier
            else '<p class="meta">总经办账号仅可查询与导出账单明细，不能导入或修改账单。</p>'
        )
        supplier_master_create_markup = (
            """
            <div class="supplier-master-section-head">
              <h3>新建供应商</h3>
              <a class="pill" href="/billing/supplier-settlements/master/template.xlsx">下载导入模板</a>
            </div>
            <form class="supplier-master-import-form" method="post" action="/billing/supplier-settlements/master/import" enctype="multipart/form-data">
              <div class="supplier-master-create-row">
                <a class="supplier-master-single-link" href="/billing/supplier-settlements/master/new">单个供应商录入</a>
                <label class="field supplier-master-file-field"><input type="file" name="supplier_master_workbook" accept=".xlsx" aria-label="供应商主档 Excel"></label>
              </div>
              <div class="supplier-master-import-actions">
                <button class="supplier-master-import-button" type="submit">导入 Excel</button>
              </div>
            </form>
            """
            if can_manage_supplier
            else """
            <h3>供应商信息</h3>
            <div class="supplier-master-import-actions">
              <a class="pill" href="/billing/supplier-settlements/master/template.xlsx">下载导入模板</a>
            </div>
            <p class="meta">总经办账号仅可查询供应商信息，不能新建、导入或修改供应商资料。</p>
            """
        )
        content = f"""
        <div class="supplier-settlement-layout">
          <div class="supplier-settlement-columns">
            <section class="panel supplier-settlement-main-panel">
              <div class="supplier-card-head"><h1>供应商结算</h1></div>
              {notice_block}
              <div class="supplier-import-block">
                <div class="supplier-card-head">
                  <h2>账单导入</h2>
                  <a class="pill" href="/billing/supplier-settlements/bills/template.xlsx">下载导入模板</a>
                </div>
                {bill_import_markup}
              </div>
            </section>
            <section class="panel supplier-master-panel">
              <div class="supplier-card-head"><h2>供应商管理</h2></div>
              <div class="supplier-master-section">
                {supplier_master_create_markup}
              </div>
              <div id="supplier-master-query" class="supplier-master-section supplier-master-query-section query-anchor">
                <h3>查询供应商</h3>
                <div class="supplier-master-query-grid">
                  <form class="supplier-master-query-form" method="get" action="/billing/supplier-settlements#supplier-master-query">
                    <input type="hidden" name="start_month" value="{html.escape(start_month, quote=True)}">
                    <input type="hidden" name="end_month" value="{html.escape(end_month, quote=True)}">
                    <input type="hidden" name="supplier_code" value="{html.escape(supplier_code, quote=True)}">
                    <input type="hidden" name="supplier_name_ids" value="{html.escape(','.join(str(item) for item in supplier_name_ids), quote=True)}">
                    <h4>按供应商名称</h4>
                    <label class="field"><input name="master_name" value="{html.escape(master_name, quote=True)}" placeholder="输入供应商名称" aria-label="供应商名称"></label>
                    <button type="submit">查询</button>
                  </form>
                  <form class="supplier-master-query-form" method="get" action="/billing/supplier-settlements#supplier-master-query">
                    <input type="hidden" name="start_month" value="{html.escape(start_month, quote=True)}">
                    <input type="hidden" name="end_month" value="{html.escape(end_month, quote=True)}">
                    <input type="hidden" name="supplier_code" value="{html.escape(supplier_code, quote=True)}">
                    <input type="hidden" name="supplier_name_ids" value="{html.escape(','.join(str(item) for item in supplier_name_ids), quote=True)}">
                    <h4>按供应商编号</h4>
                    <label class="field"><input name="master_code" value="{html.escape(master_code, quote=True)}" placeholder="输入供应商编号" aria-label="供应商编号"></label>
                    <button type="submit">查询</button>
                  </form>
                </div>
                <div class="table-wrap supplier-master-results">
                  <table class="catalog-table supplier-master-table">
                    {master_table_colgroup}
                    <thead><tr><th>供应商编号</th><th>供应商名称</th><th>供应链经理</th>{master_action_header}</tr></thead>
                    <tbody>{master_table_body}</tbody>
                  </table>
                </div>
              </div>
            </section>
          </div>
          <section id="supplier-bill-query" class="panel supplier-bill-query-panel query-anchor">
            <div class="supplier-card-head"><h2>账单查询</h2></div>
            <form id="supplier-bill-query-form" method="get" action="/billing/supplier-settlements#supplier-bill-query">
              <input id="supplier-name-ids" type="hidden" name="supplier_name_ids" value="{html.escape(','.join(str(item) for item in supplier_name_ids), quote=True)}">
              <div class="form-grid">
                <label class="field"><span>开始年月</span><input type="month" name="start_month" value="{html.escape(start_month, quote=True)}"></label>
                <label class="field"><span>结束年月</span><input type="month" name="end_month" value="{html.escape(end_month, quote=True)}"></label>
                <label class="field"><span>供应商编号</span><input id="supplier-code-filter" name="supplier_code" value="{html.escape(supplier_code, quote=True)}" list="supplier-code-suggestions" placeholder="输入供应商编号"></label>
                <label class="field"><span>供应商名称</span><div class="supplier-name-options"><span id="supplier-name-empty" class="supplier-name-empty">请选择供应商编号后多选名称</span>{supplier_name_options}</div></label>
              </div>
              <datalist id="supplier-code-suggestions">{code_suggestions}</datalist>
              <div class="supplier-bill-query-actions"><button type="submit">查询</button></div>
            </form>
            <div class="supplier-summary-block">
              <div class="supplier-summary-head">
                <h3>查询汇总</h3>
                <a class="pill supplier-detail-export" href="/billing/supplier-settlements/bills/export.xlsx?{html.escape(export_query, quote=True)}">导出明细 Excel</a>
              </div>
              <div class="supplier-query-summary">
                <div class="stat-card"><span>结算金额</span><strong>{bill_result['settlement_amount_total']:,.2f}</strong></div>
                <div class="stat-card"><span>数量</span><strong>{bill_result['quantity_total']:,}</strong></div>
              </div>
            </div>
          </section>
        </div>
        <script>
          (() => {{
            const form = document.getElementById("supplier-bill-query-form");
            const codeSelect = document.getElementById("supplier-code-filter");
            const hiddenInput = document.getElementById("supplier-name-ids");
            const emptyHint = document.getElementById("supplier-name-empty");
            if (!form || !codeSelect || !hiddenInput || !emptyHint) return;
            const options = Array.from(form.querySelectorAll(".supplier-name-option"));
            const syncNameOptions = () => {{
              const code = codeSelect.value;
              let visibleCount = 0;
              options.forEach((option) => {{
                const matches = Boolean(code) && option.dataset.supplierCode === code;
                option.hidden = !matches;
                if (!matches) {{
                  const input = option.querySelector("input");
                  if (input) input.checked = false;
                }} else {{
                  visibleCount += 1;
                }}
              }});
              emptyHint.hidden = visibleCount > 0;
              emptyHint.textContent = code ? "该编号暂无可选供应商名称" : "请选择供应商编号后多选名称";
              hiddenInput.value = options
                .filter((option) => !option.hidden)
                .map((option) => option.querySelector("input"))
                .filter((input) => input && input.checked)
                .map((input) => input.value)
                .join(",");
            }};
            codeSelect.addEventListener("input", syncNameOptions);
            codeSelect.addEventListener("change", syncNameOptions);
            form.addEventListener("change", syncNameOptions);
            form.addEventListener("submit", syncNameOptions);
            syncNameOptions();
          }})();
        </script>
        """
        return self.page("供应商结算 - 商品资料后台", content, user, current_page="billing", back_href="/billing")

    def render_bulk_tools(self, user) -> str:
        if is_department_monitor(user):
            return ""
        if user.get("department") == "A":
            return """
              <div class="list-intro-actions">
                <div class="tools">
                  <button type="submit" name="bulk_action" value="submit_to_b_selected" form="products-bulk-form" formmethod="post" formaction="/products/bulk">批量提交给商品部</button>
                </div>
                <div class="meta">先勾选资料，再一键流转给商品部继续补充。</div>
              </div>
            """
        if user.get("department") == "B":
            return """
              <div class="list-intro-actions">
                <div class="tools bulk-tools-vertical">
                  <button type="submit" name="bulk_action" value="complete_to_c_selected" form="products-bulk-form" formmethod="post" formaction="/products/bulk">批量完成给运营部</button>
                  <button type="submit" name="bulk_action" value="return_to_a_selected" form="products-bulk-form" formmethod="post" formaction="/products/bulk" class="ghost-button">批量退回给跟单部</button>
                </div>
                <div class="meta">商品部可在这里直接完成批量流转或退回跟单部。</div>
              </div>
            """
        if user.get("department") == "C":
            return """
              <div class="list-intro-actions">
                <div class="tools">
                  <button type="submit" name="bulk_action" value="receive_selected" form="products-bulk-form" formmethod="post" formaction="/products/bulk">批量接收资料</button>
                </div>
                <div class="meta">勾选状态为已完成的资料后，可一次确认接收。</div>
              </div>
            """
        if not is_admin(user):
            return ""
        return """
          <div class="list-intro-actions">
            <div class="tools">
              <button type="submit" name="bulk_action" value="publish_selected" form="products-bulk-form" formmethod="post" formaction="/products/bulk">批量完成并开放给C</button>
              <button type="submit" name="bulk_action" value="archive_selected" form="products-bulk-form" formmethod="post" formaction="/products/bulk">批量归档</button>
            </div>
            <div class="meta">管理员可在列表右上角直接执行批量开放或归档。</div>
          </div>
        """

    def render_product_form(self, user, action: str, title: str, values: dict, errors: list[str] | None = None) -> str:
        console_eyebrow = self.brand_config["brand_console_eyebrow"]
        editable_keys = self.editable_field_keys_for_request(user, values if values.get("id") else None)
        error_block = ""
        if errors:
            items = "".join(f"<li>{html.escape(error)}</li>" for error in errors)
            error_block = f'<ul class="error-list">{items}</ul>'
        sections = []
        for group, fields in FIELDS_BY_GROUP.items():
            visible_fields = [field for field in fields if field.key in editable_keys and field.key != "size_chart"]
            if not visible_fields:
                continue
            inputs = "".join(self.render_input(field, values) for field in visible_fields)
            sections.append(f'<section class="panel"><h2>{html.escape(group)}</h2><div class="form-grid">{inputs}</div></section>')
        department = user.get("department")
        if department == "A":
            intro_text = "当前由跟单部维护除品类、图片、上新价格、上新渠道、资料完成以外的主体资料字段。完成后请把资料提交给商品部补充这 5 项内容。"
            mode_title = "跟单部主体资料填写"
            next_action = "提交给商品部填写"
        elif department == "B":
            intro_text = "当前由商品部补充品类、图片、上新价格、上新渠道和资料完成。完成后即可开放给运营部读取。"
            mode_title = "商品部资料补充"
            next_action = "填写完成后开放给运营部"
        else:
            intro_text = "当前页面用于维护商品资料底库。"
            mode_title = "资料维护"
            next_action = "保存资料"
        content = f"""
        <section class="hero">
          <div class="panel">
            <div class="eyebrow">{html.escape(console_eyebrow)}</div>
            <h1>{html.escape(title)}</h1>
            <p>{html.escape(intro_text)}</p>
            <div class="spotlight">
              <div>
                <strong>当前编辑模式</strong>
                <div class="table-note">{html.escape(mode_title)}</div>
              </div>
              <div class="spotlight-value">{html.escape(department_label(user['department']))}</div>
            </div>
          </div>
          <div class="panel">
            <div class="eyebrow">Editor Workspace</div>
            <h2>填写提醒</h2>
            <div class="stats">
              <div class="stat-card"><span>资料归属</span><strong>{html.escape(department_label(user['department']))}</strong></div>
              <div class="stat-card"><span>当前阶段</span><strong>{html.escape(status_label(values.get('status') or 'draft'))}</strong></div>
              <div class="stat-card"><span>后续动作</span><strong>{html.escape(next_action)}</strong></div>
            </div>
          </div>
        </section>
        <section class="panel" style="margin-bottom:18px;">
          <div class="eyebrow">Field Editing</div>
          <h2>资料填写区</h2>
          <p class="table-note">{html.escape(intro_text)}</p>
          {error_block}
          <form method="post" action="{html.escape(action)}" enctype="multipart/form-data">
            {''.join(sections)}
            <section class="panel" style="margin-top:18px;">
              <div class="tools" style="margin-bottom:0;">
                <a class="pill" href="/products">返回资料列表</a>
                <button type="submit">保存资料</button>
              </div>
            </section>
          </form>
        </section>
        """
        return self.page(
            f"{title} - 商品资料后台",
            content,
            user,
            current_page="products",
            back_href=f"/products/{values['id']}" if values.get("id") else "/products",
        )

    def render_input(self, field, values):
        raw_value = values.get(field.key, "")
        value = "" if raw_value is None else str(raw_value)
        field_class = "field field-wide" if field.input_type == "textarea" else "field"
        label = html.escape(field.label)
        placeholder = html.escape(field.placeholder or "")
        if field.key == "image_url":
            gallery_values = self.image_gallery_values(values)
            gallery_rows = []
            for index, image_value in enumerate(gallery_values):
                gallery_rows.append(
                    f"""
                    <div style="display:grid; grid-template-columns: 90px 1fr; gap:14px; align-items:start; padding:12px; border:1px solid rgba(91,58,29,0.12); border-radius:14px; margin-top:12px;">
                      <img src="{html.escape(image_value)}" alt="图库预览 {index + 1}" style="width:90px; height:90px; object-fit:cover; border-radius:12px; border:1px solid rgba(91,58,29,0.12); background:#f6f1ea;">
                      <div>
                        <div class="meta">第 {index + 1} 张，按这里的顺序作为展示和调用顺序，第一张会自动作为主图。</div>
                        <input type="hidden" name="image_gallery_existing__{index}" value="{html.escape(image_value)}">
                        <input type="text" name="image_gallery_manual__{index}" value="{html.escape(image_value)}" placeholder="可替换为图片链接，留空则移除这一张" style="margin-top:8px;">
                      </div>
                    </div>
                    """
                )
            if not gallery_rows:
                gallery_rows.append('<div class="meta" style="margin-top:12px;">当前还没有图片。保存后第一张会同步写入“图片”主字段。</div>')
            return f"""
            <label class="{field_class}">
              <span>{label}</span>
              <input type="text" name="{field.key}" value="{html.escape(value)}" placeholder="{placeholder}">
              <span class="meta">可直接填写主图链接，也可以在下方上传多张本地图片或维护图库顺序。保存后第一张会自动作为主图。</span>
              <input type="file" name="image_upload" accept="image/png,image/jpeg,image/webp,image/gif">
              <span class="meta">多图上传</span>
              <input type="file" name="image_uploads" accept="image/png,image/jpeg,image/webp,image/gif" multiple>
              <div style="margin-top:8px;">
                {''.join(gallery_rows)}
              </div>
              <div style="margin-top:12px;">
                <span class="meta">新增外链图片</span>
                <input type="text" name="image_gallery_manual__new" value="" placeholder="可额外补一张图片链接，保存后会接在现有图库后面">
              </div>
            </label>
            """
        if field.input_type == "textarea":
            return f"""
            <label class="{field_class}">
              <span>{label}</span>
              <textarea name="{field.key}" placeholder="{placeholder}">{html.escape(value)}</textarea>
            </label>
            """
        if field.input_type == "select":
            options = ['<option value="">请选择</option>']
            for option in field.options:
                selected = "selected" if value == option else ""
                options.append(f'<option value="{html.escape(option)}" {selected}>{html.escape(option)}</option>')
            return f"""
            <label class="{field_class}">
              <span>{label}</span>
              <select name="{field.key}">
                {''.join(options)}
              </select>
            </label>
            """
        input_type = "number" if field.input_type == "number" else "text"
        step = ' step="0.01"' if field.storage_type == "REAL" else ""
        return f"""
        <label class="{field_class}">
          <span>{label}</span>
          <input type="{input_type}" name="{field.key}" value="{html.escape(value)}" placeholder="{placeholder}"{step}>
        </label>
        """

    def render_product_detail(self, user, product: dict, notice: str = "") -> str:
        console_eyebrow = self.brand_config["brand_console_eyebrow"]
        visible_fields = self.visible_fields_for_c_product(product) if user["department"] == "C" else self.visible_fields_for_user(user)
        payload = self.product_payload_for_user(product, user)
        copy_summary = self.product_copy_summary(product, user)
        payload_json = json.dumps(payload, ensure_ascii=False, indent=2)
        group_html = []
        for group, group_fields in FIELDS_BY_GROUP.items():
            rows = []
            for field in group_fields:
                if field not in visible_fields:
                    continue
                field_value_markup = self.display_value(product.get(field.key))
                if field.key == "image_url":
                    field_value_markup = self.display_image_gallery(product)
                rows.append(
                    f"""
                    <div class="detail-row">
                      <span class="detail-label">{html.escape(field.label)}</span>
                      <div>{field_value_markup}</div>
                    </div>
                    """
                )
            if rows:
                group_html.append(
                    f'<section class="detail-card"><h3>{html.escape(group)}</h3>{"".join(rows)}</section>'
                )
        edit_link = (
            f'<a class="pill" href="/products/{product["id"]}/edit">编辑这条资料</a>'
            if can_edit_product(user, product)
            else ""
        )
        log_link = (
            f'<a class="pill" href="/products/{product["id"]}/logs">查看日志</a>'
            if can_view_logs(user)
            else ""
        )
        version_link = (
            f'<a class="pill" href="/products/{product["id"]}/versions">版本记录</a>'
            if is_admin(user)
            else ""
        )
        status_cards = []
        for status_value, action_label in available_status_actions(user, product):
            if user.get("department") == "C" and status_value == "received" and product.get("c_received"):
                continue
            status_cards.append(
                f"""
                <form method="post" action="/products/{product['id']}/status" class="detail-card">
                  <input type="hidden" name="status" value="{html.escape(status_value)}">
                  <h3 style="margin-bottom:8px;">{html.escape(action_label)}</h3>
                  <p class="meta">可选填写处理说明，提交后会同步写入这条资料的操作日志。</p>
                  <label class="field field-wide" style="margin-top:14px;">
                    <span>{html.escape(self.status_note_label(user, product, status_value))}</span>
                    <textarea name="review_note" placeholder="{html.escape(self.status_note_placeholder(user, product, status_value))}"></textarea>
                  </label>
                  <div style="margin-top:14px;">
                    <button type="submit">{html.escape(action_label)}</button>
                  </div>
                </form>
                """
            )
        status_block = ""
        if status_cards:
            status_block = f"""
            <section class="panel" style="margin-top:18px;">
              <h2>阶段流转</h2>
              <p class="meta">可记录 A 到 B 的交接说明、B 的完成说明、运营部接收确认，以及管理员的人工处理备注。</p>
              <div class="detail-grid">
                {''.join(status_cards)}
              </div>
            </section>
            """
        lifecycle_forms = "".join(
            f"""
            <form method="post" action="/products/{product['id']}/lifecycle" style="display:inline-flex; gap:10px;">
              <input type="hidden" name="lifecycle_status" value="{html.escape(status_value)}">
              {'<input type="hidden" name="return_to" value="/products">' if status_value == 'deleted' else ''}
              {'<input name="confirm_text" placeholder="输入 DELETE">' if status_value == 'deleted' else ''}
              <button class="ghost-button" type="submit">{html.escape(action_label)}</button>
            </form>
            """
            for status_value, action_label in available_lifecycle_actions(user, product)
        )
        lifecycle_block = ""
        if lifecycle_forms:
            lifecycle_block = f"""
            <section class="panel" style="margin-top:18px;">
              <h2>资料生命周期操作</h2>
              <div class="tools" style="margin-bottom:0;">
                {lifecycle_forms}
              </div>
            </section>
            """
        hidden_note = (
            '<div class="warning">你当前看到的是运营部可访问字段，隐藏字段不会展示在详情页、导出和 API 中。</div>'
            if user["department"] == "C"
            else ""
        )
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        workflow_notice = (
            "状态流转：跟单部填写中 -> 待商品部填写 -> 已完成 -> 已接收"
            if user["department"] != "C"
            else "状态为已完成表示资料等待你接收；确认后会更新为已接收。"
        )
        owner_label = "跟单部发起资料" if product.get("owner_department") == "A" else department_label(product.get("owner_department"))
        revision_badge = self.revision_badge(product)
        completion_flag = payload.get("completion_flag") or ""
        completion_flag_visible = any(field.key == "completion_flag" for field in visible_fields)
        creator_name = html.escape(str(product.get("creator_name") or "未记录"))
        detail_summary_items = [
            f'<span class="detail-summary-chip">资料编号 <strong>#{product["id"]}</strong></span>',
            f'<span class="detail-summary-chip">当前状态 <strong>{html.escape(status_label(product.get("status")))}</strong></span>',
            f'<span class="detail-summary-chip">修改版本 <strong>{html.escape(self.version_label(product))}</strong></span>',
            f'<span class="detail-summary-chip">历时天数 <strong>{html.escape(self.elapsed_days_label(product.get("created_at"), product.get("completed_to_c_at")))}</strong></span>',
            f'<span class="detail-summary-chip">发起归属 <strong>{html.escape(owner_label)}</strong></span>',
            f'<span class="detail-summary-chip">录入人 <strong>{creator_name}</strong></span>',
        ]
        if completion_flag_visible:
            detail_summary_items.append(
                f'<span class="detail-summary-chip">资料完成 <strong>{html.escape(completion_flag or "空白")}</strong></span>'
            )
        quick_tools_block = self.render_product_quick_tools(product, payload_json, copy_summary)
        content = f"""
        <section class="section-stack">
          <section class="panel">
            <div class="detail-panel-head">
              <div class="detail-panel-main">
                <div class="eyebrow">{html.escape(console_eyebrow)}</div>
                <h2>资料详情</h2>
                <p class="detail-workflow-note">{html.escape(workflow_notice)}</p>
              </div>
              <div class="detail-panel-tools">
                <div class="tools">
                  <a class="pill" href="/products">返回列表</a>
                  {edit_link}
                  {log_link}
                  {version_link}
                </div>
              </div>
            </div>
            {notice_block}
            {hidden_note}
            {f'<div class="warning">这条资料在提交后有新的修改更新，后续处理请以当前版本为准。</div>' if revision_badge else ''}
            <div class="detail-summary-inline">
              {''.join(detail_summary_items)}
            </div>
            <div class="detail-grid">
              {''.join(group_html)}
            </div>
          </section>
          {quick_tools_block}
          {status_block}
          {lifecycle_block}
        </section>
        """
        return self.page(f"资料 #{product['id']} - 商品资料后台", content, user, current_page="products", back_href="/products")

    def render_product_quick_tools(self, product: dict, payload_json: str, copy_summary: str) -> str:
        gallery_values = self.image_gallery_values(product)
        image_value = gallery_values[0] if gallery_values else ""
        image_tool = ""
        if image_value:
            gallery_fields = "".join(
                f"""
                <label class="field field-wide">
                  <span>图片 {index + 1}</span>
                  <div style="display:flex; gap:10px; flex-wrap:wrap;">
                    <input value="{html.escape(gallery_value)}" readonly>
                    <button class="ghost-button" type="button" data-copy-text="{html.escape(gallery_value, quote=True)}">复制图片地址</button>
                  </div>
                </label>
                """
                for index, gallery_value in enumerate(gallery_values)
            )
            image_tool = f"""
              {gallery_fields}
            """
        return f"""
        <section class="panel" style="margin-bottom:18px;">
          <div class="eyebrow">Quick Actions</div>
          <h2>快捷复制与调用片段</h2>
          <p class="meta">适合把当前资料快速发给同事，或复制给外部系统联调时使用。</p>
          <div class="form-grid">
            <label class="field field-wide">
              <span>商品核心信息</span>
              <textarea readonly>{html.escape(copy_summary, quote=False)}</textarea>
              <button class="ghost-button" type="button" data-copy-text="{html.escape(copy_summary, quote=True)}">复制核心信息</button>
            </label>
            <label class="field field-wide">
              <span>当前详情 JSON</span>
              <textarea readonly>{html.escape(payload_json, quote=False)}</textarea>
              <button class="ghost-button" type="button" data-copy-text="{html.escape(payload_json, quote=True)}">复制 JSON</button>
            </label>
            {image_tool}
          </div>
        </section>
        """

    def product_copy_summary(self, product: dict, user: dict) -> str:
        visible_fields = self.visible_fields_for_c_product(product) if user.get("department") == "C" else self.visible_fields_for_user(user)
        visible_keys = {field.key for field in visible_fields}
        gallery_values = self.image_gallery_values(product)
        lines = [
            f"资料ID: {product.get('id', '')}",
            f"商品名称: {product.get('product_name', '') or ''}",
            f"款号: {product.get('style_code', '') or ''}",
            f"发起部门: {department_label(product.get('owner_department'))}",
            f"状态: {status_label(self.c_effective_status(product, user))}",
        ]
        for key, label in (("brand_name", "品牌名称"), ("category", "品类"), ("color_name", "颜色名称"), ("image_url", "图片")):
            if key in visible_keys and product.get(key):
                lines.append(f"{label}: {product.get(key)}")
        if "image_url" in visible_keys and len(gallery_values) > 1:
            lines.append("图片组:")
            for index, image_value in enumerate(gallery_values, start=1):
                lines.append(f"{index}. {image_value}")
        return "\n".join(lines)

    def display_image_gallery(self, product: dict) -> str:
        gallery_values = self.image_gallery_values(product)
        if not gallery_values:
            return self.display_value(product.get("image_url"))
        cards = []
        for index, image_value in enumerate(gallery_values):
            cards.append(
                f"""
                <div style="display:flex; flex-direction:column; gap:8px;">
                  <img src="{html.escape(image_value)}" alt="商品图片 {index + 1}" style="width:100%; max-width:220px; border-radius:14px; border:1px solid rgba(91,58,29,0.12);">
                  <div class="meta">第 {index + 1} 张</div>
                  <div style="word-break:break-all;">{html.escape(image_value)}</div>
                </div>
                """
            )
        return f'<div style="display:flex; gap:14px; flex-wrap:wrap;">{"".join(cards)}</div>'

    def render_import_page(self, user, report: str = "", error: str = "") -> str:
        console_eyebrow = self.brand_config["brand_console_eyebrow"]
        report_block = f'<div class="notice">{html.escape(report)}</div>' if report else ""
        error_block = f'<div class="warning">{html.escape(error)}</div>' if error else ""
        if user.get("department") == "B":
            page_title = "导入商品部补充 Excel"
            intro_text = "商品部收到跟单部流转过来的资料后，可以先导出 Excel，在表内补充“品类”“图片”“上新价格”“上新渠道”“资料完成”，再回到这里导入。系统不会新增资料，只会按款号、商品名称以及款色或颜色去匹配既有条目，并仅回填商品部负责字段。"
            stat_one = "匹配方式"
            stat_one_value = "既有资料精确匹配"
            stat_two = "更新范围"
            stat_two_value = "仅商品部字段"
            stat_three = "后续动作"
            stat_three_value = "批量流转到运营部"
            section_title = "导入商品部补充文件"
            button_text = "开始导入商品部 Excel"
            hint_text = "导入后请回到资料列表，勾选对应条目，再使用“批量完成并开放给运营部”继续流转。空白的商品部字段不会覆盖原值。"
        else:
            page_title = "从参考模板导入 Excel"
            intro_text = "导入时会读取第一张工作表，并按模板第一行表头识别字段。若发现与你本人已录入的同款号、同颜色、同商品名记录，则更新；否则新增。"
            stat_one = "工作表"
            stat_one_value = "首张表"
            stat_two = "识别方式"
            stat_two_value = "模板表头"
            stat_three = "重复策略"
            stat_three_value = "同人更新"
            section_title = "导入文件"
            button_text = "开始导入"
            hint_text = ""
        content = f"""
        <section class="hero">
          <div class="panel">
            <div class="eyebrow">{html.escape(console_eyebrow)}</div>
            <h1>{html.escape(page_title)}</h1>
            <p>{html.escape(intro_text)}</p>
          </div>
          <div class="panel">
            <div class="eyebrow">Import Notes</div>
            <div class="stats">
              <div class="stat-card"><span>{html.escape(stat_one)}</span><strong>{html.escape(stat_one_value)}</strong></div>
              <div class="stat-card"><span>{html.escape(stat_two)}</span><strong>{html.escape(stat_two_value)}</strong></div>
              <div class="stat-card"><span>{html.escape(stat_three)}</span><strong>{html.escape(stat_three_value)}</strong></div>
            </div>
          </div>
        </section>
        <section class="panel">
          <div class="eyebrow">Excel Bridge</div>
          <h2>{html.escape(section_title)}</h2>
          {f'<p class="meta">{html.escape(hint_text)}</p>' if hint_text else ''}
          {report_block}
          {error_block}
          <form method="post" action="/import" enctype="multipart/form-data">
            <div class="form-grid">
              <label class="field field-wide">
                <span>选择 Excel 文件</span>
                <input type="file" name="workbook" accept=".xlsx">
              </label>
            </div>
            <div class="tools" style="margin-top:16px; margin-bottom:0;">
              <a class="pill" href="/products">返回资料列表</a>
              <button type="submit">{html.escape(button_text)}</button>
            </div>
          </form>
        </section>
        """
        return self.page("导入 Excel - 商品资料后台", content, user, current_page="products", back_href="/products")

    def render_image_import_page(self, user, report: dict | None = None, error: str = "") -> str:
        console_eyebrow = self.brand_config["brand_console_eyebrow"]
        error_block = f'<div class="warning">{html.escape(error)}</div>' if error else ""
        report_block = ""
        if report:
            if report.get("mode") == "workbook":
                summary_text = (
                    f"导入完成：已读取 {int(report.get('mapping_row_count', 0))} 条 Excel 映射，接收 {int(report.get('selected_count', 0))} 张图片，成功更新 {int(report.get('updated_count', 0))} 条资料。"
                )
                stats_markup = f"""
                <div class="stats" style="margin-top:14px;">
                  <div class="stat-card"><span>Excel 映射</span><strong>{int(report.get('mapping_row_count', 0))}</strong></div>
                  <div class="stat-card"><span>导入图片</span><strong>{int(report.get('selected_count', 0))}</strong></div>
                  <div class="stat-card"><span>成功更新</span><strong>{int(report.get('updated_count', 0))}</strong></div>
                  <div class="stat-card"><span>未匹配图片</span><strong>{len(report.get('unmatched_images', []))}</strong></div>
                  <div class="stat-card"><span>未匹配资料</span><strong>{len(report.get('unmatched', []))}</strong></div>
                  <div class="stat-card"><span>重复映射</span><strong>{len(report.get('duplicate_mapping_rows', []))}</strong></div>
                </div>
                """
                detail_groups = (
                    ("已更新资料", report.get("matched", []), "notice"),
                    ("Excel 未匹配到图片文件", report.get("unmatched_images", []), "warning"),
                    ("Excel 未匹配到资料款色", report.get("unmatched", []), "warning"),
                    ("重复命名的图片", report.get("duplicate_uploads", []), "warning"),
                    ("匹配到多条资料的款色", report.get("ambiguous_matches", []), "warning"),
                    ("Excel 重复映射", report.get("duplicate_mapping_rows", []), "warning"),
                )
            else:
                summary_text = (
                    f"导入完成：共接收 {int(report.get('selected_count', 0))} 张图片，成功更新 {int(report.get('updated_count', 0))} 条资料。"
                )
                stats_markup = f"""
                <div class="stats" style="margin-top:14px;">
                  <div class="stat-card"><span>导入图片</span><strong>{int(report.get('selected_count', 0))}</strong></div>
                  <div class="stat-card"><span>成功更新</span><strong>{int(report.get('updated_count', 0))}</strong></div>
                  <div class="stat-card"><span>未匹配</span><strong>{len(report.get('unmatched', []))}</strong></div>
                  <div class="stat-card"><span>重复文件</span><strong>{len(report.get('duplicate_uploads', []))}</strong></div>
                  <div class="stat-card"><span>重复资料</span><strong>{len(report.get('ambiguous_matches', []))}</strong></div>
                  <div class="stat-card"><span>无效命名</span><strong>{len(report.get('invalid_names', []))}</strong></div>
                </div>
                """
                detail_groups = (
                    ("已更新资料", report.get("matched", []), "notice"),
                    ("未匹配到款色的图片", report.get("unmatched", []), "warning"),
                    ("重复命名的图片", report.get("duplicate_uploads", []), "warning"),
                    ("匹配到多条资料的款色", report.get("ambiguous_matches", []), "warning"),
                    ("未识别文件名", report.get("invalid_names", []), "warning"),
                )
            detail_sections = []
            for title, items, tone in detail_groups:
                if not items:
                    continue
                list_items = "".join(f"<li>{html.escape(str(item))}</li>" for item in items)
                detail_sections.append(
                    f"""
                    <div class="{tone}" style="margin-top:14px;">
                      <strong>{html.escape(title)}</strong>
                      <ul style="margin:10px 0 0 18px;">
                        {list_items}
                      </ul>
                    </div>
                    """
                )
            report_block = f"""
            <div class="notice">{html.escape(summary_text)}</div>
            {stats_markup}
            {''.join(detail_sections)}
            """
        content = f"""
        <section class="hero">
          <div class="panel">
            <div class="eyebrow">{html.escape(console_eyebrow)}</div>
            <h1>导入图片</h1>
            <p>支持两种导入方式：一是直接上传多张 JPG、JPEG、PNG、WEBP 或 GIF 图片，系统按文件名去掉扩展名后的“款色”自动匹配；二是上传一份包含“款色”和“图片/图片文件名”的 Excel，再连同对应图片一起导入，按 Excel 指定关系更新资料。</p>
          </div>
          <div class="panel">
            <div class="stats">
              <div class="stat-card"><span>方式一</span><strong>文件名对款色</strong></div>
              <div class="stat-card"><span>方式二</span><strong>Excel 对图片</strong></div>
              <div class="stat-card"><span>更新范围</span><strong>商品部可编辑资料</strong></div>
              <div class="stat-card"><span>图片策略</span><strong>覆盖当前图片</strong></div>
            </div>
          </div>
        </section>
        <section class="panel">
          <h2>导入图片</h2>
          <p class="meta">方式一示例：如果资料里的款色是“短袖连衣裙-蓝”，图片文件名就命名为“短袖连衣裙-蓝.jpg”。方式二示例：Excel 至少保留“款色”和“图片文件名”两列，再上传这份 Excel 和对应图片文件，系统会按映射关系一一写回资料。</p>
          {error_block}
          {report_block}
          <form method="post" action="/import-images" enctype="multipart/form-data">
            <div class="form-grid">
              <label class="field field-wide">
                <span>选择映射 Excel（可选）</span>
                <input type="file" name="mapping_workbook" accept=".xlsx,.xls">
              </label>
              <label class="field field-wide">
                <span>选择图片文件</span>
                <input type="file" name="image_files" accept=".jpg,.jpeg,.png,.webp,.gif,image/*" multiple>
              </label>
            </div>
            <div class="tools" style="margin-top:16px; margin-bottom:0;">
              <a class="pill" href="/products">返回资料列表</a>
              <button type="submit">开始导入图片</button>
            </div>
          </form>
        </section>
        """
        return self.page("导入图片 - 商品资料后台", content, user, current_page="products", back_href="/products")

    def render_password_change_page(self, user, errors: list[str] | None = None) -> str:
        console_eyebrow = self.brand_config["brand_console_eyebrow"]
        error_block = ""
        if errors:
            items = "".join(f"<li>{html.escape(error)}</li>" for error in errors)
            error_block = f'<ul class="error-list">{items}</ul>'
        force_notice = ""
        if user.get("must_change_password"):
            force_notice = '<div class="warning">当前账号被设置为首次登录或重置后必须修改密码，完成后才能继续使用后台。</div>'
        content = f"""
        <section class="hero">
          <div class="panel">
            <div class="eyebrow">{html.escape(console_eyebrow)}</div>
            <h1>修改登录密码</h1>
            <p class="meta">建议使用不少于 6 位的新密码。修改完成后会立即生效。</p>
            {force_notice}
          </div>
          <div class="panel">
            <div class="eyebrow">Security</div>
            <div class="stats">
              <div class="stat-card"><span>当前账号</span><strong>{html.escape(user.get('display_name', ''))}</strong></div>
              <div class="stat-card"><span>密码策略</span><strong>{'强制修改' if user.get('must_change_password') else '常规修改'}</strong></div>
            </div>
          </div>
        </section>
        <section class="panel" style="max-width:820px; margin:0 auto;">
          <div class="eyebrow">Security</div>
          <h2>密码更新</h2>
          {error_block}
          <form method="post" action="/profile/password">
            <div class="form-grid">
              <label class="field field-wide">
                <span>当前密码</span>
                <input type="password" name="current_password" placeholder="请输入当前密码">
              </label>
              <label class="field">
                <span>新密码</span>
                <input type="password" name="new_password" placeholder="至少 6 位">
              </label>
              <label class="field">
                <span>确认新密码</span>
                <input type="password" name="confirm_password" placeholder="请再次输入新密码">
              </label>
            </div>
            <div class="tools" style="margin-top:16px;">
              <a class="pill" href="/products">返回资料列表</a>
              <button type="submit">保存新密码</button>
            </div>
          </form>
        </section>
        """
        return self.page("修改密码 - 商品资料后台", content, user, back_href="/modules")

    def render_message_page(self, title: str, message: str, user=None) -> str:
        content = f"""
        <section class="panel" style="max-width:820px; margin:0 auto;">
          <div class="eyebrow">System Message</div>
          <h1>{html.escape(title)}</h1>
          <p class="meta">{html.escape(message)}</p>
          <div class="tools" style="margin-top:16px; margin-bottom:0;">
            <a class="pill" href="/products">返回资料列表</a>
          </div>
        </section>
        """
        return self.page(f"{title} - 商品资料后台", content, user, back_href="/modules" if user else None)

    def render_users_page(self, user, notice: str = "", form_values: dict | None = None, errors: list[str] | None = None) -> str:
        console_eyebrow = self.brand_config["brand_console_eyebrow"]
        users = db.list_users(self.db_path)
        form_values = form_values or {}
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        error_block = ""
        if errors:
            items = "".join(f"<li>{html.escape(error)}</li>" for error in errors)
            error_block = f'<ul class="error-list">{items}</ul>'
        rows = []
        for managed_user in users:
            active_label = "启用中" if managed_user.get("is_active") else "已停用"
            must_change_label = "需改密" if managed_user.get("must_change_password") else "正常"
            billing_platforms = "、".join(
                billing_platform_label(code)
                for code in normalize_billing_platform_codes(managed_user.get("billing_platforms_json"))
            )
            rows.append(
                f"""
                <tr>
                  <td>{managed_user['id']}</td>
                  <td>{html.escape(managed_user.get('username') or '')}</td>
                  <td>{html.escape(department_label(managed_user.get('department')))}</td>
                  <td>{html.escape(operating_channel_label(managed_user.get('operating_channel')) if managed_user.get('department') == 'C' else '—')}</td>
                  <td>{html.escape(billing_platforms if managed_user.get('department') == 'C' and billing_platforms else '—')}</td>
                  <td><span class="pill">{active_label}</span></td>
                  <td>{must_change_label}</td>
                  <td>{self.display_value(managed_user.get('created_at'))}</td>
                  <td class="user-account-actions-cell">
                    <div class="user-account-primary-action">
                      <a href="/users/{managed_user['id']}/edit">编辑</a>
                      <form method="post" action="/users/{managed_user['id']}/toggle">
                        {'<input name="confirm_text" placeholder="输入 DISABLE">' if managed_user.get('is_active') else ''}
                        <button class="ghost-button" type="submit">{'停用' if managed_user.get('is_active') else '启用'}</button>
                      </form>
                    </div>
                    <form class="user-account-reset-action" method="post" action="/users/{managed_user['id']}/reset-password">
                      <input type="password" name="new_password" placeholder="新密码">
                      <input name="confirm_text" placeholder="输入 RESET">
                      <button class="ghost-button" type="submit">重置密码</button>
                    </form>
                  </td>
                </tr>
                """
            )
        department_options = "".join(
            f'<option value="{code}" {"selected" if form_values.get("department") == code else ""}>{department_label(code)}</option>'
            for code in MANAGEABLE_DEPARTMENTS
        )
        operating_channel_options = "".join(
            f'<option value="{code}" {"selected" if form_values.get("operating_channel") == code else ""}>{html.escape(label)}</option>'
            for code, label in C_OPERATING_CHANNELS.items()
        )
        selected_billing_platforms = set(self.billing_platform_codes_for_user_form(form_values))
        billing_platform_options = "".join(
            f'<label class="account-platform-option"><input type="checkbox" name="billing_platform_codes__{html.escape(code)}" value="{html.escape(code)}" {"checked" if code in selected_billing_platforms else ""}> {html.escape(label)}</label>'
            for code, label in BILLING_PLATFORM_OPTIONS
        )
        content = f"""
        <section class="panel">
          <div class="eyebrow">{html.escape(console_eyebrow)}</div>
          <h1>账号管理 - 新建账号</h1>
          <p class="meta">管理员可以创建账号、分配角色、启用或停用账号，并重置登录密码。</p>
          {notice_block}
          {error_block}
          <form method="post" action="/users">
            <div class="form-grid account-create-grid">
              <label class="field">
                <span>用户名</span>
                <input name="username" value="{html.escape(form_values.get('username', ''))}" placeholder="例如 buyer_a_01">
              </label>
              <label class="field">
                <span>初始密码</span>
                <input type="password" name="password" placeholder="至少 6 位">
              </label>
              <label class="field">
                <span>首次登录</span>
                <select name="must_change_password">
                  <option value="on">要求修改密码</option>
                  <option value="">无需强制修改</option>
                </select>
              </label>
              <label class="field">
                <span>角色/部门</span>
                <select name="department">{department_options}</select>
              </label>
              <label class="field account-create-channel">
                <span>渠道属性</span>
                <select name="operating_channel">
                  <option value="">非运营部无需选择</option>
                  {operating_channel_options}
                </select>
              </label>
              <div class="field account-create-billing">
                <span>账单属性（可多选）</span>
                <div class="account-platform-options">{billing_platform_options}</div>
                <div class="meta">仅运营部生效，用于限定可上传、删除、提交或申请退回的平台账单。</div>
              </div>
              <div class="account-create-submit">
                <button type="submit">创建账号</button>
              </div>
            </div>
          </form>
        </section>
        <section class="panel" style="margin-top:18px;">
          <h2>已有账号</h2>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>用户名</th>
                  <th>角色/部门</th>
                  <th>渠道属性</th>
                  <th>账单属性</th>
                  <th>状态</th>
                  <th>密码策略</th>
                  <th>创建时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows)}
              </tbody>
            </table>
          </div>
        </section>
        """
        return self.page("账号管理 - 商品资料后台", content, user, back_href="/products")

    def render_user_edit_page(self, user, managed_user: dict, errors: list[str] | None = None) -> str:
        error_block = ""
        if errors:
            items = "".join(f"<li>{html.escape(error)}</li>" for error in errors)
            error_block = f'<ul class="error-list">{items}</ul>'
        department_options = "".join(
            f'<option value="{code}" {"selected" if managed_user.get("department") == code else ""}>{department_label(code)}</option>'
            for code in MANAGEABLE_DEPARTMENTS
        )
        operating_channel_options = "".join(
            f'<option value="{code}" {"selected" if managed_user.get("operating_channel") == code else ""}>{html.escape(label)}</option>'
            for code, label in C_OPERATING_CHANNELS.items()
        )
        selected_billing_platforms = set(self.billing_platform_codes_for_user_form(managed_user))
        billing_platform_options = "".join(
            f'<label class="account-platform-option"><input type="checkbox" name="billing_platform_codes__{html.escape(code)}" value="{html.escape(code)}" {"checked" if code in selected_billing_platforms else ""}> {html.escape(label)}</label>'
            for code, label in BILLING_PLATFORM_OPTIONS
        )
        content = f"""
        <section class="panel" style="max-width:920px; margin:0 auto;">
          <div class="eyebrow">User Profile</div>
          <h1>编辑账号 {html.escape(managed_user.get('username', ''))}</h1>
          <p class="meta">可修改显示名称和角色分配；密码请在账号列表页单独重置。</p>
          {error_block}
          <form method="post" action="/users/{managed_user['id']}/edit">
            <div class="form-grid">
              <label class="field">
                <span>用户名</span>
                <input value="{html.escape(managed_user.get('username', ''))}" disabled>
              </label>
              <label class="field">
                <span>显示名称</span>
                <input name="display_name" value="{html.escape(managed_user.get('display_name', ''))}">
              </label>
              <label class="field">
                <span>角色/部门</span>
                <select name="department">{department_options}</select>
              </label>
              <label class="field">
                <span>渠道属性</span>
                <select name="operating_channel">
                  <option value="">非运营部无需选择</option>
                  {operating_channel_options}
                </select>
              </label>
              <div class="field field-wide">
                <span>账单属性（可多选）</span>
                <div class="account-platform-options">{billing_platform_options}</div>
                <div class="meta">仅运营部生效，可选择该账号可处理的一个或多个平台。</div>
              </div>
            </div>
            <div class="tools" style="margin-top:16px;">
              <a class="pill" href="/users">返回账号管理</a>
              <button type="submit">保存账号资料</button>
            </div>
          </form>
        </section>
        """
        return self.page(f"编辑账号 - {managed_user.get('username', '')}", content, user, back_href="/users")

    def render_review_queue(self, user, query) -> str:
        console_eyebrow = self.brand_config["brand_console_eyebrow"]
        if not is_admin(user):
            return self.render_message_page("权限不足", "只有管理员可以查看流转看板。", user)
        keyword = query.get("q", "").strip()
        department_filter = query.get("department", "").strip()
        notice = query.get("notice", "").strip()
        products = db.list_products(
            self.db_path,
            query=keyword,
            department=department_filter,
            status="pending",
        )
        rows = []
        for product in products:
            rows.append(
                f"""
                <tr>
                  <td><input type="checkbox" name="product_ids" value="{product['id']}" style="width:auto;"></td>
                  <td><a href="/products/{product['id']}">#{product['id']}</a></td>
                  <td>{self.display_value(product.get('product_name'))}</td>
                  <td>{self.display_value(product.get('style_code'))}</td>
                  <td>{html.escape(department_label(product.get('owner_department')))}</td>
                  <td>{self.display_value(product.get('creator_name'))}</td>
                  <td>{self.display_value(product.get('updated_at'))}</td>
                  <td><a href="/products/{product['id']}">进入流转详情</a></td>
                </tr>
                """
            )
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        content = f"""
        <section class="hero">
          <div class="panel">
            <div class="eyebrow">{html.escape(console_eyebrow)}</div>
            <h1>资料流转看板</h1>
            <p class="meta">这里主要追踪已经由跟单部提交、等待商品部补充品类、图片、上新价格、上新渠道和资料完成的资料。系统管理员也可以在详情页人工干预流转。</p>
            {notice_block}
          </div>
          <div class="panel">
            <div class="eyebrow">Workflow Board</div>
            <div class="stats">
              <div class="stat-card"><span>当前待商品部填写</span><strong>{len(products)}</strong></div>
              <div class="stat-card"><span>优先排序</span><strong>最近提交</strong></div>
              <div class="stat-card"><span>管理员动作</span><strong>人工流转</strong></div>
            </div>
          </div>
        </section>
        <section id="review-filter" class="panel query-anchor">
          <div class="eyebrow">Workflow Filters</div>
          <h2>筛选待商品部填写资料</h2>
          <form method="get" action="/products/review#review-filter">
            <div class="form-grid">
              <label class="field">
                <span>关键词</span>
                <input name="q" value="{html.escape(keyword)}" placeholder="按商品名称、款号搜索待商品部填写资料">
              </label>
              <label class="field">
                <span>发起部门</span>
                <select name="department">
                  <option value="">全部部门</option>
                  <option value="A" {"selected" if department_filter == "A" else ""}>跟单部</option>
                </select>
              </label>
            </div>
            <div class="tools" style="margin-top:16px; margin-bottom:0;">
              <button type="submit">筛选待商品部填写资料</button>
              <a class="pill" href="/products/review">清空筛选</a>
            </div>
          </form>
        </section>
        <form method="post" action="/products/review/bulk">
          <section class="panel" style="margin-bottom:18px;">
            <div class="eyebrow">Batch Workflow</div>
            <h2>批量完成</h2>
            <p class="meta">勾选后可直接批量标记为已完成并开放给运营部；不符合条件的资料会自动跳过。</p>
            <div class="tools" style="margin-bottom:0;">
              <button type="submit">批量完成并开放给运营部</button>
            </div>
          </section>
          <section class="panel">
            <div class="table-wrap">
              <table>
              <thead>
                <tr>
                  <th>勾选</th>
                  <th>ID</th>
                  <th>商品名称</th>
                  <th>款号</th>
                  <th>发起部门</th>
                  <th>录入人</th>
                  <th>更新时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows) if rows else '<tr><td colspan="8">当前没有待商品部填写资料。</td></tr>'}
              </tbody>
              </table>
            </div>
          </section>
        </form>
        """
        return self.page("流转看板 - 商品资料后台", content, user, current_page="products", back_href="/products")

    def render_product_logs(self, user, product: dict, query: dict | None = None) -> str:
        query = query or {}
        action_query = query.get("action", "").strip()
        actor_query = query.get("actor", "").strip()
        logs = self.filtered_product_logs(product["id"], query)
        export_query = urlencode({"action": action_query, "actor": actor_query})
        rows = []
        for item in logs:
            diff_markup = ""
            if item.get("diff_items"):
                summary_labels = [html.escape(diff.get("field_label", "")) for diff in item["diff_items"] if diff.get("field_label")]
                if summary_labels:
                    summary_count = len(summary_labels)
                    change_count = int(item.get("change_count") or summary_count)
                    diff_markup = (
                        '<div style="margin-top:10px; padding:12px; border:1px solid rgba(91,58,29,0.12); border-radius:14px; background:rgba(255,255,255,0.68);">'
                        f'本次共修改 {change_count} 项，仅摘要展示 {summary_count} 项：{"、".join(summary_labels)}'
                        "</div>"
                    )
            rows.append(
                f"""
                <tr>
                  <td>{self.display_value(item.get('created_at'))}</td>
                  <td>{self.display_value(item.get('actor_name'))}</td>
                  <td>{html.escape(department_label(item.get('actor_department')))}</td>
                  <td>{self.display_value(item.get('action_label'))}</td>
                  <td>{self.display_value(item.get('details'))}{diff_markup}</td>
                </tr>
                """
            )
        content = f"""
        <section class="panel">
          <div class="eyebrow">Audit Trail</div>
          <h1>资料 #{product['id']} 操作日志</h1>
          <p class="meta">{self.display_value(product.get('product_name'))} · 当前状态 {html.escape(status_label(product.get('status')))}</p>
          <div class="tools">
            <a class="pill" href="/products/{product['id']}">返回详情</a>
            <a class="pill" href="/products/{product['id']}/logs/export.csv?{html.escape(export_query)}">导出 CSV</a>
          </div>
          <form id="product-log-filter" method="get" action="/products/{product['id']}/logs#product-log-filter" class="panel query-anchor" style="margin-bottom:18px;">
            <div class="form-grid">
              <label class="field">
                <span>按动作筛选</span>
                <input name="action" value="{html.escape(action_query)}" placeholder="例如 提交给B填写 / 填写完成，开放给C / update">
              </label>
              <label class="field">
                <span>按操作人筛选</span>
                <input name="actor" value="{html.escape(actor_query)}" placeholder="例如 A 部门录入员 / B 部门录入员 / 系统管理员">
              </label>
            </div>
            <div class="tools" style="margin-top:16px; margin-bottom:0;">
              <button type="submit">筛选日志</button>
              <a class="pill" href="/products/{product['id']}/logs">清空筛选</a>
            </div>
          </form>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>时间</th>
                  <th>操作人</th>
                  <th>部门</th>
                  <th>动作</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows) if rows else '<tr><td colspan="5">暂无操作日志。</td></tr>'}
              </tbody>
            </table>
          </div>
        </section>
        """
        return self.page(f"日志 #{product['id']} - 商品资料后台", content, user, current_page="products", back_href=f"/products/{product['id']}")

    def render_product_versions(self, user, product: dict, query: dict | None = None) -> str:
        versions = db.list_product_versions(self.db_path, product["id"])
        rows = []
        current_version_no = int(product.get("current_version_no") or 1)
        for item in versions:
            summary_labels = [html.escape(diff.get("field_label", "")) for diff in item.get("summary_items") or [] if diff.get("field_label")]
            change_count = int(item.get("change_count") or 0)
            summary_text = "、".join(summary_labels) if summary_labels else "初始版本或无字段摘要"
            if summary_labels and change_count > len(summary_labels):
                summary_text = f"共 {change_count} 项，摘要展示：{summary_text}"
            elif summary_labels:
                summary_text = f"共 {change_count} 项：{summary_text}"
            elif change_count > 0:
                summary_text = f"共 {change_count} 项，暂无摘要字段"
            version_badges = self.version_badges(item, current_version_no)
            restore_action = '<span class="meta">当前版本无需恢复</span>'
            if int(item.get("version_no") or 0) != current_version_no:
                restore_action = f"""
                <form method="post" action="/products/{product['id']}/versions/{item['version_no']}/restore" style="display:inline-flex;">
                  <button class="ghost-button" type="submit">恢复为当前版本</button>
                </form>
                """
            rows.append(
                f"""
                <tr>
                  <td>
                    <div style="display:flex; flex-direction:column; gap:8px;">
                      <strong>V{item.get('version_no')}</strong>
                      <div style="display:flex; gap:8px; flex-wrap:wrap;">{version_badges}</div>
                    </div>
                  </td>
                  <td>{self.display_value(item.get('created_at'))}</td>
                  <td>{self.display_value(item.get('actor_name'))}</td>
                  <td>{html.escape(department_label(item.get('actor_department')))}</td>
                  <td>{html.escape(item.get('note') or '')}</td>
                  <td>{change_count}</td>
                  <td>{summary_text}</td>
                  <td>{restore_action}</td>
                </tr>
                """
            )
        content = f"""
        <section class="panel">
          <div class="eyebrow">Version Archive</div>
          <h1>资料 #{product['id']} 修改版本</h1>
          <p class="meta">{self.display_value(product.get('product_name'))} · 当前版本 {html.escape(self.version_label(product))}</p>
          <div class="tools">
            <a class="pill" href="/products/{product['id']}">返回详情</a>
            <a class="pill" href="/products/{product['id']}/logs">查看日志</a>
          </div>
          <div class="table-wrap" style="margin-top:18px;">
            <table>
              <thead>
                <tr>
                  <th>修改版本</th>
                  <th>生成时间</th>
                  <th>操作人</th>
                  <th>部门</th>
                  <th>说明</th>
                  <th>修改项数量</th>
                  <th>摘要项</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows) if rows else '<tr><td colspan="8">暂无版本记录。</td></tr>'}
              </tbody>
            </table>
          </div>
        </section>
        """
        return self.page(f"版本 #{product['id']} - 商品资料后台", content, user, current_page="products", back_href=f"/products/{product['id']}")

    def render_logs_center(self, user, query: dict | None = None) -> str:
        console_eyebrow = self.brand_config["brand_console_eyebrow"]
        query = query or {}
        action_query = query.get("action", "").strip()
        actor_query = query.get("actor", "").strip()
        product_query = query.get("product", "").strip()
        logs = self.filtered_global_logs(user, query)
        export_query = urlencode({"action": action_query, "actor": actor_query, "product": product_query})
        rows = []
        for item in logs:
            rows.append(
                f"""
                <tr>
                  <td><a href="/products/{item['product_id']}">#{item['product_id']}</a></td>
                  <td>{self.display_value(item.get('product_name'))}</td>
                  <td>{self.display_value(item.get('style_code'))}</td>
                  <td>{html.escape(department_label(item.get('owner_department')))}</td>
                  <td>{self.display_value(item.get('created_at'))}</td>
                  <td>{self.display_value(item.get('actor_name'))}</td>
                  <td>{html.escape(department_label(item.get('actor_department')))}</td>
                  <td>{self.display_value(item.get('action_label'))}</td>
                  <td>{self.display_value(item.get('details'))}</td>
                  <td>{f'<a href="/products/{item["product_id"]}/logs">查看单条日志</a>' if item.get('product_id') else '<span class="meta">管理审计</span>'}</td>
                </tr>
                """
            )
        content = f"""
        <section class="hero">
          <div class="panel">
            <div class="eyebrow">{html.escape(console_eyebrow)}</div>
            <h1>全局操作日志中心</h1>
            <p class="meta">这里汇总当前账号有权查看的全部商品操作日志；管理员还会看到账号、令牌、字段开放配置等管理审计动作，可按对象、动作、操作人筛选，并导出当前结果。</p>
            <div class="tools">
              <a class="pill" href="/products">返回资料列表</a>
              <a class="pill" href="/logs/export.csv?{html.escape(export_query)}">导出 CSV</a>
            </div>
          </div>
          <div class="panel">
            <div class="eyebrow">Audit Center</div>
            <div class="stats">
              <div class="stat-card"><span>当前结果数</span><strong>{len(logs)}</strong></div>
              <div class="stat-card"><span>导出能力</span><strong>CSV</strong></div>
              <div class="stat-card"><span>审计范围</span><strong>{'全局' if is_admin(user) else '本人'} </strong></div>
            </div>
          </div>
        </section>
        <section id="logs-filter" class="panel query-anchor">
          <div class="eyebrow">Audit Filters</div>
          <h2>日志筛选</h2>
          <form method="get" action="/logs#logs-filter">
            <div class="form-grid">
              <label class="field">
                <span>按商品筛选</span>
                <input name="product" value="{html.escape(product_query)}" placeholder="例如 商品名称 / 款号 / 资料ID">
              </label>
              <label class="field">
                <span>按动作筛选</span>
                <input name="action" value="{html.escape(action_query)}" placeholder="例如 提交给B填写 / 填写完成，开放给C / update">
              </label>
              <label class="field">
                <span>按操作人筛选</span>
                <input name="actor" value="{html.escape(actor_query)}" placeholder="例如 A 部门录入员 / B 部门录入员 / 系统管理员">
              </label>
            </div>
            <div class="tools" style="margin-top:16px; margin-bottom:0;">
              <button type="submit">筛选日志</button>
              <a class="pill" href="/logs">清空筛选</a>
            </div>
          </form>
        </section>
        <section class="panel">
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>资料ID</th>
                  <th>商品名称</th>
                  <th>款号</th>
                  <th>资料发起部门</th>
                  <th>时间</th>
                  <th>操作人</th>
                  <th>操作人部门</th>
                  <th>动作</th>
                  <th>说明</th>
                  <th>跳转</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows) if rows else '<tr><td colspan="10">暂无符合条件的日志。</td></tr>'}
              </tbody>
            </table>
          </div>
        </section>
        """
        return self.page("日志中心 - 商品资料后台", content, user, current_page="products", back_href="/products")

    def render_c_field_settings_page(
        self,
        user,
        notice: str = "",
        selected_keys: list[str] | None = None,
        errors: list[str] | None = None,
        template_name: str = "",
    ) -> str:
        console_eyebrow = self.brand_config["brand_console_eyebrow"]
        selected = set(selected_keys or self.configured_c_field_keys())
        api_token = self.configured_c_api_token()
        templates = self.c_field_templates()
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        error_block = ""
        if errors:
            items = "".join(f"<li>{html.escape(error)}</li>" for error in errors)
            error_block = f'<ul class="error-list">{items}</ul>'
        matching_templates = [name for name, template in templates.items() if set(template["field_keys"]) == selected]
        if matching_templates:
            matching_text = "当前开放组合匹配模板：" + "、".join(matching_templates)
        else:
            matching_text = "当前开放组合尚未绑定到已保存模板。"
        template_cards = []
        for name, template in templates.items():
            keys = template["field_keys"]
            preview_labels = [PRODUCT_FIELD_MAP[key].label for key in keys if key in PRODUCT_FIELD_MAP][:6]
            preview = "、".join(preview_labels)
            if len(keys) > len(preview_labels):
                preview = f"{preview} 等 {len(keys)} 个字段"
            current_badge = '<span class="meta" style="color:#7c5227;">当前使用中</span>' if set(keys) == selected else ""
            template_cards.append(
                f"""
                <div class="field" style="padding:16px; border:1px solid rgba(91,58,29,0.12); border-radius:14px;">
                  <div style="display:flex; justify-content:space-between; gap:16px; align-items:flex-start; flex-wrap:wrap;">
                    <div style="flex:1; min-width:220px;">
                      <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                        <strong>{html.escape(name)}</strong>
                        {current_badge}
                      </div>
                      <div class="meta">共 {len(keys)} 个字段</div>
                      <div class="meta">{html.escape(preview)}</div>
                    </div>
                    <div class="tools" style="margin:0;">
                      <form method="post" action="/settings/c-fields" style="margin:0;">
                        <input type="hidden" name="template_name" value="{html.escape(name)}">
                        <button type="submit" name="apply_template" value="1">套用模板</button>
                      </form>
                      <form method="post" action="/settings/c-fields" style="margin:0;">
                        <input type="hidden" name="template_name" value="{html.escape(name)}">
                        <button class="ghost-button" type="submit" name="delete_template" value="1">删除模板</button>
                      </form>
                    </div>
                  </div>
                </div>
                """
            )
        available_groups: dict[str, list[FieldDef]] = {}
        for field in self.c_field_available_fields():
            available_groups.setdefault(field.group, []).append(field)
        groups = []
        for group, fields in available_groups.items():
            checkboxes = []
            for field in fields:
                checked = "checked" if field.key in selected else ""
                checkboxes.append(
                    f"""
                    <label class="field" style="padding:12px; border:1px solid rgba(91,58,29,0.12); border-radius:14px;">
                      <span style="display:flex; gap:10px; align-items:center;">
                        <input type="checkbox" name="field_keys__{field.key}" value="{field.key}" {checked} style="width:auto;">
                        <strong>{html.escape(field.label)}</strong>
                      </span>
                      <span class="meta">字段标识：{html.escape(field.key)}</span>
                    </label>
                    """
                )
            groups.append(
                f'<section class="panel" style="margin-top:18px;"><h2>{html.escape(group)}</h2><div class="form-grid">{"".join(checkboxes)}</div></section>'
            )
        content = f"""
        <section class="hero">
          <div class="panel">
            <div class="eyebrow">{html.escape(console_eyebrow)}</div>
            <h1>C 部门字段开放设置</h1>
            <p class="meta">这里决定运营部在所有已完成资料里能看到哪些字段列，也可以管理给外部系统调用的只读 API 令牌。</p>
            <p class="meta">当前已开放 {len(selected)} 个字段。{html.escape(matching_text)}</p>
            {notice_block}
            {error_block}
          </div>
          <div class="panel">
            <div class="eyebrow">Field Access</div>
            <div class="stats">
              <div class="stat-card"><span>已开放字段</span><strong>{len(selected)}</strong></div>
              <div class="stat-card"><span>字段模板</span><strong>{len(templates)}</strong></div>
              <div class="stat-card"><span>API 令牌</span><strong>{'已启用' if api_token else '未启用'}</strong></div>
            </div>
          </div>
        </section>
        <section class="panel">
          <section class="panel" style="margin-top:18px;">
            <h2>字段模板</h2>
            <p class="meta">可以把当前勾选保存成模板，后续一键套用到运营部的字段开放范围。</p>
            <div class="form-grid">
              {''.join(template_cards) if template_cards else '<div class="meta">当前还没有保存模板。你可以先在下方勾选字段，再保存为一个模板。</div>'}
            </div>
          </section>
          <form method="post" action="/settings/c-fields">
            <section class="panel" style="margin-top:18px;">
              <h2>保存当前组合为模板</h2>
              <p class="meta">模板保存后会保留当前勾选，并同步成为运营部当前开放字段。模板只保存字段列组合，不区分具体资料项。</p>
              <label class="field field-wide">
                <span>模板名称</span>
                <input name="template_name" value="{html.escape(template_name)}" placeholder="例如：招商只读 / 合规查看 / 直播提案">
              </label>
              <div class="tools" style="margin-top:16px; margin-bottom:0;">
                <button type="submit" name="save_template" value="1">保存为字段模板</button>
              </div>
            </section>
            <section class="panel" style="margin-top:18px;">
              <h2>C 部门 API 令牌</h2>
              <p class="meta">外部系统可使用 `Authorization: Bearer &lt;token&gt;` 或 `?access_token=&lt;token&gt;` 调用 `/api/products`，返回内容始终按 C 部门开放字段裁剪。</p>
              <label class="field field-wide">
                <span>当前令牌</span>
                <input value="{html.escape(api_token or '当前未启用 API 令牌')}" readonly>
              </label>
              <label class="field field-wide">
                <span>停用确认</span>
                <input name="confirm_text" placeholder="如需停用令牌，请输入 DISABLE">
              </label>
              <div class="tools" style="margin-top:16px; margin-bottom:0;">
                <button type="submit" name="rotate_token" value="1">{'重新生成令牌' if api_token else '生成令牌'}</button>
                <button class="ghost-button" type="submit" name="disable_token" value="1">停用令牌</button>
              </div>
            </section>
            {''.join(groups)}
            <section class="panel" style="margin-top:18px;">
              <button type="submit">保存字段开放设置</button>
            </section>
          </form>
        </section>
        """
        return self.page("字段开放设置 - 商品资料后台", content, user, current_page="products", back_href="/products")

    def render_list_layout_settings_page(
        self,
        user,
        notice: str = "",
        selected_keys: list[str] | None = None,
        errors: list[str] | None = None,
    ) -> str:
        console_eyebrow = self.brand_config["brand_console_eyebrow"]
        available_fields = self.list_layout_available_fields(user)
        selected = selected_keys if selected_keys is not None else self.configured_list_layout_keys(user)
        selected = self.normalize_list_layout_keys(selected, user)
        selected_set = set(selected)
        remaining_fields = [field for field in available_fields if field.key not in selected_set]
        ordered_fields = [self.list_layout_field_by_key(key) for key in selected]
        ordered_fields = [field for field in ordered_fields if field]
        ordered_fields.extend(remaining_fields)
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        error_block = ""
        if errors:
            items = "".join(f"<li>{html.escape(error)}</li>" for error in errors)
            error_block = f'<ul class="error-list">{items}</ul>'
        rows = []
        for index, field in enumerate(ordered_fields, start=1):
            checked = "checked" if field.key in selected_set else ""
            rows.append(
                f"""
                <label class="field" style="padding:14px; border:1px solid rgba(91,58,29,0.12); border-radius:14px;">
                  <input type="hidden" name="field_order__{index}" value="{html.escape(field.key)}">
                  <span style="display:grid; grid-template-columns:auto minmax(0, 1fr) 92px; gap:12px; align-items:center;">
                    <span style="display:flex; gap:10px; align-items:center;">
                      <input type="checkbox" name="field_keys__{field.key}" value="{field.key}" {checked} style="width:auto;">
                    </span>
                    <span>
                      <strong>{html.escape(field.label)}</strong>
                      <span class="meta" style="display:block; margin-top:4px;">字段标识：{html.escape(field.key)}</span>
                    </span>
                    <span>
                      <span class="meta-label" style="margin-bottom:4px;">顺序</span>
                      <input type="number" min="1" name="field_rank__{field.key}" value="{index}" style="padding:10px 12px;">
                    </span>
                  </span>
                </label>
                """
            )
        content = f"""
        <section class="hero">
          <div class="panel">
            <div class="eyebrow">{html.escape(console_eyebrow)}</div>
            <h1>{html.escape(department_label(user.get("department")))}列表字段设置</h1>
            <p class="meta">这里设置当前部门在资料列表页里想看到哪些业务字段，以及从左到右的展示顺序。系统列如状态、修改版本、历时天数、发起人和操作会固定保留；资料完成可以按需加入列表字段。</p>
            {notice_block}
            {error_block}
          </div>
          <div class="panel">
            <div class="eyebrow">Layout Rules</div>
            <div class="stats">
              <div class="stat-card"><span>当前部门</span><strong>{html.escape(department_label(user.get("department")))}</strong></div>
              <div class="stat-card"><span>可选字段</span><strong>{len(available_fields)}</strong></div>
              <div class="stat-card"><span>已选字段</span><strong>{len(selected)}</strong></div>
            </div>
          </div>
        </section>
        <section class="panel">
          <h2>字段顺序设置</h2>
          <p class="meta">勾选要显示的字段，并为每个字段填写顺序数字。列表页会按照数字从小到大展示，适合按部门固定品牌、款号、商品名称等列的排列方式。</p>
          <form method="post" action="/settings/list-layout">
            <div class="form-grid">
              {''.join(rows)}
            </div>
            <div class="tools" style="margin-top:18px; margin-bottom:0;">
              <button type="submit">保存列表字段设置</button>
              <a class="pill" href="/products">返回资料列表</a>
            </div>
          </form>
        </section>
        """
        return self.page("列表字段设置 - 商品资料后台", content, user, current_page="products", back_href="/products")

    def status_note_label(self, user, product: dict, target_status: str) -> str:
        if target_status == "pending":
            return "交接说明（选填）"
        if target_status == "published":
            return "完成说明（选填）"
        if target_status == "received":
            return "接收说明（选填）"
        return "处理说明（选填）"

    def status_note_placeholder(self, user, product: dict, target_status: str) -> str:
        if target_status == "pending":
            if user.get("department") == "A" and (product.get("status") or "") in {"published", "received"}:
                return "例如：这条已完成资料的主体字段刚有调整，请商品部按当前版本重新补充并复核品类、图片、上新价格、上新渠道和资料完成。"
            return "例如：主体资料与合规信息已补齐，请商品部补充品类、图片、上新价格、上新渠道和资料完成。"
        if target_status == "published":
            return "例如：品类、图片、上新价格、上新渠道和资料完成已补完，可以开放给运营部读取。"
        if target_status == "received":
            return "例如：资料已核对接收。"
        return "例如：说明本次撤回或下线转草稿的原因。"

    def status_change_revision_flag(self, user, product: dict, target_status: str) -> int | None:
        if (
            target_status == "pending"
            and user.get("department") == "A"
            and (product.get("status") or "") in {"published", "received"}
            and int(product.get("revision_flag") or 0)
        ):
            return 1
        return None

    def status_change_details(self, user, product: dict, target_status: str, review_note: str = "") -> str:
        actor = department_label(user.get("department"))
        if target_status == "pending":
            if user.get("department") == "A":
                if (product.get("status") or "") in {"published", "received"}:
                    details = f"{actor} 将已完成或已接收后又修改过的资料重新提交给商品部补充品类、图片、上新价格、上新渠道和资料完成，并重新走一遍 A→B→C 流程。"
                else:
                    details = f"{actor} 完成主体字段填写并转交商品部补充品类、图片、上新价格、上新渠道和资料完成。"
                note_prefix = "交接说明"
            else:
                details = f"{actor} 将资料转回商品部补充阶段。"
                note_prefix = "处理说明"
        elif target_status == "published":
            if user.get("department") == "B":
                details = f"{actor} 已补齐品类、图片、上新价格、上新渠道和资料完成，并开放给运营部读取。"
                note_prefix = "完成说明"
            else:
                details = f"{actor} 将资料标记为已完成并开放给运营部。"
                note_prefix = "处理说明"
        elif target_status == "received":
            details = f"{actor} 已确认接收商品部完成并开放的资料。"
            note_prefix = "接收说明"
        else:
            details = f"{actor} 将资料退回跟单部主体填写阶段。"
            note_prefix = "处理说明"
        if review_note:
            return f"{details} {note_prefix}：{review_note}"
        return details

    def lifecycle_change_details(self, user, target_status: str) -> str:
        actor = department_label(user.get("department"))
        if target_status == "archived":
            return f"{actor} 将资料归档。"
        if target_status == "deleted":
            return f"{actor} 将资料标记为已删除。"
        if target_status == "active":
            return f"{actor} 恢复资料为正常状态。"
        return f"{actor} 更新了资料生命周期。"

    def display_value(self, value) -> str:
        if value in (None, ""):
            return '<span class="meta">未填写</span>'
        return html.escape(str(value))
