from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from webapp.app import create_app


class WebAppAppTests(unittest.TestCase):
    def test_login_and_protected_api_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "control_panel.db"
            env_path = root / ".env.local"
            config_path = root / "control_panel.json"
            supplier_config_path = root / "suppliers.json"
            env_path.write_text("", encoding="utf-8")
            os.environ["CONTROL_PANEL_ADMIN_PASSWORD"] = "admin-pass"
            os.environ["CONTROL_PANEL_SESSION_SECRET"] = "session-secret-for-tests"
            config_path.write_text(
                json.dumps(
                    {
                        "db_path": str(db_path),
                        "supplier_config_path": str(supplier_config_path),
                        "env_file": str(env_path),
                        "scheduler_enabled": False,
                        "allowed_artifact_roots": ["output", "logs", "state"],
                        "bootstrap_users": [
                            {
                                "username": "admin",
                                "password_env_var": "CONTROL_PANEL_ADMIN_PASSWORD",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            app = create_app(config_path)
            with TestClient(app) as client:
                response = client.get("/")
                self.assertEqual(response.status_code, 200)
                self.assertIn("Sign in", response.text)

                login_response = client.post(
                    "/login",
                    data={"username": "admin", "password": "admin-pass"},
                    follow_redirects=True,
                )
                self.assertEqual(login_response.status_code, 200)
                self.assertIn("Supplier Control Panel", login_response.text)

                suppliers_response = client.get("/api/suppliers")
                self.assertEqual(suppliers_response.status_code, 200)
                self.assertIsInstance(suppliers_response.json(), list)

                public_health_response = client.get("/healthz")
                self.assertEqual(public_health_response.status_code, 200)
                self.assertEqual(public_health_response.json()["status"], "ok")

                settings_response = client.get("/settings")
                self.assertEqual(settings_response.status_code, 200)
                self.assertIn("Change Password", settings_response.text)

                connections_response = client.get("/connections")
                self.assertEqual(connections_response.status_code, 200)
                self.assertIn("Connections & Onboarding", connections_response.text)

                password_response = client.post(
                    "/settings/password",
                    data={
                        "current_password": "admin-pass",
                        "new_password": "new-admin-pass",
                        "confirm_password": "new-admin-pass",
                    },
                    follow_redirects=True,
                )
                self.assertEqual(password_response.status_code, 200)
                self.assertIn("Password updated successfully.", password_response.text)

                new_supplier_response = client.post(
                    "/suppliers/new",
                    data={
                        "supplier_slug": "demo",
                        "enabled": "on",
                        "scraper_adapter": "swissbox",
                        "base_url": "https://example.com",
                        "ybm_token_env_var": "YBM_TOKEN_DEMO",
                        "output_dir": "output/demo",
                        "ybm_api_base": "https://connect.yourbarmate.com/api",
                        "schedule_enabled": "on",
                        "schedule_frequency": "weekly",
                        "schedule_weekday": "friday",
                        "schedule_time": "04:15",
                        "concurrency": "1",
                        "min_delay_seconds": "0.10",
                        "max_delay_seconds": "0.30",
                    },
                    follow_redirects=True,
                )
                self.assertEqual(new_supplier_response.status_code, 200)
                self.assertIn("Supplier created.", new_supplier_response.text)
                self.assertIn("demo", new_supplier_response.text)

                api_supplier_response = client.get("/api/suppliers/demo")
                self.assertEqual(api_supplier_response.status_code, 200)
                self.assertEqual(
                    api_supplier_response.json()["onboarding"]["stage"],
                    "Config only",
                )

                missing_response = client.post("/api/suppliers/nope/jobs/dry-run")
                self.assertEqual(missing_response.status_code, 404)
