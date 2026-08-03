from __future__ import annotations

import cgi
import html
import json
import secrets
from http import cookies
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from catalog_backend import db
from catalog_backend.excel import parse_workbook, workbook_bytes
from catalog_backend.fields import FIELDS_BY_GROUP, PRODUCT_FIELDS, PRODUCT_FIELD_MAP
from catalog_backend.policies import (
    available_lifecycle_actions,
    available_status_actions,
    can_create_product,
    can_edit_product,
    can_manage_users,
    can_manage_lifecycle,
    can_review_product,
    can_see_product,
    can_view_logs,
    department_label,
    is_admin,
    lifecycle_label,
    MANAGEABLE_DEPARTMENTS,
    status_label,
    visible_fields_for_department,
    visible_fields_from_keys,
)
from catalog_backend.uploads import (
    MEDIA_URL_PREFIX,
    delete_local_media,
    media_content_type,
    media_file_path,
    read_validated_image_upload,
    read_validated_image_uploads,
    save_image_upload,
)


SESSIONS: dict[str, int] = {}


class CatalogApplication:
    def __init__(self, db_path: str | Path, upload_dir: str | Path):
        self.db_path = str(db_path)
        self.upload_dir = str(upload_dir)

    def __call__(self, environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")
        query = {
            key: values[0]
            for key, values in parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True).items()
        }
        user = self.current_user(environ)
        try:
            if path == "/":
                return self.redirect(start_response, "/products" if user else "/login")
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
            if path == "/products" and method == "GET":
                return self.html_response(
                    start_response,
                    self.render_products(user, query),
                )
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
                    return self.require_editor(start_response, user) or self.html_response(
                        start_response,
                        self.render_import_page(user),
                    )
                return self.require_editor(start_response, user) or self.handle_import(
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
            if path.startswith(MEDIA_URL_PREFIX) and method == "GET":
                return self.handle_media(start_response, path)

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
        form_data = {
            key: values[0]
            for key, values in parse_qs(raw_body, keep_blank_values=True).items()
        }
        return form_data, {}

    def require_editor(self, start_response, user):
        if can_create_product(user):
            return None
        return self.html_response(
            start_response,
            self.render_message_page("权限不足", "当前账号只有查看和调用权限，不能录入或修改资料。", user),
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
            ("Location", "/profile/password" if user.get("must_change_password") else "/products"),
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
        errors = self.validate_product_form(form)
        if errors:
            return self.html_response(
                start_response,
                self.render_product_form(user, "/products/new", "新建商品资料", form, errors),
                status="400 Bad Request",
            )
        with db.get_connection(self.db_path) as connection:
            product_id = db.create_product(connection, form, user["id"], user["department"])
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
                self.render_message_page("不可查看", "当前账号只能查看已发布给本角色开放的资料。", user),
                status="403 Forbidden",
            )
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

    def handle_export(self, start_response, user, query):
        products = [
            self.product_payload_for_user(product, user)
            for product in db.list_products(
                self.db_path,
                query=query.get("q", ""),
                department=query.get("department", ""),
                status=query.get("status", ""),
            )
            if can_see_product(user, product)
        ]
        visible_fields = self.visible_fields_for_user(user)
        body = workbook_bytes(products, visible_fields)
        headers = [
            ("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            ("Content-Disposition", 'attachment; filename="catalog-export.xlsx"'),
            ("Content-Length", str(len(body))),
        ]
        start_response("200 OK", headers)
        return [body]

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
        products = [
            self.product_payload_for_user(product, api_user)
            for product in db.list_products(
                self.db_path,
                query=query.get("q", ""),
                department=query.get("department", ""),
                status=query.get("status", ""),
            )
            if can_see_product(api_user, product)
        ]
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
        form, _ = self.parse_form(environ)
        target_status = form.get("status", "").strip()
        review_note = " ".join(str(form.get("review_note", "")).split())
        allowed_actions = dict(available_status_actions(user, product))
        if target_status not in allowed_actions:
            return self.html_response(
                start_response,
                self.render_message_page("不可操作", "当前账号不能执行这个状态变更。", user),
                status="403 Forbidden",
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
                self.render_message_page("不可操作", "当前账号不能执行这个生命周期操作。", user),
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
        return self.redirect(
            start_response,
            f"/products/{product_id}?notice=" + self.urlencode_message(f"生命周期已更新为{lifecycle_label(target_status)}。"),
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
        if not is_admin(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "只有审核管理员可以执行批量操作。", user),
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
        with db.get_connection(self.db_path) as connection:
            for product_id in product_ids:
                product = db.get_product(self.db_path, product_id)
                if not product:
                    skipped += 1
                    continue
                if action == "publish_selected":
                    allowed = dict(available_status_actions(user, product))
                    if "published" not in allowed:
                        skipped += 1
                        continue
                    db.change_product_status(
                        connection,
                        product_id,
                        "published",
                        user["id"],
                        allowed["published"],
                        "审核管理员批量审核发布资料。",
                    )
                    updated += 1
                    continue
                if action == "archive_selected":
                    allowed = dict(available_lifecycle_actions(user, product))
                    if "archived" not in allowed:
                        skipped += 1
                        continue
                    db.change_product_lifecycle(
                        connection,
                        product_id,
                        "archived",
                        user["id"],
                        allowed["archived"],
                        "审核管理员批量归档资料。",
                    )
                    updated += 1
                    continue
                skipped += 1
        if action == "publish_selected":
            action_label = "批量发布"
        elif action == "archive_selected":
            action_label = "批量归档"
        else:
            action_label = "批量操作"
        notice = f"{action_label}完成：成功 {updated} 条，跳过 {skipped} 条。"
        return self.redirect(
            start_response,
            "/products?notice=" + self.urlencode_message(notice),
        )

    def handle_review_bulk(self, environ, start_response, user):
        if not is_admin(user):
            return self.html_response(
                start_response,
                self.render_message_page("权限不足", "只有审核管理员可以执行批量审核。", user),
                status="403 Forbidden",
            )
        form, _ = self.parse_form(environ)
        product_ids = self.collect_numeric_values(form, "product_ids")
        if not product_ids:
            return self.redirect(
                start_response,
                "/products/review?notice=" + self.urlencode_message("请先勾选至少一条待审核资料。"),
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
                db.change_product_status(
                    connection,
                    product_id,
                    "published",
                    user["id"],
                    allowed["published"],
                    "审核管理员在审核队列中批量审核发布资料。",
                )
                updated += 1
        notice = f"批量审核发布完成：成功 {updated} 条，跳过 {skipped} 条。"
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

    def handle_media(self, start_response, path: str):
        file_path = media_file_path(self.upload_dir, path)
        if not file_path.exists():
            return self.html_response(
                start_response,
                self.render_message_page("文件不存在", "没有找到这张图片。"),
                status="404 Not Found",
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

    def handle_c_field_settings_update(self, environ, start_response, user):
        denied = self.require_admin(start_response, user)
        if denied:
            return denied
        form, _ = self.parse_form(environ)
        template_name = str(form.get("template_name", "")).strip()
        template_rule_department = str(form.get("template_rule_department", "")).strip()
        template_rule_brand = str(form.get("template_rule_brand", "")).strip()
        template_rule_category = str(form.get("template_rule_category", "")).strip()
        template_rule_style = str(form.get("template_rule_style", "")).strip()
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
                    errors=["至少需要开放一个字段给 C 部门。"],
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
            template_rules = self.build_template_rules(
                template_rule_department,
                template_rule_brand,
                template_rule_category,
                template_rule_style,
            )
            templates[template_name] = {
                "field_keys": selected_keys,
                "rules": template_rules,
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
                f"保存了字段模板，共 {len(selected_keys)} 个字段。自动规则：{json.dumps(template_rules, ensure_ascii=False) if template_rules else '无'}。",
            )
            rule_notice = "，已设置自动命中规则" if template_rules else ""
            return self.redirect(
                start_response,
                "/settings/c-fields?notice=" + self.urlencode_message(f"已保存字段模板：{template_name}{rule_notice}，并同步更新当前开放字段。"),
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
        for numeric_field in ("tag_price", "launch_price", "size_f", "size_s", "size_m", "size_l", "size_xl", "size_2xl", "size_3xl", "total_quantity"):
            value = str(form.get(numeric_field, "")).strip()
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

    def validate_user_form(self, form, creating: bool):
        errors = []
        if creating and not form.get("username", "").strip():
            errors.append("用户名不能为空。")
        if not form.get("display_name", "").strip():
            errors.append("显示名称不能为空。")
        if form.get("department", "").strip() not in MANAGEABLE_DEPARTMENTS:
            errors.append("角色或部门不合法。")
        if creating:
            password = form.get("password", "")
            if len(password) < 6:
                errors.append("初始密码至少需要 6 位。")
        return errors

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

    def configured_c_field_keys(self) -> list[str]:
        raw_value = db.get_setting(self.db_path, "c_visible_field_keys") or ""
        keys = self.normalize_field_keys(raw_value.split(","))
        if not keys:
            return [field.key for field in visible_fields_for_department("C")]
        return keys

    def build_template_rules(
        self,
        owner_department: str,
        brand_name: str,
        category: str,
        style_keyword: str,
    ) -> dict[str, str]:
        rules = {}
        if owner_department in {"A", "B"}:
            rules["owner_department"] = owner_department
        if brand_name:
            rules["brand_name"] = brand_name
        if category:
            rules["category"] = category
        if style_keyword:
            rules["style_keyword"] = style_keyword
        return rules

    def normalize_field_keys(self, values) -> list[str]:
        valid_keys = {field.key for field in PRODUCT_FIELDS}
        normalized = []
        seen = set()
        for value in values:
            key = str(value).strip()
            if not key or key not in valid_keys or key in seen:
                continue
            normalized.append(key)
            seen.add(key)
        return normalized

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
                raw_rules = raw_value.get("rules", {})
                if isinstance(raw_rules, dict):
                    rules = self.build_template_rules(
                        str(raw_rules.get("owner_department", "")).strip(),
                        str(raw_rules.get("brand_name", "")).strip(),
                        str(raw_rules.get("category", "")).strip(),
                        str(raw_rules.get("style_keyword", "")).strip(),
                    )
            if not name:
                continue
            if normalized_keys:
                templates[name] = {
                    "field_keys": normalized_keys,
                    "rules": rules,
                }
        return templates

    def save_c_field_templates(self, templates: dict[str, dict]) -> None:
        cleaned_templates = {}
        for raw_name, raw_template in templates.items():
            name = str(raw_name).strip()
            if isinstance(raw_template, dict):
                normalized_keys = self.normalize_field_keys(raw_template.get("field_keys", []))
                rules = self.build_template_rules(
                    str(raw_template.get("rules", {}).get("owner_department", "")).strip(),
                    str(raw_template.get("rules", {}).get("brand_name", "")).strip(),
                    str(raw_template.get("rules", {}).get("category", "")).strip(),
                    str(raw_template.get("rules", {}).get("style_keyword", "")).strip(),
                )
            else:
                normalized_keys = self.normalize_field_keys(raw_template)
                rules = {}
            if not name or not normalized_keys:
                continue
            cleaned_templates[name] = {
                "field_keys": normalized_keys,
                "rules": rules,
            }
        db.set_setting(
            self.db_path,
            "c_field_templates_json",
            json.dumps(cleaned_templates, ensure_ascii=False),
        )

    def matching_c_template(self, product: dict) -> tuple[str, dict] | None:
        for name, template in self.c_field_templates().items():
            if self.template_matches_product(template.get("rules", {}), product):
                return name, template
        return None

    def template_matches_product(self, rules: dict, product: dict) -> bool:
        if not rules:
            return False
        if rules.get("owner_department") and str(product.get("owner_department") or "").strip() != rules["owner_department"]:
            return False
        if rules.get("brand_name") and str(product.get("brand_name") or "").strip() != rules["brand_name"]:
            return False
        if rules.get("category") and str(product.get("category") or "").strip() != rules["category"]:
            return False
        if rules.get("style_keyword"):
            style_text = " ".join(
                str(product.get(key) or "").strip().lower()
                for key in ("style_code", "style_color", "product_name")
            )
            if rules["style_keyword"].lower() not in style_text:
                return False
        return True

    def visible_fields_for_c_product(self, product: dict):
        matched = self.matching_c_template(product)
        if matched:
            return visible_fields_from_keys(matched[1]["field_keys"])
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

    def product_payload_for_user(self, product: dict, user: dict) -> dict:
        visible_fields = self.visible_fields_for_c_product(product) if user.get("department") == "C" else self.visible_fields_for_user(user)
        payload = {
            "id": product.get("id"),
            "owner_department": product.get("owner_department"),
            "creator_name": product.get("creator_name"),
            "creator_username": product.get("creator_username"),
            "created_at": product.get("created_at"),
            "updated_at": product.get("updated_at"),
            "status": product.get("status"),
            "status_label": status_label(product.get("status")),
            "last_reviewed_at": product.get("last_reviewed_at"),
            "reviewer_name": product.get("reviewer_name"),
            "image_gallery": self.image_gallery_values(product),
        }
        for field in visible_fields:
            payload[field.key] = product.get(field.key)
        return payload

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

    def nav(self, user):
        action_links = [
            '<a href="/products">资料列表</a>',
            '<a href="/profile/password">修改密码</a>',
            '<a href="/api/products">JSON 调用</a>',
            '<a href="/export.xlsx">导出 Excel</a>',
        ]
        if can_view_logs(user):
            action_links.insert(1, '<a href="/logs">日志中心</a>')
        if can_create_product(user):
            action_links.insert(2, '<a href="/products/new">新建资料</a>')
            action_links.insert(3, '<a href="/import">导入 Excel</a>')
        if is_admin(user):
            action_links.insert(1, '<a href="/products/review">审核队列</a>')
            action_links.insert(2, '<a href="/users">账号管理</a>')
            action_links.insert(3, '<a href="/settings/c-fields">字段开放</a>')
        links = "".join(f"<li>{item}</li>" for item in action_links)
        return f"""
        <nav class="nav-shell">
          <div>
            <div class="brand">商品资料后台</div>
            <div class="meta">{html.escape(user['display_name'])} · {department_label(user['department'])}</div>
          </div>
          <ul class="nav-links">{links}</ul>
          <form method="post" action="/logout"><button class="ghost-button" type="submit">退出登录</button></form>
        </nav>
        """

    def page(self, title: str, content: str, user=None):
        nav = self.nav(user) if user else ""
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #f3efe7;
      --paper: rgba(255, 252, 246, 0.92);
      --ink: #2a241d;
      --muted: #6c6257;
      --line: rgba(85, 65, 44, 0.15);
      --accent: #8c5a2b;
      --accent-strong: #5b3a1d;
      --success: #325d48;
      --warning: #996b2b;
      --shadow: 0 24px 60px rgba(68, 50, 31, 0.12);
      --radius: 22px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(219, 188, 144, 0.35), transparent 28%),
        radial-gradient(circle at bottom right, rgba(160, 123, 83, 0.18), transparent 24%),
        linear-gradient(180deg, #f8f5ef 0%, var(--bg) 100%);
      font-family: "Noto Sans SC", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
      min-height: 100vh;
    }}
    a {{ color: var(--accent-strong); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .shell {{ max-width: 1380px; margin: 0 auto; padding: 28px 20px 52px; }}
    .nav-shell {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 22px 24px;
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: calc(var(--radius) + 6px);
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
      margin-bottom: 24px;
    }}
    .brand {{
      font-size: 26px;
      font-weight: 800;
      letter-spacing: 0.04em;
    }}
    .meta {{
      color: var(--muted);
      margin-top: 4px;
      font-size: 14px;
    }}
    .nav-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      list-style: none;
      margin: 0;
      padding: 0;
    }}
    .nav-links li {{
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(140, 90, 43, 0.08);
      border: 1px solid rgba(140, 90, 43, 0.14);
    }}
    .panel {{
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding: 24px;
      backdrop-filter: blur(10px);
    }}
    .hero {{
      display: grid;
      grid-template-columns: 1.3fr 0.7fr;
      gap: 18px;
      margin-bottom: 20px;
    }}
    .hero h1, .panel h1, .panel h2, .panel h3 {{ margin-top: 0; }}
    .eyebrow {{
      color: var(--accent);
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 14px;
    }}
    .stat-card {{
      padding: 18px;
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(255,255,255,0.78), rgba(255,248,239,0.92));
      border: 1px solid var(--line);
    }}
    .stat-card strong {{
      display: block;
      font-size: 28px;
      margin-top: 10px;
    }}
    .tools {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 18px;
    }}
    .tools form {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      width: 100%;
    }}
    input, select, textarea, button {{
      width: 100%;
      font: inherit;
      border-radius: 14px;
      border: 1px solid rgba(91, 58, 29, 0.16);
      padding: 12px 14px;
      background: rgba(255, 255, 255, 0.9);
      color: var(--ink);
    }}
    input:focus, select:focus, textarea:focus {{
      outline: 2px solid rgba(140, 90, 43, 0.18);
      border-color: rgba(140, 90, 43, 0.46);
    }}
    textarea {{ min-height: 112px; resize: vertical; }}
    button {{
      background: linear-gradient(135deg, var(--accent) 0%, var(--accent-strong) 100%);
      color: white;
      font-weight: 700;
      cursor: pointer;
      transition: transform 160ms ease, box-shadow 160ms ease;
      box-shadow: 0 14px 24px rgba(91, 58, 29, 0.16);
    }}
    button:hover {{ transform: translateY(-1px); }}
    .ghost-button {{
      background: transparent;
      color: var(--ink);
      box-shadow: none;
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
      border-radius: 18px;
      border: 1px solid var(--line);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      min-width: 980px;
      background: rgba(255,255,255,0.8);
    }}
    th, td {{
      padding: 14px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{
      position: sticky;
      top: 0;
      background: rgba(246, 240, 232, 0.96);
      font-size: 13px;
      letter-spacing: 0.03em;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(140, 90, 43, 0.09);
      color: var(--accent-strong);
      font-size: 13px;
      font-weight: 700;
    }}
    .notice {{
      padding: 14px 16px;
      border-radius: 16px;
      background: rgba(50, 93, 72, 0.12);
      color: var(--success);
      margin-bottom: 18px;
      border: 1px solid rgba(50, 93, 72, 0.18);
    }}
    .warning {{
      padding: 14px 16px;
      border-radius: 16px;
      background: rgba(153, 107, 43, 0.1);
      color: var(--warning);
      margin-bottom: 18px;
      border: 1px solid rgba(153, 107, 43, 0.18);
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
    .detail-card {{
      padding: 18px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,0.72);
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
      max-width: 980px;
      margin: 0 auto;
      padding: 48px 20px;
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 20px;
      align-items: stretch;
    }}
    .login-showcase {{
      padding: 28px;
      border-radius: 28px;
      color: #fff6ed;
      background:
        radial-gradient(circle at top right, rgba(255,255,255,0.18), transparent 28%),
        linear-gradient(135deg, #7a4b24 0%, #2f231b 100%);
      box-shadow: var(--shadow);
    }}
    .credential {{
      margin-top: 16px;
      padding: 16px;
      border-radius: 18px;
      background: rgba(255,255,255,0.08);
      border: 1px solid rgba(255,255,255,0.14);
    }}
    @media (max-width: 900px) {{
      .hero, .login-shell {{ grid-template-columns: 1fr; }}
      .nav-shell {{ align-items: flex-start; flex-direction: column; }}
      .nav-links {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    {nav}
    {content}
  </div>
  <script>
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

    def render_login(self, error: str = "") -> str:
        error_block = f'<div class="warning">{html.escape(error)}</div>' if error else ""
        content = f"""
        <div class="login-shell">
          <section class="login-showcase">
            <div class="eyebrow">Multi-Dept Workflow</div>
            <h1>把商品资料从共享表格升级成权限清晰的后台</h1>
            <p>这套原型围绕 A、B、C 三个部门设计：A/B 录入并只允许修改自己填写的资料，C 只能查看和调用被开放的字段。</p>
            <div class="credential">
              <strong>默认演示账号</strong>
              <p>A 部门：<code>a_editor / demo123</code><br>B 部门：<code>b_editor / demo123</code><br>C 部门：<code>c_viewer / demo123</code><br>审核管理员：<code>admin_reviewer / demo123</code></p>
            </div>
            <div class="credential">
              <strong>这版原型已包含</strong>
              <p>Excel 模板导入导出、字段级可见性、只读 JSON 调用、录入归属人控制、状态审核和操作日志。</p>
            </div>
          </section>
          <section class="panel">
            <div class="eyebrow">Sign In</div>
            <h2>登录商品资料后台</h2>
            <p class="meta">先用默认演示账号体验流程，后续可以再替换成正式账号体系。连续登录失败过多会触发临时锁定。</p>
            {error_block}
            <form method="post" action="/login">
              <div class="form-grid">
                <label class="field">
                  <span>用户名</span>
                  <input name="username" placeholder="例如 a_editor">
                </label>
                <label class="field">
                  <span>密码</span>
                  <input type="password" name="password" placeholder="请输入密码">
                </label>
              </div>
              <div style="margin-top:16px;">
                <button type="submit">进入后台</button>
              </div>
            </form>
          </section>
        </div>
        """
        return self.page("登录 - 商品资料后台", content)

    def render_products(self, user, query) -> str:
        keyword = query.get("q", "").strip()
        department_filter = query.get("department", "").strip()
        status_filter = query.get("status", "").strip()
        lifecycle_filter = query.get("lifecycle_status", "").strip()
        bulk_enabled = is_admin(user)
        products = [
            product
            for product in db.list_products(self.db_path, keyword, department_filter, status_filter, lifecycle_filter)
            if can_see_product(user, product)
        ]
        stats = db.department_stats(self.db_path)
        workflow_stats = db.status_stats(self.db_path)
        lifecycle_stats = db.lifecycle_stats(self.db_path)
        recent_stats = db.recent_activity_stats(self.db_path, days=7)
        recent_department_stats = db.recent_department_created_stats(self.db_path, days=7)
        notice = query.get("notice", "")
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        c_note = ""
        if user["department"] == "C":
            c_note = (
                '<div class="warning">当前账号为 C 部门：只能查看已发布且已开放的字段，页面、Excel 导出和 JSON 接口都不会返回隐藏内容，也没有编辑权限。</div>'
            )
        rows = []
        for product in products:
            payload = self.product_payload_for_user(product, user)
            visible_field_keys = set(payload.keys())
            actions = [f'<a href="/products/{product["id"]}">查看</a>']
            if can_edit_product(user, product):
                actions.append(f'<a href="/products/{product["id"]}/edit">编辑</a>')
            if can_view_logs(user):
                actions.append(f'<a href="/products/{product["id"]}/logs">日志</a>')
            selector_cell = ""
            if bulk_enabled:
                selector_cell = (
                    f'<td><input type="checkbox" name="product_ids" value="{product["id"]}" style="width:auto;"></td>'
                )
            dynamic_cells = []
            for field_key in ("brand_name", "category"):
                if field_key in visible_field_keys:
                    dynamic_cells.append(f"<td>{self.display_value(payload.get(field_key))}</td>")
            if "launch_price" in visible_field_keys:
                dynamic_cells.append(f"<td>{self.display_value(payload.get('launch_price'))}</td>")
            rows.append(
                f"""
                <tr>
                  {selector_cell}
                  <td><a href="/products/{product['id']}">#{product['id']}</a></td>
                  <td>{self.display_value(payload.get('product_name'))}</td>
                  <td>{self.display_value(payload.get('style_code'))}</td>
                  {"".join(dynamic_cells)}
                  <td><span class="pill">{html.escape(payload.get('status_label', ''))}</span></td>
                  <td><span class="pill">{html.escape(lifecycle_label(product.get('lifecycle_status')))}</span></td>
                  <td>{html.escape(department_label(product.get('owner_department')))}</td>
                  <td>{self.display_value(product.get('creator_name'))}</td>
                  <td>{self.display_value(product.get('updated_at'))}</td>
                  <td>{" / ".join(actions)}</td>
                </tr>
                """
            )
        dynamic_headers = []
        for field_key, title in (("brand_name", "品牌"), ("category", "品类"), ("launch_price", "上新价格")):
            if field_key in visible_field_keys:
                dynamic_headers.append(f"<th>{title}</th>")
        selector_header = '<th>勾选</th>' if bulk_enabled else ""
        new_button = (
            '<a class="pill" href="/products/new">新建资料</a>'
            if can_create_product(user)
            else ""
        )
        import_button = (
            '<a class="pill" href="/import">导入 Excel</a>'
            if can_create_product(user)
            else ""
        )
        if user["department"] == "C":
            stats_markup = f"""
            <div class="stats">
              <div class="stat-card"><span>可查看资料</span><strong>{len(products)}</strong></div>
              <div class="stat-card"><span>可见部门</span><strong>{len({product.get('owner_department') for product in products})}</strong></div>
              <div class="stat-card"><span>调用权限</span><strong>JSON</strong></div>
            </div>
            """
        else:
            stats_markup = f"""
            <div class="stats">
              <div class="stat-card"><span>A 部门资料</span><strong>{stats.get('A', 0)}</strong></div>
              <div class="stat-card"><span>B 部门资料</span><strong>{stats.get('B', 0)}</strong></div>
              <div class="stat-card"><span>待审核</span><strong>{workflow_stats.get('pending', 0)}</strong></div>
              <div class="stat-card"><span>已发布</span><strong>{workflow_stats.get('published', 0)}</strong></div>
              <div class="stat-card"><span>已归档</span><strong>{lifecycle_stats.get('archived', 0)}</strong></div>
              <div class="stat-card"><span>已删除</span><strong>{lifecycle_stats.get('deleted', 0)}</strong></div>
              <div class="stat-card"><span>总资料数</span><strong>{sum(stats.values())}</strong></div>
              <div class="stat-card"><span>近 7 天新增</span><strong>{recent_stats.get('recent_created', 0)}</strong></div>
              <div class="stat-card"><span>近 7 天操作</span><strong>{recent_stats.get('recent_logs', 0)}</strong></div>
              <div class="stat-card"><span>活跃待审核</span><strong>{recent_stats.get('pending_active', 0)}</strong></div>
            </div>
            """
        operations_panel = ""
        if user["department"] != "C":
            operations_panel = f"""
            <section class="panel" style="margin-bottom:18px;">
              <div class="eyebrow">Weekly Pulse</div>
              <h2>近 7 天运营概览</h2>
              <div class="stats">
                <div class="stat-card"><span>A 部门近 7 天新增</span><strong>{recent_department_stats.get('A', 0)}</strong></div>
                <div class="stat-card"><span>B 部门近 7 天新增</span><strong>{recent_department_stats.get('B', 0)}</strong></div>
                <div class="stat-card"><span>近 7 天日志总量</span><strong>{recent_stats.get('recent_logs', 0)}</strong></div>
                <div class="stat-card"><span>当前待审核资料</span><strong>{recent_stats.get('pending_active', 0)}</strong></div>
              </div>
            </section>
            """
        status_filter_markup = (
            ""
            if user["department"] == "C"
            else f"""
              <select name="status">
                <option value="">全部状态</option>
                <option value="draft" {"selected" if status_filter == "draft" else ""}>草稿</option>
                <option value="pending" {"selected" if status_filter == "pending" else ""}>待审核</option>
                <option value="published" {"selected" if status_filter == "published" else ""}>已发布</option>
              </select>
              <select name="lifecycle_status">
                <option value="">全部生命周期</option>
                <option value="active" {"selected" if lifecycle_filter == "active" else ""}>正常</option>
                <option value="archived" {"selected" if lifecycle_filter == "archived" else ""}>已归档</option>
                <option value="deleted" {"selected" if lifecycle_filter == "deleted" else ""}>已删除</option>
              </select>
            """
        )
        content = f"""
        <section class="hero">
          <div class="panel">
            <div class="eyebrow">Catalog Control</div>
            <h1>商品资料统一管理台</h1>
            <p>用一套资料底库承接 Excel 模板、多人协作、权限隔离和后续系统调用。A/B 可以录入并只修改自己提交的内容，C 仅可读取开放字段。</p>
            <div class="tools">
              {new_button}
              {import_button}
              <a class="pill" href="/export.xlsx">导出 Excel</a>
              <a class="pill" href="/api/products">查看 JSON</a>
              {'<a class="pill" href="/products/review">待审核队列</a>' if is_admin(user) else ''}
            </div>
          </div>
          <div class="panel">
            {stats_markup}
          </div>
        </section>
        <section class="panel">
          {notice_block}
          {c_note}
          {operations_panel}
          <div class="tools">
            <form method="get" action="/products">
              <input name="q" value="{html.escape(keyword)}" placeholder="按商品名称、款号、品牌搜索">
              <select name="department">
                <option value="">全部部门</option>
                <option value="A" {"selected" if department_filter == "A" else ""}>A 部门</option>
                <option value="B" {"selected" if department_filter == "B" else ""}>B 部门</option>
              </select>
              {status_filter_markup}
              <button type="submit">筛选资料</button>
            </form>
          </div>
          {self.render_bulk_tools(user)}
          <form method="post" action="/products/bulk">
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  {selector_header}
                  <th>ID</th>
                  <th>商品名称</th>
                  <th>款号</th>
                  {"".join(dynamic_headers)}
                  <th>状态</th>
                  <th>生命周期</th>
                  <th>归属部门</th>
                  <th>录入人</th>
                  <th>最后更新</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows) if rows else f'<tr><td colspan="{"12" if bulk_enabled else "11"}">暂无符合条件的商品资料。</td></tr>'}
              </tbody>
            </table>
          </div>
          </form>
        </section>
        """
        return self.page("资料列表 - 商品资料后台", content, user)

    def render_bulk_tools(self, user) -> str:
        if not is_admin(user):
            return ""
        return """
          <section class="panel" style="margin-bottom:18px;">
            <h2>批量操作</h2>
            <p class="meta">仅管理员可用。勾选列表中的资料后，可批量审核发布待审核资料，或批量归档当前可归档资料。</p>
            <div class="tools" style="margin-bottom:0;">
              <button type="submit" name="bulk_action" value="publish_selected" formmethod="post" formaction="/products/bulk">批量发布</button>
              <button type="submit" name="bulk_action" value="archive_selected" formmethod="post" formaction="/products/bulk">批量归档</button>
            </div>
          </section>
        """

    def render_product_form(self, user, action: str, title: str, values: dict, errors: list[str] | None = None) -> str:
        error_block = ""
        if errors:
            items = "".join(f"<li>{html.escape(error)}</li>" for error in errors)
            error_block = f'<ul class="error-list">{items}</ul>'
        sections = []
        for group, fields in FIELDS_BY_GROUP.items():
            inputs = "".join(self.render_input(field, values) for field in fields)
            sections.append(f'<section class="panel"><h2>{html.escape(group)}</h2><div class="form-grid">{inputs}</div></section>')
        content = f"""
        <section class="panel" style="margin-bottom:18px;">
          <div class="eyebrow">Editor</div>
          <h1>{html.escape(title)}</h1>
          <p class="meta">资料将自动归属到 {html.escape(department_label(user['department']))}，并绑定当前录入账号作为唯一可编辑人。新建或修改后的资料默认处于草稿状态，可再提交审核。</p>
          {error_block}
          <form method="post" action="{html.escape(action)}" enctype="multipart/form-data">
            {''.join(sections)}
            <section class="panel" style="margin-top:18px;">
              <button type="submit">保存资料</button>
            </section>
          </form>
        </section>
        """
        return self.page(f"{title} - 商品资料后台", content, user)

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
        status_cards = []
        for status_value, action_label in available_status_actions(user, product):
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
              <h2>状态操作</h2>
              <p class="meta">适合给 A/B 的提交说明、以及管理员的审核意见留痕。</p>
              <div class="detail-grid">
                {''.join(status_cards)}
              </div>
            </section>
            """
        lifecycle_forms = "".join(
            f"""
            <form method="post" action="/products/{product['id']}/lifecycle" style="display:inline-flex; gap:10px;">
              <input type="hidden" name="lifecycle_status" value="{html.escape(status_value)}">
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
              <h2>生命周期操作</h2>
              <div class="tools" style="margin-bottom:0;">
                {lifecycle_forms}
              </div>
            </section>
            """
        hidden_note = (
            '<div class="warning">你当前看到的是 C 部门可访问字段，隐藏字段不会展示在详情页、导出和 API 中。</div>'
            if user["department"] == "C"
            else ""
        )
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        reviewer_name = html.escape(str(product.get("reviewer_name") or "未审核"))
        workflow_notice = (
            f"状态流转：草稿 -> 待审核 -> 已发布 · 最近审核人 {reviewer_name}"
            if user["department"] != "C"
            else "当前展示的是已发布版本。"
        )
        quick_tools_block = self.render_product_quick_tools(product, payload_json, copy_summary)
        content = f"""
        <section class="panel">
          <div class="eyebrow">Record Detail</div>
          <h1>{self.display_value(product.get('product_name'))}</h1>
          <p class="meta">
            资料编号 #{product['id']} · 归属 {html.escape(department_label(product.get('owner_department')))} ·
            录入人 {self.display_value(product.get('creator_name'))} · 当前状态 {html.escape(status_label(product.get('status')))} · 生命周期 {html.escape(lifecycle_label(product.get('lifecycle_status')))}
          </p>
          <div class="notice">
            {workflow_notice}
          </div>
          <div class="tools">
            <a class="pill" href="/products">返回列表</a>
            {edit_link}
            {log_link}
          </div>
          {notice_block}
          {hidden_note}
          {quick_tools_block}
          {status_block}
          {lifecycle_block}
          <div class="detail-grid">
            {''.join(group_html)}
          </div>
        </section>
        """
        return self.page(f"资料 #{product['id']} - 商品资料后台", content, user)

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
            f"归属部门: {department_label(product.get('owner_department'))}",
            f"状态: {status_label(product.get('status'))}",
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
        report_block = f'<div class="notice">{html.escape(report)}</div>' if report else ""
        error_block = f'<div class="warning">{html.escape(error)}</div>' if error else ""
        content = f"""
        <section class="panel">
          <div class="eyebrow">Excel Bridge</div>
          <h1>从参考模板导入 Excel</h1>
          <p>导入时会读取第一张工作表，并按模板第一行表头识别字段。若发现与你本人已录入的同款号、同颜色、同商品名记录，则更新；否则新增。</p>
          {report_block}
          {error_block}
          <form method="post" action="/import" enctype="multipart/form-data">
            <div class="form-grid">
              <label class="field field-wide">
                <span>选择 Excel 文件</span>
                <input type="file" name="workbook" accept=".xlsx">
              </label>
            </div>
            <div style="margin-top:16px;">
              <button type="submit">开始导入</button>
            </div>
          </form>
        </section>
        """
        return self.page("导入 Excel - 商品资料后台", content, user)

    def render_password_change_page(self, user, errors: list[str] | None = None) -> str:
        error_block = ""
        if errors:
            items = "".join(f"<li>{html.escape(error)}</li>" for error in errors)
            error_block = f'<ul class="error-list">{items}</ul>'
        force_notice = ""
        if user.get("must_change_password"):
            force_notice = '<div class="warning">当前账号被设置为首次登录或重置后必须修改密码，完成后才能继续使用后台。</div>'
        content = f"""
        <section class="panel" style="max-width:720px; margin:0 auto;">
          <div class="eyebrow">Security</div>
          <h1>修改登录密码</h1>
          <p class="meta">建议使用不少于 6 位的新密码。修改完成后会立即生效。</p>
          {force_notice}
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
        return self.page("修改密码 - 商品资料后台", content, user)

    def render_message_page(self, title: str, message: str, user=None) -> str:
        content = f"""
        <section class="panel">
          <h1>{html.escape(title)}</h1>
          <p>{html.escape(message)}</p>
          <p><a href="/products">返回资料列表</a></p>
        </section>
        """
        return self.page(f"{title} - 商品资料后台", content, user)

    def render_users_page(self, user, notice: str = "", form_values: dict | None = None, errors: list[str] | None = None) -> str:
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
            rows.append(
                f"""
                <tr>
                  <td>{managed_user['id']}</td>
                  <td>{html.escape(managed_user.get('username') or '')}</td>
                  <td>{html.escape(managed_user.get('display_name') or '')}</td>
                  <td>{html.escape(department_label(managed_user.get('department')))}</td>
                  <td><span class="pill">{active_label}</span></td>
                  <td>{must_change_label}</td>
                  <td>{self.display_value(managed_user.get('created_at'))}</td>
                  <td>
                    <a href="/users/{managed_user['id']}/edit">编辑</a>
                    <form method="post" action="/users/{managed_user['id']}/toggle" style="display:inline-flex; gap:6px; margin-left:8px; flex-wrap:wrap;">
                      {'<input name="confirm_text" placeholder="输入 DISABLE">' if managed_user.get('is_active') else ''}
                      <button class="ghost-button" type="submit">{'停用' if managed_user.get('is_active') else '启用'}</button>
                    </form>
                    <form method="post" action="/users/{managed_user['id']}/reset-password" style="display:inline-flex; gap:6px; margin-left:8px;">
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
        content = f"""
        <section class="panel">
          <div class="eyebrow">User Admin</div>
          <h1>账号管理</h1>
          <p class="meta">管理员可以创建账号、分配角色、启用或停用账号，并重置登录密码。</p>
          {notice_block}
          {error_block}
        </section>
        <section class="panel" style="margin-top:18px;">
          <h2>新建账号</h2>
          <form method="post" action="/users">
            <div class="form-grid">
              <label class="field">
                <span>用户名</span>
                <input name="username" value="{html.escape(form_values.get('username', ''))}" placeholder="例如 buyer_a_01">
              </label>
              <label class="field">
                <span>显示名称</span>
                <input name="display_name" value="{html.escape(form_values.get('display_name', ''))}" placeholder="例如 A 部门资料员">
              </label>
              <label class="field">
                <span>角色/部门</span>
                <select name="department">{department_options}</select>
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
            </div>
            <div style="margin-top:16px;">
              <button type="submit">创建账号</button>
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
                  <th>显示名称</th>
                  <th>角色/部门</th>
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
        return self.page("账号管理 - 商品资料后台", content, user)

    def render_user_edit_page(self, user, managed_user: dict, errors: list[str] | None = None) -> str:
        error_block = ""
        if errors:
            items = "".join(f"<li>{html.escape(error)}</li>" for error in errors)
            error_block = f'<ul class="error-list">{items}</ul>'
        department_options = "".join(
            f'<option value="{code}" {"selected" if managed_user.get("department") == code else ""}>{department_label(code)}</option>'
            for code in MANAGEABLE_DEPARTMENTS
        )
        content = f"""
        <section class="panel">
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
            </div>
            <div class="tools" style="margin-top:16px;">
              <a class="pill" href="/users">返回账号管理</a>
              <button type="submit">保存账号资料</button>
            </div>
          </form>
        </section>
        """
        return self.page(f"编辑账号 - {managed_user.get('username', '')}", content, user)

    def render_review_queue(self, user, query) -> str:
        if not is_admin(user):
            return self.render_message_page("权限不足", "只有审核管理员可以查看审核队列。", user)
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
                  <td><a href="/products/{product['id']}">进入审核</a></td>
                </tr>
                """
            )
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        content = f"""
        <section class="panel">
          <div class="eyebrow">Review Queue</div>
          <h1>待审核资料</h1>
          <p class="meta">审核管理员可以在详情页填写审核意见后执行“审核发布”或“驳回为草稿”。当前列表按最近提交时间优先显示。</p>
          {notice_block}
          <form method="get" action="/products/review" class="panel" style="margin-bottom:18px;">
            <div class="form-grid">
              <label class="field">
                <span>关键词</span>
                <input name="q" value="{html.escape(keyword)}" placeholder="按商品名称、款号搜索待审核资料">
              </label>
              <label class="field">
                <span>归属部门</span>
                <select name="department">
                  <option value="">全部部门</option>
                  <option value="A" {"selected" if department_filter == "A" else ""}>A 部门</option>
                  <option value="B" {"selected" if department_filter == "B" else ""}>B 部门</option>
                </select>
              </label>
            </div>
            <div class="tools" style="margin-top:16px; margin-bottom:0;">
              <button type="submit">筛选待审核资料</button>
              <a class="pill" href="/products/review">清空筛选</a>
            </div>
          </form>
          <form method="post" action="/products/review/bulk">
            <section class="panel" style="margin-bottom:18px;">
              <h2>批量审核</h2>
              <p class="meta">勾选后可直接批量审核发布；不符合条件的资料会自动跳过。</p>
              <div class="tools" style="margin-bottom:0;">
                <button type="submit">批量审核发布</button>
              </div>
            </section>
            <div class="table-wrap">
              <table>
              <thead>
                <tr>
                  <th>勾选</th>
                  <th>ID</th>
                  <th>商品名称</th>
                  <th>款号</th>
                  <th>归属部门</th>
                  <th>录入人</th>
                  <th>提交时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows) if rows else '<tr><td colspan="8">当前没有待审核资料。</td></tr>'}
              </tbody>
              </table>
            </div>
          </form>
        </section>
        """
        return self.page("审核队列 - 商品资料后台", content, user)

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
                diff_rows = []
                for diff in item["diff_items"]:
                    diff_rows.append(
                        f"""
                        <div class="detail-row">
                          <span class="detail-label">{html.escape(diff.get('field_label', ''))}</span>
                          <div><strong>修改前：</strong>{self.display_value(diff.get('before'))}</div>
                          <div><strong>修改后：</strong>{self.display_value(diff.get('after'))}</div>
                        </div>
                        """
                    )
                diff_markup = f'<div style="margin-top:10px; padding:12px; border:1px solid rgba(91,58,29,0.12); border-radius:14px; background:rgba(255,255,255,0.68);">{"".join(diff_rows)}</div>'
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
          <form method="get" action="/products/{product['id']}/logs" class="panel" style="margin-bottom:18px;">
            <div class="form-grid">
              <label class="field">
                <span>按动作筛选</span>
                <input name="action" value="{html.escape(action_query)}" placeholder="例如 审核发布 / update / lifecycle">
              </label>
              <label class="field">
                <span>按操作人筛选</span>
                <input name="actor" value="{html.escape(actor_query)}" placeholder="例如 A 部门录入员 / ADMIN / 资料审核管理员">
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
        return self.page(f"日志 #{product['id']} - 商品资料后台", content, user)

    def render_logs_center(self, user, query: dict | None = None) -> str:
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
        <section class="panel">
          <div class="eyebrow">Audit Center</div>
          <h1>全局操作日志中心</h1>
          <p class="meta">这里汇总当前账号有权查看的全部商品操作日志；管理员还会看到账号、令牌、字段开放配置等管理审计动作，可按对象、动作、操作人筛选，并导出当前结果。</p>
          <div class="tools">
            <a class="pill" href="/products">返回资料列表</a>
            <a class="pill" href="/logs/export.csv?{html.escape(export_query)}">导出 CSV</a>
          </div>
          <form method="get" action="/logs" class="panel" style="margin-bottom:18px;">
            <div class="form-grid">
              <label class="field">
                <span>按商品筛选</span>
                <input name="product" value="{html.escape(product_query)}" placeholder="例如 商品名称 / 款号 / 资料ID">
              </label>
              <label class="field">
                <span>按动作筛选</span>
                <input name="action" value="{html.escape(action_query)}" placeholder="例如 审核发布 / update / lifecycle">
              </label>
              <label class="field">
                <span>按操作人筛选</span>
                <input name="actor" value="{html.escape(actor_query)}" placeholder="例如 A 部门录入员 / 审核管理员">
              </label>
            </div>
            <div class="tools" style="margin-top:16px; margin-bottom:0;">
              <button type="submit">筛选日志</button>
              <a class="pill" href="/logs">清空筛选</a>
            </div>
          </form>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>资料ID</th>
                  <th>商品名称</th>
                  <th>款号</th>
                  <th>资料归属部门</th>
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
        return self.page("日志中心 - 商品资料后台", content, user)

    def render_c_field_settings_page(
        self,
        user,
        notice: str = "",
        selected_keys: list[str] | None = None,
        errors: list[str] | None = None,
        template_name: str = "",
        template_rule_department: str = "",
        template_rule_brand: str = "",
        template_rule_category: str = "",
        template_rule_style: str = "",
    ) -> str:
        selected = set(selected_keys or self.configured_c_field_keys())
        api_token = self.configured_c_api_token()
        templates = self.c_field_templates()
        notice_block = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
        error_block = ""
        if errors:
            items = "".join(f"<li>{html.escape(error)}</li>" for error in errors)
            error_block = f'<ul class="error-list">{items}</ul>'
        matching_templates = [
            name for name, template in templates.items()
            if set(template["field_keys"]) == selected
        ]
        if matching_templates:
            matching_text = "当前开放组合匹配模板：" + "、".join(matching_templates)
        else:
            matching_text = "当前开放组合尚未绑定到已保存模板。"
        template_cards = []
        for name, template in templates.items():
            keys = template["field_keys"]
            rules = template.get("rules", {})
            preview_labels = [PRODUCT_FIELD_MAP[key].label for key in keys if key in PRODUCT_FIELD_MAP][:6]
            preview = "、".join(preview_labels)
            if len(keys) > len(preview_labels):
                preview = f"{preview} 等 {len(keys)} 个字段"
            rule_labels = []
            if rules.get("owner_department"):
                rule_labels.append(f"归属部门={rules['owner_department']}")
            if rules.get("brand_name"):
                rule_labels.append(f"品牌={rules['brand_name']}")
            if rules.get("category"):
                rule_labels.append(f"品类={rules['category']}")
            if rules.get("style_keyword"):
                rule_labels.append(f"款号/款色关键词={rules['style_keyword']}")
            rule_text = "；".join(rule_labels) if rule_labels else "未设置自动命中规则"
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
                      <div class="meta">{html.escape(rule_text)}</div>
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
        groups = []
        for group, fields in FIELDS_BY_GROUP.items():
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
        <section class="panel">
          <div class="eyebrow">Field Access</div>
          <h1>C 部门字段开放设置</h1>
          <p class="meta">这里决定 C 部门在页面、导出和 JSON 接口里能看到哪些资料字段，也可以管理给外部系统调用的只读 API 令牌。</p>
          <p class="meta">当前已开放 {len(selected)} 个字段。{html.escape(matching_text)}</p>
          {notice_block}
          {error_block}
          <section class="panel" style="margin-top:18px;">
            <h2>字段模板</h2>
            <p class="meta">可以把当前勾选保存成模板，后续一键套用到 C 部门的开放范围。</p>
            <div class="form-grid">
              {''.join(template_cards) if template_cards else '<div class="meta">当前还没有保存模板。你可以先在下方勾选字段，再保存为一个模板。</div>'}
            </div>
          </section>
          <form method="post" action="/settings/c-fields">
            <section class="panel" style="margin-top:18px;">
              <h2>保存当前组合为模板</h2>
              <p class="meta">模板保存后会保留当前勾选，并同步成为 C 部门当前开放字段。你还可以设置自动命中规则，让不同商品自动使用不同字段模板。</p>
              <label class="field field-wide">
                <span>模板名称</span>
                <input name="template_name" value="{html.escape(template_name)}" placeholder="例如：招商只读 / 合规查看 / 直播提案">
              </label>
              <div class="form-grid" style="margin-top:16px;">
                <label class="field">
                  <span>自动规则：归属部门</span>
                  <select name="template_rule_department">
                    <option value="">不限制</option>
                    <option value="A" {"selected" if template_rule_department == "A" else ""}>A 部门</option>
                    <option value="B" {"selected" if template_rule_department == "B" else ""}>B 部门</option>
                  </select>
                </label>
                <label class="field">
                  <span>自动规则：品牌名称</span>
                  <input name="template_rule_brand" value="{html.escape(template_rule_brand)}" placeholder="完全匹配，例如 North Harbor">
                </label>
                <label class="field">
                  <span>自动规则：品类</span>
                  <input name="template_rule_category" value="{html.escape(template_rule_category)}" placeholder="完全匹配，例如 连衣裙">
                </label>
                <label class="field">
                  <span>自动规则：款号/款色关键词</span>
                  <input name="template_rule_style" value="{html.escape(template_rule_style)}" placeholder="包含匹配，例如 NH- 或 牛仔">
                </label>
              </div>
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
        return self.page("字段开放设置 - 商品资料后台", content, user)

    def status_note_label(self, user, product: dict, target_status: str) -> str:
        if target_status == "pending":
            return "提交说明（选填）"
        if can_review_product(user, product):
            return "审核意见（选填）"
        return "处理说明（选填）"

    def status_note_placeholder(self, user, product: dict, target_status: str) -> str:
        if target_status == "pending":
            return "例如：图片、尺码、合规信息都已补齐，可以进入审核。"
        if target_status == "published":
            return "例如：已核对关键信息无误，同意发布。"
        if can_review_product(user, product):
            return "例如：请补充检测报告或修正价格后再提交。"
        return "例如：说明本次撤回或下线转草稿的原因。"

    def status_change_details(self, user, product: dict, target_status: str, review_note: str = "") -> str:
        actor = department_label(user.get("department"))
        if target_status == "pending":
            details = f"{actor} 提交资料进入审核流程。"
            note_prefix = "提交说明"
        elif target_status == "published":
            details = f"{actor} 审核通过并发布资料。"
            note_prefix = "审核意见"
        elif can_review_product(user, product):
            details = f"{actor} 将资料退回为草稿。"
            note_prefix = "审核意见"
        else:
            details = f"{actor} 将资料从 {status_label(product.get('status'))} 调整为草稿。"
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
