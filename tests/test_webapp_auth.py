from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from webapp.auth import (
    authenticate_user,
    change_password,
    ensure_bootstrap_users,
    hash_password,
    verify_password,
)
from webapp.config import BootstrapUser, WebAppConfig
from webapp.db import connect, init_db


class WebAppAuthTests(unittest.TestCase):
    def test_hash_and_verify_password(self) -> None:
        password_hash = hash_password("secret-password")
        self.assertNotEqual(password_hash, "secret-password")
        self.assertTrue(verify_password("secret-password", password_hash))
        self.assertFalse(verify_password("wrong-password", password_hash))

    def test_bootstrap_user_is_created_and_authenticates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "control_panel.db"
            conn = connect(db_path)
            init_db(conn)
            os.environ["TEST_CONTROL_PANEL_PASSWORD"] = "topsecret"
            config = WebAppConfig(
                db_path=str(db_path),
                bootstrap_users=[
                    BootstrapUser(
                        username="admin",
                        password_env_var="TEST_CONTROL_PANEL_PASSWORD",
                    )
                ],
            )

            ensure_bootstrap_users(conn, config)
            user = authenticate_user(conn, "admin", "topsecret")
            self.assertEqual(user, {"username": "admin", "role": "admin"})
            self.assertIsNone(authenticate_user(conn, "admin", "wrong"))

            conn.close()

    def test_change_password_updates_existing_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "control_panel.db"
            conn = connect(db_path)
            init_db(conn)
            os.environ["TEST_CONTROL_PANEL_PASSWORD"] = "topsecret12345"
            config = WebAppConfig(
                db_path=str(db_path),
                bootstrap_users=[
                    BootstrapUser(
                        username="admin",
                        password_env_var="TEST_CONTROL_PANEL_PASSWORD",
                    )
                ],
            )
            ensure_bootstrap_users(conn, config)

            error = change_password(
                conn,
                username="admin",
                current_password="topsecret12345",
                new_password="new-password-123",
            )
            self.assertIsNone(error)
            self.assertIsNone(authenticate_user(conn, "admin", "topsecret12345"))
            self.assertEqual(
                authenticate_user(conn, "admin", "new-password-123"),
                {"username": "admin", "role": "admin"},
            )
            conn.close()
