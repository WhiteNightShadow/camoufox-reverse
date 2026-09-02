#!/usr/bin/env python3
"""Install one verified Camoufox Reverse archive beside the active browser.

The installer is intentionally local-only: it never downloads an archive,
changes Camoufox's active config, or migrates a legacy 0.4 flat cache.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path


CAPABILITIES_FILE = "camoufox-reverse-capabilities.json"


class InstallError(RuntimeError):
    """The archive or local Camoufox cache is unsafe for installation."""


def default_cache_dir() -> Path:
    try:
        from platformdirs import user_cache_dir

        return Path(user_cache_dir("camoufox"))
    except ImportError:
        if sys.platform == "darwin":
            return Path.home() / "Library" / "Caches" / "camoufox"
        if sys.platform == "win32":
            local = os.environ.get("LOCALAPPDATA")
            if not local:
                raise InstallError("LOCALAPPDATA is not set")
            return Path(local) / "camoufox" / "camoufox" / "Cache"
        return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "camoufox"


def _read_json_from_zip(archive: zipfile.ZipFile, name: str) -> dict:
    try:
        value = json.loads(archive.read(name))
    except KeyError as exc:
        raise InstallError(f"archive is missing {name}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InstallError(f"archive has invalid {name}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"archive {name} must contain a JSON object")
    return value


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    for member in members:
        path = Path(member.filename)
        if path.is_absolute() or ".." in path.parts:
            raise InstallError(f"unsafe archive path: {member.filename}")
        unix_mode = member.external_attr >> 16
        if unix_mode and stat.S_ISLNK(unix_mode):
            raise InstallError(f"archive symlinks are not supported: {member.filename}")
    return members


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_archive(
    archive_path: str | Path,
    *,
    cache_dir: str | Path | None = None,
    expected_sha256: str | None = None,
) -> dict:
    archive_path = Path(archive_path).expanduser().resolve()
    if not archive_path.is_file():
        raise InstallError(f"archive not found: {archive_path}")
    digest = _sha256(archive_path)
    if expected_sha256 and digest.lower() != expected_sha256.lower():
        raise InstallError(
            f"SHA256 mismatch: expected {expected_sha256.lower()}, got {digest}"
        )

    cache = Path(cache_dir).expanduser().resolve() if cache_dir else default_cache_dir()
    compat_flag = cache / ".0.5_FLAG"
    if cache.exists() and any(cache.iterdir()) and not compat_flag.exists():
        raise InstallError(
            "legacy Camoufox 0.4 flat cache detected; run the official Camoufox "
            "0.5 migration/backup flow first. No files were changed."
        )
    if not compat_flag.exists():
        raise InstallError(
            "Camoufox 0.5 cache is not initialized. Install/fetch one official "
            "browser first so an active same-major browser remains available."
        )

    with zipfile.ZipFile(archive_path) as archive:
        members = _safe_members(archive)
        version_data = _read_json_from_zip(archive, "version.json")
        capabilities = _read_json_from_zip(archive, CAPABILITIES_FILE)
        if capabilities.get("distribution") != "WhiteNightShadow/camoufox-reverse":
            raise InstallError("archive capability marker has an unexpected distribution")
        if not capabilities.get("property_trace"):
            raise InstallError("archive does not declare PropertyTracer support")
        version = str(version_data.get("version") or "").strip()
        build = str(
            version_data.get("build")
            or version_data.get("release")
            or version_data.get("tag")
            or ""
        ).strip()
        reverse_release = str(capabilities.get("reverse_release") or "reverse.1").strip()
        if not version or not build:
            raise InstallError("archive version.json is missing version/build")
        folder = f"{version}-{build}-{reverse_release}"
        repo_dir = cache / "browsers" / "whitenightshadow"
        destination = repo_dir / folder
        if destination.exists():
            raise InstallError(f"destination already exists: {destination}")

        repo_dir.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".reverse-install-", dir=repo_dir))
        try:
            archive.extractall(stage, members=members)
            if not (stage / "version.json").is_file():
                raise InstallError("extracted archive lost version.json")
            if not (stage / CAPABILITIES_FILE).is_file():
                raise InstallError("extracted archive lost capability marker")
            if os.name != "nt":
                for path in stage.rglob("*"):
                    path.chmod(0o755)
            stage.rename(destination)
        except Exception:
            if stage.exists():
                shutil.rmtree(stage)
            raise

    selector = f"whitenightshadow/{folder}"
    return {
        "status": "installed",
        "path": str(destination),
        "selector": selector,
        "version": version,
        "build": build,
        "sha256": digest,
        "active_config_changed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", help="downloaded camoufox-*.zip release asset")
    parser.add_argument("--sha256", help="expected SHA256 from SHA256SUMS")
    parser.add_argument("--cache-dir", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        result = install_archive(
            args.archive,
            cache_dir=args.cache_dir,
            expected_sha256=args.sha256,
        )
    except (InstallError, OSError, zipfile.BadZipFile) as exc:
        print(f"Camoufox Reverse install failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    print(
        "Use: launch_browser(browser_version=\""
        + result["selector"]
        + "\", enable_trace=True)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
