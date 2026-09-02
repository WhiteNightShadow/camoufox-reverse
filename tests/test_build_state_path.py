"""Tests for opt-in isolation of Mozilla's persistent build state."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "multibuild.py"
SPEC = importlib.util.spec_from_file_location("camoufox_multibuild", SCRIPT)
assert SPEC and SPEC.loader
multibuild = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = multibuild
SPEC.loader.exec_module(multibuild)


class BuildStatePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.mock_home = self.root / "mock-home"
        self.mock_home.mkdir()
        self.isolated = self.root / "isolated-mozbuild"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _environment(self, *, isolated: bool) -> dict[str, str]:
        environment = os.environ.copy()
        if isolated:
            environment["MOZBUILD_STATE_PATH"] = str(self.isolated)
        else:
            environment.pop("MOZBUILD_STATE_PATH", None)
        return environment

    def _make_dry_run(self, target: str, *, isolated: bool = True) -> str:
        result = subprocess.run(
            ["make", "-n", target, "arch=x86_64"],
            cwd=ROOT,
            env=self._environment(isolated=isolated),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def test_python_path_uses_default_and_environment_override(self):
        with mock.patch.dict(os.environ, {"MOZBUILD_STATE_PATH": ""}), mock.patch.object(
            Path, "home", return_value=self.mock_home
        ):
            self.assertEqual(
                multibuild.get_mozbuild_state_path(),
                self.mock_home / ".mozbuild",
            )

        with mock.patch.dict(
            os.environ,
            {"MOZBUILD_STATE_PATH": str(self.isolated)},
        ):
            self.assertEqual(multibuild.get_mozbuild_state_path(), self.isolated)

    def test_linux_sysroot_fix_is_confined_to_isolated_state(self):
        relative = Path(
            "sysroot-x86_64-linux-gnu/usr/lib/x86_64-linux-gnu"
        )
        isolated_lib = self.isolated / relative
        default_lib = self.mock_home / ".mozbuild" / relative
        for directory in (isolated_lib, default_lib):
            directory.mkdir(parents=True)
            (directory / "libsqlite3.so.0").touch()

        with mock.patch.dict(
            os.environ,
            {"MOZBUILD_STATE_PATH": str(self.isolated)},
        ):
            multibuild.setup_linux_sysroots()

        link = isolated_lib / "libsqlite3.so"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "libsqlite3.so.0")
        self.assertFalse((default_lib / "libsqlite3.so").exists())

    def test_make_targets_use_isolated_state(self):
        for target in ("mozbootstrap", "setup-macos-sdk", "package-windows"):
            with self.subTest(target=target):
                output = self._make_dry_run(target)
                self.assertIn(str(self.isolated), output)
                self.assertNotIn(str(Path.home() / ".mozbuild"), output)

    def test_make_default_remains_home_mozbuild(self):
        output = self._make_dry_run("mozbootstrap", isolated=False)
        self.assertIn(str(Path.home() / ".mozbuild"), output)

        environment = self._environment(isolated=False)
        environment["MOZBUILD_STATE_PATH"] = ""
        result = subprocess.run(
            ["make", "-n", "mozbootstrap", "arch=x86_64"],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn(str(Path.home() / ".mozbuild"), result.stdout)

    def test_macos_mozconfig_uses_isolated_sdk(self):
        script = """
uname() { printf '%s\\n' 'Linux'; }
ac_add_options() { printf '%s\\n' "$*"; }
. "$1"
"""
        result = subprocess.run(
            ["/bin/sh", "-c", script, "sh", str(ROOT / "assets/macos.mozconfig")],
            env=self._environment(isolated=True),
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn(
            f"--with-macos-sdk={self.isolated}/MacOSX26.5.sdk",
            result.stdout,
        )


if __name__ == "__main__":
    unittest.main()
