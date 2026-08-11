from __future__ import annotations

import argparse
import os
from pathlib import Path
from wsgiref.simple_server import make_server

from catalog_backend import CatalogApplication, DEMO_PASSWORD, init_db


def load_env_file(env_path: str | Path) -> None:
    path = Path(env_path)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return int(value)


def env_text(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip() or default


def parse_args():
    parser = argparse.ArgumentParser(description="思安娜的藏寶閣")
    parser.add_argument("--host", default=os.environ.get("CATALOG_HOST", "127.0.0.1"), help="监听地址")
    parser.add_argument("--port", type=int, default=env_int("CATALOG_PORT", 8765), help="监听端口")
    parser.add_argument(
        "--db",
        default=os.environ.get("CATALOG_DB", str(Path(__file__).resolve().parent / "data" / "catalog.db")),
        help="SQLite 数据库路径",
    )
    parser.add_argument(
        "--uploads",
        default=os.environ.get("CATALOG_UPLOADS", str(Path(__file__).resolve().parent / "data" / "uploads")),
        help="上传文件目录",
    )
    parser.add_argument("--seed-demo", action="store_true", default=env_flag("CATALOG_SEED_DEMO", True), help="初始化演示账号和演示数据")
    parser.add_argument("--no-seed-demo", action="store_false", dest="seed_demo", help="不初始化演示账号和演示数据")
    parser.add_argument("--seed-samples", action="store_true", default=env_flag("CATALOG_SEED_SAMPLES", True), help="初始化演示商品数据")
    parser.add_argument("--no-seed-samples", action="store_false", dest="seed_samples", help="不初始化演示商品数据")
    parser.add_argument("--bootstrap-admin-username", default=os.environ.get("CATALOG_BOOTSTRAP_ADMIN_USERNAME", ""), help="首次初始化时创建管理员用户名")
    parser.add_argument("--bootstrap-admin-password", default=os.environ.get("CATALOG_BOOTSTRAP_ADMIN_PASSWORD", ""), help="首次初始化时创建管理员密码")
    parser.add_argument("--bootstrap-admin-name", default=os.environ.get("CATALOG_BOOTSTRAP_ADMIN_NAME", "系统管理员"), help="首次初始化时创建管理员显示名称")
    parser.add_argument("--bootstrap-admin-no-force-reset", action="store_true", default=not env_flag("CATALOG_BOOTSTRAP_ADMIN_FORCE_RESET", True), help="初始化管理员后不强制首次改密")
    parser.add_argument("--brand-name", default=env_text("CATALOG_BRAND_NAME", "思安娜的\n藏寶閣"), help="系统品牌名称")
    parser.add_argument("--brand-mark", default=env_text("CATALOG_BRAND_MARK", "Sienna"), help="系统品牌简称")
    parser.add_argument("--brand-tagline", default=env_text("CATALOG_BRAND_TAGLINE", "让商品资料从分散表格进入统一底库"), help="登录页主文案")
    parser.add_argument("--brand-subtitle", default=env_text("CATALOG_BRAND_SUBTITLE", "面向跟单部、商品部与运营部协作的内部资料后台，支持跟单部主体填写、商品部补充品类、图片、上新价格、上新渠道和资料完成，运营部只读开放与结构化调用。"), help="登录页副文案")
    parser.add_argument("--brand-eyebrow", default=env_text("CATALOG_BRAND_EYEBROW", "Siana Treasure Pavilion"), help="登录页英文副标题")
    parser.add_argument("--brand-console-eyebrow", default=env_text("CATALOG_CONSOLE_EYEBROW", "Siana Treasure Workspace"), help="后台首页英文副标题")
    parser.add_argument("--brand-accent", default=env_text("CATALOG_BRAND_ACCENT", "#bc6c25"), help="主色")
    parser.add_argument("--brand-accent-strong", default=env_text("CATALOG_BRAND_ACCENT_STRONG", "#7f3b08"), help="深主色")
    parser.add_argument("--brand-accent-deep", default=env_text("CATALOG_BRAND_ACCENT_DEEP", "#355f52"), help="辅色")
    return parser.parse_args()


def main():
    load_env_file(Path(__file__).resolve().parent / ".env")
    args = parse_args()
    bootstrap_admin = None
    if args.bootstrap_admin_username and args.bootstrap_admin_password:
        bootstrap_admin = {
            "username": args.bootstrap_admin_username,
            "display_name": args.bootstrap_admin_name,
            "password": args.bootstrap_admin_password,
            "must_change_password": not args.bootstrap_admin_no_force_reset,
        }
    init_db(
        args.db,
        seed_demo=args.seed_demo,
        seed_samples=args.seed_samples and args.seed_demo,
        bootstrap_admin=bootstrap_admin,
    )
    app = CatalogApplication(
        args.db,
        args.uploads,
        brand_config={
            "brand_name": args.brand_name,
            "brand_mark": args.brand_mark,
            "brand_tagline": args.brand_tagline,
            "brand_subtitle": args.brand_subtitle,
            "brand_eyebrow": args.brand_eyebrow,
            "brand_console_eyebrow": args.brand_console_eyebrow,
            "accent": args.brand_accent,
            "accent_strong": args.brand_accent_strong,
            "accent_deep": args.brand_accent_deep,
        },
    )
    print("思安娜的藏寶閣已启动")
    print(f"访问地址: http://{args.host}:{args.port}")
    print(f"数据库: {args.db}")
    print(f"上传目录: {args.uploads}")
    if args.seed_demo:
        print("默认演示账号:")
        print(f"  跟单部: a_editor / {DEMO_PASSWORD}")
        print(f"  商品部: b_editor / {DEMO_PASSWORD}")
        print(f"  运营部: c_viewer / {DEMO_PASSWORD}")
        print(f"  系统管理员: admin_reviewer / {DEMO_PASSWORD}")
    elif bootstrap_admin:
        print("已启用正式初始化模式:")
        print(f"  管理员账号: {bootstrap_admin['username']}")
        print("  建议首次登录后立即修改密码。")
    else:
        print("当前未注入演示账号。请先通过初始化管理员参数创建首个管理员账号。")
    with make_server(args.host, args.port, app) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
