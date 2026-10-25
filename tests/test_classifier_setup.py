import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from setup_classifier import download_model


MODEL_REPO = "knowledgator/gliclass-v4-edge"
MODEL_REVISION = "90be5550327b138635db1f63531f9dfa15e624da"


class ClassifierSetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name) / "sample-cache"
        self.snapshot = Path(self.directory.name) / "sample-snapshot"
        self.snapshot.mkdir()
        self.download = Mock(return_value=str(self.snapshot))
        hub = types.ModuleType("huggingface_hub")
        hub.snapshot_download = self.download
        self.hub_patch = patch.dict(sys.modules, {"huggingface_hub": hub})
        self.hub_patch.start()
        self.addCleanup(self.hub_patch.stop)
        self.environment_patch = patch.dict(os.environ)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)

    def manifest_path(self):
        return self.cache / "manifest.json"

    def test_downloads_only_the_pinned_v4_model_without_implicit_token_or_code(self):
        observed = {}

        def download(**kwargs):
            observed.update({key: os.environ.get(key) for key in (
                "HF_HUB_DISABLE_TELEMETRY", "HF_HUB_DISABLE_IMPLICIT_TOKEN", "DO_NOT_TRACK",
            )})
            return str(self.snapshot)

        self.download.side_effect = download
        download_model(self.cache)
        self.download.assert_called_once_with(
            repo_id=MODEL_REPO,
            revision=MODEL_REVISION,
            cache_dir=str(self.cache / "hub"),
            token=False,
            allow_patterns=["*.json", "*.safetensors", "*.txt", "*.model"],
        )
        self.assertEqual(set(observed.values()), {"1"})
        self.assertEqual(json.loads(self.manifest_path().read_text()), {
            MODEL_REPO: {"revision": MODEL_REVISION, "path": str(self.snapshot.resolve())},
        })
        self.assertFalse((self.cache / "manifest.json.tmp").exists())

    def test_manifest_update_preserves_other_entries_and_refreshes_only_target(self):
        self.cache.mkdir()
        existing = {
            "EXAMPLE EXISTING MODEL": {"revision": "sample-revision", "path": "sample-cache-entry"},
            MODEL_REPO: {"revision": "sample-old-revision", "path": "sample-old-entry"},
        }
        self.manifest_path().write_text(json.dumps(existing))
        download_model(self.cache)
        manifest = json.loads(self.manifest_path().read_text())
        self.assertEqual(manifest["EXAMPLE EXISTING MODEL"], existing["EXAMPLE EXISTING MODEL"])
        self.assertEqual(set(manifest), set(existing))
        self.assertEqual(manifest[MODEL_REPO], {
            "revision": MODEL_REVISION, "path": str(self.snapshot.resolve()),
        })
        self.download.assert_called_once()

    def test_failed_download_leaves_existing_manifest_unchanged(self):
        self.cache.mkdir()
        original = b'{"EXAMPLE EXISTING MODEL": {"revision": "sample-revision"}}\n'
        self.manifest_path().write_bytes(original)
        self.download.side_effect = RuntimeError("EXAMPLE download unavailable.")
        with self.assertRaisesRegex(RuntimeError, "EXAMPLE download unavailable"):
            download_model(self.cache)
        self.assertEqual(self.manifest_path().read_bytes(), original)
        self.assertFalse((self.cache / "manifest.json.tmp").exists())

    def test_failed_first_download_does_not_create_a_manifest(self):
        self.download.side_effect = RuntimeError("EXAMPLE download unavailable.")
        with self.assertRaises(RuntimeError):
            download_model(self.cache)
        self.assertFalse(self.manifest_path().exists())
        self.assertFalse((self.cache / "manifest.json.tmp").exists())

    def test_invalid_existing_manifest_fails_before_any_download(self):
        self.cache.mkdir()
        original = b'[]\n'
        self.manifest_path().write_bytes(original)
        with self.assertRaisesRegex(ValueError, "Invalid local model manifest"):
            download_model(self.cache)
        self.download.assert_not_called()
        self.assertEqual(self.manifest_path().read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
