# 🦊 Camoufox-Reverse

> 反检测浏览器 | 逆向工程分支 — SpiderMonkey 引擎层属性访问追踪（JSVMP 不可检测）

基于 [daijro/camoufox](https://github.com/daijro/camoufox) 的 fork，新增 **PropertyTracer** 功能：在 C++ 引擎层追踪 JSVMP 实际读取的 DOM 属性，用于精准指导环境伪装（路径 B）。

## 核心能力

- **C++ 层拦截**：在 SpiderMonkey 的 DOM getter 入口记录属性访问，JS 层完全不可见
- **JSVMP 不可检测**：不修改任何 JS 对象的 getter/descriptor/prototype，不引入 Proxy
- **62 个 DOM getter 覆盖**：Navigator(17) / Screen(3) / Window(13) / Canvas(4) / WebGL(4) / AudioContext(1) / Plugins(4) / Performance(1) / Document.cookie(2) / History(1) / Battery(4) / WorkerNavigator(5)
- **多进程支持**：主进程 + content 进程的属性访问全部捕获
- **按需开关**：不启用 trace 时零开销（单次 atomic load ~1ns）

## 与官方 Camoufox 的关系

| | 官方 Camoufox | Camoufox-Reverse |
|---|---|---|
| 来源 | `pip install camoufox` + `python3 -m camoufox fetch` | 本仓库 GitHub Releases |
| 功能 | 反检测浏览器（指纹伪装） | 反检测 + PropertyTracer 引擎层追踪 |
| 兼容性 | 完全兼容 camoufox-reverse-mcp | 完全兼容，额外支持 `enable_trace=True` |
| 安装方式 | 自动下载到缓存目录 | 手动下载 zip 替换缓存目录 |

**默认使用官方 Camoufox**。只有需要引擎层追踪时才替换为本仓库的构建产物。

---

## 安装

### 前置条件

```bash
pip install camoufox-reverse-mcp
python3 -m camoufox fetch  # 先安装官方 Camoufox
```

### 下载 Camoufox-Reverse

从 [GitHub Releases](https://github.com/WhiteNightShadow/camoufox-reverse/releases) 下载对应平台的 zip 包。

### 替换浏览器

**macOS (Apple Silicon)**

```bash
# 备份官方版本（首次操作）
cp -r ~/Library/Caches/camoufox ~/Library/Caches/camoufox-official-backup

# 替换
rm -rf ~/Library/Caches/camoufox/Camoufox.app
unzip -o camoufox-*-mac.arm64.zip -d ~/Library/Caches/camoufox/

# 创建版本文件
cat > ~/Library/Caches/camoufox/version.json << 'EOF'
{"version": "135.0.1", "release": "beta.24"}
EOF

# macOS 签名（交叉编译的二进制必须签名）
codesign --force --deep --sign - ~/Library/Caches/camoufox/Camoufox.app
```

**macOS (Intel)**

```bash
cp -r ~/Library/Caches/camoufox ~/Library/Caches/camoufox-official-backup
rm -rf ~/Library/Caches/camoufox/Camoufox.app
unzip -o camoufox-*-mac.x86_64.zip -d ~/Library/Caches/camoufox/
cat > ~/Library/Caches/camoufox/version.json << 'EOF'
{"version": "135.0.1", "release": "beta.24"}
EOF
codesign --force --deep --sign - ~/Library/Caches/camoufox/Camoufox.app
```

**Linux**

```bash
cp -r ~/.cache/camoufox ~/.cache/camoufox-official-backup
rm -rf ~/.cache/camoufox/*
unzip -o camoufox-*-lin.x86_64.zip -d ~/.cache/camoufox/
cat > ~/.cache/camoufox/version.json << 'EOF'
{"version": "135.0.1", "release": "beta.24"}
EOF
```

**Windows**

```powershell
# 备份
Copy-Item -Recurse "$env:LOCALAPPDATA\camoufox\camoufox\Cache" "$env:LOCALAPPDATA\camoufox\camoufox\Cache-backup"

# 替换
Remove-Item -Recurse "$env:LOCALAPPDATA\camoufox\camoufox\Cache\*"
Expand-Archive camoufox-*-win.x86_64.zip -DestinationPath "$env:LOCALAPPDATA\camoufox\camoufox\Cache"

# 创建版本文件
'{"version": "135.0.1", "release": "beta.24"}' | Out-File "$env:LOCALAPPDATA\camoufox\camoufox\Cache\version.json"
```

### 切回官方版本

```bash
# macOS
rm -rf ~/Library/Caches/camoufox
cp -r ~/Library/Caches/camoufox-official-backup ~/Library/Caches/camoufox

# Linux
rm -rf ~/.cache/camoufox
cp -r ~/.cache/camoufox-official-backup ~/.cache/camoufox

# 或者直接重新下载官方版本
python3 -m camoufox fetch
```

---

## 使用

### 通过 MCP 工具（推荐）

```python
# 1. 启动浏览器（启用 trace）
>>> launch_browser(enable_trace=True)

# 2. 导航到目标页面
>>> navigate(url="https://target-site.com")

# 3. 等待 JSVMP 执行后，采集属性访问
>>> trace_property_access(duration=10, mode="summary")
{
  "mode": "summary",
  "total_events": 3445,
  "unique_properties": 42,
  "by_property": [
    {"path": "document.cookie.get", "count": 939},
    {"path": "navigator.maxTouchPoints", "count": 869},
    {"path": "webgl.getParameter", "count": 186},
    {"path": "screen.availRect", "count": 168},
    ...
  ]
}

# 4. 按时间线查看
>>> trace_property_access(duration=10, mode="timeline", bucket_ms=500)

# 5. 按对象过滤
>>> trace_property_access(duration=10, filter_object="navigator")

# 6. 搜索特定属性
>>> trace_property_access(duration=10, mode="search", search_query="cookie")
```

### 通过 Playwright 直接使用

```python
import os, json
from playwright.sync_api import sync_playwright
from camoufox.pkgman import launch_path

config = {
    "propertyTrace": {
        "enabled": True,
        "logDir": os.path.expanduser("~/.cache/camoufox-reverse"),
        "objects": [],
        "maxEventsPerSession": 100000,
    }
}

env = os.environ.copy()
env["CAMOU_CONFIG"] = json.dumps(config)
env["MOZ_DISABLE_CONTENT_SANDBOX"] = "1"  # macOS 必须

with sync_playwright() as p:
    browser = p.firefox.launch(
        executable_path=launch_path(),
        headless=False,
        env=env,
    )
    page = browser.new_page()
    page.goto("https://target-site.com")
    # ... 等待 JSVMP 执行 ...
    browser.close()

# 分析 trace 文件
from pathlib import Path
from collections import Counter

events = []
for f in Path("~/.cache/camoufox-reverse/traces").expanduser().glob("*.jsonl"):
    for line in f.read_text().strip().split("\n"):
        if line:
            try: events.append(json.loads(line))
            except: pass

paths = Counter()
for ev in events:
    paths[f"{ev['o']}.{ev['p']}"] += 1

for k, v in paths.most_common(30):
    print(f"  {v:>4} {k}")
```

---

## 注意事项

### macOS 必须设置 MOZ_DISABLE_CONTENT_SANDBOX=1

Firefox 在 macOS 上对 content 进程有严格的沙箱策略，阻止文件写入。PropertyTracer 需要写 trace 文件到磁盘，因此必须禁用 content 进程沙箱。

通过 MCP 的 `launch_browser(enable_trace=True)` 会自动设置此环境变量。

### version.json 格式

替换浏览器后必须创建 `version.json`，否则 camoufox Python 包无法识别。

```json
{"version": "135.0.1", "release": "beta.24"}
```

注意字段名是 `release`（不是 `build`），取决于你安装的 camoufox Python 包版本。

### 不启用 trace 时零影响

如果不传 `enable_trace=True`（或不设置 `CAMOU_CONFIG` 的 `propertyTrace`），PropertyTracer 不会初始化，后台线程不会启动，`Record()` 的 `mEnabled` 永远是 false，热路径只有一次 atomic load（~1ns），对浏览器性能零影响。

---

## 构建

详见 [docs/build-and-dev-workflow.md](../../docs/build-and-dev-workflow.md)。

### 快速编译（Ubuntu 22.04 服务器）

```bash
git clone https://github.com/WhiteNightShadow/camoufox-reverse.git
cd camoufox-reverse
git checkout releases/135

# 下载 Firefox 源码 + 打 patch
make dir BUILD_TARGET=macos,arm64

# 注入 DOM getter 追踪点
python3 scripts/inject-trace-to-source.py camoufox-135.0.1-beta.24

# 确保 camoucfg 在 DIRS 里
echo 'DIRS += ["camoucfg"]' >> camoufox-135.0.1-beta.24/moz.build

# 编译
cd camoufox-135.0.1-beta.24
make bootstrap
./mach build

# 打包
cd obj-aarch64-apple-darwin
make -C browser/installer stage-package
```

---

## 技术架构

```
┌─────────────────────────────────────────────────┐
│           AI 编码助手 (Kiro / Cursor / Claude)    │
│                    ↕ MCP (stdio)                 │
├─────────────────────────────────────────────────┤
│      camoufox-reverse-mcp v1.1.0 (35 tools)     │
│  ┌──────────┬──────────┬──────────┬──────────┐  │
│  │Navigation│ Script   │Debugging │ Hooking  │  │
│  │          │ Analysis │          │          │  │
│  ├──────────┼──────────┼──────────┼──────────┤  │
│  │ Network  │ JSVMP    │  Cookie  │  Verify  │  │
│  │ Capture  │ Analysis │ Storage  │  Signer  │  │
│  ├──────────┴──────────┴──────────┴──────────┤  │
│  │ ★ PropertyTracer (trace_property_access)  │  │
│  └───────────────────────────────────────────┘  │
│                    ↕ Playwright API               │
├─────────────────────────────────────────────────┤
│   Camoufox-Reverse (反指纹 Firefox + PropertyTracer) │
│   C++ 引擎级指纹伪造 · 62 个 DOM getter 追踪点       │
└─────────────────────────────────────────────────┘
```

---

## 许可证

MIT（同上游 Camoufox）

## 反馈

- GitHub Issues: https://github.com/WhiteNightShadow/camoufox-reverse/issues
- 微信：`han8888v8888`（备注 camoufox-reverse）
