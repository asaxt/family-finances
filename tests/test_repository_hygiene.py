import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_sensitive_files import (  # noqa: E402
    is_sensitive_path,
    repository_paths,
    sensitive_paths,
)


class RepositoryHygieneTests(unittest.TestCase):
    def test_sensitive_local_data_names_are_recognized(self):
        sensitive_examples = (
            ".env",
            ".env.production",
            ".local.env",
            ".local.env.backup",
            ".auth.json",
            "..auth.json.123.tmp",
            "family-finances.vault",
            ".family-finances.vault.123.tmp",
            "finance.db-wal",
            "data.sqlite3",
            "backups/family-finances.vault",
            ".migration-backup-123/.auth.json",
            ".password-change-backup-123/family-finances.vault",
        )
        safe_examples = (
            "app.py",
            "vault.py",
            "tests/test_vault.py",
            ".github/dependabot.yml",
            "docs/environment.md",
        )

        for path in sensitive_examples:
            with self.subTest(path=path):
                self.assertTrue(is_sensitive_path(path))
        for path in safe_examples:
            with self.subTest(path=path):
                self.assertFalse(is_sensitive_path(path))

    def test_repository_history_contains_no_sensitive_local_data_names(self):
        self.assertEqual(sensitive_paths(repository_paths(ROOT)), [])


if __name__ == "__main__":
    unittest.main()
