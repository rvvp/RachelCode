from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from replenishment_center import db
from replenishment_center.browser_capture import launch_dedicated_browser, run_browser_worker
from replenishment_center.vipshop import sync_to_database


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _record_schedule_key(db_path: str | Path, schedule_key: str) -> None:
    with db.get_connection(db_path) as connection:
        connection.execute(
            "UPDATE settings SET last_schedule_key = ?, updated_at = ? WHERE id = 1",
            (schedule_key, db.utc_now()),
        )


def run_due_jobs(db_path: str | Path, now: datetime | None = None) -> int | None:
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    settings = db.get_settings(db_path)
    if not settings["auto_generate"] or now.isoweekday() not in settings["schedule_weekdays"]:
        return None
    schedule_hour, schedule_minute = [int(part) for part in settings["schedule_time"].split(":", 1)]
    if (now.hour, now.minute) < (schedule_hour, schedule_minute):
        return None
    schedule_key = f"{now.date().isoformat()}@{settings['schedule_time']}"
    if settings["last_schedule_key"] == schedule_key:
        return None
    api_config = db.get_api_config(db_path)
    if settings["api_status"] == "connected" and api_config["credentials_complete"]:
        sync_to_database(db_path)
        generation_type = "scheduled_api"
    elif settings["data_source_mode"] == "browser":
        config = db.get_browser_capture_config(db_path)
        report_url = config["sales_report_url"] or config["backend_url"]
        status = launch_dedicated_browser(
            db_path,
            port=int(config["debug_port"]),
            url=report_url,
        )
        db.record_browser_status(db_path, status)
        session_available = not status.get("loginRequired")
        if not session_available:
            db.record_browser_schedule_issue(
                db_path,
                "唯品后台登录已失效，请在专用 Chrome 中重新登录后手工更新报表。",
                session_available=False,
            )
            _record_schedule_key(db_path, schedule_key)
            return None
        try:
            sales_job = db.create_browser_capture_job(db_path, "sales", None)
            inventory_job = db.create_browser_capture_job(db_path, "inventory", None)
            end_date = now.date() - timedelta(days=1)
            start_date = end_date - timedelta(days=13)
            capture = run_browser_worker(
                db_path,
                port=int(config["debug_port"]),
                action="export_product_detail",
                url=report_url,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                brand=settings["brand_name"],
            )
            db.record_browser_status(db_path, capture)
            db.process_browser_capture_jobs(db_path)
            result = db.import_latest_browser_reports(db_path)
            plan_id = int(result["plan_id"])
            if not {sales_job, inventory_job}.issubset(set(result["job_ids"])):
                raise ValueError("本次销售和库存报表未同时导入。")
        except Exception as exc:
            db.record_browser_schedule_issue(
                db_path,
                f"唯品自动更新未完成：{exc}请在数据中心检查并手工更新。",
                session_available=True,
            )
            _record_schedule_key(db_path, schedule_key)
            return None
        _record_schedule_key(db_path, schedule_key)
        return plan_id
    else:
        db.record_test_sync(db_path, source="scheduled_test")
        generation_type = "scheduled_test"
    plan_id = db.generate_plan(db_path, generation_type=generation_type, force=True)
    _record_schedule_key(db_path, schedule_key)
    return plan_id


class SchedulerThread(threading.Thread):
    def __init__(self, db_path: str | Path, interval_seconds: int = 30):
        super().__init__(name="replenishment-scheduler", daemon=True)
        self.db_path = str(db_path)
        self.interval_seconds = interval_seconds
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                db.process_browser_capture_jobs(self.db_path)
                run_due_jobs(self.db_path)
            except Exception as exc:  # The web app remains available if a scheduled run fails.
                print(f"补货定时任务失败: {exc}")
            self.stop_event.wait(self.interval_seconds)

    def stop(self) -> None:
        self.stop_event.set()
