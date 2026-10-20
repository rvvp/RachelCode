"""Read-only rotation evidence. Does not mutate product workflows or receipts."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict

from catalog_backend.db import get_connection


def catalog_page(db_path, after=0, limit=500):
    limit = min(500, max(1, int(limit)))
    with get_connection(db_path) as connection:
        connection.execute("BEGIN")
        # All normal product writes append a log/version. Receipts and recall
        # notices are append-only evidence. Check the same revision on EVERY
        # page so concurrent work cannot create a mixed-round local snapshot.
        revision = [list(connection.execute(sql).fetchone()) for sql in (
            "SELECT COUNT(*),MAX(id),MAX(updated_at),SUM(current_version_no),SUM(c_release_no) FROM products",
            "SELECT MAX(id) FROM product_logs",
            "SELECT COUNT(*),MAX(rowid) FROM product_c_receipts",
            "SELECT COUNT(*),MAX(id),MAX(recalled_at) FROM product_c_recall_notices",
        )]
        revision = hashlib.sha256(json.dumps(revision).encode()).hexdigest()
        rows = connection.execute(
            "SELECT id, style_code, style_color, product_name, season_year, supplier, category, launch_channel, status, lifecycle_status, product_line, c_release_no, workflow_restart_required, current_version_no, updated_at FROM products WHERE id > ? ORDER BY id LIMIT ?",
            (int(after), limit),
        ).fetchall()
        if not rows:
            return {"schema_version": 1, "revision": revision, "items": [], "next_after": int(after), "has_more": False}
        ids = [int(row["id"]) for row in rows]
        marks = ",".join("?" for _ in ids)
        receipts = defaultdict(list)
        for row in connection.execute(
            f"SELECT product_id, recipient_platform, release_no, received_at FROM product_c_receipts WHERE product_id IN ({marks}) AND recipient_platform IN ('tmall','vip') ORDER BY product_id,release_no,recipient_platform,recipient_user_id", ids
        ):
            receipts[int(row["product_id"])].append(dict(row))
        recalls = {}
        for row in connection.execute(
            f"SELECT product_id, MAX(recalled_at) AS stamp FROM product_c_recall_notices WHERE product_id IN ({marks}) GROUP BY product_id", ids
        ):
            recalls[int(row["product_id"])] = row["stamp"]
        # A recall before anyone received the old release has no notice.
        # Recover that transition from the permanent status audit trail.
        last_status = {}
        for row in connection.execute(
            f"SELECT product_id, action, created_at FROM product_logs WHERE product_id IN ({marks}) AND action LIKE 'status:%' ORDER BY id", ids
        ):
            pid = int(row["product_id"])
            status = row["action"].split(":", 1)[1]
            if status in {"pending", "draft"} and last_status.get(pid) in {"published", "received"}:
                recalls[pid] = max(recalls.get(pid, ""), row["created_at"])
            last_status[pid] = status
        items = []
        for row in rows:
            item = dict(row)
            item["recall_at"] = recalls.get(item["id"], "")
            item["receipts"] = receipts[item["id"]]
            items.append(item)
        return {"schema_version": 1, "revision": revision, "items": items, "next_after": ids[-1], "has_more": len(rows) == limit}
