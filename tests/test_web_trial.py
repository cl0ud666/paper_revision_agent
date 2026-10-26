"""Registration and three lifetime opportunities. No external SMS/model calls."""

import os
from unittest.mock import patch

from app.web import service
from app.web.db import transaction
from app.web.models import Run
from app.web.settings import settings
from tests.test_web_api import WebTestCase, paper_bytes


class TestFreeTrial(WebTestCase):
    def setUp(self):
        super().setUp()
        self.login("member")

    def quota(self):
        return self.client.get("/api/v1/users/me").json()["free_quota"]

    def test_admin_uploads_do_not_use_lifetime_trial_quota(self):
        self.login("admin")
        runs = [self.upload() for _ in range(4)]
        self.assertTrue(all(run["free_quota"] is False for run in runs))
        self.assertEqual(self.quota(), {"total": 3, "remaining": 3, "completed": 0, "reserved": 0})

    def test_registration_creates_normal_account_session_and_three_opportunities(self):
        self.client.cookies.clear()
        with patch("app.web.routes.auth.registration_rate_limit") as limit:
            response = self.client.post(
                "/api/v1/auth/register",
                json={"username": "New.User", "password": "strong-password-123", "is_admin": True},
            )
        self.assertEqual(response.status_code, 201, response.text)
        limit.assert_called_once()
        user = response.json()
        self.assertEqual(user["username"], "new.user")
        self.assertFalse(user["is_admin"])
        self.assertEqual(user["free_quota"]["remaining"], 3)
        self.assertNotIn("run_token_limit", user)
        self.assertNotIn("password_hash", user)
        self.assertIn("httponly", response.headers["set-cookie"].lower())
        self.client.headers["X-CSRF-Token"] = user["csrf_token"]
        self.assertEqual(self.client.get("/api/v1/users/me").status_code, 200)
        self.upload()
        self.assertEqual(self.quota()["remaining"], 2)

    def test_six_character_password_allowed_five_rejected(self):
        self.client.cookies.clear()
        with patch("app.web.routes.auth.registration_rate_limit"):
            short = self.client.post(
                "/api/v1/auth/register", json={"username": "shortpass", "password": "12345"}
            )
            self.assertEqual(short.status_code, 422)
            valid = self.client.post(
                "/api/v1/auth/register", json={"username": "sixpass", "password": "123456"}
            )
            self.assertEqual(valid.status_code, 201, valid.text)
        with self.assertRaises(service.ApiError):
            service.add_user("directshort", "12345")
        with self.assertRaises(service.ApiError):
            service.reset_password("sixpass", "12345")

    def test_registration_disabled_weak_password_duplicate_and_rate_fail_closed(self):
        body = {"username": "admin", "password": "testing-password-123"}
        with patch("app.web.routes.auth.registration_rate_limit"):
            self.assertEqual(self.client.post("/api/v1/auth/register", json=body).status_code, 409)
            self.assertEqual(
                self.client.post(
                    "/api/v1/auth/register", json={**body, "password": "123"}
                ).status_code,
                422,
            )
        with patch("app.web.routes.auth.registration_rate_limit", side_effect=ConnectionError()):
            self.assertEqual(self.client.post("/api/v1/auth/register", json=body).status_code, 503)
        with patch(
            "app.web.routes.auth.registration_rate_limit",
            side_effect=service.ApiError(429, "slow down"),
        ):
            self.assertEqual(self.client.post("/api/v1/auth/register", json=body).status_code, 429)
        with patch.dict(os.environ, {"PRA_REGISTRATION_OPEN": "0"}):
            settings.cache_clear()
            self.assertFalse(self.client.get("/api/v1/auth/options").json()["registration_open"])
            self.assertEqual(self.client.post("/api/v1/auth/register", json=body).status_code, 403)
        settings.cache_clear()

    def test_three_reserved_slots_prevent_fourth_and_cancel_releases(self):
        runs = [self.upload() for _ in range(3)]
        self.assertEqual(self.quota(), {"total": 3, "remaining": 0, "completed": 0, "reserved": 3})
        response = self.client.post("/api/v1/runs", files={"file": ("fourth.docx", paper_bytes())})
        self.assertEqual(response.status_code, 403)
        self.client.post(f"/api/v1/runs/{runs[0]['id']}/cancel")
        self.assertEqual(self.quota()["remaining"], 1)
        self.upload()
        self.assertEqual(self.quota()["remaining"], 0)

    def test_failed_retry_cannot_bypass_three_other_tasks(self):
        failed = self.upload()
        with transaction() as db:
            db.get(Run, failed["id"]).status = "failed"
        self.assertEqual(self.quota()["remaining"], 3)
        other = [self.upload() for _ in range(3)]
        self.assertEqual(self.client.post(f"/api/v1/runs/{failed['id']}/retry").status_code, 403)
        self.client.post(f"/api/v1/runs/{other[0]['id']}/cancel")
        self.assertEqual(self.client.post(f"/api/v1/runs/{failed['id']}/retry").status_code, 202)
        self.assertEqual(self.quota()["remaining"], 0)

    def test_completed_stays_used_cancel_requested_reserved_history_exempt(self):
        runs = [self.upload() for _ in range(3)]
        with transaction() as db:
            db.get(Run, runs[0]["id"]).status = "completed"
            db.get(Run, runs[1]["id"]).status = "cancel_requested"
            old = db.get(Run, runs[2]["id"])
            old.free_quota, old.status = False, "completed"
        self.assertEqual(self.quota(), {"total": 3, "remaining": 1, "completed": 1, "reserved": 1})
        self.client.post(f"/api/v1/runs/{runs[0]['id']}/cancel")
        self.assertEqual(self.quota()["completed"], 1)
        self.login("admin")
        self.assertEqual(self.quota()["remaining"], 3)

    def test_registration_ip_limit_does_not_use_username(self):
        from app.web.queue import registration_rate_limit

        with patch("app.web.queue.redis_client") as redis:
            redis.return_value.eval.return_value = 6
            with self.assertRaises(service.ApiError) as error:
                registration_rate_limit("203.0.113.1")
            self.assertEqual(error.exception.status, 429)

    def test_public_report_removes_internal_usage_but_preserves_unfinished(self):
        run = self.upload()
        from app.web.storage import store

        with transaction() as db:
            row = db.get(Run, run["id"])
            row.status, row.report_key = "completed", "test/report.md"
            store().put(
                row.report_key,
                (
                    "## 修改内容\n正常正文\n## 六、运行观测（Agent Harness）\n"
                    "Token：已用 12345\n## 七、异常与降级记录\n请核对原文\n"
                ).encode(),
                "text/markdown",
            )
        report = self.client.get(f"/api/v1/runs/{run['id']}/download?kind=report").text
        self.assertNotIn("12345", report)
        self.assertNotIn("Agent Harness", report)
        self.assertIn("请核对原文", report)
