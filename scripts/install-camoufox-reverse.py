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
import re


CAPABILITIES_FILE = "camoufox-reverse-capabilities.json"
EXPECTED_DISTRIBUTION = "WhiteNightShadow/camoufox-reverse"
EXPECTED_REVERSE_RELEASE = "reverse.4"
REQUIRED_TRACE_FEATURES = {
    "async_buffered_io",
    "event_kind",
    "native_site",
    "wall_time_us",
    "sequence",
    "exclusive_session_files",
    "control_ack",
    "utf8_paths",
    "process_scope",
}
MAX_MEMBERS = 50_000
MAX_TOTAL_SIZE = 4 * 1024 * 1024 * 1024
MAX_SINGLE_FILE_SIZE = 2 * 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 500
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
BUILD_RE = re.compile(r"^[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*$")
ASSET_RE = re.compile(
    r"^camoufox-(?P<version>[^-]+)-(?P<build>[^-]+)-"
    r"(?P<platform>lin|mac|win)\.(?P<arch>x86_64|arm64)\.zip$"
)
PLATFORM_EXECUTABLE = {
    "lin": "camoufox-bin",
    "mac": "Camoufox.app/Contents/MacOS/camoufox",
    "win": "camoufox.exe",
}


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
    if len(members) > MAX_MEMBERS:
        raise InstallError(f"archive has too many entries: {len(members)}")
    total_size = 0
    for member in members:
        path = Path(member.filename)
        if path.is_absolute() or ".." in path.parts:
            raise InstallError(f"unsafe archive path: {member.filename}")
        unix_mode = member.external_attr >> 16
        if unix_mode and stat.S_ISLNK(unix_mode):
            raise InstallError(f"archive symlinks are not supported: {member.filename}")
        if member.file_size > MAX_SINGLE_FILE_SIZE:
            raise InstallError(f"archive member is too large: {member.filename}")
        total_size += member.file_size
        if total_size > MAX_TOTAL_SIZE:
            raise InstallError("archive expands beyond the 4 GiB safety limit")
        if (
            member.file_size > 1024 * 1024
            and member.compress_size > 0
            and member.file_size / member.compress_size > MAX_COMPRESSION_RATIO
        ):
            raise InstallError(f"suspicious compression ratio: {member.filename}")
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
    asset_match = ASSET_RE.fullmatch(archive_path.name)
    if not asset_match:
        raise InstallError(
            "archive filename must match "
            "camoufox-<version>-<build>-<lin|mac|win>.<arch>.zip"
        )
    digest = _sha256(archive_path)
    if not expected_sha256:
        raise InstallError("expected SHA256 is required")
    if digest.lower() != expected_sha256.lower():
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
        if capabilities.get("distribution") != EXPECTED_DISTRIBUTION:
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
        reverse_release = str(capabilities.get("reverse_release") or "").strip()
        if not version or not build:
            raise InstallError("archive version.json is missing version/build")
        if not VERSION_RE.fullmatch(version):
            raise InstallError(f"invalid version metadata: {version!r}")
        if not BUILD_RE.fullmatch(build):
            raise InstallError(f"invalid build metadata: {build!r}")
        if not BUILD_RE.fullmatch(reverse_release):
            raise InstallError(f"invalid reverse_release metadata: {reverse_release!r}")
        if version != asset_match.group("version") or build != asset_match.group("build"):
            raise InstallError("archive filename and version.json do not agree")
        if capabilities.get("schema") != 1:
            raise InstallError("unsupported capability schema")
        if capabilities.get("upstream_version") != f"{version}-{build}":
            raise InstallError("capability marker and version.json do not agree")
        if reverse_release != EXPECTED_REVERSE_RELEASE:
            raise InstallError(
                f"expected {EXPECTED_REVERSE_RELEASE}, got {reverse_release or 'missing'}"
            )
        if capabilities.get("property_trace_protocol") != 1:
            raise InstallError("unsupported PropertyTracer protocol")
        if capabilities.get("property_trace_hooks") != 75:
            raise InstallError("archive does not contain the expected 75 trace hooks")
        features = capabilities.get("property_trace_features")
        if not isinstance(features, list):
            raise InstallError("archive does not declare PropertyTracer feature metadata")
        missing_features = REQUIRED_TRACE_FEATURES - {str(item) for item in features}
        if missing_features:
            raise InstallError(
                "archive is missing PropertyTracer features: "
                + ", ".join(sorted(missing_features))
            )
        names = {member.filename.rstrip("/") for member in members}
        executable = PLATFORM_EXECUTABLE[asset_match.group("platform")]
        if executable not in names:
            raise InstallError(f"archive is missing platform executable: {executable}")
        folder = f"{version}-{build}-{reverse_release}"
        repo_dir = cache / "browsers" / "whitenightshadow"
        destination = (repo_dir / folder).resolve()
        if destination.parent != repo_dir.resolve():
            raise InstallError("installation metadata escapes the repository directory")
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
    parser.add_argument("--sha256", required=True, help="expected SHA256 from SHA256SUMS")
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
