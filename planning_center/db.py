from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path

DEMO_PASSWORD = "demo123"
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
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return secrets.compare_digest(expected, digest.hex())


def init_db(db_path: str | Path, *, seed_demo: bool = True, bootstrap_admin: dict | None = None) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'planner',
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_products (
                id INTEGER PRIMARY KEY,
                style_code TEXT NOT NULL DEFAULT '',
                style_color TEXT NOT NULL DEFAULT '',
                color_name TEXT NOT NULL DEFAULT '',
                product_name TEXT NOT NULL DEFAULT '',
                brand_name TEXT NOT NULL DEFAULT '',
                season_year TEXT NOT NULL DEFAULT '',
                supplier TEXT NOT NULL DEFAULT '',
                supplier_code TEXT NOT NULL DEFAULT '',
                supplier_style_code TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                actual_cost REAL,
                tax_included_price REAL,
                status TEXT NOT NULL DEFAULT '',
                lifecycle_status TEXT NOT NULL DEFAULT '',
                source_version_no INTEGER NOT NULL DEFAULT 1,
                source_updated_at TEXT NOT NULL DEFAULT '',
                creator_name TEXT NOT NULL DEFAULT '',
                synced_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS category_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_year TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL,
                multiplier REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                UNIQUE(season_year, category)
            );
            CREATE TABLE IF NOT EXISTS supplier_coefficients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_year TEXT NOT NULL DEFAULT '',
                supplier TEXT NOT NULL,
                coefficient REAL NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                UNIQUE(season_year, supplier)
            );
            CREATE TABLE IF NOT EXISTS price_bands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                lower_bound REAL,
                upper_bound REAL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS pricing_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                publication_id TEXT NOT NULL UNIQUE,
                source_product_id INTEGER NOT NULL,
                source_version_no INTEGER NOT NULL,
                season_year TEXT NOT NULL,
                style_code TEXT NOT NULL DEFAULT '',
                product_name TEXT NOT NULL DEFAULT '',
                supplier TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL,
                cost REAL NOT NULL,
                fixed_multiplier REAL NOT NULL,
                supplier_coefficient REAL NOT NULL,
                raw_price REAL NOT NULL,
                launch_price REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'suggested',
                operator_name TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                published_at TEXT,
                error_message TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_source_products_season ON source_products(season_year, status);
            CREATE INDEX IF NOT EXISTS idx_pricing_records_season ON pricing_records(season_year, category, status);
            """
        )
        if seed_demo and connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            now = utc_now()
            connection.executemany(
                "INSERT INTO users (username, display_name, role, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                [
                    ("planner", "商品部企划员", "planner", hash_password(DEMO_PASSWORD), now),
                    ("planning_admin", "企划管理员", "admin", hash_password(DEMO_PASSWORD), now),
                ],
            )
        if bootstrap_admin and connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            username = str(bootstrap_admin.get("username") or "").strip()
            password = str(bootstrap_admin.get("password") or "").strip()
            display_name = str(bootstrap_admin.get("display_name") or "企划管理员").strip()
            if not username or not password:
                raise ValueError("正式初始化必须同时提供企划管理员账号和密码。")
            connection.execute(
                "INSERT INTO users (username, display_name, role, password_hash, created_at) VALUES (?, ?, 'admin', ?, ?)",
                (username, display_name, hash_password(password), utc_now()),
            )
        if connection.execute("SELECT COUNT(*) FROM price_bands").fetchone()[0] == 0:
            connection.executemany(
                "INSERT INTO price_bands (label, lower_bound, upper_bound, sort_order) VALUES (?, ?, ?, ?)",
                [("300及以下", None, 300, 10), ("301-500", 300, 500, 20), ("501-800", 500, 800, 30), ("801-1200", 800, 1200, 40), ("1201以上", 1200, None, 50)],
            )


def authenticate_user(db_path: str | Path, username: str, password: str) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username.strip(),)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    return dict(row)


def list_source_products(db_path: str | Path, *, season_year: str = "", status: str = "") -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM source_products WHERE (? = '' OR season_year = ?) AND (? = '' OR status = ?) ORDER BY season_year DESC, source_updated_at DESC, id DESC",
            (season_year, season_year, status, status),
        ).fetchall()
    return [dict(row) for row in rows]


def get_source_product(db_path: str | Path, product_id: int) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM source_products WHERE id = ?", (product_id,)).fetchone()
    return dict(row) if row else None


def upsert_source_products(db_path: str | Path, items: list[dict]) -> int:
    now = utc_now()
    with get_connection(db_path) as connection:
        for item in items:
            connection.execute(
                """
                INSERT INTO source_products (
                    id, style_code, style_color, color_name, product_name, brand_name, season_year,
                    supplier, supplier_code, supplier_style_code, category, actual_cost, tax_included_price,
                    status, lifecycle_status, source_version_no, source_updated_at, creator_name, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    style_code=excluded.style_code, style_color=excluded.style_color, color_name=excluded.color_name,
                    product_name=excluded.product_name, brand_name=excluded.brand_name, season_year=excluded.season_year,
                    supplier=excluded.supplier, supplier_code=excluded.supplier_code, supplier_style_code=excluded.supplier_style_code,
                    category=excluded.category, actual_cost=excluded.actual_cost, tax_included_price=excluded.tax_included_price,
                    status=excluded.status, lifecycle_status=excluded.lifecycle_status, source_version_no=excluded.source_version_no,
                    source_updated_at=excluded.source_updated_at, creator_name=excluded.creator_name, synced_at=excluded.synced_at
                """,
                (
                    int(item["id"]), item.get("style_code", ""), item.get("style_color", ""), item.get("color_name", ""),
                    item.get("product_name", ""), item.get("brand_name", ""), item.get("season_year", ""), item.get("supplier", ""),
                    item.get("supplier_code", ""), item.get("supplier_style_code", ""), item.get("category", ""), item.get("actual_cost"),
                    item.get("tax_included_price"), item.get("status", ""), item.get("lifecycle_status", ""), int(item.get("source_version_no") or 1),
                    item.get("updated_at", ""), item.get("creator_name", ""), now,
                ),
            )
    return len(items)


def list_category_rules(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM category_rules ORDER BY season_year, category").fetchall()]


def list_supplier_coefficients(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM supplier_coefficients ORDER BY season_year, supplier").fetchall()]


def save_category_rule(db_path: str | Path, season_year: str, category: str, multiplier: float, note: str = "") -> None:
    if not category.strip():
        raise ValueError("品类不能为空。")
    if multiplier <= 0:
        raise ValueError("品类固定倍率必须大于 0。")
    with get_connection(db_path) as connection:
        connection.execute(
            "INSERT INTO category_rules (season_year, category, multiplier, note, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(season_year, category) DO UPDATE SET multiplier=excluded.multiplier, note=excluded.note, updated_at=excluded.updated_at",
            (season_year.strip(), category.strip(), float(multiplier), note.strip(), utc_now()),
        )


def save_supplier_coefficient(db_path: str | Path, season_year: str, supplier: str, coefficient: float, note: str = "") -> None:
    if not supplier.strip():
        raise ValueError("供应商不能为空。")
    if coefficient <= 0:
        raise ValueError("供应商浮动系数必须大于 0。")
    with get_connection(db_path) as connection:
        connection.execute(
            "INSERT INTO supplier_coefficients (season_year, supplier, coefficient, note, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(season_year, supplier) DO UPDATE SET coefficient=excluded.coefficient, note=excluded.note, updated_at=excluded.updated_at",
            (season_year.strip(), supplier.strip(), float(coefficient), note.strip(), utc_now()),
        )


def resolve_rules(db_path: str | Path, season_year: str, category: str, supplier: str) -> tuple[float | None, float]:
    with get_connection(db_path) as connection:
        category_rows = connection.execute(
            "SELECT season_year, multiplier FROM category_rules WHERE category = ? AND season_year IN (?, '') AND enabled = 1 ORDER BY CASE WHEN season_year = ? THEN 0 ELSE 1 END LIMIT 1",
            (category.strip(), season_year.strip(), season_year.strip()),
        ).fetchone()
        supplier_row = connection.execute(
            "SELECT season_year, coefficient FROM supplier_coefficients WHERE supplier = ? AND season_year IN (?, '') ORDER BY CASE WHEN season_year = ? THEN 0 ELSE 1 END LIMIT 1",
            (supplier.strip(), season_year.strip(), season_year.strip()),
        ).fetchone()
    return (float(category_rows["multiplier"]) if category_rows else None, float(supplier_row["coefficient"]) if supplier_row else 1.0)


def round_price_to_9(raw_price: float) -> int:
    try:
        value = Decimal(str(raw_price))
    except InvalidOperation:
        raise ValueError("计算价格不是有效数字。")
    if value <= 9:
        return 9
    return int(((value - Decimal("9")) / Decimal("10")).to_integral_value(rounding=ROUND_FLOOR) * Decimal("10") + Decimal("9"))


def create_pricing_record(db_path: str | Path, product: dict, operator_name: str) -> dict:
    cost = product.get("actual_cost")
    if cost is None or float(cost) <= 0:
        raise ValueError("藏宝阁尚未提供有效的含税采购成本。")
    category = str(product.get("category") or "").strip()
    if not category:
        raise ValueError("请先在定价工作台选择品类，再计算建议价。")
    fixed, coefficient = resolve_rules(db_path, product.get("season_year", ""), category, product.get("supplier", ""))
    if fixed is None:
        raise ValueError(f"品类“{category}”尚未配置固定倍率，请先到定价规则中配置。")
    raw = float(cost) * fixed * coefficient
    launch = round_price_to_9(raw)
    publication_id = f"PC-{product['id']}-V{int(product.get('source_version_no') or 1)}-{secrets.token_hex(4).upper()}"
    now = utc_now()
    with get_connection(db_path) as connection:
        connection.execute(
            "INSERT INTO pricing_records (publication_id, source_product_id, source_version_no, season_year, style_code, product_name, supplier, category, cost, fixed_multiplier, supplier_coefficient, raw_price, launch_price, status, operator_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'suggested', ?, ?)",
            (publication_id, product["id"], int(product.get("source_version_no") or 1), product.get("season_year", ""), product.get("style_code", ""), product.get("product_name", ""), product.get("supplier", ""), category, float(cost), fixed, coefficient, raw, launch, operator_name, now),
        )
        row = connection.execute("SELECT * FROM pricing_records WHERE publication_id = ?", (publication_id,)).fetchone()
    return dict(row)


def list_pricing_records(db_path: str | Path, *, season_year: str = "", status: str = "") -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM pricing_records WHERE (? = '' OR season_year = ?) AND (? = '' OR status = ?) ORDER BY created_at DESC, id DESC",
            (season_year, season_year, status, status),
        ).fetchall()
    return [dict(row) for row in rows]


def get_pricing_record(db_path: str | Path, record_id: int) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
    return dict(row) if row else None


def confirm_pricing_record(db_path: str | Path, record_id: int, operator_name: str) -> dict:
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            raise LookupError("定价记录不存在。")
        if row["status"] not in {"suggested", "conflict"}:
            return dict(row)
        connection.execute("UPDATE pricing_records SET status = 'confirmed', operator_name = ?, confirmed_at = ?, error_message = '' WHERE id = ?", (operator_name, utc_now(), record_id))
        updated = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
    return dict(updated)


def mark_record_published(db_path: str | Path, record_id: int, result: dict) -> dict:
    with get_connection(db_path) as connection:
        if result.get("status") in {"published", "already_published"}:
            connection.execute("UPDATE pricing_records SET status = 'published', published_at = ?, error_message = '' WHERE id = ?", (result.get("published_at") or utc_now(), record_id))
        else:
            connection.execute("UPDATE pricing_records SET status = 'conflict', error_message = ? WHERE id = ?", (result.get("message") or "回传失败", record_id))
        row = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
    return dict(row)


def pricing_stats(db_path: str | Path, season_year: str = "", category: str = "") -> list[dict]:
    with get_connection(db_path) as connection:
        records = [dict(row) for row in connection.execute("SELECT * FROM pricing_records WHERE status IN ('confirmed', 'published') AND (? = '' OR season_year = ?) AND (? = '' OR category = ?)", (season_year, season_year, category, category)).fetchall()]
        bands = [dict(row) for row in connection.execute("SELECT * FROM price_bands WHERE enabled = 1 ORDER BY sort_order, id").fetchall()]
    total = len(records)
    output = []
    for band in bands:
        lower, upper = band["lower_bound"], band["upper_bound"]
        count = sum(1 for item in records if (lower is None or item["launch_price"] > lower) and (upper is None or item["launch_price"] <= upper))
        output.append({**band, "count": count, "share": round((count * 100 / total), 1) if total else 0})
    return output
