from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

from config import load_supplier_configs, save_supplier_configs
from models import SupplierConfig
from webapp.config import BootstrapUser, WebAppConfig


def load_runtime_config_merge_module():
    module_path = Path(__file__).resolve().parents[1] / "deploy" / "aws" / "merge_supplier_configs.py"
    spec = importlib.util.spec_from_file_location("merge_supplier_configs", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeploymentConfigTests(unittest.TestCase):
    def test_supplier_output_root_env_redirects_relative_output_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["SUPPLIER_OUTPUT_ROOT"] = tmpdir
            config = SupplierConfig(
                supplier_slug="swissbox",
                enabled=True,
                scraper_adapter="swissbox",
                base_url="https://www.swissbox-ag.ch",
                ybm_token_env_var="YBM_TOKEN_SWISSBOX",
                output_dir="output/swissbox",
            )
            resolved = config.output_path(Path("/repo"))
            self.assertEqual(resolved, Path(tmpdir) / "output" / "swissbox")

    def test_webapp_config_can_enforce_non_placeholder_secrets(self) -> None:
        os.environ["TEST_SESSION_SECRET"] = "replace-me"
        os.environ["TEST_ADMIN_PASSWORD"] = "replace-me"
        config = WebAppConfig(
            session_secret_env_var="TEST_SESSION_SECRET",
            enforce_non_placeholder_secrets=True,
            bootstrap_users=[
                BootstrapUser(
                    username="admin",
                    password_env_var="TEST_ADMIN_PASSWORD",
                )
            ],
        )
        with self.assertRaisesRegex(ValueError, "non-placeholder"):
            config.validate_runtime()

    def test_supplier_configs_can_roundtrip_to_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "suppliers.json"
            save_supplier_configs(
                [
                    SupplierConfig(
                        supplier_slug="demo",
                        enabled=True,
                        scraper_adapter="swissbox",
                        base_url="https://example.com",
                        ybm_token_env_var="YBM_TOKEN_DEMO",
                        output_dir="output/demo",
                        catalog_update_policy="keep_existing",
                        schedule={"frequency": "weekly", "weekday": "friday", "time": "04:15"},
                    )
                ],
                config_path=path,
            )
            loaded = load_supplier_configs(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].supplier_slug, "demo")
            self.assertEqual(loaded[0].schedule["weekday"], "friday")
            self.assertEqual(loaded[0].catalog_update_policy, "keep_existing")

    def test_supplier_configs_can_load_from_inline_json_env(self) -> None:
        inline = {
            "suppliers": [
                {
                    "supplier_slug": "demo",
                    "enabled": True,
                    "scraper_adapter": "swissbox",
                    "base_url": "https://example.com",
                    "ybm_token_env_var": "YBM_TOKEN_DEMO",
                    "output_dir": "output/demo",
                    "catalog_update_policy": "keep_existing",
                }
            ]
        }
        previous = os.environ.get("SUPPLIER_CONFIG_JSON")
        os.environ["SUPPLIER_CONFIG_JSON"] = json.dumps(inline)
        try:
            loaded = load_supplier_configs()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].supplier_slug, "demo")
            self.assertEqual(loaded[0].catalog_update_policy, "keep_existing")
        finally:
            if previous is None:
                os.environ.pop("SUPPLIER_CONFIG_JSON", None)
            else:
                os.environ["SUPPLIER_CONFIG_JSON"] = previous

    def test_github_actions_and_deploy_script_exist(self) -> None:
        root = Path(__file__).resolve().parents[1]
        self.assertTrue((root / ".github" / "workflows" / "ci.yml").exists())
        self.assertTrue((root / ".github" / "workflows" / "deploy.yml").exists())
        self.assertTrue((root / ".github" / "workflows" / "build-worker.yml").exists())
        self.assertTrue((root / "deploy" / "aws" / "deploy-from-git.sh").exists())
        self.assertTrue((root / "deploy" / "aws" / "ecs-task-definition.worker.json").exists())
        self.assertTrue((root / "deploy" / "aws" / "setup-ecs-worker.sh").exists())
        self.assertTrue((root / "Dockerfile.worker").exists())

    def test_webapp_config_supports_ecs_backend_settings(self) -> None:
        config = WebAppConfig(
            job_backend="ecs_fargate",
        )
        self.assertEqual(config.job_backend, "ecs_fargate")
        self.assertEqual(config.ecs_backend.launch_type, "FARGATE")

    def test_webapp_config_requires_ecs_fields_when_backend_enabled(self) -> None:
        config = WebAppConfig(job_backend="ecs_fargate")
        with self.assertRaisesRegex(ValueError, "ecs_backend"):
            config.validate_runtime()

    def test_worker_task_definition_template_uses_four_vcpu_shape(self) -> None:
        root = Path(__file__).resolve().parents[1]
        task_def = (root / "deploy" / "aws" / "ecs-task-definition.worker.json").read_text(
            encoding="utf-8"
        )
        self.assertIn('"cpu": "4096"', task_def)
        self.assertIn('"memory": "8192"', task_def)
        self.assertIn('"stopTimeout": 10', task_def)

    def test_runtime_supplier_config_preserves_dashboard_settings_and_adds_new_defaults(self) -> None:
        merge_module = load_runtime_config_merge_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            defaults_path = root / "suppliers.defaults.json"
            legacy_path = root / "legacy-suppliers.json"
            runtime_path = root / "state" / "suppliers.json"
            defaults_path.write_text(
                json.dumps(
                    {
                        "suppliers": [
                            {"supplier_slug": "swissbox", "enabled": True},
                            {"supplier_slug": "walker", "enabled": True},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            legacy_path.write_text(
                json.dumps(
                    {
                        "suppliers": [
                            {
                                "supplier_slug": "swissbox",
                                "enabled": True,
                                "schedule": {"frequency": "disabled"},
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            preserved, added = merge_module.merge_supplier_configs(
                defaults_path=defaults_path,
                runtime_path=runtime_path,
                legacy_path=legacy_path,
            )
            payload = json.loads(runtime_path.read_text(encoding="utf-8"))

            self.assertEqual((preserved, added), (1, 1))
            self.assertEqual(payload["suppliers"][0]["schedule"]["frequency"], "disabled")
            self.assertEqual(payload["suppliers"][1]["supplier_slug"], "walker")

            defaults_path.write_text(
                json.dumps({"suppliers": [{"supplier_slug": "swissbox", "enabled": True}]}),
                encoding="utf-8",
            )
            merge_module.merge_supplier_configs(
                defaults_path=defaults_path,
                runtime_path=runtime_path,
                legacy_path=legacy_path,
            )
            persisted = json.loads(runtime_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["suppliers"][0]["schedule"]["frequency"], "disabled")

    def test_deploy_keeps_dashboard_supplier_config_outside_the_synced_app_directory(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        production_config = (root / "deploy" / "aws" / "control_panel.production.json").read_text(
            encoding="utf-8"
        )
        self.assertIn('--exclude "suppliers.json"', workflow)
        self.assertIn("suppliers.defaults.json", workflow)
        self.assertIn("/var/lib/yourbarmate-suppliers/control_panel/suppliers.json", production_config)

    def test_production_alert_email_uses_the_frankfurt_ses_endpoint(self) -> None:
        root = Path(__file__).resolve().parents[1]
        production_config = json.loads(
            (root / "deploy" / "aws" / "control_panel.production.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(
            production_config["alert_email"]["smtp_host"],
            "email-smtp.eu-central-1.amazonaws.com",
        )
