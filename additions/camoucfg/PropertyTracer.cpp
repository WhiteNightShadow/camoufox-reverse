#include "PropertyTracer.hpp"

#include <chrono>
#include <algorithm>
#include <cerrno>
#include <climits>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>

#ifdef _WIN32
#  include <io.h>
#  include <process.h>
#  include <direct.h>
typedef int pid_t;
#  define close  _close
#  define write  _write
#  define fsync  _commit
#  define getpid _getpid
#else
#  include <unistd.h>
#  ifndef O_BINARY
#    define O_BINARY 0
#  endif
#endif

namespace camou {

namespace {

std::filesystem::path NativePath(const std::string& path) {
  return std::filesystem::u8path(path);
}

int OpenTraceFile(const std::string& path) {
#ifdef _WIN32
  return _wopen(NativePath(path).c_str(),
                _O_WRONLY | _O_CREAT | _O_EXCL | _O_NOINHERIT | _O_BINARY,
                _S_IREAD | _S_IWRITE);
#else
  return open(path.c_str(),
              O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_BINARY, 0600);
#endif
}

void UnlinkPath(const std::string& path) {
  std::error_code error;
  std::filesystem::remove(NativePath(path), error);
}

// JSON string escaping
void AppendJsonString(std::string& out, const std::string& s) {
  out.push_back('"');
  for (char c : s) {
    switch (c) {
      case '"':  out.append("\\\""); break;
      case '\\': out.append("\\\\"); break;
      case '\n': out.append("\\n"); break;
      case '\r': out.append("\\r"); break;
      case '\t': out.append("\\t"); break;
      default:
        if (static_cast<unsigned char>(c) < 0x20) {
          char buf[8];
          snprintf(buf, sizeof(buf), "\\u%04x", c);
          out.append(buf);
        } else {
          out.push_back(c);
        }
    }
  }
  out.push_back('"');
}

void SerializeEvent(std::string& out, const PropertyAccessEvent& ev) {
  out.append("{\"o\":");
  AppendJsonString(out, ev.object);
  out.append(",\"p\":");
  AppendJsonString(out, ev.property);
  out.append(",\"v\":");
  AppendJsonString(out, ev.value);
  out.append(",\"t\":");
  out.append(std::to_string(ev.tsMs));
  out.append(",\"k\":");
  out.append(std::to_string(ev.kind));
  out.append(",\"u\":");
  out.append(std::to_string(ev.tsUs));
  out.append(",\"w\":");
  out.append(std::to_string(ev.wallUs));
  out.append(",\"q\":");
  out.append(std::to_string(ev.sequence));
  if (!ev.site.empty()) {
    out.append(",\"s\":");
    AppendJsonString(out, ev.site);
  }
  out.append("}\n");
}

bool WriteAll(int fd, const char* data, size_t size) {
  while (size > 0) {
#ifdef _WIN32
    const unsigned int chunk = static_cast<unsigned int>(
        std::min<size_t>(size, static_cast<size_t>(INT_MAX)));
    const int written = write(fd, data, chunk);
#else
    const ssize_t written = write(fd, data, size);
#endif
    if (written < 0 && errno == EINTR) continue;
    if (written <= 0) return false;
    data += written;
    size -= static_cast<size_t>(written);
  }
  return true;
}

// Read single-line control file
std::string ReadControlFile(const std::string& path) {
  std::ifstream f(NativePath(path));
  if (!f) return "";
  std::string line;
  std::getline(f, line);
  while (!line.empty() &&
         (line.back() == '\n' || line.back() == '\r' || line.back() == ' ')) {
    line.pop_back();
  }
  return line;
}

// Recursive mkdir
void MkdirP(const std::string& path) {
  std::error_code error;
  std::filesystem::create_directories(NativePath(path), error);
#ifndef _WIN32
  chmod(path.c_str(), 0700);
#endif
}

}  // anonymous namespace

void PropertyTracer::Initialize(const std::string& baseDir,
                                const std::vector<std::string>& objects,
                                uint32_t maxEventsPerSession) {
  if (mInitialized) return;

  // Build paths
  std::string controlDir = baseDir + "/control";
  mLogDir = baseDir + "/traces";
  mDesiredPath = baseDir + "/desired.state";
  MkdirP(baseDir.c_str());
  MkdirP(controlDir.c_str());
  MkdirP(mLogDir.c_str());

  pid_t pid = getpid();
  char ctrlPath[1024];
  snprintf(ctrlPath, sizeof(ctrlPath), "%s/control-%d.cmd",
           controlDir.c_str(), pid);
  mControlPath = ctrlPath;
  char statusPath[1024];
  snprintf(statusPath, sizeof(statusPath), "%s/status-%d.state",
           controlDir.c_str(), pid);
  mStatusPath = statusPath;

  mMaxEventsPerSession = maxEventsPerSession;
  mWhitelist.clear();
  for (const auto& obj : objects) {
    mWhitelist.insert(obj);
  }

  // Write initial "off" to control file
  {
    std::ofstream f(NativePath(mControlPath));
    f << "off";
  }
#ifndef _WIN32
  chmod(mControlPath.c_str(), 0600);
#endif

  mInitialized = true;
  mStop.store(false);

  fprintf(stderr, "PropertyTracer: initialized, logDir=%s\n", mLogDir.c_str());
  // A run-level desired state prevents a content process created during a stop
  // transition from auto-starting a new trace behind the controller's back.
  const bool autoStart = ReadControlFile(mDesiredPath) != "off";
  {
    std::ofstream f(NativePath(mControlPath));
    f << (autoStart ? "on" : "off");
  }
  if (autoStart) {
    StartNewSession();
  } else {
    WriteStatus("off");
  }

  // Start background threads
  mControlThread = std::thread(&PropertyTracer::ControlThreadLoop, this);
  mFlushThread = std::thread(&PropertyTracer::FlushThreadLoop, this);
}

void PropertyTracer::Shutdown() {
  if (!mInitialized) return;
  mEnabled.store(false, std::memory_order_release);
  mStop.store(true);
  mBufferCv.notify_all();

  if (mControlThread.joinable()) mControlThread.join();
  if (mFlushThread.joinable()) mFlushThread.join();

  StopSession();

  // Clean up control file
  if (!mControlPath.empty()) {
    UnlinkPath(mControlPath);
  }
  if (!mStatusPath.empty()) {
    UnlinkPath(mStatusPath);
  }

  mInitialized = false;
}

bool PropertyTracer::ShouldRecord(const char* objName) const {
  if (!objName) return false;
  if (mWhitelist.empty()) return true;
  return mWhitelist.count(objName) > 0;
}

void PropertyTracer::RecordSlow(const char* object, const char* property,
                                const char* value, uint32_t kind,
                                const char* site, uint64_t generation) {
  if (!ShouldRecord(object)) return;

  std::lock_guard<std::mutex> lock(mBufferMutex);
  // Record() may have observed enabled=true immediately before a stop command.
  if (!mEnabled.load(std::memory_order_acquire)) return;
  if (mGeneration.load(std::memory_order_acquire) != generation) return;
  if (mEventsThisSession >= mMaxEventsPerSession) {
    mSaturated.store(true, std::memory_order_release);
    ++mDroppedEventsThisSession;
    return;
  }

  // Truncate value
  std::string vStr;
  if (value) {
    size_t len = strlen(value);
    if (len > 200) {
      vStr.assign(value, 200);
      vStr.append("...[trunc]");
    } else {
      vStr.assign(value, len);
    }
  }

  // Use one steady-clock read. The wall-clock origin is captured when the
  // session starts, so events from different Firefox processes can be merged.
  const auto now = std::chrono::steady_clock::now();
  const int64_t tsUs = std::chrono::duration_cast<std::chrono::microseconds>(
                           now - mSessionStartTime)
                           .count();

  // Build JSONL line
  PropertyAccessEvent ev;
  ev.object = object ? object : "";
  ev.property = property ? property : "";
  ev.value = std::move(vStr);
  ev.tsMs = tsUs / 1000;
  ev.tsUs = tsUs;
  ev.wallUs = mSessionStartWallUs + tsUs;
  ev.sequence = mSequence++;
  ev.kind = kind;
  ev.site = site ? site : "";
  mWriteBuffer.emplace_back(std::move(ev));
  ++mEventsThisSession;
  if (mWriteBuffer.size() >= 256) mBufferCv.notify_one();
}

void PropertyTracer::ControlThreadLoop() {
  // Initialize from the state already acknowledged by Initialize(). Rewriting
  // the same state on the first poll briefly truncated the status file, so an
  // external reader could observe an empty acknowledgement. A command written
  // before this thread starts is still handled whenever it differs here.
  std::string lastCmd =
      mEnabled.load(std::memory_order_acquire) ? "on" : "off";
  while (!mStop.load()) {
    std::string cmd = ReadControlFile(mControlPath);
    if (cmd != lastCmd) {
      if (cmd == "on" && !mEnabled.load()) {
        StartNewSession();
      } else if (cmd == "off") {
        if (mEnabled.load()) {
          StopSession();
        } else {
          // Clear a previous start/open error so a later on command can retry.
          WriteStatus("off");
        }
      }
      lastCmd = cmd;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }
}

void PropertyTracer::FlushThreadLoop() {
  while (true) {
    {
      std::unique_lock<std::mutex> lock(mBufferMutex);
      mBufferCv.wait_for(lock, std::chrono::milliseconds(100), [this]() {
        return mStop.load() || mWriteBuffer.size() >= 256;
      });
    }
    FlushPending();
    if (mStop.load()) {
      std::lock_guard<std::mutex> lock(mBufferMutex);
      if (mWriteBuffer.empty()) break;
    }
  }
}

void PropertyTracer::FlushPending() {
  // Serialize the complete swap+write operation. StopSession uses the same
  // gate, so it cannot close the descriptor while another flush is in flight.
  std::lock_guard<std::mutex> flushLock(mFlushMutex);
  std::vector<PropertyAccessEvent> pending;
  {
    std::lock_guard<std::mutex> lock(mBufferMutex);
    if (mWriteBuffer.empty()) return;
    pending.swap(mWriteBuffer);
  }
  WriteBatch(pending);
}

void PropertyTracer::WriteBatch(
    const std::vector<PropertyAccessEvent>& events) {
  if (events.empty()) return;
  std::string batch;
  batch.reserve(events.size() * 180);
  for (const auto& ev : events) SerializeEvent(batch, ev);

  // Keep the descriptor valid for the complete write. StopSession takes this
  // same mutex before fsync/close, eliminating the old flush-vs-close race.
  std::lock_guard<std::mutex> lock(mSessionMutex);
  if (mCurrentFd >= 0 && !WriteAll(mCurrentFd, batch.data(), batch.size())) {
    mWriteFailed.store(true, std::memory_order_release);
    WriteStatus("error");
    fprintf(stderr, "PropertyTracer: failed to write trace batch: %s\n",
            strerror(errno));
  }
}

void PropertyTracer::WriteStatus(const char* state, const char* detail) {
  if (mStatusPath.empty()) return;
  std::ofstream file(NativePath(mStatusPath), std::ios::trunc);
  if (!file) return;
  file << state << " " << (mSessionId == 0 ? 0 : mSessionId - 1);
  if (detail) file << " " << detail;
  file << "\n";
  file.flush();
#ifndef _WIN32
  chmod(mStatusPath.c_str(), 0600);
#endif
}

void PropertyTracer::StartNewSession() {
  std::lock_guard<std::mutex> lock(mSessionMutex);
  if (mCurrentFd >= 0) return;  // already open

  // Use parent PID for content processes (they share the same trace dir)
  pid_t pid = getpid();
  char path[1024];
  snprintf(path, sizeof(path), "%s/%d_%u.jsonl",
           mLogDir.c_str(), pid, mSessionId++);

  int fd = OpenTraceFile(path);
  while (fd < 0 && errno == EEXIST) {
    snprintf(path, sizeof(path), "%s/%d_%u.jsonl",
             mLogDir.c_str(), pid, mSessionId++);
    fd = OpenTraceFile(path);
  }
  if (fd < 0) {
    WriteStatus("error");
    fprintf(stderr, "PropertyTracer: failed to create trace file: %s\n",
            strerror(errno));
    return;
  }

  mCurrentFd = fd;
  mCurrentLogPath = path;
  {
    std::lock_guard<std::mutex> bufferLock(mBufferMutex);
    mSessionStartTime = std::chrono::steady_clock::now();
    mSessionStartWallUs =
        std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::system_clock::now().time_since_epoch())
            .count();
    mEventsThisSession = 0;
    mDroppedEventsThisSession = 0;
    mSequence = 0;
    mSaturated.store(false, std::memory_order_release);
    mWriteFailed.store(false, std::memory_order_release);
    mGeneration.fetch_add(1, std::memory_order_acq_rel);
  }

  mEnabled.store(true, std::memory_order_release);
  WriteStatus("on");
}

void PropertyTracer::StopSession() {
  mEnabled.store(false, std::memory_order_release);
  FlushPending();

  std::lock_guard<std::mutex> lock(mSessionMutex);
  if (mCurrentFd >= 0) {
    if (fsync(mCurrentFd) != 0) {
      mWriteFailed.store(true, std::memory_order_release);
      fprintf(stderr, "PropertyTracer: failed to sync trace file: %s\n",
              strerror(errno));
    }
    close(mCurrentFd);
    mCurrentFd = -1;
  }
  if (mDroppedEventsThisSession > 0) {
    fprintf(stderr,
            "PropertyTracer: session event cap reached; later events skipped\n");
  }
  WriteStatus("off",
              mWriteFailed.load(std::memory_order_acquire) ? "write_error"
                                                           : nullptr);
}

}  // namespace camou
