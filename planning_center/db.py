from __future__ import annotations

import hashlib
import json
import math
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
                image_url TEXT NOT NULL DEFAULT '',
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
            CREATE TABLE IF NOT EXISTS category_cost_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_year TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '其他品类',
                lower_cost REAL,
                upper_cost REAL,
                multiplier REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
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
                calculated_price REAL,
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
            CREATE INDEX IF NOT EXISTS idx_category_cost_rules_lookup ON category_cost_rules(season_year, category, enabled, lower_cost, upper_cost);
            """
        )
        source_columns = {row["name"] for row in connection.execute("PRAGMA table_info(source_products)").fetchall()}
        if "image_url" not in source_columns:
            connection.execute("ALTER TABLE source_products ADD COLUMN image_url TEXT NOT NULL DEFAULT ''")
        pricing_columns = {row["name"] for row in connection.execute("PRAGMA table_info(pricing_records)").fetchall()}
        if "calculated_price" not in pricing_columns:
            connection.execute("ALTER TABLE pricing_records ADD COLUMN calculated_price REAL")
            connection.execute("UPDATE pricing_records SET calculated_price = launch_price WHERE calculated_price IS NULL")
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
                    image_url, status, lifecycle_status, source_version_no, source_updated_at, creator_name, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    style_code=excluded.style_code, style_color=excluded.style_color, color_name=excluded.color_name,
                    product_name=excluded.product_name, brand_name=excluded.brand_name, season_year=excluded.season_year,
                    supplier=excluded.supplier, supplier_code=excluded.supplier_code, supplier_style_code=excluded.supplier_style_code,
                    category=excluded.category, actual_cost=excluded.actual_cost, tax_included_price=excluded.tax_included_price, image_url=excluded.image_url,
                    status=excluded.status, lifecycle_status=excluded.lifecycle_status, source_version_no=excluded.source_version_no,
                    source_updated_at=excluded.source_updated_at, creator_name=excluded.creator_name, synced_at=excluded.synced_at
                """,
                (
                    int(item["id"]), item.get("style_code", ""), item.get("style_color", ""), item.get("color_name", ""),
                    item.get("product_name", ""), item.get("brand_name", ""), item.get("season_year", ""), item.get("supplier", ""),
                    item.get("supplier_code", ""), item.get("supplier_style_code", ""), item.get("category", ""), item.get("actual_cost"),
                    item.get("tax_included_price"), item.get("image_url", ""), item.get("status", ""), item.get("lifecycle_status", ""), int(item.get("source_version_no") or 1),
                    item.get("updated_at", ""), item.get("creator_name", ""), now,
                ),
            )
    return len(items)


def list_category_rules(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM category_rules WHERE category = '连衣裙' ORDER BY season_year").fetchall()]


def list_category_cost_rules(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM category_cost_rules WHERE category = '其他品类' ORDER BY season_year, lower_cost IS NOT NULL, lower_cost, upper_cost"
            ).fetchall()
        ]


def list_supplier_coefficients(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM supplier_coefficients ORDER BY season_year, supplier").fetchall()]


def save_category_rule(
    db_path: str | Path,
    season_year: str,
    category: str,
    multiplier: float,
    note: str = "",
    rule_id: int | None = None,
) -> None:
    if category.strip() != "连衣裙":
        raise ValueError("固定倍率只适用于“连衣裙”，其他品类请按成本区间配置。")
    if not math.isfinite(float(multiplier)) or multiplier <= 0:
        raise ValueError("连衣裙固定倍率必须大于 0。")
    with get_connection(db_path) as connection:
        clean_season = season_year.strip()
        now = utc_now()
        if rule_id is not None:
            target = connection.execute(
                "SELECT id FROM category_rules WHERE id = ? AND category = '连衣裙'",
                (int(rule_id),),
            ).fetchone()
            if not target:
                raise LookupError("连衣裙固定倍率规则不存在。")
            duplicate = connection.execute(
                "SELECT id FROM category_rules WHERE season_year = ? AND category = '连衣裙' AND id != ?",
                (clean_season, int(rule_id)),
            ).fetchone()
            if duplicate:
                raise ValueError("该季节已存在连衣裙固定倍率，请编辑已有规则。")
            connection.execute(
                "UPDATE category_rules SET season_year = ?, category = ?, multiplier = ?, note = ?, updated_at = ?, enabled = 1 WHERE id = ? AND category = '连衣裙'",
                (clean_season, category.strip(), float(multiplier), note.strip(), now, int(rule_id)),
            )
            return
        existing = connection.execute(
            "SELECT id FROM category_rules WHERE season_year = ? AND category = '连衣裙'",
            (clean_season,),
        ).fetchone()
        if existing:
            raise ValueError("该季节已存在连衣裙固定倍率，请点击编辑已有规则。")
        connection.execute(
            "INSERT INTO category_rules (season_year, category, multiplier, note, updated_at) VALUES (?, ?, ?, ?, ?)",
            (clean_season, category.strip(), float(multiplier), note.strip(), now),
        )


def delete_category_rule(db_path: str | Path, rule_id: int) -> None:
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            "DELETE FROM category_rules WHERE id = ? AND category = '连衣裙'",
            (int(rule_id),),
        )
        if cursor.rowcount != 1:
            raise LookupError("连衣裙固定倍率规则不存在。")


def _cost_bound(value, label: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}必须是有效数字。")
    if not math.isfinite(number):
        raise ValueError(f"{label}必须是有效数字。")
    if number < 0:
        raise ValueError(f"{label}不能小于 0。")
    return number


def _cost_ranges_overlap(left: dict, right: dict) -> bool:
    left_lower = float("-inf") if left.get("lower_cost") is None else float(left["lower_cost"])
    left_upper = float("inf") if left.get("upper_cost") is None else float(left["upper_cost"])
    right_lower = float("-inf") if right.get("lower_cost") is None else float(right["lower_cost"])
    right_upper = float("inf") if right.get("upper_cost") is None else float(right["upper_cost"])
    return max(left_lower, right_lower) < min(left_upper, right_upper)


def save_category_cost_rule(
    db_path: str | Path,
    season_year: str,
    lower_cost,
    upper_cost,
    multiplier: float,
    note: str = "",
    rule_id: int | None = None,
) -> None:
    lower = _cost_bound(lower_cost, "成本下限")
    upper = _cost_bound(upper_cost, "成本上限")
    if lower is None and upper is None:
        raise ValueError("成本区间至少需要填写一个边界。")
    if lower is not None and upper is not None and lower >= upper:
        raise ValueError("成本区间必须满足下限小于上限；上限不包含。")
    if not math.isfinite(float(multiplier)) or multiplier <= 0:
        raise ValueError("其他品类成本区间倍率必须大于 0。")
    clean_season = season_year.strip()
    candidate = {"lower_cost": lower, "upper_cost": upper}
    with get_connection(db_path) as connection:
        existing_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM category_cost_rules WHERE season_year = ? AND category = '其他品类' AND enabled = 1 AND (? IS NULL OR id != ?)",
                (clean_season, rule_id, rule_id),
            ).fetchall()
        ]
        for row in existing_rows:
            same_range = row.get("lower_cost") == lower and row.get("upper_cost") == upper
            if not same_range and _cost_ranges_overlap(candidate, row):
                raise ValueError("其他品类的成本区间与已有规则重叠，请调整边界。")
        now = utc_now()
        matching = next(
            (
                row
                for row in existing_rows
                if row.get("lower_cost") == lower and row.get("upper_cost") == upper
            ),
            None,
        )
        if rule_id is not None:
            if matching:
                raise ValueError("该成本区间已存在，请编辑已有规则。")
            target = connection.execute(
                "SELECT id FROM category_cost_rules WHERE id = ? AND category = '其他品类'",
                (int(rule_id),),
            ).fetchone()
            if not target:
                raise LookupError("成本区间规则不存在。")
            connection.execute(
                "UPDATE category_cost_rules SET season_year = ?, lower_cost = ?, upper_cost = ?, multiplier = ?, note = ?, updated_at = ?, enabled = 1 WHERE id = ? AND category = '其他品类'",
                (clean_season, lower, upper, float(multiplier), note.strip(), now, int(rule_id)),
            )
            return
        if matching:
            raise ValueError("该成本区间已存在，请点击编辑已有规则。")
        connection.execute(
            "INSERT INTO category_cost_rules (season_year, category, lower_cost, upper_cost, multiplier, note, updated_at) VALUES (?, '其他品类', ?, ?, ?, ?, ?)",
            (clean_season, lower, upper, float(multiplier), note.strip(), now),
        )


def delete_category_cost_rule(db_path: str | Path, rule_id: int) -> None:
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            "DELETE FROM category_cost_rules WHERE id = ? AND category = '其他品类'",
            (int(rule_id),),
        )
        if cursor.rowcount != 1:
            raise LookupError("成本区间规则不存在。")


def save_supplier_coefficient(
    db_path: str | Path,
    season_year: str,
    supplier: str,
    coefficient: float,
    note: str = "",
    rule_id: int | None = None,
) -> None:
    if not supplier.strip():
        raise ValueError("供应商不能为空。")
    if not math.isfinite(float(coefficient)) or coefficient <= 0:
        raise ValueError("供应商浮动系数必须大于 0。")
    with get_connection(db_path) as connection:
        clean_season = season_year.strip()
        clean_supplier = supplier.strip()
        now = utc_now()
        if rule_id is not None:
            target = connection.execute(
                "SELECT id FROM supplier_coefficients WHERE id = ?",
                (int(rule_id),),
            ).fetchone()
            if not target:
                raise LookupError("供应商浮动系数规则不存在。")
            duplicate = connection.execute(
                "SELECT id FROM supplier_coefficients WHERE season_year = ? AND supplier = ? AND id != ?",
                (clean_season, clean_supplier, int(rule_id)),
            ).fetchone()
            if duplicate:
                raise ValueError("该季节已存在此供应商的浮动系数，请编辑已有规则。")
            connection.execute(
                "UPDATE supplier_coefficients SET season_year = ?, supplier = ?, coefficient = ?, note = ?, updated_at = ? WHERE id = ?",
                (clean_season, clean_supplier, float(coefficient), note.strip(), now, int(rule_id)),
            )
            return
        existing = connection.execute(
            "SELECT id FROM supplier_coefficients WHERE season_year = ? AND supplier = ?",
            (clean_season, clean_supplier),
        ).fetchone()
        if existing:
            raise ValueError("该季节已存在此供应商的浮动系数，请点击编辑已有规则。")
        connection.execute(
            "INSERT INTO supplier_coefficients (season_year, supplier, coefficient, note, updated_at) VALUES (?, ?, ?, ?, ?)",
            (clean_season, clean_supplier, float(coefficient), note.strip(), now),
        )


def delete_supplier_coefficient(db_path: str | Path, rule_id: int) -> None:
    with get_connection(db_path) as connection:
        cursor = connection.execute("DELETE FROM supplier_coefficients WHERE id = ?", (int(rule_id),))
        if cursor.rowcount != 1:
            raise LookupError("供应商浮动系数规则不存在。")


def resolve_rules(db_path: str | Path, season_year: str, category: str, supplier: str, cost: float | None = None) -> tuple[float | None, float]:
    clean_season = season_year.strip()
    category_type = "连衣裙" if category.strip() == "连衣裙" else "其他品类"
    with get_connection(db_path) as connection:
        category_row = None
        if category_type == "连衣裙":
            category_row = connection.execute(
                "SELECT season_year, multiplier FROM category_rules WHERE category = '连衣裙' AND season_year IN (?, '') AND enabled = 1 ORDER BY CASE WHEN season_year = ? THEN 0 ELSE 1 END LIMIT 1",
                (clean_season, clean_season),
            ).fetchone()
        elif cost is not None:
            candidates = connection.execute(
                "SELECT season_year, lower_cost, upper_cost, multiplier FROM category_cost_rules WHERE category = '其他品类' AND season_year IN (?, '') AND enabled = 1",
                (clean_season,),
            ).fetchall()
            matching = []
            for row in candidates:
                lower = row["lower_cost"]
                upper = row["upper_cost"]
                if (lower is None or float(cost) >= float(lower)) and (upper is None or float(cost) < float(upper)):
                    matching.append(row)
            matching.sort(key=lambda row: (0 if row["season_year"] == clean_season else 1, float("-inf") if row["lower_cost"] is None else float(row["lower_cost"])), reverse=False)
            category_row = matching[0] if matching else None
        supplier_row = connection.execute(
            "SELECT season_year, coefficient FROM supplier_coefficients WHERE supplier = ? AND season_year IN (?, '') ORDER BY CASE WHEN season_year = ? THEN 0 ELSE 1 END LIMIT 1",
            (supplier.strip(), clean_season, clean_season),
        ).fetchone()
    return (float(category_row["multiplier"]) if category_row else None, float(supplier_row["coefficient"]) if supplier_row else 1.0)


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
        raise ValueError("请先在定价工作台选择品类，再生成测算上新价。")
    fixed, coefficient = resolve_rules(db_path, product.get("season_year", ""), category, product.get("supplier", ""), float(cost))
    if fixed is None:
        if category == "连衣裙":
            raise ValueError("连衣裙尚未配置固定倍率，请先到定价规则中配置。")
        raise ValueError(f"其他品类成本 {float(cost):g} 尚未落入成本区间倍率规则，请先到定价规则中配置。")
    raw = float(cost) * fixed * coefficient
    launch = round_price_to_9(raw)
    source_version_no = int(product.get("source_version_no") or 1)
    with get_connection(db_path) as connection:
        existing = connection.execute(
            "SELECT * FROM pricing_records WHERE source_product_id = ? AND source_version_no = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (int(product["id"]), source_version_no),
        ).fetchone()
        if existing and existing["status"] in {"suggested", "review_pending", "confirmed", "published"}:
            return dict(existing)
    publication_id = f"PC-{product['id']}-V{source_version_no}-{secrets.token_hex(4).upper()}"
    now = utc_now()
    with get_connection(db_path) as connection:
        connection.execute(
            "INSERT INTO pricing_records (publication_id, source_product_id, source_version_no, season_year, style_code, product_name, supplier, category, cost, fixed_multiplier, supplier_coefficient, raw_price, calculated_price, launch_price, status, operator_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'suggested', ?, ?)",
            (publication_id, product["id"], source_version_no, product.get("season_year", ""), product.get("style_code", ""), product.get("product_name", ""), product.get("supplier", ""), category, float(cost), fixed, coefficient, raw, launch, launch, operator_name, now),
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


def _validated_launch_price(value) -> int:
    try:
        price = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError, ValueError):
        raise ValueError("上新价必须是有效数字。")
    if not price.is_finite() or price <= 0 or price != price.to_integral_value():
        raise ValueError("上新价必须是大于 0 的整数，不保留小数位。")
    return int(price)


def submit_pricing_for_review(db_path: str | Path, record_id: int, launch_price, operator_name: str) -> dict:
    price = _validated_launch_price(launch_price)
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            raise LookupError("定价记录不存在。")
        if row["status"] not in {"suggested", "conflict"}:
            raise ValueError("当前定价记录不在商品部初审阶段。")
        connection.execute(
            "UPDATE pricing_records SET launch_price = ?, status = 'review_pending', operator_name = ?, confirmed_at = NULL, error_message = '' WHERE id = ?",
            (price, operator_name, record_id),
        )
        updated = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
    return dict(updated)


def save_review_price(db_path: str | Path, record_id: int, launch_price, operator_name: str) -> dict:
    price = _validated_launch_price(launch_price)
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            raise LookupError("定价记录不存在。")
        if row["status"] != "review_pending":
            raise ValueError("当前定价记录不在企划管理员复核阶段。")
        connection.execute(
            "UPDATE pricing_records SET launch_price = ?, operator_name = ?, error_message = '' WHERE id = ?",
            (price, operator_name, record_id),
        )
        updated = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
    return dict(updated)


def approve_pricing_record(db_path: str | Path, record_id: int, launch_price, operator_name: str) -> dict:
    price = _validated_launch_price(launch_price)
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            raise LookupError("定价记录不存在。")
        if row["status"] != "review_pending":
            raise ValueError("当前定价记录不在企划管理员复核阶段。")
        if price != _validated_launch_price(row["launch_price"]):
            raise ValueError("复核上新价已修改，请先点击“修改保存”，再进行复核通过。")
        connection.execute(
            "UPDATE pricing_records SET status = 'confirmed', operator_name = ?, confirmed_at = ?, error_message = '' WHERE id = ?",
            (operator_name, utc_now(), record_id),
        )
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
