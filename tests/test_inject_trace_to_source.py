"""Focused, browser-free tests for the Firefox 152 PropertyTracer injector."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "inject-trace-to-source.py"
SPEC = importlib.util.spec_from_file_location("property_trace_injector", SCRIPT)
assert SPEC and SPEC.loader
injector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = injector
SPEC.loader.exec_module(injector)


class InjectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self._write("browser/config/version.txt", "152.0.4-beta.30\n")
        self._write("moz.build", 'DIRS += ["lw"]\n')

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path

    def _source(self, relative: str, text: str) -> Path:
        path = self._write(relative, text)
        mozbuild = path.parent / "moz.build"
        if not mozbuild.exists():
            mozbuild.write_text('FINAL_LIBRARY = "xul"\n', encoding="utf-8")
        return path

    @staticmethod
    def _hook(path: str, signature: str = r"Foo::Bar\s*\("):
        return injector.Hook(path, signature, "test", "value")

    def _run(self, hooks=(), **kwargs):
        return injector.run_injection(
            self.root,
            hooks=hooks,
            include_audio_sample_rate=False,
            expect_hooks=len(hooks),
            **kwargs,
        )

    def test_manifest_has_exactly_75_unique_sites(self):
        sites = [hook.site_id for hook in injector.HOOKS]
        sites.append(injector.AUDIO_SITE)
        self.assertEqual(len(injector.HOOKS) + 1, 75)
        self.assertEqual(len(sites), len(set(sites)))
        kinds = [hook.kind for hook in injector.HOOKS] + [injector.GET]
        self.assertEqual(set(kinds), {injector.GET, injector.SET, injector.CALL})
        self.assertEqual(kinds.count(injector.SET), 1)
        self.assertGreater(kinds.count(injector.CALL), 10)

    def test_missing_file_and_symbol_fail_closed(self):
        with self.assertRaisesRegex(injector.InjectionError, "required file"):
            self._run([self._hook("dom/base/Missing.cpp")])

        self._source("dom/base/Present.cpp", "#include <x>\nint Foo::Other() { return 1; }\n")
        with self.assertRaisesRegex(injector.InjectionError, "matched 0 times"):
            self._run([self._hook("dom/base/Present.cpp")])

    def test_ambiguous_signature_fails(self):
        self._source(
            "dom/base/Ambiguous.cpp",
            "#include <x>\nint Foo::Bar() { return 1; }\n"
            "int Foo::Bar(int x) { return x; }\n",
        )
        with self.assertRaisesRegex(injector.InjectionError, "matched 2 times"):
            self._run([self._hook("dom/base/Ambiguous.cpp")])

    def test_check_mode_plans_without_writing(self):
        path = self._source(
            "dom/base/Check.cpp", "#include <x>\nint Foo::Bar() { return 1; }\n"
        )
        before = path.read_bytes()
        result = self._run([self._hook("dom/base/Check.cpp")], mode="check")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result["applied"], 1)
        self.assertIn("dom/base/Check.cpp", result["files_changed"])

    def test_planning_failure_is_atomic(self):
        first = self._source(
            "dom/base/First.cpp", "#include <x>\nint Foo::Bar() { return 1; }\n"
        )
        before = first.read_bytes()
        hooks = [
            self._hook("dom/base/First.cpp"),
            injector.Hook("dom/base/Second.cpp", r"Second::Run\s*\(", "second", "run"),
        ]
        with self.assertRaises(injector.InjectionError):
            self._run(hooks)
        self.assertEqual(first.read_bytes(), before)

    def test_apply_is_idempotent_and_updates_build_files_once(self):
        path = self._source(
            "dom/base/Idempotent.cpp",
            '#include "MaskConfig.hpp"\nint Foo::Bar() { return 1; }\n',
        )
        hook = self._hook("dom/base/Idempotent.cpp")
        first = self._run([hook], mode="apply", ensure_build_files=True)
        after_first = path.read_bytes()
        second = self._run([hook], mode="apply", ensure_build_files=True)
        self.assertEqual(path.read_bytes(), after_first)
        self.assertEqual(first["applied"], 1)
        self.assertEqual(second["already"], 1)
        self.assertEqual(second["files_changed"], [])
        self.assertEqual(path.read_text().count(injector.INCLUDE_LINE), 1)
        self.assertIn(f', {injector.GET}, "{hook.site_id}"', path.read_text())
        self.assertEqual((path.parent / "moz.build").read_text().count("/camoucfg"), 1)
        self.assertEqual((self.root / "moz.build").read_text().count("camoucfg"), 1)

    def test_webgl_get_extension_selects_js_facing_overload(self):
        path = self._source(
            "dom/canvas/WebGLContextExtensions.cpp",
            "#include <x>\n"
            "void ClientWebGLContext::GetExtension(JSContext* cx, const nsAString& name, "
            "JS::MutableHandle<JSObject*> retval) { retval.set(nullptr); }\n"
            "RefPtr<X> ClientWebGLContext::GetExtension(WebGLExtensionID ext, "
            "CallerType caller) { return nullptr; }\n",
        )
        injector.run_injection(
            self.root,
            hooks=[injector.WEBGL_GET_EXTENSION_HOOK],
            include_audio_sample_rate=False,
            expect_hooks=1,
            ensure_build_files=False,
        )
        text = path.read_text()
        self.assertEqual(text.count(injector.WEBGL_GET_EXTENSION_HOOK.marker), 1)
        self.assertLess(text.index(injector.WEBGL_GET_EXTENSION_HOOK.marker), text.index("retval.set"))
        native_body = text.split("RefPtr<X>", 1)[1]
        self.assertNotIn(injector.WEBGL_GET_EXTENSION_HOOK.marker, native_body)

    def test_audio_sample_rate_moves_inline_getter_out_of_line(self):
        header = self._source(
            "dom/media/webaudio/AudioContext.h",
            "#pragma once\nclass AudioContext {\n public:\n"
            "  float SampleRate() const { return mSampleRate; }\n"
            "  float mSampleRate;\n};\n",
        )
        source = self._source(
            "dom/media/webaudio/AudioContext.cpp",
            '#include "AudioContext.h"\n'
            "double AudioContext::OutputLatency() { return 0.0; }\n",
        )
        result = injector.run_injection(
            self.root,
            hooks=[],
            include_audio_sample_rate=True,
            expect_hooks=1,
            ensure_build_files=False,
        )
        self.assertEqual(result["applied"], 1)
        self.assertIn(injector.AUDIO_DECL, header.read_text())
        self.assertNotIn(injector.AUDIO_INLINE, header.read_text())
        source_text = source.read_text()
        self.assertIn(injector.AUDIO_DEF, source_text)
        self.assertIn(injector.AUDIO_MARKER, source_text)
        self.assertLess(source_text.index(injector.AUDIO_DEF), source_text.index(injector.AUDIO_ANCHOR))

        second = injector.run_injection(
            self.root,
            hooks=[],
            include_audio_sample_rate=True,
            expect_hooks=1,
            ensure_build_files=False,
        )
        self.assertEqual(second["already"], 1)
        self.assertEqual(second["files_changed"], [])

    def test_version_mismatch_fails_before_source_access(self):
        (self.root / "browser/config/version.txt").write_text("152.0.4-beta.29\n")
        with self.assertRaisesRegex(injector.InjectionError, "source version"):
            self._run([])


if __name__ == "__main__":
    unittest.main()
