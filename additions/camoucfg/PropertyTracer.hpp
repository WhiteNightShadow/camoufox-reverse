#ifndef CAMOU_PROPERTY_TRACER_H
#define CAMOU_PROPERTY_TRACER_H

/*
 * PropertyTracer — Engine-level DOM property access tracing.
 *
 * Records which MaskConfig keys are accessed by page JS (including JSVMP).
 * No direct JS reflection surface: no window globals, WebIDL changes, or Proxy.
 * Data is written to JSONL files on disk; MCP reads them via filesystem.
 *
 * Design:
 *   - Disabled hot path: one atomic load and immediate return
 *   - Enabled path: copy one compact event into a bounded in-memory buffer
 *   - Flush thread: batches JSONL writes and drains deterministically on stop
 *   - Control thread: polls the control file every 50ms for on/off commands
 *
 * Tracing is opt-in and capped per session. High-volume traces can perturb page
 * timing; keep sessions short or use object filters for timing-sensitive work.
 *
 * Written for camoufox-reverse project.
 */

#include <atomic>
#include <condition_variable>
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
  int64_t tsUs;          // microseconds since session start (optional v1 extension)
  int64_t wallUs;        // Unix epoch microseconds (optional v1 extension)
  uint64_t sequence;     // per-process/session sequence (optional v1 extension)
  uint32_t kind;         // 0=get, 1=set, 2=call
  std::string site;      // stable native injection site (optional v1 extension)
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
                     const char* value = nullptr, uint32_t kind = 0,
                     const char* site = nullptr) {
    const uint64_t generation = mGeneration.load(std::memory_order_acquire);
    if (!mEnabled.load(std::memory_order_acquire)) return;
    if (mSaturated.load(std::memory_order_relaxed)) return;
    RecordSlow(object, property, value, kind, site, generation);
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
                  const char* value, uint32_t kind, const char* site,
                  uint64_t generation);
  void ControlThreadLoop();
  void FlushThreadLoop();
  void FlushPending();
  void WriteBatch(const std::vector<PropertyAccessEvent>& events);
  void StartNewSession();
  void StopSession();
  void WriteStatus(const char* state, const char* detail = nullptr);
  bool ShouldRecord(const char* objName) const;

  // State
  std::atomic<bool> mEnabled{false};
  std::atomic<bool> mSaturated{false};
  std::atomic<bool> mWriteFailed{false};
  std::atomic<bool> mStop{false};
  std::atomic<uint64_t> mGeneration{0};
  bool mInitialized{false};

  // Config
  std::string mControlPath;
  std::string mStatusPath;
  std::string mDesiredPath;
  std::string mLogDir;
  std::unordered_set<std::string> mWhitelist;
  uint32_t mMaxEventsPerSession{100000};

  // Session state
  std::mutex mSessionMutex;
  int mCurrentFd{-1};
  std::string mCurrentLogPath;
  uint32_t mSessionId{0};
  uint32_t mEventsThisSession{0};
  uint32_t mDroppedEventsThisSession{0};
  uint64_t mSequence{0};
  int64_t mSessionStartWallUs{0};
  std::chrono::steady_clock::time_point mSessionStartTime;

  // Double buffer
  std::mutex mFlushMutex;
  std::mutex mBufferMutex;
  std::condition_variable mBufferCv;
  std::vector<PropertyAccessEvent> mWriteBuffer;

  // Background threads
  std::thread mControlThread;
  std::thread mFlushThread;
};

}  // namespace camou

#endif  // CAMOU_PROPERTY_TRACER_H
