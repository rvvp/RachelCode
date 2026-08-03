from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldDef:
    key: str
    label: str
    excel_header: str
    group: str
    storage_type: str = "TEXT"
    input_type: str = "text"
    visible_to_c: bool = True
    options: tuple[str, ...] = ()
    placeholder: str = ""


PRODUCT_FIELDS: list[FieldDef] = [
    FieldDef("detection_report", "检测报告", "检测报告", "商品基础", visible_to_c=False),
    FieldDef("shipping_warehouse", "发货仓库", "发货仓库", "商品基础", visible_to_c=False),
    FieldDef("brand_name", "品牌名称", "品牌\n名称", "商品基础"),
    FieldDef(
        "season_year",
        "年份季节",
        "年份季节",
        "商品基础",
        placeholder="例如 2026夏",
    ),
    FieldDef("image_url", "图片", "图片", "商品基础", placeholder="建议填图片链接或素材路径"),
    FieldDef("style_color", "款色", "款色", "商品基础"),
    FieldDef("style_code", "款号", "款号", "商品基础"),
    FieldDef("color_name", "颜色名称", "颜色\n名称", "商品基础"),
    FieldDef("product_name", "商品名称", "商品名称", "商品基础"),
    FieldDef("category", "品类", "品类", "商品基础"),
    FieldDef(
        "has_accessories",
        "是否有配饰",
        "是否有配饰",
        "商品基础",
        input_type="select",
        options=("无", "有"),
    ),
    FieldDef("supplier", "供应商", "供应商", "价格与供应", visible_to_c=False),
    FieldDef("tag_price", "吊牌价", "吊牌价", "价格与供应", storage_type="REAL", input_type="number", visible_to_c=False),
    FieldDef("launch_price", "上新价格", "上新价格", "价格与供应", storage_type="REAL", input_type="number", visible_to_c=False),
    FieldDef(
        "launch_channel",
        "上新渠道",
        "上新渠道",
        "价格与供应",
        visible_to_c=False,
        placeholder="例如 直播 / 门店 / 电商",
    ),
    FieldDef("size_range", "尺码段", "尺码段", "尺码与数量"),
    FieldDef("size_f", "F", "F", "尺码与数量", storage_type="INTEGER", input_type="number", visible_to_c=False),
    FieldDef("size_s", "S", "S", "尺码与数量", storage_type="INTEGER", input_type="number", visible_to_c=False),
    FieldDef("size_m", "M", "M", "尺码与数量", storage_type="INTEGER", input_type="number", visible_to_c=False),
    FieldDef("size_l", "L", "L", "尺码与数量", storage_type="INTEGER", input_type="number", visible_to_c=False),
    FieldDef("size_xl", "XL", "XL", "尺码与数量", storage_type="INTEGER", input_type="number", visible_to_c=False),
    FieldDef("size_2xl", "2XL", "2XL", "尺码与数量", storage_type="INTEGER", input_type="number", visible_to_c=False),
    FieldDef("size_3xl", "3XL", "3XL", "尺码与数量", storage_type="INTEGER", input_type="number", visible_to_c=False),
    FieldDef("total_quantity", "合计", "合计", "尺码与数量", storage_type="INTEGER", input_type="number", visible_to_c=False),
    FieldDef("material", "材质", "材质", "材质与合规"),
    FieldDef("composition", "成分", "成分", "材质与合规"),
    FieldDef("washing_method", "洗涤方式", "洗涤方式", "材质与合规"),
    FieldDef("washing_method_en", "洗涤方式（英文）", "洗涤方式 （英文）", "材质与合规"),
    FieldDef("safety_category", "安全技术类别", "安全技术类别", "材质与合规"),
    FieldDef("standard_code", "执行标准", "执行标准", "材质与合规"),
    FieldDef("size_chart", "尺寸表", "尺寸表", "材质与合规", input_type="textarea"),
]

PRODUCT_FIELD_MAP = {field.key: field for field in PRODUCT_FIELDS}
FIELDS_BY_GROUP: dict[str, list[FieldDef]] = {}
for product_field in PRODUCT_FIELDS:
    FIELDS_BY_GROUP.setdefault(product_field.group, []).append(product_field)

C_VISIBLE_FIELDS = [field for field in PRODUCT_FIELDS if field.visible_to_c]

EXCEL_HEADER_LOOKUP = {field.excel_header: field for field in PRODUCT_FIELDS}
EXCEL_HEADERS = [field.excel_header for field in PRODUCT_FIELDS]
