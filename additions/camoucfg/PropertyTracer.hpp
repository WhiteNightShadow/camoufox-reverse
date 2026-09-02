#ifndef CAMOU_PROPERTY_TRACER_H
#define CAMOU_PROPERTY_TRACER_H

/*
 * PropertyTracer — Engine-level DOM property access tracing.
 *
 * Records which MaskConfig keys are accessed by page JS (including JSVMP).
 * Completely invisible to JS layer: no window globals, no WebIDL, no Proxy.
 * Data is written to JSONL files on disk; MCP reads them via filesystem.
 *
 * Design:
 *   - Hot path: single atomic load (~1ns), zero overhead when disabled
 *   - Slow path: push to write buffer (mutex-protected)
 *   - Control thread: polls control file every 50ms for on/off commands
 *   - Flush thread: writes buffered events to disk every 100ms
 *
 * Written for camoufox-reverse project.
 */

#include <atomic>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_set>
#include <vector>
#include <chrono>

namespace camou {

struct PropertyAccessEvent {
  std::string object;    // config key prefix: "navigator", "screen", "window", ...
  std::string property;  // config key suffix: "userAgent", "width", ...
  std::string value;     // stringified return value (truncated to 200 chars)
  int64_t tsMs;          // milliseconds since session start
  uint32_t kind;         // 0=get, 1=set, 2=call
};

class PropertyTracer {
 public:
  static PropertyTracer& Instance() {
    static PropertyTracer inst;
    return inst;
  }

  // Initialize trace infrastructure (call once at browser startup).
  //   baseDir: base directory (e.g. ~/.cache/camoufox-reverse)
  //            control file: <baseDir>/control/control-<pid>.cmd
  //            log files:    <baseDir>/traces/<pid>_<session>.jsonl
  //   objects: whitelist of object prefixes to trace (empty = trace all)
  //   maxEventsPerSession: cap per session
  void Initialize(const std::string& baseDir,
                  const std::vector<std::string>& objects,
                  uint32_t maxEventsPerSession);

  // Shutdown (call at browser exit)
  void Shutdown();

  // ★ Hot path ★ — must be extremely fast
  inline void Record(const char* object, const char* property,
                     const char* value = nullptr, uint32_t kind = 0) {
    if (!mEnabled.load(std::memory_order_acquire)) return;
    RecordSlow(object, property, value, kind);
  }

  // Query whether tracing is active
  bool IsEnabled() const {
    return mEnabled.load(std::memory_order_acquire);
  }

  bool IsInitialized() const { return mInitialized; }

 private:
  PropertyTracer() = default;
  ~PropertyTracer() { Shutdown(); }
  PropertyTracer(const PropertyTracer&) = delete;
  PropertyTracer& operator=(const PropertyTracer&) = delete;

  void RecordSlow(const char* object, const char* property,
                  const char* value, uint32_t kind);
  void ControlThreadLoop();
  void FlushThreadLoop();
  void StartNewSession();
  void StopSession();
  bool ShouldRecord(const char* objName) const;

  // State
  std::atomic<bool> mEnabled{false};
  std::atomic<bool> mStop{false};
  bool mInitialized{false};

  // Config
  std::string mControlPath;
  std::string mLogDir;
  std::unordered_set<std::string> mWhitelist;
  uint32_t mMaxEventsPerSession{100000};

  // Session state
  std::mutex mSessionMutex;
  int mCurrentFd{-1};
  std::string mCurrentLogPath;
  uint32_t mSessionId{0};
  uint32_t mEventsThisSession{0};
  std::chrono::steady_clock::time_point mSessionStartTime;

  // Double buffer
  std::mutex mBufferMutex;
  std::vector<PropertyAccessEvent> mWriteBuffer;
  std::vector<PropertyAccessEvent> mFlushBuffer;

  // Background threads
  std::thread mControlThread;
  std::thread mFlushThread;
};

}  // namespace camou

#endif  // CAMOU_PROPERTY_TRACER_H

