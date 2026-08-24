from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from catalog_backend import CatalogApplication, init_db as init_catalog_db
from catalog_backend import db as catalog_db
from planning_center import PlanningApplication
from planning_center import db as planning_db


class PlanningCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.catalog_db_path = root / "catalog.db"
        self.planning_db_path = root / "planning.db"
        init_catalog_db(self.catalog_db_path)
        planning_db.init_db(self.planning_db_path)

    def tearDown(self):
        self.temp.cleanup()

    def wsgi_request(self, app, path, method="GET", body=b"", content_type="application/x-www-form-urlencoded", cookie="", authorization=""):
        environ = {}
        setup_testing_defaults(environ)
        if "?" in path:
            path, environ["QUERY_STRING"] = path.split("?", 1)
        environ.update({"PATH_INFO": path, "REQUEST_METHOD": method, "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": content_type, "wsgi.input": io.BytesIO(body)})
        if cookie:
            environ["HTTP_COOKIE"] = cookie
        if authorization:
            environ["HTTP_AUTHORIZATION"] = authorization
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = headers

        captured["body"] = b"".join(app(environ, start_response))
        return captured

    def login_cookie(self, app, username):
        response = self.wsgi_request(app, "/login", method="POST", body=urlencode({"username": username, "password": "demo123"}).encode())
        return dict(response["headers"])["Set-Cookie"].split(";", 1)[0]

    def test_internal_api_requires_token_and_publishes_with_version_check(self):
        app = CatalogApplication(self.catalog_db_path, Path(self.temp.name) / "uploads", planning_api_token="planning-secret")
        response = self.wsgi_request(app, "/api/internal/planning/products")
        self.assertTrue(response["status"].startswith("401"))
        response = self.wsgi_request(app, "/api/internal/planning/products", authorization="Bearer planning-secret")
        payload = json.loads(response["body"])
        self.assertEqual(payload["count"], 2)
        source = next(item for item in payload["items"] if item["actual_cost"])
        publication = {
            "publication_id": "PC-TEST-001",
            "source_version_no": source["source_version_no"],
            "category": "连衣裙",
            "launch_price": 599,
            "fixed_multiplier": 4,
            "supplier_coefficient": 1,
            "raw_price": 600,
            "operator_name": "测试企划员",
        }
        fractional = self.wsgi_request(
            app,
            f"/api/internal/planning/products/{source['id']}/price-publication",
            method="POST",
            body=json.dumps(dict(publication, publication_id="PC-TEST-FRACTION", launch_price=599.5)).encode(),
            content_type="application/json",
            authorization="Bearer planning-secret",
        )
        self.assertTrue(fractional["status"].startswith("400"))
        self.assertIn("必须是大于 0 的整数", fractional["body"].decode("utf-8"))
        response = self.wsgi_request(
            app,
            f"/api/internal/planning/products/{source['id']}/price-publication",
            method="POST",
            body=json.dumps(publication).encode(),
            content_type="application/json",
            authorization="Bearer planning-secret",
        )
        self.assertTrue(response["status"].startswith("200"))
        self.assertEqual(catalog_db.get_product(self.catalog_db_path, source["id"])["launch_price"], 599)
        retry = self.wsgi_request(
            app,
            f"/api/internal/planning/products/{source['id']}/price-publication",
            method="POST",
            body=json.dumps(publication).encode(),
            content_type="application/json",
            authorization="Bearer planning-secret",
        )
        self.assertEqual(json.loads(retry["body"])["status"], "already_published")
        stale = dict(publication, publication_id="PC-TEST-002")
        response = self.wsgi_request(
            app,
            f"/api/internal/planning/products/{source['id']}/price-publication",
            method="POST",
            body=json.dumps(stale).encode(),
            content_type="application/json",
            authorization="Bearer planning-secret",
        )
        self.assertTrue(response["status"].startswith("409"))

    def test_pricing_formula_rounds_down_to_9_and_snapshots_rules(self):
        planning_db.save_category_cost_rule(self.planning_db_path, "2026秋", None, 600, 4)
        product = {
            "id": 7, "style_code": "M001", "product_name": "测试毛衣", "season_year": "2026秋",
            "supplier": "供应商 A", "category": "毛衣", "actual_cost": 150, "source_version_no": 3,
        }
        record = planning_db.create_pricing_record(self.planning_db_path, product, "测试企划员")
        self.assertEqual(record["raw_price"], 600)
        self.assertEqual(record["launch_price"], 599)
        self.assertEqual(record["fixed_multiplier"], 4)

    def test_missing_category_rule_blocks_pricing(self):
        product = {
            "id": 8, "style_code": "M008", "product_name": "未配置品类", "season_year": "2026秋",
            "supplier": "供应商 A", "category": "外套", "actual_cost": 150, "source_version_no": 1,
        }
        with self.assertRaisesRegex(ValueError, "尚未落入成本区间倍率规则"):
            planning_db.create_pricing_record(self.planning_db_path, product, "测试企划员")

    def test_idempotent_publication_is_marked_published(self):
        planning_db.save_category_cost_rule(self.planning_db_path, "2026秋", None, 600, 4)
        product = {
            "id": 10, "style_code": "M010", "product_name": "幂等测试", "season_year": "2026秋",
            "supplier": "供应商 A", "category": "毛衣", "actual_cost": 150, "source_version_no": 1,
        }
        record = planning_db.create_pricing_record(self.planning_db_path, product, "测试企划员")
        updated = planning_db.mark_record_published(
            self.planning_db_path,
            record["id"],
            {"status": "already_published"},
        )
        self.assertEqual(updated["status"], "published")

    def test_catalog_launch_price_input_and_validation_require_integer(self):
        app = CatalogApplication(self.catalog_db_path, Path(self.temp.name) / "uploads")
        markup = app.render_input(catalog_db.PRODUCT_FIELD_MAP["launch_price"], {"launch_price": 2539.0})
        self.assertIn('min="1" step="1" inputmode="numeric"', markup)
        self.assertIn('value="2539"', markup)
        self.assertNotIn('value="2539.0"', markup)

        valid_errors = app.validate_product_form(
            {"product_name": "整数价格测试", "style_code": "INTEGER-PRICE", "launch_price": "2539"}
        )
        self.assertEqual(valid_errors, [])
        fractional_errors = app.validate_product_form(
            {"product_name": "小数价格测试", "style_code": "FRACTION-PRICE", "launch_price": "2539.5"}
        )
        self.assertIn("上新价格必须是大于 0 的整数，不保留小数位。", fractional_errors)

    def test_dress_uses_fixed_multiplier_regardless_of_cost(self):
        planning_db.save_category_rule(self.planning_db_path, "", "连衣裙", 4.2)
        for product_id, cost in ((20, 150), (21, 900)):
            product = {
                "id": product_id,
                "style_code": f"D{product_id}",
                "product_name": "测试连衣裙",
                "season_year": "2026秋",
                "supplier": "供应商 A",
                "category": "连衣裙",
                "actual_cost": cost,
                "source_version_no": 1,
            }
            record = planning_db.create_pricing_record(self.planning_db_path, product, "测试企划员")
            self.assertEqual(record["fixed_multiplier"], 4.2)
            self.assertEqual(record["raw_price"], cost * 4.2)

    def test_other_category_cost_ranges_use_inclusive_lower_exclusive_upper(self):
        planning_db.save_category_cost_rule(self.planning_db_path, "", None, 600, 4)
        planning_db.save_category_cost_rule(self.planning_db_path, "", 600, 791, 3.9)
        planning_db.save_category_cost_rule(self.planning_db_path, "", 791, 1001, 3.8)
        planning_db.save_category_cost_rule(self.planning_db_path, "", 1001, None, 3.7)

        cases = ((599.99, 4), (600, 3.9), (790, 3.9), (791, 3.8), (1000, 3.8), (1001, 3.7))
        for cost, expected in cases:
            fixed, coefficient = planning_db.resolve_rules(
                self.planning_db_path,
                "2026秋",
                "毛衣",
                "供应商 A",
                cost,
            )
            self.assertEqual(fixed, expected)
            self.assertEqual(coefficient, 1)

    def test_other_category_cost_ranges_reject_overlap_and_allow_season_override(self):
        planning_db.save_category_cost_rule(self.planning_db_path, "", None, 600, 4)
        with self.assertRaisesRegex(ValueError, "区间.*重叠"):
            planning_db.save_category_cost_rule(self.planning_db_path, "", 500, 700, 3.9)

        planning_db.save_category_cost_rule(self.planning_db_path, "2026秋", None, 600, 4.1)
        fixed, _ = planning_db.resolve_rules(
            self.planning_db_path,
            "2026秋",
            "外套",
            "供应商 A",
            500,
        )
        self.assertEqual(fixed, 4.1)

    def test_all_pricing_rule_types_can_be_edited_and_deleted(self):
        planning_db.save_category_rule(self.planning_db_path, "", "连衣裙", 4.2, "默认")
        dress = planning_db.list_category_rules(self.planning_db_path)[0]
        planning_db.save_category_rule(
            self.planning_db_path,
            "2026秋冬",
            "连衣裙",
            4.3,
            "秋冬调整",
            dress["id"],
        )
        edited_dress = planning_db.list_category_rules(self.planning_db_path)[0]
        self.assertEqual(edited_dress["season_year"], "2026秋冬")
        self.assertEqual(edited_dress["multiplier"], 4.3)

        planning_db.save_category_cost_rule(self.planning_db_path, "", None, 600, 4, "首档")
        cost_rule = planning_db.list_category_cost_rules(self.planning_db_path)[0]
        planning_db.save_category_cost_rule(
            self.planning_db_path,
            "",
            None,
            650,
            4.1,
            "调整首档",
            cost_rule["id"],
        )
        fixed, _ = planning_db.resolve_rules(self.planning_db_path, "2026秋冬", "毛衣", "供应商 A", 620)
        self.assertEqual(fixed, 4.1)

        planning_db.save_supplier_coefficient(self.planning_db_path, "", "供应商 A", 1.0, "默认")
        supplier_rule = planning_db.list_supplier_coefficients(self.planning_db_path)[0]
        planning_db.save_supplier_coefficient(
            self.planning_db_path,
            "2026秋冬",
            "供应商 A",
            1.05,
            "秋冬调整",
            supplier_rule["id"],
        )
        _, coefficient = planning_db.resolve_rules(self.planning_db_path, "2026秋冬", "连衣裙", "供应商 A", 300)
        self.assertEqual(coefficient, 1.05)

        planning_db.delete_category_rule(self.planning_db_path, dress["id"])
        planning_db.delete_category_cost_rule(self.planning_db_path, cost_rule["id"])
        planning_db.delete_supplier_coefficient(self.planning_db_path, supplier_rule["id"])
        self.assertEqual(planning_db.list_category_rules(self.planning_db_path), [])
        self.assertEqual(planning_db.list_category_cost_rules(self.planning_db_path), [])
        self.assertEqual(planning_db.list_supplier_coefficients(self.planning_db_path), [])

    def test_rule_page_explains_category_and_cost_logic(self):
        app = PlanningApplication(self.planning_db_path, "http://catalog.test")
        login = self.wsgi_request(
            app,
            "/login",
            method="POST",
            body=urlencode({"username": "planning_admin", "password": "demo123"}).encode(),
        )
        cookie = dict(login["headers"])["Set-Cookie"].split(";", 1)[0]
        response = self.wsgi_request(app, "/rules", cookie=cookie)
        page = response["body"].decode("utf-8")
        self.assertIn("连衣裙固定倍率", page)
        self.assertIn("其他品类成本区间倍率", page)
        self.assertIn("下限包含，上限不包含", page)
        self.assertIn("可按实际业务新增任意数量的成本区间", page)
        self.assertIn("规则维护账号", page)

    def test_only_admin_can_maintain_pricing_rules(self):
        app = PlanningApplication(self.planning_db_path, "http://catalog.test")
        planner_login = self.wsgi_request(
            app,
            "/login",
            method="POST",
            body=urlencode({"username": "planner", "password": "demo123"}).encode(),
        )
        planner_cookie = dict(planner_login["headers"])["Set-Cookie"].split(";", 1)[0]
        readonly_page = self.wsgi_request(app, "/rules", cookie=planner_cookie)["body"].decode("utf-8")
        self.assertIn("规则只读账号", readonly_page)
        self.assertNotIn("action='/rules/category-cost'", readonly_page)
        denied = self.wsgi_request(
            app,
            "/rules/category-cost",
            method="POST",
            body=urlencode({"upper_cost": "500", "multiplier": "4"}).encode(),
            cookie=planner_cookie,
        )
        self.assertTrue(denied["status"].startswith("403"))

        admin_login = self.wsgi_request(
            app,
            "/login",
            method="POST",
            body=urlencode({"username": "planning_admin", "password": "demo123"}).encode(),
        )
        admin_cookie = dict(admin_login["headers"])["Set-Cookie"].split(";", 1)[0]
        created = self.wsgi_request(
            app,
            "/rules/category-cost",
            method="POST",
            body=urlencode({"upper_cost": "600", "multiplier": "4", "note": "首档"}).encode(),
            cookie=admin_cookie,
        )
        self.assertTrue(created["status"].startswith("302"))
        cost_rule = planning_db.list_category_cost_rules(self.planning_db_path)[0]
        edit_page = self.wsgi_request(app, f"/rules?edit_cost={cost_rule['id']}", cookie=admin_cookie)["body"].decode("utf-8")
        self.assertIn("保存修改", edit_page)
        self.assertIn("编辑", edit_page)
        self.assertIn("删除", edit_page)

        updated = self.wsgi_request(
            app,
            "/rules/category-cost",
            method="POST",
            body=urlencode({"rule_id": str(cost_rule["id"]), "upper_cost": "650", "multiplier": "4.1", "note": "调整后"}).encode(),
            cookie=admin_cookie,
        )
        self.assertTrue(updated["status"].startswith("302"))
        edited = planning_db.list_category_cost_rules(self.planning_db_path)[0]
        self.assertEqual(edited["upper_cost"], 650)
        self.assertEqual(edited["multiplier"], 4.1)

    def test_formal_database_can_bootstrap_first_admin(self):
        formal_path = Path(self.temp.name) / "formal-planning.db"
        planning_db.init_db(
            formal_path,
            seed_demo=False,
            bootstrap_admin={"username": "owner", "password": "ChangeMe123", "display_name": "正式管理员"},
        )
        user = planning_db.authenticate_user(formal_path, "owner", "ChangeMe123")
        self.assertEqual(user["role"], "admin")

    def test_planning_workbench_login_and_sync(self):
        app = PlanningApplication(self.planning_db_path, "http://catalog.test", "token")
        login = self.wsgi_request(app, "/login", method="POST", body=urlencode({"username": "planner", "password": "demo123"}).encode())
        self.assertTrue(login["status"].startswith("302"))
        cookie = dict(login["headers"])["Set-Cookie"].split(";", 1)[0]
        source = {"id": 9, "style_code": "M009", "product_name": "测试款", "season_year": "2026秋", "supplier": "供应商", "category": "毛衣", "actual_cost": 150, "tax_included_price": 150, "status": "pending", "lifecycle_status": "active", "source_version_no": 1, "updated_at": "", "creator_name": "跟单员"}
        with patch.object(app, "fetch_catalog_products", return_value=[source]):
            response = self.wsgi_request(app, "/sync", method="POST", cookie=cookie)
        self.assertTrue(response["status"].startswith("302"))
        self.assertEqual(planning_db.get_source_product(self.planning_db_path, 9)["style_code"], "M009")

    def test_pricing_workbench_uses_one_card_and_requires_admin_review(self):
        planning_db.save_category_cost_rule(self.planning_db_path, "2026秋冬", None, 700, 4)
        planning_db.save_category_rule(self.planning_db_path, "2026秋冬", "连衣裙", 4.2)
        source = {
            "id": 31,
            "style_code": "M031",
            "style_color": "M031-黑",
            "image_url": "https://example.com/m031.jpg",
            "product_name": "审核测试款",
            "season_year": "2026秋冬",
            "supplier": "供应商审核",
            "category": "毛衣",
            "actual_cost": 150,
            "tax_included_price": 150,
            "status": "pending",
            "lifecycle_status": "active",
            "source_version_no": 1,
            "updated_at": "",
            "creator_name": "跟单员",
        }
        app = PlanningApplication(self.planning_db_path, "http://catalog.test")
        planner_login = self.wsgi_request(
            app,
            "/login",
            method="POST",
            body=urlencode({"username": "planner", "password": "demo123"}).encode(),
        )
        planner_cookie = dict(planner_login["headers"])["Set-Cookie"].split(";", 1)[0]
        with patch.object(app, "fetch_catalog_products", return_value=[source]):
            self.wsgi_request(app, "/sync", method="POST", cookie=planner_cookie)
        suggest = self.wsgi_request(
            app,
            "/pricing/suggest",
            method="POST",
            body=urlencode({"product_id": "31", "category": "毛衣"}).encode(),
            cookie=planner_cookie,
        )
        self.assertTrue(suggest["status"].startswith("302"))
        self.assertTrue(dict(suggest["headers"])["Location"].endswith("#pricing-row-31"))
        record = planning_db.list_pricing_records(self.planning_db_path)[0]
        self.assertEqual(record["status"], "suggested")
        workbench = self.wsgi_request(app, "/workbench", cookie=planner_cookie)["body"].decode("utf-8")
        self.assertEqual(workbench.count("<section class='panel pricing-board'>"), 1)
        self.assertEqual(workbench.count("<table class='pricing-table'>"), 1)
        self.assertNotIn("<article class='product-card'>", workbench)
        headers = ["年份季节", "款号", "款色", "图片", "商品名称", "供应商", "含税成本", "来源状态"]
        header_positions = [workbench.index(f"<th>{header}</th>") for header in headers]
        self.assertEqual(header_positions, sorted(header_positions))
        self.assertIn("品类与规则计算", workbench)
        self.assertIn("定价初审与复核", workbench)
        self.assertIn("测算上新价", workbench)
        self.assertIn("初审上新价", workbench)
        self.assertIn("min='1' step='1' inputmode='numeric'", workbench)
        self.assertIn("待初审", workbench)
        self.assertIn("定价状态", workbench)
        self.assertIn("初审 / 复核 / 回传", workbench)
        self.assertIn("确认并提交复核", workbench)
        self.assertIn("id='pricing-row-31'", workbench)
        self.assertIn("planning-workbench-scroll", workbench)
        self.assertIn("tableWrap.scrollLeft", workbench)
        self.assertIn("history.replaceState", workbench)
        self.assertIn("setTimeout(restoreScroll, 150)", workbench)
        self.assertNotIn("PRICING RECORDS", workbench)

        rejected_fractional = self.wsgi_request(
            app,
            f"/pricing/{record['id']}/submit-review",
            method="POST",
            body=urlencode({"launch_price": "589.5"}).encode(),
            cookie=planner_cookie,
        )
        self.assertTrue(rejected_fractional["status"].startswith("400"))
        self.assertIn("必须是大于 0 的整数", rejected_fractional["body"].decode("utf-8"))
        self.assertEqual(planning_db.get_pricing_record(self.planning_db_path, record["id"])["status"], "suggested")

        submitted = self.wsgi_request(
            app,
            f"/pricing/{record['id']}/submit-review",
            method="POST",
            body=urlencode({"launch_price": "2539"}).encode(),
            cookie=planner_cookie,
        )
        self.assertTrue(submitted["status"].startswith("302"))
        self.assertTrue(dict(submitted["headers"])["Location"].endswith("#pricing-row-31"))
        submitted_record = planning_db.get_pricing_record(self.planning_db_path, record["id"])
        self.assertEqual(submitted_record["status"], "review_pending")
        self.assertEqual(submitted_record["calculated_price"], 599)
        self.assertEqual(submitted_record["launch_price"], 2539)
        denied = self.wsgi_request(
            app,
            f"/pricing/{record['id']}/approve",
            method="POST",
            body=urlencode({"launch_price": "579"}).encode(),
            cookie=planner_cookie,
        )
        self.assertTrue(denied["status"].startswith("403"))

        admin_login = self.wsgi_request(
            app,
            "/login",
            method="POST",
            body=urlencode({"username": "planning_admin", "password": "demo123"}).encode(),
        )
        admin_cookie = dict(admin_login["headers"])["Set-Cookie"].split(";", 1)[0]
        review_page = self.wsgi_request(app, "/workbench?status=review_pending", cookie=admin_cookie)["body"].decode("utf-8")
        self.assertEqual(review_page.count("复核上新价"), 1)
        self.assertEqual(review_page.count("<input name='launch_price'"), 1)
        self.assertIn("min='1' step='1' inputmode='numeric'", review_page)
        self.assertIn("data-saved-value='2539'", review_page)
        self.assertIn("修改保存", review_page)
        self.assertIn("复核通过", review_page)
        self.assertIn("grid-template-columns:max-content max-content", review_page)
        self.assertIn(".review-approve-button{grid-column:2;justify-self:end}", review_page)
        rejected_review_fractional = self.wsgi_request(
            app,
            f"/pricing/{record['id']}/approve",
            method="POST",
            body=urlencode({"launch_price": "2539.5"}).encode(),
            cookie=admin_cookie,
        )
        self.assertTrue(rejected_review_fractional["status"].startswith("400"))
        self.assertEqual(planning_db.get_pricing_record(self.planning_db_path, record["id"])["status"], "review_pending")
        rejected_unsaved_review = self.wsgi_request(
            app,
            f"/pricing/{record['id']}/approve",
            method="POST",
            body=urlencode({"launch_price": "579"}).encode(),
            cookie=admin_cookie,
        )
        self.assertTrue(rejected_unsaved_review["status"].startswith("400"))
        self.assertIn("请先点击“修改保存”", rejected_unsaved_review["body"].decode("utf-8"))
        self.assertEqual(planning_db.get_pricing_record(self.planning_db_path, record["id"])["status"], "review_pending")
        saved_review = self.wsgi_request(
            app,
            f"/pricing/{record['id']}/review-save",
            method="POST",
            body=urlencode({"launch_price": "579"}).encode(),
            cookie=admin_cookie,
        )
        self.assertTrue(saved_review["status"].startswith("302"))
        saved_record = planning_db.get_pricing_record(self.planning_db_path, record["id"])
        self.assertEqual(saved_record["status"], "review_pending")
        self.assertEqual(saved_record["launch_price"], 579)
        approved = self.wsgi_request(
            app,
            f"/pricing/{record['id']}/approve",
            method="POST",
            body=urlencode({"launch_price": "579"}).encode(),
            cookie=admin_cookie,
        )
        self.assertTrue(approved["status"].startswith("302"))
        approved_record = planning_db.get_pricing_record(self.planning_db_path, record["id"])
        self.assertEqual(approved_record["status"], "confirmed")
        self.assertEqual(approved_record["launch_price"], 579)

        app.catalog_api_token = "token"
        admin_confirmed_page = self.wsgi_request(
            app,
            "/workbench?status=confirmed",
            cookie=admin_cookie,
        )["body"].decode("utf-8")
        self.assertIn("复核已通过，待商品部回传", admin_confirmed_page)
        self.assertNotIn(f"action='/pricing/{record['id']}/publish'", admin_confirmed_page)
        self.assertNotIn("action='/sync'", admin_confirmed_page)
        denied_publish = self.wsgi_request(
            app,
            f"/pricing/{record['id']}/publish",
            method="POST",
            cookie=admin_cookie,
        )
        self.assertTrue(denied_publish["status"].startswith("403"))
        denied_sync = self.wsgi_request(app, "/sync", method="POST", cookie=admin_cookie)
        self.assertTrue(denied_sync["status"].startswith("403"))

        with patch.object(app, "fetch_catalog_products", return_value=[]):
            planner_confirmed_page = self.wsgi_request(
                app,
                "/workbench?status=confirmed",
                cookie=planner_cookie,
            )["body"].decode("utf-8")
        self.assertIn(f"action='/pricing/{record['id']}/publish'", planner_confirmed_page)
        self.assertIn("回传藏宝阁", planner_confirmed_page)
        self.assertIn("action='/sync'", planner_confirmed_page)
        publication_result = io.BytesIO(json.dumps({"status": "published"}).encode("utf-8"))
        with patch("planning_center.web.urlopen", return_value=publication_result) as mocked_urlopen:
            published = self.wsgi_request(
                app,
                f"/pricing/{record['id']}/publish",
                method="POST",
                cookie=planner_cookie,
            )
        self.assertTrue(published["status"].startswith("302"))
        publication_payload = json.loads(mocked_urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(publication_payload["operator_name"], "商品部企划员")
        self.assertEqual(planning_db.get_pricing_record(self.planning_db_path, record["id"])["status"], "published")

    def test_batch_initial_review_approval_and_publication(self):
        planning_db.save_category_cost_rule(self.planning_db_path, "2026秋冬", None, 700, 4)
        products = [
            {
                "id": product_id,
                "style_code": f"B{product_id}",
                "style_color": f"B{product_id}-黑",
                "product_name": f"批量测试款 {product_id}",
                "season_year": "2026秋冬",
                "supplier": "批量供应商",
                "category": "毛衣",
                "actual_cost": 150,
                "status": "pending",
                "source_version_no": 1,
            }
            for product_id in (71, 72)
        ]
        planning_db.upsert_source_products(self.planning_db_path, products)
        records = [planning_db.create_pricing_record(self.planning_db_path, product, "商品部企划员") for product in products]
        first, second = records
        app = PlanningApplication(self.planning_db_path, "http://catalog.test")
        planner_cookie = self.login_cookie(app, "planner")
        admin_cookie = self.login_cookie(app, "planning_admin")

        initial_page = self.wsgi_request(app, "/workbench?status=suggested", cookie=planner_cookie)["body"].decode("utf-8")
        self.assertIn("pricing-select-all", initial_page)
        self.assertIn("批量初审提交", initial_page)
        self.assertIn("批量回传藏宝阁", initial_page)
        self.assertIn(".pricing-select-cell{position:sticky;left:0", initial_page)
        self.assertIn(".pricing-table thead .pricing-select-cell{z-index:2", initial_page)
        self.assertIn(".pricing-table .pricing-action-cell{vertical-align:middle}", initial_page)
        self.assertIn(".pricing-table .pricing-action-cell>form:only-child{margin-bottom:0}", initial_page)
        self.assertEqual(initial_page.count("name='submit_review_ids'"), 2)
        no_selection = self.wsgi_request(
            app,
            "/pricing/batch",
            method="POST",
            body=urlencode({"batch_action": "submit-review"}).encode(),
            cookie=planner_cookie,
        )
        self.assertTrue(no_selection["status"].startswith("400"))
        batch_initial = self.wsgi_request(
            app,
            "/pricing/batch",
            method="POST",
            body=urlencode(
                [
                    ("batch_action", "submit-review"),
                    ("submit_review_ids", str(first["id"])),
                    ("submit_review_ids", str(second["id"])),
                    (f"launch_price_{first['id']}", "609"),
                    (f"launch_price_{second['id']}", "619"),
                ]
            ).encode(),
            cookie=planner_cookie,
        )
        self.assertTrue(batch_initial["status"].startswith("302"))
        self.assertIn("status=review_pending", dict(batch_initial["headers"])["Location"])
        self.assertEqual(planning_db.get_pricing_record(self.planning_db_path, first["id"])["launch_price"], 609)
        self.assertEqual(planning_db.get_pricing_record(self.planning_db_path, second["id"])["launch_price"], 619)

        denied_approval = self.wsgi_request(
            app,
            "/pricing/batch",
            method="POST",
            body=urlencode([("batch_action", "approve"), ("approve_ids", str(first["id"])), (f"review_price_{first['id']}", "609")]).encode(),
            cookie=planner_cookie,
        )
        self.assertTrue(denied_approval["status"].startswith("403"))
        review_page = self.wsgi_request(app, "/workbench?status=review_pending", cookie=admin_cookie)["body"].decode("utf-8")
        self.assertIn("批量复核通过", review_page)
        self.assertEqual(review_page.count("name='approve_ids'"), 2)
        missing_price = self.wsgi_request(
            app,
            "/pricing/batch",
            method="POST",
            body=urlencode([("batch_action", "approve"), ("approve_ids", str(first["id"]))]).encode(),
            cookie=admin_cookie,
        )
        self.assertTrue(missing_price["status"].startswith("400"))
        self.assertEqual(planning_db.get_pricing_record(self.planning_db_path, first["id"])["status"], "review_pending")
        unsaved_price = self.wsgi_request(
            app,
            "/pricing/batch",
            method="POST",
            body=urlencode(
                [
                    ("batch_action", "approve"),
                    ("approve_ids", str(first["id"])),
                    (f"review_price_{first['id']}", "629"),
                ]
            ).encode(),
            cookie=admin_cookie,
        )
        self.assertTrue(unsaved_price["status"].startswith("400"))
        self.assertIn("请先点击“修改保存”", unsaved_price["body"].decode("utf-8"))
        self.assertEqual(planning_db.get_pricing_record(self.planning_db_path, first["id"])["status"], "review_pending")
        batch_approval = self.wsgi_request(
            app,
            "/pricing/batch",
            method="POST",
            body=urlencode(
                [
                    ("batch_action", "approve"),
                    ("approve_ids", str(first["id"])),
                    ("approve_ids", str(second["id"])),
                    (f"review_price_{first['id']}", "609"),
                    (f"review_price_{second['id']}", "619"),
                ]
            ).encode(),
            cookie=admin_cookie,
        )
        self.assertTrue(batch_approval["status"].startswith("302"))
        self.assertEqual(planning_db.get_pricing_record(self.planning_db_path, first["id"])["status"], "confirmed")
        self.assertEqual(planning_db.get_pricing_record(self.planning_db_path, second["id"])["status"], "confirmed")

        app.catalog_api_token = "token"
        with patch.object(app, "fetch_catalog_products", return_value=[]):
            publish_page = self.wsgi_request(app, "/workbench?status=confirmed", cookie=planner_cookie)["body"].decode("utf-8")
        self.assertEqual(publish_page.count("name='publish_ids'"), 2)
        publication_responses = [
            io.BytesIO(json.dumps({"status": "published"}).encode("utf-8")),
            io.BytesIO(json.dumps({"status": "published"}).encode("utf-8")),
        ]
        with patch("planning_center.web.urlopen", side_effect=publication_responses) as mocked_urlopen:
            batch_publish = self.wsgi_request(
                app,
                "/pricing/batch",
                method="POST",
                body=urlencode(
                    [
                        ("batch_action", "publish"),
                        ("publish_ids", str(first["id"])),
                        ("publish_ids", str(second["id"])),
                    ]
                ).encode(),
                cookie=planner_cookie,
            )
        self.assertTrue(batch_publish["status"].startswith("302"))
        self.assertEqual(mocked_urlopen.call_count, 2)
        self.assertEqual(planning_db.get_pricing_record(self.planning_db_path, first["id"])["status"], "published")
        self.assertEqual(planning_db.get_pricing_record(self.planning_db_path, second["id"])["status"], "published")

    def test_initial_review_defaults_to_calculated_price_when_price_is_omitted(self):
        planning_db.save_category_cost_rule(self.planning_db_path, "2026秋冬", None, 700, 4)
        product = {
            "id": 35,
            "style_code": "M035",
            "product_name": "默认测算价",
            "season_year": "2026秋冬",
            "supplier": "供应商",
            "category": "毛衣",
            "actual_cost": 150,
            "source_version_no": 1,
        }
        planning_db.upsert_source_products(self.planning_db_path, [product])
        app = PlanningApplication(self.planning_db_path, "http://catalog.test")
        user = planning_db.authenticate_user(self.planning_db_path, "planner", "demo123")
        record = planning_db.create_pricing_record(self.planning_db_path, product, user["display_name"])
        response = self.wsgi_request(
            app,
            f"/pricing/{record['id']}/submit-review",
            method="POST",
            body=b"",
            cookie=self.login_cookie(app, "planner"),
        )
        self.assertTrue(response["status"].startswith("302"))
        submitted = planning_db.get_pricing_record(self.planning_db_path, record["id"])
        self.assertEqual(submitted["calculated_price"], 599)
        self.assertEqual(submitted["launch_price"], 599)

    def test_pricing_workbench_keeps_multiple_styles_in_one_board(self):
        app = PlanningApplication(self.planning_db_path, "http://catalog.test")
        products = [
            {
                "id": 41,
                "style_code": "M041",
                "style_color": "M041-黑",
                "product_name": "多款测试一",
                "season_year": "2026秋冬",
                "supplier": "供应商一",
                "actual_cost": 150,
                "status": "pending",
                "source_version_no": 1,
            },
            {
                "id": 42,
                "style_code": "M042",
                "style_color": "M042-白",
                "product_name": "多款测试二",
                "season_year": "2026秋冬",
                "supplier": "供应商二",
                "actual_cost": 620,
                "status": "pending",
                "source_version_no": 1,
            },
        ]
        planning_db.upsert_source_products(self.planning_db_path, products)
        user = planning_db.authenticate_user(self.planning_db_path, "planner", "demo123")
        workbench = app.render_workbench(user, {})
        self.assertEqual(workbench.count("<section class='panel pricing-board'>"), 1)
        self.assertEqual(workbench.count("<tr"), 3)
        self.assertIn("M041", workbench)
        self.assertIn("M042", workbench)

    def test_category_planning_phase_two_entry_is_visible(self):
        app = PlanningApplication(self.planning_db_path, "http://catalog.test")
        login = self.wsgi_request(
            app,
            "/login",
            method="POST",
            body=urlencode({"username": "planner", "password": "demo123"}).encode(),
        )
        cookie = dict(login["headers"])["Set-Cookie"].split(";", 1)[0]

        dashboard = self.wsgi_request(app, "/dashboard", cookie=cookie)
        dashboard_html = dashboard["body"].decode("utf-8")
        self.assertIn("品类企划", dashboard_html)
        self.assertIn("/category-planning", dashboard_html)

        category_planning = self.wsgi_request(app, "/category-planning", cookie=cookie)
        category_html = category_planning["body"].decode("utf-8")
        self.assertTrue(category_planning["status"].startswith("200"))
        self.assertIn("年份季节", category_html)
        self.assertIn("品类组合", category_html)
        self.assertIn("SKU 数", category_html)
        self.assertIn("第二阶段", category_html)


if __name__ == "__main__":
    unittest.main()
