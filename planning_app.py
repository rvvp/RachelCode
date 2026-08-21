from __future__ import annotations

import argparse
import os
from pathlib import Path
from wsgiref.simple_server import make_server

from planning_center import DEMO_PASSWORD, PlanningApplication, init_db


def parse_args():
    parser = argparse.ArgumentParser(description="商品企划中心")
    parser.add_argument("--host", default=os.environ.get("PLANNING_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PLANNING_PORT", "8785")))
    parser.add_argument("--db", default=os.environ.get("PLANNING_DB", str(Path(__file__).resolve().parent / "planning_data" / "planning.db")))
    parser.add_argument("--catalog-api-url", default=os.environ.get("PLANNING_CATALOG_API_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--catalog-api-token", default=os.environ.get("PLANNING_CATALOG_API_TOKEN", ""))
    parser.add_argument("--bootstrap-admin-username", default=os.environ.get("PLANNING_BOOTSTRAP_ADMIN_USERNAME", ""))
    parser.add_argument("--bootstrap-admin-password", default=os.environ.get("PLANNING_BOOTSTRAP_ADMIN_PASSWORD", ""))
    parser.add_argument("--bootstrap-admin-name", default=os.environ.get("PLANNING_BOOTSTRAP_ADMIN_NAME", "企划管理员"))
    parser.add_argument("--seed-demo", action="store_true", default=os.environ.get("PLANNING_SEED_DEMO", "1").lower() in {"1", "true", "yes", "on"})
    parser.add_argument("--no-seed-demo", action="store_false", dest="seed_demo")
    return parser.parse_args()


def main():
    args = parse_args()
    bootstrap_admin = None
    if args.bootstrap_admin_username or args.bootstrap_admin_password:
        bootstrap_admin = {
            "username": args.bootstrap_admin_username,
            "password": args.bootstrap_admin_password,
            "display_name": args.bootstrap_admin_name,
        }
    init_db(args.db, seed_demo=args.seed_demo, bootstrap_admin=bootstrap_admin)
    app = PlanningApplication(args.db, args.catalog_api_url, args.catalog_api_token)
    print("商品企划中心已启动")
    print(f"访问地址: http://{args.host}:{args.port}")
    print(f"独立数据库: {args.db}")
    if args.seed_demo:
        print(f"商品部企划员: planner / {DEMO_PASSWORD}")
        print(f"企划管理员: planning_admin / {DEMO_PASSWORD}")
    with make_server(args.host, args.port, app) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
