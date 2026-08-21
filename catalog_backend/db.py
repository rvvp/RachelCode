from __future__ import annotations

import json
import csv
import hashlib
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from catalog_backend.fields import CATALOG_EXPORT_FIELD_ORDER, PRODUCT_FIELDS, PRODUCT_FIELD_MAP
from catalog_backend.policies import B_STAGE_FIELD_KEYS, c_visible_launch_channels, normalize_billing_platform_codes


DEMO_PASSWORD = "demo123"
LOGIN_FAILURE_LIMIT = 5
LOGIN_LOCK_MINUTES = 15
UTC = timezone.utc
LIST_LAYOUT_VIRTUAL_KEYS = ("completion_flag",)
LIST_LAYOUT_HIDDEN_KEYS = (
    "size_chart",
    "size_f",
    "size_s",
    "size_m",
    "size_l",
    "size_xl",
    "size_2xl",
    "size_3xl",
    "total_quantity",
)
LIST_LAYOUT_A_EXCLUDED_KEYS = ("category", "image_url", "launch_price", "launch_channel")
PLATFORM_BILL_PLATFORM_ORDER = ("tmall", "jd", "vip", "douyin", "miniprogram")
PLATFORM_BILL_PLATFORM_LABELS = {
    "tmall": "天猫",
    "jd": "京东",
    "vip": "唯品",
    "douyin": "抖音",
    "miniprogram": "小程序",
}
PLATFORM_BILL_FILE_ROLE_LABELS = {
    "main": "账单文件",
    "attachment": "账单文件",
}
PLATFORM_SETTINGS_KEY = "platform_bill_platforms_json"
SUPPLIER_BILL_CHANGE_WINDOW_DAYS = 30
PRODUCT_DATE_FIELD_KEYS = {"shooting_date", "inspection_date"}
EMPTY_DATE_MARKERS = {
    "0",
    "0.0",
    "1899-12-30",
    "1899-12-30 00:00:00",
    "1899-12-31",
    "1899-12-31 00:00:00",
    "1900",
    "1900.0",
    "1900-01-00",
    "1900-01-00 00:00:00",
    "1900-01-01",
    "1900-01-01 00:00:00",
    "1900/01/01",
    "1900/1/1",
}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def is_placeholder_excel_date(value) -> bool:
    clean_value = str(value or "").strip()
    if not clean_value:
        return False
    normalized = clean_value.replace("/", "-")
    if normalized in EMPTY_DATE_MARKERS:
        return True
    if normalized.startswith("1900-01-0") or normalized.startswith("1899-12-3"):
        return True
    return False


def normalize_optional_date_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        clean_value = value.date().isoformat()
    elif isinstance(value, date):
        clean_value = value.isoformat()
    else:
        clean_value = str(value).strip()
    if not clean_value or is_placeholder_excel_date(clean_value):
        return None
    return clean_value


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    salt, expected = password_hash.split("$", 1)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return secrets.compare_digest(expected, digest.hex())


def ordered_list_layout_keys(available_keys: list[str], department: str) -> list[str]:
    department = str(department or "").strip().upper()
    excluded_keys = set()
    if department == "A":
        excluded_keys.update(LIST_LAYOUT_A_EXCLUDED_KEYS)
    filtered_available_keys = [key for key in available_keys if key not in excluded_keys]
    ordered_keys = []
    for virtual_key in LIST_LAYOUT_VIRTUAL_KEYS:
        if virtual_key in filtered_available_keys and virtual_key not in ordered_keys:
            ordered_keys.append(virtual_key)
    template_keys = [key for key in CATALOG_EXPORT_FIELD_ORDER if key in filtered_available_keys]
    for key in template_keys:
        if key not in ordered_keys:
            ordered_keys.append(key)
    for key in filtered_available_keys:
        if key not in ordered_keys:
            ordered_keys.append(key)
    return ordered_keys


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
                operating_channel TEXT NOT NULL DEFAULT '',
                billing_platforms_json TEXT NOT NULL DEFAULT '[]',
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
        if "operating_channel" not in existing_user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN operating_channel TEXT NOT NULL DEFAULT ''")
        if "billing_platforms_json" not in existing_user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN billing_platforms_json TEXT NOT NULL DEFAULT ''")
            connection.execute(
                """
                UPDATE users
                SET billing_platforms_json = CASE operating_channel
                    WHEN 'tmall' THEN '["tmall"]'
                    WHEN 'vip' THEN '["vip"]'
                    ELSE '[]'
                END
                WHERE department = 'C' AND TRIM(COALESCE(billing_platforms_json, '')) = ''
                """
            )
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
                revision_flag INTEGER NOT NULL DEFAULT 0,
                current_version_no INTEGER NOT NULL DEFAULT 1,
                last_reviewed_by INTEGER,
                last_reviewed_at TEXT,
                completed_to_c_at TEXT,
                c_release_no INTEGER NOT NULL DEFAULT 0,
                received_by INTEGER,
                received_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(created_by) REFERENCES users(id),
                FOREIGN KEY(last_reviewed_by) REFERENCES users(id),
                FOREIGN KEY(received_by) REFERENCES users(id)
            )
            """
        )
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(products)").fetchall()
        }
        for field in PRODUCT_FIELDS:
            if field.key in existing_columns:
                continue
            connection.execute(f"ALTER TABLE products ADD COLUMN {field.key} {field.storage_type}")
            existing_columns.add(field.key)
        if "status" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN status TEXT NOT NULL DEFAULT 'draft'")
            existing_columns.add("status")
        if "lifecycle_status" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'active'")
            existing_columns.add("lifecycle_status")
        if "revision_flag" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN revision_flag INTEGER NOT NULL DEFAULT 0")
            existing_columns.add("revision_flag")
        if "current_version_no" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN current_version_no INTEGER NOT NULL DEFAULT 1")
            existing_columns.add("current_version_no")
        if "last_reviewed_by" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN last_reviewed_by INTEGER")
            existing_columns.add("last_reviewed_by")
        if "last_reviewed_at" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN last_reviewed_at TEXT")
            existing_columns.add("last_reviewed_at")
        if "completed_to_c_at" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN completed_to_c_at TEXT")
            existing_columns.add("completed_to_c_at")
        if "c_release_no" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN c_release_no INTEGER NOT NULL DEFAULT 0")
            existing_columns.add("c_release_no")
        if "received_by" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN received_by INTEGER")
            existing_columns.add("received_by")
        if "received_at" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN received_at TEXT")
            existing_columns.add("received_at")
        if "image_gallery_json" not in existing_columns:
            connection.execute("ALTER TABLE products ADD COLUMN image_gallery_json TEXT NOT NULL DEFAULT '[]'")
            existing_columns.add("image_gallery_json")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS product_c_receipts (
                product_id INTEGER NOT NULL,
                recipient_user_id INTEGER NOT NULL,
                release_no INTEGER NOT NULL,
                received_at TEXT NOT NULL,
                PRIMARY KEY(product_id, recipient_user_id, release_no),
                FOREIGN KEY(product_id) REFERENCES products(id),
                FOREIGN KEY(recipient_user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_product_c_receipts_recipient ON product_c_receipts(recipient_user_id, product_id, release_no)"
        )
        connection.execute(
            """
            UPDATE products
            SET launch_channel = '天猫'
            WHERE TRIM(COALESCE(launch_channel, '')) IN ('天猫/京东/抖音', '天猫、京东、抖音')
            """
        )
        connection.execute(
            """
            UPDATE products
            SET c_release_no = 1
            WHERE c_release_no = 0
              AND status IN ('published', 'received')
              AND TRIM(COALESCE(launch_channel, '')) IN ('天猫', '唯品', '同款', '天猫/京东/抖音', '天猫、京东、抖音')
            """
        )
        connection.execute(
            """
            UPDATE products
            SET status = 'received',
                received_by = COALESCE(
                    received_by,
                    (
                        SELECT receipt.recipient_user_id
                        FROM product_c_receipts receipt
                        WHERE receipt.product_id = products.id
                          AND receipt.release_no = products.c_release_no
                        ORDER BY receipt.received_at ASC
                        LIMIT 1
                    )
                ),
                received_at = COALESCE(
                    received_at,
                    (
                        SELECT receipt.received_at
                        FROM product_c_receipts receipt
                        WHERE receipt.product_id = products.id
                          AND receipt.release_no = products.c_release_no
                        ORDER BY receipt.received_at ASC
                        LIMIT 1
                    )
                )
            WHERE status = 'published'
              AND c_release_no > 0
              AND EXISTS (
                  SELECT 1
                  FROM product_c_receipts receipt
                  WHERE receipt.product_id = products.id
                    AND receipt.release_no = products.c_release_no
              )
            """
        )
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
                diff_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(product_id) REFERENCES products(id),
                FOREIGN KEY(actor_user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS product_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                summary_json TEXT NOT NULL DEFAULT '[]',
                change_count INTEGER NOT NULL DEFAULT 0,
                source_version_no INTEGER,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                UNIQUE(product_id, version_no),
                FOREIGN KEY(product_id) REFERENCES products(id),
                FOREIGN KEY(created_by) REFERENCES users(id)
            )
            """
        )
        existing_log_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(product_logs)").fetchall()
        }
        if "diff_json" not in existing_log_columns:
            connection.execute("ALTER TABLE product_logs ADD COLUMN diff_json TEXT NOT NULL DEFAULT '[]'")
        if "diff_count" not in existing_log_columns:
            connection.execute("ALTER TABLE product_logs ADD COLUMN diff_count INTEGER NOT NULL DEFAULT 0")
        existing_version_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(product_versions)").fetchall()
        }
        if existing_version_columns:
            if "summary_json" not in existing_version_columns:
                connection.execute("ALTER TABLE product_versions ADD COLUMN summary_json TEXT NOT NULL DEFAULT '[]'")
            if "change_count" not in existing_version_columns:
                connection.execute("ALTER TABLE product_versions ADD COLUMN change_count INTEGER NOT NULL DEFAULT 0")
            if "source_version_no" not in existing_version_columns:
                connection.execute("ALTER TABLE product_versions ADD COLUMN source_version_no INTEGER")
            if "note" not in existing_version_columns:
                connection.execute("ALTER TABLE product_versions ADD COLUMN note TEXT NOT NULL DEFAULT ''")
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
            """
            CREATE TABLE IF NOT EXISTS planning_publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                publication_id TEXT NOT NULL,
                source_version_no INTEGER NOT NULL,
                category TEXT NOT NULL,
                launch_price REAL NOT NULL,
                fixed_multiplier REAL,
                supplier_coefficient REAL,
                raw_price REAL,
                operator_name TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'published',
                FOREIGN KEY(product_id) REFERENCES products(id)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_planning_publications_product ON planning_publications(product_id, published_at DESC)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS billing_months (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'draft',
                platforms_json TEXT NOT NULL DEFAULT '',
                created_by INTEGER NOT NULL,
                submitted_by INTEGER,
                submitted_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(created_by) REFERENCES users(id),
                FOREIGN KEY(submitted_by) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_bill_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                billing_month_id INTEGER NOT NULL,
                platform_code TEXT NOT NULL,
                file_role TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                uploaded_by INTEGER NOT NULL,
                version_no INTEGER NOT NULL DEFAULT 1,
                is_current INTEGER NOT NULL DEFAULT 1,
                submitted_by INTEGER,
                submitted_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(billing_month_id) REFERENCES billing_months(id) ON DELETE CASCADE,
                FOREIGN KEY(uploaded_by) REFERENCES users(id)
            )
            """
        )
        existing_platform_file_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(platform_bill_files)").fetchall()
        }
        existing_billing_month_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(billing_months)").fetchall()
        }
        if "platforms_json" not in existing_billing_month_columns:
            connection.execute("ALTER TABLE billing_months ADD COLUMN platforms_json TEXT NOT NULL DEFAULT ''")
            existing_billing_month_columns.add("platforms_json")
        if "submitted_by" not in existing_platform_file_columns:
            connection.execute("ALTER TABLE platform_bill_files ADD COLUMN submitted_by INTEGER")
            existing_platform_file_columns.add("submitted_by")
        if "submitted_at" not in existing_platform_file_columns:
            connection.execute("ALTER TABLE platform_bill_files ADD COLUMN submitted_at TEXT")
            existing_platform_file_columns.add("submitted_at")
        if "version_no" not in existing_platform_file_columns:
            connection.execute("ALTER TABLE platform_bill_files ADD COLUMN version_no INTEGER NOT NULL DEFAULT 1")
            existing_platform_file_columns.add("version_no")
        if "is_current" not in existing_platform_file_columns:
            connection.execute("ALTER TABLE platform_bill_files ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1")
            existing_platform_file_columns.add("is_current")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS platform_bill_return_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                billing_month_id INTEGER NOT NULL,
                platform_code TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_by INTEGER NOT NULL,
                requested_at TEXT NOT NULL,
                resolved_by INTEGER,
                resolved_at TEXT,
                resolution_note TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(billing_month_id) REFERENCES billing_months(id) ON DELETE CASCADE,
                FOREIGN KEY(requested_by) REFERENCES users(id),
                FOREIGN KEY(resolved_by) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS brand_bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'draft',
                source_billing_month_id INTEGER,
                dashboard_source_type TEXT NOT NULL DEFAULT '',
                dashboard_source_filename TEXT NOT NULL DEFAULT '',
                dashboard_source_path TEXT NOT NULL DEFAULT '',
                dashboard_updated_by INTEGER,
                dashboard_updated_at TEXT,
                created_by INTEGER NOT NULL,
                submitted_by INTEGER,
                submitted_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(source_billing_month_id) REFERENCES billing_months(id),
                FOREIGN KEY(created_by) REFERENCES users(id),
                FOREIGN KEY(submitted_by) REFERENCES users(id)
            )
            """
        )
        existing_brand_bill_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(brand_bills)").fetchall()
        }
        if "dashboard_source_type" not in existing_brand_bill_columns:
            connection.execute("ALTER TABLE brand_bills ADD COLUMN dashboard_source_type TEXT NOT NULL DEFAULT ''")
            existing_brand_bill_columns.add("dashboard_source_type")
        if "dashboard_source_filename" not in existing_brand_bill_columns:
            connection.execute("ALTER TABLE brand_bills ADD COLUMN dashboard_source_filename TEXT NOT NULL DEFAULT ''")
            existing_brand_bill_columns.add("dashboard_source_filename")
        if "dashboard_source_path" not in existing_brand_bill_columns:
            connection.execute("ALTER TABLE brand_bills ADD COLUMN dashboard_source_path TEXT NOT NULL DEFAULT ''")
            existing_brand_bill_columns.add("dashboard_source_path")
        if "dashboard_updated_by" not in existing_brand_bill_columns:
            connection.execute("ALTER TABLE brand_bills ADD COLUMN dashboard_updated_by INTEGER")
            existing_brand_bill_columns.add("dashboard_updated_by")
        if "dashboard_updated_at" not in existing_brand_bill_columns:
            connection.execute("ALTER TABLE brand_bills ADD COLUMN dashboard_updated_at TEXT")
            existing_brand_bill_columns.add("dashboard_updated_at")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS brand_bill_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_bill_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                original_filename TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                uploaded_by INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(brand_bill_id, version_no),
                FOREIGN KEY(brand_bill_id) REFERENCES brand_bills(id) ON DELETE CASCADE,
                FOREIGN KEY(uploaded_by) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS brand_bill_return_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_bill_id INTEGER NOT NULL,
                version_no INTEGER NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                requested_by INTEGER NOT NULL,
                requested_at TEXT NOT NULL,
                resolved_by INTEGER,
                resolved_at TEXT,
                resolution_note TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(brand_bill_id) REFERENCES brand_bills(id) ON DELETE CASCADE,
                FOREIGN KEY(requested_by) REFERENCES users(id),
                FOREIGN KEY(resolved_by) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS brand_bill_dashboard_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_bill_id INTEGER NOT NULL,
                row_no INTEGER NOT NULL,
                month_label TEXT NOT NULL DEFAULT '',
                platform_name TEXT NOT NULL DEFAULT '',
                shop_name TEXT NOT NULL DEFAULT '',
                total_qty REAL NOT NULL DEFAULT 0,
                total_amount REAL NOT NULL DEFAULT 0,
                gz_qty REAL NOT NULL DEFAULT 0,
                gz_amount REAL NOT NULL DEFAULT 0,
                wh_qty REAL NOT NULL DEFAULT 0,
                wh_amount REAL NOT NULL DEFAULT 0,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(brand_bill_id) REFERENCES brand_bills(id) ON DELETE CASCADE,
                FOREIGN KEY(created_by) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_code TEXT NOT NULL UNIQUE,
                supplier_name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(created_by) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS supplier_invoice_names (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_id INTEGER NOT NULL,
                invoice_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(supplier_id, invoice_name),
                FOREIGN KEY(supplier_id) REFERENCES suppliers(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS supplier_settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                month_key TEXT NOT NULL,
                supplier_id INTEGER NOT NULL,
                invoice_name TEXT NOT NULL DEFAULT '',
                amount_due REAL NOT NULL DEFAULT 0,
                amount_paid REAL NOT NULL DEFAULT 0,
                payment_status TEXT NOT NULL DEFAULT 'unpaid',
                payment_date TEXT,
                note TEXT NOT NULL DEFAULT '',
                source_brand_bill_id INTEGER,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(month_key, supplier_id),
                FOREIGN KEY(supplier_id) REFERENCES suppliers(id),
                FOREIGN KEY(source_brand_bill_id) REFERENCES brand_bills(id),
                FOREIGN KEY(created_by) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS supplier_code_masters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_code TEXT NOT NULL UNIQUE,
                supply_chain_manager TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(created_by) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS supplier_master_names (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_code_master_id INTEGER NOT NULL,
                supplier_name TEXT NOT NULL UNIQUE,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(supplier_code_master_id) REFERENCES supplier_code_masters(id) ON DELETE CASCADE
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS supplier_bill_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_month TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                original_filename TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                line_count INTEGER NOT NULL DEFAULT 0,
                is_current INTEGER NOT NULL DEFAULT 1,
                imported_by INTEGER NOT NULL,
                imported_at TEXT NOT NULL,
                UNIQUE(period_month, version_no),
                FOREIGN KEY(imported_by) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS supplier_bill_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplier_bill_batch_id INTEGER NOT NULL,
                supplier_master_name_id INTEGER NOT NULL,
                supplier_code TEXT NOT NULL,
                supplier_name TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT '',
                supply_chain_manager TEXT NOT NULL DEFAULT '',
                supplier_style_code TEXT NOT NULL DEFAULT '',
                brand_name TEXT NOT NULL DEFAULT '',
                style_color TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL DEFAULT 0,
                tax_included_price REAL NOT NULL DEFAULT 0,
                settlement_amount REAL NOT NULL DEFAULT 0,
                source_row_no INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(supplier_bill_batch_id, source_row_no),
                FOREIGN KEY(supplier_bill_batch_id) REFERENCES supplier_bill_batches(id) ON DELETE CASCADE,
                FOREIGN KEY(supplier_master_name_id) REFERENCES supplier_master_names(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS supplier_monthly_boards (
                board_year INTEGER NOT NULL,
                month_no INTEGER NOT NULL CHECK(month_no BETWEEN 1 AND 12),
                payable_supplier_count INTEGER,
                payable_total_amount REAL,
                updated_by INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(board_year, month_no),
                FOREIGN KEY(updated_by) REFERENCES users(id)
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
            "CREATE INDEX IF NOT EXISTS idx_product_versions_product_id ON product_versions(product_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_audit_logs_created_at ON admin_audit_logs(created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_billing_months_status ON billing_months(status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_platform_bill_files_month_platform ON platform_bill_files(billing_month_id, platform_code)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_platform_bill_files_month_role ON platform_bill_files(billing_month_id, file_role)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_platform_bill_return_requests_month_platform ON platform_bill_return_requests(billing_month_id, platform_code, status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_brand_bills_status ON brand_bills(status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_brand_bill_versions_bill_id ON brand_bill_versions(brand_bill_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_brand_bill_return_requests_bill_status ON brand_bill_return_requests(brand_bill_id, status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_suppliers_code ON suppliers(supplier_code)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_supplier_invoice_names_supplier_id ON supplier_invoice_names(supplier_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_supplier_settlements_month ON supplier_settlements(month_key)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_supplier_settlements_supplier_id ON supplier_settlements(supplier_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_supplier_monthly_boards_year ON supplier_monthly_boards(board_year)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_supplier_master_names_code ON supplier_master_names(supplier_code_master_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_supplier_bill_batches_period_current ON supplier_bill_batches(period_month, is_current)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_supplier_bill_lines_batch ON supplier_bill_lines(supplier_bill_batch_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_supplier_bill_lines_code ON supplier_bill_lines(supplier_code)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_supplier_bill_lines_name ON supplier_bill_lines(supplier_master_name_id)"
        )
        seed_default_settings(connection)
        if seed_demo:
            seed_demo_users(connection)
            if seed_samples:
                seed_sample_products(connection)
        if bootstrap_admin:
            ensure_bootstrap_admin(connection, bootstrap_admin)
        migrate_deprecated_composition_to_material(connection)
        backfill_non_customized_list_layout_defaults(connection)
        backfill_list_layout_virtual_fields(connection)
        backfill_supplier_product_list_layout_fields(connection)
        backfill_placeholder_product_dates(connection)
        backfill_product_completion_timestamps(connection)
        backfill_billing_month_platform_snapshots(connection)
        backfill_supplier_master_data(connection)
        connection.commit()


def migrate_deprecated_composition_to_material(connection: sqlite3.Connection) -> None:
    product_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(products)").fetchall()
    }
    if "composition" not in product_columns or "material" not in product_columns:
        return
    connection.execute(
        """
        UPDATE products
        SET material = composition
        WHERE (material IS NULL OR TRIM(material) = '')
          AND composition IS NOT NULL
          AND TRIM(composition) <> ''
        """
    )


def department_default_list_layout_keys(department: str) -> list[str]:
    department = str(department or "").strip().upper()
    available_keys = [field.key for field in PRODUCT_FIELDS if field.key not in LIST_LAYOUT_HIDDEN_KEYS]
    if department == "C":
        available_keys = [field.key for field in PRODUCT_FIELDS if field.visible_to_c and field.key not in LIST_LAYOUT_HIDDEN_KEYS]
    return ordered_list_layout_keys(available_keys, department)


def backfill_non_customized_list_layout_defaults(connection: sqlite3.Connection) -> None:
    for department in ("A", "B"):
        customized_key = f"list_layout_customized_{department}"
        fields_key = f"list_layout_fields_{department}"
        customized_row = connection.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = ?",
            (customized_key,),
        ).fetchone()
        is_customized = str((customized_row["setting_value"] if customized_row else "0") or "0") == "1"
        if is_customized:
            continue
        default_value = ",".join(department_default_list_layout_keys(department))
        connection.execute(
            "UPDATE app_settings SET setting_value = ?, updated_at = ? WHERE setting_key = ?",
            (default_value, utc_now(), fields_key),
        )


def backfill_list_layout_virtual_fields(connection: sqlite3.Connection) -> None:
    setting_keys = (
        "list_layout_fields_A",
        "list_layout_fields_B",
        "list_layout_fields_C",
    )
    for setting_key in setting_keys:
        row = connection.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = ?",
            (setting_key,),
        ).fetchone()
        if not row:
            continue
        raw_value = str(row["setting_value"] or "")
        values = [item.strip() for item in raw_value.split(",") if item.strip()]
        changed = False
        filtered_values = [value for value in values if value not in LIST_LAYOUT_HIDDEN_KEYS]
        if filtered_values != values:
            values = filtered_values
            changed = True
        if not changed:
            continue
        connection.execute(
            "UPDATE app_settings SET setting_value = ?, updated_at = ? WHERE setting_key = ?",
            (",".join(values), utc_now(), setting_key),
        )


def backfill_supplier_product_list_layout_fields(connection: sqlite3.Connection) -> None:
    insertions = (
        ("style_code", "supplier_style_code"),
        ("supplier", "supplier_code"),
    )
    for department in ("A", "B"):
        setting_key = f"list_layout_fields_{department}"
        row = connection.execute(
            "SELECT setting_value FROM app_settings WHERE setting_key = ?",
            (setting_key,),
        ).fetchone()
        if not row:
            continue
        values = [item.strip() for item in str(row["setting_value"] or "").split(",") if item.strip()]
        changed = False
        for anchor_key, new_key in insertions:
            if anchor_key not in values or new_key in values:
                continue
            values.insert(values.index(anchor_key) + 1, new_key)
            changed = True
        if changed:
            connection.execute(
                "UPDATE app_settings SET setting_value = ?, updated_at = ? WHERE setting_key = ?",
                (",".join(values), utc_now(), setting_key),
            )


def backfill_product_completion_timestamps(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        UPDATE products
        SET completed_to_c_at = NULL
        WHERE status IN ('draft', 'pending') AND completed_to_c_at IS NOT NULL
        """
    )
    connection.execute(
        """
        UPDATE products
        SET completed_to_c_at = COALESCE(
            (
                SELECT pl.created_at
                FROM product_logs pl
                WHERE pl.product_id = products.id AND pl.action = 'status:published'
                ORDER BY pl.id DESC
                LIMIT 1
            ),
            last_reviewed_at,
            updated_at,
            created_at
        )
        WHERE status IN ('published', 'received') AND completed_to_c_at IS NULL
        """
    )


def seed_demo_users(connection: sqlite3.Connection) -> None:
    existing = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if existing:
        return
    now = utc_now()
    demo_users = [
        ("a_editor", "跟单部录入员", "A", "", "[]", hash_password(DEMO_PASSWORD), 1, 0, now),
        ("b_editor", "商品部录入员", "B", "", "[]", hash_password(DEMO_PASSWORD), 1, 0, now),
        ("c_viewer", "天猫类运营查看员", "C", "tmall", '["tmall"]', hash_password(DEMO_PASSWORD), 1, 0, now),
        ("admin_reviewer", "系统管理员", "ADMIN", "", "[]", hash_password(DEMO_PASSWORD), 1, 0, now),
    ]
    connection.executemany(
        """
        INSERT INTO users (username, display_name, department, operating_channel, billing_platforms_json, password_hash, is_active, must_change_password, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            "shooting_date": "2026-06-08",
            "inspection_date": "2026-06-09",
            "brand_name": "North Harbor",
            "season_year": "2026夏",
            "style_color": "短袖连衣裙-蓝",
            "style_code": "NH-2601",
            "color_name": "海盐蓝",
            "product_name": "褶皱短袖连衣裙",
            "category": "连衣裙",
            "has_accessories": "无",
            "supplier": "杭州云锦供应链",
            "cooperation_mode": "买断",
            "supply_chain_manager": "陈主管",
            "tax_included_price": 359,
            "tag_price": 499,
            "launch_price": 329,
            "launch_channel": "天猫",
            "completion_flag": "Y",
            "size_range": "S-XL",
            "size_s": 20,
            "size_m": 28,
            "size_l": 18,
            "size_xl": 10,
            "total_quantity": 76,
            "material": "梭织",
            "composition": "面料 85%棉 15%锦纶",
            "composition_en": "SHELL: 85% COTTON 15% NYLON",
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
            "shooting_date": "",
            "inspection_date": "",
            "brand_name": "Studio Pine",
            "season_year": "2026秋",
            "style_color": "针织开衫-米白",
            "style_code": "SP-8420",
            "color_name": "燕麦白",
            "product_name": "毛感针织开衫",
            "category": "针织衫",
            "has_accessories": "有",
            "supplier": "嘉兴尚品针织",
            "cooperation_mode": "",
            "supply_chain_manager": "",
            "tax_included_price": None,
            "tag_price": 399,
            "launch_price": 269,
            "launch_channel": "门店首发",
            "completion_flag": "",
            "size_range": "F",
            "size_f": 48,
            "total_quantity": 48,
            "material": "针织",
            "composition": "46%腈纶 30%聚酯纤维 24%锦纶",
            "composition_en": "",
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
        "提交给商品部填写",
        "系统预置演示数据：跟单部已完成主体字段，转交商品部补充品类、图片、上新价格、上新渠道和资料完成。",
    )
    change_product_status(
        connection,
        first_product_id,
        "published",
        b_user_id,
        "填写完成，开放给运营部",
        "系统预置演示数据：商品部已补齐品类、图片、上新价格、上新渠道和资料完成，资料已开放给运营部。",
    )

    second_product_id = create_product(connection, sample_rows[1], a_user_id, "A")
    change_product_status(
        connection,
        second_product_id,
        "pending",
        a_user_id,
        "提交给商品部填写",
        "系统预置演示数据：跟单部已完成主体字段，等待商品部补充品类、图片、上新价格、上新渠道和资料完成。",
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
    a_default_fields = ",".join(
        [
            "shooting_date",
            "inspection_date",
            "detection_report",
            "size_chart",
            "shipping_warehouse",
            "brand_name",
            "season_year",
            "image_url",
            "style_color",
            "style_code",
            "supplier_style_code",
            "color_name",
            "product_name",
            "category",
            "has_accessories",
            "supplier",
            "supplier_code",
            "cooperation_mode",
            "supply_chain_manager",
            "tax_included_price",
            "tag_price",
            "size_range",
            "material",
            "composition_en",
            "washing_method",
            "washing_method_en",
            "safety_category",
            "standard_code",
            "size_69",
        ]
    )
    b_default_fields = ",".join(
        [
            "shooting_date",
            "inspection_date",
            "detection_report",
            "size_chart",
            "shipping_warehouse",
            "brand_name",
            "season_year",
            "image_url",
            "style_color",
            "style_code",
            "supplier_style_code",
            "color_name",
            "product_name",
            "category",
            "has_accessories",
            "supplier",
            "supplier_code",
            "cooperation_mode",
            "supply_chain_manager",
            "tax_included_price",
            "tag_price",
            "launch_price",
            "launch_channel",
            "completion_flag",
            "size_range",
            "material",
            "composition_en",
            "washing_method",
            "washing_method_en",
            "safety_category",
            "standard_code",
            "size_69",
        ]
    )
    defaults = {
        "c_visible_field_keys": ",".join(field.key for field in PRODUCT_FIELDS if field.visible_to_c),
        "c_api_token": "",
        "c_field_templates_json": "{}",
        "list_layout_fields_A": a_default_fields,
        "list_layout_fields_B": b_default_fields,
        "list_layout_fields_C": "",
        "list_layout_customized_A": "0",
        "list_layout_customized_B": "0",
        "list_layout_customized_C": "0",
        PLATFORM_SETTINGS_KEY: json.dumps(
            [
                {"code": "tmall", "label": "天猫"},
                {"code": "jd", "label": "京东"},
                {"code": "vip", "label": "唯品"},
                {"code": "douyin", "label": "抖音"},
                {"code": "miniprogram", "label": "小程序"},
            ],
            ensure_ascii=False,
        ),
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


def _platform_configs_from_raw_value(raw_value: str | None) -> list[dict]:
    parsed = []
    if str(raw_value or "").strip():
        try:
            loaded = json.loads(str(raw_value or ""))
        except json.JSONDecodeError:
            loaded = []
        if isinstance(loaded, list):
            parsed = loaded
    cleaned: list[dict] = []
    seen_codes: set[str] = set()
    for item in parsed:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip().lower()
        label = str(item.get("label") or "").strip()
        if not code or not label or code in seen_codes:
            continue
        cleaned.append({"code": code, "label": label})
        seen_codes.add(code)
    return cleaned


def backfill_billing_month_platform_snapshots(connection: sqlite3.Connection) -> None:
    setting_row = connection.execute(
        "SELECT setting_value FROM app_settings WHERE setting_key = ?",
        (PLATFORM_SETTINGS_KEY,),
    ).fetchone()
    default_json = (
        str(setting_row["setting_value"] or "").strip()
        if setting_row and _platform_configs_from_raw_value(setting_row["setting_value"])
        else json.dumps(_fallback_platform_configs(), ensure_ascii=False)
    )
    connection.execute(
        """
        UPDATE billing_months
        SET platforms_json = ?
        WHERE COALESCE(platforms_json, '') = ''
        """,
        (default_json,),
    )


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def parse_utc(value: str | None) -> datetime | None:
    clean_value = normalize_optional_date_text(value)
    if not clean_value:
        return None
    try:
        return datetime.fromisoformat(str(clean_value).replace("Z", "+00:00"))
    except ValueError:
        return None


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


def get_or_create_planning_service_user(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT id FROM users WHERE username = 'planning_service'",
    ).fetchone()
    if row:
        return int(row["id"])
    connection.execute(
        """
        INSERT INTO users (
            username, display_name, department, operating_channel, billing_platforms_json,
            password_hash, is_active, must_change_password, created_at
        ) VALUES ('planning_service', '商品企划中心', 'ADMIN', '', '[]', ?, 0, 0, ?)
        """,
        (hash_password(secrets.token_urlsafe(32)), utc_now()),
    )
    return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def list_users(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, username, display_name, department, operating_channel, billing_platforms_json, is_active, must_change_password, created_at
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
    operating_channel: str = "",
    billing_platform_codes=None,
) -> int:
    clean_department = department.strip()
    clean_operating_channel = operating_channel.strip() if clean_department == "C" else ""
    if billing_platform_codes is None and clean_department == "C":
        billing_platform_codes = (clean_operating_channel,)
    billing_platforms_json = json.dumps(
        list(normalize_billing_platform_codes(billing_platform_codes if clean_department == "C" else ())),
        ensure_ascii=False,
    )
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO users (username, display_name, department, operating_channel, billing_platforms_json, password_hash, is_active, must_change_password, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                username.strip(),
                display_name.strip(),
                clean_department,
                clean_operating_channel,
                billing_platforms_json,
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
    operating_channel: str,
    billing_platform_codes=None,
) -> None:
    clean_department = department.strip()
    clean_operating_channel = operating_channel.strip() if clean_department == "C" else ""
    if billing_platform_codes is None and clean_department == "C":
        billing_platform_codes = (clean_operating_channel,)
    billing_platforms_json = json.dumps(
        list(normalize_billing_platform_codes(billing_platform_codes if clean_department == "C" else ())),
        ensure_ascii=False,
    )
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE users
            SET display_name = ?, department = ?, operating_channel = ?, billing_platforms_json = ?
            WHERE id = ?
            """,
            (
                display_name.strip(),
                clean_department,
                clean_operating_channel,
                billing_platforms_json,
                user_id,
            ),
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
            SELECT id, username, display_name, department, operating_channel, billing_platforms_json, is_active, must_change_password, created_at
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


def normalize_month_key(month_key: str | None) -> str:
    value = str(month_key or "").strip()
    if len(value) != 7 or value[4] != "-":
        raise ValueError("月份格式必须为 YYYY-MM。")
    year_part, month_part = value.split("-", 1)
    if not (year_part.isdigit() and month_part.isdigit()):
        raise ValueError("月份格式必须为 YYYY-MM。")
    year = int(year_part)
    month = int(month_part)
    if year < 2000 or year > 2100 or month < 1 or month > 12:
        raise ValueError("月份不在可用范围内。")
    return f"{year:04d}-{month:02d}"


def platform_bill_platform_codes() -> tuple[str, ...]:
    return PLATFORM_BILL_PLATFORM_ORDER


def platform_bill_platform_label(platform_code: str) -> str:
    return PLATFORM_BILL_PLATFORM_LABELS.get(platform_code, platform_code or "未知平台")


def _fallback_platform_configs() -> list[dict]:
    return [
        {"code": code, "label": PLATFORM_BILL_PLATFORM_LABELS.get(code, code)}
        for code in PLATFORM_BILL_PLATFORM_ORDER
    ]


def normalize_platform_code(value: str, existing_codes: set[str] | None = None) -> str:
    base = "".join(
        ch.lower()
        for ch in str(value or "").strip()
        if ch.isascii() and (ch.isalnum() or ch == "_")
    )
    if not base:
        base = "platform"
    if base[0].isdigit():
        base = f"platform_{base}"
    candidate = base
    seen = {str(code or "").strip().lower() for code in (existing_codes or set()) if str(code or "").strip()}
    index = 2
    while candidate in seen:
        candidate = f"{base}_{index}"
        index += 1
    return candidate


def platform_bill_platform_configs(db_path: str | Path) -> list[dict]:
    raw_value = get_setting(db_path, PLATFORM_SETTINGS_KEY) or ""
    cleaned = _platform_configs_from_raw_value(raw_value)
    if cleaned:
        return cleaned
    return _fallback_platform_configs()


def save_platform_bill_platform_configs(db_path: str | Path, rows: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    seen_codes: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or "").strip()
        raw_code = str(row.get("code") or "").strip().lower()
        code = normalize_platform_code(raw_code or label, seen_codes)
        if not label or code in seen_codes:
            continue
        cleaned.append({"code": code, "label": label})
        seen_codes.add(code)
    if not cleaned:
        cleaned = _fallback_platform_configs()
    set_setting(db_path, PLATFORM_SETTINGS_KEY, json.dumps(cleaned, ensure_ascii=False))
    return cleaned


def platform_bill_platform_configs_for_month(db_path: str | Path, month_key: str) -> list[dict]:
    month = get_billing_month_by_key(db_path, month_key)
    if month:
        cleaned = _platform_configs_from_raw_value(month.get("platforms_json"))
        if cleaned:
            return cleaned
    return platform_bill_platform_configs(db_path)


def update_billing_month_platform_configs(
    db_path: str | Path,
    month_key: str,
    rows: list[dict],
    *,
    update_default: bool = True,
) -> list[dict]:
    cleaned = save_platform_bill_platform_configs(db_path, rows) if update_default else []
    if not update_default:
        cleaned = []
        seen_codes: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            label = str(row.get("label") or "").strip()
            raw_code = str(row.get("code") or "").strip().lower()
            code = normalize_platform_code(raw_code or label, seen_codes)
            if not label or code in seen_codes:
                continue
            cleaned.append({"code": code, "label": label})
            seen_codes.add(code)
        if not cleaned:
            cleaned = platform_bill_platform_configs(db_path)
    normalized_month_key = normalize_month_key(month_key)
    with get_connection(db_path) as connection:
        month = get_or_create_billing_month(connection, normalized_month_key, 1)
        connection.execute(
            "UPDATE billing_months SET platforms_json = ?, updated_at = ? WHERE id = ?",
            (json.dumps(cleaned, ensure_ascii=False), utc_now(), month["id"]),
        )
    return cleaned


def platform_bill_platform_codes_for_db(db_path: str | Path) -> tuple[str, ...]:
    return tuple(item["code"] for item in platform_bill_platform_configs(db_path))


def platform_bill_platform_label_for_db(db_path: str | Path, platform_code: str) -> str:
    for item in platform_bill_platform_configs(db_path):
        if item["code"] == platform_code:
            return item["label"]
    return platform_bill_platform_label(platform_code)


def platform_bill_file_role_label(file_role: str) -> str:
    return PLATFORM_BILL_FILE_ROLE_LABELS.get(file_role, file_role or "文件")


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
        if field.key in PRODUCT_DATE_FIELD_KEYS:
            normalized[field.key] = normalize_optional_date_text(raw_values.get(field.key))
            continue
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


def has_meaningful_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def product_has_image(product: dict) -> bool:
    raw_gallery = product.get("image_gallery_json")
    if isinstance(raw_gallery, str) and raw_gallery.strip():
        try:
            parsed = json.loads(raw_gallery)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list) and any(str(item).strip() for item in parsed):
            return True
    return has_meaningful_value(product.get("image_url"))


def parsed_size_tokens(size_range: str | None) -> set[str]:
    raw_value = str(size_range or "").upper()
    separators = ["/", "、", ",", "，", " ", "\n", "\t", ";", "；"]
    normalized = raw_value
    for separator in separators:
        normalized = normalized.replace(separator, "|")
    tokens = {token.strip() for token in normalized.split("|") if token.strip()}
    aliases = {
        "F": "size_f",
        "69码": "size_69",
        "69": "size_69",
        "S": "size_s",
        "M": "size_m",
        "L": "size_l",
        "XL": "size_xl",
        "2XL": "size_2xl",
        "XXL": "size_2xl",
        "3XL": "size_3xl",
        "XXXL": "size_3xl",
    }
    return {aliases[token] for token in tokens if token in aliases}


def completion_required_field_keys(product: dict) -> list[str]:
    required = [
        "shooting_date",
        "inspection_date",
        "detection_report",
        "shipping_warehouse",
        "brand_name",
        "season_year",
        "image_url",
        "style_color",
        "style_code",
        "color_name",
        "product_name",
        "category",
        "has_accessories",
        "supplier",
        "cooperation_mode",
        "supply_chain_manager",
        "tax_included_price",
        "tag_price",
        "size_range",
        "material",
        "composition_en",
        "washing_method",
        "washing_method_en",
        "safety_category",
        "standard_code",
    ]
    size_keys = sorted(parsed_size_tokens(product.get("size_range")))
    required.extend(size_keys)
    return required


def completion_missing_field_keys(product: dict, excluded_keys=None) -> list[str]:
    missing = []
    excluded = set(excluded_keys or ())
    for field_key in completion_required_field_keys(product):
        if field_key in excluded:
            continue
        if field_key in PRODUCT_DATE_FIELD_KEYS:
            if normalize_optional_date_text(product.get(field_key)):
                continue
            missing.append(field_key)
            continue
        if field_key == "image_url":
            if product_has_image(product):
                continue
        if has_meaningful_value(product.get(field_key)):
            continue
        missing.append(field_key)
    return missing


def backfill_placeholder_product_dates(connection: sqlite3.Connection) -> None:
    marker_values = tuple(EMPTY_DATE_MARKERS)
    for field_key in PRODUCT_DATE_FIELD_KEYS:
        connection.execute(
            f"""
            UPDATE products
            SET {field_key} = NULL
            WHERE {field_key} IS NOT NULL
              AND (
                TRIM({field_key}) IN ({",".join("?" for _ in marker_values)})
                OR TRIM(REPLACE({field_key}, '/', '-')) LIKE '1900-01-0%'
                OR TRIM(REPLACE({field_key}, '/', '-')) LIKE '1899-12-3%'
              )
            """,
            marker_values,
        )


def completion_flag(product: dict) -> str:
    return "Y" if not completion_missing_field_keys(product) else ""


def compute_elapsed_days(created_at: str | None, completed_to_c_at: str | None) -> int | None:
    created_dt = parse_utc(created_at)
    if not created_dt:
        return None
    end_dt = parse_utc(completed_to_c_at)
    if not end_dt:
        end_dt = datetime.now(UTC).replace(microsecond=0)
    if end_dt < created_dt:
        end_dt = created_dt
    return (end_dt.date() - created_dt.date()).days + 1


def product_snapshot_from_payload(payload: dict, product: dict | None = None) -> dict:
    snapshot = {}
    for field in PRODUCT_FIELDS:
        snapshot[field.key] = payload.get(field.key)
    snapshot["image_gallery_json"] = payload.get("image_gallery_json", "[]")
    if product:
        snapshot["status"] = product.get("status")
        snapshot["lifecycle_status"] = product.get("lifecycle_status")
        snapshot["completed_to_c_at"] = product.get("completed_to_c_at")
        snapshot["owner_department"] = product.get("owner_department")
        snapshot["created_by"] = product.get("created_by")
    return snapshot


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
        "revision_flag",
        "created_at",
        "updated_at",
    ]
    values = [payload[field.key] for field in PRODUCT_FIELDS]
    values.append(payload["image_gallery_json"])
    values.extend([owner_department, created_by, "draft", "active", 0, timestamp, timestamp])
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(
        f"INSERT INTO products ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    product_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
    record_product_version(
        connection,
        product_id,
        version_no=1,
        snapshot=product_snapshot_from_payload(
            payload,
            {
                "status": "draft",
                "lifecycle_status": "active",
                "owner_department": owner_department,
                "created_by": created_by,
            },
        ),
        summary_json=[],
        change_count=0,
        created_by=created_by,
        note="初始版本",
    )
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
    diff_items = build_product_diff(before_product, payload)
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
        if not diff_items:
            return
        next_status = before_product.get("status") or "draft"
        reset_reviewer = False
        revision_flag = int(before_product.get("revision_flag") or 0)
        next_version_no = int(before_product.get("current_version_no") or 1) + 1
        actor_department_row = connection.execute(
            "SELECT department FROM users WHERE id = ?",
            (actor_user_id,),
        ).fetchone()
        actor_department = actor_department_row["department"] if actor_department_row else ""
        if actor_department == "A":
            if before_product.get("status") == "draft":
                next_status = "draft"
                revision_flag = 0
            elif before_product.get("status") in {"pending", "published", "received"}:
                next_status = before_product.get("status")
                revision_flag = 1
        if actor_department == "B" and before_product.get("status") in {"pending", "published"}:
            next_status = "pending"
            reset_reviewer = before_product.get("status") == "published"
            revision_flag = 0
        connection.execute(
            """
            UPDATE products
            SET status = ?, revision_flag = ?, current_version_no = ?, last_reviewed_by = ?, last_reviewed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                next_status,
                revision_flag,
                next_version_no,
                None if reset_reviewer else before_product.get("last_reviewed_by"),
                None if reset_reviewer else before_product.get("last_reviewed_at"),
                timestamp,
                product_id,
            ),
        )
        summary_items = summarize_diff_items(diff_items)
        record_product_version(
            connection,
            product_id,
            version_no=next_version_no,
            snapshot=product_snapshot_from_payload(
                payload,
                {
                    **before_product,
                    "status": next_status,
                    "lifecycle_status": before_product.get("lifecycle_status"),
                },
            ),
            summary_json=summary_items,
            change_count=len(diff_items),
            created_by=actor_user_id,
            note="资料修改后生成的新版本",
        )
        details = "编辑了商品资料字段内容，并将状态重置为 A 填写阶段等待再次提交流转。"
        if actor_department == "A" and before_product.get("status") == "pending":
            details = "跟单部在已提交给商品部后更新了主体资料，资料继续保留在待商品部填写阶段，并标记为已更新。"
        elif actor_department == "A" and before_product.get("status") == "published":
            details = "跟单部在已完成资料上更新了主体资料，资料继续保持已完成状态，并标记为已更新。"
        elif actor_department == "A" and before_product.get("status") == "received":
            details = "跟单部在运营部已接收的资料上更新了主体资料，资料保持已接收状态并标记为已更新；重新提交后将再次流转给商品部和运营部。"
        if actor_department == "B" and before_product.get("status") == "pending":
            details = "补充了 B 部门负责的品类、图片、上新价格、上新渠道和资料完成字段，资料仍保留在待B填写阶段。"
        elif actor_department == "B" and before_product.get("status") == "published":
            details = "更新了 B 部门负责的品类、图片、上新价格、上新渠道和资料完成字段，资料已重新进入待B填写阶段，完成后可再次开放给 C。"
        log_product_action(
            connection,
            product_id,
            actor_user_id,
            "update",
            "更新资料",
            f"{details} 当前修改版本 V{next_version_no}。",
            diff_json=summary_items,
            diff_count=len(diff_items),
        )


def record_product_version(
    connection: sqlite3.Connection,
    product_id: int,
    version_no: int,
    snapshot: dict,
    summary_json: list[dict],
    change_count: int,
    created_by: int,
    note: str = "",
    source_version_no: int | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO product_versions (
            product_id, version_no, snapshot_json, summary_json, change_count, source_version_no, created_by, created_at, note
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product_id,
            version_no,
            json.dumps(snapshot, ensure_ascii=False),
            json.dumps(summary_json or [], ensure_ascii=False),
            int(change_count or 0),
            source_version_no,
            created_by,
            utc_now(),
            note,
        ),
    )


def log_product_action(
    connection: sqlite3.Connection,
    product_id: int,
    actor_user_id: int,
    action: str,
    action_label: str,
    details: str,
    diff_json: list[dict] | None = None,
    diff_count: int = 0,
) -> None:
    connection.execute(
        """
        INSERT INTO product_logs (product_id, actor_user_id, action, action_label, details, diff_json, diff_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product_id,
            actor_user_id,
            action,
            action_label,
            details,
            json.dumps(diff_json or [], ensure_ascii=False),
            int(diff_count or 0),
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
    revision_flag_override: int | None = None,
) -> None:
    current_row = connection.execute(
        "SELECT status, completed_to_c_at, c_release_no, received_by, received_at FROM products WHERE id = ?",
        (product_id,),
    ).fetchone()
    current_completed_to_c_at = current_row["completed_to_c_at"] if current_row else None
    timestamp = utc_now()
    completed_to_c_at = current_completed_to_c_at
    received_by = current_row["received_by"] if current_row else None
    received_at = current_row["received_at"] if current_row else None
    c_release_no = int(current_row["c_release_no"] or 0) if current_row else 0
    revision_flag = 0 if revision_flag_override is None else int(revision_flag_override)
    if status == "published":
        if not current_completed_to_c_at:
            completed_to_c_at = timestamp
        if not current_row or current_row["status"] != "published":
            c_release_no += 1
    if status == "received":
        received_by = actor_user_id
        received_at = timestamp
    if status in {"draft", "pending", "published"}:
        received_by = None
        received_at = None
    if status in {"draft", "pending"}:
        completed_to_c_at = None
    connection.execute(
        """
        UPDATE products
        SET status = ?, revision_flag = ?, last_reviewed_by = ?, last_reviewed_at = ?, completed_to_c_at = ?, c_release_no = ?, received_by = ?, received_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, revision_flag, actor_user_id, timestamp, completed_to_c_at, c_release_no, received_by, received_at, timestamp, product_id),
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


def summarize_diff_items(diff_items: list[dict], limit: int = 3) -> list[dict]:
    summary = []
    for item in diff_items[:limit]:
        summary.append(
            {
                "field_key": item.get("field_key", ""),
                "field_label": item.get("field_label", ""),
                "before": "",
                "after": "",
            }
        )
    return summary


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


def _planning_source_query() -> str:
    return """
        SELECT p.*, u.display_name AS creator_name, u.username AS creator_username,
               reviewer.display_name AS reviewer_name
        FROM products p
        JOIN users u ON u.id = p.created_by
        LEFT JOIN users reviewer ON reviewer.id = p.last_reviewed_by
        WHERE p.lifecycle_status = 'active'
          AND p.status IN ('pending', 'published', 'received')
          AND EXISTS (
              SELECT 1 FROM product_logs pl
              WHERE pl.product_id = p.id AND pl.action = 'status:pending'
          )
    """


def list_planning_source_products(db_path: str | Path, product_id: int | None = None) -> list[dict]:
    query = _planning_source_query()
    params: list[object] = []
    if product_id is not None:
        query += " AND p.id = ?"
        params.append(int(product_id))
    query += " ORDER BY p.updated_at DESC, p.id DESC"
    with get_connection(db_path) as connection:
        rows = connection.execute(query, params).fetchall()
    return [dict(row) for row in rows]


def _planning_product_payload(product: dict) -> dict:
    return {
        "id": int(product["id"]),
        "style_code": product.get("style_code") or "",
        "style_color": product.get("style_color") or "",
        "image_url": product.get("image_url") or "",
        "color_name": product.get("color_name") or "",
        "product_name": product.get("product_name") or "",
        "brand_name": product.get("brand_name") or "",
        "season_year": product.get("season_year") or "",
        "supplier": product.get("supplier") or "",
        "supplier_code": product.get("supplier_code") or "",
        "supplier_style_code": product.get("supplier_style_code") or "",
        "category": product.get("category") or "",
        "actual_cost": product.get("tax_included_price"),
        "tax_included_price": product.get("tax_included_price"),
        "status": product.get("status") or "",
        "lifecycle_status": product.get("lifecycle_status") or "",
        "source_version_no": int(product.get("current_version_no") or 1),
        "updated_at": product.get("updated_at") or "",
        "created_at": product.get("created_at") or "",
        "creator_name": product.get("creator_name") or "",
    }


def planning_source_payloads(db_path: str | Path, product_id: int | None = None) -> list[dict]:
    return [_planning_product_payload(product) for product in list_planning_source_products(db_path, product_id)]


def publish_planning_price(
    connection: sqlite3.Connection,
    product_id: int,
    payload: dict,
    actor_user_id: int,
) -> dict:
    product_row = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    product = row_to_dict(product_row)
    if not product:
        raise LookupError("商品资料不存在。")
    if product.get("lifecycle_status") != "active" or product.get("status") not in {"pending", "published", "received"}:
        raise ValueError("当前商品尚未提交到商品部，不能接收商品企划回传。")
    eligible = connection.execute(
        "SELECT 1 FROM product_logs WHERE product_id = ? AND action = 'status:pending' LIMIT 1",
        (product_id,),
    ).fetchone()
    if not eligible:
        raise ValueError("当前商品没有有效的提交商品部记录。")
    publication_id = str(payload.get("publication_id") or "").strip()
    if not publication_id:
        raise ValueError("回传必须包含企划定价记录号。")
    duplicate = connection.execute(
        "SELECT product_id FROM planning_publications WHERE publication_id = ? LIMIT 1",
        (publication_id,),
    ).fetchone()
    if duplicate:
        if int(duplicate["product_id"]) != int(product_id):
            raise ValueError("企划定价记录号已被其他商品使用。")
        return {"status": "already_published", "product_id": product_id, "publication_id": publication_id}
    expected_version = int(payload.get("source_version_no") or 0)
    current_version = int(product.get("current_version_no") or 1)
    if expected_version != current_version:
        error = ValueError(f"商品资料已发生变化，请重新同步后定价。当前版本为 V{current_version}。")
        error.code = "version_conflict"
        raise error
    category = str(payload.get("category") or "").strip()
    if not category:
        raise ValueError("回传必须包含品类。")
    try:
        launch_price = float(payload.get("launch_price"))
    except (TypeError, ValueError):
        raise ValueError("回传上新价格必须是数字。")
    if launch_price <= 0:
        raise ValueError("回传上新价格必须大于 0。")
    next_version = current_version + 1
    after = dict(product)
    after["category"] = category
    after["launch_price"] = launch_price
    diff_items = build_product_diff(product, after)
    timestamp = utc_now()
    connection.execute(
        """
        UPDATE products
        SET category = ?, launch_price = ?, current_version_no = ?, revision_flag = 0,
            last_reviewed_by = ?, last_reviewed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (category, launch_price, next_version, actor_user_id, timestamp, timestamp, product_id),
    )
    record_product_version(
        connection,
        product_id,
        version_no=next_version,
        snapshot=product_snapshot_from_payload(
            {**product, "category": category, "launch_price": launch_price},
            {**product, "current_version_no": next_version},
        ),
        summary_json=summarize_diff_items(diff_items),
        change_count=len(diff_items),
        created_by=actor_user_id,
        source_version_no=expected_version,
        note=f"商品企划中心回传定价 {publication_id}",
    )
    operator_name = str(payload.get("operator_name") or "商品企划中心").strip() or "商品企划中心"
    connection.execute(
        """
        INSERT INTO planning_publications (
            product_id, publication_id, source_version_no, category, launch_price,
            fixed_multiplier, supplier_coefficient, raw_price, operator_name, published_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            product_id,
            publication_id,
            expected_version,
            category,
            launch_price,
            payload.get("fixed_multiplier"),
            payload.get("supplier_coefficient"),
            payload.get("raw_price"),
            operator_name,
            str(payload.get("published_at") or timestamp),
        ),
    )
    log_product_action(
        connection,
        product_id,
        actor_user_id,
        "planning_publish",
        "接收商品企划定价",
        f"商品企划中心回传定价记录 {publication_id}，品类：{category}，上新价格：{launch_price:g}，来源资料 V{expected_version}。",
        diff_json=summarize_diff_items(diff_items),
        diff_count=len(diff_items),
    )
    return {
        "status": "published",
        "product_id": product_id,
        "publication_id": publication_id,
        "source_version_no": expected_version,
        "current_version_no": next_version,
        "category": category,
        "launch_price": launch_price,
        "published_at": str(payload.get("published_at") or timestamp),
    }


def get_product_version(db_path: str | Path, product_id: int, version_no: int) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT v.*, u.display_name AS actor_name, u.department AS actor_department
            FROM product_versions v
            JOIN users u ON u.id = v.created_by
            WHERE v.product_id = ? AND v.version_no = ?
            """,
            (product_id, version_no),
        ).fetchone()
    item = row_to_dict(row)
    if not item:
        return None
    item["snapshot"] = json.loads(item.get("snapshot_json") or "{}")
    item["summary_items"] = json.loads(item.get("summary_json") or "[]")
    item["change_count"] = int(item.get("change_count") or 0)
    return item


def list_product_versions(db_path: str | Path, product_id: int) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT v.*, u.display_name AS actor_name, u.department AS actor_department
            FROM product_versions v
            JOIN users u ON u.id = v.created_by
            WHERE v.product_id = ?
            ORDER BY v.version_no DESC, v.id DESC
            """,
            (product_id,),
        ).fetchall()
    versions = []
    for row in rows:
        item = dict(row)
        item["snapshot"] = json.loads(item.get("snapshot_json") or "{}")
        item["summary_items"] = json.loads(item.get("summary_json") or "[]")
        item["change_count"] = int(item.get("change_count") or 0)
        versions.append(item)
    return versions


def restore_product_version(
    connection: sqlite3.Connection,
    product_id: int,
    target_version_no: int,
    actor_user_id: int,
) -> int:
    product_row = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    product = row_to_dict(product_row) or {}
    version_row = connection.execute(
        "SELECT * FROM product_versions WHERE product_id = ? AND version_no = ?",
        (product_id, target_version_no),
    ).fetchone()
    if not version_row:
        raise ValueError("没有找到要恢复的版本。")
    version = row_to_dict(version_row) or {}
    snapshot = json.loads(version.get("snapshot_json") or "{}")
    payload = normalize_product_data(snapshot)
    next_version_no = int(product.get("current_version_no") or 1) + 1
    assignments = ", ".join([*(f"{field.key} = ?" for field in PRODUCT_FIELDS), "image_gallery_json = ?"])
    values = [payload[field.key] for field in PRODUCT_FIELDS]
    values.append(payload["image_gallery_json"])
    timestamp = utc_now()
    values.extend(
        [
            snapshot.get("status") or product.get("status") or "draft",
            snapshot.get("lifecycle_status") or product.get("lifecycle_status") or "active",
            snapshot.get("completed_to_c_at") or product.get("completed_to_c_at"),
            1 if next_version_no > 1 else 0,
            next_version_no,
            actor_user_id,
            timestamp,
            timestamp,
            product_id,
        ]
    )
    connection.execute(
        f"""
        UPDATE products
        SET {assignments},
            status = ?,
            lifecycle_status = ?,
            completed_to_c_at = ?,
            revision_flag = ?,
            current_version_no = ?,
            last_reviewed_by = ?,
            last_reviewed_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        values,
    )
    diff_items = build_product_diff(product, payload)
    summary_items = summarize_diff_items(diff_items)
    record_product_version(
        connection,
        product_id,
        version_no=next_version_no,
        snapshot=product_snapshot_from_payload(
            payload,
            {
                **product,
                "status": snapshot.get("status") or product.get("status"),
                "lifecycle_status": snapshot.get("lifecycle_status") or product.get("lifecycle_status"),
                "completed_to_c_at": snapshot.get("completed_to_c_at") or product.get("completed_to_c_at"),
            },
        ),
        summary_json=summary_items,
        change_count=len(diff_items),
        created_by=actor_user_id,
        source_version_no=target_version_no,
        note=f"管理员恢复自 V{target_version_no}",
    )
    log_product_action(
        connection,
        product_id,
        actor_user_id,
        "version_restore",
        "恢复版本",
        f"管理员将资料恢复到 V{target_version_no}，并生成当前版本 V{next_version_no}。",
        diff_json=summary_items,
        diff_count=len(diff_items),
    )
    return next_version_no


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
        sanitized_values = dict(raw_values)
        for field in PRODUCT_FIELDS:
            if field.key in B_STAGE_FIELD_KEYS:
                sanitized_values[field.key] = existing.get(field.key) if existing else ""
                continue
            if field.key not in sanitized_values and existing:
                sanitized_values[field.key] = existing.get(field.key)
        if existing:
            sanitized_values["image_gallery_json"] = existing.get("image_gallery_json") or "[]"
        if existing:
            update_product(connection, existing["id"], sanitized_values, created_by)
            return "updated", existing["id"]
        product_id = create_product(connection, sanitized_values, created_by, owner_department)
        return "created", product_id


def find_matching_products_for_import(
    connection: sqlite3.Connection,
    style_code: str | None,
    style_color: str | None,
    color_name: str | None,
    product_name: str | None,
) -> list[dict]:
    clean_style_code = str(style_code or "").strip()
    clean_product_name = str(product_name or "").strip()
    clean_style_color = str(style_color or "").strip()
    clean_color_name = str(color_name or "").strip()
    if not clean_style_code or not clean_product_name:
        return []
    rows = connection.execute(
        """
        SELECT *
        FROM products
        WHERE lifecycle_status = 'active'
          AND COALESCE(style_code, '') = ?
          AND COALESCE(product_name, '') = ?
        ORDER BY id DESC
        """,
        (clean_style_code, clean_product_name),
    ).fetchall()
    candidates = [row_to_dict(row) for row in rows]
    if clean_style_color:
        style_color_matches = [
            item for item in candidates
            if str(item.get("style_color") or "").strip() == clean_style_color
        ]
        if style_color_matches:
            return style_color_matches
    if clean_color_name:
        color_name_matches = [
            item for item in candidates
            if str(item.get("color_name") or "").strip() == clean_color_name
        ]
        if color_name_matches:
            return color_name_matches
    return candidates


def list_products(
    db_path: str | Path,
    query: str = "",
    department: str = "",
    status: str = "",
    lifecycle_status: str = "",
    supplier: str = "",
) -> list[dict]:
    like_query = f"%{query.strip()}%"
    supplier_query = f"%{supplier.strip()}%"
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
            AND (? = '' OR COALESCE(p.supplier, '') LIKE ?)
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
                supplier.strip(),
                supplier_query,
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
    stats = {"draft": 0, "pending": 0, "published": 0, "received": 0}
    for row in rows:
        stats[row["status"]] = row["total"]
    return stats


def b_workflow_stats(db_path: str | Path, days: int = 7) -> dict[str, int]:
    """Return the current queue and recent handoff activity relevant to B."""
    cutoff = iso_days_ago(days)
    with get_connection(db_path) as connection:
        current = connection.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN status IN ('published', 'received') THEN 1 ELSE 0 END), 0) AS completed,
                COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_completion,
                COALESCE(SUM(CASE WHEN status = 'published' THEN 1 ELSE 0 END), 0) AS awaiting_receipt
            FROM products
            WHERE lifecycle_status = 'active'
            """
        ).fetchone()
        recent = connection.execute(
            """
            SELECT
                COUNT(DISTINCT CASE
                    WHEN log.action = 'status:pending' AND actor.department = 'A' THEN log.product_id
                END) AS recent_submitted_to_b,
                COUNT(DISTINCT CASE
                    WHEN log.action = 'status:draft' AND actor.department = 'B' THEN log.product_id
                END) AS recent_returned_to_a
            FROM product_logs log
            JOIN users actor ON actor.id = log.actor_user_id
            JOIN products product ON product.id = log.product_id
            WHERE log.created_at >= ?
              AND product.lifecycle_status = 'active'
            """,
            (cutoff,),
        ).fetchone()
    return {
        "completed": int(current["completed"] or 0),
        "recent_submitted_to_b": int(recent["recent_submitted_to_b"] or 0),
        "pending_completion": int(current["pending_completion"] or 0),
        "awaiting_receipt": int(current["awaiting_receipt"] or 0),
        "recent_returned_to_a": int(recent["recent_returned_to_a"] or 0),
    }


def c_receipt_stats(db_path: str | Path) -> dict[str, int]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT status, COUNT(*) AS total
            FROM products
            WHERE owner_department IN ('A', 'B')
              AND lifecycle_status = 'active'
              AND status IN ('published', 'received')
            GROUP BY status
            """
        ).fetchall()
    stats = {"published": 0, "received": 0}
    for row in rows:
        stats[row["status"]] = row["total"]
    return stats


def c_department_receipt_stats(db_path: str | Path, days: int = 7) -> dict[str, int]:
    """Aggregate operating-department receipt progress for administrator monitoring."""
    cutoff = iso_days_ago(days)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status = 'received' THEN 1 ELSE 0 END), 0) AS received,
                COALESCE(SUM(CASE WHEN status = 'published' THEN 1 ELSE 0 END), 0) AS pending,
                COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) AS recent_created
            FROM products
            WHERE owner_department IN ('A', 'B')
              AND lifecycle_status = 'active'
              AND status IN ('published', 'received')
            """,
            (cutoff,),
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "received": int(row["received"] or 0),
        "pending": int(row["pending"] or 0),
        "recent_created": int(row["recent_created"] or 0),
    }


def c_receipt_release_numbers(db_path: str | Path, recipient_user_id: int) -> dict[int, set[int]]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT product_id, release_no
            FROM product_c_receipts
            WHERE recipient_user_id = ?
            """,
            (recipient_user_id,),
        ).fetchall()
    releases: dict[int, set[int]] = {}
    for row in rows:
        releases.setdefault(int(row["product_id"]), set()).add(int(row["release_no"]))
    return releases


def c_product_received_by_user(db_path: str | Path, product_id: int, recipient_user_id: int, release_no: int) -> bool:
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT 1
            FROM product_c_receipts
            WHERE product_id = ? AND recipient_user_id = ? AND release_no = ?
            """,
            (product_id, recipient_user_id, release_no),
        ).fetchone()
    return bool(row)


def record_c_product_receipt(
    connection: sqlite3.Connection,
    product_id: int,
    recipient_user_id: int,
    release_no: int,
) -> bool:
    result = connection.execute(
        """
        INSERT OR IGNORE INTO product_c_receipts (product_id, recipient_user_id, release_no, received_at)
        VALUES (?, ?, ?, ?)
        """,
        (product_id, recipient_user_id, release_no, utc_now()),
    )
    return result.rowcount > 0


def c_user_receipt_stats(db_path: str | Path, user: dict) -> dict[str, int]:
    visible_channels = c_visible_launch_channels(user)
    if not visible_channels:
        return {"total": 0, "received": 0, "pending": 0, "recent_created": 0}
    placeholders = ", ".join("?" for _ in visible_channels)
    cutoff = iso_days_ago(7)
    with get_connection(db_path) as connection:
        row = connection.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN receipt.product_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS received,
                COALESCE(SUM(CASE WHEN receipt.product_id IS NULL THEN 1 ELSE 0 END), 0) AS pending,
                COALESCE(SUM(CASE WHEN p.created_at >= ? THEN 1 ELSE 0 END), 0) AS recent_created
            FROM products p
            LEFT JOIN product_c_receipts receipt
              ON receipt.product_id = p.id
             AND receipt.recipient_user_id = ?
             AND receipt.release_no = p.c_release_no
            WHERE p.owner_department IN ('A', 'B')
              AND p.lifecycle_status = 'active'
              AND p.status IN ('published', 'received')
              AND p.launch_channel IN ({placeholders})
            """,
            (cutoff, int(user["id"]), *visible_channels),
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "received": int(row["received"] or 0),
        "pending": int(row["pending"] or 0),
        "recent_created": int(row["recent_created"] or 0),
    }


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
        item["change_count"] = int(item.get("diff_count") or len(item["diff_items"]))
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
        item["change_count"] = int(item.get("diff_count") or len(item["diff_items"]))
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
        item["change_count"] = 0
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


def get_or_create_billing_month(connection: sqlite3.Connection, month_key: str, actor_user_id: int) -> dict:
    normalized_month_key = normalize_month_key(month_key)
    row = connection.execute(
        "SELECT * FROM billing_months WHERE month_key = ?",
        (normalized_month_key,),
    ).fetchone()
    if row:
        return dict(row)
    timestamp = utc_now()
    settings_row = connection.execute(
        "SELECT setting_value FROM app_settings WHERE setting_key = ?",
        (PLATFORM_SETTINGS_KEY,),
    ).fetchone()
    default_platforms_json = (
        str(settings_row["setting_value"] or "").strip()
        if settings_row and _platform_configs_from_raw_value(settings_row["setting_value"])
        else json.dumps(_fallback_platform_configs(), ensure_ascii=False)
    )
    connection.execute(
        """
        INSERT INTO billing_months (month_key, status, platforms_json, created_by, submitted_by, submitted_at, created_at, updated_at)
        VALUES (?, 'draft', ?, ?, NULL, NULL, ?, ?)
        """,
        (normalized_month_key, default_platforms_json, actor_user_id, timestamp, timestamp),
    )
    created_row = connection.execute(
        "SELECT * FROM billing_months WHERE month_key = ?",
        (normalized_month_key,),
    ).fetchone()
    return dict(created_row)


def get_billing_month_by_key(db_path: str | Path, month_key: str) -> dict | None:
    normalized_month_key = normalize_month_key(month_key)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT bm.*,
                   creator.display_name AS creator_name,
                   creator.department AS creator_department,
                   submitter.display_name AS submitter_name,
                   submitter.department AS submitter_department
            FROM billing_months bm
            JOIN users creator ON creator.id = bm.created_by
            LEFT JOIN users submitter ON submitter.id = bm.submitted_by
            WHERE bm.month_key = ?
            """,
            (normalized_month_key,),
        ).fetchone()
    return row_to_dict(row)


def list_billing_months(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT bm.*,
                   creator.display_name AS creator_name,
                   submitter.display_name AS submitter_name
            FROM billing_months bm
            JOIN users creator ON creator.id = bm.created_by
            LEFT JOIN users submitter ON submitter.id = bm.submitted_by
            ORDER BY bm.month_key DESC, bm.id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_platform_bill_file_by_id(db_path: str | Path, file_id: int) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT pbf.*, bm.month_key, bm.status AS month_status,
                   uploader.display_name AS uploader_name,
                   uploader.department AS uploader_department,
                   submitter.display_name AS submitter_name,
                   submitter.department AS submitter_department
            FROM platform_bill_files pbf
            JOIN billing_months bm ON bm.id = pbf.billing_month_id
            JOIN users uploader ON uploader.id = pbf.uploaded_by
            LEFT JOIN users submitter ON submitter.id = pbf.submitted_by
            WHERE pbf.id = ?
            """,
            (file_id,),
        ).fetchone()
    return row_to_dict(row)


def delete_platform_bill_file(connection: sqlite3.Connection, file_id: int) -> dict | None:
    row = connection.execute(
        "SELECT * FROM platform_bill_files WHERE id = ?",
        (file_id,),
    ).fetchone()
    if not row:
        return None
    file_item = dict(row)
    connection.execute("DELETE FROM platform_bill_files WHERE id = ?", (file_id,))
    connection.execute(
        "UPDATE billing_months SET updated_at = ? WHERE id = ?",
        (utc_now(), file_item["billing_month_id"]),
    )
    return file_item


def replace_platform_main_file(
    connection: sqlite3.Connection,
    billing_month_id: int,
    platform_code: str,
    original_filename: str,
    stored_path: str,
    uploaded_by: int,
) -> dict:
    month_row = connection.execute(
        "SELECT month_key, platforms_json FROM billing_months WHERE id = ?",
        (billing_month_id,),
    ).fetchone()
    month_configs = _platform_configs_from_raw_value(month_row["platforms_json"] if month_row else "")
    if platform_code not in {item["code"] for item in month_configs}:
        raise ValueError("平台编码不合法。")
    current_rows = connection.execute(
        """
        SELECT *
        FROM platform_bill_files
        WHERE billing_month_id = ? AND platform_code = ? AND file_role = 'main' AND is_current = 1
        ORDER BY id DESC
        """,
        (billing_month_id, platform_code),
    ).fetchall()
    current_version_row = connection.execute(
        """
        SELECT MAX(version_no) AS version_no
        FROM platform_bill_files
        WHERE billing_month_id = ? AND platform_code = ? AND is_current = 1
        """,
        (billing_month_id, platform_code),
    ).fetchone()
    current_version_no = int(current_version_row["version_no"] or 0) if current_version_row else 0
    if not current_version_no:
        history_version_row = connection.execute(
            """
            SELECT MAX(version_no) AS version_no
            FROM platform_bill_files
            WHERE billing_month_id = ? AND platform_code = ?
            """,
            (billing_month_id, platform_code),
        ).fetchone()
        current_version_no = int(history_version_row["version_no"] or 0) + 1 if history_version_row else 1
    for row in current_rows:
        connection.execute("DELETE FROM platform_bill_files WHERE id = ?", (row["id"],))
    timestamp = utc_now()
    connection.execute(
        """
        INSERT INTO platform_bill_files (
            billing_month_id, platform_code, file_role, original_filename, stored_path, uploaded_by, version_no, is_current, created_at
        )
        VALUES (?, ?, 'main', ?, ?, ?, ?, 1, ?)
        """,
        (billing_month_id, platform_code, original_filename, stored_path, uploaded_by, current_version_no, timestamp),
    )
    connection.execute(
        "UPDATE billing_months SET updated_at = ? WHERE id = ?",
        (timestamp, billing_month_id),
    )
    row = connection.execute("SELECT * FROM platform_bill_files WHERE id = last_insert_rowid()").fetchone()
    return dict(row)


def add_platform_attachment(
    connection: sqlite3.Connection,
    billing_month_id: int,
    platform_code: str,
    original_filename: str,
    stored_path: str,
    uploaded_by: int,
) -> dict:
    month_row = connection.execute(
        "SELECT month_key, platforms_json FROM billing_months WHERE id = ?",
        (billing_month_id,),
    ).fetchone()
    month_configs = _platform_configs_from_raw_value(month_row["platforms_json"] if month_row else "")
    if platform_code not in {item["code"] for item in month_configs}:
        raise ValueError("平台编码不合法。")
    timestamp = utc_now()
    current_version_row = connection.execute(
        """
        SELECT MAX(version_no) AS version_no
        FROM platform_bill_files
        WHERE billing_month_id = ? AND platform_code = ? AND is_current = 1
        """,
        (billing_month_id, platform_code),
    ).fetchone()
    current_version_no = int(current_version_row["version_no"] or 0) if current_version_row else 0
    if not current_version_no:
        raise ValueError("请先上传账单主文件后再添加文件。")
    connection.execute(
        """
        INSERT INTO platform_bill_files (
            billing_month_id, platform_code, file_role, original_filename, stored_path, uploaded_by, version_no, is_current, created_at
        )
        VALUES (?, ?, 'attachment', ?, ?, ?, ?, 1, ?)
        """,
        (billing_month_id, platform_code, original_filename, stored_path, uploaded_by, current_version_no, timestamp),
    )
    connection.execute(
        "UPDATE billing_months SET updated_at = ? WHERE id = ?",
        (timestamp, billing_month_id),
    )
    row = connection.execute("SELECT * FROM platform_bill_files WHERE id = last_insert_rowid()").fetchone()
    return dict(row)


def list_platform_bill_files(db_path: str | Path, month_key: str | None = None) -> list[dict]:
    where_sql = ""
    params: list[object] = []
    if month_key:
        where_sql = "WHERE bm.month_key = ?"
        params.append(normalize_month_key(month_key))
    platform_codes = list(platform_bill_platform_codes_for_db(db_path))
    platform_order_sql = "CASE pbf.platform_code " + " ".join(
        f"WHEN '{code}' THEN {index}" for index, code in enumerate(platform_codes, start=1)
    ) + " ELSE 99 END"
    with get_connection(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT pbf.*, bm.month_key, bm.status AS month_status,
                   uploader.display_name AS uploader_name,
                   uploader.department AS uploader_department,
                   submitter.display_name AS submitter_name,
                   submitter.department AS submitter_department
            FROM platform_bill_files pbf
            JOIN billing_months bm ON bm.id = pbf.billing_month_id
            JOIN users uploader ON uploader.id = pbf.uploaded_by
            LEFT JOIN users submitter ON submitter.id = pbf.submitted_by
            {where_sql}
            ORDER BY bm.month_key DESC,
                     {platform_order_sql} ASC,
                     pbf.is_current DESC,
                     pbf.version_no DESC,
                     CASE pbf.file_role WHEN 'main' THEN 1 ELSE 2 END ASC,
                     pbf.id DESC
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def list_platform_bill_return_requests(db_path: str | Path, month_key: str | None = None) -> list[dict]:
    where_sql = ""
    params: list[object] = []
    if month_key:
        where_sql = "WHERE bm.month_key = ?"
        params.append(normalize_month_key(month_key))
    with get_connection(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT request.*, bm.month_key,
                   requester.display_name AS requester_name,
                   resolver.display_name AS resolver_name
            FROM platform_bill_return_requests request
            JOIN billing_months bm ON bm.id = request.billing_month_id
            JOIN users requester ON requester.id = request.requested_by
            LEFT JOIN users resolver ON resolver.id = request.resolved_by
            {where_sql}
            ORDER BY bm.month_key DESC, request.platform_code ASC, request.requested_at DESC, request.id DESC
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def platform_bill_month_summary(db_path: str | Path, month_key: str) -> dict:
    month = get_billing_month_by_key(db_path, month_key)
    files = list_platform_bill_files(db_path, month_key)
    return_requests = list_platform_bill_return_requests(db_path, month_key)
    platform_configs = platform_bill_platform_configs_for_month(db_path, month_key)
    latest_return_request_by_platform: dict[str, dict] = {}
    for request in return_requests:
        latest_return_request_by_platform.setdefault(str(request.get("platform_code") or ""), request)
    file_map: dict[str, dict[str, object]] = {}
    for item in platform_configs:
        platform_code = item["code"]
        file_map[platform_code] = {
            "platform_code": platform_code,
            "platform_label": item["label"],
            "main_file": None,
            "attachments": [],
            "main_ready": False,
            "submitted": False,
            "submitted_at": None,
            "submitted_by": None,
            "submitted_by_name": "",
            "version_no": 0,
            "history_files": [],
            "return_request": latest_return_request_by_platform.get(platform_code),
        }
    for item in files:
        bucket = file_map.get(item["platform_code"])
        if not bucket:
            continue
        if int(item.get("is_current") or 0) != 1:
            bucket["history_files"].append(item)
            continue
        bucket["version_no"] = max(int(bucket.get("version_no") or 0), int(item.get("version_no") or 1))
        if item.get("submitted_at") and not bucket.get("submitted_at"):
            bucket["submitted"] = True
            bucket["submitted_at"] = item.get("submitted_at")
            bucket["submitted_by"] = item.get("submitted_by")
            bucket["submitted_by_name"] = item.get("submitter_name") or ""
        if item["file_role"] == "main":
            bucket["main_file"] = item
            bucket["main_ready"] = True
        else:
            bucket["attachments"].append(item)
    all_main_ready = all(bool(file_map[item["code"]]["main_ready"]) for item in platform_configs)
    all_submitted = all(bool(file_map[item["code"]]["submitted"]) for item in platform_configs)
    return {
        "month": month,
        "platforms": [file_map[item["code"]] for item in platform_configs],
        "all_main_ready": all_main_ready,
        "all_submitted": all_submitted,
    }


def platform_bill_month_overview(db_path: str | Path, month_key: str) -> dict:
    summary = platform_bill_month_summary(db_path, month_key)
    month = summary.get("month")
    platforms = summary.get("platforms") or []
    main_ready_count = sum(1 for item in platforms if item.get("main_ready"))
    attachment_count = sum(len(item.get("attachments") or []) for item in platforms)
    return {
        "month_key": normalize_month_key(month_key),
        "status": month.get("status") if month else "draft",
        "status_label": billing_month_status_label(month.get("status") if month else "draft"),
        "main_ready_count": main_ready_count,
        "submitted_count": sum(1 for item in platforms if item.get("submitted")),
        "platform_total": len(platforms),
        "attachment_count": attachment_count,
        "all_main_ready": bool(summary.get("all_main_ready")),
        "all_submitted": bool(summary.get("all_submitted")),
        "updated_at": month.get("updated_at") if month else None,
    }


def submit_platform_bill_platform(
    connection: sqlite3.Connection,
    month_key: str,
    platform_code: str,
    actor_user_id: int,
) -> dict:
    normalized_month_key = normalize_month_key(month_key)
    month_row = connection.execute(
        "SELECT * FROM billing_months WHERE month_key = ?",
        (normalized_month_key,),
    ).fetchone()
    if not month_row:
        raise ValueError("还没有创建这个月份的平台账单。")
    month = dict(month_row)
    month_configs = _platform_configs_from_raw_value(month.get("platforms_json"))
    if not month_configs:
        month_configs = _fallback_platform_configs()
    platform_labels = {item["code"]: item["label"] for item in month_configs}
    allowed_codes = {item["code"] for item in month_configs}
    if platform_code not in allowed_codes:
        raise ValueError("平台不合法。")
    platform_rows = connection.execute(
        """
        SELECT *
        FROM platform_bill_files
        WHERE billing_month_id = ? AND platform_code = ? AND is_current = 1
        ORDER BY CASE file_role WHEN 'main' THEN 1 ELSE 2 END, id ASC
        """,
        (month["id"], platform_code),
    ).fetchall()
    if not platform_rows:
        raise ValueError(f"{platform_labels.get(platform_code, platform_bill_platform_label(platform_code))}还没有上传账单文件，暂时不能提交。")
    platform_items = [dict(row) for row in platform_rows]
    if not any(item.get("file_role") == "main" for item in platform_items):
        raise ValueError(f"{platform_labels.get(platform_code, platform_bill_platform_label(platform_code))}还缺少账单文件，暂时不能提交。")
    if any(item.get("submitted_at") for item in platform_items):
        raise ValueError(f"{platform_labels.get(platform_code, platform_bill_platform_label(platform_code))}已经确认提交，不能重复提交。")
    timestamp = utc_now()
    connection.execute(
        """
        UPDATE platform_bill_files
        SET submitted_by = ?, submitted_at = ?
        WHERE billing_month_id = ? AND platform_code = ? AND is_current = 1
        """,
        (actor_user_id, timestamp, month["id"], platform_code),
    )
    return refresh_billing_month_submission_status(connection, month, actor_user_id)


def refresh_billing_month_submission_status(
    connection: sqlite3.Connection,
    month: dict,
    actor_user_id: int | None = None,
) -> dict:
    month_configs = _platform_configs_from_raw_value(month.get("platforms_json"))
    if not month_configs:
        month_configs = _fallback_platform_configs()
    allowed_codes = {item["code"] for item in month_configs}
    submitted_rows = connection.execute(
        """
        SELECT platform_code, MAX(submitted_at) AS latest_submitted_at
        FROM platform_bill_files
        WHERE billing_month_id = ? AND is_current = 1
        GROUP BY platform_code
        """,
        (month["id"],),
    ).fetchall()
    submitted_codes = {row["platform_code"] for row in submitted_rows if row["latest_submitted_at"]}
    month_status = "partial_to_b" if submitted_codes else "draft"
    month_submitted_at = None
    month_submitted_by = None
    if allowed_codes and len(submitted_codes) == len(allowed_codes):
        month_status = "submitted_to_b"
        month_submitted_at = utc_now()
        month_submitted_by = actor_user_id
    connection.execute(
        """
        UPDATE billing_months
        SET status = ?, submitted_by = ?, submitted_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (month_status, month_submitted_by, month_submitted_at, utc_now(), month["id"]),
    )
    updated_row = connection.execute(
        "SELECT * FROM billing_months WHERE id = ?",
        (month["id"],),
    ).fetchone()
    return dict(updated_row)


def create_platform_bill_return_request(
    connection: sqlite3.Connection,
    month_key: str,
    platform_code: str,
    requested_by: int,
    reason: str,
) -> dict:
    normalized_month_key = normalize_month_key(month_key)
    month_row = connection.execute(
        "SELECT * FROM billing_months WHERE month_key = ?",
        (normalized_month_key,),
    ).fetchone()
    if not month_row:
        raise ValueError("还没有创建这个月份的平台账单。")
    month = dict(month_row)
    current_rows = connection.execute(
        """
        SELECT * FROM platform_bill_files
        WHERE billing_month_id = ? AND platform_code = ? AND is_current = 1
        ORDER BY id ASC
        """,
        (month["id"], platform_code),
    ).fetchall()
    if not current_rows or not all(row["submitted_at"] for row in current_rows):
        raise ValueError("只有已确认提交的平台账单可以申请退回。")
    pending_row = connection.execute(
        """
        SELECT id FROM platform_bill_return_requests
        WHERE billing_month_id = ? AND platform_code = ? AND status = 'pending'
        """,
        (month["id"], platform_code),
    ).fetchone()
    if pending_row:
        raise ValueError("该平台已有待处理的退回申请。")
    version_no = max(int(row["version_no"] or 1) for row in current_rows)
    timestamp = utc_now()
    connection.execute(
        """
        INSERT INTO platform_bill_return_requests (
            billing_month_id, platform_code, version_no, reason, status, requested_by, requested_at
        )
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """,
        (month["id"], platform_code, version_no, reason, requested_by, timestamp),
    )
    connection.execute("UPDATE billing_months SET updated_at = ? WHERE id = ?", (timestamp, month["id"]))
    row = connection.execute("SELECT * FROM platform_bill_return_requests WHERE id = last_insert_rowid()").fetchone()
    return dict(row)


def resolve_platform_bill_return_request(
    connection: sqlite3.Connection,
    request_id: int,
    resolved_by: int,
    *,
    approve: bool,
    resolution_note: str = "",
) -> dict:
    request_row = connection.execute(
        "SELECT * FROM platform_bill_return_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    if not request_row:
        raise ValueError("没有找到退回申请。")
    request = dict(request_row)
    if request.get("status") != "pending":
        raise ValueError("该退回申请已经处理。")
    timestamp = utc_now()
    status = "approved" if approve else "rejected"
    connection.execute(
        """
        UPDATE platform_bill_return_requests
        SET status = ?, resolved_by = ?, resolved_at = ?, resolution_note = ?
        WHERE id = ?
        """,
        (status, resolved_by, timestamp, resolution_note, request_id),
    )
    if approve:
        connection.execute(
            """
            UPDATE platform_bill_files
            SET is_current = 0
            WHERE billing_month_id = ? AND platform_code = ? AND version_no = ? AND is_current = 1
            """,
            (request["billing_month_id"], request["platform_code"], request["version_no"]),
        )
        month_row = connection.execute(
            "SELECT * FROM billing_months WHERE id = ?",
            (request["billing_month_id"],),
        ).fetchone()
        if month_row:
            refresh_billing_month_submission_status(connection, dict(month_row), None)
    else:
        connection.execute(
            "UPDATE billing_months SET updated_at = ? WHERE id = ?",
            (timestamp, request["billing_month_id"]),
        )
    resolved_row = connection.execute(
        "SELECT * FROM platform_bill_return_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    return dict(resolved_row)


def billing_month_status_label(status: str | None) -> str:
    if status == "submitted_to_b":
        return "已提交给商品部"
    if status == "partial_to_b":
        return "部分已提交"
    return "待运营部提交"


def get_or_create_brand_bill(connection: sqlite3.Connection, month_key: str, actor_user_id: int) -> dict:
    normalized_month_key = normalize_month_key(month_key)
    row = connection.execute(
        "SELECT * FROM brand_bills WHERE month_key = ?",
        (normalized_month_key,),
    ).fetchone()
    if row:
        return dict(row)
    source_month_row = connection.execute(
        "SELECT id FROM billing_months WHERE month_key = ?",
        (normalized_month_key,),
    ).fetchone()
    timestamp = utc_now()
    connection.execute(
        """
        INSERT INTO brand_bills (
            month_key, status, source_billing_month_id, created_by, submitted_by, submitted_at, created_at, updated_at
        )
        VALUES (?, 'draft', ?, ?, NULL, NULL, ?, ?)
        """,
        (
            normalized_month_key,
            source_month_row["id"] if source_month_row else None,
            actor_user_id,
            timestamp,
            timestamp,
        ),
    )
    created_row = connection.execute(
        "SELECT * FROM brand_bills WHERE month_key = ?",
        (normalized_month_key,),
    ).fetchone()
    return dict(created_row)


def get_brand_bill_by_key(db_path: str | Path, month_key: str) -> dict | None:
    normalized_month_key = normalize_month_key(month_key)
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT bb.*,
                   creator.display_name AS creator_name,
                   submitter.display_name AS submitter_name,
                   bm.status AS source_billing_status
            FROM brand_bills bb
            JOIN users creator ON creator.id = bb.created_by
            LEFT JOIN users submitter ON submitter.id = bb.submitted_by
            LEFT JOIN billing_months bm ON bm.id = bb.source_billing_month_id
            WHERE bb.month_key = ?
            """,
            (normalized_month_key,),
        ).fetchone()
    return row_to_dict(row)


def list_brand_bills(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT bb.*,
                   creator.display_name AS creator_name,
                   submitter.display_name AS submitter_name,
                   bm.status AS source_billing_status
            FROM brand_bills bb
            JOIN users creator ON creator.id = bb.created_by
            LEFT JOIN users submitter ON submitter.id = bb.submitted_by
            LEFT JOIN billing_months bm ON bm.id = bb.source_billing_month_id
            ORDER BY bb.month_key DESC, bb.id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def list_brand_bill_versions(db_path: str | Path, month_key: str | None = None) -> list[dict]:
    where_sql = ""
    params: list[object] = []
    if month_key:
        where_sql = "WHERE bb.month_key = ?"
        params.append(normalize_month_key(month_key))
    with get_connection(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT bbv.*, bb.month_key, bb.status AS brand_bill_status,
                   uploader.display_name AS uploader_name,
                   uploader.department AS uploader_department
            FROM brand_bill_versions bbv
            JOIN brand_bills bb ON bb.id = bbv.brand_bill_id
            JOIN users uploader ON uploader.id = bbv.uploaded_by
            {where_sql}
            ORDER BY bb.month_key DESC, bbv.version_no DESC, bbv.id DESC
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def list_brand_bill_return_requests(db_path: str | Path, month_key: str | None = None) -> list[dict]:
    where_sql = ""
    params: list[object] = []
    if month_key:
        where_sql = "WHERE bb.month_key = ?"
        params.append(normalize_month_key(month_key))
    with get_connection(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT request.*, bb.month_key,
                   requester.display_name AS requester_name,
                   resolver.display_name AS resolver_name
            FROM brand_bill_return_requests request
            JOIN brand_bills bb ON bb.id = request.brand_bill_id
            JOIN users requester ON requester.id = request.requested_by
            LEFT JOIN users resolver ON resolver.id = request.resolved_by
            {where_sql}
            ORDER BY bb.month_key DESC, request.requested_at DESC, request.id DESC
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_brand_bill_version_by_id(db_path: str | Path, version_id: int) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT bbv.*, bb.month_key, bb.status AS brand_bill_status,
                   uploader.display_name AS uploader_name,
                   uploader.department AS uploader_department
            FROM brand_bill_versions bbv
            JOIN brand_bills bb ON bb.id = bbv.brand_bill_id
            JOIN users uploader ON uploader.id = bbv.uploaded_by
            WHERE bbv.id = ?
            """,
            (version_id,),
        ).fetchone()
    return row_to_dict(row)


def add_brand_bill_version(
    connection: sqlite3.Connection,
    brand_bill_id: int,
    original_filename: str,
    stored_path: str,
    uploaded_by: int,
    note: str = "",
) -> dict:
    current_row = connection.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS max_version FROM brand_bill_versions WHERE brand_bill_id = ?",
        (brand_bill_id,),
    ).fetchone()
    next_version_no = int((current_row["max_version"] if current_row else 0) or 0) + 1
    timestamp = utc_now()
    connection.execute(
        """
        INSERT INTO brand_bill_versions (
            brand_bill_id, version_no, original_filename, stored_path, uploaded_by, note, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (brand_bill_id, next_version_no, original_filename, stored_path, uploaded_by, note, timestamp),
    )
    connection.execute(
        "UPDATE brand_bills SET updated_at = ? WHERE id = ?",
        (timestamp, brand_bill_id),
    )
    row = connection.execute("SELECT * FROM brand_bill_versions WHERE id = last_insert_rowid()").fetchone()
    return dict(row)


def delete_latest_brand_bill_version(connection: sqlite3.Connection, month_key: str) -> dict:
    normalized_month_key = normalize_month_key(month_key)
    bill_row = connection.execute(
        "SELECT * FROM brand_bills WHERE month_key = ?",
        (normalized_month_key,),
    ).fetchone()
    if not bill_row:
        raise ValueError("当前月份没有可删除的品牌月账单。")
    brand_bill = dict(bill_row)
    if brand_bill.get("status") == "submitted_to_a":
        raise ValueError("该月份品牌月账单已提交给跟单部，不能删除。")
    version_row = connection.execute(
        """
        SELECT *
        FROM brand_bill_versions
        WHERE brand_bill_id = ?
        ORDER BY version_no DESC, id DESC
        LIMIT 1
        """,
        (brand_bill["id"],),
    ).fetchone()
    if not version_row:
        raise ValueError("当前月份没有可删除的品牌月账单版本。")
    version = dict(version_row)
    return_reference = connection.execute(
        """
        SELECT id
        FROM brand_bill_return_requests
        WHERE brand_bill_id = ? AND version_no = ?
        LIMIT 1
        """,
        (brand_bill["id"], version["version_no"]),
    ).fetchone()
    if return_reference:
        raise ValueError("该账单版本已进入流转历史，不能删除；请上传新版本调整。")
    connection.execute("DELETE FROM brand_bill_versions WHERE id = ?", (version["id"],))
    connection.execute(
        "UPDATE brand_bills SET updated_at = ? WHERE id = ?",
        (utc_now(), brand_bill["id"]),
    )
    return version


def brand_bill_month_summary(db_path: str | Path, month_key: str) -> dict:
    brand_bill = get_brand_bill_by_key(db_path, month_key)
    versions = list_brand_bill_versions(db_path, month_key)
    return_requests = list_brand_bill_return_requests(db_path, month_key)
    latest_version = versions[0] if versions else None
    dashboard_rows = []
    if brand_bill:
        with get_connection(db_path) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM brand_bill_dashboard_rows
                WHERE brand_bill_id = ?
                ORDER BY row_no ASC, id ASC
                """,
                (brand_bill["id"],),
            ).fetchall()
        dashboard_rows = [dict(row) for row in rows]
    return {
        "brand_bill": brand_bill,
        "versions": versions,
        "latest_version": latest_version,
        "has_version": bool(latest_version),
        "return_requests": return_requests,
        "latest_return_request": return_requests[0] if return_requests else None,
        "dashboard_rows": dashboard_rows,
        "has_dashboard_rows": bool(dashboard_rows),
    }


def replace_brand_bill_dashboard_rows(
    connection: sqlite3.Connection,
    brand_bill_id: int,
    rows: list[dict],
    actor_user_id: int,
    *,
    source_type: str = "",
    source_filename: str = "",
    source_path: str = "",
) -> None:
    timestamp = utc_now()
    connection.execute(
        "DELETE FROM brand_bill_dashboard_rows WHERE brand_bill_id = ?",
        (brand_bill_id,),
    )
    for index, row in enumerate(rows, start=1):
        connection.execute(
            """
            INSERT INTO brand_bill_dashboard_rows (
                brand_bill_id, row_no, month_label, platform_name, shop_name,
                total_qty, total_amount, gz_qty, gz_amount, wh_qty, wh_amount,
                created_by, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                brand_bill_id,
                index,
                str(row.get("month_label") or "").strip(),
                str(row.get("platform_name") or "").strip(),
                str(row.get("shop_name") or "").strip(),
                float(row.get("total_qty") or 0),
                float(row.get("total_amount") or 0),
                float(row.get("gz_qty") or 0),
                float(row.get("gz_amount") or 0),
                float(row.get("wh_qty") or 0),
                float(row.get("wh_amount") or 0),
                actor_user_id,
                timestamp,
                timestamp,
            ),
        )
    connection.execute(
        """
        UPDATE brand_bills
        SET dashboard_source_type = ?, dashboard_source_filename = ?, dashboard_source_path = ?,
            dashboard_updated_by = ?, dashboard_updated_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            str(source_type or "").strip(),
            str(source_filename or "").strip(),
            str(source_path or "").strip(),
            actor_user_id,
            timestamp,
            timestamp,
            brand_bill_id,
        ),
    )


def submit_brand_bill(connection: sqlite3.Connection, month_key: str, actor_user_id: int) -> dict:
    normalized_month_key = normalize_month_key(month_key)
    row = connection.execute(
        "SELECT * FROM brand_bills WHERE month_key = ?",
        (normalized_month_key,),
    ).fetchone()
    if not row:
        raise ValueError("还没有创建这个月份的品牌月账单。")
    brand_bill = dict(row)
    if brand_bill.get("status") == "submitted_to_a":
        raise ValueError("该月份品牌月账单已经提交给跟单部。")
    version_row = connection.execute(
        "SELECT COUNT(*) AS total FROM brand_bill_versions WHERE brand_bill_id = ?",
        (brand_bill["id"],),
    ).fetchone()
    if int(version_row["total"] or 0) <= 0:
        raise ValueError("请先上传至少一个品牌月账单版本，再提交给跟单部。")
    timestamp = utc_now()
    connection.execute(
        """
        UPDATE brand_bills
        SET status = 'submitted_to_a', submitted_by = ?, submitted_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (actor_user_id, timestamp, timestamp, brand_bill["id"]),
    )
    updated_row = connection.execute(
        "SELECT * FROM brand_bills WHERE id = ?",
        (brand_bill["id"],),
    ).fetchone()
    return dict(updated_row)


def create_brand_bill_return_request(
    connection: sqlite3.Connection,
    month_key: str,
    requested_by: int,
    reason: str,
) -> dict:
    normalized_month_key = normalize_month_key(month_key)
    row = connection.execute(
        "SELECT * FROM brand_bills WHERE month_key = ?",
        (normalized_month_key,),
    ).fetchone()
    if not row:
        raise ValueError("还没有创建这个月份的品牌月账单。")
    brand_bill = dict(row)
    if brand_bill.get("status") != "submitted_to_a":
        raise ValueError("只有已提交给跟单部的品牌月账单可以申请退回。")
    latest_version_row = connection.execute(
        "SELECT MAX(version_no) AS version_no FROM brand_bill_versions WHERE brand_bill_id = ?",
        (brand_bill["id"],),
    ).fetchone()
    version_no = int((latest_version_row["version_no"] if latest_version_row else 0) or 0)
    if version_no <= 0:
        raise ValueError("当前月份没有可退回的品牌月账单版本。")
    pending_row = connection.execute(
        "SELECT id FROM brand_bill_return_requests WHERE brand_bill_id = ? AND status = 'pending'",
        (brand_bill["id"],),
    ).fetchone()
    if pending_row:
        raise ValueError("该月份已有待处理的退回申请。")
    timestamp = utc_now()
    connection.execute(
        """
        INSERT INTO brand_bill_return_requests (
            brand_bill_id, version_no, reason, status, requested_by, requested_at
        )
        VALUES (?, ?, ?, 'pending', ?, ?)
        """,
        (brand_bill["id"], version_no, reason, requested_by, timestamp),
    )
    connection.execute("UPDATE brand_bills SET updated_at = ? WHERE id = ?", (timestamp, brand_bill["id"]))
    created_row = connection.execute("SELECT * FROM brand_bill_return_requests WHERE id = last_insert_rowid()").fetchone()
    return dict(created_row)


def resolve_brand_bill_return_request(
    connection: sqlite3.Connection,
    request_id: int,
    resolved_by: int,
    *,
    approve: bool,
    resolution_note: str = "",
) -> dict:
    request_row = connection.execute(
        "SELECT * FROM brand_bill_return_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    if not request_row:
        raise ValueError("没有找到品牌月账单退回申请。")
    request = dict(request_row)
    if request.get("status") != "pending":
        raise ValueError("该品牌月账单退回申请已经处理。")
    timestamp = utc_now()
    next_status = "approved" if approve else "rejected"
    connection.execute(
        """
        UPDATE brand_bill_return_requests
        SET status = ?, resolved_by = ?, resolved_at = ?, resolution_note = ?
        WHERE id = ?
        """,
        (next_status, resolved_by, timestamp, str(resolution_note or "").strip(), request_id),
    )
    if approve:
        connection.execute(
            """
            UPDATE brand_bills
            SET status = 'draft', submitted_by = NULL, submitted_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (timestamp, request["brand_bill_id"]),
        )
    else:
        connection.execute(
            "UPDATE brand_bills SET updated_at = ? WHERE id = ?",
            (timestamp, request["brand_bill_id"]),
        )
    resolved_row = connection.execute(
        "SELECT * FROM brand_bill_return_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    return dict(resolved_row)


def brand_bill_status_label(status: str | None) -> str:
    if status == "submitted_to_a":
        return "已提交给跟单部"
    return "待商品部整理"


def settlement_payment_status_label(status: str | None) -> str:
    if status == "paid":
        return "已支付"
    return "待支付"


def list_suppliers(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT s.*, creator.display_name AS creator_name
            FROM suppliers s
            JOIN users creator ON creator.id = s.created_by
            ORDER BY s.supplier_code ASC, s.id ASC
            """
        ).fetchall()
    suppliers = []
    for row in rows:
        item = dict(row)
        item["invoice_names"] = list_supplier_invoice_names(db_path, int(item["id"]))
        suppliers.append(item)
    return suppliers


def list_supplier_invoice_names(db_path: str | Path, supplier_id: int) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM supplier_invoice_names
            WHERE supplier_id = ?
            ORDER BY id ASC
            """,
            (supplier_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_supplier_by_code(db_path: str | Path, supplier_code: str) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT s.*, creator.display_name AS creator_name
            FROM suppliers s
            JOIN users creator ON creator.id = s.created_by
            WHERE s.supplier_code = ?
            """,
            (str(supplier_code or "").strip(),),
        ).fetchone()
    supplier = row_to_dict(row)
    if not supplier:
        return None
    supplier["invoice_names"] = list_supplier_invoice_names(db_path, int(supplier["id"]))
    return supplier


def create_supplier(
    db_path: str | Path,
    supplier_code: str,
    supplier_name: str,
    invoice_names: list[str],
    created_by: int,
) -> int:
    clean_code = str(supplier_code or "").strip()
    clean_name = str(supplier_name or "").strip()
    if not clean_code or not clean_name:
        raise ValueError("供应商编码和供应商名称不能为空。")
    timestamp = utc_now()
    with get_connection(db_path) as connection:
        connection.execute(
            """
            INSERT INTO suppliers (supplier_code, supplier_name, is_active, created_by, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?, ?)
            """,
            (clean_code, clean_name, created_by, timestamp, timestamp),
        )
        supplier_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        for invoice_name in normalize_invoice_name_list(invoice_names):
            connection.execute(
                """
                INSERT INTO supplier_invoice_names (supplier_id, invoice_name, created_at)
                VALUES (?, ?, ?)
                """,
                (supplier_id, invoice_name, timestamp),
            )
        return supplier_id


def update_supplier(
    db_path: str | Path,
    supplier_id: int,
    supplier_name: str,
    invoice_names: list[str],
    is_active: bool = True,
) -> None:
    clean_name = str(supplier_name or "").strip()
    if not clean_name:
        raise ValueError("供应商名称不能为空。")
    timestamp = utc_now()
    normalized_names = normalize_invoice_name_list(invoice_names)
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE suppliers
            SET supplier_name = ?, is_active = ?, updated_at = ?
            WHERE id = ?
            """,
            (clean_name, 1 if is_active else 0, timestamp, supplier_id),
        )
        connection.execute("DELETE FROM supplier_invoice_names WHERE supplier_id = ?", (supplier_id,))
        for invoice_name in normalized_names:
            connection.execute(
                """
                INSERT INTO supplier_invoice_names (supplier_id, invoice_name, created_at)
                VALUES (?, ?, ?)
                """,
                (supplier_id, invoice_name, timestamp),
            )


def normalize_invoice_name_list(invoice_names: list[str] | tuple[str, ...]) -> list[str]:
    normalized = []
    seen = set()
    for raw_name in invoice_names:
        name = str(raw_name or "").strip()
        if not name or name in seen:
            continue
        normalized.append(name)
        seen.add(name)
    return normalized


def backfill_supplier_master_data(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT supplier_code, supplier_name, created_by, created_at, updated_at FROM suppliers"
    ).fetchall()
    for row in rows:
        supplier_code = str(row["supplier_code"] or "").strip()
        supplier_name = str(row["supplier_name"] or "").strip()
        if not supplier_code or not supplier_name:
            continue
        code_row = connection.execute(
            "SELECT id FROM supplier_code_masters WHERE supplier_code = ?",
            (supplier_code,),
        ).fetchone()
        if code_row:
            code_id = int(code_row["id"])
        else:
            connection.execute(
                """
                INSERT INTO supplier_code_masters (
                    supplier_code, supply_chain_manager, is_active, created_by, created_at, updated_at
                )
                VALUES (?, '', 1, ?, ?, ?)
                """,
                (supplier_code, row["created_by"], row["created_at"], row["updated_at"]),
            )
            code_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        name_row = connection.execute(
            "SELECT id FROM supplier_master_names WHERE supplier_name = ?",
            (supplier_name,),
        ).fetchone()
        if not name_row:
            connection.execute(
                """
                INSERT INTO supplier_master_names (
                    supplier_code_master_id, supplier_name, is_active, created_at, updated_at
                )
                VALUES (?, ?, 1, ?, ?)
                """,
                (code_id, supplier_name, row["created_at"], row["updated_at"]),
            )


def save_supplier_master(
    connection: sqlite3.Connection,
    supplier_code: str,
    supplier_name: str,
    supply_chain_manager: str,
    actor_user_id: int,
) -> dict:
    clean_code = str(supplier_code or "").strip()
    clean_name = str(supplier_name or "").strip()
    clean_manager = str(supply_chain_manager or "").strip()
    if not clean_code or not clean_name or not clean_manager:
        raise ValueError("供应商编号、供应商名称和供应链经理不能为空。")
    timestamp = utc_now()
    code_row = connection.execute(
        "SELECT * FROM supplier_code_masters WHERE supplier_code = ?",
        (clean_code,),
    ).fetchone()
    if code_row:
        code_id = int(code_row["id"])
        connection.execute(
            """
            UPDATE supplier_code_masters
            SET supply_chain_manager = ?, is_active = 1, updated_at = ?
            WHERE id = ?
            """,
            (clean_manager, timestamp, code_id),
        )
    else:
        connection.execute(
            """
            INSERT INTO supplier_code_masters (
                supplier_code, supply_chain_manager, is_active, created_by, created_at, updated_at
            )
            VALUES (?, ?, 1, ?, ?, ?)
            """,
            (clean_code, clean_manager, actor_user_id, timestamp, timestamp),
        )
        code_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    name_row = connection.execute(
        "SELECT * FROM supplier_master_names WHERE supplier_name = ?",
        (clean_name,),
    ).fetchone()
    if name_row:
        if int(name_row["supplier_code_master_id"]) != code_id:
            raise ValueError(f"供应商名称 {clean_name} 已关联其他供应商编号。")
        connection.execute(
            "UPDATE supplier_master_names SET is_active = 1, updated_at = ? WHERE id = ?",
            (timestamp, name_row["id"]),
        )
        name_id = int(name_row["id"])
    else:
        connection.execute(
            """
            INSERT INTO supplier_master_names (
                supplier_code_master_id, supplier_name, is_active, created_at, updated_at
            )
            VALUES (?, ?, 1, ?, ?)
            """,
            (code_id, clean_name, timestamp, timestamp),
        )
        name_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    row = connection.execute(
        """
        SELECT smn.id, scm.supplier_code, smn.supplier_name, scm.supply_chain_manager
        FROM supplier_master_names smn
        JOIN supplier_code_masters scm ON scm.id = smn.supplier_code_master_id
        WHERE smn.id = ?
        """,
        (name_id,),
    ).fetchone()
    return dict(row)


def list_supplier_master_names(
    db_path: str | Path,
    supplier_code: str = "",
    supplier_name: str = "",
) -> list[dict]:
    clean_code = str(supplier_code or "").strip()
    clean_name = str(supplier_name or "").strip()
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT smn.id, scm.id AS supplier_code_master_id, scm.supplier_code,
                   smn.supplier_name, scm.supply_chain_manager
            FROM supplier_master_names smn
            JOIN supplier_code_masters scm ON scm.id = smn.supplier_code_master_id
            WHERE smn.is_active = 1
              AND scm.is_active = 1
              AND (? = '' OR scm.supplier_code = ?)
              AND (? = '' OR smn.supplier_name LIKE ?)
            ORDER BY scm.supplier_code ASC, smn.supplier_name ASC
            """,
            (clean_code, clean_code, clean_name, f"%{clean_name}%"),
        ).fetchall()
    return [dict(row) for row in rows]


def get_supplier_master_name(db_path: str | Path, supplier_master_name_id: int) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT smn.id, scm.id AS supplier_code_master_id, scm.supplier_code,
                   smn.supplier_name, scm.supply_chain_manager
            FROM supplier_master_names smn
            JOIN supplier_code_masters scm ON scm.id = smn.supplier_code_master_id
            WHERE smn.id = ? AND smn.is_active = 1 AND scm.is_active = 1
            """,
            (supplier_master_name_id,),
        ).fetchone()
    return row_to_dict(row)


def update_supplier_master(
    connection: sqlite3.Connection,
    supplier_master_name_id: int,
    supplier_code: str,
    supplier_name: str,
    supply_chain_manager: str,
    actor_user_id: int,
) -> dict:
    clean_code = str(supplier_code or "").strip()
    clean_name = str(supplier_name or "").strip()
    clean_manager = str(supply_chain_manager or "").strip()
    if not clean_code or not clean_name or not clean_manager:
        raise ValueError("供应商编号、供应商名称和供应链经理不能为空。")

    current_row = connection.execute(
        """
        SELECT smn.id, smn.supplier_code_master_id, smn.supplier_name,
               scm.supplier_code, scm.supply_chain_manager
        FROM supplier_master_names smn
        JOIN supplier_code_masters scm ON scm.id = smn.supplier_code_master_id
        WHERE smn.id = ? AND smn.is_active = 1 AND scm.is_active = 1
        """,
        (supplier_master_name_id,),
    ).fetchone()
    if not current_row:
        raise ValueError("供应商信息不存在或已停用。")

    same_name_row = connection.execute(
        "SELECT id FROM supplier_master_names WHERE supplier_name = ?",
        (clean_name,),
    ).fetchone()
    if same_name_row and int(same_name_row["id"]) != supplier_master_name_id:
        raise ValueError(f"供应商名称 {clean_name} 已存在，不能重复使用。")

    timestamp = utc_now()
    target_code_row = connection.execute(
        "SELECT * FROM supplier_code_masters WHERE supplier_code = ?",
        (clean_code,),
    ).fetchone()
    if target_code_row:
        target_code_id = int(target_code_row["id"])
        connection.execute(
            """
            UPDATE supplier_code_masters
            SET supply_chain_manager = ?, is_active = 1, updated_at = ?
            WHERE id = ?
            """,
            (clean_manager, timestamp, target_code_id),
        )
    else:
        connection.execute(
            """
            INSERT INTO supplier_code_masters (
                supplier_code, supply_chain_manager, is_active, created_by, created_at, updated_at
            )
            VALUES (?, ?, 1, ?, ?, ?)
            """,
            (clean_code, clean_manager, actor_user_id, timestamp, timestamp),
        )
        target_code_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    previous_code_id = int(current_row["supplier_code_master_id"])
    connection.execute(
        """
        UPDATE supplier_master_names
        SET supplier_code_master_id = ?, supplier_name = ?, is_active = 1, updated_at = ?
        WHERE id = ?
        """,
        (target_code_id, clean_name, timestamp, supplier_master_name_id),
    )
    connection.execute(
        """
        UPDATE supplier_bill_lines
        SET supplier_code = ?, supplier_name = ?
        WHERE supplier_master_name_id = ?
        """,
        (clean_code, clean_name, supplier_master_name_id),
    )
    connection.execute(
        """
        UPDATE supplier_bill_lines
        SET supply_chain_manager = ?
        WHERE supplier_master_name_id IN (
            SELECT id FROM supplier_master_names WHERE supplier_code_master_id = ?
        )
        """,
        (clean_manager, target_code_id),
    )
    if previous_code_id != target_code_id:
        remaining_names = connection.execute(
            "SELECT COUNT(*) AS count FROM supplier_master_names WHERE supplier_code_master_id = ? AND is_active = 1",
            (previous_code_id,),
        ).fetchone()
        if not int(remaining_names["count"] or 0):
            connection.execute(
                "UPDATE supplier_code_masters SET is_active = 0, updated_at = ? WHERE id = ?",
                (timestamp, previous_code_id),
            )
    saved_row = connection.execute(
        """
        SELECT smn.id, scm.supplier_code, smn.supplier_name, scm.supply_chain_manager
        FROM supplier_master_names smn
        JOIN supplier_code_masters scm ON scm.id = smn.supplier_code_master_id
        WHERE smn.id = ?
        """,
        (supplier_master_name_id,),
    ).fetchone()
    return dict(saved_row)


def list_supplier_code_masters(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT scm.id, scm.supplier_code, scm.supply_chain_manager, COUNT(smn.id) AS supplier_name_count
            FROM supplier_code_masters scm
            LEFT JOIN supplier_master_names smn
              ON smn.supplier_code_master_id = scm.id AND smn.is_active = 1
            WHERE scm.is_active = 1
            GROUP BY scm.id
            ORDER BY scm.supplier_code ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def resolve_supplier_bill_rows(connection: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    resolved_rows = []
    errors = []
    for row in rows:
        supplier_code = str(row.get("supplier_code") or "").strip()
        supplier_name = str(row.get("supplier_name") or "").strip()
        master_row = connection.execute(
            """
            SELECT smn.id, scm.supplier_code, smn.supplier_name, scm.supply_chain_manager
            FROM supplier_master_names smn
            JOIN supplier_code_masters scm ON scm.id = smn.supplier_code_master_id
            WHERE scm.supplier_code = ? AND smn.supplier_name = ?
              AND scm.is_active = 1 AND smn.is_active = 1
            """,
            (supplier_code, supplier_name),
        ).fetchone()
        row_label = f"第 {int(row.get('source_row_no') or 0)} 行"
        if not master_row:
            errors.append(f"{row_label} 的供应商编号和名称未在供应商管理中建立对应关系。")
            continue
        supplied_manager = str(row.get("supply_chain_manager") or "").strip()
        master_manager = str(master_row["supply_chain_manager"] or "").strip()
        if supplied_manager and master_manager and supplied_manager != master_manager:
            errors.append(f"{row_label} 的供应链经理与供应商主档不一致。")
            continue
        if not master_manager:
            errors.append(f"{row_label} 对应供应商主档缺少供应链经理。")
            continue
        resolved_rows.append(
            {
                **row,
                "supplier_master_name_id": int(master_row["id"]),
                "supplier_code": str(master_row["supplier_code"]),
                "supplier_name": str(master_row["supplier_name"]),
                "supply_chain_manager": master_manager,
            }
        )
    if errors:
        raise ValueError("；".join(errors[:6]))
    return resolved_rows


def create_supplier_bill_batch(
    connection: sqlite3.Connection,
    period_month: str,
    original_filename: str,
    stored_path: str,
    rows: list[dict],
    imported_by: int,
) -> dict:
    normalized_month = normalize_month_key(period_month)
    version_row = connection.execute(
        "SELECT COALESCE(MAX(version_no), 0) AS version_no FROM supplier_bill_batches WHERE period_month = ?",
        (normalized_month,),
    ).fetchone()
    version_no = int(version_row["version_no"] or 0) + 1
    timestamp = utc_now()
    connection.execute(
        "UPDATE supplier_bill_batches SET is_current = 0 WHERE period_month = ? AND is_current = 1",
        (normalized_month,),
    )
    connection.execute(
        """
        INSERT INTO supplier_bill_batches (
            period_month, version_no, original_filename, stored_path, line_count, is_current, imported_by, imported_at
        )
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (normalized_month, version_no, str(original_filename or "").strip(), str(stored_path or "").strip(), len(rows), imported_by, timestamp),
    )
    batch_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
    for row in rows:
        connection.execute(
            """
            INSERT INTO supplier_bill_lines (
                supplier_bill_batch_id, supplier_master_name_id, supplier_code, supplier_name, mode,
                supply_chain_manager, supplier_style_code, brand_name, style_color, quantity,
                tax_included_price, settlement_amount, source_row_no, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                row["supplier_master_name_id"],
                row["supplier_code"],
                row["supplier_name"],
                str(row.get("mode") or "").strip(),
                row["supply_chain_manager"],
                str(row.get("supplier_style_code") or "").strip(),
                str(row.get("brand_name") or "").strip(),
                str(row.get("style_color") or "").strip(),
                int(row.get("quantity") or 0),
                float(row.get("tax_included_price") or 0),
                float(row.get("settlement_amount") or 0),
                int(row.get("source_row_no") or 0),
                timestamp,
            ),
        )
    batch_row = connection.execute("SELECT * FROM supplier_bill_batches WHERE id = ?", (batch_id,)).fetchone()
    return dict(batch_row)


def supplier_bill_change_window(
    connection: sqlite3.Connection,
    period_month: str,
    *,
    now: datetime | None = None,
) -> dict:
    normalized_month = normalize_month_key(period_month)
    first_batch = connection.execute(
        """
        SELECT id, imported_at
        FROM supplier_bill_batches
        WHERE period_month = ?
        ORDER BY imported_at ASC, id ASC
        LIMIT 1
        """,
        (normalized_month,),
    ).fetchone()
    current_batch = connection.execute(
        """
        SELECT * FROM supplier_bill_batches
        WHERE period_month = ? AND is_current = 1
        ORDER BY version_no DESC, id DESC
        LIMIT 1
        """,
        (normalized_month,),
    ).fetchone()
    if not first_batch:
        return {
            "period_month": normalized_month,
            "first_imported_at": "",
            "deadline_at": "",
            "within_window": True,
            "current_batch": row_to_dict(current_batch),
        }
    first_imported_at = parse_utc(first_batch["imported_at"])
    if not first_imported_at:
        raise ValueError("账单首次导入时间无效，无法判断是否允许删除。")
    if first_imported_at.tzinfo is None:
        first_imported_at = first_imported_at.replace(tzinfo=UTC)
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=UTC)
    deadline_at = first_imported_at + timedelta(days=SUPPLIER_BILL_CHANGE_WINDOW_DAYS)
    return {
        "period_month": normalized_month,
        "first_imported_at": first_imported_at.isoformat().replace("+00:00", "Z"),
        "deadline_at": deadline_at.isoformat().replace("+00:00", "Z"),
        "within_window": current_time <= deadline_at,
        "current_batch": row_to_dict(current_batch),
    }


def deactivate_current_supplier_bill_batch(
    connection: sqlite3.Connection,
    period_month: str,
    *,
    now: datetime | None = None,
) -> dict:
    window = supplier_bill_change_window(connection, period_month, now=now)
    current_batch = window.get("current_batch")
    if not current_batch:
        raise ValueError(f"{window['period_month']} 当前没有可删除的账单。")
    if not window["within_window"]:
        raise ValueError(
            f"{window['period_month']} 账单已超过首次导入后 {SUPPLIER_BILL_CHANGE_WINDOW_DAYS} 天，不能删除或重新上传。"
        )
    connection.execute(
        "UPDATE supplier_bill_batches SET is_current = 0 WHERE id = ?",
        (current_batch["id"],),
    )
    return {
        **window,
        "deleted_batch": current_batch,
    }


def query_supplier_bill_lines(
    db_path: str | Path,
    start_month: str,
    end_month: str,
    supplier_code: str = "",
    supplier_name_ids: list[int] | None = None,
) -> dict:
    normalized_start = normalize_month_key(start_month)
    normalized_end = normalize_month_key(end_month)
    if normalized_start > normalized_end:
        raise ValueError("开始月份不能晚于结束月份。")
    clauses = ["sbb.is_current = 1", "sbb.period_month >= ?", "sbb.period_month <= ?"]
    params: list[object] = [normalized_start, normalized_end]
    clean_code = str(supplier_code or "").strip()
    if clean_code:
        clauses.append("sbl.supplier_code = ?")
        params.append(clean_code)
    clean_name_ids = sorted({int(item) for item in (supplier_name_ids or []) if int(item) > 0})
    if clean_name_ids:
        placeholders = ", ".join("?" for _ in clean_name_ids)
        clauses.append(f"sbl.supplier_master_name_id IN ({placeholders})")
        params.extend(clean_name_ids)
    where_sql = " AND ".join(clauses)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT sbb.period_month, sbb.version_no, sbl.supplier_code, sbl.supplier_name,
                   sbl.mode, sbl.supply_chain_manager, sbl.supplier_style_code,
                   sbl.brand_name, sbl.style_color, sbl.quantity,
                   sbl.tax_included_price, sbl.settlement_amount
            FROM supplier_bill_lines sbl
            JOIN supplier_bill_batches sbb ON sbb.id = sbl.supplier_bill_batch_id
            WHERE {where_sql}
            ORDER BY sbb.period_month DESC, sbl.supplier_code ASC, sbl.supplier_name ASC, sbl.source_row_no ASC
            """,
            params,
        ).fetchall()
    items = [dict(row) for row in rows]
    return {
        "start_month": normalized_start,
        "end_month": normalized_end,
        "items": items,
        "quantity_total": sum(int(item.get("quantity") or 0) for item in items),
        "settlement_amount_total": sum(float(item.get("settlement_amount") or 0) for item in items),
    }


def get_or_create_supplier_settlement(
    connection: sqlite3.Connection,
    month_key: str,
    supplier_id: int,
    actor_user_id: int,
) -> dict:
    normalized_month_key = normalize_month_key(month_key)
    row = connection.execute(
        """
        SELECT *
        FROM supplier_settlements
        WHERE month_key = ? AND supplier_id = ?
        """,
        (normalized_month_key, supplier_id),
    ).fetchone()
    if row:
        return dict(row)
    brand_bill_row = connection.execute(
        "SELECT id FROM brand_bills WHERE month_key = ?",
        (normalized_month_key,),
    ).fetchone()
    timestamp = utc_now()
    connection.execute(
        """
        INSERT INTO supplier_settlements (
            month_key, supplier_id, invoice_name, amount_due, amount_paid, payment_status, payment_date, note,
            source_brand_bill_id, created_by, created_at, updated_at
        )
        VALUES (?, ?, '', 0, 0, 'unpaid', NULL, '', ?, ?, ?, ?)
        """,
        (
            normalized_month_key,
            supplier_id,
            brand_bill_row["id"] if brand_bill_row else None,
            actor_user_id,
            timestamp,
            timestamp,
        ),
    )
    created_row = connection.execute("SELECT * FROM supplier_settlements WHERE id = last_insert_rowid()").fetchone()
    return dict(created_row)


def upsert_supplier_settlement(
    connection: sqlite3.Connection,
    month_key: str,
    supplier_id: int,
    invoice_name: str,
    amount_due: float,
    payment_status: str,
    payment_date: str | None,
    note: str,
    actor_user_id: int,
) -> dict:
    settlement = get_or_create_supplier_settlement(connection, month_key, supplier_id, actor_user_id)
    normalized_month_key = normalize_month_key(month_key)
    clean_payment_status = "paid" if str(payment_status or "").strip() == "paid" else "unpaid"
    amount_due_value = float(amount_due or 0)
    amount_paid_value = amount_due_value if clean_payment_status == "paid" else 0.0
    timestamp = utc_now()
    connection.execute(
        """
        UPDATE supplier_settlements
        SET invoice_name = ?, amount_due = ?, amount_paid = ?, payment_status = ?, payment_date = ?, note = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            str(invoice_name or "").strip(),
            amount_due_value,
            amount_paid_value,
            clean_payment_status,
            str(payment_date or "").strip() or None,
            str(note or "").strip(),
            timestamp,
            settlement["id"],
        ),
    )
    row = connection.execute(
        """
        SELECT *
        FROM supplier_settlements
        WHERE month_key = ? AND supplier_id = ?
        """,
        (normalized_month_key, supplier_id),
    ).fetchone()
    return dict(row)


def list_supplier_settlements(db_path: str | Path, month_key: str | None = None) -> list[dict]:
    where_sql = ""
    params: list[object] = []
    if month_key:
        where_sql = "WHERE ss.month_key = ?"
        params.append(normalize_month_key(month_key))
    with get_connection(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT ss.*, s.supplier_code, s.supplier_name,
                   creator.display_name AS creator_name
            FROM supplier_settlements ss
            JOIN suppliers s ON s.id = ss.supplier_id
            JOIN users creator ON creator.id = ss.created_by
            {where_sql}
            ORDER BY ss.month_key DESC, s.supplier_code ASC, ss.id ASC
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def supplier_settlement_month_summary(db_path: str | Path, month_key: str) -> dict:
    normalized_month_key = normalize_month_key(month_key)
    settlements = list_supplier_settlements(db_path, normalized_month_key)
    total_due = sum(float(item.get("amount_due") or 0) for item in settlements)
    total_paid = sum(float(item.get("amount_paid") or 0) for item in settlements)
    return {
        "month_key": normalized_month_key,
        "items": settlements,
        "total_due": total_due,
        "total_paid": total_paid,
        "total_unpaid": max(total_due - total_paid, 0.0),
    }


def normalize_monthly_board_year(year: int | str) -> int:
    try:
        normalized_year = int(str(year).strip())
    except (TypeError, ValueError) as error:
        raise ValueError("年度格式不正确。") from error
    if not 2000 <= normalized_year <= 2100:
        raise ValueError("年度应在 2000 至 2100 之间。")
    return normalized_year


def supplier_monthly_board_for_year(db_path: str | Path, year: int | str) -> dict:
    normalized_year = normalize_monthly_board_year(year)
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT board_year, month_no, payable_supplier_count, payable_total_amount, updated_by, updated_at
            FROM supplier_monthly_boards
            WHERE board_year = ?
            ORDER BY month_no DESC
            """,
            (normalized_year,),
        ).fetchall()
    months = {int(row["month_no"]): dict(row) for row in rows}
    supplier_counts = [int(item["payable_supplier_count"]) for item in months.values() if item["payable_supplier_count"] is not None]
    total_amounts = [float(item["payable_total_amount"]) for item in months.values() if item["payable_total_amount"] is not None]
    return {
        "year": normalized_year,
        "months": months,
        "supplier_count_total": sum(supplier_counts) if supplier_counts else None,
        "payable_amount_total": sum(total_amounts) if total_amounts else None,
    }


def save_supplier_monthly_board(
    connection: sqlite3.Connection,
    year: int | str,
    month_values: dict[int, dict[str, int | float | None]],
    updated_by: int,
) -> None:
    normalized_year = normalize_monthly_board_year(year)
    timestamp = utc_now()
    for month_no in range(1, 13):
        values = month_values.get(month_no, {})
        supplier_count = values.get("payable_supplier_count")
        payable_amount = values.get("payable_total_amount")
        if supplier_count is None and payable_amount is None:
            connection.execute(
                "DELETE FROM supplier_monthly_boards WHERE board_year = ? AND month_no = ?",
                (normalized_year, month_no),
            )
            continue
        connection.execute(
            """
            INSERT INTO supplier_monthly_boards (
                board_year, month_no, payable_supplier_count, payable_total_amount, updated_by, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(board_year, month_no) DO UPDATE SET
                payable_supplier_count = excluded.payable_supplier_count,
                payable_total_amount = excluded.payable_total_amount,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            (normalized_year, month_no, supplier_count, payable_amount, updated_by, timestamp),
        )


def supplier_year_summary(db_path: str | Path, supplier_id: int, year: int) -> dict:
    year_prefix = f"{int(year):04d}-"
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT ss.*, s.supplier_code, s.supplier_name
            FROM supplier_settlements ss
            JOIN suppliers s ON s.id = ss.supplier_id
            WHERE ss.supplier_id = ? AND ss.month_key LIKE ?
            ORDER BY ss.month_key ASC
            """,
            (supplier_id, f"{year_prefix}%"),
        ).fetchall()
    items = [dict(row) for row in rows]
    total_due = sum(float(item.get("amount_due") or 0) for item in items)
    total_paid = sum(float(item.get("amount_paid") or 0) for item in items)
    return {
        "year": int(year),
        "items": items,
        "total_due": total_due,
        "total_paid": total_paid,
        "total_unpaid": max(total_due - total_paid, 0.0),
    }


def billing_workboard_risk_label(risk_level: str | None) -> str:
    if risk_level == "done":
        return "已完成"
    if risk_level == "danger":
        return "异常待处理"
    if risk_level == "warning":
        return "需跟进"
    return "正常推进"


def billing_month_workboard(db_path: str | Path) -> list[dict]:
    month_keys = set()
    for item in list_billing_months(db_path):
        month_keys.add(str(item.get("month_key") or ""))
    for item in list_brand_bills(db_path):
        month_keys.add(str(item.get("month_key") or ""))
    for item in list_supplier_settlements(db_path):
        month_keys.add(str(item.get("month_key") or ""))
    clean_month_keys = sorted((key for key in month_keys if key), reverse=True)
    board = []
    for month_key in clean_month_keys:
        platform_summary = platform_bill_month_summary(db_path, month_key)
        brand_summary = brand_bill_month_summary(db_path, month_key)
        supplier_summary = supplier_settlement_month_summary(db_path, month_key)
        platform_month = platform_summary.get("month")
        brand_bill = brand_summary.get("brand_bill")
        supplier_items = supplier_summary.get("items") or []
        platform_main_count = sum(1 for item in platform_summary.get("platforms", []) if item.get("main_ready"))
        platform_submitted_count = sum(1 for item in platform_summary.get("platforms", []) if item.get("submitted"))
        platform_total = len(platform_summary.get("platforms", []))
        platform_missing_count = max(platform_total - platform_main_count, 0)
        platform_submitted = bool(platform_month and platform_month.get("status") == "submitted_to_b")
        brand_version_count = len(brand_summary.get("versions") or [])
        brand_has_version = bool(brand_summary.get("latest_version"))
        brand_submitted = bool(brand_bill and brand_bill.get("status") == "submitted_to_a")
        supplier_record_count = len(supplier_items)
        supplier_total_due = float(supplier_summary.get("total_due", 0.0) or 0.0)
        supplier_total_paid = float(supplier_summary.get("total_paid", 0.0) or 0.0)
        supplier_total_unpaid = float(supplier_summary.get("total_unpaid", 0.0) or 0.0)
        anomaly_notes: list[str] = []
        if brand_has_version and not platform_submitted:
            anomaly_notes.append("品牌月账单已先于平台账单进入下游流程")
        if supplier_record_count and not brand_submitted:
            anomaly_notes.append("供应商结算已先于品牌月账单进入下游流程")
        if supplier_record_count and not brand_has_version:
            anomaly_notes.append("供应商结算已有记录，但品牌月账单仍缺少版本")

        if platform_missing_count > 0:
            current_blocker = f"待运营部补齐 {platform_missing_count} 个平台账单"
            blocker_note = f"当前已完成 {platform_main_count} / {platform_total} 个平台账单。"
            risk_level = "danger" if anomaly_notes else "warning"
            risk_note = "；".join(anomaly_notes[:2]) if anomaly_notes else "平台账单尚未齐套。"
        elif not platform_submitted:
            if platform_submitted_count > 0:
                current_blocker = f"待运营部提交剩余 {platform_total - platform_submitted_count} 个平台"
                blocker_note = f"当前已确认提交 {platform_submitted_count} / {platform_total} 个平台，剩余平台提交后才能作为完整月份流转。"
            else:
                current_blocker = "待运营部提交平台账单"
                blocker_note = f"{platform_total} 个平台账单已齐，待运营部逐个平台确认提交给商品部。"
            risk_level = "danger" if anomaly_notes else "warning"
            risk_note = "；".join(anomaly_notes[:2]) if anomaly_notes else "当前月份已具备提交流转条件。"
        elif not brand_has_version:
            current_blocker = "待商品部上传品牌月账单"
            blocker_note = "平台账单已提交，等待商品部开始整理品牌完整月账单。"
            risk_level = "danger" if anomaly_notes else "normal"
            risk_note = "；".join(anomaly_notes[:2]) if anomaly_notes else "流程正按顺序流转到商品部。"
        elif not brand_submitted:
            current_blocker = "待商品部提交给跟单部"
            blocker_note = f"当前已有 {brand_version_count} 个版本，待商品部确认后继续流转。"
            risk_level = "danger" if anomaly_notes else "warning"
            risk_note = "；".join(anomaly_notes[:2]) if anomaly_notes else "品牌月账单已开始整理，但还未正式交接。"
        elif not supplier_record_count:
            current_blocker = "待跟单部录入供应商结算"
            blocker_note = "品牌月账单已提交，等待跟单部拆分供应商应付账单。"
            risk_level = "danger" if anomaly_notes else "normal"
            risk_note = "；".join(anomaly_notes[:2]) if anomaly_notes else "流程正按顺序流转到跟单部。"
        elif supplier_total_unpaid > 0.00001:
            current_blocker = "本月仍有待支付金额"
            blocker_note = f"当前待支付 {supplier_total_unpaid:.2f}，需继续跟进付款。"
            risk_level = "danger"
            risk_note = "供应商账单已生成，但本月仍有未完成支付的金额。"
        else:
            current_blocker = "本月流程已完成"
            blocker_note = "平台账单、品牌月账单、供应商结算三段流程已全部闭环。"
            risk_level = "done"
            risk_note = "本月无异常，适合继续归档或复盘。"

        is_month_complete = bool(
            platform_month
            and platform_submitted
            and brand_bill
            and brand_submitted
            and supplier_record_count
            and supplier_total_unpaid <= 0.00001
        )
        board.append(
            {
                "month_key": month_key,
                "platform_status": platform_month.get("status") if platform_month else "draft",
                "platform_status_label": billing_month_status_label(platform_month.get("status") if platform_month else "draft"),
                "platform_main_ready": platform_summary.get("all_main_ready", False),
                "platform_main_count": platform_main_count,
                "platform_submitted_count": platform_submitted_count,
                "platform_total": platform_total,
                "brand_status": brand_bill.get("status") if brand_bill else "draft",
                "brand_status_label": brand_bill_status_label(brand_bill.get("status") if brand_bill else "draft"),
                "brand_version_count": brand_version_count,
                "brand_has_version": brand_has_version,
                "supplier_record_count": supplier_record_count,
                "supplier_total_due": supplier_total_due,
                "supplier_total_paid": supplier_total_paid,
                "supplier_total_unpaid": supplier_total_unpaid,
                "supplier_status_label": (
                    "已结算"
                    if supplier_items and supplier_total_unpaid <= 0.00001
                    else ("待跟单部拆分" if brand_summary.get("latest_version") else "未开始")
                ),
                "current_blocker": current_blocker,
                "blocker_note": blocker_note,
                "risk_level": risk_level,
                "risk_label": billing_workboard_risk_label(risk_level),
                "risk_note": risk_note,
                "is_exception_month": risk_level in {"warning", "danger"},
                "is_month_complete": is_month_complete,
            }
        )
    return board


def product_logs_csv_bytes(logs: list[dict]) -> bytes:
    rows = []
    for item in logs:
        diff_summary = []
        for diff in item.get("diff_items") or []:
            diff_summary.append(diff.get("field_label", ""))
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
                "修改项数量": item.get("change_count", 0),
                "字段差异摘要": "、".join(diff_summary),
            }
        )
    from io import StringIO

    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=["资料ID", "商品名称", "款号", "资料发起部门", "时间", "操作人", "部门", "动作", "说明", "修改项数量", "字段差异摘要"],
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def iso_days_ago(days: int) -> str:
    now = datetime.now(UTC).replace(microsecond=0)
    return (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")
