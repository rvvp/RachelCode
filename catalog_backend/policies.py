from __future__ import annotations

import json

from catalog_backend.fields import C_VISIBLE_FIELDS, PRODUCT_FIELDS, PRODUCT_FIELD_MAP


DEPARTMENT_LABELS = {
    "A": "跟单部",
    "B": "商品部",
    "C": "运营部",
    "EXECUTIVE": "总经办",
    "ADMIN": "系统管理员",
}

EDITOR_DEPARTMENTS = {"A", "B"}
EXECUTIVE_READ_ONLY_DEPARTMENTS = {"EXECUTIVE"}
ADMIN_DEPARTMENTS = {"ADMIN"}
MANAGEABLE_DEPARTMENTS = ("A", "B", "C", "EXECUTIVE", "ADMIN")
# These fields are completed across the two applications.  The planning center
# owns the three planning outputs; the catalog remains the source of truth for
# the image and the completion flag.
B_STAGE_FIELD_KEYS = {"category", "image_url", "launch_price", "launch_channel", "completion_flag"}
B_PLANNING_MANAGED_FIELD_KEYS = {"category", "launch_price", "launch_channel"}
A_STAGE_FIELD_KEYS = tuple(field.key for field in PRODUCT_FIELDS if field.key not in B_STAGE_FIELD_KEYS)
COLLABORATION_START_FIELD_KEYS = (
    "brand_name",
    "season_year",
    "style_code",
    "style_color",
    "product_name",
    "tax_included_price",
)
WORKFLOW_RESTART_FIELD_KEYS = frozenset(
    {
        "style_code",
        "style_color",
        "product_name",
        "color_name",
        "tag_price",
        "launch_price",
        "launch_channel",
        "material",
        "composition_en",
        "washing_method",
        "safety_category",
        "standard_code",
    }
)
C_OPERATING_CHANNELS = {
    "tmall": "天猫类",
    "vip": "唯品类",
}
BILLING_PLATFORM_OPTIONS = (
    ("tmall", "天猫"),
    ("douyin", "抖音"),
    ("jd", "京东"),
    ("vip", "唯品"),
    ("miniprogram", "小程序"),
)
BILLING_PLATFORM_LABELS = dict(BILLING_PLATFORM_OPTIONS)
LAUNCH_CHANNEL_OPTIONS = ("天猫", "唯品", "同款")
LAUNCH_CHANNEL_ALIASES = {
    "天猫/京东/抖音": "天猫",
    "天猫、京东、抖音": "天猫",
    "天猫,京东,抖音": "天猫",
    "天猫 京东 抖音": "天猫",
}


def is_department_monitor(user: dict | None) -> bool:
    """True when an administrator is viewing a department's read-only workspace."""
    return bool(user and user.get("monitor_department") in {"A", "B", "C"})

STATUS_LABELS = {
    "draft": "跟单整理中",
    "pending": "A/B协作中",
    "published": "待运营接收",
    "received": "已接收",
}

LIFECYCLE_LABELS = {
    "active": "正常",
    "archived": "已归档",
    "deleted": "已删除",
}


def department_label(department: str) -> str:
    return DEPARTMENT_LABELS.get(department, department or "未分配")


def operating_channel_label(channel: str | None) -> str:
    return C_OPERATING_CHANNELS.get(str(channel or "").strip(), "未分配")


def normalize_launch_channel(value) -> str:
    clean_value = "" if value is None else str(value).strip()
    clean_value = clean_value.replace("／", "/").replace("，", ",")
    if clean_value in LAUNCH_CHANNEL_OPTIONS:
        return clean_value
    return LAUNCH_CHANNEL_ALIASES.get(clean_value, "")


def c_user_can_see_launch_channel(user: dict | None, launch_channel) -> bool:
    if not user or user.get("department") != "C":
        return False
    operating_channel = str(user.get("operating_channel") or "").strip()
    normalized_channel = normalize_launch_channel(launch_channel)
    if operating_channel not in C_OPERATING_CHANNELS or not normalized_channel:
        if operating_channel == "all":
            return True
        return False
    if normalized_channel == "同款":
        return True
    return (normalized_channel == "天猫" and operating_channel == "tmall") or (
        normalized_channel == "唯品" and operating_channel == "vip"
    )


def c_visible_launch_channels(user: dict | None) -> tuple[str, ...]:
    if not user or user.get("department") != "C":
        return ()
    if user.get("operating_channel") == "tmall":
        return ("天猫", "同款")
    if user.get("operating_channel") == "vip":
        return ("唯品", "同款")
    return ()


def normalize_billing_platform_codes(value) -> tuple[str, ...]:
    raw_values = value
    if isinstance(value, str):
        try:
            raw_values = json.loads(value)
        except json.JSONDecodeError:
            raw_values = value.split(",")
    if not isinstance(raw_values, (list, tuple, set, frozenset)):
        raw_values = ()
    selected = {str(code or "").strip().lower() for code in raw_values}
    return tuple(code for code, _ in BILLING_PLATFORM_OPTIONS if code in selected)


def billing_platform_label(platform_code: str | None) -> str:
    return BILLING_PLATFORM_LABELS.get(str(platform_code or "").strip(), "未知平台")


def c_user_can_manage_platform_bill(user: dict | None, platform_code: str | None) -> bool:
    if not user or user.get("department") != "C":
        return False
    return str(platform_code or "").strip() in normalize_billing_platform_codes(
        user.get("billing_platforms_json")
    )


def platform_bill_platform_codes_for_user(user: dict | None, platform_codes) -> tuple[str, ...]:
    configured_codes = tuple(str(code or "").strip() for code in platform_codes if str(code or "").strip())
    if not user:
        return ()
    if user.get("department") == "C":
        return tuple(code for code in configured_codes if c_user_can_manage_platform_bill(user, code))
    if user.get("department") in {"B", "ADMIN"}:
        return configured_codes
    return ()


def can_create_product(user: dict | None) -> bool:
    return bool(user and not is_department_monitor(user) and user.get("department") == "A")


def can_import_product_excel(user: dict | None) -> bool:
    return bool(user and not is_department_monitor(user) and user.get("department") in {"A", "B"})


def can_import_product_images(user: dict | None) -> bool:
    return bool(user and not is_department_monitor(user) and user.get("department") == "B")


def is_admin(user: dict | None) -> bool:
    return bool(user and user.get("department") in ADMIN_DEPARTMENTS)


def is_executive_read_only(user: dict | None) -> bool:
    return bool(user and user.get("department") in EXECUTIVE_READ_ONLY_DEPARTMENTS)


def can_manage_users(user: dict | None) -> bool:
    return bool(not is_department_monitor(user) and is_admin(user))


def can_edit_product(user: dict | None, product: dict | None) -> bool:
    return bool(not is_department_monitor(user) and editable_field_keys_for_user(user, product))


def can_review_product(user: dict | None, product: dict | None) -> bool:
    return bool(user and product and not is_department_monitor(user) and is_admin(user))


def can_view_logs(user: dict | None) -> bool:
    return bool(
        user
        and (
            user.get("department") in EDITOR_DEPARTMENTS
            or is_executive_read_only(user)
            or is_admin(user)
        )
    )


def can_view_tax_included_price_history(user: dict | None) -> bool:
    """Price history follows the existing non-C catalog visibility boundary."""
    return bool(
        user
        and user.get("department") in (EDITOR_DEPARTMENTS | EXECUTIVE_READ_ONLY_DEPARTMENTS | ADMIN_DEPARTMENTS)
    )


def can_access_billing_module(user: dict | None) -> bool:
    return bool(
        user
        and user.get("department") in ({"A", "B", "C"} | EXECUTIVE_READ_ONLY_DEPARTMENTS | ADMIN_DEPARTMENTS)
    )


def can_upload_platform_bills(user: dict | None) -> bool:
    return bool(user and not is_department_monitor(user) and user.get("department") == "C")


def can_access_platform_bills(user: dict | None) -> bool:
    return bool(user and (user.get("department") in {"B", "C"} or is_admin(user)))


def can_process_brand_bills(user: dict | None) -> bool:
    return bool(user and not is_department_monitor(user) and (user.get("department") == "B" or is_admin(user)))


def can_access_brand_bills(user: dict | None) -> bool:
    return bool(
        user
        and (user.get("department") in {"A", "B"} | EXECUTIVE_READ_ONLY_DEPARTMENTS or is_admin(user))
    )


def can_manage_supplier_settlements(user: dict | None) -> bool:
    return bool(user and not is_department_monitor(user) and (user.get("department") == "A" or is_admin(user)))


def can_access_supplier_settlements(user: dict | None) -> bool:
    return bool(
        user
        and (user.get("department") in {"A"} | EXECUTIVE_READ_ONLY_DEPARTMENTS or is_admin(user))
    )


def can_see_product(user: dict | None, product: dict | None) -> bool:
    if not user or not product:
        return False
    if product.get("lifecycle_status") == "deleted" and not is_admin(user):
        return False
    if is_department_monitor(user):
        if user.get("department") == "C":
            return product.get("status") in {"published", "received"}
        return True
    if is_admin(user):
        return True
    status = product.get("status")
    if product.get("lifecycle_status") == "archived":
        return user.get("id") == product.get("created_by")
    if user.get("department") == "C":
        return status in {"published", "received"} and c_user_can_see_launch_channel(user, product.get("launch_channel"))
    return True


def editable_field_keys_for_user(user: dict | None, product: dict | None = None) -> tuple[str, ...]:
    if not user or is_department_monitor(user):
        return ()
    department = user.get("department")
    if department == "A":
        if product is None:
            return A_STAGE_FIELD_KEYS
        if product.get("lifecycle_status") != "active":
            return ()
        if user.get("id") != product.get("created_by"):
            return ()
        if product.get("status") in {"draft", "pending", "published", "received"}:
            return A_STAGE_FIELD_KEYS
        return ()
    if department == "B":
        if not product or product.get("lifecycle_status") != "active":
            return ()
        if product.get("status") in {"pending", "published"}:
            return ("image_url", "completion_flag")
        if product.get("status") == "received":
            return ("image_url", "completion_flag")
        return ()
    return ()


def available_status_actions(user: dict | None, product: dict | None) -> list[tuple[str, str]]:
    if not user or not product or is_department_monitor(user):
        return []
    if product.get("lifecycle_status") != "active":
        return []
    status = product.get("status") or "draft"
    if user.get("department") == "A" and user.get("id") == product.get("created_by"):
        actions = []
        if status == "draft":
            actions.append(("pending", "开启商品部协作"))
        return actions
    if user.get("department") == "B":
        actions = []
        if status == "pending":
            actions.append(("published", "确认资料齐全，提交运营部"))
            actions.append(("draft", "退回跟单部补充"))
        if status in {"published", "received"} and int(product.get("workflow_restart_required") or 0):
            actions.append(("published", "重新提交给运营部"))
        return actions
    if user.get("department") == "C":
        if status in {"published", "received"} and not int(product.get("workflow_restart_required") or 0):
            return [("received", "接收资料")]
        return []
    if can_review_product(user, product):
        actions = []
        if status in {"published", "received"} and int(product.get("workflow_restart_required") or 0):
            actions.append(("published", "管理员代为重新提交运营部"))
        if status == "pending":
            actions.append(("published", "管理员代为完成"))
            actions.append(("draft", "管理员退回跟单部"))
        if status == "published":
            actions.append(("draft", "管理员转回跟单部修改"))
            actions.append(("pending", "管理员转回商品部补充"))
        if status == "received":
            actions.append(("draft", "管理员转回跟单部修改"))
            actions.append(("pending", "管理员转回商品部补充"))
        if status == "draft":
            actions.append(("pending", "管理员转交商品部填写"))
        return actions
    return []


def status_label(status: str | None) -> str:
    return STATUS_LABELS.get(status or "", status or "未设置")


def lifecycle_label(status: str | None) -> str:
    return LIFECYCLE_LABELS.get(status or "", status or "未设置")


def can_manage_lifecycle(user: dict | None, product: dict | None) -> bool:
    return bool(user and product and not is_department_monitor(user) and (can_edit_product(user, product) or is_admin(user)))


def can_delete_product(user: dict | None, product: dict | None) -> bool:
    """Only the originating A user (or an administrator) can delete a record."""
    if not user or not product or is_department_monitor(user):
        return False
    if is_admin(user):
        return True
    return bool(
        user.get("department") == "A"
        and user.get("id") == product.get("created_by")
    )


def available_lifecycle_actions(user: dict | None, product: dict | None) -> list[tuple[str, str]]:
    if not can_manage_lifecycle(user, product):
        return []
    lifecycle = product.get("lifecycle_status") or "active"
    if lifecycle == "active":
        actions = [("archived", "归档资料")]
        if can_delete_product(user, product):
            actions.append(("deleted", "删除资料"))
        return actions
    if lifecycle == "archived":
        actions = [("active", "恢复为正常")]
        if can_delete_product(user, product):
            actions.append(("deleted", "删除资料"))
        return actions
    if lifecycle == "deleted" and is_admin(user):
        return [("active", "恢复已删除资料"), ("archived", "恢复为归档")]
    return []


def visible_fields_for_department(department: str | None):
    if department in EDITOR_DEPARTMENTS or department in EXECUTIVE_READ_ONLY_DEPARTMENTS or department in ADMIN_DEPARTMENTS:
        return PRODUCT_FIELDS
    return C_VISIBLE_FIELDS


def visible_fields_from_keys(keys: list[str] | tuple[str, ...]):
    return [PRODUCT_FIELD_MAP[key] for key in keys if key in PRODUCT_FIELD_MAP]


def product_payload_for_department(product: dict, department: str | None) -> dict:
    payload = {
        "id": product.get("id"),
        "owner_department": product.get("owner_department"),
        "creator_name": product.get("creator_name"),
        "creator_username": product.get("creator_username"),
        "created_at": product.get("created_at"),
        "updated_at": product.get("updated_at"),
        "status": product.get("status"),
        "status_label": status_label(product.get("status")),
        "lifecycle_status": product.get("lifecycle_status"),
        "lifecycle_label": lifecycle_label(product.get("lifecycle_status")),
        "revision_flag": int(product.get("revision_flag") or 0),
        "last_reviewed_at": product.get("last_reviewed_at"),
        "reviewer_name": product.get("reviewer_name"),
    }
    for field in visible_fields_for_department(department):
        payload[field.key] = product.get(field.key)
    return payload
