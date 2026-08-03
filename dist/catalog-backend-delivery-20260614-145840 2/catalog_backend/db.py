from __future__ import annotations

import json
import csv
import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from catalog_backend.fields import PRODUCT_FIELDS, PRODUCT_FIELD_MAP


DEMO_PASSWORD = "demo123"
LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCK_MINUTES = 15
UTC = timezone.utc


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    salt, expected = password_hash.split("$", 1)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return secrets.compare_digest(expected, digest.hex())


def init_db(
    db_path: str | Path,
    *,
    seed_demo: bool = True,
    seed_samples: bool = True,
    bootstrap_admin: dict | None = None,
) -> None:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                department TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                must_change_password INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        existing_user_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        if "is_active" not in existing_user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        if "must_change_password" not in existing_user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        product_columns = ",\n".join(
            f"{field.key} {field.storage_type}" for field in PRODUCT_FIELDS
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                {product_columns},
                owner_department TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                lifecycle_status TEXT NOT NULL DEFAULT 'active',
                last_reviewed_by INTEGER,
                last_reviewed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(created_by) REFERENCES users(id),
                FOREIGN KEY(last_reviewed_by) REFERENCES users(id)
            )
            """
        )
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(products)").fetchall()
        }
        if "status" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN status TEXT NOT NULL DEFAULT 'draft'")
        if "lifecycle_status" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'active'")
        if "last_reviewed_by" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN last_reviewed_by INTEGER")
        if "last_reviewed_at" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN last_reviewed_at TEXT")
        if "image_gallery_json" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN image_gallery_json TEXT NOT NULL DEFAULT '[]'")
        if "launch_channel" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN launch_channel TEXT")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS product_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                actor_user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                action_label TEXT NOT NULL,
                details TEXT NOT NULL,
                diff_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                FOREIGN KEY(product_id) REFERENCES products(id),
                FOREIGN KEY(actor_user_id) REFERENCES users(id)
            )
            """
        )
        existing_log_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(product_logs)").fetchall()
        }
        if "diff_json" not in existing_log_columns:
            connection.execute("ALTER TABLE product_logs ADD COLUMN diff_json TEXT NOT NULL DEFAULT '[]'")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS login_attempts (
                username TEXT PRIMARY KEY,
                failure_count INTEGER NOT NULL DEFAULT 0,
                locked_until TEXT,
                last_attempt_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                action_label TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL DEFAULT '',
                target_label TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(actor_user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_owner_department ON products(owner_department)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_style_code ON products(style_code)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_product_name ON products(product_name)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_status ON products(status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_products_lifecycle_status ON products(lifecycle_status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_logs_product_id ON product_logs(product_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_created_at ON admin_audit_logs(created_at)"
        )
        seed_default_settings(connection)
        if seed_demo:
            seed_demo_users(connection)
            if seed_samples:
                seed_sample_products(connection)
        if bootstrap_admin:
            ensure_bootstrap_admin(connection, bootstrap_admin)


def seed_demo_users(connection: sqlite3.Connection) -> None:
    existing = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if existing:
        return
    now = utc_now()
    demo_users = [
        ("a_editor", "A 部门录入员", "A", hash_password(DEMO_PASSWORD), 1, 0, now),
        ("b_editor", "B 部门录入员", "B", hash_password(DEMO_PASSWORD), 1, 0, now),
        ("c_viewer", "C 部门查看员", "C", hash_password(DEMO_PASSWORD), 1, 0, now),
        ("admin_reviewer", "系统管理员", "ADMIN", hash_password(DEMO_PASSWORD), 1, 0, now),
    ]
    connection.executemany(
        """
        INSERT INTO users (username, display_name, department, password_hash, is_active, must_change_password, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        demo_users,
    )


def seed_sample_products(connection: sqlite3.Connection) -> None:
    existing = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if existing:
        return
    users = {
        row["department"]: dict(row)
        for row in connection.execute("SELECT id, department FROM users").fetchall()
    }
    sample_rows = [
        {
            "brand_name": "North Harbor",
            "season_year": "2026夏",
            "style_color": "短袖连衣裙-蓝",
            "style_code": "NH-2601",
            "color_name": "海盐蓝",
            "product_name": "褶皱短袖连衣裙",
            "category": "连衣裙",
            "has_accessories": "无",
            "supplier": "杭州云锦供应链",
            "tag_price": 499,
            "launch_price": 329,
            "launch_channel": "直播首发",
            "size_range": "S-XL",
            "size_s": 20,
            "size_m": 28,
            "size_l": 18,
            "size_xl": 10,
            "total_quantity": 76,
            "material": "梭织",
            "composition": "面料 85%棉 15%锦纶",
            "washing_method": "建议冷水轻柔机洗",
            "washing_method_en": "Machine wash cold, gentle cycle",
            "safety_category": "B类",
            "standard_code": "GB/T 2660",
            "size_chart": "S: 肩宽37 / 胸围92 / 衣长112",
            "detection_report": "已归档",
            "shipping_warehouse": "杭州一仓",
            "image_url": "https://example.com/images/nh-2601.jpg",
        },
        {
            "brand_name": "Studio Pine",
            "season_year": "2026秋",
            "style_color": "针织开衫-米白",
            "style_code": "SP-8420",
            "color_name": "燕麦白",
            "product_name": "毛感针织开衫",
            "category": "针织衫",
            "has_accessories": "有",
            "supplier": "嘉兴尚品针织",
            "tag_price": 399,
            "launch_price": 269,
            "launch_channel": "门店首发",
            "size_range": "F",
            "size_f": 48,
            "total_quantity": 48,
            "material": "针织",
            "composition": "46%腈纶 30%聚酯纤维 24%锦纶",
            "washing_method": "建议平铺晾干",
            "washing_method_en": "Dry flat",
            "safety_category": "B类",
            "standard_code": "FZ/T 73018",
            "size_chart": "F: 肩宽58 / 胸围118 / 衣长62",
            "detection_report": "待补充",
            "shipping_warehouse": "嘉兴二仓",
            "image_url": "https://example.com/images/sp-8420.jpg",
        },
    ]
    a_user_id = users["A"]["id"]
    b_user_id = users["B"]["id"]
    if len(sample_rows) != 2:
        raise ValueError("演示商品配置数量应为 2 条。")

    first_product_id = create_product(connection, sample_rows[0], a_user_id, "A")
    change_product_status(
        connection,
        first_product_id,
        "pending",
        a_user_id,
        "提交给B填写",
        "系统预置演示数据：A 部门已完成主体字段，转交 B 部门补充价格与渠道。",
    )
    change_product_status(
        connection,
        first_product_id,
        "published",
        b_user_id,
        "填写完成，开放给C",
        "系统预置演示数据：B 部门已补齐价格与渠道，资料已开放给 C 部门。",
    )

    second_product_id = create_product(connection, sample_rows[1], a_user_id, "A")
    change_product_status(
        connection,
        second_product_id,
        "pending",
        a_user_id,
        "提交给B填写",
        "系统预置演示数据：A 部门已完成主体字段，等待 B 部门补充价格与渠道。",
    )


def ensure_bootstrap_admin(connection: sqlite3.Connection, bootstrap_admin: dict) -> None:
    username = str(bootstrap_admin.get("username", "")).strip()
    display_name = str(bootstrap_admin.get("display_name", "")).strip() or "系统管理员"
    password = str(bootstrap_admin.get("password", "")).strip()
    must_change_password = bool(bootstrap_admin.get("must_change_password", True))
    if not username or not password:
        return
    existing = connection.execute(
        "SELECT id FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if existing:
        return
    connection.execute(
        """
        INSERT INTO users (username, display_name, department, password_hash, is_active, must_change_password, created_at)
        VALUES (?, ?, 'ADMIN', ?, 1, ?, ?)
        """,
        (
            username,
            display_name,
            hash_password(password),
            1 if must_change_password else 0,
            utc_now(),
        ),
    )


def seed_default_settings(connection: sqlite3.Connection) -> None:
    defaults = {
        "c_visible_field_keys": ",".join(field.key for field in PRODUCT_FIELDS if field.visible_to_c),
        "c_api_token": "",
        "c_field_templates_json": "{}",
    }
    for key, value in defaults.items():
        existing = connection.execute(
            "SELECT setting_key FROM app_settings WHERE setting_key = ?",
            (key,),
        ).fetchone()
        if existing:
            continue
        connection.execute(
            """
            INSERT INTO app_settings (setting_key, setting_value, updated_at)
            VALUES (?, ?, ?)
            """,
            (key, value, utc_now()),
        )


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def authenticate_user(db_path: str | Path, username: str, password: str) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if not row:
        return None
    user = dict(row)
    if not user.get("is_active"):
        return None
    if verify_password(password, user["password_hash"]):
        return user
    return None


def get_login_attempt_status(db_path: str | Path, username: str) -> dict:
    clean_username = username.strip()
    if not clean_username:
        return {
            "username": "",
            "failure_count": 0,
            "locked_until": None,
            "is_locked": False,
            "remaining_seconds": 0,
        }
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT username, failure_count, locked_until, last_attempt_at
            FROM login_attempts
            WHERE username = ?
            """,
            (clean_username,),
        ).fetchone()
    attempt = row_to_dict(row) or {
        "username": clean_username,
        "failure_count": 0,
        "locked_until": None,
        "last_attempt_at": utc_now(),
    }
    locked_until = parse_utc(attempt.get("locked_until"))
    now = datetime.now(UTC)
    is_locked = bool(locked_until and locked_until > now)
    remaining_seconds = 0
    if is_locked:
        remaining_seconds = max(0, int((locked_until - now).total_seconds()))
    return {
        "username": clean_username,
        "failure_count": int(attempt.get("failure_count") or 0),
        "locked_until": attempt.get("locked_until"),
        "is_locked": is_locked,
        "remaining_seconds": remaining_seconds,
    }


def register_login_failure(db_path: str | Path, username: str) -> dict:
    clean_username = username.strip()
    now = datetime.now(UTC)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT failure_count, locked_until
            FROM login_attempts
            WHERE username = ?
            """,
            (clean_username,),
        ).fetchone()
        current_failure_count = 0
        if row:
            locked_until = parse_utc(row["locked_until"])
            if locked_until and locked_until <= now:
                current_failure_count = 0
            else:
                current_failure_count = int(row["failure_count"] or 0)
        failure_count = current_failure_count + 1
        locked_until_value = None
        if failure_count >= LOGIN_FAILURE_LIMIT:
            locked_until_value = (
                now + timedelta(minutes=LOGIN_LOCK_MINUTES)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        connection.execute(
            """
            INSERT INTO login_attempts (username, failure_count, locked_until, last_attempt_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                failure_count = excluded.failure_count,
                locked_until = excluded.locked_until,
                last_attempt_at = excluded.last_attempt_at
            """,
            (clean_username, failure_count, locked_until_value, utc_now()),
        )
    return get_login_attempt_status(db_path, clean_username)


def clear_login_failures(db_path: str | Path, username: str) -> None:
    clean_username = username.strip()
    if not clean_username:
        return
    with get_connection(db_path) as connection:
        connection.execute(
            "DELETE FROM login_attempts WHERE username = ?",
            (clean_username,),
        )


def get_user_by_id(db_path: str | Path, user_id: int) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return row_to_dict(row)


def list_users(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, username, display_name, department, is_active, must_change_password, created_at
            FROM users
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def create_user(
    db_path: str | Path,
    username: str,
    display_name: str,
    department: str,
    password: str,
    must_change_password: bool = True,
) -> int:
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users (username, display_name, department, password_hash, is_active, must_change_password, created_at)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (
                username.strip(),
                display_name.strip(),
                department.strip(),
                hash_password(password),
                1 if must_change_password else 0,
                utc_now(),
            ),
        )
        return connection.execute("SELECT last_insert_rowid()").fetchone()[0]


def update_user_profile(
    db_path: str | Path,
    user_id: int,
    display_name: str,
    department: str,
) -> None:
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE users
            SET display_name = ?, department = ?
            WHERE id = ?
            """,
            (display_name.strip(), department.strip(), user_id),
        )


def set_user_active(db_path: str | Path, user_id: int, is_active: bool) -> None:
    with get_connection(db_path) as connection:
        connection.execute(
            "UPDATE users SET is_active = ? WHERE id = ?",
            (1 if is_active else 0, user_id),
        )


def reset_user_password(
    db_path: str | Path,
    user_id: int,
    new_password: str,
    must_change_password: bool = True,
) -> None:
    update_user_password(db_path, user_id, new_password, must_change_password)


def update_user_password(
    db_path: str | Path,
    user_id: int,
    new_password: str,
    must_change_password: bool = False,
) -> None:
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE users
            SET password_hash = ?, must_change_password = ?
            WHERE id = ?
            """,
            (hash_password(new_password), 1 if must_change_password else 0, user_id),
        )


def get_user_record(db_path: str | Path, user_id: int) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT id, username, display_name, department, is_active, must_change_password, created_at
            FROM users
            WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
    return row_to_dict(row)


def get_setting(db_path: str | Path, key: str) -> str | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = ?",
            (key,),
        ).fetchone()
    return row["setting_value"] if row else None


def set_setting(db_path: str | Path, key: str, value: str) -> None:
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO app_settings (setting_key, setting_value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value = excluded.setting_value,
                updated_at = excluded.updated_at
            """,
            (key, value, utc_now()),
        )


def normalize_value(raw_value, storage_type: str):
    if raw_value is None:
        return None
    if isinstance(raw_value, str):
        raw_value = raw_value.strip()
        if raw_value == "":
            return None
    if storage_type == "REAL":
        return float(raw_value)
    if storage_type == "INTEGER":
        return int(float(raw_value))
    return str(raw_value)


def normalize_product_data(raw_values: dict) -> dict:
    normalized = {}
    for field in PRODUCT_FIELDS:
        normalized[field.key] = normalize_value(raw_values.get(field.key), field.storage_type)
    normalized["image_gallery_json"] = normalize_image_gallery(raw_values)
    return normalized


def normalize_image_gallery(raw_values: dict) -> str:
    raw_gallery = raw_values.get("image_gallery_json")
    if isinstance(raw_gallery, str):
        try:
            parsed = json.loads(raw_gallery)
        except json.JSONDecodeError:
            parsed = []
    elif isinstance(raw_gallery, list):
        parsed = raw_gallery
    else:
        parsed = []
    normalized = []
    seen = set()
    for item in parsed:
        value = str(item).strip()
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    if not normalized:
        fallback = str(raw_values.get("image_url") or "").strip()
        if fallback:
            normalized = [fallback]
    return json.dumps(normalized, ensure_ascii=False)


def create_product(
    connection: sqlite3.Connection,
    raw_values: dict,
    created_by: int,
    owner_department: str,
) -> int:
    payload = normalize_product_data(raw_values)
    timestamp = utc_now()
    columns = [field.key for field in PRODUCT_FIELDS] + [
        "image_gallery_json",
        "owner_department",
        "created_by",
        "status",
        "lifecycle_status",
        "created_at",
        "updated_at",
    ]
    values = [payload[field.key] for field in PRODUCT_FIELDS]
    values.append(payload["image_gallery_json"])
    values.extend([owner_department, created_by, "draft", "active", timestamp, timestamp])
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO products ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    product_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
    log_product_action(
        connection,
        product_id,
        created_by,
        "create",
        "创建资料",
        "创建了一条新的商品资料，初始状态为草稿。",
    )
    return product_id


def update_product(connection: sqlite3.Connection, product_id: int, raw_values: dict, actor_user_id: int | None = None) -> None:
    payload = normalize_product_data(raw_values)
    before_row = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    before_product = row_to_dict(before_row) or {}
    assignments = ", ".join([*(f"{field.key} = ?" for field in PRODUCT_FIELDS), "image_gallery_json = ?"])
    values = [payload[field.key] for field in PRODUCT_FIELDS]
    values.append(payload["image_gallery_json"])
    timestamp = utc_now()
    values.extend([timestamp, product_id])
    connection.execute(
        f"UPDATE products SET {assignments}, updated_at = ? WHERE id = ?",
        values,
    )
    if actor_user_id:
        next_status = "draft"
        reset_reviewer = True
        actor_department_row = connection.execute(
            "SELECT department FROM users WHERE id = ?",
            (actor_user_id,),
        ).fetchone()
        actor_department = actor_department_row["department"] if actor_department_row else ""
        if actor_department == "B" and before_product.get("status") in {"pending", "published"}:
            next_status = "pending"
            reset_reviewer = before_product.get("status") == "published"
        connection.execute(
            """
            UPDATE products
            SET status = ?, last_reviewed_by = ?, last_reviewed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                next_status,
                None if reset_reviewer else before_product.get("last_reviewed_by"),
                None if reset_reviewer else before_product.get("last_reviewed_at"),
                timestamp,
                product_id,
            ),
        )
        details = "编辑了商品资料字段内容，并将状态重置为 A 填写阶段等待再次提交流转。"
        if actor_department == "B" and before_product.get("status") == "pending":
            details = "补充了 B 部门负责的价格与渠道字段，资料仍保留在待B填写阶段。"
        elif actor_department == "B" and before_product.get("status") == "published":
            details = "更新了 B 部门负责的价格与渠道字段，资料已重新进入待B填写阶段，完成后可再次开放给 C。"
        log_product_action(
            connection,
            product_id,
            actor_user_id,
            "update",
            "更新资料",
            details,
            diff_json=build_product_diff(before_product, payload),
        )


def log_product_action(
    connection: sqlite3.Connection,
    product_id: int,
    actor_user_id: int,
    action: str,
    action_label: str,
    details: str,
    diff_json: list[dict] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO product_logs (product_id, actor_user_id, action, action_label, details, diff_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product_id,
            actor_user_id,
            action,
            action_label,
            details,
            json.dumps(diff_json or [], ensure_ascii=False),
            utc_now(),
        ),
    )


def log_admin_audit_action(
    db_path: str | Path,
    actor_user_id: int,
    action: str,
    action_label: str,
    target_type: str,
    target_id: str,
    target_label: str,
    details: str,
) -> None:
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO admin_audit_logs (
                actor_user_id, action, action_label, target_type, target_id, target_label, details, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                actor_user_id,
                action,
                action_label,
                target_type,
                target_id,
                target_label,
                details,
                utc_now(),
            ),
        )


def change_product_status(
    connection: sqlite3.Connection,
    product_id: int,
    status: str,
    actor_user_id: int,
    action_label: str,
    details: str,
) -> None:
    timestamp = utc_now()
    connection.execute(
        """
        UPDATE products
        SET status = ?, last_reviewed_by = ?, last_reviewed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, actor_user_id, timestamp, timestamp, product_id),
    )
    log_product_action(connection, product_id, actor_user_id, f"status:{status}", action_label, details)


def change_product_lifecycle(
    connection: sqlite3.Connection,
    product_id: int,
    lifecycle_status: str,
    actor_user_id: int,
    action_label: str,
    details: str,
) -> None:
    timestamp = utc_now()
    connection.execute(
        """
        UPDATE products
        SET lifecycle_status = ?, updated_at = ?
        WHERE id = ?
        """,
        (lifecycle_status, timestamp, product_id),
    )
    log_product_action(connection, product_id, actor_user_id, f"lifecycle:{lifecycle_status}", action_label, details)


def build_product_diff(before_product: dict, after_payload: dict) -> list[dict]:
    diffs = []
    for field in PRODUCT_FIELDS:
        before_value = normalize_diff_value(before_product.get(field.key))
        after_value = normalize_diff_value(after_payload.get(field.key))
        if before_value == after_value:
            continue
        diffs.append(
            {
                "field_key": field.key,
                "field_label": field.label,
                "before": before_value,
                "after": after_value,
            }
        )
    return diffs


def normalize_diff_value(value):
    if value in (None, ""):
        return ""
    return str(value)


def get_product(db_path: str | Path, product_id: int) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT p.*, u.display_name AS creator_name, u.username AS creator_username,
                   reviewer.display_name AS reviewer_name
            FROM products p
            JOIN users u ON u.id = p.created_by
            LEFT JOIN users reviewer ON reviewer.id = p.last_reviewed_by
            WHERE p.id = ?
            """,
            (product_id,),
        ).fetchone()
        return row_to_dict(row)


def find_matching_owned_product(
    connection: sqlite3.Connection,
    created_by: int,
    style_code: str | None,
    color_name: str | None,
    product_name: str | None,
) -> dict | None:
    if not style_code and not product_name:
        return None
    row = connection.execute(
        """
        SELECT *
        FROM products
        WHERE created_by = ?
          AND COALESCE(style_code, '') = COALESCE(?, '')
          AND COALESCE(color_name, '') = COALESCE(?, '')
          AND COALESCE(product_name, '') = COALESCE(?, '')
        ORDER BY id DESC
        LIMIT 1
        """,
        (created_by, style_code or "", color_name or "", product_name or ""),
    ).fetchone()
    return row_to_dict(row)


def save_or_update_owned_product(
    db_path: str | Path,
    raw_values: dict,
    created_by: int,
    owner_department: str,
) -> tuple[str, int]:
    if owner_department != "A":
        raise ValueError("只有 A 部门可以通过导入创建或更新主体资料。")
    with get_connection(db_path) as connection:
        existing = find_matching_owned_product(
            connection,
            created_by,
            raw_values.get("style_code"),
            raw_values.get("color_name"),
            raw_values.get("product_name"),
        )
        if existing:
            update_product(connection, existing["id"], raw_values, created_by)
            return "updated", existing["id"]
        product_id = create_product(connection, raw_values, created_by, owner_department)
        return "created", product_id


def list_products(
    db_path: str | Path,
    query: str = "",
    department: str = "",
    status: str = "",
    lifecycle_status: str = "",
) -> list[dict]:
    like_query = f"%{query.strip()}%"
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT p.*, u.display_name AS creator_name, u.username AS creator_username,
                   reviewer.display_name AS reviewer_name
            FROM products p
            JOIN users u ON u.id = p.created_by
            LEFT JOIN users reviewer ON reviewer.id = p.last_reviewed_by
            WHERE (
                ? = ''
                OR COALESCE(p.product_name, '') LIKE ?
                OR COALESCE(p.style_code, '') LIKE ?
                OR COALESCE(p.brand_name, '') LIKE ?
            )
            AND (? = '' OR p.owner_department = ?)
            AND (? = '' OR p.status = ?)
            AND (? = '' OR p.lifecycle_status = ?)
            ORDER BY p.updated_at DESC, p.id DESC
            """,
            (
                query.strip(),
                like_query,
                like_query,
                like_query,
                department.strip(),
                department.strip(),
                status.strip(),
                status.strip(),
                lifecycle_status.strip(),
                lifecycle_status.strip(),
            ),
        ).fetchall()
        return [dict(row) for row in rows]


def department_stats(db_path: str | Path) -> dict[str, int]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT owner_department, COUNT(*) AS total FROM products GROUP BY owner_department"
        ).fetchall()
    stats = {"A": 0, "B": 0, "C": 0}
    for row in rows:
        stats[row["owner_department"]] = row["total"]
    return stats


def status_stats(db_path: str | Path) -> dict[str, int]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT status, COUNT(*) AS total FROM products GROUP BY status"
        ).fetchall()
    stats = {"draft": 0, "pending": 0, "published": 0}
    for row in rows:
        stats[row["status"]] = row["total"]
    return stats


def lifecycle_stats(db_path: str | Path) -> dict[str, int]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT lifecycle_status, COUNT(*) AS total FROM products GROUP BY lifecycle_status"
        ).fetchall()
    stats = {"active": 0, "archived": 0, "deleted": 0}
    for row in rows:
        stats[row["lifecycle_status"]] = row["total"]
    return stats


def recent_activity_stats(db_path: str | Path, days: int = 7) -> dict[str, int]:
    cutoff = iso_days_ago(days)
    with get_connection(db_path) as connection:
        created_count = connection.execute(
            "SELECT COUNT(*) FROM products WHERE created_at >= ?",
            (cutoff,),
        ).fetchone()[0]
        updated_count = connection.execute(
            "SELECT COUNT(*) FROM product_logs WHERE created_at >= ?",
            (cutoff,),
        ).fetchone()[0]
        pending_count = connection.execute(
            "SELECT COUNT(*) FROM products WHERE status = 'pending' AND lifecycle_status = 'active'"
        ).fetchone()[0]
    return {
        "recent_created": created_count,
        "recent_logs": updated_count,
        "pending_active": pending_count,
    }


def recent_department_created_stats(db_path: str | Path, days: int = 7) -> dict[str, int]:
    cutoff = iso_days_ago(days)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT owner_department, COUNT(*) AS total
            FROM products
            WHERE created_at >= ?
            GROUP BY owner_department
            """,
            (cutoff,),
        ).fetchall()
    stats = {"A": 0, "B": 0, "C": 0}
    for row in rows:
        stats[row["owner_department"]] = row["total"]
    return stats


def get_product_logs(db_path: str | Path, product_id: int) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT l.*, u.display_name AS actor_name, u.department AS actor_department,
                   p.product_name, p.style_code, p.owner_department, p.created_by
            FROM product_logs l
            JOIN users u ON u.id = l.actor_user_id
            JOIN products p ON p.id = l.product_id
            WHERE l.product_id = ?
            ORDER BY l.created_at DESC, l.id DESC
            """,
            (product_id,),
        ).fetchall()
    logs = []
    for row in rows:
        item = dict(row)
        item["diff_items"] = json.loads(item.get("diff_json") or "[]")
        logs.append(item)
    return logs


def list_product_logs(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT l.*, u.display_name AS actor_name, u.department AS actor_department,
                   p.product_name, p.style_code, p.owner_department, p.created_by
            FROM product_logs l
            JOIN users u ON u.id = l.actor_user_id
            JOIN products p ON p.id = l.product_id
            ORDER BY l.created_at DESC, l.id DESC
            """
        ).fetchall()
    logs = []
    for row in rows:
        item = dict(row)
        item["diff_items"] = json.loads(item.get("diff_json") or "[]")
        logs.append(item)
    return logs


def list_admin_audit_logs(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT l.*, u.display_name AS actor_name, u.department AS actor_department
            FROM admin_audit_logs l
            JOIN users u ON u.id = l.actor_user_id
            ORDER BY l.created_at DESC, l.id DESC
            """
        ).fetchall()
    logs = []
    for row in rows:
        item = dict(row)
        item["product_id"] = ""
        item["product_name"] = item.get("target_label", "")
        item["style_code"] = item.get("target_id", "")
        item["owner_department"] = item.get("target_type", "")
        item["created_by"] = None
        item["diff_items"] = []
        logs.append(item)
    return logs


def filter_product_logs(
    logs: list[dict],
    action_query: str = "",
    actor_query: str = "",
    product_query: str = "",
) -> list[dict]:
    action_keyword = action_query.strip().lower()
    actor_keyword = actor_query.strip().lower()
    product_keyword = product_query.strip().lower()
    filtered = []
    for item in logs:
        action_text = f"{item.get('action', '')} {item.get('action_label', '')}".lower()
        actor_text = f"{item.get('actor_name', '')} {item.get('actor_department', '')}".lower()
        product_text = f"{item.get('product_name', '')} {item.get('style_code', '')} {item.get('product_id', '')}".lower()
        if action_keyword and action_keyword not in action_text:
            continue
        if actor_keyword and actor_keyword not in actor_text:
            continue
        if product_keyword and product_keyword not in product_text:
            continue
        filtered.append(item)
    return filtered


def filter_admin_logs(
    logs: list[dict],
    action_query: str = "",
    actor_query: str = "",
    product_query: str = "",
) -> list[dict]:
    action_keyword = action_query.strip().lower()
    actor_keyword = actor_query.strip().lower()
    target_keyword = product_query.strip().lower()
    filtered = []
    for item in logs:
        action_text = f"{item.get('action', '')} {item.get('action_label', '')}".lower()
        actor_text = f"{item.get('actor_name', '')} {item.get('actor_department', '')}".lower()
        target_text = f"{item.get('target_type', '')} {item.get('target_label', '')} {item.get('target_id', '')}".lower()
        if action_keyword and action_keyword not in action_text:
            continue
        if actor_keyword and actor_keyword not in actor_text:
            continue
        if target_keyword and target_keyword not in target_text:
            continue
        filtered.append(item)
    return filtered


def product_logs_csv_bytes(logs: list[dict]) -> bytes:
    rows = []
    for item in logs:
        diff_summary = []
        for diff in item.get("diff_items") or []:
            diff_summary.append(
                f"{diff.get('field_label', '')}: 修改前={diff.get('before', '')} / 修改后={diff.get('after', '')}"
            )
        rows.append(
            {
                "资料ID": item.get("product_id", ""),
                "商品名称": item.get("product_name", ""),
                "款号": item.get("style_code", ""),
                "资料发起部门": item.get("owner_department", ""),
                "时间": item.get("created_at", ""),
                "操作人": item.get("actor_name", ""),
                "部门": item.get("actor_department", ""),
                "动作": item.get("action_label", ""),
                "说明": item.get("details", ""),
                "字段差异": " | ".join(diff_summary),
            }
        )
    from io import StringIO

    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=["资料ID", "商品名称", "款号", "资料发起部门", "时间", "操作人", "部门", "动作", "说明", "字段差异"],
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def iso_days_ago(days: int) -> str:
    now = datetime.now(UTC).replace(microsecond=0)
    return (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")
