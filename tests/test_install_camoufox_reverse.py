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


def _archive(path: Path, *, unsafe: bool = False, reverse_release: str = "reverse.2") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "version.json",
            json.dumps({"version": "152.0.4", "release": "beta.30"}),
        )
        archive.writestr(
            installer.CAPABILITIES_FILE,
            json.dumps(
                {
                    "schema": 1,
                    "distribution": "WhiteNightShadow/camoufox-reverse",
                    "upstream_version": "152.0.4-beta.30",
                    "reverse_release": reverse_release,
                    "property_trace": True,
                    "property_trace_protocol": 1,
                    "property_trace_hooks": 75,
                }
            ),
        )
        archive.writestr("camoufox-bin", "binary")
        if unsafe:
            archive.writestr("../escape", "bad")
    return path


def _digest(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


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

        archive = _archive(
            self.root / "camoufox-152.0.4-beta.30-lin.x86_64.zip"
        )
        result = installer.install_archive(
            archive, cache_dir=cache, expected_sha256=_digest(archive)
        )

        self.assertEqual(
            result["selector"],
            "whitenightshadow/152.0.4-beta.30-reverse.2",
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
            archive = _archive(
                self.root / "camoufox-152.0.4-beta.30-lin.x86_64.zip"
            )
            installer.install_archive(
                archive, cache_dir=cache, expected_sha256=_digest(archive)
            )
        self.assertEqual(version.read_bytes(), before)
        self.assertFalse((cache / "browsers").exists())

    def test_archive_traversal_and_hash_mismatch_are_rejected(self):
        cache = self.root / "cache"
        cache.mkdir()
        (cache / ".0.5_FLAG").touch()
        archive = _archive(
            self.root / "camoufox-152.0.4-beta.30-lin.x86_64.zip",
            unsafe=True,
        )

        with self.assertRaisesRegex(installer.InstallError, "unsafe archive path"):
            installer.install_archive(
                archive, cache_dir=cache, expected_sha256=_digest(archive)
            )
        with self.assertRaisesRegex(installer.InstallError, "SHA256 mismatch"):
            installer.install_archive(
                archive,
                cache_dir=cache,
                expected_sha256="0" * 64,
            )

    def test_metadata_path_escape_is_rejected(self):
        cache = self.root / "cache"
        cache.mkdir()
        (cache / ".0.5_FLAG").touch()
        archive = _archive(
            self.root / "camoufox-152.0.4-beta.30-lin.x86_64.zip",
            reverse_release="../escape",
        )

        with self.assertRaisesRegex(installer.InstallError, "invalid reverse_release"):
            installer.install_archive(
                archive, cache_dir=cache, expected_sha256=_digest(archive)
            )


if __name__ == "__main__":
    unittest.main()
