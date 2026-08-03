from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta


RISK_ORDER = {"critical": 0, "warning": 1, "watch": 2, "healthy": 3, "no_sales": 4}


def _as_date(value) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _round_pack(quantity: float, pack_size: int) -> int:
    if quantity <= 0:
        return 0
    pack_size = max(1, int(pack_size or 1))
    return int(math.ceil(quantity / pack_size) * pack_size)


def _stockout_day(sellable: int, inbound: int, inbound_date, daily_demand: float, as_of: date, horizon: int = 90) -> int | None:
    if daily_demand <= 0:
        return None
    balance = float(sellable)
    arrival = _as_date(inbound_date)
    for day_no in range(1, horizon + 1):
        current_day = as_of + timedelta(days=day_no)
        if arrival == current_day:
            balance += inbound
        balance -= daily_demand
        if balance < 0:
            return day_no
    return None


def _risk_for(stockout_day: int | None, coverage_days: float | None) -> str:
    if (stockout_day is not None and stockout_day <= 7) or (coverage_days is not None and coverage_days <= 7):
        return "critical"
    if (stockout_day is not None and stockout_day <= 14) or (coverage_days is not None and coverage_days <= 14):
        return "warning"
    if coverage_days is not None and coverage_days <= 30:
        return "watch"
    if coverage_days is None:
        return "no_sales"
    return "healthy"


def build_replenishment_items(
    skus: list[dict],
    sales_rows: list[dict],
    inventory_by_sku: dict[int, dict],
    *,
    as_of: date,
    target_days: int,
    safety_days: int,
    weight_7: float = 0.6,
    weight_14: float = 0.4,
    min_sales_7: int = 5,
    min_sales_14: int = 10,
    min_consecutive_sales_days: int = 3,
    max_coverage_days: float = 14,
) -> list[dict]:
    """Build explainable size-level suggestions for a frozen replenishment run."""
    sales_by_sku: dict[int, list[dict]] = defaultdict(list)
    for row in sales_rows:
        sales_by_sku[int(row["sku_id"])].append(row)

    style_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for raw_sku in skus:
        sku = dict(raw_sku)
        goods_code = str(sku.get("outer_sku_id") or sku["color_name"])
        style_groups[(sku["style_code"], goods_code)].append(sku)

    results: list[dict] = []
    for style_key, style_skus in style_groups.items():
        sku_stats = []
        style_sales_7 = 0
        style_sales_14 = 0
        goods_sales_by_date: dict[date, int] = defaultdict(int)
        start_7 = as_of - timedelta(days=6)
        start_14 = as_of - timedelta(days=13)
        for sku in style_skus:
            sku_sales_7 = 0
            sku_sales_14 = 0
            for sale in sales_by_sku.get(int(sku["id"]), []):
                sale_date = _as_date(sale.get("sale_date"))
                if not sale_date or sale_date > as_of:
                    continue
                net_units = int(sale.get("net_units") or 0)
                if sale_date >= start_14:
                    sku_sales_14 += net_units
                    goods_sales_by_date[sale_date] += net_units
                if sale_date >= start_7:
                    sku_sales_7 += net_units
            style_sales_7 += sku_sales_7
            style_sales_14 += sku_sales_14
            sku_stats.append({"sku": sku, "sales_7": sku_sales_7, "sales_14": sku_sales_14})

        actual_weight = 0.7 if style_sales_14 >= 30 else 0.5 if style_sales_14 >= 10 else 0.3
        default_total = sum(max(0.0, float(row["sku"].get("default_size_share") or 0)) for row in sku_stats)
        size_count = max(1, len(sku_stats))
        shares = []
        for row in sku_stats:
            actual_share = row["sales_14"] / style_sales_14 if style_sales_14 > 0 else 1 / size_count
            default_raw = max(0.0, float(row["sku"].get("default_size_share") or 0))
            default_share = default_raw / default_total if default_total > 0 else 1 / size_count
            shares.append(actual_share * actual_weight + default_share * (1 - actual_weight))
        share_total = sum(shares) or 1
        shares = [share / share_total for share in shares]

        style_avg_7 = style_sales_7 / 7
        style_avg_14 = style_sales_14 / 14
        style_daily = style_avg_7 * weight_7 + style_avg_14 * weight_14

        goods_inventory_14 = 0
        for row in sku_stats:
            sku = row["sku"]
            inventory = dict(inventory_by_sku.get(int(sku["id"]), {}))
            sellable = max(
                0,
                int(inventory.get("on_hand") or 0)
                - int(inventory.get("locked") or 0)
                - int(inventory.get("defective") or 0),
            )
            inbound = max(0, int(inventory.get("inbound") or 0))
            inbound_date = _as_date(inventory.get("inbound_date"))
            goods_inventory_14 += sellable + (
                inbound if inbound_date and inbound_date <= as_of + timedelta(days=14) else 0
            )
        goods_coverage_days = goods_inventory_14 / style_daily if style_daily > 0 else None
        consecutive_sales_days = 0
        current_streak = 0
        for day_offset in range(13, -1, -1):
            sale_day = as_of - timedelta(days=day_offset)
            if goods_sales_by_date.get(sale_day, 0) > 0:
                current_streak += 1
                consecutive_sales_days = max(consecutive_sales_days, current_streak)
            else:
                current_streak = 0
        condition_1 = style_sales_7 >= min_sales_7 and style_sales_14 >= min_sales_14
        condition_2 = consecutive_sales_days >= min_consecutive_sales_days
        if goods_coverage_days is None or goods_coverage_days > max_coverage_days or not (condition_1 or condition_2):
            continue
        selection_reason = ",".join(
            reason for reason, matched in (("condition_1", condition_1), ("condition_2", condition_2)) if matched
        )

        style_results = []
        for row, size_share in zip(sku_stats, shares):
            sku = row["sku"]
            inventory = dict(inventory_by_sku.get(int(sku["id"]), {}))
            on_hand = int(inventory.get("on_hand") or 0)
            locked = int(inventory.get("locked") or 0)
            defective = int(inventory.get("defective") or 0)
            sellable = max(0, on_hand - locked - defective)
            inbound = max(0, int(inventory.get("inbound") or 0))
            inbound_date = _as_date(inventory.get("inbound_date"))
            inbound_14 = inbound if inbound_date and inbound_date <= as_of + timedelta(days=14) else 0
            inbound_target = inbound if inbound_date and inbound_date <= as_of + timedelta(days=target_days) else 0
            demand_factor = max(0.0, float(sku.get("demand_factor") or 1))
            daily_demand = style_daily * size_share * demand_factor
            inventory_14 = sellable + inbound_14
            projected_14 = inventory_14 - daily_demand * 14
            coverage_days = inventory_14 / daily_demand if daily_demand > 0 else None
            stockout_day = _stockout_day(sellable, inbound, inbound_date, daily_demand, as_of)
            risk = _risk_for(stockout_day, coverage_days)
            target_stock = daily_demand * (target_days + safety_days)
            raw_suggestion = max(0.0, target_stock - sellable - inbound_target)
            suggested_qty = _round_pack(raw_suggestion, int(sku.get("pack_size") or 1))
            style_results.append(
                {
                    "sku_id": int(sku["id"]),
                    "style_code": style_key[0],
                    "outer_sku_id": style_key[1],
                    "style_name": sku.get("style_name") or "",
                    "color_name": sku.get("color_name") or "",
                    "size_name": sku.get("size_name") or "",
                    "category": sku.get("category") or "",
                    "supplier": sku.get("supplier") or "",
                    "core_size": int(sku.get("core_size") or 0),
                    "sales_7": row["sales_7"],
                    "sales_14": row["sales_14"],
                    "consecutive_sales_days": consecutive_sales_days,
                    "selection_reason": selection_reason,
                    "avg_7": row["sales_7"] / 7,
                    "avg_14": row["sales_14"] / 14,
                    "size_share": size_share,
                    "daily_demand": daily_demand,
                    "sellable": sellable,
                    "inbound": inbound,
                    "inbound_date": inbound_date.isoformat() if inbound_date else "",
                    "projected_14": projected_14,
                    "coverage_days": coverage_days,
                    "stockout_day": stockout_day,
                    "risk_level": risk,
                    "broken_core": 1 if int(sku.get("core_size") or 0) and stockout_day is not None and stockout_day <= 14 else 0,
                    "suggested_qty": suggested_qty,
                    "confirmed_qty": suggested_qty,
                    "pack_size": max(1, int(sku.get("pack_size") or 1)),
                    "moq": max(0, int(sku.get("moq") or 0)),
                }
            )

        style_moq = max((item["moq"] for item in style_results), default=0)
        suggested_total = sum(item["suggested_qty"] for item in style_results)
        if 0 < suggested_total < style_moq:
            shortfall = style_moq - suggested_total
            ranked = sorted(style_results, key=lambda item: (-item["size_share"], -item["core_size"], item["size_name"]))
            index = 0
            while shortfall > 0 and ranked:
                item = ranked[index % len(ranked)]
                item["suggested_qty"] += item["pack_size"]
                item["confirmed_qty"] = item["suggested_qty"]
                shortfall -= item["pack_size"]
                index += 1
        results.extend(style_results)

    return sorted(
        results,
        key=lambda row: (
            RISK_ORDER.get(row["risk_level"], 9),
            row["style_code"],
            row["outer_sku_id"],
            row["color_name"],
            row["size_name"],
        ),
    )
