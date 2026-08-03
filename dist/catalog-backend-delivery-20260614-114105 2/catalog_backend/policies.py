from __future__ import annotations

from catalog_backend.fields import C_VISIBLE_FIELDS, PRODUCT_FIELDS, PRODUCT_FIELD_MAP


DEPARTMENT_LABELS = {
    "A": "A 部门",
    "B": "B 部门",
    "C": "C 部门",
    "ADMIN": "审核管理员",
}

EDITOR_DEPARTMENTS = {"A", "B"}
ADMIN_DEPARTMENTS = {"ADMIN"}
MANAGEABLE_DEPARTMENTS = ("A", "B", "C", "ADMIN")

STATUS_LABELS = {
    "draft": "草稿",
    "pending": "待审核",
    "published": "已发布",
}

LIFECYCLE_LABELS = {
    "active": "正常",
    "archived": "已归档",
    "deleted": "已删除",
}


def department_label(department: str) -> str:
    return DEPARTMENT_LABELS.get(department, department or "未分配")


def can_create_product(user: dict | None) -> bool:
    return bool(user and user.get("department") in EDITOR_DEPARTMENTS)


def is_admin(user: dict | None) -> bool:
    return bool(user and user.get("department") in ADMIN_DEPARTMENTS)


def can_manage_users(user: dict | None) -> bool:
    return is_admin(user)


def can_edit_product(user: dict | None, product: dict | None) -> bool:
    if not user or not product:
        return False
    return can_create_product(user) and user.get("id") == product.get("created_by")


def can_review_product(user: dict | None, product: dict | None) -> bool:
    return bool(user and product and is_admin(user))


def can_view_logs(user: dict | None) -> bool:
    return bool(user and (user.get("department") in EDITOR_DEPARTMENTS or is_admin(user)))


def can_see_product(user: dict | None, product: dict | None) -> bool:
    if not user or not product:
        return False
    if product.get("lifecycle_status") == "deleted" and not is_admin(user):
        return False
    if is_admin(user):
        return True
    status = product.get("status")
    if product.get("lifecycle_status") == "archived":
        return user.get("id") == product.get("created_by")
    if user.get("department") == "C":
        return status == "published"
    return True


def available_status_actions(user: dict | None, product: dict | None) -> list[tuple[str, str]]:
    if not user or not product:
        return []
    if product.get("lifecycle_status") != "active":
        return []
    status = product.get("status") or "draft"
    if can_edit_product(user, product):
        actions = []
        if status == "draft":
            actions.append(("pending", "提交审核"))
        if status == "pending":
            actions.append(("draft", "撤回为草稿"))
        if status == "published":
            actions.append(("draft", "修改后转回草稿"))
        return actions
    if can_review_product(user, product):
        actions = []
        if status == "pending":
            actions.append(("published", "审核发布"))
            actions.append(("draft", "驳回为草稿"))
        if status == "published":
            actions.append(("draft", "下线转草稿"))
        return actions
    return []


def status_label(status: str | None) -> str:
    return STATUS_LABELS.get(status or "", status or "未设置")


def lifecycle_label(status: str | None) -> str:
    return LIFECYCLE_LABELS.get(status or "", status or "未设置")


def can_manage_lifecycle(user: dict | None, product: dict | None) -> bool:
    return bool(user and product and (can_edit_product(user, product) or is_admin(user)))


def available_lifecycle_actions(user: dict | None, product: dict | None) -> list[tuple[str, str]]:
    if not can_manage_lifecycle(user, product):
        return []
    lifecycle = product.get("lifecycle_status") or "active"
    if lifecycle == "active":
        return [("archived", "归档资料"), ("deleted", "删除资料")]
    if lifecycle == "archived":
        return [("active", "恢复为正常"), ("deleted", "删除资料")]
    if lifecycle == "deleted" and is_admin(user):
        return [("active", "恢复已删除资料"), ("archived", "恢复为归档")]
    return []


def visible_fields_for_department(department: str | None):
    if department in EDITOR_DEPARTMENTS or department in ADMIN_DEPARTMENTS:
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
        "last_reviewed_at": product.get("last_reviewed_at"),
        "reviewer_name": product.get("reviewer_name"),
    }
    for field in visible_fields_for_department(department):
        payload[field.key] = product.get(field.key)
    return payload
