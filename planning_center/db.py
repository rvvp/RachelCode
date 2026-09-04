from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path

DEMO_PASSWORD = "demo123"
PASSWORD_MIN_LENGTH = 8
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
                category_suggestion TEXT NOT NULL DEFAULT '',
                actual_cost REAL,
                tax_included_price REAL,
                image_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                lifecycle_status TEXT NOT NULL DEFAULT '',
                source_version_no INTEGER NOT NULL DEFAULT 1,
                image_version_no INTEGER NOT NULL DEFAULT 1,
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
            CREATE TABLE IF NOT EXISTS category_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                keywords TEXT NOT NULL DEFAULT '',
                pricing_group TEXT NOT NULL DEFAULT 'other',
                sort_order INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channel_options (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                sort_order INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
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
                channel TEXT NOT NULL DEFAULT '',
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
        if "category_suggestion" not in source_columns:
            connection.execute("ALTER TABLE source_products ADD COLUMN category_suggestion TEXT NOT NULL DEFAULT ''")
        if "image_version_no" not in source_columns:
            connection.execute("ALTER TABLE source_products ADD COLUMN image_version_no INTEGER NOT NULL DEFAULT 1")
        pricing_columns = {row["name"] for row in connection.execute("PRAGMA table_info(pricing_records)").fetchall()}
        if "calculated_price" not in pricing_columns:
            connection.execute("ALTER TABLE pricing_records ADD COLUMN calculated_price REAL")
            connection.execute("UPDATE pricing_records SET calculated_price = launch_price WHERE calculated_price IS NULL")
        if "channel" not in pricing_columns:
            connection.execute("ALTER TABLE pricing_records ADD COLUMN channel TEXT NOT NULL DEFAULT ''")
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
        if connection.execute("SELECT COUNT(*) FROM category_options").fetchone()[0] == 0:
            now = utc_now()
            connection.executemany(
                "INSERT INTO category_options (name, keywords, pricing_group, sort_order, note, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("连衣裙", "连衣裙,裙装,裙子", "dress", 10, "命中关键词时自动判定；使用连衣裙固定倍率。", now),
                    ("毛衣", "针织衫,毛衣,针织上衣", "other", 20, "", now),
                    ("衬衫", "衬衫,衬衣", "other", 30, "", now),
                    ("外套", "外套,大衣,风衣,夹克", "other", 40, "", now),
                    ("半身裙", "半身裙", "other", 50, "", now),
                    ("裤装", "裤装,裤子,长裤,短裤", "other", 60, "", now),
                    ("其他品类", "", "other", 999, "未命中其他关键词时的默认品类；使用成本区间倍率。", now),
                ],
            )
        for row in connection.execute(
            "SELECT DISTINCT TRIM(category) AS name FROM source_products WHERE TRIM(category) != '' "
            "UNION SELECT DISTINCT TRIM(category) AS name FROM pricing_records WHERE TRIM(category) != ''"
        ).fetchall():
            name = str(row["name"] or "").strip()
            if name:
                connection.execute(
                    "INSERT OR IGNORE INTO category_options (name, keywords, pricing_group, sort_order, note, updated_at) VALUES (?, ?, ?, 500, '', ?)",
                    (name, name, "dress" if name == "连衣裙" else "other", utc_now()),
                )
        if connection.execute("SELECT COUNT(*) FROM channel_options").fetchone()[0] == 0:
            now = utc_now()
            connection.executemany(
                "INSERT INTO channel_options (name, sort_order, note, updated_at) VALUES (?, ?, ?, ?)",
                [("天猫", 10, "", now), ("唯品", 20, "", now), ("同款", 30, "", now)],
            )


def authenticate_user(db_path: str | Path, username: str, password: str) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username.strip(),)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        return None
    return dict(row)


def list_users(db_path: str | Path) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT id, username, display_name, role, is_active, created_at
            FROM users
            ORDER BY is_active DESC, CASE role WHEN 'admin' THEN 0 ELSE 1 END, created_at, id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_user(db_path: str | Path, user_id: int, *, include_password: bool = False) -> dict | None:
    columns = "*" if include_password else "id, username, display_name, role, is_active, created_at"
    with get_connection(db_path) as connection:
        row = connection.execute(f"SELECT {columns} FROM users WHERE id = ?", (int(user_id),)).fetchone()
    return dict(row) if row else None


def _validate_account_fields(username: str, display_name: str, role: str) -> tuple[str, str, str]:
    clean_username = str(username or "").strip()
    clean_display_name = str(display_name or "").strip()
    clean_role = str(role or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,39}", clean_username):
        raise ValueError("登录账号需为 3-40 位字母、数字、点、下划线或短横线，并以字母或数字开头。")
    if not clean_display_name:
        raise ValueError("姓名不能为空。")
    if len(clean_display_name) > 50:
        raise ValueError("姓名不能超过 50 个字符。")
    if clean_role not in {"planner", "admin"}:
        raise ValueError("账号角色不正确。")
    return clean_username, clean_display_name, clean_role


def validate_account_password(password: str) -> str:
    clean_password = str(password or "")
    if len(clean_password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"密码至少需要 {PASSWORD_MIN_LENGTH} 位。")
    if len(clean_password) > 128:
        raise ValueError("密码不能超过 128 位。")
    return clean_password


def create_user(db_path: str | Path, username: str, display_name: str, role: str, password: str) -> dict:
    clean_username, clean_display_name, clean_role = _validate_account_fields(username, display_name, role)
    clean_password = validate_account_password(password)
    try:
        with get_connection(db_path) as connection:
            duplicate = connection.execute(
                "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
                (clean_username,),
            ).fetchone()
            if duplicate:
                raise ValueError("该登录账号已存在。")
            cursor = connection.execute(
                """
                INSERT INTO users (username, display_name, role, password_hash, is_active, created_at)
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (clean_username, clean_display_name, clean_role, hash_password(clean_password), utc_now()),
            )
            user_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError as error:
        raise ValueError("该登录账号已存在。") from error
    return get_user(db_path, user_id)


def set_user_active(db_path: str | Path, user_id: int, is_active: bool) -> dict:
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
        if not row:
            raise LookupError("账号不存在。")
        if not is_active and row["role"] == "admin" and row["is_active"]:
            active_admins = connection.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active = 1"
            ).fetchone()[0]
            if active_admins <= 1:
                raise ValueError("不能停用最后一个有效的企划管理员账号。")
        connection.execute(
            "UPDATE users SET is_active = ? WHERE id = ?",
            (1 if is_active else 0, int(user_id)),
        )
    return get_user(db_path, user_id)


def reset_user_password(db_path: str | Path, user_id: int, new_password: str) -> None:
    clean_password = validate_account_password(new_password)
    with get_connection(db_path) as connection:
        cursor = connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(clean_password), int(user_id)),
        )
        if cursor.rowcount != 1:
            raise LookupError("账号不存在。")


def change_user_password(db_path: str | Path, user_id: int, current_password: str, new_password: str) -> None:
    clean_password = validate_account_password(new_password)
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT password_hash FROM users WHERE id = ? AND is_active = 1",
            (int(user_id),),
        ).fetchone()
        if not row:
            raise LookupError("账号不存在或已停用。")
        if not verify_password(str(current_password or ""), row["password_hash"]):
            raise ValueError("当前密码不正确。")
        if verify_password(clean_password, row["password_hash"]):
            raise ValueError("新密码不能与当前密码相同。")
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(clean_password), int(user_id)),
        )


def list_source_products(db_path: str | Path, *, season_year: str = "", status: str = "") -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM source_products WHERE status = 'pending' AND lifecycle_status = 'active' AND (? = '' OR season_year = ?) AND (? = '' OR status = ?) ORDER BY season_year DESC, source_updated_at DESC, id DESC",
            (season_year, season_year, status, status),
        ).fetchall()
    return [dict(row) for row in rows]


def list_waiting_source_products(db_path: str | Path, *, season_year: str = "") -> list[dict]:
    """Return every source item that has not entered a pricing cycle yet."""
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT sp.*
            FROM source_products sp
            WHERE sp.status = 'pending'
              AND sp.lifecycle_status = 'active'
              AND (? = '' OR sp.season_year = ?)
              AND NOT EXISTS (
                  SELECT 1 FROM pricing_records pr
                  WHERE pr.source_product_id = sp.id
              )
            ORDER BY sp.season_year DESC, sp.source_updated_at DESC, sp.id DESC
            """,
            (season_year, season_year),
        ).fetchall()
    return [dict(row) for row in rows]


def list_published_source_products(db_path: str | Path, *, season_year: str = "") -> list[dict]:
    """Return completed source rows so operators can start a same-style revision."""
    with get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT sp.*
            FROM source_products sp
            WHERE sp.lifecycle_status = 'withdrawn'
              AND EXISTS (
                  SELECT 1 FROM pricing_records pr
                  WHERE pr.source_product_id = sp.id AND pr.status = 'published'
              )
              AND (? = '' OR sp.season_year = ?)
            ORDER BY sp.season_year DESC, sp.source_updated_at DESC, sp.id DESC
            """,
            (season_year, season_year),
        ).fetchall()
    return [dict(row) for row in rows]


def list_all_source_products(db_path: str | Path) -> list[dict]:
    """Return local source history ids so image-only refreshes can be requested."""
    with get_connection(db_path) as connection:
        rows = connection.execute("SELECT * FROM source_products ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def get_source_product(db_path: str | Path, product_id: int) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM source_products WHERE id = ?", (product_id,)).fetchone()
    return dict(row) if row else None


def list_category_options(db_path: str | Path, *, enabled_only: bool = False) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM category_options WHERE (? = 0 OR enabled = 1) ORDER BY sort_order, id",
            (1 if enabled_only else 0,),
        ).fetchall()
    return [dict(row) for row in rows]


def list_channel_options(db_path: str | Path, *, enabled_only: bool = False) -> list[dict]:
    with get_connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM channel_options WHERE (? = 0 OR enabled = 1) ORDER BY sort_order, id",
            (1 if enabled_only else 0,),
        ).fetchall()
    return [dict(row) for row in rows]


def _option_sort_order(value) -> int:
    try:
        order = int(str(value or "0").strip())
    except ValueError:
        raise ValueError("排序必须是整数。")
    if order < 0 or order > 9999:
        raise ValueError("排序必须在 0 到 9999 之间。")
    return order


def save_category_option(
    db_path: str | Path,
    name: str,
    keywords: str = "",
    sort_order=0,
    note: str = "",
    option_id: int | None = None,
) -> None:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("品类名称不能为空。")
    if len(clean_name) > 40:
        raise ValueError("品类名称不能超过 40 个字符。")
    clean_keywords = str(keywords or "").strip()
    pricing_group = "dress" if clean_name == "连衣裙" else "other"
    order = _option_sort_order(sort_order)
    with get_connection(db_path) as connection:
        duplicate = connection.execute(
            "SELECT id FROM category_options WHERE name = ? AND (? IS NULL OR id != ?)",
            (clean_name, option_id, option_id),
        ).fetchone()
        if duplicate:
            raise ValueError("该品类选项已存在。")
        if option_id is not None:
            target = connection.execute("SELECT id FROM category_options WHERE id = ?", (int(option_id),)).fetchone()
            if not target:
                raise LookupError("品类选项不存在。")
            connection.execute(
                "UPDATE category_options SET name = ?, keywords = ?, pricing_group = ?, sort_order = ?, note = ?, enabled = 1, updated_at = ? WHERE id = ?",
                (clean_name, clean_keywords, pricing_group, order, str(note or "").strip(), utc_now(), int(option_id)),
            )
            return
        connection.execute(
            "INSERT INTO category_options (name, keywords, pricing_group, sort_order, note, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (clean_name, clean_keywords, pricing_group, order, str(note or "").strip(), utc_now()),
        )


def delete_category_option(db_path: str | Path, option_id: int) -> None:
    with get_connection(db_path) as connection:
        cursor = connection.execute("DELETE FROM category_options WHERE id = ?", (int(option_id),))
        if cursor.rowcount != 1:
            raise LookupError("品类选项不存在。")


def save_channel_option(
    db_path: str | Path,
    name: str,
    sort_order=0,
    note: str = "",
    option_id: int | None = None,
) -> None:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("渠道名称不能为空。")
    if len(clean_name) > 40:
        raise ValueError("渠道名称不能超过 40 个字符。")
    order = _option_sort_order(sort_order)
    with get_connection(db_path) as connection:
        duplicate = connection.execute(
            "SELECT id FROM channel_options WHERE name = ? AND (? IS NULL OR id != ?)",
            (clean_name, option_id, option_id),
        ).fetchone()
        if duplicate:
            raise ValueError("该渠道选项已存在。")
        if option_id is not None:
            target = connection.execute("SELECT id FROM channel_options WHERE id = ?", (int(option_id),)).fetchone()
            if not target:
                raise LookupError("渠道选项不存在。")
            connection.execute(
                "UPDATE channel_options SET name = ?, sort_order = ?, note = ?, enabled = 1, updated_at = ? WHERE id = ?",
                (clean_name, order, str(note or "").strip(), utc_now(), int(option_id)),
            )
            return
        connection.execute(
            "INSERT INTO channel_options (name, sort_order, note, updated_at) VALUES (?, ?, ?, ?)",
            (clean_name, order, str(note or "").strip(), utc_now()),
        )


def delete_channel_option(db_path: str | Path, option_id: int) -> None:
    with get_connection(db_path) as connection:
        cursor = connection.execute("DELETE FROM channel_options WHERE id = ?", (int(option_id),))
        if cursor.rowcount != 1:
            raise LookupError("渠道选项不存在。")


def _category_keywords(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,，;；\n\r]+", str(value or "")) if part.strip()]


def infer_category(product_name: str, options: list[dict]) -> str:
    clean_name = str(product_name or "").strip().casefold()
    matches: list[tuple[int, int, str]] = []
    fallback = ""
    for option in options:
        if not option.get("enabled", 1):
            continue
        name = str(option.get("name") or "").strip()
        if name == "其他品类":
            fallback = name
        for keyword in _category_keywords(option.get("keywords", "")):
            if keyword.casefold() in clean_name:
                matches.append((len(keyword), -int(option.get("sort_order") or 0), name))
    return max(matches)[2] if matches else fallback


def validate_category_option(db_path: str | Path, category: str) -> str:
    clean_category = str(category or "").strip()
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT name FROM category_options WHERE name = ? AND enabled = 1",
            (clean_category,),
        ).fetchone()
    if not row:
        raise ValueError("请选择规则中已启用的品类选项。")
    return str(row["name"])


def resolve_product_category(db_path: str | Path, product: dict) -> str:
    """Resolve a product category against the currently enabled rule options.

    A product may carry a category suggestion generated before an administrator
    changed the available options. Re-infer it from the current product name
    whenever that stored value is no longer enabled, so historical sync data
    continues to work after category rules are simplified or renamed.
    """
    options = list_category_options(db_path, enabled_only=True)
    enabled_names = {str(option.get("name") or "").strip() for option in options}
    for value in (product.get("category"), product.get("category_suggestion")):
        stored_category = str(value or "").strip()
        if stored_category in enabled_names:
            return stored_category
    inferred = infer_category(str(product.get("product_name") or ""), options)
    if inferred:
        return validate_category_option(db_path, inferred)
    raise ValueError("请选择规则中已启用的品类选项。")


def validate_channel_option(db_path: str | Path, channel: str) -> str:
    clean_channel = str(channel or "").strip()
    with get_connection(db_path) as connection:
        row = connection.execute(
            "SELECT name FROM channel_options WHERE name = ? AND enabled = 1",
            (clean_channel,),
        ).fetchone()
    if not row:
        raise ValueError("请选择规则中已启用的渠道选项。")
    return str(row["name"])


def planning_source_item_is_eligible(item: dict) -> bool:
    return (
        str(item.get("status") or "").strip() == "pending"
        and str(item.get("lifecycle_status") or "active").strip() == "active"
        and bool(str(item.get("image_url") or "").strip() or str(item.get("image_gallery_json") or "").strip() not in {"", "[]"})
    )


def _source_item_has_image(item: dict) -> bool:
    image_url = str(item.get("image_url") or "").strip()
    if image_url:
        return True
    gallery = str(item.get("image_gallery_json") or "").strip()
    return gallery not in {"", "[]"}


def upsert_source_products(db_path: str | Path, items: list[dict], *, require_image: bool = False) -> int:
    eligible_items = [
        item
        for item in items
        if str(item.get("status") or "").strip() == "pending"
        and str(item.get("lifecycle_status") or "active").strip() == "active"
        and (not require_image or _source_item_has_image(item))
    ]
    now = utc_now()
    with get_connection(db_path) as connection:
        category_options = [dict(row) for row in connection.execute("SELECT * FROM category_options WHERE enabled = 1 ORDER BY sort_order, id").fetchall()]
        for item in eligible_items:
            category_suggestion = infer_category(item.get("product_name", ""), category_options)
            connection.execute(
                """
                INSERT INTO source_products (
                    id, style_code, style_color, color_name, product_name, brand_name, season_year,
                    supplier, supplier_code, supplier_style_code, category, category_suggestion, actual_cost, tax_included_price,
                    image_url, status, lifecycle_status, source_version_no, image_version_no, source_updated_at, creator_name, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    style_code=excluded.style_code, style_color=excluded.style_color, color_name=excluded.color_name,
                    product_name=excluded.product_name, brand_name=excluded.brand_name, season_year=excluded.season_year,
                    supplier=excluded.supplier, supplier_code=excluded.supplier_code, supplier_style_code=excluded.supplier_style_code,
                    category=CASE WHEN NOT EXISTS (SELECT 1 FROM pricing_records WHERE source_product_id = excluded.id) THEN excluded.category ELSE source_products.category END,
                    category_suggestion=excluded.category_suggestion, actual_cost=excluded.actual_cost, tax_included_price=excluded.tax_included_price, image_url=excluded.image_url,
                    status=excluded.status, lifecycle_status=excluded.lifecycle_status, source_version_no=excluded.source_version_no, image_version_no=excluded.image_version_no,
                    source_updated_at=excluded.source_updated_at, creator_name=excluded.creator_name, synced_at=excluded.synced_at
                """,
                (
                    int(item["id"]), item.get("style_code", ""), item.get("style_color", ""), item.get("color_name", ""),
                    item.get("product_name", ""), item.get("brand_name", ""), item.get("season_year", ""), item.get("supplier", ""),
                    item.get("supplier_code", ""), item.get("supplier_style_code", ""), category_suggestion, category_suggestion, item.get("actual_cost"),
                    item.get("tax_included_price"), item.get("image_url", ""), "pending", "active", int(item.get("source_version_no") or 1), int(item.get("image_version_no") or 1),
                    item.get("updated_at", ""), item.get("creator_name", ""), now,
                ),
            )
    return len(eligible_items)


def upsert_source_image_updates(db_path: str | Path, items: list[dict]) -> int:
    """Apply image-only updates to existing source rows without reopening work."""
    updated = 0
    now = utc_now()
    with get_connection(db_path) as connection:
        for item in items:
            if not _source_item_has_image(item):
                continue
            source_id = int(item.get("id") or 0)
            if source_id <= 0:
                continue
            existing = connection.execute(
                "SELECT image_url, image_version_no, source_version_no FROM source_products WHERE id = ?",
                (source_id,),
            ).fetchone()
            if not existing:
                continue
            incoming_version = int(item.get("image_version_no") or 1)
            current_version = int(existing["image_version_no"] or 1)
            incoming_image = str(item.get("image_url") or "").strip()
            incoming_source_version = int(item.get("source_version_no") or 1)
            image_changed = (
                incoming_version > current_version
                or incoming_image != str(existing["image_url"] or "").strip()
            )
            if (
                not image_changed
                and incoming_source_version == int(existing["source_version_no"] or 1)
            ):
                continue
            connection.execute(
                "UPDATE source_products SET image_url = ?, image_version_no = ?, source_version_no = ?, source_updated_at = ?, synced_at = ? WHERE id = ?",
                (
                    incoming_image,
                    max(incoming_version, current_version + 1) if image_changed else max(incoming_version, current_version),
                    incoming_source_version,
                    item.get("updated_at", ""),
                    now,
                    source_id,
                ),
            )
            if image_changed:
                updated += 1
    return updated


def synchronize_source_products(
    db_path: str | Path,
    items: list[dict],
    *,
    withdrawn_ids: list[int] | tuple[int, ...] | set[int] = (),
    image_updates: list[dict] | tuple[dict, ...] = (),
    require_image: bool = True,
) -> dict:
    eligible_items = [
        item
        for item in items
        if (
            planning_source_item_is_eligible(item)
            if require_image
            else str(item.get("status") or "").strip() == "pending"
            and str(item.get("lifecycle_status") or "active").strip() == "active"
        )
    ]
    # Keep already imported legacy rows usable when they predate the image gate;
    # new rows still require a real image before entering the workbench.
    strict_ids = {int(item.get("id") or 0) for item in eligible_items}
    legacy_items = []
    with get_connection(db_path) as connection:
        existing_ids = {
            int(row["id"])
            for row in connection.execute("SELECT id FROM source_products").fetchall()
        }
    for item in items:
        source_id = int(item.get("id") or 0)
        if source_id in existing_ids and source_id not in strict_ids:
            if str(item.get("status") or "").strip() == "pending" and str(item.get("lifecycle_status") or "active").strip() == "active":
                legacy_items.append(item)
    explicit_withdrawn_ids = {int(product_id) for product_id in withdrawn_ids}
    synced = upsert_source_products(db_path, eligible_items, require_image=require_image)
    if legacy_items:
        synced += upsert_source_products(db_path, legacy_items, require_image=False)
    image_updated = upsert_source_image_updates(db_path, list(image_updates))
    removed = 0
    withdrawn = 0
    with get_connection(db_path) as connection:
        source_rows = connection.execute(
            "SELECT id, status, lifecycle_status FROM source_products"
        ).fetchall()
        for row in source_rows:
            source_id = int(row["id"])
            locally_ineligible = row["status"] != "pending" or row["lifecycle_status"] != "active"
            if source_id not in explicit_withdrawn_ids and not locally_ineligible:
                continue
            latest_record = connection.execute(
                "SELECT status FROM pricing_records WHERE source_product_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (source_id,),
            ).fetchone()
            if (
                latest_record
                and latest_record["status"] in {"suggested", "review_pending", "confirmed", "conflict"}
                and row["status"] == "pending"
                and row["lifecycle_status"] == "active"
            ):
                # A revision is intentionally kept in the workbench even while
                # the catalog reports the prior published version.
                continue
            has_pricing_record = connection.execute(
                "SELECT 1 FROM pricing_records WHERE source_product_id = ? LIMIT 1",
                (source_id,),
            ).fetchone()
            if has_pricing_record:
                if row["lifecycle_status"] != "withdrawn":
                    connection.execute(
                        "UPDATE source_products SET lifecycle_status = 'withdrawn', synced_at = ? WHERE id = ?",
                        (utc_now(), source_id),
                    )
                    withdrawn += 1
                continue
            connection.execute("DELETE FROM source_products WHERE id = ?", (source_id,))
            removed += 1
    result = {"synced": synced, "removed": removed, "withdrawn": withdrawn}
    if image_updated:
        result["image_updated"] = image_updated
    return result


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


def calculate_pricing(
    db_path: str | Path,
    season_year: str,
    category: str,
    supplier: str,
    cost,
) -> dict:
    if cost is None or float(cost) <= 0:
        raise ValueError("藏宝阁尚未提供有效的含税采购成本。")
    clean_category = str(category or "").strip()
    if not clean_category:
        raise ValueError("请先选择品类，再生成测算上新价。")
    fixed, coefficient = resolve_rules(db_path, str(season_year or ""), clean_category, str(supplier or ""), float(cost))
    if fixed is None:
        if clean_category == "连衣裙":
            raise ValueError("连衣裙尚未配置固定倍率，请先到规则中配置。")
        raise ValueError(f"其他品类成本 {float(cost):g} 尚未落入成本区间倍率规则，请先到规则中配置。")
    raw = float(cost) * fixed * coefficient
    return {
        "category": clean_category,
        "fixed_multiplier": fixed,
        "supplier_coefficient": coefficient,
        "raw_price": raw,
        "calculated_price": round_price_to_9(raw),
    }


def create_pricing_records(db_path: str | Path, products: list[dict], operator_name: str) -> list[dict]:
    prepared = []
    for product in products:
        cost = product.get("actual_cost")
        category = str(product.get("category") or product.get("category_suggestion") or "").strip()
        calculation = calculate_pricing(
            db_path,
            product.get("season_year", ""),
            category,
            product.get("supplier", ""),
            cost,
        )
        prepared.append(
            (
                product,
                category,
                float(cost),
                calculation,
                int(product.get("source_version_no") or 1),
            )
        )

    now = utc_now()
    created = []
    with get_connection(db_path) as connection:
        for product, category, cost, calculation, source_version_no in prepared:
            existing = connection.execute(
                "SELECT * FROM pricing_records WHERE source_product_id = ? AND source_version_no = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                (int(product["id"]), source_version_no),
            ).fetchone()
            connection.execute("UPDATE source_products SET category = ? WHERE id = ?", (category, int(product["id"])))
            if existing and existing["status"] in {"suggested", "review_pending", "confirmed", "published"}:
                created.append(dict(existing))
                continue
            publication_id = f"PC-{product['id']}-V{source_version_no}-{secrets.token_hex(4).upper()}"
            launch = calculation["calculated_price"]
            connection.execute(
                "INSERT INTO pricing_records (publication_id, source_product_id, source_version_no, season_year, style_code, product_name, supplier, category, channel, cost, fixed_multiplier, supplier_coefficient, raw_price, calculated_price, launch_price, status, operator_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, 'suggested', ?, ?)",
                (
                    publication_id,
                    product["id"],
                    source_version_no,
                    product.get("season_year", ""),
                    product.get("style_code", ""),
                    product.get("product_name", ""),
                    product.get("supplier", ""),
                    category,
                    cost,
                    calculation["fixed_multiplier"],
                    calculation["supplier_coefficient"],
                    calculation["raw_price"],
                    launch,
                    launch,
                    operator_name,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM pricing_records WHERE publication_id = ?", (publication_id,)).fetchone()
            created.append(dict(row))
    return created


def create_pricing_record(db_path: str | Path, product: dict, operator_name: str) -> dict:
    return create_pricing_records(db_path, [product], operator_name)[0]


def start_pricing_revision(db_path: str | Path, source_product_id: int, operator_name: str) -> dict:
    """Reopen the same source product for a new planning review cycle."""
    source = get_source_product(db_path, source_product_id)
    if not source:
        raise LookupError("同步商品不存在。")
    if not _source_item_has_image(source):
        raise ValueError("当前商品没有图片，不能发起企划修订。")
    with get_connection(db_path) as connection:
        latest = connection.execute(
            "SELECT * FROM pricing_records WHERE source_product_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (int(source_product_id),),
        ).fetchone()
        if not latest or latest["status"] != "published":
            raise ValueError("只有已回传的商品才能发起新的企划修订。")
        active_revision = connection.execute(
            "SELECT * FROM pricing_records WHERE source_product_id = ? AND status IN ('suggested', 'review_pending', 'confirmed', 'conflict') ORDER BY created_at DESC, id DESC LIMIT 1",
            (int(source_product_id),),
        ).fetchone()
        if active_revision:
            return dict(active_revision)
    category = str(source.get("category") or latest["category"] or "").strip()
    calculation = calculate_pricing(
        db_path,
        source.get("season_year", ""),
        category,
        source.get("supplier", ""),
        source.get("actual_cost"),
    )
    now = utc_now()
    source_version = int(source.get("source_version_no") or 1)
    publication_id = f"PC-{int(source_product_id)}-REV-V{source_version}-{secrets.token_hex(4).upper()}"
    with get_connection(db_path) as connection:
        connection.execute(
            "UPDATE source_products SET status = 'pending', lifecycle_status = 'active', category = ?, synced_at = ? WHERE id = ?",
            (category, now, int(source_product_id)),
        )
        connection.execute(
            "INSERT INTO pricing_records (publication_id, source_product_id, source_version_no, season_year, style_code, product_name, supplier, category, channel, cost, fixed_multiplier, supplier_coefficient, raw_price, calculated_price, launch_price, status, operator_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, 'suggested', ?, ?)",
            (
                publication_id,
                int(source_product_id),
                source_version,
                source.get("season_year", ""),
                source.get("style_code", ""),
                source.get("product_name", ""),
                source.get("supplier", ""),
                category,
                source.get("actual_cost"),
                calculation["fixed_multiplier"],
                calculation["supplier_coefficient"],
                calculation["raw_price"],
                calculation["calculated_price"],
                calculation["calculated_price"],
                operator_name,
                now,
            ),
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


def list_pricing_export_rows(
    db_path: str | Path,
    *,
    season_year: str = "",
    status: str = "",
    record_ids: list[int] | tuple[int, ...] | set[int] = (),
) -> list[dict]:
    base_conditions = ["(? = '' OR pr.season_year = ?)"]
    base_params: list[object] = [season_year, season_year]
    clean_status = str(status or "").strip()
    if clean_status:
        base_conditions.append("pr.status = ?")
        base_params.append(clean_status)
    clean_ids = sorted({int(record_id) for record_id in record_ids if str(record_id).isdigit()})
    id_chunks = [clean_ids[index : index + 800] for index in range(0, len(clean_ids), 800)] or [None]
    rows: list[object] = []
    with get_connection(db_path) as connection:
        for id_chunk in id_chunks:
            conditions = list(base_conditions)
            params = list(base_params)
            if id_chunk is not None:
                placeholders = ", ".join("?" for _ in id_chunk)
                conditions.append(f"pr.id IN ({placeholders})")
                params.extend(id_chunk)
            rows.extend(
                connection.execute(
                    f"""
                    SELECT pr.*, sp.style_color, sp.color_name,
                           sp.source_version_no AS current_source_version_no
                    FROM pricing_records pr
                    JOIN source_products sp ON sp.id = pr.source_product_id
                    WHERE {' AND '.join(conditions)}
                    ORDER BY pr.season_year DESC, pr.created_at DESC, pr.id DESC
                    """,
                    params,
                ).fetchall()
            )
    rows.sort(
        key=lambda row: (
            str(row["season_year"] or ""),
            str(row["created_at"] or ""),
            int(row["id"]),
        ),
        reverse=True,
    )
    return [dict(row) for row in rows]


def list_initial_review_export_rows(db_path: str | Path, *, season_year: str = "") -> list[dict]:
    """Backward-compatible query for the editable initial-review export."""
    rows = list_pricing_export_rows(db_path, season_year=season_year)
    return [row for row in rows if str(row.get("status") or "") in {"suggested", "conflict"}]


def get_pricing_record(db_path: str | Path, record_id: int) -> dict | None:
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
    return dict(row) if row else None


def import_initial_review_edits(db_path: str | Path, rows: list[dict], operator_name: str) -> int:
    """Validate and atomically save Excel edits without advancing the workflow."""
    if not rows:
        raise ValueError("Excel 中没有可导入的初审资料。")
    prepared: list[tuple[dict, dict, str, str, int]] = []
    seen_ids: set[int] = set()
    with get_connection(db_path) as connection:
        active_categories = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM category_options WHERE enabled = 1").fetchall()
        }
        active_channels = {
            str(row["name"])
            for row in connection.execute("SELECT name FROM channel_options WHERE enabled = 1").fetchall()
        }
        for item in rows:
            row_number = int(item.get("row_number") or 0)
            record_id = int(item.get("record_id") or 0)
            if record_id in seen_ids:
                raise ValueError(f"第 {row_number} 行的定价记录ID重复。")
            seen_ids.add(record_id)
            record_row = connection.execute(
                "SELECT * FROM pricing_records WHERE id = ?",
                (record_id,),
            ).fetchone()
            if not record_row:
                raise ValueError(f"第 {row_number} 行对应的定价记录不存在。")
            record = dict(record_row)
            source_row = connection.execute(
                "SELECT source_version_no FROM source_products WHERE id = ?",
                (int(record["source_product_id"]),),
            ).fetchone()
            if not source_row:
                raise ValueError(f"第 {row_number} 行对应的来源商品不存在。")
            if record["status"] not in {"suggested", "conflict"}:
                raise ValueError(f"第 {row_number} 行已不在待初审阶段，请重新导出最新资料。")
            if (
                str(item.get("publication_id") or "") != str(record["publication_id"])
                or int(item.get("source_product_id") or 0) != int(record["source_product_id"])
            ):
                raise ValueError(f"第 {row_number} 行的企划记录号或来源商品ID不匹配。")
            imported_version = int(item.get("source_version_no") or 0)
            if (
                imported_version != int(record["source_version_no"])
                or imported_version != int(source_row["source_version_no"])
            ):
                raise ValueError(f"第 {row_number} 行来源版本已变化，请重新导出最新资料。")
            category = str(item.get("category") or "").strip()
            channel = str(item.get("channel") or "").strip()
            if category not in active_categories:
                raise ValueError(f"第 {row_number} 行请选择规则中已启用的品类选项。")
            if channel not in active_channels:
                raise ValueError(f"第 {row_number} 行请选择规则中已启用的渠道选项。")
            price = validated_launch_price(item.get("launch_price"))
            calculation = calculate_pricing(
                db_path,
                record["season_year"],
                category,
                record["supplier"],
                record["cost"],
            )
            prepared.append((record, calculation, category, channel, price))

        for record, calculation, category, channel, price in prepared:
            connection.execute(
                """
                UPDATE pricing_records
                SET category = ?, channel = ?, fixed_multiplier = ?, supplier_coefficient = ?,
                    raw_price = ?, calculated_price = ?, launch_price = ?, operator_name = ?
                WHERE id = ?
                """,
                (
                    category,
                    channel,
                    calculation["fixed_multiplier"],
                    calculation["supplier_coefficient"],
                    calculation["raw_price"],
                    calculation["calculated_price"],
                    price,
                    operator_name,
                    int(record["id"]),
                ),
            )
            connection.execute(
                "UPDATE source_products SET category = ? WHERE id = ?",
                (category, int(record["source_product_id"])),
            )
    return len(prepared)


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


def validated_launch_price(value) -> int:
    try:
        price = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError, ValueError):
        raise ValueError("上新价必须是有效数字。")
    if not price.is_finite() or price <= 0 or price != price.to_integral_value():
        raise ValueError("上新价必须是大于 0 的整数，不保留小数位。")
    return int(price)


def recalculate_pricing_record(db_path: str | Path, record_id: int, category: str, operator_name: str) -> dict:
    clean_category = validate_category_option(db_path, category)
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            raise LookupError("定价记录不存在。")
        if row["status"] not in {"suggested", "conflict"}:
            raise ValueError("当前定价记录不在商品部初审阶段。")
    calculation = calculate_pricing(
        db_path,
        row["season_year"],
        clean_category,
        row["supplier"],
        row["cost"],
    )
    old_calculated = validated_launch_price(row["calculated_price"] or row["launch_price"])
    launch_price = (
        calculation["calculated_price"]
        if validated_launch_price(row["launch_price"]) == old_calculated
        else validated_launch_price(row["launch_price"])
    )
    with get_connection(db_path) as connection:
        connection.execute(
            "UPDATE pricing_records SET category = ?, fixed_multiplier = ?, supplier_coefficient = ?, raw_price = ?, calculated_price = ?, launch_price = ?, operator_name = ?, error_message = '' WHERE id = ?",
            (
                clean_category,
                calculation["fixed_multiplier"],
                calculation["supplier_coefficient"],
                calculation["raw_price"],
                calculation["calculated_price"],
                launch_price,
                operator_name,
                record_id,
            ),
        )
        updated = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
    return dict(updated)


def submit_pricing_for_review(
    db_path: str | Path,
    record_id: int,
    launch_price,
    operator_name: str,
    category: str | None = None,
    channel: str | None = None,
) -> dict:
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            raise LookupError("定价记录不存在。")
        if row["status"] not in {"suggested", "conflict"}:
            raise ValueError("当前定价记录不在商品部初审阶段。")
    clean_category = validate_category_option(db_path, category if category is not None else row["category"])
    clean_channel = validate_channel_option(db_path, channel if channel is not None else row["channel"])
    calculation = calculate_pricing(
        db_path,
        row["season_year"],
        clean_category,
        row["supplier"],
        row["cost"],
    )
    old_calculated = validated_launch_price(row["calculated_price"] or row["launch_price"])
    submitted_price = old_calculated if launch_price in (None, "") else validated_launch_price(launch_price)
    price = calculation["calculated_price"] if clean_category != row["category"] and submitted_price == old_calculated else submitted_price
    with get_connection(db_path) as connection:
        connection.execute(
            "UPDATE pricing_records SET category = ?, channel = ?, fixed_multiplier = ?, supplier_coefficient = ?, raw_price = ?, calculated_price = ?, launch_price = ?, status = 'review_pending', operator_name = ?, confirmed_at = NULL, error_message = '' WHERE id = ?",
            (
                clean_category,
                clean_channel,
                calculation["fixed_multiplier"],
                calculation["supplier_coefficient"],
                calculation["raw_price"],
                calculation["calculated_price"],
                price,
                operator_name,
                record_id,
            ),
        )
        connection.execute("UPDATE source_products SET category = ? WHERE id = ?", (clean_category, row["source_product_id"]))
        updated = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
    return dict(updated)


def save_review_price(
    db_path: str | Path,
    record_id: int,
    launch_price,
    channel: str,
    operator_name: str,
) -> dict:
    price = validated_launch_price(launch_price)
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            raise LookupError("定价记录不存在。")
        if row["status"] != "review_pending":
            raise ValueError("当前定价记录不在企划管理员复核阶段。")
        clean_channel = validate_channel_option(db_path, channel)
        connection.execute(
            "UPDATE pricing_records SET launch_price = ?, channel = ?, operator_name = ?, error_message = '' WHERE id = ?",
            (price, clean_channel, operator_name, record_id),
        )
        updated = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
    return dict(updated)


def approve_pricing_record(
    db_path: str | Path,
    record_id: int,
    launch_price,
    channel: str,
    operator_name: str,
) -> dict:
    price = validated_launch_price(launch_price)
    with get_connection(db_path) as connection:
        row = connection.execute("SELECT * FROM pricing_records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            raise LookupError("定价记录不存在。")
        if row["status"] != "review_pending":
            raise ValueError("当前定价记录不在企划管理员复核阶段。")
        clean_channel = validate_channel_option(db_path, channel)
        if price != validated_launch_price(row["launch_price"]) or clean_channel != row["channel"]:
            raise ValueError("复核上新价或渠道已修改，请先点击“修改保存”，再进行复核通过。")
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
