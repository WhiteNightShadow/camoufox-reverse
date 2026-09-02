"""Tests for side-by-side Camoufox Reverse archive installation."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "install-camoufox-reverse.py"
SPEC = importlib.util.spec_from_file_location("reverse_installer", SCRIPT)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


def _archive(path: Path, *, unsafe: bool = False) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "version.json",
            json.dumps({"version": "152.0.4", "release": "beta.30"}),
        )
        archive.writestr(
            installer.CAPABILITIES_FILE,
            json.dumps(
                {
                    "distribution": "WhiteNightShadow/camoufox-reverse",
                    "reverse_release": "reverse.1",
                    "property_trace": True,
                }
            ),
        )
        archive.writestr("camoufox-bin", "binary")
        if unsafe:
            archive.writestr("../escape", "bad")
    return path


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_install_is_side_by_side_and_keeps_active_config(self):
        cache = self.root / "cache"
        cache.mkdir()
        (cache / ".0.5_FLAG").touch()
        config = cache / "config.json"
        config.write_bytes(b'{"active_version":"browsers/official/152.0.4-beta.30"}')
        before = config.read_bytes()

        result = installer.install_archive(
            _archive(self.root / "browser.zip"), cache_dir=cache
        )

        self.assertEqual(
            result["selector"],
            "whitenightshadow/152.0.4-beta.30-reverse.1",
        )
        self.assertFalse(result["active_config_changed"])
        self.assertTrue((Path(result["path"]) / "camoufox-bin").is_file())
        self.assertEqual(config.read_bytes(), before)

    def test_legacy_cache_is_rejected_without_changes(self):
        cache = self.root / "cache"
        cache.mkdir()
        version = cache / "version.json"
        version.write_text('{"version":"135.0.1","release":"beta.24"}')
        before = version.read_bytes()

        with self.assertRaisesRegex(installer.InstallError, "legacy Camoufox 0.4"):
            installer.install_archive(
                _archive(self.root / "browser.zip"), cache_dir=cache
            )
        self.assertEqual(version.read_bytes(), before)
        self.assertFalse((cache / "browsers").exists())

    def test_archive_traversal_and_hash_mismatch_are_rejected(self):
        cache = self.root / "cache"
        cache.mkdir()
        (cache / ".0.5_FLAG").touch()
        archive = _archive(self.root / "unsafe.zip", unsafe=True)

        with self.assertRaisesRegex(installer.InstallError, "unsafe archive path"):
            installer.install_archive(archive, cache_dir=cache)
        with self.assertRaisesRegex(installer.InstallError, "SHA256 mismatch"):
            installer.install_archive(
                archive,
                cache_dir=cache,
                expected_sha256="0" * 64,
            )


if __name__ == "__main__":
    unittest.main()
