from __future__ import annotations

import argparse
import os
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from replenishment_center import DEMO_PASSWORD, ReplenishmentApplication, init_db
from replenishment_center.scheduler import SchedulerThread


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_args():
    parser = argparse.ArgumentParser(description="智能补货中心（独立服务）")
    parser.add_argument("--host", default=os.environ.get("REPLENISH_HOST", "127.0.0.1"), help="监听地址")
    parser.add_argument("--port", type=int, default=int(os.environ.get("REPLENISH_PORT", "8877")), help="监听端口")
    parser.add_argument(
        "--db",
        default=os.environ.get("REPLENISH_DB", str(Path(__file__).resolve().parent / "replenishment_data" / "replenishment.db")),
        help="独立 SQLite 数据库路径",
    )
    parser.add_argument("--seed-demo", action="store_true", default=env_flag("REPLENISH_SEED_DEMO", True), help="初始化单店试跑数据")
    parser.add_argument("--no-seed-demo", action="store_false", dest="seed_demo", help="不初始化试跑账号和数据")
    parser.add_argument("--scheduler", action="store_true", default=env_flag("REPLENISH_SCHEDULER", True), help="启用自动计划线程")
    parser.add_argument("--no-scheduler", action="store_false", dest="scheduler", help="关闭自动计划线程")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    init_db(args.db, seed_demo=args.seed_demo)
    app = ReplenishmentApplication(args.db)
    scheduler = SchedulerThread(args.db) if args.scheduler else None
    if scheduler:
        scheduler.start()
    print("智能补货中心已启动（独立服务）")
    print(f"访问地址: http://{args.host}:{args.port}")
    print(f"独立数据库: {args.db}")
    print("店铺工作区: 马天奴唯品会 / 马天奴天猫官方旗舰店 / BNX唯品会")
    print("唯品会默认补货频率: 周二、周五 10:00（可在页面修改）")
    if args.seed_demo:
        print(f"商品部: merch / {DEMO_PASSWORD}")
        print(f"跟单部: followup / {DEMO_PASSWORD}")
        print(f"管理层: manager / {DEMO_PASSWORD}")
        print(f"管理员: admin / {DEMO_PASSWORD}")
    try:
        with make_server(args.host, args.port, app, server_class=ThreadingWSGIServer) as server:
            server.serve_forever()
    finally:
        if scheduler:
            scheduler.stop()
            scheduler.join(timeout=2)


if __name__ == "__main__":
    main()
