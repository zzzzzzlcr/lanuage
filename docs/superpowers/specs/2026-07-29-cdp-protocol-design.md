# CDPClient Protocol + Bit Browser Integration — Design Spec (v3)

## Context

lanuage 通过 `CDPHelper` 直连本地 Chrome（localhost:9222）。newTaskTest
通过 bit.sh 启动比特浏览器 → WS URL → CDP 命令。两条路径需要共同 `CDPClient`
Protocol，同时修复 `_pipeline_form` 漏 frame_id/check 和 returncode 丢失的问题。

现有 11 个 test_common.py 单测全部通过，eval 解码契约已锁定。

---

## 一、CDPClient Protocol（冻结版）

```python
# lanuage_core/cdp_protocol.py

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

class CDPError(RuntimeError): ...
class CDPTransportError(CDPError): ...
class CDPExecutionError(CDPError): ...

@dataclass(frozen=True)
class CommandResult:
    ok: bool
    raw_output: str = ""
    error: str | None = None
    returncode: int | None = None
    error_category: str | None = None  # "transport" | "execution" | "ambiguous_mutation"

@runtime_checkable
class CDPClient(Protocol):
    def eval(self, script: str, *, frame_id: str = "") -> Any: ...
    def click(self, selector: str, *, frame_id: str = "") -> CommandResult: ...
    def snapshot(self) -> dict[str, Any]: ...
    def form(self, selector: str, *,
             value: str | None = None,
             check: str | None = None,
             select: str | None = None,
             frame_id: str = "") -> CommandResult: ...
    def get_page_info(self) -> dict[str, str]: ...
    def wait_page_stable(self, timeout: float = 15) -> bool: ...
```

**语义约束**：

| 方法 | 正常返回 | 错误 |
|------|---------|------|
| `eval` | `Any`（str/list/dict/int），含空串 `""` | `CDPExecutionError` 或 `CDPTransportError` |
| `click`/`form` | `CommandResult.ok=True` | `ok=False` + `error_category` |
| `snapshot`/`get_page_info` | dict | `CDPExecutionError` 或 `CDPTransportError` |
| `wait_page_stable` | `True`/`False` | `CDPTransportError`（连接丢失） |

---

## 二、共享 SubprocessCDPClient

### 2.1 CLI argv（精确匹配现有 common.py）

| 方法 | CLI 格式 | 证据 |
|------|---------|------|
| `eval` | `cdp eval <script> [--frame-id <id>]` | common.py L130 |
| `click` | `cdp click --selector <sel> [--frame-id <id>]` | common.py L105-108 |
| `form` | `cdp form <sel> [--value v] [--check c] [--select s] [--frame-id id]` | common.py L175 |
| `snapshot` | `cdp snapshot` | common.py L78 |

### 2.2 eval 解码器（精确复制现有契约）

```python
def _decode_eval_output(raw: str) -> Any:
    """Replicates existing CDPHelper.eval() behavior exactly.
    
    11 test_common.py tests lock this contract:
    - Parse raw as JSON. If not JSON, return raw string.
    - If result is a string AND starts with { or [, parse one more layer.
    - "42", "true", "null" stay as strings (NOT converted to int/bool/None).
    """
    stripped = raw.strip()
    if not stripped:
        return stripped
    import json as _json
    try:
        decoded = _json.loads(stripped)
    except (_json.JSONDecodeError, ValueError):
        return stripped
    # Second decode ONLY for objects/arrays, NOT primitives
    if isinstance(decoded, str) and decoded and decoded[0] in "{[":
        try:
            return _json.loads(decoded)
        except (_json.JSONDecodeError, ValueError):
            return decoded
    return decoded
```

### 2.3 统一错误分类器（覆盖所有命令）

```python
class CDPResultClassifier:
    """Shared error classifier for all CDP commands.
    Covers: non-zero returncode, stderr errors, stdout-only errors,
    connection loss, timeout, binary not found.
    """
    
    @staticmethod
    def classify(rc: RawCommand, subcmd: str = "") -> CommandResult:
        ok, error, category = True, None, None
        
        # Transport errors already raised in _run()
        
        # 1. Non-zero returncode → execution error
        if rc.returncode != 0:
            ok, error = False, (rc.stderr or rc.stdout or f"exit {rc.returncode}")[:200]
            category = "execution"
        # 2. rc=0 but stderr has CDP-level error
        elif rc.stderr and _has_error_pattern(rc.stderr):
            ok, error = False, rc.stderr[:200]
            category = "execution" if "BugError" not in rc.stderr else "transport"
        # 3. rc=0 but stdout-only error (some CDP versions)
        elif rc.stdout and _has_error_pattern(rc.stdout):
            ok, error = False, rc.stdout[:200]
            category = "execution"
        
        return CommandResult(ok=ok, raw_output=rc.stdout + rc.stderr,
                            error=error, returncode=rc.returncode,
                            error_category=category)


ERROR_PATTERNS = (
    "Error", "Exception", "BugError", "no page target",
    "Protocol error", "failed to create client", "not connected",
    "No such element", "missing target",
)

def _has_error_pattern(text: str) -> bool:
    return any(p in text for p in ERROR_PATTERNS)
```

### 2.4 SubprocessCDPClient 实现

```python
class SubprocessCDPClient:
    """Shared CDPClient implementation — called by both Legacy and Bit adapters."""

    def __init__(self, cdp_binary: str, host: str, port: str):
        self._binary = cdp_binary
        self._host = host
        self._port = port

    def _run(self, subcmd: str, args: list[str], timeout_s: float = 15) -> RawCommand:
        cmd = [self._binary, subcmd] + args + ["--host", self._host, "--port", self._port]
        try:
            import subprocess
            r = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s, shell=False)
            return RawCommand(returncode=r.returncode, stdout=r.stdout, stderr=r.stderr)
        except subprocess.TimeoutExpired:
            raise CDPTransportError(f"cdp {subcmd} timed out")
        except FileNotFoundError:
            raise CDPTransportError(f"cdp binary not found: {self._binary}")
        except OSError as e:
            raise CDPTransportError(f"cdp {subcmd} OS error: {e}")

    # -- eval --
    def eval(self, script: str, *, frame_id: str = "") -> Any:
        args = [script]
        if frame_id:
            args.extend(["--frame-id", frame_id])
        r = self._run("eval", args)
        if r.returncode != 0:
            raise CDPExecutionError(f"eval failed (rc={r.returncode}): {r.stderr[:200]}")
        result = CDPResultClassifier.classify(r, "eval")
        if not result.ok:
            raise CDPExecutionError(f"eval: {result.error}")
        return _decode_eval_output(r.stdout)

    # -- click --
    def click(self, selector: str, *, frame_id: str = "") -> CommandResult:
        args = ["--selector", selector]
        if frame_id:
            args.extend(["--frame-id", frame_id])
        r = self._run("click", args)
        return CDPResultClassifier.classify(r, "click")

    # -- snapshot --
    def snapshot(self) -> dict[str, Any]:
        r = self._run("snapshot", [])
        result = CDPResultClassifier.classify(r, "snapshot")
        if not result.ok:
            raise CDPExecutionError(f"snapshot: {result.error}")
        import json
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            raise CDPExecutionError("snapshot: invalid JSON")

    # -- form --
    def form(self, selector: str, *,
             value=None, check=None, select=None,
             frame_id: str = "") -> CommandResult:
        args = [selector]
        if value is not None:
            args.extend(["--value", str(value)])
        if check is not None:
            args.extend(["--check", str(check)])
        if select is not None:
            args.extend(["--select", str(select)])
        if frame_id:
            args.extend(["--frame-id", frame_id])
        r = self._run("form", args)
        return CDPResultClassifier.classify(r, "form")

    # -- get_page_info --
    def get_page_info(self) -> dict[str, str]:
        url = self.eval("window.location.href")
        title = self.eval("document.title")
        return {"url": str(url), "title": str(title)}

    # -- wait_page_stable --
    def wait_page_stable(self, timeout: float = 15) -> bool:
        """Poll until 3 consecutive 'complete' readings. Replicates existing
        CDPHelper.wait_page_stable() behavior (common.py L271-304)."""
        import time
        deadline = time.time() + timeout
        stable_count = 0
        while time.time() < deadline:
            try:
                state = self.eval("document.readyState")
            except CDPError:
                raise CDPTransportError("Connection lost")
            if state == "complete":
                stable_count += 1
                if stable_count >= 3:
                    return True
            else:
                stable_count = 0
            time.sleep(0.8)
        return False
```

### 2.5 重试规则（冻结）

| 操作 | 透明重试 | 说明 |
|------|---------|------|
| `snapshot` | 最多 1 次 | 纯只读 |
| `get_page_info` | 最多 1 次 | 纯只读（底层调 eval） |
| CDP 连接就绪检查 | 最多 1 次 | 连接建立阶段 |
| `eval` | **不重试** | 大量 eval 是 mutation（导航/点击/表单修改） |
| `click` | **禁止自动重放** | 可能重复提交 |
| `form` | **禁止自动重放** | 可能重复填写 |
| mutation 结果不确定 | **不重放** | 由执行器做页面状态验证 |

---

## 三、LegacyAdapter + BitCDPAdapter

```python
# lanuage_core/legacy_adapter.py

class LegacyAdapter(SubprocessCDPClient):
    """Local Chrome (localhost:9222)."""

    def __init__(self, ws_url: str = None, cdp_binary: str = None):
        import os, re
        ws = ws_url or os.environ.get("WS_URL",
            "ws://127.0.0.1:9222/devtools/browser/0")
        m = re.match(r'ws://([^:]+):(\d+)', ws)
        if not m:
            raise CDPTransportError(f"Invalid WS_URL: {ws}")
        binary = cdp_binary or os.environ.get("CDP_PATH", "/company/cdpcli/cdp")
        super().__init__(cdp_binary=binary, host=m.group(1), port=m.group(2))
```

```python
# newTaskTest/src/bit_cdp_adapter.py

class BitCDPAdapter(SubprocessCDPClient):
    """Bit browser — endpoint from BrowserLease."""

    def __init__(self, lease: "BrowserLease", cdp_binary: str):
        # Prefer http_endpoint (Bit Local API), fall back to ws_url regex
        host, port = lease.http_endpoint.rsplit(":", 1) if ":" in lease.http_endpoint else ("", "")
        if not host:
            import re
            m = re.match(r'ws://([^:]+):(\d+)', lease.ws_url)
            if not m:
                raise CDPTransportError(f"Invalid lease: {lease.ws_url}")
            host, port = m.group(1), m.group(2)
        super().__init__(cdp_binary=cdp_binary, host=host, port=port)
```

---

## 四、BrowserManager 生命周期 — SessionRecord + BrowserLease

### Phase 1 决策：不做自动重连

连接失效 → 结束当前 runtime → 重新 `acquire()` lease → 重建 adapter/executor。
不实现断线恢复、不自动重放 mutation。

### 数据结构

```python
@dataclass(frozen=True)
class DebugEndpoint:
    host: str
    port: int


@dataclass
class SessionRecord:
    session_id: str
    profile_id: str
    pid: int
    generation: int
    endpoint: DebugEndpoint
    manager_owned: bool
    refcount: int


@dataclass(frozen=True)
class BrowserLease:
    lease_id: str
    session_id: str
    profile_id: str
    generation: int
    ws_url: str
    http_endpoint: str
```

### 生命周期规则

| 规则 | 说明 |
|------|------|
| `acquire()` | `/browser/pids/alive` 检测现有进程 → 存在则复用（generation 不变）→ 否则 `bit.sh open` → 新 generation |
| `release(lease)` | 核对 lease_id + generation + 当前 PID；`refcount -= 1`；仅在最后一个引用且 `manager_owned=True` 时调用 `bit.sh close` |
| 旧 lease stale release | PID 或 generation 不匹配 → no-op，打 warning |
| `health_check()` | Bit Local API `/health`（不是 Chrome DevTools /health） |
| CDP readiness | 连接建立后 `cdp snapshot` 验证 `frameId` 存在 |

### 简化为 Phase 1

```python
class BrowserManager:
    def open(self, profile_id: str, device: DeviceConfig, proxy=None) -> BrowserLease:
        """Open browser, return immutable lease. No reconnection."""
        ...

    def close(self, lease: BrowserLease):
        """Release lease. Idempotent."""
        ...

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close(self._active_lease)
```

**Phase 1 约束**：
- 不自动重连
- `health_check()` 仅用于诊断，不自动恢复
- lease 失效后外部重建 adapter/executor

---

## 五、所有 click/form 必须检查 .ok + AST 守卫

```python
# AST guard: every bare call to click/form MUST capture result
# grep guard: grep -rn "\.click(\|\.form(" src/ | grep -v "result\|\.ok\|=.*click\|=.*form"
```

| 文件 | 方法 | 变更 |
|------|------|------|
| `_execute_step` | `cdp.click()` ×2 | `result.ok==False` → retry or CLICK_FAILED |
| `_smart_form` | `cdp.form()` ×5 | `.strip()` → `result.raw_output`; error check → `result.ok` |
| `_select_option` | `cdp.click()` ×3 | `not result.ok` → return False |
| `_pipeline_form` | `cdp.form()` ×1 | **补全 frame_id + check**; return `result.ok` |
| `SelectExplorer.execute` | `cdp.click()` ×2 | `not result.ok` → return error status |
| `SelectExplorer._try_native` | `cdp.form()` ×1 | `not result.ok` → return NOT_VERIFIED |

---

## 六、迁移清单

### A 类：frame_id → keyword-only

```bash
grep -rn "\.eval(" src/ | grep -v "frame_id=" | grep -v "^.*:#"
grep -rn "\.click(" src/ | grep -v "frame_id="
grep -rn "\.form(" src/ | grep -v "frame_id="
```

| 文件 | 调用点 |
|------|--------|
| `locator.py` | ~23 × `eval(js, fid)` → `eval(js, frame_id=fid)` |
| `json_executor.py` | ~40 × `eval(js, fid)` + ~15 × `click(sel, fid)` |
| `json_pipeline.py` | ~6 × `eval(js, frame_id=fid)`（已有） + ~8 × 无 frame_id（不变） |
| `element_finder.py` | 2 × `eval(js, frame_id)` → keyword |
| `select_explorer.py` | ~5 × `eval(js, fid)` + ~2 × `click(sel)` |

### B 类：click/form → check .ok（见上表）

### C 类：数据归一化

| 调用 | 变更 |
|------|------|
| `cdp.snapshot()` | Adapter 已解析 JSON；调用方删除多余 `json.loads()` |
| `cdp.eval()` | 类型保持 `Any`（不变） |
| `cdp.get_page_info()` | 保证返回 `{"url": str, "title": str}` |

### D 类：删除

- `common.py` 末尾的 `click_checked()` 独立函数

### E 类：composition roots

| 入口 | 注入 |
|------|------|
| `web_editor.py` | `LegacyAdapter(ws_url)` → `JSONExecutor(cdp=adapter)` |
| `json_pipeline.py` CLI | `LegacyAdapter(ws_url)` |
| mock runner | `LegacyAdapter(ws_url)` |
| newTaskTest | `BitCDPAdapter(lease, cdp_binary)` |

### F 类：公共 API

```python
# lanuage_core/__init__.py
from .cdp_protocol import CDPClient, CommandResult, CDPError, CDPTransportError, CDPExecutionError
from .subprocess_cdp_client import SubprocessCDPClient, CDPResultClassifier
from .legacy_adapter import LegacyAdapter

def run_automation(description: str, profile: dict, cdp: CDPClient, *, navigate_url: str = "") -> dict:
    """Public API: run a form automation from NL description."""
    from json_pipeline import JSONPipeline
    ...
```

---

## 七、文件结构

```
lanuage_core/
├── pyproject.toml
├── src/lanuage_core/
│   ├── __init__.py                      # Public API: run_automation + exports
│   ├── cdp_protocol.py                  # CDPClient Protocol + CommandResult + 异常
│   ├── subprocess_cdp_client.py         # SubprocessCDPClient + CDPResultClassifier
│   └── legacy_adapter.py               # LegacyAdapter
└── tests/
    ├── test_cdp_decoder.py              # _decode_eval_output exact contract
    ├── test_classifier.py               # CDPResultClassifier edge cases
    ├── test_legacy_adapter.py           # frame_id + _pipeline_form + check

company/newTaskTest/
├── src/
│   ├── session.py                       # SessionRecord + BrowserLease + BrowserManager
│   ├── bit_cdp_adapter.py               # BitCDPAdapter
│   ├── browser.py                       # 已有，适配 SessionRecord
│   ├── config.py
│   └── logger.py
├── tests/
│   └── test_bit_adapter.py
├── config.yaml
├── pyproject.toml                       # depends: lanuage_core
├── bit.sh
└── cdp
```

---

## 八、测试

### test_cdp_decoder.py

```python
def test_eval_keeps_numeric_string():
    assert _decode_eval_output("42") == "42"

def test_eval_keeps_boolean_string():
    assert _decode_eval_output("true") == "true"

def test_eval_keeps_null_string():
    assert _decode_eval_output("null") == "null"

def test_eval_decodes_flat_json():
    assert _decode_eval_output('[1,2,3]') == [1, 2, 3]

def test_eval_decodes_nested_json_string():
    assert _decode_eval_output('"[1,2,3]"') == [1, 2, 3]

def test_eval_decodes_object():
    assert _decode_eval_output('{"a":1}') == {"a": 1}

def test_eval_empty_string():
    assert _decode_eval_output("") == ""

def test_eval_plain_text():
    assert _decode_eval_output("hello world") == "hello world"
```

### test_classifier.py

```python
def test_nonzero_returncode():
    rc = RawCommand(returncode=1, stdout="", stderr="cdp: refused")
    result = CDPResultClassifier.classify(rc)
    assert not result.ok
    assert result.error_category == "execution"

def test_bugerror_in_stderr():
    rc = RawCommand(returncode=0, stdout="ok", stderr="BugError: no page target")
    result = CDPResultClassifier.classify(rc)
    assert not result.ok
    assert result.error_category == "transport"

def test_timeout_raw():
    with pytest.raises(CDPTransportError):
        SubprocessCDPClient._run("eval", ["1+1"], timeout_s=0.001)

def test_click_argv():
    cdp = FakeCDP()
    cdp.click("#btn", frame_id="f1")
    call = cdp.calls[-1]
    assert "--selector" in call["args"]
    assert "#btn" in call["args"]

def test_form_argv():
    cdp = FakeCDP()
    cdp.form("select", value="US", frame_id="f1")
    call = cdp.calls[-1]
    assert call["args"][0] == "select"  # positional, not --selector
    assert "--value" in call["args"]
    assert "--frame-id" in call["args"]

def test_click_grep_guard():
    """Every click/form call must capture result."""
    import subprocess, os
    src_dir = os.path.join(os.path.dirname(__file__), "../../src")
    # grep for bare calls that don't capture result
    result = subprocess.run(
        f"grep -rn '\.click(\|\.form(' {src_dir}/*.py | grep -v 'result\|\.ok\|=\|#\|_run(\|_run_command'",
        shell=True, capture_output=True, text=True
    )
    assert result.stdout.strip() == "", f"Bare click/form calls found:\n{result.stdout}"
```

### test_legacy_adapter.py

```python
def test_pipeline_form_forwards_frame_id():
    cdp = FakeCDP()
    cdp._eval_results = {"e.tagName": "SELECT"}
    pipeline._pipeline_form("select", select="US", frame_id="iframe1")
    eval_frame = [c for c in cdp.calls if c["method"] == "eval"][0]["frame_id"]
    form_frame = [c for c in cdp.calls if c["method"] == "form"][0]["frame_id"]
    assert eval_frame == "iframe1"
    assert form_frame == "iframe1"

def test_pipeline_form_forwards_check():
    cdp = FakeCDP()
    cdp._eval_results = {"String(e.checked)": "false"}
    pipeline._pipeline_form("#cb", check="true", frame_id="main")
    form_call = [c for c in cdp.calls if c["method"] == "form"][0]
    assert form_call["check"] == "true"

def test_json_executor_iframe_form():
    """JSONExecutor iframe form fill: fake locator returns frame_id."""
    cdp = FakeCDP()
    # Inject fake locator that returns frame_id
    executor = JSONExecutor({"steps": [{"action": "form", "field": {"label": "Name"}}]},
                            {"task_id": "test"}, cdp)
    executor.locator = FakeLocator(frame_id="iframe1")
    executor.run()
    form_call = [c for c in cdp.calls if c["method"] == "form"][0]
    assert form_call["frame_id"] == "iframe1"
```

### test_bit_adapter.py（环境变量控制）

```python
@pytest.mark.skipif(not os.environ.get("BIT_TEST_ENABLED"), reason="requires Bit browser")
def test_lease_lifecycle():
    with BrowserManager(config.browser) as bm:
        lease = bm.open("test-profile", device)
        assert lease.ws_url.startswith("ws://")
        assert lease.http_endpoint
        cdp = BitCDPAdapter(lease, config.cdp_binary)
        # CDP readiness
        snap = cdp.snapshot()
        assert "frameId" in str(snap)
        bm.close(lease)

@pytest.mark.skipif(not os.environ.get("BIT_TEST_ENABLED"), reason="requires Bit browser")
def test_form_in_iframe():
    """form fill 的值进入 iframe 内控件。frame_id 从 snapshot 动态获取。"""
    with BrowserManager(config.browser) as bm:
        lease = bm.open("test-profile", device)
        cdp = BitCDPAdapter(lease, config.cdp_binary)
        cdp.eval("window.location.href = 'http://fixture/iframe-form'")
        snap = cdp.snapshot()
        frame_id = get_frame_id_by_url(snap, "iframe-form")
        result = cdp.form("#name", value="John", frame_id=frame_id)
        assert result.ok
        val = cdp.eval("document.querySelector('#name').value", frame_id=frame_id)
        assert val == "John"
        bm.close(lease)
```

---

## 九、实施顺序

| Phase | 内容 |
|-------|------|
| 1 | `cdp_protocol.py` — Protocol + CommandResult + 异常 |
| 2 | `subprocess_cdp_client.py` — SubprocessCDPClient + classifier + decoder |
| 3 | test_cdp_decoder.py + test_classifier.py — 解码器 + 分类器精确验证 |
| 4 | `CDPHelper._run_command()` — 底层 RawCommand 出口 |
| 5 | `LegacyAdapter` |
| 6 | 迁移 A — frame_id → keyword-only |
| 7 | 迁移 B — click/form → check .ok |
| 8 | 迁移 C — snapshot 归一化 + `_pipeline_form` frame_id/check |
| 9 | 迁移 D — 删除 `click_checked()` |
| 10 | 迁移 E — composition roots |
| 11 | AST grep guard — 确保无裸 click/form 调用 |
| 12 | `session.py` — SessionRecord + BrowserLease + BrowserManager（Phase 1 无重连） |
| 13 | `BitCDPAdapter` |
| 14 | test_bit_adapter.py — 集成测试（环境变量控制） |
| 15 | 迁移 F — `lanuage_core/__init__.py` public API + `pyproject.toml` |
