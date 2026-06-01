from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from config import load_supplier_configs, save_supplier_configs
from models import SupplierConfig
from webapp.config import BootstrapUser, WebAppConfig


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
                        schedule={"frequency": "weekly", "weekday": "friday", "time": "04:15"},
                    )
                ],
                config_path=path,
            )
            loaded = load_supplier_configs(path)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].supplier_slug, "demo")
            self.assertEqual(loaded[0].schedule["weekday"], "friday")

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
