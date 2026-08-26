from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from check_repo_hygiene import check_repository  # noqa: E402


class RepositoryHygieneTests(unittest.TestCase):
    def test_tracked_repository_contents_meet_hygiene_rules(self) -> None:
        self.assertEqual(check_repository(), [])
