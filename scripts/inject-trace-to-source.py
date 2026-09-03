#!/usr/bin/env python3
"""Fail-closed, atomic and idempotent PropertyTracer injector for Firefox 152."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

DEFAULT_EXPECT_VERSION = "152.0.4-beta.30"
DEFAULT_EXPECT_HOOKS = 75
GET = 0
SET = 1
CALL = 2
INCLUDE_LINE = '#include "PropertyTracer.hpp"'
LOCAL_INCLUDE_LINE = 'LOCAL_INCLUDES += ["/camoucfg"]'
ROOT_DIR_LINE = 'DIRS += ["camoucfg"]'


class InjectionError(RuntimeError):
    """The pinned source layout does not match the injection manifest."""


@dataclass(frozen=True)
class Hook:
    path: str
    signature: str
    object_name: str
    property_name: str
    kind: int = GET

    @property
    def site_id(self) -> str:
        # The same observable path can exist in Window and Worker bindings.
        # Include the source path in the marker identity without changing the
        # JSONL object/property contract consumed by the MCP.
        return f"{self.object_name}.{self.property_name}@{self.path}"

    @property
    def marker(self) -> str:
        return f"/* PropertyTracer injected: {self.site_id} */"

    @property
    def record(self) -> str:
        return (
            "camou::PropertyTracer::Instance().Record("
            f'"{self.object_name}", "{self.property_name}", nullptr, '
            f'{self.kind}, "{self.site_id}");'
        )


def _hook(
    path: str,
    cls: str,
    func: str,
    obj: str,
    prop: str,
    kind: int = GET,
) -> Hook:
    return Hook(
        path,
        rf"\b{re.escape(cls)}::{re.escape(func)}\s*\(",
        obj,
        prop,
        kind,
    )


WEBGL_GET_EXTENSION_HOOK = Hook(
    "dom/canvas/WebGLContextExtensions.cpp",
    r"void\s+ClientWebGLContext::GetExtension\s*\(\s*JSContext\s*\*\s*cx\s*,",
    "webgl",
    "getExtension",
    CALL,
)


HOOKS: tuple[Hook, ...] = (
    Hook("dom/base/Navigator.cpp", r"Navigator::GetUserAgent\s*\(\s*nsAString\s*&", "navigator", "userAgent"),
    _hook("dom/base/Navigator.cpp", "Navigator", "GetAppCodeName", "navigator", "appCodeName"),
    Hook("dom/base/Navigator.cpp", r"Navigator::GetAppVersion\s*\(\s*nsAString\s*&\s*aAppVersion\s*,\s*CallerType", "navigator", "appVersion"),
    _hook("dom/base/Navigator.cpp", "Navigator", "GetAppName", "navigator", "appName"),
    _hook("dom/base/Navigator.cpp", "Navigator", "GetLanguage", "navigator", "language"),
    Hook("dom/base/Navigator.cpp", r"Navigator::GetPlatform\s*\(\s*nsAString\s*&\s*aPlatform\s*,\s*CallerType", "navigator", "platform"),
    _hook("dom/base/Navigator.cpp", "Navigator", "GetOscpu", "navigator", "oscpu"),
    _hook("dom/base/Navigator.cpp", "Navigator", "GetProduct", "navigator", "product"),
    _hook("dom/base/Navigator.cpp", "Navigator", "GetProductSub", "navigator", "productSub"),
    _hook("dom/base/Navigator.cpp", "Navigator", "GetBuildID", "navigator", "buildID"),
    _hook("dom/base/Navigator.cpp", "Navigator", "GetDoNotTrack", "navigator", "doNotTrack"),
    _hook("dom/base/Navigator.cpp", "Navigator", "HardwareConcurrency", "navigator", "hardwareConcurrency"),
    _hook("dom/base/Navigator.cpp", "Navigator", "MaxTouchPoints", "navigator", "maxTouchPoints"),
    _hook("dom/base/Navigator.cpp", "Navigator", "CookieEnabled", "navigator", "cookieEnabled"),
    _hook("dom/base/Navigator.cpp", "Navigator", "OnLine", "navigator", "onLine"),
    _hook("dom/base/Navigator.cpp", "Navigator", "PdfViewerEnabled", "navigator", "pdfViewerEnabled"),
    _hook("dom/base/Navigator.cpp", "Navigator", "GlobalPrivacyControl", "navigator", "globalPrivacyControl"),
    _hook("dom/base/nsScreen.cpp", "nsScreen", "PixelDepth", "screen", "pixelDepth"),
    _hook("dom/base/nsScreen.cpp", "nsScreen", "GetRect", "screen", "rect"),
    _hook("dom/base/nsScreen.cpp", "nsScreen", "GetAvailRect", "screen", "availRect"),
    Hook("dom/base/nsGlobalWindowInner.cpp", r"double\s+nsGlobalWindowInner::GetInnerWidth\s*\(\s*ErrorResult\s*&", "window", "innerWidth"),
    Hook("dom/base/nsGlobalWindowInner.cpp", r"double\s+nsGlobalWindowInner::GetInnerHeight\s*\(\s*ErrorResult\s*&", "window", "innerHeight"),
    _hook("dom/base/nsGlobalWindowInner.cpp", "nsGlobalWindowInner", "GetOuterWidth", "window", "outerWidth"),
    _hook("dom/base/nsGlobalWindowInner.cpp", "nsGlobalWindowInner", "GetOuterHeight", "window", "outerHeight"),
    _hook("dom/base/nsGlobalWindowInner.cpp", "nsGlobalWindowInner", "GetScreenX", "window", "screenX"),
    _hook("dom/base/nsGlobalWindowInner.cpp", "nsGlobalWindowInner", "GetScreenY", "window", "screenY"),
    _hook("dom/base/nsGlobalWindowInner.cpp", "nsGlobalWindowInner", "GetDevicePixelRatio", "window", "devicePixelRatio"),
    _hook("dom/base/nsGlobalWindowInner.cpp", "nsGlobalWindowInner", "GetScrollMinX", "window", "scrollMinX"),
    _hook("dom/base/nsGlobalWindowInner.cpp", "nsGlobalWindowInner", "GetScrollMinY", "window", "scrollMinY"),
    _hook("dom/base/nsGlobalWindowInner.cpp", "nsGlobalWindowInner", "GetScrollMaxX", "window", "scrollMaxX"),
    _hook("dom/base/nsGlobalWindowInner.cpp", "nsGlobalWindowInner", "GetScrollMaxY", "window", "scrollMaxY"),
    _hook("dom/base/nsGlobalWindowInner.cpp", "nsGlobalWindowInner", "GetScrollX", "window", "scrollX"),
    _hook("dom/base/nsGlobalWindowInner.cpp", "nsGlobalWindowInner", "GetScrollY", "window", "scrollY"),
    _hook("dom/workers/WorkerNavigator.cpp", "WorkerNavigator", "GetUserAgent", "navigator", "userAgent"),
    _hook("dom/workers/WorkerNavigator.cpp", "WorkerNavigator", "GetAppVersion", "navigator", "appVersion"),
    _hook("dom/workers/WorkerNavigator.cpp", "WorkerNavigator", "GetPlatform", "navigator", "platform"),
    _hook("dom/workers/WorkerNavigator.cpp", "WorkerNavigator", "HardwareConcurrency", "navigator", "hardwareConcurrency"),
    _hook("dom/workers/WorkerNavigator.cpp", "WorkerNavigator", "GlobalPrivacyControl", "navigator", "globalPrivacyControl"),
    _hook("dom/base/nsHistory.cpp", "nsHistory", "GetLength", "history", "length"),
    _hook("dom/battery/BatteryManager.cpp", "BatteryManager", "Charging", "battery", "charging"),
    _hook("dom/battery/BatteryManager.cpp", "BatteryManager", "ChargingTime", "battery", "chargingTime"),
    _hook("dom/battery/BatteryManager.cpp", "BatteryManager", "DischargingTime", "battery", "dischargingTime"),
    _hook("dom/battery/BatteryManager.cpp", "BatteryManager", "Level", "battery", "level"),
    _hook("dom/canvas/CanvasRenderingContext2D.cpp", "CanvasRenderingContext2D", "GetImageData", "canvas2d", "getImageData", CALL),
    _hook("dom/html/HTMLCanvasElement.cpp", "HTMLCanvasElement", "ToDataURL", "canvas", "toDataURL", CALL),
    _hook("dom/html/HTMLCanvasElement.cpp", "HTMLCanvasElement", "ToBlob", "canvas", "toBlob", CALL),
    Hook("dom/html/HTMLCanvasElement.cpp", r"already_AddRefed<nsISupports>\s+HTMLCanvasElement::GetContext\s*\(\s*JSContext\s*\*", "canvas", "getContext", CALL),
    _hook("dom/canvas/ClientWebGLContext.cpp", "ClientWebGLContext", "GetParameter", "webgl", "getParameter", CALL),
    _hook("dom/canvas/ClientWebGLContext.cpp", "ClientWebGLContext", "GetSupportedExtensions", "webgl", "getSupportedExtensions", CALL),
    WEBGL_GET_EXTENSION_HOOK,
    _hook("dom/canvas/ClientWebGLContext.cpp", "ClientWebGLContext", "GetShaderPrecisionFormat", "webgl", "getShaderPrecisionFormat", CALL),
    _hook("dom/media/webaudio/AudioContext.cpp", "AudioContext", "OutputLatency", "audioContext", "outputLatency"),
    _hook("dom/base/nsPluginArray.cpp", "nsPluginArray", "IndexedGetter", "navigator.plugins", "indexedGetter"),
    _hook("dom/base/nsPluginArray.cpp", "nsPluginArray", "NamedGetter", "navigator.plugins", "namedGetter"),
    _hook("dom/base/nsMimeTypeArray.cpp", "nsMimeTypeArray", "IndexedGetter", "navigator.mimeTypes", "indexedGetter"),
    _hook("dom/base/nsMimeTypeArray.cpp", "nsMimeTypeArray", "NamedGetter", "navigator.mimeTypes", "namedGetter"),
    _hook("dom/performance/PerformanceMainThread.cpp", "PerformanceMainThread", "Timing", "performance", "timing"),
    _hook("dom/base/Document.cpp", "Document", "GetCookie", "document", "cookie.get"),
    _hook("dom/base/Document.cpp", "Document", "SetCookie", "document", "cookie.set", SET),
    Hook("dom/media/webrtc/jsapi/PeerConnectionImpl.cpp", r"PeerConnectionImpl::CreateOffer\s*\(\s*const\s+RTCOfferOptions\s*&", "webrtc", "createOffer", CALL),
    _hook("dom/media/webrtc/jsapi/PeerConnectionImpl.cpp", "PeerConnectionImpl", "CreateAnswer", "webrtc", "createAnswer", CALL),
    Hook("dom/media/webrtc/jsapi/PeerConnectionImpl.cpp", r"already_AddRefed<RTCDataChannel>\s+PeerConnectionImpl::CreateDataChannel\s*\(", "webrtc", "createDataChannel", CALL),
    _hook("dom/media/MediaDevices.cpp", "MediaDevices", "EnumerateDevices", "mediaDevices", "enumerateDevices", CALL),
    Hook("dom/media/MediaDevices.cpp", r"already_AddRefed<Promise>\s+MediaDevices::GetUserMedia\s*\(", "mediaDevices", "getUserMedia", CALL),
    _hook("dom/localstorage/LSObject.cpp", "LSObject", "GetItem", "localStorage", "getItem", CALL),
    _hook("dom/localstorage/LSObject.cpp", "LSObject", "SetItem", "localStorage", "setItem", CALL),
    _hook("dom/storage/SessionStorage.cpp", "SessionStorage", "GetItem", "sessionStorage", "getItem", CALL),
    _hook("dom/storage/SessionStorage.cpp", "SessionStorage", "SetItem", "sessionStorage", "setItem", CALL),
    _hook("layout/style/FontFaceSet.cpp", "FontFaceSet", "Check", "fonts", "check", CALL),
    _hook("layout/style/FontFaceSet.cpp", "FontFaceSet", "ForEach", "fonts", "forEach", CALL),
    _hook("dom/canvas/OffscreenCanvas.cpp", "OffscreenCanvas", "GetContext", "offscreenCanvas", "getContext", CALL),
    _hook("dom/canvas/OffscreenCanvas.cpp", "OffscreenCanvas", "TransferToImageBitmap", "offscreenCanvas", "transferToImageBitmap", CALL),
    Hook("dom/geolocation/Geolocation.cpp", r"void\s+Geolocation::GetCurrentPosition\s*\(\s*PositionCallback\s*&", "geolocation", "getCurrentPosition", CALL),
    Hook("dom/geolocation/Geolocation.cpp", r"int32_t\s+Geolocation::WatchPosition\s*\(\s*PositionCallback\s*&", "geolocation", "watchPosition", CALL),
)

AUDIO_SITE = "audioContext.sampleRate"
AUDIO_MARKER = f"/* PropertyTracer injected: {AUDIO_SITE} */"
AUDIO_INLINE = "float SampleRate() const { return mSampleRate; }"
AUDIO_DECL = "float SampleRate() const;"
AUDIO_DEF = "float AudioContext::SampleRate() const {"
AUDIO_ANCHOR = "double AudioContext::OutputLatency() {"
DEPRECATED_SITE_PATHS = {
    "dom/storage/LocalStorage.cpp": (
        "localStorage.getItem@dom/storage/LocalStorage.cpp",
        "localStorage.setItem@dom/storage/LocalStorage.cpp",
    ),
}


class SourcePlan:
    """An edit plan that keeps the filesystem untouched until validation passes."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.original: dict[Path, str] = {}
        self.current: dict[Path, str] = {}

    def path(self, relative: str) -> Path:
        result = (self.root / relative).resolve()
        try:
            result.relative_to(self.root)
        except ValueError as exc:
            raise InjectionError(f"path escapes source root: {relative}") from exc
        return result

    def read(self, relative: str) -> str:
        path = self.path(relative)
        if path in self.current:
            return self.current[path]
        if not path.is_file():
            raise InjectionError(f"required file not found: {relative}")
        with path.open("r", encoding="utf-8", newline="") as handle:
            text = handle.read()
        self.original[path] = text
        self.current[path] = text
        return text

    def set(self, relative: str, text: str) -> None:
        path = self.path(relative)
        if path not in self.current:
            self.read(relative)
        self.current[path] = text

    def changes(self) -> dict[Path, tuple[str, str]]:
        return {
            path: (self.original[path], text)
            for path, text in self.current.items()
            if text != self.original[path]
        }


def _reject_deprecated_sites(plan: SourcePlan) -> None:
    """Fail closed when an older injected source tree is reused.

    The injector is intentionally additive and cannot infer whether arbitrary
    old native records are safe to remove. A reverse.4 tree contains two
    LocalStorage markers outside the reverse.5 manifest, so building on it
    would silently leave 77 physical sites while advertising 75.
    """

    found: list[str] = []
    for relative, sites in DEPRECATED_SITE_PATHS.items():
        path = plan.path(relative)
        if not path.is_file():
            continue
        text = plan.read(relative)
        found.extend(site for site in sites if site in text)
    if found:
        raise InjectionError(
            "deprecated PropertyTracer sites found; use a clean Firefox source tree: "
            + ", ".join(sorted(found))
        )


def _ensure_include(text: str) -> str:
    count = text.count(INCLUDE_LINE)
    if count > 1:
        raise InjectionError(f"duplicate include: {INCLUDE_LINE}")
    if count == 1:
        return text
    mask = '#include "MaskConfig.hpp"'
    if mask in text:
        return text.replace(mask, mask + "\n" + INCLUDE_LINE, 1)
    match = re.search(r"(?m)^\s*#include\b", text)
    if not match:
        raise InjectionError("source file has no #include anchor")
    return text[: match.start()] + INCLUDE_LINE + "\n" + text[match.start() :]


def _body_brace(text: str, match: re.Match[str], site: str) -> int:
    """Find a definition body, rejecting declarations and malformed signatures."""

    opening = text.find("(", match.start(), match.end())
    if opening < 0:
        raise InjectionError(f"signature has no opening parenthesis: {site}")
    depth = 0
    quote: str | None = None
    escaped = False
    line_comment = False
    block_comment = False
    closing = -1
    i = opening
    while i < len(text):
        char = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if line_comment:
            line_comment = char != "\n"
            i += 1
            continue
        if block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                i += 2
            else:
                i += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            i += 1
            continue
        if char == "/" and nxt == "/":
            line_comment = True
            i += 2
            continue
        if char == "/" and nxt == "*":
            block_comment = True
            i += 2
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                closing = i
                break
        i += 1
    if closing < 0:
        raise InjectionError(f"unbalanced signature: {site}")
    tail = text[closing + 1 : closing + 1001]
    brace = tail.find("{")
    semicolon = tail.find(";")
    if brace < 0 or (semicolon >= 0 and semicolon < brace):
        raise InjectionError(f"function body not found: {site}")
    return closing + 1 + brace


def _apply_hook(plan: SourcePlan, hook: Hook) -> str:
    text = plan.read(hook.path)
    marker_count = text.count(hook.marker)
    record_count = text.count(hook.record)
    if marker_count > 1 or record_count > 1:
        raise InjectionError(
            f"duplicate {hook.site_id}: marker={marker_count}, record={record_count}"
        )
    if marker_count or record_count:
        if marker_count == record_count == 1:
            plan.set(hook.path, _ensure_include(text))
            return "already"
        raise InjectionError(
            f"partial {hook.site_id}: marker={marker_count}, record={record_count}"
        )
    matches = list(re.finditer(hook.signature, text, re.MULTILINE))
    if len(matches) != 1:
        raise InjectionError(
            f"{hook.site_id} in {hook.path} matched {len(matches)} times; expected 1"
        )
    brace = _body_brace(text, matches[0], hook.site_id)
    insertion = f"\n  {hook.record} {hook.marker}"
    text = text[: brace + 1] + insertion + text[brace + 1 :]
    plan.set(hook.path, _ensure_include(text))
    return "applied"


def _apply_audio(plan: SourcePlan) -> str:
    header_path = "dom/media/webaudio/AudioContext.h"
    source_path = "dom/media/webaudio/AudioContext.cpp"
    header = plan.read(header_path)
    source = plan.read(source_path)
    record = (
        'camou::PropertyTracer::Instance().Record('
        '"audioContext", "sampleRate", nullptr, 0, '
        '"audioContext.sampleRate@dom/media/webaudio/AudioContext.cpp");'
    )
    counts = (
        header.count(AUDIO_INLINE),
        header.count(AUDIO_DECL),
        source.count(AUDIO_DEF),
        source.count(AUDIO_MARKER),
        source.count(record),
    )
    fresh = counts == (1, 0, 0, 0, 0)
    already = counts == (0, 1, 1, 1, 1)
    if not fresh and not already:
        raise InjectionError(f"AudioContext.sampleRate missing/ambiguous/partial: {counts}")
    if fresh:
        if source.count(AUDIO_ANCHOR) != 1:
            raise InjectionError("AudioContext OutputLatency anchor must occur once")
        header = header.replace(AUDIO_INLINE, AUDIO_DECL, 1)
        definition = (
            "float AudioContext::SampleRate() const {\n"
            f"  {record} {AUDIO_MARKER}\n"
            "  return mSampleRate;\n"
            "}\n\n"
        )
        source = source.replace(AUDIO_ANCHOR, definition + AUDIO_ANCHOR, 1)
    plan.set(header_path, header)
    plan.set(source_path, _ensure_include(source))
    return "already" if already else "applied"


def _ensure_local_include(plan: SourcePlan, source_path: str) -> None:
    mozbuild = str(Path(source_path).parent / "moz.build")
    text = plan.read(mozbuild)
    count = text.count("/camoucfg")
    if count > 1:
        raise InjectionError(f"duplicate /camoucfg in {mozbuild}")
    if count == 0:
        newline = "" if text.endswith("\n") else "\n"
        plan.set(
            mozbuild,
            text + newline + "\n# PropertyTracer\n" + LOCAL_INCLUDE_LINE + "\n",
        )


def _ensure_root_dir(plan: SourcePlan) -> None:
    text = plan.read("moz.build")
    pattern = re.compile(
        r"(?m)^\s*DIRS\s*\+=\s*\[\s*['\"]camoucfg['\"]\s*\]\s*$"
    )
    count = len(pattern.findall(text))
    if count > 1:
        raise InjectionError("duplicate root camoucfg DIRS entry")
    if count == 0:
        newline = "" if text.endswith("\n") else "\n"
        plan.set("moz.build", text + newline + "\n" + ROOT_DIR_LINE + "\n")


def _atomic_write(path: Path, text: str, mode: int) -> None:
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.property-tracer.",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, stat.S_IMODE(mode))
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _commit(plan: SourcePlan) -> None:
    changes = plan.changes()
    committed: list[Path] = []
    try:
        for path in sorted(changes, key=str):
            old, new = changes[path]
            _atomic_write(path, new, path.stat().st_mode)
            committed.append(path)
    except Exception:
        for path in reversed(committed):
            try:
                _atomic_write(path, changes[path][0], path.stat().st_mode)
            except Exception:
                pass
        raise


def run_injection(
    source_root: str | Path,
    *,
    mode: str = "apply",
    expect_version: str = DEFAULT_EXPECT_VERSION,
    expect_hooks: int = DEFAULT_EXPECT_HOOKS,
    hooks: Sequence[Hook] | None = None,
    include_audio_sample_rate: bool = True,
    ensure_build_files: bool = True,
) -> dict[str, object]:
    """Validate, plan and optionally apply the injection.

    The injectable manifest arguments are intentionally public for small,
    network-free unit tests.  The CLI always uses the full pinned manifest.
    """

    if mode not in {"apply", "check", "verify"}:
        raise ValueError(f"unsupported mode: {mode}")
    root = Path(source_root)
    if not root.is_dir():
        raise InjectionError(f"source directory not found: {root}")
    selected = tuple(HOOKS if hooks is None else hooks)
    total = len(selected) + int(include_audio_sample_rate)
    if total != expect_hooks:
        raise InjectionError(f"manifest has {total} hooks; expected {expect_hooks}")
    sites = [hook.site_id for hook in selected]
    if include_audio_sample_rate:
        sites.append(AUDIO_SITE)
    duplicates = sorted({site for site in sites if sites.count(site) > 1})
    if duplicates:
        raise InjectionError(f"duplicate site ids: {', '.join(duplicates)}")

    plan = SourcePlan(root)
    actual_version = plan.read("browser/config/version.txt").strip()
    if actual_version != expect_version:
        raise InjectionError(
            f"source version {actual_version!r}; expected {expect_version!r}"
        )
    _reject_deprecated_sites(plan)

    applied = already = 0
    source_paths: set[str] = set()
    for hook in selected:
        status = _apply_hook(plan, hook)
        applied += status == "applied"
        already += status == "already"
        source_paths.add(hook.path)
    if include_audio_sample_rate:
        status = _apply_audio(plan)
        applied += status == "applied"
        already += status == "already"
        source_paths.update(
            {
                "dom/media/webaudio/AudioContext.h",
                "dom/media/webaudio/AudioContext.cpp",
            }
        )
    if applied + already != expect_hooks:
        raise InjectionError(
            f"postcondition failed: applied={applied}, already={already}, expected={expect_hooks}"
        )
    if ensure_build_files:
        for source_path in sorted(source_paths):
            _ensure_local_include(plan, source_path)
        _ensure_root_dir(plan)

    changes = plan.changes()
    if mode == "verify" and changes:
        names = ", ".join(
            str(path.relative_to(plan.root)) for path in sorted(changes, key=str)
        )
        raise InjectionError(f"verification requires changes: {names}")
    if mode == "apply":
        _commit(plan)
    return {
        "mode": mode,
        "source": str(plan.root),
        "version": expect_version,
        "expected": expect_hooks,
        "applied": applied,
        "already": already,
        "files_changed": [
            str(path.relative_to(plan.root))
            for path in sorted(changes, key=str)
        ],
    }


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="apply the validated plan (default; explicit for build scripts)",
    )
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="compatibility flag; validation is always strict and fail-closed",
    )
    parser.add_argument("--expect-version", default=DEFAULT_EXPECT_VERSION)
    parser.add_argument("--expect-hooks", type=int, default=DEFAULT_EXPECT_HOOKS)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    selected_mode = "verify" if args.verify else "check" if args.check else "apply"
    try:
        result = run_injection(
            args.source,
            mode=selected_mode,
            expect_version=args.expect_version,
            expect_hooks=args.expect_hooks,
        )
    except InjectionError as exc:
        print(f"PropertyTracer injection failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
