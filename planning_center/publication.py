from __future__ import annotations

from pathlib import Path

from planning_center import db


def current_style_publication_records(db_path: str | Path, style_code: str) -> tuple[list[dict], int]:
    """Read the current workbench's colors and latest pricing in one snapshot.

    Withdrawn colors retain their pricing history but cannot participate in a
    new publication. A LEFT JOIN keeps active colors without pricing visible
    to the completeness check rather than silently dropping them.
    """
    with db.get_connection(db_path) as connection:
        rows = connection.execute(
            """
            SELECT pr.*
            FROM source_products sp
            LEFT JOIN pricing_records pr ON pr.id = (
                SELECT candidate.id
                FROM pricing_records candidate
                WHERE candidate.source_product_id = sp.id
                ORDER BY candidate.created_at DESC, candidate.id DESC
                LIMIT 1
            )
            WHERE TRIM(sp.style_code) = ?
              AND sp.status = 'pending'
              AND sp.lifecycle_status = 'active'
            ORDER BY sp.id
            """,
            (str(style_code or "").strip(),),
        ).fetchall()
    return [dict(row) for row in rows if row["id"] is not None], len(rows)
