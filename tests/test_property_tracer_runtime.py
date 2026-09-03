"""Native smoke tests for PropertyTracer buffering and control transitions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "additions" / "camoucfg" / "PropertyTracer.cpp"
INCLUDE = ROOT / "additions" / "camoucfg"

HARNESS = r"""
#include "PropertyTracer.hpp"

#include <chrono>
#include <fstream>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#  include <process.h>
#  define getpid _getpid
#else
#  include <unistd.h>
#endif

int main(int argc, char** argv) {
  if (argc != 2) return 2;
  const std::string base = argv[1];
  auto& tracer = camou::PropertyTracer::Instance();
  tracer.Initialize(base, {}, 10000);

  std::vector<std::thread> workers;
  for (int worker = 0; worker < 4; ++worker) {
    workers.emplace_back([&tracer, worker]() {
      for (int i = 0; i < 250; ++i) {
        const uint32_t kind = static_cast<uint32_t>((worker + i) % 3);
        tracer.Record("navigator", "userAgent", nullptr, kind,
                      "navigator.userAgent@dom/base/Navigator.cpp");
      }
    });
  }
  for (auto& worker : workers) worker.join();

  const std::string control = base + "/control/control-" +
                              std::to_string(getpid()) + ".cmd";
  const std::string status = base + "/control/status-" +
                             std::to_string(getpid()) + ".state";
  { std::ofstream file(control); file << "off"; }
  std::this_thread::sleep_for(std::chrono::milliseconds(180));
  {
    std::ifstream file(status);
    std::string state;
    file >> state;
    if (state != "off") return 3;
  }
  for (int i = 0; i < 20; ++i) {
    tracer.Record("document", "cookie.set", nullptr, 1,
                  "document.cookie.set@dom/base/Document.cpp");
  }

  { std::ofstream file(base + "/desired.state"); file << "on"; }
  { std::ofstream file(control); file << "on"; }
  std::this_thread::sleep_for(std::chrono::milliseconds(180));
  {
    std::ifstream file(status);
    std::string state;
    file >> state;
    if (state != "on") return 4;
  }
  for (int i = 0; i < 25; ++i) {
    tracer.Record("canvas", "getContext", nullptr, 2,
                  "canvas.getContext@dom/html/HTMLCanvasElement.cpp");
  }
  tracer.Shutdown();

  // A newly-created process/session must honor the run-level desired state.
  { std::ofstream file(base + "/desired.state"); file << "off"; }
  tracer.Initialize(base, {}, 10000);
  for (int i = 0; i < 20; ++i) {
    tracer.Record("window", "innerWidth", nullptr, 0, "window.innerWidth@test");
  }
  {
    std::ifstream file(status);
    std::string state;
    file >> state;
    if (state != "off") return 5;
  }
  { std::ofstream file(base + "/desired.state"); file << "on"; }
  { std::ofstream file(control); file << "on"; }
  std::this_thread::sleep_for(std::chrono::milliseconds(180));
  for (int i = 0; i < 5; ++i) {
    tracer.Record("window", "innerWidth", nullptr, 0, "window.innerWidth@test");
  }
  tracer.Shutdown();
  return 0;
}
"""


class PropertyTracerRuntimeTests(unittest.TestCase):
    def test_buffered_events_are_complete_typed_and_drained(self):
        compiler = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
        if not compiler:
            self.skipTest("no C++ compiler available")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness = root / "property_tracer_harness.cpp"
            binary = root / (
                "property_tracer_harness.exe" if os.name == "nt"
                else "property_tracer_harness"
            )
            trace_root = root / "trace-run-追踪测试"
            trace_root.mkdir()
            harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
            command = [
                compiler,
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Wpedantic",
                "-Werror",
                f"-I{INCLUDE}",
                str(harness),
                str(SOURCE),
                "-o",
                str(binary),
            ]
            if os.name != "nt":
                command.insert(2, "-pthread")
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run([str(binary), str(trace_root)], check=True, timeout=20)

            files = sorted((trace_root / "traces").glob("*.jsonl"))
            self.assertEqual(len(files), 3)
            sessions = []
            for path in files:
                events = [json.loads(line) for line in path.read_text().splitlines()]
                sessions.append(events)
                self.assertEqual([event["q"] for event in events], list(range(len(events))))
                self.assertTrue(all(event["w"] > 0 and event["u"] >= 0 for event in events))
                self.assertTrue(all(event["s"] for event in events))

            self.assertEqual(len(sessions[0]), 1000)
            self.assertEqual({event["k"] for event in sessions[0]}, {0, 1, 2})
            self.assertEqual(len(sessions[1]), 25)
            self.assertEqual({event["k"] for event in sessions[1]}, {2})
            self.assertEqual(len(sessions[2]), 5)
            self.assertEqual({event["k"] for event in sessions[2]}, {0})
            self.assertEqual(list((trace_root / "control").glob("control-*.cmd")), [])


if __name__ == "__main__":
    unittest.main()
