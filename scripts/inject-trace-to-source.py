#!/usr/bin/env python3
"""
在已打 patch 的 Firefox 源码中，批量在 DOM getter 入口注入 PropertyTracer::Record 调用。
在 make dir 之后、make build 之前执行。

用法：python3 scripts/inject-trace-to-source.py <firefox-source-dir>
示例：python3 scripts/inject-trace-to-source.py camoufox-135.0.1-beta.24

覆盖 75 个 DOM getter/setter/call：
  Navigator(17) Screen(3) Window(13) WorkerNavigator(5) History(1) Battery(4)
  Canvas2D(1) HTMLCanvasElement(3) WebGL(4) AudioContext(2)
  Plugins(2) MimeTypes(2) Performance(1) Document.cookie(2)
  WebRTC(3) MediaDevices(2) LocalStorage(2) SessionStorage(2)
  Fonts(2) OffscreenCanvas(2) Geolocation(2)
"""
import re
import sys
import os

MARKER = "/* PropertyTracer injected */"
INCLUDE_LINE = '#include "PropertyTracer.hpp"'


def ensure_include(content: str) -> str:
    if INCLUDE_LINE in content:
        return content
    if '#include "MaskConfig.hpp"' in content:
        return content.replace(
            '#include "MaskConfig.hpp"',
            '#include "MaskConfig.hpp"\n' + INCLUDE_LINE, 1
        )
    # 没有 MaskConfig.hpp 的文件：在第一个 #include 之前插入
    # 这样保证在全局作用域，不会落入 namespace 内部
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("#include"):
            lines.insert(i, INCLUDE_LINE)
            return "\n".join(lines)
    # 没找到任何 include，插在文件开头
    return INCLUDE_LINE + "\n" + content


def ensure_local_includes(src_dir: str, rel_path: str):
    """确保文件所在目录的 moz.build 有 LOCAL_INCLUDES += ['/camoucfg']"""
    dir_path = os.path.dirname(os.path.join(src_dir, rel_path))
    mozbuild = os.path.join(dir_path, "moz.build")
    if not os.path.exists(mozbuild):
        return
    with open(mozbuild, "r") as f:
        content = f.read()
    if "/camoucfg" in content:
        return
    # 追加到文件末尾
    content += '\n\n# PropertyTracer\nLOCAL_INCLUDES += ["/camoucfg"]\n'
    with open(mozbuild, "w") as f:
        f.write(content)


def inject_record(content, class_name, func_name, trace_obj, trace_prop):
    record = f'  camou::PropertyTracer::Instance().Record("{trace_obj}", "{trace_prop}"); {MARKER}'
    if f'Record("{trace_obj}", "{trace_prop}")' in content:
        return content, False
    pattern = rf'{re.escape(class_name)}::{re.escape(func_name)}\s*\('
    match = re.search(pattern, content)
    if not match:
        return content, False
    brace_pos = content.find('{', match.start())
    if brace_pos < 0 or (brace_pos - match.start()) > 500:
        return content, False
    insert_pos = brace_pos + 1
    content = content[:insert_pos] + "\n" + record + content[insert_pos:]
    return content, True


def process_file(src_dir, rel_path, getters, label):
    filepath = os.path.join(src_dir, rel_path)
    if not os.path.exists(filepath):
        print(f"  [SKIP] {rel_path} not found")
        return
    with open(filepath, "r") as f:
        content = f.read()
    if MARKER in content:
        print(f"  [SKIP] {label} already injected")
        return
    # 确保 moz.build 有 /camoucfg include path
    ensure_local_includes(src_dir, rel_path)
    content = ensure_include(content)
    count = 0
    for cls, func, obj, prop in getters:
        content, changed = inject_record(content, cls, func, obj, prop)
        if changed:
            count += 1
    with open(filepath, "w") as f:
        f.write(content)
    print(f"  [OK]   {label}: {count}/{len(getters)} injected")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <firefox-source-dir>")
        sys.exit(1)
    src_dir = sys.argv[1]
    if not os.path.isdir(src_dir):
        print(f"Error: {src_dir} not found")
        sys.exit(1)

    print(f"=== Injecting PropertyTracer::Record into DOM getters ===")
    print(f"Source: {src_dir}\n")

    # Navigator
    process_file(src_dir, "dom/base/Navigator.cpp", [
        ("Navigator", "GetUserAgent", "navigator", "userAgent"),
        ("Navigator", "GetAppCodeName", "navigator", "appCodeName"),
        ("Navigator", "GetAppVersion", "navigator", "appVersion"),
        ("Navigator", "GetAppName", "navigator", "appName"),
        ("Navigator", "GetLanguage", "navigator", "language"),
        ("Navigator", "GetPlatform", "navigator", "platform"),
        ("Navigator", "GetOscpu", "navigator", "oscpu"),
        ("Navigator", "GetProduct", "navigator", "product"),
        ("Navigator", "GetProductSub", "navigator", "productSub"),
        ("Navigator", "GetBuildID", "navigator", "buildID"),
        ("Navigator", "GetDoNotTrack", "navigator", "doNotTrack"),
        ("Navigator", "HardwareConcurrency", "navigator", "hardwareConcurrency"),
        ("Navigator", "MaxTouchPoints", "navigator", "maxTouchPoints"),
        ("Navigator", "CookieEnabled", "navigator", "cookieEnabled"),
        ("Navigator", "OnLine", "navigator", "onLine"),
        ("Navigator", "PdfViewerEnabled", "navigator", "pdfViewerEnabled"),
        ("Navigator", "GlobalPrivacyControl", "navigator", "globalPrivacyControl"),
    ], "Navigator.cpp")

    # Screen
    process_file(src_dir, "dom/base/nsScreen.cpp", [
        ("nsScreen", "PixelDepth", "screen", "pixelDepth"),
        ("nsScreen", "GetRect", "screen", "rect"),
        ("nsScreen", "GetAvailRect", "screen", "availRect"),
    ], "nsScreen.cpp")

    # Window
    process_file(src_dir, "dom/base/nsGlobalWindowInner.cpp", [
        ("nsGlobalWindowInner", "GetInnerWidth", "window", "innerWidth"),
        ("nsGlobalWindowInner", "GetInnerHeight", "window", "innerHeight"),
        ("nsGlobalWindowInner", "GetOuterWidth", "window", "outerWidth"),
        ("nsGlobalWindowInner", "GetOuterHeight", "window", "outerHeight"),
        ("nsGlobalWindowInner", "GetScreenX", "window", "screenX"),
        ("nsGlobalWindowInner", "GetScreenY", "window", "screenY"),
        ("nsGlobalWindowInner", "GetDevicePixelRatio", "window", "devicePixelRatio"),
        ("nsGlobalWindowInner", "GetScrollMinX", "window", "scrollMinX"),
        ("nsGlobalWindowInner", "GetScrollMinY", "window", "scrollMinY"),
        ("nsGlobalWindowInner", "GetScrollMaxX", "window", "scrollMaxX"),
        ("nsGlobalWindowInner", "GetScrollMaxY", "window", "scrollMaxY"),
        ("nsGlobalWindowInner", "GetScrollX", "window", "scrollX"),
        ("nsGlobalWindowInner", "GetScrollY", "window", "scrollY"),
    ], "nsGlobalWindowInner.cpp")

    # WorkerNavigator
    process_file(src_dir, "dom/workers/WorkerNavigator.cpp", [
        ("WorkerNavigator", "GetUserAgent", "navigator", "userAgent"),
        ("WorkerNavigator", "GetAppVersion", "navigator", "appVersion"),
        ("WorkerNavigator", "GetPlatform", "navigator", "platform"),
        ("WorkerNavigator", "HardwareConcurrency", "navigator", "hardwareConcurrency"),
        ("WorkerNavigator", "GlobalPrivacyControl", "navigator", "globalPrivacyControl"),
    ], "WorkerNavigator.cpp")

    # History
    process_file(src_dir, "dom/base/nsHistory.cpp", [
        ("nsHistory", "GetLength", "history", "length"),
    ], "nsHistory.cpp")

    # Battery
    process_file(src_dir, "dom/battery/BatteryManager.cpp", [
        ("BatteryManager", "Charging", "battery", "charging"),
        ("BatteryManager", "ChargingTime", "battery", "chargingTime"),
        ("BatteryManager", "DischargingTime", "battery", "dischargingTime"),
        ("BatteryManager", "Level", "battery", "level"),
    ], "BatteryManager.cpp")

    # Canvas 2D
    process_file(src_dir, "dom/canvas/CanvasRenderingContext2D.cpp", [
        ("CanvasRenderingContext2D", "GetImageData", "canvas2d", "getImageData"),
    ], "CanvasRenderingContext2D.cpp")

    # HTMLCanvasElement
    process_file(src_dir, "dom/html/HTMLCanvasElement.cpp", [
        ("HTMLCanvasElement", "ToDataURL", "canvas", "toDataURL"),
        ("HTMLCanvasElement", "ToBlob", "canvas", "toBlob"),
        ("HTMLCanvasElement", "GetContext", "canvas", "getContext"),
    ], "HTMLCanvasElement.cpp")

    # WebGL
    process_file(src_dir, "dom/canvas/ClientWebGLContext.cpp", [
        ("ClientWebGLContext", "GetParameter", "webgl", "getParameter"),
        ("ClientWebGLContext", "GetSupportedExtensions", "webgl", "getSupportedExtensions"),
        ("ClientWebGLContext", "GetExtension", "webgl", "getExtension"),
        ("ClientWebGLContext", "GetShaderPrecisionFormat", "webgl", "getShaderPrecisionFormat"),
    ], "ClientWebGLContext.cpp")

    # AudioContext
    process_file(src_dir, "dom/media/webaudio/AudioContext.cpp", [
        ("AudioContext", "OutputLatency", "audioContext", "outputLatency"),
    ], "AudioContext.cpp")

    process_file(src_dir, "dom/media/webaudio/BaseAudioContext.cpp", [
        ("BaseAudioContext", "SampleRate", "audioContext", "sampleRate"),
    ], "BaseAudioContext.cpp")

    # navigator.plugins
    process_file(src_dir, "dom/base/nsPluginArray.cpp", [
        ("nsPluginArray", "IndexedGetter", "navigator.plugins", "indexedGetter"),
        ("nsPluginArray", "NamedGetter", "navigator.plugins", "namedGetter"),
    ], "nsPluginArray.cpp")

    process_file(src_dir, "dom/base/nsMimeTypeArray.cpp", [
        ("nsMimeTypeArray", "IndexedGetter", "navigator.mimeTypes", "indexedGetter"),
        ("nsMimeTypeArray", "NamedGetter", "navigator.mimeTypes", "namedGetter"),
    ], "nsMimeTypeArray.cpp")

    # Performance
    process_file(src_dir, "dom/performance/PerformanceMainThread.cpp", [
        ("PerformanceMainThread", "Timing", "performance", "timing"),
    ], "PerformanceMainThread.cpp")

    # Document.cookie
    process_file(src_dir, "dom/base/Document.cpp", [
        ("Document", "GetCookie", "document", "cookie.get"),
        ("Document", "SetCookie", "document", "cookie.set"),
    ], "Document.cpp")

    # ==================== WebRTC ====================
    process_file(src_dir, "dom/media/webrtc/jsapi/PeerConnectionImpl.cpp", [
        ("PeerConnectionImpl", "CreateOffer", "webrtc", "createOffer"),
        ("PeerConnectionImpl", "CreateAnswer", "webrtc", "createAnswer"),
        ("PeerConnectionImpl", "CreateDataChannel", "webrtc", "createDataChannel"),
    ], "PeerConnectionImpl.cpp")

    # ==================== MediaDevices ====================
    process_file(src_dir, "dom/media/MediaDevices.cpp", [
        ("MediaDevices", "EnumerateDevices", "mediaDevices", "enumerateDevices"),
        ("MediaDevices", "GetUserMedia", "mediaDevices", "getUserMedia"),
    ], "MediaDevices.cpp")

    # ==================== Storage ====================
    process_file(src_dir, "dom/storage/LocalStorage.cpp", [
        ("LocalStorage", "GetItem", "localStorage", "getItem"),
        ("LocalStorage", "SetItem", "localStorage", "setItem"),
    ], "LocalStorage.cpp")

    process_file(src_dir, "dom/storage/SessionStorage.cpp", [
        ("SessionStorage", "GetItem", "sessionStorage", "getItem"),
        ("SessionStorage", "SetItem", "sessionStorage", "setItem"),
    ], "SessionStorage.cpp")

    # ==================== Fonts ====================
    process_file(src_dir, "layout/style/FontFaceSet.cpp", [
        ("FontFaceSet", "Check", "fonts", "check"),
        ("FontFaceSet", "ForEach", "fonts", "forEach"),
    ], "FontFaceSet.cpp")

    # ==================== OffscreenCanvas ====================
    process_file(src_dir, "dom/canvas/OffscreenCanvas.cpp", [
        ("OffscreenCanvas", "GetContext", "offscreenCanvas", "getContext"),
        ("OffscreenCanvas", "TransferToImageBitmap", "offscreenCanvas", "transferToImageBitmap"),
    ], "OffscreenCanvas.cpp")

    # ==================== Geolocation ====================
    process_file(src_dir, "dom/geolocation/Geolocation.cpp", [
        ("Geolocation", "GetCurrentPosition", "geolocation", "getCurrentPosition"),
        ("Geolocation", "WatchPosition", "geolocation", "watchPosition"),
    ], "Geolocation.cpp")

    print(f"\n=== Done. Run 'make build' now. ===")


if __name__ == "__main__":
    main()
