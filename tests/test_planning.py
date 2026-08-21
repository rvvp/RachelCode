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
        planning_db.save_category_rule(self.planning_db_path, "2026秋", "毛衣", 4)
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
        with self.assertRaisesRegex(ValueError, "尚未配置固定倍率"):
            planning_db.create_pricing_record(self.planning_db_path, product, "测试企划员")

    def test_idempotent_publication_is_marked_published(self):
        planning_db.save_category_rule(self.planning_db_path, "2026秋", "毛衣", 4)
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
