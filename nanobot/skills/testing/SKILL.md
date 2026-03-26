---
name: testing
description: "Write and run Python test scripts to verify functionality. Use when testing APIs, scripts, services, cron jobs, or any system behavior."
always: false
---

# Testing — 编写和运行测试

当需要验证任何功能是否正常工作时使用此技能。写一个 Python 测试脚本，然后用 exec 运行它。

## 快速开始

1. 将测试脚本写入 workspace：
```bash
write_file path="test_xxx.py" content="..."
```

2. 用 exec 运行：
```bash
exec command="cd /root/.nanobot/workspace && python3 test_xxx.py"
```

## 测试模板

### 基础功能测试

```python
#!/usr/bin/env python3
"""Test: <describe what you're testing>"""
import sys

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))

# ── Tests ──
print("🧪 Testing <feature>...")

check("basic case", 1 + 1 == 2)
check("string ops", "hello".upper() == "HELLO")

# ── Summary ──
total = passed + failed
print(f"\n{'✅' if failed == 0 else '❌'} {passed}/{total} passed")
sys.exit(0 if failed == 0 else 1)
```

### HTTP API 测试

```python
#!/usr/bin/env python3
"""Test HTTP endpoints."""
import json, sys, urllib.request, urllib.error

passed = failed = 0
BASE = "http://localhost:8080"

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1; print(f"  ✅ {name}")
    else:
        failed += 1; print(f"  ❌ {name} — {detail}")

def get(path):
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)

def post(path, data):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(data).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)

print("🧪 Testing API...")
status, body = get("/health")
check("health endpoint", status == 200, f"got {status}")

total = passed + failed
print(f"\n{'✅' if failed == 0 else '❌'} {passed}/{total} passed")
sys.exit(0 if failed == 0 else 1)
```

### 文件系统测试

```python
#!/usr/bin/env python3
"""Test file operations."""
import os, sys, tempfile

passed = failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1; print(f"  ✅ {name}")
    else:
        failed += 1; print(f"  ❌ {name} — {detail}")

print("🧪 Testing file operations...")

with tempfile.TemporaryDirectory() as tmp:
    # Write
    path = os.path.join(tmp, "test.txt")
    with open(path, "w") as f:
        f.write("hello world")
    check("file created", os.path.exists(path))

    # Read
    with open(path) as f:
        content = f.read()
    check("content correct", content == "hello world", f"got: {content!r}")

    # Delete
    os.remove(path)
    check("file deleted", not os.path.exists(path))

total = passed + failed
print(f"\n{'✅' if failed == 0 else '❌'} {passed}/{total} passed")
sys.exit(0 if failed == 0 else 1)
```

### 进程/命令测试

```python
#!/usr/bin/env python3
"""Test shell commands and processes."""
import subprocess, sys

passed = failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1; print(f"  ✅ {name}")
    else:
        failed += 1; print(f"  ❌ {name} — {detail}")

def run(cmd, timeout=10):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return r.returncode, r.stdout.strip(), r.stderr.strip()

print("🧪 Testing commands...")

code, out, err = run("echo hello")
check("echo works", code == 0 and out == "hello", f"code={code} out={out!r}")

code, out, err = run("python3 --version")
check("python3 available", code == 0, f"code={code} err={err}")

total = passed + failed
print(f"\n{'✅' if failed == 0 else '❌'} {passed}/{total} passed")
sys.exit(0 if failed == 0 else 1)
```

### Cron 任务测试

```python
#!/usr/bin/env python3
"""Test cron job creation and listing."""
import json, subprocess, sys
from pathlib import Path

passed = failed = 0
CRON_CLI = "{baseDir}/../cron/scripts/cron_cli.py"

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1; print(f"  ✅ {name}")
    else:
        failed += 1; print(f"  ❌ {name} — {detail}")

def cron(args):
    r = subprocess.run(
        f"python3 {CRON_CLI} {args}",
        shell=True, capture_output=True, text=True, timeout=10,
        env={**__import__("os").environ, "NANOBOT_CHANNEL": "test", "NANOBOT_CHAT_ID": "0"},
    )
    return r.returncode, r.stdout.strip(), r.stderr.strip()

print("🧪 Testing cron...")

# Add job
code, out, _ = cron('add --message "test ping" --every 60')
check("add job", code == 0 and "Created job" in out, f"code={code} out={out!r}")

# List jobs
code, out, _ = cron("list")
check("list jobs", code == 0 and "test ping" in out, f"code={code} out={out!r}")

# Read jobs.json
jobs_path = Path.home() / ".nanobot" / "cron" / "jobs.json"
data = json.loads(jobs_path.read_text())
test_jobs = [j for j in data["jobs"] if "test ping" in j.get("payload", {}).get("message", "")]
check("job in store", len(test_jobs) > 0)

# Cleanup
if test_jobs:
    job_id = test_jobs[0]["id"]
    code, out, _ = cron(f"remove --id {job_id}")
    check("remove job", code == 0 and "Removed" in out, f"code={code} out={out!r}")

total = passed + failed
print(f"\n{'✅' if failed == 0 else '❌'} {passed}/{total} passed")
sys.exit(0 if failed == 0 else 1)
```

## 使用原则

1. **用 check() 而不是 assert** — 测试不中断，能看到全部结果
2. **总是打印摘要** — `✅ X/Y passed` 或 `❌ X/Y passed`
3. **用 sys.exit(1) 表示失败** — 可以在 exec 输出中看到 exit code
4. **尽量不依赖 pip install** — 用标准库（urllib, json, subprocess, os, tempfile）
5. **测试后清理** — 删除临时文件和测试数据
6. **写入 workspace** — 测试脚本放在 `/root/.nanobot/workspace/` 下
