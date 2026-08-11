from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from replenishment_center.engine import build_replenishment_items
from replenishment_center import secret_store


DEMO_PASSWORD = "demo123"
SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def local_today() -> date:
    return datetime.now(SHANGHAI).date()


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return secrets.compare_digest(expected, digest.hex())


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(db_path: str | Path, *, seed_demo: bool = True) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform_code TEXT NOT NULL,
                platform_name TEXT NOT NULL,
                store_code TEXT NOT NULL UNIQUE,
                store_name TEXT NOT NULL,
                brand_name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                store_id INTEGER NOT NULL,
                schedule_weekdays TEXT NOT NULL DEFAULT '[2,5]',
                schedule_time TEXT NOT NULL DEFAULT '10:00',
                auto_generate INTEGER NOT NULL DEFAULT 1,
                target_days INTEGER NOT NULL DEFAULT 45,
                safety_days INTEGER NOT NULL DEFAULT 7,
                weight_7 REAL NOT NULL DEFAULT 0.6,
                weight_14 REAL NOT NULL DEFAULT 0.4,
                min_sales_7 INTEGER NOT NULL DEFAULT 5,
                min_sales_14 INTEGER NOT NULL DEFAULT 10,
                min_consecutive_sales_days INTEGER NOT NULL DEFAULT 3,
                max_coverage_days REAL NOT NULL DEFAULT 14,
                data_source_mode TEXT NOT NULL DEFAULT 'test',
                api_status TEXT NOT NULL DEFAULT 'unconfigured',
                last_schedule_key TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(store_id) REFERENCES stores(id)
            );

            CREATE TABLE IF NOT EXISTS vipshop_api_config (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                environment TEXT NOT NULL DEFAULT 'production',
                app_key TEXT NOT NULL DEFAULT '',
                app_secret_enc TEXT NOT NULL DEFAULT '',
                access_token_enc TEXT NOT NULL DEFAULT '',
                expected_store_name TEXT NOT NULL DEFAULT '马天奴',
                external_store_id TEXT NOT NULL DEFAULT '',
                external_seller_id TEXT NOT NULL DEFAULT '',
                verified_store_name TEXT NOT NULL DEFAULT '',
                last_test_status TEXT NOT NULL DEFAULT 'not_tested',
                last_test_message TEXT NOT NULL DEFAULT '',
                last_test_at TEXT NOT NULL DEFAULT '',
                last_sync_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS vipshop_store_api_config (
                store_id INTEGER PRIMARY KEY,
                environment TEXT NOT NULL DEFAULT 'production',
                app_key TEXT NOT NULL DEFAULT '',
                app_secret_enc TEXT NOT NULL DEFAULT '',
                access_token_enc TEXT NOT NULL DEFAULT '',
                refresh_token_enc TEXT NOT NULL DEFAULT '',
                open_id TEXT NOT NULL DEFAULT '',
                access_token_expires_at TEXT NOT NULL DEFAULT '',
                refresh_token_expires_at TEXT NOT NULL DEFAULT '',
                oauth_authorized_at TEXT NOT NULL DEFAULT '',
                expected_store_name TEXT NOT NULL DEFAULT '',
                external_store_id TEXT NOT NULL DEFAULT '',
                external_seller_id TEXT NOT NULL DEFAULT '',
                verified_store_name TEXT NOT NULL DEFAULT '',
                last_test_status TEXT NOT NULL DEFAULT 'not_tested',
                last_test_message TEXT NOT NULL DEFAULT '',
                last_test_at TEXT NOT NULL DEFAULT '',
                last_sync_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(store_id) REFERENCES stores(id)
            );

            CREATE TABLE IF NOT EXISTS vipshop_oauth_states (
                state_hash TEXT PRIMARY KEY,
                store_id INTEGER NOT NULL,
                initiated_by INTEGER NOT NULL,
                redirect_uri TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error_code TEXT NOT NULL DEFAULT '',
                error_description TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(store_id) REFERENCES stores(id),
                FOREIGN KEY(initiated_by) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS tmall_api_config (
                store_id INTEGER PRIMARY KEY,
                environment TEXT NOT NULL DEFAULT 'production',
                app_key TEXT NOT NULL DEFAULT '',
                app_secret_enc TEXT NOT NULL DEFAULT '',
                session_key_enc TEXT NOT NULL DEFAULT '',
                expected_store_name TEXT NOT NULL DEFAULT '马天奴天猫官方旗舰店',
                external_shop_id TEXT NOT NULL DEFAULT '',
                seller_nick TEXT NOT NULL DEFAULT '',
                verified_store_name TEXT NOT NULL DEFAULT '',
                last_test_status TEXT NOT NULL DEFAULT 'not_tested',
                last_test_message TEXT NOT NULL DEFAULT '',
                last_test_at TEXT NOT NULL DEFAULT '',
                last_sync_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                FOREIGN KEY(store_id) REFERENCES stores(id)
            );

            CREATE TABLE IF NOT EXISTS skus (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                style_code TEXT NOT NULL,
                style_name TEXT NOT NULL,
                color_name TEXT NOT NULL,
                size_name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                supplier TEXT NOT NULL DEFAULT '',
                lead_time_days INTEGER NOT NULL DEFAULT 14,
                moq INTEGER NOT NULL DEFAULT 0,
                pack_size INTEGER NOT NULL DEFAULT 1,
                default_size_share REAL NOT NULL DEFAULT 0,
                core_size INTEGER NOT NULL DEFAULT 0,
                demand_factor REAL NOT NULL DEFAULT 1,
                lifecycle TEXT NOT NULL DEFAULT 'active',
                current_price REAL NOT NULL DEFAULT 0,
                tag_price REAL NOT NULL DEFAULT 0,
                UNIQUE(store_id, style_code, color_name, size_name),
                FOREIGN KEY(store_id) REFERENCES stores(id)
            );

            CREATE TABLE IF NOT EXISTS sales_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku_id INTEGER NOT NULL,
                sale_date TEXT NOT NULL,
                gross_units INTEGER NOT NULL DEFAULT 0,
                return_units INTEGER NOT NULL DEFAULT 0,
                net_units INTEGER NOT NULL DEFAULT 0,
                gross_sales_amount REAL NOT NULL DEFAULT 0,
                refund_amount REAL NOT NULL DEFAULT 0,
                net_sales_amount REAL NOT NULL DEFAULT 0,
                UNIQUE(sku_id, sale_date),
                FOREIGN KEY(sku_id) REFERENCES skus(id)
            );

            CREATE TABLE IF NOT EXISTS inventory_current (
                sku_id INTEGER PRIMARY KEY,
                snapshot_at TEXT NOT NULL,
                on_hand INTEGER NOT NULL DEFAULT 0,
                locked INTEGER NOT NULL DEFAULT 0,
                defective INTEGER NOT NULL DEFAULT 0,
                inbound INTEGER NOT NULL DEFAULT 0,
                inbound_date TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(sku_id) REFERENCES skus(id)
            );

            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                sales_through_date TEXT NOT NULL DEFAULT '',
                inventory_snapshot_at TEXT NOT NULL DEFAULT '',
                row_count INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(store_id) REFERENCES stores(id)
            );

            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                plan_no TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'merchandise_pending',
                generation_type TEXT NOT NULL,
                target_days INTEGER NOT NULL,
                safety_days INTEGER NOT NULL,
                min_sales_7 INTEGER NOT NULL DEFAULT 5,
                min_sales_14 INTEGER NOT NULL DEFAULT 10,
                min_consecutive_sales_days INTEGER NOT NULL DEFAULT 3,
                max_coverage_days REAL NOT NULL DEFAULT 14,
                sales_through_date TEXT NOT NULL,
                inventory_snapshot_at TEXT NOT NULL,
                merchandise_user_id INTEGER,
                merchandise_confirmed_at TEXT,
                submitted_at TEXT,
                followup_user_id INTEGER,
                followup_updated_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(store_id) REFERENCES stores(id),
                FOREIGN KEY(merchandise_user_id) REFERENCES users(id),
                FOREIGN KEY(followup_user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS plan_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plan_id INTEGER NOT NULL,
                sku_id INTEGER NOT NULL,
                style_code TEXT NOT NULL,
                outer_sku_id TEXT NOT NULL DEFAULT '',
                style_name TEXT NOT NULL,
                color_name TEXT NOT NULL,
                size_name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                supplier TEXT NOT NULL DEFAULT '',
                core_size INTEGER NOT NULL DEFAULT 0,
                sales_7 INTEGER NOT NULL,
                sales_14 INTEGER NOT NULL,
                consecutive_sales_days INTEGER NOT NULL DEFAULT 0,
                selection_reason TEXT NOT NULL DEFAULT '',
                avg_7 REAL NOT NULL,
                avg_14 REAL NOT NULL,
                size_share REAL NOT NULL,
                daily_demand REAL NOT NULL,
                sellable INTEGER NOT NULL,
                inbound INTEGER NOT NULL,
                inbound_date TEXT NOT NULL DEFAULT '',
                projected_14 REAL NOT NULL,
                coverage_days REAL,
                stockout_day INTEGER,
                risk_level TEXT NOT NULL,
                broken_core INTEGER NOT NULL DEFAULT 0,
                pack_size INTEGER NOT NULL DEFAULT 1,
                moq INTEGER NOT NULL DEFAULT 0,
                suggested_qty INTEGER NOT NULL,
                confirmed_qty INTEGER NOT NULL,
                adjustment_reason TEXT NOT NULL DEFAULT '',
                followup_qty INTEGER,
                expected_order_date TEXT NOT NULL DEFAULT '',
                expected_arrival_date TEXT NOT NULL DEFAULT '',
                followup_status TEXT NOT NULL DEFAULT 'pending',
                followup_note TEXT NOT NULL DEFAULT '',
                price_snapshot REAL NOT NULL DEFAULT 0,
                unit_cost_snapshot REAL,
                gross_margin_snapshot REAL,
                margin_gate_status TEXT NOT NULL DEFAULT 'unchecked',
                actual_arrival_date TEXT NOT NULL DEFAULT '',
                actual_arrived_qty INTEGER NOT NULL DEFAULT 0,
                arrival_variance_level TEXT NOT NULL DEFAULT 'none',
                arrival_variance_note TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(plan_id) REFERENCES plans(id),
                FOREIGN KEY(sku_id) REFERENCES skus(id)
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                store_id INTEGER,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                link TEXT NOT NULL DEFAULT '',
                read_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(store_id) REFERENCES stores(id)
            );

            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id INTEGER,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS sku_cost_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku_id INTEGER NOT NULL,
                unit_cost REAL NOT NULL,
                effective_from TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'excel',
                version_label TEXT NOT NULL DEFAULT '',
                created_by INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(sku_id) REFERENCES skus(id),
                FOREIGN KEY(created_by) REFERENCES users(id)
            );

            CREATE INDEX IF NOT EXISTS idx_sku_cost_versions_lookup
                ON sku_cost_versions(sku_id, effective_from DESC, id DESC);

            CREATE TABLE IF NOT EXISTS price_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                batch_no TEXT NOT NULL UNIQUE,
                mode TEXT NOT NULL DEFAULT 'system',
                status TEXT NOT NULL DEFAULT 'draft',
                rule_label TEXT NOT NULL DEFAULT '',
                rule_payload TEXT NOT NULL DEFAULT '{}',
                created_by INTEGER,
                confirmed_by INTEGER,
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                exported_at TEXT,
                FOREIGN KEY(store_id) REFERENCES stores(id),
                FOREIGN KEY(created_by) REFERENCES users(id),
                FOREIGN KEY(confirmed_by) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS price_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                price_plan_id INTEGER NOT NULL,
                sku_id INTEGER NOT NULL,
                style_code TEXT NOT NULL,
                outer_sku_id TEXT NOT NULL DEFAULT '',
                style_name TEXT NOT NULL,
                color_name TEXT NOT NULL,
                size_name TEXT NOT NULL,
                current_price REAL NOT NULL DEFAULT 0,
                proposed_price REAL NOT NULL DEFAULT 0,
                confirmed_price REAL NOT NULL DEFAULT 0,
                floor_price REAL NOT NULL DEFAULT 0,
                unit_cost REAL,
                current_margin REAL,
                proposed_margin REAL,
                sales_14 INTEGER NOT NULL DEFAULT 0,
                sellable INTEGER NOT NULL DEFAULT 0,
                coverage_days REAL,
                decision TEXT NOT NULL DEFAULT 'hold',
                reason TEXT NOT NULL DEFAULT '',
                include_flag INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(price_plan_id) REFERENCES price_plans(id),
                FOREIGN KEY(sku_id) REFERENCES skus(id)
            );

            CREATE INDEX IF NOT EXISTS idx_price_items_plan ON price_items(price_plan_id);

            CREATE TABLE IF NOT EXISTS api_unmatched_skus (
                external_sku_id TEXT PRIMARY KEY,
                external_spu_id TEXT NOT NULL DEFAULT '',
                outer_sku_id TEXT NOT NULL DEFAULT '',
                style_code TEXT NOT NULL DEFAULT '',
                color_name TEXT NOT NULL DEFAULT '',
                size_name TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_store_unmatched_skus (
                store_id INTEGER NOT NULL,
                external_sku_id TEXT NOT NULL,
                external_spu_id TEXT NOT NULL DEFAULT '',
                outer_sku_id TEXT NOT NULL DEFAULT '',
                style_code TEXT NOT NULL DEFAULT '',
                color_name TEXT NOT NULL DEFAULT '',
                size_name TEXT NOT NULL DEFAULT '',
                raw_json TEXT NOT NULL DEFAULT '{}',
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY(store_id, external_sku_id),
                FOREIGN KEY(store_id) REFERENCES stores(id)
            );

            CREATE TABLE IF NOT EXISTS browser_capture_config (
                id INTEGER PRIMARY KEY CHECK(id = 1),
                backend_url TEXT NOT NULL DEFAULT 'https://vis.vip.com/',
                sales_report_url TEXT NOT NULL DEFAULT '',
                inventory_report_url TEXT NOT NULL DEFAULT '',
                master_report_url TEXT NOT NULL DEFAULT '',
                debug_port INTEGER NOT NULL DEFAULT 9223,
                session_status TEXT NOT NULL DEFAULT 'browser_closed',
                current_url TEXT NOT NULL DEFAULT '',
                current_title TEXT NOT NULL DEFAULT '',
                last_check_message TEXT NOT NULL DEFAULT '',
                last_check_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS browser_capture_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting_download',
                started_epoch REAL NOT NULL,
                source_file TEXT NOT NULL DEFAULT '',
                archive_file TEXT NOT NULL DEFAULT '',
                file_name TEXT NOT NULL DEFAULT '',
                file_size INTEGER NOT NULL DEFAULT 0,
                file_sha256 TEXT NOT NULL DEFAULT '',
                analysis_json TEXT NOT NULL DEFAULT '{}',
                message TEXT NOT NULL DEFAULT '',
                created_by INTEGER,
                created_at TEXT NOT NULL,
                completed_at TEXT NOT NULL DEFAULT '',
                imported_plan_id INTEGER,
                FOREIGN KEY(created_by) REFERENCES users(id)
            );

            CREATE INDEX IF NOT EXISTS idx_sales_date ON sales_daily(sale_date);
            CREATE INDEX IF NOT EXISTS idx_plan_items_plan ON plan_items(plan_id);
            CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read_at);
            """
        )
        sku_columns = {row["name"] for row in connection.execute("PRAGMA table_info(skus)")}
        for column, definition in {
            "external_sku_id": "TEXT NOT NULL DEFAULT ''",
            "external_spu_id": "TEXT NOT NULL DEFAULT ''",
            "outer_sku_id": "TEXT NOT NULL DEFAULT ''",
            "is_demo": "INTEGER NOT NULL DEFAULT 0",
            "current_price": "REAL NOT NULL DEFAULT 0",
            "tag_price": "REAL NOT NULL DEFAULT 0",
        }.items():
            if column not in sku_columns:
                connection.execute(f"ALTER TABLE skus ADD COLUMN {column} {definition}")
        sales_columns = {row["name"] for row in connection.execute("PRAGMA table_info(sales_daily)")}
        if "source" not in sales_columns:
            connection.execute("ALTER TABLE sales_daily ADD COLUMN source TEXT NOT NULL DEFAULT 'test'")
        for column, definition in {
            "gross_sales_amount": "REAL NOT NULL DEFAULT 0",
            "refund_amount": "REAL NOT NULL DEFAULT 0",
            "net_sales_amount": "REAL NOT NULL DEFAULT 0",
        }.items():
            if column not in sales_columns:
                connection.execute(f"ALTER TABLE sales_daily ADD COLUMN {column} {definition}")
        inventory_columns = {row["name"] for row in connection.execute("PRAGMA table_info(inventory_current)")}
        if "source" not in inventory_columns:
            connection.execute("ALTER TABLE inventory_current ADD COLUMN source TEXT NOT NULL DEFAULT 'test'")
        settings_columns = {row["name"] for row in connection.execute("PRAGMA table_info(settings)")}
        for column, definition in {
            "min_sales_7": "INTEGER NOT NULL DEFAULT 5",
            "min_sales_14": "INTEGER NOT NULL DEFAULT 10",
            "min_consecutive_sales_days": "INTEGER NOT NULL DEFAULT 3",
            "max_coverage_days": "REAL NOT NULL DEFAULT 14",
        }.items():
            if column not in settings_columns:
                connection.execute(f"ALTER TABLE settings ADD COLUMN {column} {definition}")
        plan_columns = {row["name"] for row in connection.execute("PRAGMA table_info(plans)")}
        for column, definition in {
            "min_sales_7": "INTEGER NOT NULL DEFAULT 5",
            "min_sales_14": "INTEGER NOT NULL DEFAULT 10",
            "min_consecutive_sales_days": "INTEGER NOT NULL DEFAULT 3",
            "max_coverage_days": "REAL NOT NULL DEFAULT 14",
        }.items():
            if column not in plan_columns:
                connection.execute(f"ALTER TABLE plans ADD COLUMN {column} {definition}")
        plan_item_columns = {row["name"] for row in connection.execute("PRAGMA table_info(plan_items)")}
        if "outer_sku_id" not in plan_item_columns:
            connection.execute("ALTER TABLE plan_items ADD COLUMN outer_sku_id TEXT NOT NULL DEFAULT ''")
        if "consecutive_sales_days" not in plan_item_columns:
            connection.execute("ALTER TABLE plan_items ADD COLUMN consecutive_sales_days INTEGER NOT NULL DEFAULT 0")
        if "selection_reason" not in plan_item_columns:
            connection.execute("ALTER TABLE plan_items ADD COLUMN selection_reason TEXT NOT NULL DEFAULT ''")
        for column, definition in {
            "price_snapshot": "REAL NOT NULL DEFAULT 0",
            "unit_cost_snapshot": "REAL",
            "gross_margin_snapshot": "REAL",
            "margin_gate_status": "TEXT NOT NULL DEFAULT 'unchecked'",
            "actual_arrival_date": "TEXT NOT NULL DEFAULT ''",
            "actual_arrived_qty": "INTEGER NOT NULL DEFAULT 0",
            "arrival_variance_level": "TEXT NOT NULL DEFAULT 'none'",
            "arrival_variance_note": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in plan_item_columns:
                connection.execute(f"ALTER TABLE plan_items ADD COLUMN {column} {definition}")
        connection.execute(
            """
            UPDATE plan_items SET outer_sku_id = COALESCE(
                (SELECT skus.outer_sku_id FROM skus WHERE skus.id = plan_items.sku_id), ''
            ) WHERE outer_sku_id = ''
            """
        )
        browser_config_columns = {row["name"] for row in connection.execute("PRAGMA table_info(browser_capture_config)")}
        if "master_report_url" not in browser_config_columns:
            connection.execute("ALTER TABLE browser_capture_config ADD COLUMN master_report_url TEXT NOT NULL DEFAULT ''")
        browser_job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(browser_capture_jobs)")}
        if "imported_plan_id" not in browser_job_columns:
            connection.execute("ALTER TABLE browser_capture_jobs ADD COLUMN imported_plan_id INTEGER")
        notification_columns = {row["name"] for row in connection.execute("PRAGMA table_info(notifications)")}
        if "store_id" not in notification_columns:
            connection.execute("ALTER TABLE notifications ADD COLUMN store_id INTEGER")
        vipshop_config_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(vipshop_store_api_config)")
        }
        for column, definition in {
            "refresh_token_enc": "TEXT NOT NULL DEFAULT ''",
            "open_id": "TEXT NOT NULL DEFAULT ''",
            "access_token_expires_at": "TEXT NOT NULL DEFAULT ''",
            "refresh_token_expires_at": "TEXT NOT NULL DEFAULT ''",
            "oauth_authorized_at": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in vipshop_config_columns:
                connection.execute(
                    f"ALTER TABLE vipshop_store_api_config ADD COLUMN {column} {definition}"
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_vipshop_oauth_states_store ON vipshop_oauth_states(store_id, created_at)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_skus_external ON skus(store_id, external_sku_id) WHERE external_sku_id <> ''"
        )
        connection.execute(
            """
            UPDATE skus SET is_demo = 1
            WHERE external_sku_id = '' AND style_code IN (
                'MTN260701', 'MTN260715', 'MTN260628', 'MTN260722', 'MTN260610', 'MTN260530'
            )
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO vipshop_api_config(id, updated_at) VALUES (1, ?)",
            (utc_now(),),
        )
        connection.execute(
            "INSERT OR IGNORE INTO browser_capture_config(id, updated_at) VALUES (1, ?)",
            (utc_now(),),
        )
        _ensure_stores(connection)
        if seed_demo:
            _seed_demo(connection)
            _ensure_demo_enrichment(connection)
    if seed_demo:
        with get_connection(path) as connection:
            count = connection.execute("SELECT COUNT(*) AS count FROM plans").fetchone()["count"]
        if not count:
            generate_plan(path, generation_type="initial_test")


def _ensure_stores(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO stores(platform_code, platform_name, store_code, store_name, brand_name)
        VALUES ('vip', '唯品会', 'VIP-MTN', '马天奴唯品会', '马天奴')
        """
    )
    connection.execute(
        "UPDATE stores SET store_name = '马天奴唯品会', platform_name = '唯品会' WHERE store_code = 'VIP-MTN'"
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO stores(platform_code, platform_name, store_code, store_name, brand_name)
        VALUES ('tmall', '天猫', 'TMALL-MTN-FLAGSHIP', '马天奴天猫官方旗舰店', '马天奴')
        """
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO stores(platform_code, platform_name, store_code, store_name, brand_name)
        VALUES ('vip', '唯品会', 'VIP-BNX', 'BNX唯品会', 'BNX')
        """
    )
    tmall_store = connection.execute(
        "SELECT id FROM stores WHERE store_code = 'TMALL-MTN-FLAGSHIP'"
    ).fetchone()
    connection.execute(
        "INSERT OR IGNORE INTO tmall_api_config(store_id, updated_at) VALUES (?, ?)",
        (tmall_store["id"], utc_now()),
    )
    vip_mtn = connection.execute(
        "SELECT id FROM stores WHERE store_code = 'VIP-MTN'"
    ).fetchone()
    connection.execute(
        """
        INSERT OR IGNORE INTO vipshop_store_api_config(
            store_id, environment, app_key, app_secret_enc, access_token_enc,
            expected_store_name, external_store_id, external_seller_id,
            verified_store_name, last_test_status, last_test_message, last_test_at,
            last_sync_at, updated_at
        )
        SELECT ?, environment, app_key, app_secret_enc, access_token_enc,
            expected_store_name, external_store_id, external_seller_id,
            verified_store_name, last_test_status, last_test_message, last_test_at,
            last_sync_at, updated_at
        FROM vipshop_api_config WHERE id = 1
        """,
        (vip_mtn["id"],),
    )
    vip_bnx = connection.execute(
        "SELECT id FROM stores WHERE store_code = 'VIP-BNX'"
    ).fetchone()
    connection.execute(
        """
        INSERT OR IGNORE INTO vipshop_store_api_config(
            store_id, expected_store_name, updated_at
        ) VALUES (?, 'BNX', ?)
        """,
        (vip_bnx["id"], utc_now()),
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO api_store_unmatched_skus(
            store_id, external_sku_id, external_spu_id, outer_sku_id, style_code,
            color_name, size_name, raw_json, last_seen_at
        )
        SELECT ?, external_sku_id, external_spu_id, outer_sku_id, style_code,
            color_name, size_name, raw_json, last_seen_at FROM api_unmatched_skus
        """,
        (vip_mtn["id"],),
    )


def _seed_demo(connection: sqlite3.Connection) -> None:
    if not connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        users = [
            ("merch", "林晓 · 商品部", "merchandise"),
            ("followup", "周岚 · 跟单部", "followup"),
            ("manager", "陈总 · 管理层", "manager"),
            ("admin", "系统管理员", "admin"),
        ]
        for username, display_name, role in users:
            connection.execute(
                "INSERT INTO users(username, display_name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (username, display_name, role, hash_password(DEMO_PASSWORD), utc_now()),
            )
    store_id = connection.execute("SELECT id FROM stores WHERE store_code = 'VIP-MTN'").fetchone()["id"]
    connection.execute(
        """
        INSERT OR IGNORE INTO settings(
            id, store_id, schedule_weekdays, schedule_time, auto_generate, target_days,
            safety_days, weight_7, weight_14, data_source_mode, api_status, updated_at
        ) VALUES (1, ?, '[2,5]', '10:00', 1, 45, 7, 0.6, 0.4, 'test', 'unconfigured', ?)
        """,
        (store_id, utc_now()),
    )
    if connection.execute("SELECT 1 FROM skus LIMIT 1").fetchone():
        return

    styles = [
        ("MTN260701", "轻薄针织开衫", "雾灰", "针织衫", "嘉兴锦尚", 10, 80, 2, 8.0),
        ("MTN260715", "醋酸通勤连衣裙", "深海蓝", "连衣裙", "杭州衣尚", 14, 60, 2, 6.2),
        ("MTN260628", "天丝宽松衬衫", "云朵白", "衬衫", "宁波梵序", 12, 60, 2, 4.2),
        ("MTN260722", "轻量腰带风衣", "苔绿", "风衣", "海宁远洲", 21, 48, 2, 2.8),
        ("MTN260610", "高腰直筒西裤", "炭黑", "西裤", "嘉兴锦尚", 10, 80, 2, 5.0),
        ("MTN260530", "几何印花上衣", "琥珀棕", "上衣", "绍兴意澜", 14, 40, 2, 1.1),
    ]
    sizes = [("S", 0.18, 0), ("M", 0.36, 1), ("L", 0.30, 1), ("XL", 0.16, 0)]
    today = local_today()
    inventory_patterns = [
        [4, 7, 8, 6],
        [10, 20, 18, 12],
        [24, 40, 34, 22],
        [22, 36, 30, 18],
        [30, 5, 32, 26],
        [36, 52, 46, 30],
    ]
    for style_index, style in enumerate(styles):
        style_code, style_name, color_name, category, supplier, lead, moq, pack, base_daily = style
        sku_ids = []
        for size_name, share, core in sizes:
            cursor = connection.execute(
                """
                INSERT INTO skus(
                    store_id, style_code, style_name, color_name, size_name, category,
                    supplier, lead_time_days, moq, pack_size, default_size_share, core_size, is_demo
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (store_id, style_code, style_name, color_name, size_name, category, supplier, lead, moq, pack, share, core),
            )
            sku_ids.append((cursor.lastrowid, share))
        for days_ago in range(20, -1, -1):
            sale_date = today - timedelta(days=days_ago)
            recent_factor = 1.28 if style_index in {0, 4} and days_ago <= 6 else 1.0
            launch_factor = 0.55 if style_index == 3 and days_ago > 10 else 1.0
            weekday_factor = 1.16 if sale_date.weekday() in {4, 5, 6} else 0.9
            for size_index, (sku_id, share) in enumerate(sku_ids):
                variation = 1 + (((days_ago + size_index + style_index) % 3) - 1) * 0.12
                gross = max(0, round(base_daily * share * recent_factor * launch_factor * weekday_factor * variation))
                returns = 1 if gross >= 3 and (days_ago + size_index * 2 + style_index) % 11 == 0 else 0
                connection.execute(
                    "INSERT INTO sales_daily(sku_id, sale_date, gross_units, return_units, net_units) VALUES (?, ?, ?, ?, ?)",
                    (sku_id, sale_date.isoformat(), gross, returns, max(0, gross - returns)),
                )
        for size_index, (sku_id, _) in enumerate(sku_ids):
            inbound = 12 if style_index in {1, 3} and size_index in {1, 2} else 0
            inbound_date = (today + timedelta(days=8 + style_index)).isoformat() if inbound else ""
            on_hand = inventory_patterns[style_index][size_index]
            locked = 1 if (style_index + size_index) % 3 == 0 else 0
            defective = 1 if style_index == 2 and size_index == 3 else 0
            connection.execute(
                """
                INSERT INTO inventory_current(sku_id, snapshot_at, on_hand, locked, defective, inbound, inbound_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (sku_id, f"{today.isoformat()} 06:10", on_hand, locked, defective, inbound, inbound_date),
            )
    connection.execute(
        """
        INSERT INTO sync_runs(store_id, source, status, sales_through_date, inventory_snapshot_at, row_count, message, created_at)
        VALUES (?, 'test_seed', 'success', ?, ?, ?, '单店试跑数据已就绪；正式 API 尚待授权。', ?)
        """,
        (store_id, today.isoformat(), f"{today.isoformat()} 06:10", len(styles) * len(sizes), utc_now()),
    )


def _ensure_demo_enrichment(connection: sqlite3.Connection) -> None:
    """Seed safe, clearly-labelled demo pricing and cost values for the prototype."""
    connection.execute(
        """
        INSERT OR IGNORE INTO users(username, display_name, role, password_hash, created_at)
        VALUES ('ops', '运营部 · 只读执行', 'operations', ?, ?)
        """,
        (hash_password(DEMO_PASSWORD), utc_now()),
    )
    price_map = {
        "MTN260701": (599, 189), "MTN260715": (899, 268), "MTN260628": (499, 158),
        "MTN260722": (1299, 438), "MTN260610": (699, 218), "MTN260530": (399, 128),
    }
    today = local_today().isoformat()
    for style_code, (price, cost) in price_map.items():
        connection.execute(
            "UPDATE skus SET current_price = CASE WHEN current_price = 0 THEN ? ELSE current_price END, tag_price = CASE WHEN tag_price = 0 THEN ? ELSE tag_price END WHERE style_code = ? AND is_demo = 1",
            (price, round(price * 1.7, 2), style_code),
        )
        for row in connection.execute("SELECT id FROM skus WHERE style_code = ? AND is_demo = 1", (style_code,)).fetchall():
            connection.execute(
                "INSERT INTO sku_cost_versions(sku_id, unit_cost, effective_from, source, version_label, created_at) SELECT ?, ?, ?, 'demo', '首期演示成本', ? WHERE NOT EXISTS (SELECT 1 FROM sku_cost_versions WHERE sku_id = ? AND source = 'demo' AND version_label = '首期演示成本')",
                (row["id"], cost, today, utc_now(), row["id"]),
            )
    connection.execute(
        """
        UPDATE sales_daily SET gross_sales_amount = gross_units * COALESCE((SELECT current_price FROM skus WHERE skus.id = sales_daily.sku_id), 0),
            refund_amount = return_units * COALESCE((SELECT current_price FROM skus WHERE skus.id = sales_daily.sku_id), 0),
            net_sales_amount = net_units * COALESCE((SELECT current_price FROM skus WHERE skus.id = sales_daily.sku_id), 0)
        WHERE net_sales_amount = 0 AND source = 'test'
        """
    )


def _mask(value: str, visible: int = 4) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= visible:
        return "*" * len(text)
    return "*" * max(4, len(text) - visible) + text[-visible:]


def _state_hash(state: str) -> str:
    return hashlib.sha256(str(state or "").encode("utf-8")).hexdigest()


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _duration_expiry(seconds) -> str:
    try:
        duration = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return ""
    return _utc_iso(datetime.now(UTC) + timedelta(seconds=duration))


def _absolute_expiry(value) -> str:
    if value in {None, ""}:
        return ""
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        parsed = _parse_utc(str(value))
        return _utc_iso(parsed) if parsed else ""
    if timestamp > 100_000_000_000:
        timestamp /= 1000
    try:
        return _utc_iso(datetime.fromtimestamp(timestamp, UTC))
    except (OverflowError, OSError, ValueError):
        return ""


def list_stores(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM stores WHERE is_active = 1 ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def get_store(db_path: str | Path, store_code: str) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM stores WHERE store_code = ? AND is_active = 1",
            (str(store_code or "").strip(),),
        ).fetchone()
    return dict(row) if row else None


def get_tmall_api_config(
    db_path: str | Path, store_id: int, *, include_secrets: bool = False
) -> dict:
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM tmall_api_config WHERE store_id = ?", (int(store_id),)
        ).fetchone()
    if not row:
        raise ValueError("该店铺尚未初始化天猫 API 配置。")
    result = dict(row)
    environment = os.environ.get("TMALL_ENVIRONMENT", "").strip()
    app_key = os.environ.get("TMALL_APP_KEY", "").strip()
    app_secret = os.environ.get("TMALL_APP_SECRET", "").strip()
    session_key = os.environ.get("TMALL_SESSION_KEY", "").strip()
    if environment in {"production", "sandbox"}:
        result["environment"] = environment
    stored_secret = secret_store.unseal(db_path, result["app_secret_enc"]) if result["app_secret_enc"] else ""
    stored_session = secret_store.unseal(db_path, result["session_key_enc"]) if result["session_key_enc"] else ""
    effective_key = app_key or result["app_key"]
    effective_secret = app_secret or stored_secret
    effective_session = session_key or stored_session
    result.update(
        {
            "app_key": effective_key,
            "app_key_masked": _mask(effective_key),
            "has_app_secret": bool(effective_secret),
            "has_session_key": bool(effective_session),
            "credentials_complete": bool(effective_key and effective_secret and effective_session),
            "credential_source": (
                "environment" if app_key or app_secret or session_key else "encrypted_database"
            ),
        }
    )
    if include_secrets:
        result["app_secret"] = effective_secret
        result["session_key"] = effective_session
    result.pop("app_secret_enc", None)
    result.pop("session_key_enc", None)
    return result


def save_tmall_api_config(
    db_path: str | Path, store_id: int, payload: dict, user_id: int
) -> dict:
    environment = str(payload.get("environment") or "production").strip()
    if environment not in {"production", "sandbox"}:
        raise ValueError("天猫接口环境只能选择正式环境或沙箱环境。")
    app_key = str(payload.get("app_key") or "").strip()
    expected_store_name = str(
        payload.get("expected_store_name") or "马天奴天猫官方旗舰店"
    ).strip()
    if not expected_store_name:
        raise ValueError("请填写预期店铺名称，用于防止授权到错误店铺。")
    with get_connection(db_path) as connection:
        store = connection.execute("SELECT * FROM stores WHERE id = ?", (int(store_id),)).fetchone()
        if not store or store["platform_code"] != "tmall":
            raise ValueError("当前店铺不是天猫店铺。")
        existing = connection.execute(
            "SELECT * FROM tmall_api_config WHERE store_id = ?", (int(store_id),)
        ).fetchone()
        app_secret_enc = existing["app_secret_enc"]
        session_key_enc = existing["session_key_enc"]
        if str(payload.get("app_secret") or "").strip():
            app_secret_enc = secret_store.seal(db_path, str(payload["app_secret"]).strip())
        if str(payload.get("session_key") or "").strip():
            session_key_enc = secret_store.seal(db_path, str(payload["session_key"]).strip())
        if payload.get("clear_app_secret"):
            app_secret_enc = ""
        if payload.get("clear_session_key"):
            session_key_enc = ""
        connection.execute(
            """
            UPDATE tmall_api_config SET environment = ?, app_key = ?, app_secret_enc = ?,
                session_key_enc = ?, expected_store_name = ?, last_test_status = 'not_tested',
                last_test_message = '', updated_at = ? WHERE store_id = ?
            """,
            (
                environment, app_key, app_secret_enc, session_key_enc,
                expected_store_name, utc_now(), int(store_id),
            ),
        )
        _audit(
            connection, user_id, "tmall_api_config_saved", "tmall_api_config",
            int(store_id), f"environment={environment}",
        )
    return get_tmall_api_config(db_path, store_id)


def tmall_client_config(db_path: str | Path, store_id: int):
    from replenishment_center.tmall import TmallConfig

    config = get_tmall_api_config(db_path, store_id, include_secrets=True)
    return TmallConfig(
        environment=config["environment"],
        app_key=config["app_key"],
        app_secret=config["app_secret"],
        session_key=config["session_key"],
        expected_store_name=config["expected_store_name"],
    )


def record_tmall_api_test(
    db_path: str | Path,
    store_id: int,
    *,
    status: str,
    message: str,
    store: dict | None = None,
) -> None:
    store = store or {}
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE tmall_api_config SET last_test_status = ?, last_test_message = ?, last_test_at = ?,
                external_shop_id = ?, seller_nick = ?, verified_store_name = ?, updated_at = ?
            WHERE store_id = ?
            """,
            (
                status, message, utc_now(), str(store.get("shop_id") or ""),
                str(store.get("seller_nick") or ""), str(store.get("store_name") or ""),
                utc_now(), int(store_id),
            ),
        )


def _vipshop_store(connection: sqlite3.Connection, store_id: int | None = None) -> sqlite3.Row:
    if store_id is None:
        store = connection.execute(
            "SELECT * FROM stores WHERE store_code = 'VIP-MTN'"
        ).fetchone()
    else:
        store = connection.execute(
            "SELECT * FROM stores WHERE id = ?", (int(store_id),)
        ).fetchone()
    if not store or store["platform_code"] != "vip":
        raise ValueError("当前店铺不是唯品会店铺。")
    return store


def get_api_config(
    db_path: str | Path, store_id: int | None = None, *, include_secrets: bool = False
) -> dict:
    with get_connection(db_path) as connection:
        store = _vipshop_store(connection, store_id)
        row = connection.execute(
            "SELECT * FROM vipshop_store_api_config WHERE store_id = ?", (store["id"],)
        ).fetchone()
    if not row:
        raise ValueError("该店铺尚未初始化唯品会 API 配置。")
    result = dict(row)
    env_prefix = "VIPSHOP" if store["store_code"] == "VIP-MTN" else "VIPSHOP_BNX"
    environment = os.environ.get(f"{env_prefix}_ENVIRONMENT", "").strip()
    app_key = os.environ.get(f"{env_prefix}_APP_KEY", "").strip()
    app_secret = os.environ.get(f"{env_prefix}_APP_SECRET", "").strip()
    access_token = os.environ.get(f"{env_prefix}_ACCESS_TOKEN", "").strip()
    refresh_token = os.environ.get(f"{env_prefix}_REFRESH_TOKEN", "").strip()
    if environment in {"production", "sandbox"}:
        result["environment"] = environment
    stored_secret = secret_store.unseal(db_path, result["app_secret_enc"]) if result["app_secret_enc"] else ""
    stored_token = secret_store.unseal(db_path, result["access_token_enc"]) if result["access_token_enc"] else ""
    stored_refresh = secret_store.unseal(db_path, result["refresh_token_enc"]) if result["refresh_token_enc"] else ""
    effective_key = app_key or result["app_key"]
    effective_secret = app_secret or stored_secret
    effective_token = access_token or stored_token
    effective_refresh = refresh_token or stored_refresh
    access_expiry = _parse_utc(result["access_token_expires_at"])
    refresh_expiry = _parse_utc(result["refresh_token_expires_at"])
    now = datetime.now(UTC)
    result.update(
        {
            "app_key": effective_key,
            "app_key_masked": _mask(effective_key),
            "has_app_secret": bool(effective_secret),
            "has_access_token": bool(effective_token),
            "has_refresh_token": bool(effective_refresh),
            "credentials_complete": bool(effective_key and effective_secret and effective_token),
            "credential_source": "environment" if app_key or app_secret or access_token or refresh_token else "encrypted_database",
            "access_token_expired": bool(access_expiry and access_expiry <= now),
            "refresh_token_expired": bool(refresh_expiry and refresh_expiry <= now),
            "store_code": store["store_code"],
            "store_name": store["store_name"],
            "brand_name": store["brand_name"],
            "environment_prefix": env_prefix,
        }
    )
    if include_secrets:
        result["app_secret"] = effective_secret
        result["access_token"] = effective_token
        result["refresh_token"] = effective_refresh
    result.pop("app_secret_enc", None)
    result.pop("access_token_enc", None)
    result.pop("refresh_token_enc", None)
    return result


def save_api_config(
    db_path: str | Path, payload: dict, user_id: int, store_id: int | None = None
) -> dict:
    environment = str(payload.get("environment") or "production").strip()
    if environment not in {"production", "sandbox"}:
        raise ValueError("唯品会接口环境只能选择正式环境或沙箱环境。")
    app_key = str(payload.get("app_key") or "").strip()
    with get_connection(db_path) as connection:
        store = _vipshop_store(connection, store_id)
        expected_store_name = str(
            payload.get("expected_store_name") or store["brand_name"]
        ).strip()
        if not expected_store_name:
            raise ValueError("请填写预期店铺名称，用于防止授权到错误店铺。")
        existing = connection.execute(
            "SELECT * FROM vipshop_store_api_config WHERE store_id = ?", (store["id"],)
        ).fetchone()
        app_secret_enc = existing["app_secret_enc"]
        access_token_enc = existing["access_token_enc"]
        if str(payload.get("app_secret") or "").strip():
            app_secret_enc = secret_store.seal(db_path, str(payload["app_secret"]).strip())
        if str(payload.get("access_token") or "").strip():
            access_token_enc = secret_store.seal(db_path, str(payload["access_token"]).strip())
        if payload.get("clear_app_secret"):
            app_secret_enc = ""
        if payload.get("clear_access_token"):
            access_token_enc = ""
        connection.execute(
            """
            UPDATE vipshop_store_api_config SET environment = ?, app_key = ?, app_secret_enc = ?,
                access_token_enc = ?, expected_store_name = ?, last_test_status = 'not_tested',
                last_test_message = '', updated_at = ? WHERE store_id = ?
            """,
            (
                environment, app_key, app_secret_enc, access_token_enc,
                expected_store_name, utc_now(), store["id"],
            ),
        )
        default_store = connection.execute("SELECT store_id FROM settings WHERE id = 1").fetchone()
        if default_store and int(default_store["store_id"]) == int(store["id"]):
            connection.execute(
                "UPDATE settings SET api_status = 'configured', updated_at = ? WHERE id = 1",
                (utc_now(),),
            )
        _audit(
            connection, user_id, "vipshop_api_config_saved",
            "vipshop_store_api_config", store["id"], f"environment={environment}",
        )
    return get_api_config(db_path, store["id"])


def create_vipshop_oauth_state(
    db_path: str | Path,
    store_id: int,
    user_id: int,
    redirect_uri: str,
    *,
    ttl_minutes: int = 15,
) -> str:
    redirect_uri = str(redirect_uri or "").strip()
    if not redirect_uri:
        raise ValueError("唯品 OAuth 回调地址不能为空。")
    state = secrets.token_urlsafe(32)
    now = datetime.now(UTC).replace(microsecond=0)
    expires_at = now + timedelta(minutes=max(5, min(int(ttl_minutes), 30)))
    with get_connection(db_path) as connection:
        store = _vipshop_store(connection, store_id)
        connection.execute(
            "UPDATE vipshop_oauth_states SET status = 'expired' WHERE status = 'pending' AND expires_at <= ?",
            (_utc_iso(now),),
        )
        connection.execute(
            """
            INSERT INTO vipshop_oauth_states(
                state_hash, store_id, initiated_by, redirect_uri, status,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                _state_hash(state), store["id"], int(user_id), redirect_uri,
                _utc_iso(now), _utc_iso(expires_at),
            ),
        )
        _audit(
            connection, int(user_id), "vipshop_oauth_started", "stores",
            int(store["id"]), f"redirect_uri={redirect_uri}",
        )
    return state


def consume_vipshop_oauth_state(db_path: str | Path, state: str) -> dict:
    state = str(state or "").strip()
    if not state:
        raise ValueError("唯品授权回调缺少 state。")
    now = datetime.now(UTC).replace(microsecond=0)
    with get_connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT oauth.*, stores.store_code, stores.store_name
            FROM vipshop_oauth_states AS oauth
            JOIN stores ON stores.id = oauth.store_id
            WHERE oauth.state_hash = ?
            """,
            (_state_hash(state),),
        ).fetchone()
        if not row:
            raise ValueError("唯品授权 state 无效，请从货品监控中心重新发起授权。")
        if row["status"] != "pending" or row["consumed_at"]:
            raise ValueError("本次唯品授权已处理，不能重复使用。")
        expires_at = _parse_utc(row["expires_at"])
        if not expires_at or expires_at <= now:
            connection.execute(
                "UPDATE vipshop_oauth_states SET status = 'expired', consumed_at = ? WHERE state_hash = ?",
                (_utc_iso(now), row["state_hash"]),
            )
            raise ValueError("唯品授权请求已过期，请重新发起授权。")
        connection.execute(
            "UPDATE vipshop_oauth_states SET status = 'processing', consumed_at = ? WHERE state_hash = ?",
            (_utc_iso(now), row["state_hash"]),
        )
    return dict(row)


def record_vipshop_oauth_failure(
    db_path: str | Path,
    state_hash: str,
    error_code: str,
    error_description: str,
) -> None:
    with get_connection(db_path) as connection:
        state_row = connection.execute(
            "SELECT store_id FROM vipshop_oauth_states WHERE state_hash = ?",
            (str(state_hash or ""),),
        ).fetchone()
        connection.execute(
            """
            UPDATE vipshop_oauth_states
            SET status = 'failed', error_code = ?, error_description = ?
            WHERE state_hash = ?
            """,
            (
                str(error_code or "oauth-failed")[:120],
                str(error_description or "唯品授权失败")[:500],
                str(state_hash or ""),
            ),
        )
        if state_row:
            connection.execute(
                """
                UPDATE vipshop_store_api_config
                SET last_test_status = 'oauth_failed', last_test_message = ?,
                    last_test_at = ?, updated_at = ?
                WHERE store_id = ?
                """,
                (
                    str(error_description or "唯品授权失败")[:500],
                    utc_now(), utc_now(), int(state_row["store_id"]),
                ),
            )


def save_vipshop_oauth_tokens(
    db_path: str | Path,
    oauth_state: dict,
    token_payload: dict,
) -> dict:
    access_token = str(token_payload.get("access_token") or "").strip()
    refresh_token = str(token_payload.get("refresh_token") or "").strip()
    if not access_token:
        raise ValueError("唯品 OAuth 响应缺少 AccessToken。")
    token_info = token_payload.get("token_info") or {}
    open_id = str(token_payload.get("open_id") or token_info.get("open_id") or "").strip()
    access_expires_at = _duration_expiry(
        token_payload.get("expires_in") or token_info.get("expires_in")
    )
    refresh_expires_at = _absolute_expiry(token_payload.get("refresh_expires_time"))
    store_id = int(oauth_state["store_id"])
    user_id = int(oauth_state["initiated_by"])
    now = utc_now()
    with get_connection(db_path) as connection:
        _vipshop_store(connection, store_id)
        connection.execute(
            """
            UPDATE vipshop_store_api_config
            SET access_token_enc = ?, refresh_token_enc = ?, open_id = ?,
                access_token_expires_at = ?, refresh_token_expires_at = ?,
                oauth_authorized_at = ?, last_test_status = 'oauth_authorized',
                last_test_message = 'OAuth 授权成功，正在校验店铺及接口权限。',
                last_test_at = ?, updated_at = ?
            WHERE store_id = ?
            """,
            (
                secret_store.seal(db_path, access_token),
                secret_store.seal(db_path, refresh_token) if refresh_token else "",
                open_id, access_expires_at, refresh_expires_at,
                now, now, now, store_id,
            ),
        )
        connection.execute(
            "UPDATE vipshop_oauth_states SET status = 'succeeded' WHERE state_hash = ?",
            (oauth_state["state_hash"],),
        )
        default_store = connection.execute("SELECT store_id FROM settings WHERE id = 1").fetchone()
        if default_store and int(default_store["store_id"]) == store_id:
            connection.execute(
                "UPDATE settings SET api_status = 'configured', updated_at = ? WHERE id = 1",
                (now,),
            )
        _audit(
            connection, user_id, "vipshop_oauth_succeeded", "stores",
            store_id, f"open_id={_mask(open_id)}",
        )
    return get_api_config(db_path, store_id)


def vipshop_client_config(db_path: str | Path, store_id: int | None = None):
    from replenishment_center.vipshop import VipshopConfig

    config = get_api_config(db_path, store_id, include_secrets=True)
    return VipshopConfig(
        environment=config["environment"],
        app_key=config["app_key"],
        app_secret=config["app_secret"],
        access_token=config["access_token"],
        expected_store_name=config["expected_store_name"],
    )


def record_api_test(
    db_path: str | Path,
    store_id: int | None = None,
    *,
    status: str,
    message: str,
    store: dict | None = None,
) -> None:
    external_store = store or {}
    with get_connection(db_path) as connection:
        selected_store = _vipshop_store(connection, store_id)
        connection.execute(
            """
            UPDATE vipshop_store_api_config SET last_test_status = ?, last_test_message = ?, last_test_at = ?,
                external_store_id = ?, external_seller_id = ?, verified_store_name = ?, updated_at = ?
            WHERE store_id = ?
            """,
            (
                status, message, utc_now(), str(external_store.get("store_id") or ""),
                str(external_store.get("seller_id") or ""),
                str(external_store.get("store_name") or ""), utc_now(), selected_store["id"],
            ),
        )
        default_store = connection.execute("SELECT store_id FROM settings WHERE id = 1").fetchone()
        if default_store and int(default_store["store_id"]) == int(selected_store["id"]):
            connection.execute(
                "UPDATE settings SET api_status = ?, updated_at = ? WHERE id = 1",
                (status, utc_now()),
            )


def apply_vipshop_data(
    db_path: str | Path,
    data: dict,
    user_id: int | None = None,
    *,
    source: str = "vipshop_api",
    store_id: int | None = None,
) -> dict:
    if source not in {"vipshop_api", "vipshop_browser"}:
        raise ValueError("唯品数据来源不正确。")
    settings = get_settings(db_path)
    counts = {"sku": 0, "sales": 0, "inventory": 0, "unmatched": 0}
    with get_connection(db_path) as connection:
        selected_store = _vipshop_store(connection, store_id)
        selected_store_id = int(selected_store["id"])
        if source == "vipshop_browser" and selected_store_id != int(settings["store_id"]):
            raise ValueError("唯品浏览器报表当前只用于马天奴唯品会店铺。")
        if source == "vipshop_browser":
            sales_end = date.fromisoformat(str(data["sales_through_date"])[:10])
            sales_start = sales_end - timedelta(days=13)
            connection.execute(
                "UPDATE skus SET lifecycle = 'inactive' WHERE store_id = ? AND is_demo = 0",
                (selected_store_id,),
            )
            connection.execute(
                """
                DELETE FROM sales_daily
                WHERE sku_id IN (SELECT id FROM skus WHERE store_id = ?)
                  AND sale_date BETWEEN ? AND ?
                """,
                (selected_store_id, sales_start.isoformat(), sales_end.isoformat()),
            )
        external_lookup: dict[str, int] = {}
        for sku in data.get("skus", []):
            external_id = str(sku.get("external_sku_id") or "").strip()
            style_code = str(sku.get("style_code") or "").strip()
            color_name = str(sku.get("color_name") or "").strip()
            size_name = str(sku.get("size_name") or "").strip()
            if not external_id:
                continue
            if not style_code or not color_name or not size_name:
                connection.execute(
                    """
                    INSERT INTO api_store_unmatched_skus(
                        store_id, external_sku_id, external_spu_id, outer_sku_id, style_code, color_name,
                        size_name, raw_json, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(store_id, external_sku_id) DO UPDATE SET external_spu_id = excluded.external_spu_id,
                        outer_sku_id = excluded.outer_sku_id, style_code = excluded.style_code,
                        color_name = excluded.color_name, size_name = excluded.size_name,
                        raw_json = excluded.raw_json, last_seen_at = excluded.last_seen_at
                    """,
                    (
                        selected_store_id, external_id, str(sku.get("external_spu_id") or ""), str(sku.get("outer_sku_id") or ""),
                        style_code, color_name, size_name, json.dumps(sku, ensure_ascii=False), utc_now(),
                    ),
                )
                counts["unmatched"] += 1
                continue
            existing = connection.execute(
                "SELECT id FROM skus WHERE store_id = ? AND external_sku_id = ?",
                (selected_store_id, external_id),
            ).fetchone()
            if not existing:
                existing = connection.execute(
                    "SELECT id FROM skus WHERE store_id = ? AND style_code = ? AND color_name = ? AND size_name = ?",
                    (selected_store_id, style_code, color_name, size_name),
                ).fetchone()
            if existing:
                sku_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE skus SET external_sku_id = ?, external_spu_id = ?, outer_sku_id = ?,
                        style_code = ?, style_name = ?, color_name = ?, size_name = ?, lifecycle = 'active', is_demo = 0
                    WHERE id = ?
                    """,
                    (
                        external_id, str(sku.get("external_spu_id") or ""), str(sku.get("outer_sku_id") or ""),
                        style_code, str(sku.get("style_name") or style_code), color_name, size_name, sku_id,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO skus(
                        store_id, style_code, style_name, color_name, size_name, category, supplier,
                        lead_time_days, moq, pack_size, default_size_share, core_size, lifecycle,
                        external_sku_id, external_spu_id, outer_sku_id, is_demo
                    ) VALUES (?, ?, ?, ?, ?, '', '', 14, 0, 1, 0, ?, 'active', ?, ?, ?, 0)
                    """,
                    (
                        selected_store_id, style_code, str(sku.get("style_name") or style_code), color_name,
                        size_name, 1 if size_name.upper() in {"M", "L"} else 0, external_id,
                        str(sku.get("external_spu_id") or ""), str(sku.get("outer_sku_id") or ""),
                    ),
                )
                sku_id = int(cursor.lastrowid)
            external_lookup[external_id] = sku_id
            connection.execute(
                "DELETE FROM api_store_unmatched_skus WHERE store_id = ? AND external_sku_id = ?",
                (selected_store_id, external_id),
            )
            counts["sku"] += 1
        if not external_lookup:
            raise ValueError("唯品会返回的 SKU 缺少可用款号、颜色或尺码映射，未覆盖现有试跑数据。")
        connection.execute(
            "UPDATE skus SET lifecycle = 'inactive' WHERE store_id = ? AND is_demo = 1",
            (selected_store_id,),
        )
        for row in data.get("sales", []):
            sku_id = external_lookup.get(str(row.get("external_sku_id") or ""))
            if not sku_id:
                continue
            gross = max(0, int(row.get("gross_units") or 0))
            returns = max(0, int(row.get("return_units") or 0))
            connection.execute(
                """
                INSERT INTO sales_daily(sku_id, sale_date, gross_units, return_units, net_units, source)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(sku_id, sale_date) DO UPDATE SET gross_units = excluded.gross_units,
                    return_units = excluded.return_units, net_units = excluded.net_units, source = excluded.source
                """,
                (sku_id, row["sale_date"], gross, returns, max(0, gross - returns), source),
            )
            counts["sales"] += 1
        for row in data.get("inventory", []):
            sku_id = external_lookup.get(str(row.get("external_sku_id") or ""))
            if not sku_id:
                continue
            connection.execute(
                """
                INSERT INTO inventory_current(
                    sku_id, snapshot_at, on_hand, locked, defective, inbound, inbound_date, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sku_id) DO UPDATE SET snapshot_at = excluded.snapshot_at,
                    on_hand = excluded.on_hand, locked = excluded.locked, defective = excluded.defective,
                    inbound = excluded.inbound, inbound_date = excluded.inbound_date, source = excluded.source
                """,
                (
                    sku_id, data["inventory_snapshot_at"], max(0, int(row.get("on_hand") or 0)),
                    max(0, int(row.get("locked") or 0)), max(0, int(row.get("defective") or 0)),
                    max(0, int(row.get("inbound") or 0)), str(row.get("inbound_date") or ""), source,
                ),
            )
            counts["inventory"] += 1
        store = data.get("store") or {}
        source_label = "唯品会 API" if source == "vipshop_api" else "唯品会浏览器报表"
        message = (
            f"{source_label}同步成功：商品 {data.get('product_count', 0)} 款，"
            f"匹配 SKU {counts['sku']} 个，待匹配 {counts['unmatched']} 个。"
        )
        cursor = connection.execute(
            """
            INSERT INTO sync_runs(
                store_id, source, status, sales_through_date, inventory_snapshot_at, row_count, message, created_at
            ) VALUES (?, ?, 'success', ?, ?, ?, ?, ?)
            """,
            (
                selected_store_id, source, data["sales_through_date"], data["inventory_snapshot_at"],
                counts["sku"] + counts["sales"] + counts["inventory"], message, utc_now(),
            ),
        )
        if source == "vipshop_api":
            connection.execute(
                """
                UPDATE vipshop_store_api_config SET last_test_status = 'connected', last_test_message = ?,
                    last_test_at = ?, last_sync_at = ?, external_store_id = ?, external_seller_id = ?,
                    verified_store_name = ?, updated_at = ? WHERE store_id = ?
                """,
                (
                    message, utc_now(), utc_now(), str(store.get("store_id") or ""),
                    str(store.get("seller_id") or ""), str(store.get("store_name") or ""),
                    utc_now(), selected_store_id,
                ),
            )
            if selected_store_id == int(settings["store_id"]):
                connection.execute(
                    "UPDATE settings SET data_source_mode = 'api', api_status = 'connected', updated_at = ? WHERE id = 1",
                    (utc_now(),),
                )
            audit_action = "vipshop_api_sync"
        else:
            connection.execute(
                "UPDATE settings SET data_source_mode = 'browser', updated_at = ? WHERE id = 1",
                (utc_now(),),
            )
            audit_action = "vipshop_browser_sync"
        _audit(connection, user_id, audit_action, "sync_run", cursor.lastrowid, message)
    counts["message"] = message
    return counts


def apply_browser_report_data(db_path: str | Path, data: dict, user_id: int | None = None) -> dict:
    return apply_vipshop_data(db_path, data, user_id=user_id, source="vipshop_browser")


def record_api_sync_failure(
    db_path: str | Path,
    message: str,
    user_id: int | None = None,
    *,
    store_id: int | None = None,
) -> None:
    settings = get_settings(db_path)
    with get_connection(db_path) as connection:
        selected_store = _vipshop_store(connection, store_id)
        selected_store_id = int(selected_store["id"])
        cursor = connection.execute(
            """
            INSERT INTO sync_runs(store_id, source, status, message, created_at)
            VALUES (?, 'vipshop_api', 'failed', ?, ?)
            """,
            (selected_store_id, message, utc_now()),
        )
        connection.execute(
            "UPDATE vipshop_store_api_config SET last_test_status = 'failed', last_test_message = ?, last_test_at = ?, updated_at = ? WHERE store_id = ?",
            (message, utc_now(), utc_now(), selected_store_id),
        )
        if selected_store_id == int(settings["store_id"]):
            connection.execute(
                "UPDATE settings SET api_status = 'failed', updated_at = ? WHERE id = 1",
                (utc_now(),),
            )
        _audit(connection, user_id, "vipshop_api_sync_failed", "sync_run", cursor.lastrowid, message)


def get_browser_capture_config(db_path: str | Path) -> dict:
    from replenishment_center.browser_capture import browser_paths

    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM browser_capture_config WHERE id = 1").fetchone()
    result = dict(row)
    result.update({key: str(value) for key, value in browser_paths(db_path).items()})
    return result


def save_browser_report_url(db_path: str | Path, kind: str, url: str, user_id: int) -> None:
    from replenishment_center.browser_capture import is_official_report_url

    if kind not in {"sales", "inventory", "master"}:
        raise ValueError("报表类型不正确。")
    clean_url = str(url or "").strip()
    if not is_official_report_url(clean_url):
        raise ValueError("只能记录唯品 VIS 或魔方罗盘的官方报表页面。")
    column = {"sales": "sales_report_url", "inventory": "inventory_report_url", "master": "master_report_url"}[kind]
    with get_connection(db_path) as connection:
        connection.execute(f"UPDATE browser_capture_config SET {column} = ?, updated_at = ? WHERE id = 1", (clean_url, utc_now()))
        _audit(connection, user_id, "browser_report_url_saved", "browser_capture_config", 1, f"{kind}:{clean_url}")


def record_browser_status(db_path: str | Path, status: dict) -> None:
    current = status.get("current") or {}
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE browser_capture_config SET session_status = ?, current_url = ?, current_title = ?,
                last_check_message = ?, last_check_at = ?, updated_at = ? WHERE id = 1
            """,
            (
                str(status.get("sessionStatus") or "unknown"), str(current.get("url") or ""),
                str(current.get("title") or ""), str(status.get("message") or ""), utc_now(), utc_now(),
            ),
        )


def create_browser_capture_job(db_path: str | Path, kind: str, user_id: int | None) -> int:
    if kind not in {"sales", "inventory", "master"}:
        raise ValueError("报表类型不正确。")
    with get_connection(db_path) as connection:
        pending = connection.execute(
            "SELECT id FROM browser_capture_jobs WHERE kind = ? AND status = 'waiting_download' ORDER BY id DESC LIMIT 1",
            (kind,),
        ).fetchone()
        if pending:
            return int(pending["id"])
        cursor = connection.execute(
            """
            INSERT INTO browser_capture_jobs(kind, status, started_epoch, message, created_by, created_at)
            VALUES (?, 'waiting_download', ?, '等待在唯品专用浏览器中导出报表', ?, ?)
            """,
            (kind, time.time(), user_id, utc_now()),
        )
        _audit(connection, user_id, "browser_capture_started", "browser_capture_job", cursor.lastrowid, kind)
        return int(cursor.lastrowid)


def list_browser_capture_jobs(db_path: str | Path, limit: int = 20) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute("SELECT * FROM browser_capture_jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["analysis"] = json.loads(item["analysis_json"] or "{}")
        except json.JSONDecodeError:
            item["analysis"] = {}
        result.append(item)
    return result


def mark_browser_capture_jobs_imported(
    db_path: str | Path, job_ids: list[int], plan_id: int, user_id: int | None
) -> None:
    clean_ids = sorted({int(job_id) for job_id in job_ids})
    if not clean_ids:
        return
    placeholders = ",".join("?" for _ in clean_ids)
    with get_connection(db_path) as connection:
        connection.execute(
            f"UPDATE browser_capture_jobs SET status = 'imported', imported_plan_id = ?, message = ? WHERE id IN ({placeholders})",
            (plan_id, f"已导入并生成补货计划 #{plan_id}", *clean_ids),
        )
        _audit(connection, user_id, "browser_reports_imported", "plan", plan_id, f"jobs={clean_ids}")


def import_latest_browser_reports(db_path: str | Path, user_id: int | None = None) -> dict:
    from replenishment_center.browser_import import normalize_browser_reports

    latest = {}
    for job in list_browser_capture_jobs(db_path, limit=100):
        if job["kind"] in {"sales", "inventory", "master"} and job["archive_file"] and job["kind"] not in latest:
            latest[job["kind"]] = job
    missing = [kind for kind in ("sales", "inventory", "master") if kind not in latest]
    if missing:
        raise ValueError("请先接收最近14天商品明细和商品基础信息主数据。")
    if latest["sales"]["file_sha256"] != latest["inventory"]["file_sha256"]:
        raise ValueError("销售与库存任务不是同一份联合报表，请重新采集商品明细。")
    not_ready = [
        kind for kind in ("sales", "inventory")
        if latest[kind]["status"] not in {"ready_for_import", "imported"}
    ]
    if not_ready:
        raise ValueError("销售或库存报表尚未通过字段校验。")
    if latest["sales"]["status"] == "imported" and latest["inventory"]["status"] == "imported":
        raise ValueError("这组报表已经导入，请勿重复生成计划。")

    settings = get_settings(db_path)
    normalized = normalize_browser_reports(
        latest["sales"]["archive_file"],
        latest["master"]["archive_file"],
        expected_brand=settings["brand_name"],
    )
    counts = apply_browser_report_data(db_path, normalized, user_id)
    plan_id = generate_plan(db_path, generation_type="browser_report", created_by=user_id, force=True)
    job_ids = [latest["sales"]["id"], latest["inventory"]["id"]]
    if latest["master"]["status"] != "imported":
        job_ids.append(latest["master"]["id"])
    mark_browser_capture_jobs_imported(db_path, job_ids, plan_id, user_id)
    return {"plan_id": plan_id, "counts": counts, "stats": normalized["stats"], "job_ids": job_ids}


def process_browser_capture_jobs(db_path: str | Path) -> list[int]:
    from replenishment_center.browser_capture import archive_report, candidate_downloads, inspect_report, sha256_file

    completed = []
    with get_connection(db_path) as connection:
        pending_jobs = connection.execute(
            "SELECT * FROM browser_capture_jobs WHERE status = 'waiting_download' ORDER BY id"
        ).fetchall()
        if not pending_jobs:
            return completed
        known_files = {
            (row["kind"], row["file_sha256"])
            for row in connection.execute("SELECT kind, file_sha256 FROM browser_capture_jobs WHERE file_sha256 <> ''")
        }
        earliest_start = min(float(job["started_epoch"]) for job in pending_jobs)
        available_jobs = {int(job["id"]): job for job in pending_jobs}
        for candidate in candidate_downloads(db_path, earliest_start):
            digest = sha256_file(candidate)
            eligible = [
                job for job in available_jobs.values()
                if candidate.stat().st_mtime >= float(job["started_epoch"])
                and (job["kind"], digest) not in known_files
            ]
            if not eligible:
                continue

            analyses = {int(job["id"]): inspect_report(candidate, job["kind"]) for job in eligible}
            exact_jobs = [job for job in eligible if not (analyses[int(job["id"])].get("missing") or [])]
            if exact_jobs:
                selected_jobs = exact_jobs
            else:
                selected_jobs = [
                    min(
                        eligible,
                        key=lambda item: len(analyses[int(item["id"])].get("missing") or []),
                    )
                ]
            for job in selected_jobs:
                analysis = analyses[int(job["id"])]
                archive = archive_report(db_path, candidate, job["kind"])
                missing = analysis.get("missing") or []
                if not analysis.get("supported"):
                    status = "needs_conversion"
                    message = "文件已留档，需要解压或转换为 xlsx/csv。"
                elif missing:
                    status = "needs_mapping"
                    message = f"文件已接收，待确认字段映射：{', '.join(missing)}"
                else:
                    status = "ready_for_import"
                    message = f"文件校验通过，共 {analysis.get('row_count', 0)} 行。"
                connection.execute(
                    """
                    UPDATE browser_capture_jobs SET status = ?, source_file = ?, archive_file = ?,
                        file_name = ?, file_size = ?, file_sha256 = ?, analysis_json = ?, message = ?,
                        completed_at = ? WHERE id = ?
                    """,
                    (
                        status, str(candidate), str(archive), candidate.name, candidate.stat().st_size, digest,
                        json.dumps(analysis, ensure_ascii=False), message, utc_now(), job["id"],
                    ),
                )
                known_files.add((job["kind"], digest))
                completed.append(int(job["id"]))
                available_jobs.pop(int(job["id"]), None)
            if not available_jobs:
                break
    return completed


def authenticate(db_path: str | Path, username: str, password: str) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM users WHERE username = ? AND is_active = 1", (username.strip(),)
        ).fetchone()
    if row and verify_password(password, row["password_hash"]):
        return dict(row)
    return None


def get_user(db_path: str | Path, user_id: int) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (user_id,)).fetchone()
    return dict(row) if row else None


def get_settings(db_path: str | Path) -> dict:
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT settings.*, stores.platform_name, stores.store_name, stores.store_code, stores.brand_name
            FROM settings JOIN stores ON stores.id = settings.store_id WHERE settings.id = 1
            """
        ).fetchone()
    result = dict(row)
    result["schedule_weekdays"] = json.loads(result["schedule_weekdays"] or "[]")
    return result


def _latest_cost(connection: sqlite3.Connection, sku_id: int, as_of: str | None = None) -> float | None:
    as_of = as_of or local_today().isoformat()
    row = connection.execute(
        "SELECT unit_cost FROM sku_cost_versions WHERE sku_id = ? AND effective_from <= ? ORDER BY effective_from DESC, id DESC LIMIT 1",
        (sku_id, as_of),
    ).fetchone()
    return float(row["unit_cost"]) if row else None


def cost_summary(db_path: str | Path, *, store_id: int | None = None) -> dict:
    with get_connection(db_path) as connection:
        selected_store_id = int(store_id or get_settings(db_path)["store_id"])
        rows = connection.execute("SELECT id, style_code, outer_sku_id, color_name FROM skus WHERE store_id = ? AND lifecycle = 'active'", (selected_store_id,)).fetchall()
        groups = defaultdict(list)
        for row in rows:
            groups[(row["style_code"], row["outer_sku_id"] or row["color_name"])].append(row)
        covered = sum(all(_latest_cost(connection, int(row["id"])) is not None for row in group) for group in groups.values())
    return {"sku_count": len(groups), "covered_count": covered, "missing_count": len(groups) - covered}


def product_monitor_data(db_path: str | Path, *, store_id: int | None = None) -> dict:
    settings = get_settings(db_path)
    selected_store_id = int(store_id or settings["store_id"])
    with get_connection(db_path) as connection:
        latest_sale = connection.execute("SELECT MAX(sale_date) AS value FROM sales_daily WHERE sku_id IN (SELECT id FROM skus WHERE store_id = ?)", (selected_store_id,)).fetchone()["value"]
        today = date.fromisoformat(str(latest_sale)[:10]) if latest_sale else local_today()
        month_start = today.replace(day=1).isoformat()
        start_14 = (today - timedelta(days=13)).isoformat()
        start_60 = (today - timedelta(days=59)).isoformat()
        skus = [dict(row) for row in connection.execute("SELECT * FROM skus WHERE store_id = ? AND lifecycle = 'active' ORDER BY style_code, color_name, size_name", (selected_store_id,)).fetchall()]
        sales = connection.execute(
            "SELECT sku_id, sale_date, gross_units, return_units, net_units, gross_sales_amount, refund_amount, net_sales_amount FROM sales_daily WHERE sku_id IN (SELECT id FROM skus WHERE store_id = ?) AND sale_date >= ?",
            (selected_store_id, start_60),
        ).fetchall()
        sales_by = defaultdict(list)
        for row in sales:
            sales_by[int(row["sku_id"])].append(dict(row))
        inventory = {int(row["sku_id"]): dict(row) for row in connection.execute("SELECT * FROM inventory_current WHERE sku_id IN (SELECT id FROM skus WHERE store_id = ?)", (selected_store_id,)).fetchall()}
        rows = []
        for sku in skus:
            sku_sales = sales_by.get(int(sku["id"]), [])
            sales_14 = sum(int(row["net_units"] or 0) for row in sku_sales if row["sale_date"] >= start_14)
            sales_30 = sum(int(row["net_units"] or 0) for row in sku_sales if row["sale_date"] >= max(start_60, (today - timedelta(days=29)).isoformat()))
            month_units = sum(int(row["net_units"] or 0) for row in sku_sales if row["sale_date"] >= month_start)
            amount = sum(float(row["net_sales_amount"] or 0) for row in sku_sales if row["sale_date"] >= month_start)
            if amount == 0 and sku["current_price"]:
                amount = month_units * float(sku["current_price"])
            inv = inventory.get(int(sku["id"]), {})
            sellable = max(0, int(inv.get("on_hand", 0)) - int(inv.get("locked", 0)) - int(inv.get("defective", 0)))
            daily_demand = sales_14 / 14 if sales_14 else 0
            coverage = sellable / daily_demand if daily_demand else None
            cost = _latest_cost(connection, int(sku["id"]))
            price = float(sku["current_price"] or 0)
            margin = (price - cost) / price if price and cost is not None else None
            sell_through = sales_14 / (sales_14 + sellable) if sales_14 + sellable else 0
            if coverage is not None and coverage <= 14 and sales_14 >= 5:
                health = "risk"
            elif sales_14 <= 2 and (coverage is None or coverage > 60):
                health = "slow"
            elif sales_14 >= 10 and (coverage is None or coverage <= 30):
                health = "potential"
            else:
                health = "normal"
            rows.append({
                **sku, "sales_14": sales_14, "sales_30": sales_30, "month_units": month_units,
                "month_amount": amount, "sellable": sellable, "coverage_days": coverage,
                "unit_cost": cost, "margin": margin, "sell_through": sell_through, "health": health,
                "inventory_snapshot_at": inv.get("snapshot_at", ""),
            })
        total_amount = sum(row["month_amount"] for row in rows)
        cost_complete = all(row["unit_cost"] is not None for row in rows if row["month_units"] > 0)
        total_cost = sum(row["month_units"] * row["unit_cost"] for row in rows if row["unit_cost"] is not None)
        total_sales_14 = sum(row["sales_14"] for row in rows)
        total_sellable = sum(row["sellable"] for row in rows)
        exact_amount_rows = connection.execute("SELECT COUNT(*) AS c FROM sales_daily WHERE sku_id IN (SELECT id FROM skus WHERE store_id = ?) AND sale_date >= ? AND net_sales_amount != 0", (selected_store_id, month_start)).fetchone()["c"]
    grouped = defaultdict(list)
    for row in rows:
        if row["sales_14"] <= 0 and row["sellable"] <= 0:
            continue
        grouped[(row["style_code"], row.get("outer_sku_id") or row["color_name"])].append(row)
    goods_rows = []
    for (_, goods_code), group in grouped.items():
        sales_14 = sum(row["sales_14"] for row in group)
        sales_30 = sum(row["sales_30"] for row in group)
        month_units = sum(row["month_units"] for row in group)
        month_amount = sum(row["month_amount"] for row in group)
        sellable = sum(row["sellable"] for row in group)
        coverage = sellable / (sales_14 / 14) if sales_14 else None
        sell_through = sales_14 / (sales_14 + sellable) if sales_14 + sellable else 0
        costs = [row["unit_cost"] for row in group if row["unit_cost"] is not None]
        prices = [row["current_price"] for row in group if row["current_price"]]
        cost = sum(costs) / len(costs) if len(costs) == len(group) and costs else None
        price = sum(prices) / len(prices) if prices else 0
        margin = (price - cost) / price if price and cost is not None else None
        if coverage is not None and coverage <= 14 and sales_14 >= 5:
            health = "risk"
        elif sales_14 <= 2 and (coverage is None or coverage > 60):
            health = "slow"
        elif sales_14 >= 10 and (coverage is None or coverage <= 30):
            health = "potential"
        else:
            health = "normal"
        goods_rows.append({
            **group[0], "outer_sku_id": goods_code, "sales_14": sales_14, "sales_30": sales_30,
            "month_units": month_units, "month_amount": month_amount, "sellable": sellable,
            "coverage_days": coverage, "sell_through": sell_through, "unit_cost": cost,
            "current_price": price, "margin": margin, "health": health,
            "inventory_snapshot_at": max((row["inventory_snapshot_at"] for row in group), default=""),
        })
    goods_rows.sort(key=lambda row: ({"risk": 0, "potential": 1, "slow": 2, "normal": 3}.get(row["health"], 9), row["sales_14"] * -1))
    lifecycle_counts = Counter(row["lifecycle"] for row in goods_rows)
    return {
        "rows": goods_rows,
        "sku_count": len(goods_rows),
        "sales_amount": total_amount,
        "cost_amount": total_cost,
        "gross_margin": (total_amount - total_cost) / total_amount if total_amount and cost_complete else None,
        "gross_margin_basis": "actual_amount" if exact_amount_rows else "current_price_estimate",
        "sell_through": total_sales_14 / (total_sales_14 + total_sellable) if total_sales_14 + total_sellable else 0,
        "sales_14": total_sales_14,
        "sellable": total_sellable,
        "risk_count": sum(row["health"] == "risk" for row in goods_rows),
        "slow_count": sum(row["health"] == "slow" for row in goods_rows),
        "potential_count": sum(row["health"] == "potential" for row in goods_rows),
        "missing_cost_count": sum(row["unit_cost"] is None for row in goods_rows),
        "lifecycle_counts": dict(lifecycle_counts),
        "snapshot_at": max((row["inventory_snapshot_at"] for row in goods_rows), default=""),
    }


def list_price_plans(db_path: str | Path, *, store_id: int | None = None, limit: int = 12) -> list[dict]:
    with get_connection(db_path) as connection:
        if store_id is None:
            rows = connection.execute("SELECT price_plans.*, stores.store_name FROM price_plans JOIN stores ON stores.id = price_plans.store_id ORDER BY price_plans.id DESC LIMIT ?", (limit,)).fetchall()
        else:
            rows = connection.execute("SELECT price_plans.*, stores.store_name FROM price_plans JOIN stores ON stores.id = price_plans.store_id WHERE store_id = ? ORDER BY price_plans.id DESC LIMIT ?", (int(store_id), limit)).fetchall()
    return [dict(row) for row in rows]


def get_price_plan(db_path: str | Path, plan_id: int) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT price_plans.*, stores.platform_name, stores.store_name, stores.brand_name FROM price_plans JOIN stores ON stores.id = price_plans.store_id WHERE price_plans.id = ?", (plan_id,)).fetchone()
    return dict(row) if row else None


def get_price_items(db_path: str | Path, plan_id: int, *, include_internal: bool = True) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute("SELECT * FROM price_items WHERE price_plan_id = ? ORDER BY style_code, outer_sku_id, color_name, size_name", (plan_id,)).fetchall()
    result = [dict(row) for row in rows]
    if not include_internal:
        for row in result:
            for key in ("unit_cost", "current_margin", "proposed_margin", "floor_price"):
                row.pop(key, None)
    return result


def generate_price_plan(db_path: str | Path, *, store_id: int, user_id: int, mode: str = "system", rule_payload: dict | None = None) -> int:
    payload = dict(rule_payload or {})
    target_margin = float(payload.get("target_margin") or 0.55)
    if target_margin > 1:
        target_margin /= 100
    discount_rate = float(payload.get("discount_rate") or 0) / 100
    if not 0 <= target_margin < 0.95:
        raise ValueError("目标毛利率需在 0%–95% 之间。")
    if not 0 <= discount_rate <= 0.9:
        raise ValueError("手工调价幅度需在 0%–90% 之间。")
    today = local_today()
    with get_connection(db_path) as connection:
        store = connection.execute("SELECT * FROM stores WHERE id = ?", (store_id,)).fetchone()
        if not store:
            raise ValueError("店铺不存在。")
        batch_no = f"{store['store_code']}-PRICE-{today.strftime('%Y%m%d')}-{connection.execute('SELECT COUNT(*) AS c FROM price_plans WHERE store_id = ? AND date(created_at) = ?', (store_id, today.isoformat())).fetchone()['c'] + 1:02d}"
        cursor = connection.execute("INSERT INTO price_plans(store_id, batch_no, mode, status, rule_label, rule_payload, created_by, created_at) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?)", (store_id, batch_no, mode, "系统建议" if mode == "system" else "手工规则", json.dumps(payload, ensure_ascii=False), user_id, utc_now()))
        plan_id = int(cursor.lastrowid)
        start_14 = (today - timedelta(days=13)).isoformat()
        sku_groups = defaultdict(list)
        for sku_row in connection.execute("SELECT * FROM skus WHERE store_id = ? AND lifecycle = 'active' ORDER BY style_code, color_name, size_name", (store_id,)).fetchall():
            sku_groups[(sku_row["style_code"], sku_row["outer_sku_id"] or sku_row["color_name"])].append(dict(sku_row))
        for _, group in sku_groups.items():
            sku = group[0]
            sales_14 = 0
            sellable = 0
            coverage_rows = []
            for size_sku in group:
                sales_14 += int(connection.execute("SELECT COALESCE(SUM(net_units), 0) AS c FROM sales_daily WHERE sku_id = ? AND sale_date >= ?", (size_sku["id"], start_14)).fetchone()["c"])
                inv = connection.execute("SELECT * FROM inventory_current WHERE sku_id = ?", (size_sku["id"],)).fetchone()
                available = max(0, int(inv["on_hand"] if inv else 0) - int(inv["locked"] if inv else 0) - int(inv["defective"] if inv else 0))
                sellable += available
                coverage_rows.append((size_sku, available))
            if sales_14 <= 0 and sellable <= 0:
                continue
            coverage = sellable / (sales_14 / 14) if sales_14 else None
            costs = [_latest_cost(connection, size_sku["id"], today.isoformat()) for size_sku in group]
            cost = sum(costs) / len(costs) if costs and all(value is not None for value in costs) else None
            current_price = float(sku["current_price"] or 0)
            floor_price = cost / (1 - target_margin) if cost is not None else 0
            decision, reason = "hold", "销量与库存均处于日常区间，暂不调整"
            proposed = current_price
            if not current_price or cost is None:
                decision, reason = "excluded", "价格或成本缺失，不进入自动建议"
            elif mode == "manual":
                proposed = max(floor_price, current_price * (1 - discount_rate))
                decision, reason = "manual", f"手工规则：当前价下调 {discount_rate * 100:g}%；不低于目标毛利率保护价"
            elif sales_14 <= 2 and (coverage is None or coverage > 60):
                proposed = max(floor_price, current_price * 0.9)
                decision, reason = "markdown", "近14天低动销且库存支撑较高，建议清理库存"
            elif current_price and cost is not None and (current_price - cost) / current_price < target_margin:
                proposed = max(floor_price, current_price * 1.08)
                decision, reason = "margin_protect", "当前毛利率低于目标，建议先保护毛利"
            elif sales_14 >= 10 and coverage is not None and coverage <= 14:
                decision, reason = "hold", "高动销且库存偏紧，暂不降价，优先补货"
            proposed = round(proposed / 10) * 10 if proposed else 0
            proposed = max(proposed, round(floor_price / 10) * 10) if floor_price else proposed
            current_margin = (current_price - cost) / current_price if current_price and cost is not None else None
            proposed_margin = (proposed - cost) / proposed if proposed and cost is not None else None
            connection.execute("INSERT INTO price_items(price_plan_id, sku_id, style_code, outer_sku_id, style_name, color_name, size_name, current_price, proposed_price, confirmed_price, floor_price, unit_cost, current_margin, proposed_margin, sales_14, sellable, coverage_days, decision, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (plan_id, sku["id"], sku["style_code"], sku.get("outer_sku_id", ""), sku["style_name"], sku["color_name"], "整货号", current_price, proposed, proposed, floor_price, cost, current_margin, proposed_margin, sales_14, sellable, coverage, decision, reason))
        _audit(connection, user_id, "price_plan_generated", "price_plan", plan_id, mode)
    return plan_id


def save_price_plan(db_path: str | Path, plan_id: int, prices: dict[int, float], include_flags: dict[int, int], user_id: int, *, confirm: bool = False) -> None:
    with get_connection(db_path) as connection:
        plan = connection.execute("SELECT * FROM price_plans WHERE id = ?", (plan_id,)).fetchone()
        if not plan or plan["status"] not in {"draft", "confirmed"}:
            raise ValueError("当前调价批次不能继续修改。")
        for row in connection.execute("SELECT id, proposed_price FROM price_items WHERE price_plan_id = ?", (plan_id,)).fetchall():
            value = max(0, float(prices.get(int(row["id"]), row["proposed_price"])))
            connection.execute("UPDATE price_items SET confirmed_price = ?, include_flag = ? WHERE id = ?", (value, int(include_flags.get(int(row["id"]), 1)), row["id"]))
        if confirm:
            connection.execute("UPDATE price_plans SET status = 'confirmed', confirmed_by = ?, confirmed_at = ? WHERE id = ?", (user_id, utc_now(), plan_id))
        _audit(connection, user_id, "price_plan_saved" if not confirm else "price_plan_confirmed", "price_plan", plan_id, "商品部调价确认")


def mark_price_plan_exported(db_path: str | Path, plan_id: int, user_id: int) -> None:
    with get_connection(db_path) as connection:
        connection.execute("UPDATE price_plans SET status = 'exported', exported_at = ? WHERE id = ?", (utc_now(), plan_id))
        _audit(connection, user_id, "price_plan_exported", "price_plan", plan_id, "导出运营执行明细")


def update_settings(db_path: str | Path, payload: dict, user_id: int) -> None:
    weekdays = sorted({int(day) for day in payload.get("schedule_weekdays", []) if int(day) in range(1, 8)})
    if not weekdays:
        raise ValueError("请至少选择一个补货计划生成日。")
    schedule_time = str(payload.get("schedule_time") or "").strip()
    try:
        datetime.strptime(schedule_time, "%H:%M")
    except ValueError as exc:
        raise ValueError("生成时间格式不正确。") from exc
    target_days = int(payload.get("target_days") or 45)
    safety_days = int(payload.get("safety_days") or 7)
    min_sales_7 = int(payload.get("min_sales_7") or 5)
    min_sales_14 = int(payload.get("min_sales_14") or 10)
    min_consecutive_sales_days = int(payload.get("min_consecutive_sales_days") or 3)
    max_coverage_days = float(payload.get("max_coverage_days") or 14)
    if target_days not in {30, 45, 60}:
        raise ValueError("目标覆盖天数只能选择 30、45 或 60 天。")
    if not 0 <= safety_days <= 30:
        raise ValueError("安全库存天数需在 0–30 天之间。")
    if min_sales_7 < 0 or min_sales_14 < 0:
        raise ValueError("销量筛选门槛不能小于 0。")
    if min_sales_14 < min_sales_7:
        raise ValueError("近14天销量门槛不能小于近7天销量门槛。")
    if not 1 <= min_consecutive_sales_days <= 14:
        raise ValueError("连续有销量天数需在 1–14 天之间。")
    if not 1 <= max_coverage_days <= 90:
        raise ValueError("库存支撑天数需在 1–90 天之间。")
    with get_connection(db_path) as connection:
        connection.execute(
            """
            UPDATE settings SET schedule_weekdays = ?, schedule_time = ?, auto_generate = ?,
                target_days = ?, safety_days = ?, min_sales_7 = ?, min_sales_14 = ?,
                min_consecutive_sales_days = ?, max_coverage_days = ?, updated_at = ? WHERE id = 1
            """,
            (
                json.dumps(weekdays), schedule_time, int(bool(payload.get("auto_generate"))), target_days,
                safety_days, min_sales_7, min_sales_14, min_consecutive_sales_days,
                max_coverage_days, utc_now(),
            ),
        )
        _audit(connection, user_id, "settings_updated", "settings", 1, json.dumps(payload, ensure_ascii=False))


def latest_sync(db_path: str | Path, *, store_id: int | None = None) -> dict | None:
    with get_connection(db_path) as connection:
        if store_id is None:
            row = connection.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM sync_runs WHERE store_id = ? ORDER BY id DESC LIMIT 1",
                (int(store_id),),
            ).fetchone()
    return dict(row) if row else None


def record_test_sync(db_path: str | Path, *, source: str = "scheduled_test") -> dict:
    settings = get_settings(db_path)
    today = local_today()
    with get_connection(db_path) as connection:
        snapshot = connection.execute("SELECT MAX(snapshot_at) AS value FROM inventory_current").fetchone()["value"] or ""
        row_count = connection.execute("SELECT COUNT(*) AS count FROM inventory_current").fetchone()["count"]
        cursor = connection.execute(
            """
            INSERT INTO sync_runs(store_id, source, status, sales_through_date, inventory_snapshot_at, row_count, message, created_at)
            VALUES (?, ?, 'success', ?, ?, ?, '试跑模式校验完成；保留当前单店测试数据。', ?)
            """,
            (settings["store_id"], source, today.isoformat(), snapshot, row_count, utc_now()),
        )
        _audit(connection, None, "test_sync", "sync_run", cursor.lastrowid, source)
        return {"id": cursor.lastrowid, "status": "success", "row_count": row_count}


def record_browser_schedule_issue(db_path: str | Path, message: str, *, session_available: bool) -> dict:
    settings = get_settings(db_path)
    status = "action_required" if session_available else "failed"
    with get_connection(db_path) as connection:
        sales_through_date = connection.execute(
            "SELECT MAX(sale_date) AS value FROM sales_daily"
        ).fetchone()["value"] or ""
        inventory_snapshot_at = connection.execute(
            "SELECT MAX(snapshot_at) AS value FROM inventory_current"
        ).fetchone()["value"] or ""
        row_count = connection.execute("SELECT COUNT(*) AS count FROM inventory_current").fetchone()["count"]
        cursor = connection.execute(
            """
            INSERT INTO sync_runs(
                store_id, source, status, sales_through_date, inventory_snapshot_at, row_count, message, created_at
            ) VALUES (?, 'vipshop_browser_schedule', ?, ?, ?, ?, ?, ?)
            """,
            (
                settings["store_id"], status, sales_through_date, inventory_snapshot_at,
                row_count, message, utc_now(),
            ),
        )
        _audit(connection, None, "vipshop_browser_schedule_blocked", "sync_run", cursor.lastrowid, message)
        _notify_roles(
            connection,
            ("merchandise", "admin"),
            "唯品数据更新未完成",
            message,
            "/data/browser",
            store_id=settings["store_id"],
        )
        return {"id": cursor.lastrowid, "status": status, "row_count": row_count}


def generate_plan(
    db_path: str | Path,
    *,
    generation_type: str = "manual",
    created_by: int | None = None,
    force: bool = False,
    store_id: int | None = None,
) -> int:
    settings = get_settings(db_path)
    as_of = local_today()
    with get_connection(db_path) as connection:
        selected_store_id = int(store_id or settings["store_id"])
        store = connection.execute(
            "SELECT * FROM stores WHERE id = ? AND is_active = 1", (selected_store_id,)
        ).fetchone()
        if not store:
            raise ValueError("店铺不存在或已停用。")
        latest_sale_date = connection.execute(
            """
            SELECT MAX(sales_daily.sale_date) AS value
            FROM sales_daily JOIN skus ON skus.id = sales_daily.sku_id
            WHERE skus.store_id = ? AND skus.lifecycle = 'active'
            """,
            (selected_store_id,),
        ).fetchone()["value"]
        if latest_sale_date:
            as_of = date.fromisoformat(str(latest_sale_date)[:10])
        if not force:
            same_day = connection.execute(
                "SELECT id FROM plans WHERE store_id = ? AND sales_through_date = ? ORDER BY id DESC LIMIT 1",
                (selected_store_id, as_of.isoformat()),
            ).fetchone()
            if same_day and generation_type != "initial_test":
                return int(same_day["id"])
        skus = [dict(row) for row in connection.execute("SELECT * FROM skus WHERE store_id = ? AND lifecycle = 'active'", (selected_store_id,))]
        sales_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT sales_daily.* FROM sales_daily JOIN skus ON skus.id = sales_daily.sku_id
                WHERE skus.store_id = ? AND sale_date BETWEEN ? AND ?
                """,
                (selected_store_id, (as_of - timedelta(days=13)).isoformat(), as_of.isoformat()),
            )
        ]
        inventory_by_sku = {
            int(row["sku_id"]): dict(row)
            for row in connection.execute(
                "SELECT inventory_current.* FROM inventory_current JOIN skus ON skus.id = inventory_current.sku_id WHERE skus.store_id = ?",
                (selected_store_id,),
            )
        }
        snapshot_at = connection.execute(
            """
            SELECT MAX(inventory_current.snapshot_at) AS value
            FROM inventory_current JOIN skus ON skus.id = inventory_current.sku_id
            WHERE skus.store_id = ?
            """,
            (selected_store_id,),
        ).fetchone()["value"] or ""
        items = build_replenishment_items(
            skus,
            sales_rows,
            inventory_by_sku,
            as_of=as_of,
            target_days=int(settings["target_days"]),
            safety_days=int(settings["safety_days"]),
            weight_7=float(settings["weight_7"]),
            weight_14=float(settings["weight_14"]),
            min_sales_7=int(settings["min_sales_7"]),
            min_sales_14=int(settings["min_sales_14"]),
            min_consecutive_sales_days=int(settings["min_consecutive_sales_days"]),
            max_coverage_days=float(settings["max_coverage_days"]),
        )
        day_run = connection.execute(
            "SELECT COUNT(*) AS count FROM plans WHERE store_id = ? AND sales_through_date = ?",
            (selected_store_id, as_of.isoformat()),
        ).fetchone()["count"] + 1
        plan_no = f"{store['store_code']}-{as_of.strftime('%Y%m%d')}-{day_run:02d}"
        if generation_type in {"browser_report", "scheduled_api", "manual_api"}:
            connection.execute(
                """
                UPDATE plans SET status = 'superseded'
                WHERE store_id = ? AND status IN ('merchandise_pending', 'merchandise_editing')
                """,
                (selected_store_id,),
            )
        cursor = connection.execute(
            """
            INSERT INTO plans(
                store_id, plan_no, status, generation_type, target_days, safety_days,
                min_sales_7, min_sales_14, min_consecutive_sales_days, max_coverage_days,
                sales_through_date, inventory_snapshot_at, created_at
            ) VALUES (?, ?, 'merchandise_pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                selected_store_id, plan_no, generation_type, settings["target_days"], settings["safety_days"],
                settings["min_sales_7"], settings["min_sales_14"], settings["min_consecutive_sales_days"],
                settings["max_coverage_days"],
                as_of.isoformat(), snapshot_at, utc_now(),
            ),
        )
        plan_id = cursor.lastrowid
        columns = [
            "sku_id", "style_code", "outer_sku_id", "style_name", "color_name", "size_name", "category", "supplier", "core_size",
            "sales_7", "sales_14", "consecutive_sales_days", "selection_reason", "avg_7", "avg_14",
            "size_share", "daily_demand", "sellable", "inbound",
            "inbound_date", "projected_14", "coverage_days", "stockout_day", "risk_level", "broken_core", "pack_size",
            "moq", "suggested_qty", "confirmed_qty",
            "price_snapshot", "unit_cost_snapshot", "gross_margin_snapshot", "margin_gate_status",
        ]
        placeholders = ",".join("?" for _ in columns)
        for item in items:
            sku_row = next((sku for sku in skus if int(sku["id"]) == int(item["sku_id"])), None)
            price = float((sku_row or {}).get("current_price") or 0)
            unit_cost = _latest_cost(connection, int(item["sku_id"]), as_of.isoformat())
            margin = (price - unit_cost) / price if price and unit_cost is not None else None
            gate = "ok"
            if generation_type == "main" and unit_cost is None:
                gate = "cost_missing"
                item["suggested_qty"] = 0
                item["confirmed_qty"] = 0
            elif generation_type == "main" and margin < 0.45:
                gate = "below_target"
                item["suggested_qty"] = 0
                item["confirmed_qty"] = 0
            item.update({"price_snapshot": price, "unit_cost_snapshot": unit_cost, "gross_margin_snapshot": margin, "margin_gate_status": gate})
            connection.execute(
                f"INSERT INTO plan_items(plan_id, {','.join(columns)}) VALUES (?, {placeholders})",
                [plan_id, *[item.get(column) for column in columns]],
            )
        _audit(connection, created_by, "plan_generated", "plan", plan_id, generation_type)
        _notify_roles(
            connection,
            ("merchandise", "admin"),
            "新补货任务待确认",
            f"{plan_no} 已生成，请商品部核对缺货风险和尺码数量。",
            f"/plans/{plan_id}",
            store_id=selected_store_id,
        )
    return int(plan_id)


def list_plans(
    db_path: str | Path, limit: int = 50, *, store_id: int | None = None
) -> list[dict]:
    with get_connection(db_path) as connection:
        store_filter = "WHERE plans.store_id = ?" if store_id is not None else ""
        params = (int(store_id), limit) if store_id is not None else (limit,)
        rows = connection.execute(
            f"""
            SELECT plans.*, stores.store_name,
                COUNT(DISTINCT plan_items.style_code || '|' || plan_items.outer_sku_id) AS style_count,
                SUM(CASE WHEN plan_items.risk_level IN ('critical','warning') THEN 1 ELSE 0 END) AS risk_sku_count,
                SUM(plan_items.confirmed_qty) AS confirmed_total
            FROM plans JOIN stores ON stores.id = plans.store_id
            LEFT JOIN plan_items ON plan_items.plan_id = plans.id
            {store_filter}
            GROUP BY plans.id ORDER BY plans.id DESC LIMIT ?
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def get_plan(db_path: str | Path, plan_id: int) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute(
            """
            SELECT plans.*, stores.platform_name, stores.store_name, stores.brand_name,
                merchandise.display_name AS merchandise_name, followup.display_name AS followup_name
            FROM plans JOIN stores ON stores.id = plans.store_id
            LEFT JOIN users AS merchandise ON merchandise.id = plans.merchandise_user_id
            LEFT JOIN users AS followup ON followup.id = plans.followup_user_id
            WHERE plans.id = ?
            """,
            (plan_id,),
        ).fetchone()
    return dict(row) if row else None


def get_plan_items(db_path: str | Path, plan_id: int) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM plan_items WHERE plan_id = ?
            ORDER BY CASE risk_level WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 WHEN 'watch' THEN 2 ELSE 3 END,
                style_code, outer_sku_id, color_name, size_name
            """,
            (plan_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def grouped_plan_items(items: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in items:
        goods_code = item.get("outer_sku_id") or item["color_name"]
        grouped[(item["style_code"], goods_code)].append(item)
    result = []
    for (style_code, goods_code), rows in grouped.items():
        risk_rank = {"critical": 0, "warning": 1, "watch": 2, "healthy": 3, "no_sales": 4}
        daily_demand = sum(float(row["daily_demand"] or 0) for row in rows)
        available_14 = sum(int(row["sellable"] or 0) + int(row["inbound"] or 0) for row in rows)
        coverage_days = available_14 / daily_demand if daily_demand > 0 else None
        if coverage_days is not None and coverage_days <= 7:
            group_risk = "critical"
        elif coverage_days is not None and coverage_days <= 14:
            group_risk = "warning"
        else:
            group_risk = min((row["risk_level"] for row in rows), key=lambda risk: risk_rank.get(risk, 9))
        result.append(
            {
                "style_code": style_code,
                "outer_sku_id": goods_code,
                "style_name": rows[0]["style_name"],
                "color_name": rows[0]["color_name"],
                "category": rows[0]["category"],
                "supplier": rows[0]["supplier"],
                "risk_level": group_risk,
                "broken_core_count": sum(row["broken_core"] for row in rows),
                "sales_7": sum(row["sales_7"] for row in rows),
                "sales_14": sum(row["sales_14"] for row in rows),
                "consecutive_sales_days": max(int(row.get("consecutive_sales_days") or 0) for row in rows),
                "selection_reasons": sorted(
                    {
                        reason
                        for row in rows
                        for reason in str(row.get("selection_reason") or "").split(",")
                        if reason
                    }
                ),
                "sellable": sum(row["sellable"] for row in rows),
                "suggested_qty": sum(row["suggested_qty"] for row in rows),
                "confirmed_qty": sum(row["confirmed_qty"] for row in rows),
                "followup_qty": sum(
                    row.get("followup_qty") if row.get("followup_qty") is not None else row["confirmed_qty"]
                    for row in rows
                ),
                "min_stockout_day": min((row["stockout_day"] for row in rows if row["stockout_day"] is not None), default=None),
                "coverage_days": coverage_days,
                "items": rows,
            }
        )
    return sorted(
        result,
        key=lambda row: (
            {"critical": 0, "warning": 1, "watch": 2, "healthy": 3, "no_sales": 4}.get(row["risk_level"], 9),
            row["style_code"], row["outer_sku_id"],
        ),
    )


def grouped_plan_styles(items: list[dict]) -> list[dict]:
    styles: dict[str, list[dict]] = defaultdict(list)
    for goods in grouped_plan_items(items):
        styles[goods["style_code"]].append(goods)
    risk_rank = {"critical": 0, "warning": 1, "watch": 2, "healthy": 3, "no_sales": 4}
    result = []
    for style_code, goods_rows in styles.items():
        result.append(
            {
                "style_code": style_code,
                "style_name": goods_rows[0]["style_name"],
                "risk_level": min(
                    (row["risk_level"] for row in goods_rows),
                    key=lambda risk: risk_rank.get(risk, 9),
                ),
                "goods_count": len(goods_rows),
                "confirmed_qty": sum(row["confirmed_qty"] for row in goods_rows),
                "followup_qty": sum(row["followup_qty"] for row in goods_rows),
                "goods": goods_rows,
            }
        )
    return sorted(result, key=lambda row: (risk_rank.get(row["risk_level"], 9), row["style_code"]))


def save_merchandise_adjustments(db_path: str | Path, plan_id: int, quantities: dict[int, int], reasons: dict[int, str], user_id: int) -> None:
    with get_connection(db_path) as connection:
        plan = connection.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not plan or plan["status"] not in {"merchandise_pending", "merchandise_editing"}:
            raise ValueError("当前计划已提交，不能继续修改商品部数量。")
        rows = connection.execute("SELECT id, suggested_qty, pack_size FROM plan_items WHERE plan_id = ?", (plan_id,)).fetchall()
        for row in rows:
            quantity = max(0, int(quantities.get(int(row["id"]), row["suggested_qty"])))
            reason = str(reasons.get(int(row["id"]), "")).strip()
            if quantity != row["suggested_qty"] and not reason:
                raise ValueError("修改系统建议数量时必须填写调整原因。")
            connection.execute(
                "UPDATE plan_items SET confirmed_qty = ?, adjustment_reason = ? WHERE id = ?",
                (quantity, reason, row["id"]),
            )
        connection.execute(
            "UPDATE plans SET status = 'merchandise_editing', merchandise_user_id = ?, merchandise_confirmed_at = ? WHERE id = ?",
            (user_id, utc_now(), plan_id),
        )
        _audit(connection, user_id, "merchandise_saved", "plan", plan_id, "商品部保存修正")


def submit_to_followup(db_path: str | Path, plan_id: int, user_id: int) -> None:
    with get_connection(db_path) as connection:
        plan = connection.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not plan or plan["status"] not in {"merchandise_pending", "merchandise_editing"}:
            raise ValueError("当前计划不能重复提交。")
        connection.execute(
            """
            UPDATE plans SET status = 'followup_pending', merchandise_user_id = ?,
                merchandise_confirmed_at = ?, submitted_at = ? WHERE id = ?
            """,
            (user_id, utc_now(), utc_now(), plan_id),
        )
        connection.execute("UPDATE plan_items SET followup_qty = confirmed_qty WHERE plan_id = ?", (plan_id,))
        _audit(connection, user_id, "submitted_to_followup", "plan", plan_id, "商品部提交跟单部")
        _notify_roles(
            connection,
            ("followup", "admin"),
            "补货明细已流转",
            f"{plan['plan_no']} 已由商品部确认，请跟单部回复可供数量和交期。",
            f"/plans/{plan_id}/followup",
            store_id=plan["store_id"],
        )


def _business_days_late(expected: str, actual: str) -> int:
    if not expected or not actual:
        return 0
    try:
        current = date.fromisoformat(expected) + timedelta(days=1)
        actual_date = date.fromisoformat(actual)
    except ValueError:
        return 0
    days = 0
    while current <= actual_date:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)
    return days


def save_followup_response(db_path: str | Path, plan_id: int, payloads: dict[int, dict], user_id: int, *, complete: bool = False) -> None:
    with get_connection(db_path) as connection:
        plan = connection.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        if not plan or plan["status"] not in {"followup_pending", "followup_processing"}:
            raise ValueError("当前计划不在跟单处理阶段。")
        rows = connection.execute("SELECT id, confirmed_qty FROM plan_items WHERE plan_id = ?", (plan_id,)).fetchall()
        for row in rows:
            item = payloads.get(int(row["id"]), {})
            quantity = max(0, int(item.get("followup_qty", row["confirmed_qty"])))
            status = str(item.get("followup_status") or "pending")
            if status not in {"pending", "confirmed", "limited", "ordered", "arrived"}:
                status = "pending"
            connection.execute(
                """
                UPDATE plan_items SET followup_qty = ?, expected_order_date = ?, expected_arrival_date = ?,
                    followup_status = ?, followup_note = ?, actual_arrival_date = ?,
                    actual_arrived_qty = ?, arrival_variance_note = ? WHERE id = ?
                """,
                (
                    quantity,
                    str(item.get("expected_order_date") or "").strip(),
                    str(item.get("expected_arrival_date") or "").strip(),
                    status,
                    str(item.get("followup_note") or "").strip(),
                    str(item.get("actual_arrival_date") or "").strip(),
                    max(0, int(item.get("actual_arrived_qty") or 0)),
                    str(item.get("arrival_variance_note") or "").strip(),
                    row["id"],
                ),
            )
        receipt_rows = [dict(row) for row in connection.execute("SELECT * FROM plan_items WHERE plan_id = ?", (plan_id,)).fetchall()]
        grouped = defaultdict(list)
        for row in receipt_rows:
            grouped[(row["style_code"], row["outer_sku_id"] or row["color_name"])].append(row)
        major_count = 0
        for goods_rows in grouped.values():
            receipt_active = any(row["actual_arrival_date"] or row["actual_arrived_qty"] > 0 or row["followup_status"] == "arrived" for row in goods_rows)
            if not receipt_active:
                continue
            planned_total = sum(max(0, int(row["confirmed_qty"] or 0)) for row in goods_rows)
            actual_total = sum(max(0, int(row["actual_arrived_qty"] or 0)) for row in goods_rows)
            group_major = planned_total > 0 and actual_total / planned_total < 0.9
            for row in goods_rows:
                planned = max(0, int(row["confirmed_qty"] or 0))
                actual = max(0, int(row["actual_arrived_qty"] or 0))
                shortage = max(0, planned - actual)
                size_major = planned > 0 and (actual == 0 or (actual / planned < 0.7 and shortage >= 5))
                late_major = _business_days_late(row["expected_arrival_date"], row["actual_arrival_date"]) > 2
                if group_major or size_major or late_major:
                    level = "major"
                    major_count += 1
                elif planned != actual or (row["expected_arrival_date"] and row["actual_arrival_date"] != row["expected_arrival_date"]):
                    level = "general"
                else:
                    level = "none"
                connection.execute("UPDATE plan_items SET arrival_variance_level = ? WHERE id = ?", (level, row["id"]))
        next_status = "completed" if complete else "followup_processing"
        connection.execute(
            """
            UPDATE plans SET status = ?, followup_user_id = ?, followup_updated_at = ?, completed_at = ? WHERE id = ?
            """,
            (next_status, user_id, utc_now(), utc_now() if complete else None, plan_id),
        )
        _audit(connection, user_id, "followup_completed" if complete else "followup_saved", "plan", plan_id, "跟单部回填")
        if complete:
            title = "补货到货存在重大差异" if major_count else "补货任务已完成"
            body = f"{plan['plan_no']} 有 {major_count} 个尺码命中重大差异，请商品部重点查看。" if major_count else f"{plan['plan_no']} 已完成跟单确认，可查看最终数量和交期。"
            _notify_roles(
                connection,
                ("merchandise", "manager", "admin"),
                title,
                body,
                f"/plans/{plan_id}",
                store_id=plan["store_id"],
            )


def dashboard_data(db_path: str | Path, *, store_id: int | None = None) -> dict:
    plans = list_plans(db_path, limit=12, store_id=store_id)
    current = plans[0] if plans else None
    groups = []
    items = []
    if current:
        items = get_plan_items(db_path, int(current["id"]))
        groups = grouped_plan_items(items)
    risk_styles = [group for group in groups if group["risk_level"] in {"critical", "warning"}]
    return {
        "current": current,
        "groups": groups,
        "risk_styles": risk_styles,
        "critical_styles": sum(group["risk_level"] == "critical" for group in groups),
        "warning_styles": sum(group["risk_level"] == "warning" for group in groups),
        "broken_core": sum(group["broken_core_count"] for group in groups),
        "goods_count": len(groups),
        "suggested_total": sum(group["confirmed_qty"] for group in groups),
        "healthy_styles": sum(group["risk_level"] in {"healthy", "no_sales"} for group in groups),
        "plans": plans,
    }


def unread_notifications(
    db_path: str | Path, user_id: int, limit: int = 8, *, store_id: int | None = None
) -> list[dict]:
    with get_connection(db_path) as connection:
        if store_id is None:
            rows = connection.execute(
                "SELECT * FROM notifications WHERE user_id = ? AND read_at IS NULL ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM notifications
                WHERE user_id = ? AND read_at IS NULL AND (
                    store_id = ? OR (
                        store_id IS NULL AND ? = (
                            SELECT id FROM stores WHERE store_code = 'VIP-MTN'
                        )
                    )
                )
                ORDER BY id DESC LIMIT ?
                """,
                (user_id, int(store_id), int(store_id), limit),
            ).fetchall()
    return [dict(row) for row in rows]


def mark_notifications_read(
    db_path: str | Path, user_id: int, *, store_id: int | None = None
) -> None:
    with get_connection(db_path) as connection:
        if store_id is None:
            connection.execute(
                "UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
                (utc_now(), user_id),
            )
        else:
            connection.execute(
                """
                UPDATE notifications SET read_at = ?
                WHERE user_id = ? AND read_at IS NULL AND (
                    store_id = ? OR (
                        store_id IS NULL AND ? = (
                            SELECT id FROM stores WHERE store_code = 'VIP-MTN'
                        )
                    )
                )
                """,
                (utc_now(), user_id, int(store_id), int(store_id)),
            )


def _notify_roles(
    connection: sqlite3.Connection,
    roles: tuple[str, ...],
    title: str,
    body: str,
    link: str,
    *,
    store_id: int | None = None,
) -> None:
    placeholders = ",".join("?" for _ in roles)
    users = connection.execute(f"SELECT id FROM users WHERE role IN ({placeholders}) AND is_active = 1", roles).fetchall()
    for user in users:
        connection.execute(
            "INSERT INTO notifications(user_id, store_id, title, body, link, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], store_id, title, body, link, utc_now()),
        )


def _audit(connection: sqlite3.Connection, user_id: int | None, action: str, object_type: str, object_id: int | None, detail: str) -> None:
    connection.execute(
        "INSERT INTO audit_events(user_id, action, object_type, object_id, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, action, object_type, object_id, detail, utc_now()),
    )
