#include "PropertyTracer.hpp"

#include <chrono>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

namespace camou {

namespace {

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
  out.append("}\n");
}

// Read single-line control file
std::string ReadControlFile(const std::string& path) {
  std::ifstream f(path);
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
#ifdef _WIN32
  _mkdir(path.c_str());
#else
  mkdir(path.c_str(), 0700);
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
  MkdirP(baseDir.c_str());
  MkdirP(controlDir.c_str());
  MkdirP(mLogDir.c_str());

  pid_t pid = getpid();
  char ctrlPath[1024];
  snprintf(ctrlPath, sizeof(ctrlPath), "%s/control-%d.cmd",
           controlDir.c_str(), pid);
  mControlPath = ctrlPath;

  mMaxEventsPerSession = maxEventsPerSession;
  mWhitelist.clear();
  for (const auto& obj : objects) {
    mWhitelist.insert(obj);
  }

  // Write initial "off" to control file
  {
    std::ofstream f(mControlPath);
    f << "off";
  }

  mInitialized = true;
  mStop.store(false);

  // Start background threads
  mControlThread = std::thread(&PropertyTracer::ControlThreadLoop, this);
  mFlushThread = std::thread(&PropertyTracer::FlushThreadLoop, this);
}

void PropertyTracer::Shutdown() {
  if (!mInitialized) return;
  mStop.store(true);

  if (mControlThread.joinable()) mControlThread.join();
  if (mFlushThread.joinable()) mFlushThread.join();

  StopSession();

  // Clean up control file
  if (!mControlPath.empty()) {
    unlink(mControlPath.c_str());
  }

  mInitialized = false;
}

bool PropertyTracer::ShouldRecord(const char* objName) const {
  if (!objName) return false;
  if (mWhitelist.empty()) return true;
  return mWhitelist.count(objName) > 0;
}

void PropertyTracer::RecordSlow(const char* object, const char* property,
                                const char* value, uint32_t kind) {
  if (!ShouldRecord(object)) return;

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

  // Compute relative timestamp
  int64_t tsMs = 0;
  {
    std::lock_guard<std::mutex> slock(mSessionMutex);
    auto now = std::chrono::steady_clock::now();
    tsMs = std::chrono::duration_cast<std::chrono::milliseconds>(
               now - mSessionStartTime)
               .count();
  }

  // Push to write buffer
  std::lock_guard<std::mutex> lock(mBufferMutex);
  if (mEventsThisSession >= mMaxEventsPerSession) return;

  PropertyAccessEvent ev;
  ev.object = object ? object : "";
  ev.property = property ? property : "";
  ev.value = std::move(vStr);
  ev.tsMs = tsMs;
  ev.kind = kind;

  mWriteBuffer.push_back(std::move(ev));
  mEventsThisSession++;
}

void PropertyTracer::ControlThreadLoop() {
  std::string lastCmd = "off";
  while (!mStop.load()) {
    std::this_thread::sleep_for(std::chrono::milliseconds(50));

    std::string cmd = ReadControlFile(mControlPath);
    if (cmd == lastCmd) continue;

    if (cmd == "on" && !mEnabled.load()) {
      StartNewSession();
    } else if (cmd == "off" && mEnabled.load()) {
      StopSession();
    }
    lastCmd = cmd;
  }
}

void PropertyTracer::FlushThreadLoop() {
  while (!mStop.load()) {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    // Double-buffer swap
    {
      std::lock_guard<std::mutex> lock(mBufferMutex);
      if (mWriteBuffer.empty()) continue;
      std::swap(mWriteBuffer, mFlushBuffer);
    }

    // Write to disk (no lock held)
    int fd;
    {
      std::lock_guard<std::mutex> lock(mSessionMutex);
      fd = mCurrentFd;
    }
    if (fd < 0) {
      mFlushBuffer.clear();
      continue;
    }

    std::string batch;
    batch.reserve(mFlushBuffer.size() * 120);
    for (const auto& ev : mFlushBuffer) {
      SerializeEvent(batch, ev);
    }
    if (!batch.empty()) {
      (void)write(fd, batch.data(), batch.size());
    }
    mFlushBuffer.clear();
  }
}

void PropertyTracer::StartNewSession() {
  std::lock_guard<std::mutex> lock(mSessionMutex);
  if (mCurrentFd >= 0) return;  // already open

  pid_t pid = getpid();
  char path[1024];
  snprintf(path, sizeof(path), "%s/%d_%u.jsonl",
           mLogDir.c_str(), pid, mSessionId++);

  int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
  if (fd < 0) return;

  mCurrentFd = fd;
  mCurrentLogPath = path;
  mSessionStartTime = std::chrono::steady_clock::now();
  mEventsThisSession = 0;

  // Clear leftover buffers
  {
    std::lock_guard<std::mutex> blk(mBufferMutex);
    mWriteBuffer.clear();
    mFlushBuffer.clear();
  }

  mEnabled.store(true, std::memory_order_release);
}

void PropertyTracer::StopSession() {
  mEnabled.store(false, std::memory_order_release);

  // Wait for flush thread to drain last batch
  std::this_thread::sleep_for(std::chrono::milliseconds(200));

  std::lock_guard<std::mutex> lock(mSessionMutex);
  if (mCurrentFd >= 0) {
    fsync(mCurrentFd);
    close(mCurrentFd);
    mCurrentFd = -1;
  }
}

}  // namespace camou
