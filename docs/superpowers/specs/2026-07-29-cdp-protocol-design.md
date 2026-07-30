# CDPClient Protocol + Bit Browser Integration — Design Spec (v2)

## Context

当前 lanuage 通过 `CDPHelper` 直连本地 Chrome（localhost:9222）。newTaskTest
通过 bit.sh 启动比特浏览器 → WS URL → CDP 命令。两条路径需要共同 `CDPClient`
Protocol，同时修复 CDPHelper 丢 returncode 和 `_pipeline_form` 漏 frame_id 的问题。

review 确认 11 个现有单元测试通过（test_common.py）。

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
    raw_output: str = ""        # diagnostic only, not for success decision
    error: str | None = None
    returncode: int | None = None

@runtime_checkable
class CDPClient(Protocol):
    def eval(self, script: str, *, frame_id: str = "") -> Any:
        """Execute JS. Returns decoded result (str/list/dict/int).
        Raises CDPExecutionError on failure. Decodes 1-2 layers of JSON
        like existing CDPHelper.eval()."""
        ...

    def click(self, selector: str, *, frame_id: str = "") -> CommandResult:
        """CDP click. ok=True only after shared error classifier passes."""
        ...

    def snapshot(self) -> dict[str, Any]:
        """Returns parsed CDP snapshot dict. Raises CDPExecutionError on failure."""
        ...

    def form(self, selector: str, *,
             value: str | None = None,
             check: str | None = None,
             select: str | None = None,
             frame_id: str = "") -> CommandResult:
        """Form fill. ok=True only after shared error classifier passes."""
        ...

    def get_page_info(self) -> dict[str, str]:
        """Returns {"url": str, "title": str}. Raises CDPExecutionError on failure."""
        ...

    def wait_page_stable(self, timeout: float = 15) -> bool:
        """Polls document.readyState via eval. Timeout→False, transport error→CDPTransportError."""
        ...
```

**语义约束**：

| 方法 | 正常返回 | 错误 |
|------|---------|------|
| `eval` | 解码后的 `Any`（str/list/dict/int），含空串 `""` | `CDPExecutionError` |
| `click`/`form` | `CommandResult.ok=True` | `ok=False`（进程/协议错误）或 `CDPTransportError`（连接丢失） |
| `snapshot` | 已解析 `dict` | `CDPExecutionError` |
| `get_page_info` | `{"url": str, "title": str}` | `CDPExecutionError` |
| `wait_page_stable` | `True`/`False` | `CDPTransportError` |

**不在 Protocol 内**：`open`/`close`/`ws_url`/`profile_id`。

---

## 二、共享 SubprocessCDPClient

LegacyAdapter 和 BitCDPAdapter 调用同一个 cdp 二进制，只是 endpoint 来源不同。
抽一个共享实现，不复制两套：

```python
# lanuage_core/subprocess_cdp_client.py

@dataclass
class RawCommand:
    returncode: int
    stdout: str
    stderr: str


class CDPResultClassifier:
    """Shared error classification. raw_output is diagnostic-only."""
    
    @staticmethod
    def classify(rc: RawCommand) -> CommandResult:
        ok, error = True, None
        # 1. Non-zero returncode → fail
        if rc.returncode != 0:
            ok, error = False, rc.stderr or rc.stdout or f"exit code {rc.returncode}"
        # 2. rc=0 but stderr has CDP-level error
        elif rc.stderr and any(x in rc.stderr for x in 
            ("Error", "Exception", "BugError", "no page target", "Protocol error")):
            ok, error = False, rc.stderr[:200]
        # 3. rc=0 but stdout has error pattern (some CDP versions)
        elif rc.stdout and any(x in rc.stdout for x in
            ("Error", "Exception", "BugError", "no page target")):
            ok, error = False, rc.stdout[:200]
        return CommandResult(ok=ok, raw_output=rc.stdout + rc.stderr,
                            error=error, returncode=rc.returncode)


def _decode_eval_output(raw: str) -> Any:
    """Shared eval output decoder. Replicates existing CDPHelper behavior:
    decode 1-2 layers of JSON. If raw is a bare string, return it as-is.
    """
    stripped = raw.strip()
    if not stripped:
        return stripped
    # Try parse as JSON (existing behavior from CDPHelper.eval)
    try:
        import json
        decoded = json.loads(stripped)
        # If result is still a JSON string, decode one more layer
        if isinstance(decoded, str):
            try:
                return json.loads(decoded)
            except (json.JSONDecodeError, ValueError):
                return decoded
        return decoded
    except (json.JSONDecodeError, ValueError):
        return stripped


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
            raise CDPTransportError(f"cdp {subcmd} timed out after {timeout_s}s")
        except FileNotFoundError:
            raise CDPTransportError(f"cdp binary not found: {self._binary}")
        except OSError as e:
            raise CDPTransportError(f"cdp {subcmd} OS error: {e}")

    def eval(self, script: str, *, frame_id: str = "") -> Any:
        args = [script]
        if frame_id:
            args.extend(["--frame-id", frame_id])
        r = self._run("eval", args)
        if r.returncode != 0:
            raise CDPExecutionError(f"eval failed (rc={r.returncode}): {r.stderr[:200]}")
        return _decode_eval_output(r.stdout)

    def click(self, selector: str, *, frame_id: str = "") -> CommandResult:
        args = [selector]
        if frame_id:
            args.extend(["--frame-id", frame_id])
        r = self._run("click", args)
        return CDPResultClassifier.classify(r)

    def snapshot(self) -> dict[str, Any]:
        r = self._run("snapshot", [])
        if r.returncode != 0:
            raise CDPExecutionError(f"snapshot failed: {r.stderr[:200]}")
        import json
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            raise CDPExecutionError("snapshot: invalid JSON from CDP")

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
        return CDPResultClassifier.classify(r)

    def get_page_info(self) -> dict[str, str]:
        url = self.eval("window.location.href")
        title = self.eval("document.title")
        return {"url": str(url), "title": str(title)}

    def wait_page_stable(self, timeout: float = 15) -> bool:
        """Poll document.readyState via eval. Does NOT require new cdp wait subcommand."""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = self.eval("document.readyState")
                if state == "complete":
                    return True
            except CDPError:
                raise CDPTransportError("Connection lost during wait_page_stable")
            time.sleep(0.8)
        return False
```

**关键修正**：
- `eval() -> Any`：保留现有 JSON 解码行为（11 个单测通过）
- `form <selector> --value`：selector 是位置参数，不是 `--selector`
- `wait_page_stable`：eval 轮询，不需要新的 `cdp wait` 子命令
- `CDPResultClassifier`：统一分类 returncode + stderr + stdout 错误模式
- `raw_output`：仅供诊断，`CommandResult.ok` 由 classifier 决定

---

## 三、LegacyAdapter（静态 endpoint）

```python
# lanuage_core/legacy_adapter.py

class LegacyAdapter(SubprocessCDPClient):
    """Local Chrome (localhost:9222). Endpoint from WS_URL env or fixed debug port."""

    def __init__(self, ws_url: str = None, cdp_binary: str = None):
        import os, re
        ws = ws_url or os.environ.get("WS_URL", "ws://127.0.0.1:9222/devtools/browser/0")
        m = re.match(r'ws://([^:]+):(\d+)', ws)
        if not m:
            raise CDPTransportError(f"Invalid WS_URL: {ws}")
        host, port = m.group(1), m.group(2)
        binary = cdp_binary or os.environ.get("CDP_PATH", "/company/cdpcli/cdp")
        super().__init__(cdp_binary=binary, host=host, port=port)
```

---

## 四、BitCDPAdapter（Bit 浏览器 endpoint）

```python
# newTaskTest/src/bit_cdp_adapter.py

class BitCDPAdapter(SubprocessCDPClient):
    """Bit browser. Endpoint from BrowserLease."""

    def __init__(self, lease: "BrowserLease", cdp_binary: str):
        import re
        m = re.match(r'ws://([^:]+):(\d+)', lease.ws_url)
        if not m:
            raise CDPTransportError(f"Invalid ws_url in lease: {lease.ws_url}")
        super().__init__(cdp_binary=cdp_binary, host=m.group(1), port=m.group(2))
```

优先使用 Bit 官方 `/browser/open` 返回的 `http` 调试地址解析 host/port，不硬拆 WS URL。

---

## 五、BrowserManager 生命周期 — BrowserLease

```python
# newTaskTest/src/browser_lease.py

@dataclass(frozen=True)
class BrowserLease:
    profile_id: str
    pid: int | None
    generation: int
    ws_url: str
    http_endpoint: str
    owned: bool           # True if this lease opened the browser


class BrowserManager:
    """Manages BrowserLease lifecycle via bit.sh."""

    def acquire(self, profile_id: str, device: DeviceConfig,
                proxy: ProxyConfig | None = None) -> BrowserLease:
        """Health check → detect existing → open or attach.
        Increments generation on new open."""
        ...

    def release(self, lease: BrowserLease) -> None:
        """Idempotent close. Only closes if lease.owned=True."""
        ...

    def health_check(self, lease: BrowserLease) -> bool:
        """GET /health on http_endpoint."""
        ...
```

**关键规则**：
- `acquire()` 检查 `/browser/pids/alive`，存活则复用旧连接（generation 不变）
- `release()` 只关闭 `owned=True`，幂等
- generation 改变后清空 frame_id / marker / locator cache
- read-only 操作（eval/snapshot）最多重试 1 次
- click/form 发生不确定性错误时禁止自动重放
- 同一 profile 需要进程内锁

---

## 六、所有 click/form 必须检查 .ok

**硬约束**：所有 `click()`/`form()` 调用点必须检查 `result.ok`；`ok=False` 时当前 step 失败。

| 位置 | 当前行为 | 修正 |
|------|---------|------|
| `_execute_step` click | 忽略返回 | `result.ok` → 重试或 CLICK_FAILED |
| `_smart_form` form | `.strip()` 字符串判断 | `result.ok`/`result.raw_output` |
| `_select_option` click | 忽略返回 | `result.ok==False` → 重试或返回 False |
| `SelectExplorer` click | 忽略返回 | `result.ok==False` → 返回错误 status |
| RadioStrategy | 已用 eval 激活 ✅ | 不变（本轮已改为 JS 设置 checked） |
| `_pipeline_form` form | **漏 frame_id + 漏 check** | 补全 + 返回 `result.ok` |
| `_try_native` form | 忽略返回 | `result.ok` |

**`_pipeline_form` 完整修复**：
```python
result = self.cdp.form(
    selector,
    value=value,
    check=check,
    select=select,
    frame_id=frame_id,
)
return result.ok
```

---

## 七、迁移清单

### A 类：frame_id → keyword-only（机械搜索替换）

```bash
grep -rn "\.eval(" src/ | grep -v "frame_id=" | grep -v "^.*:#"
grep -rn "\.click(" src/ | grep -v "frame_id="
grep -rn "\.form(" src/ | grep -v "frame_id="
```

| 文件 | 调用点 |
|------|--------|
| `locator.py` | ~23 × `eval(js, fid)` → `eval(js, frame_id=fid)` |
| `json_executor.py` | ~40 × `eval(js, fid)` + ~15 × `click(sel, fid)` |
| `json_pipeline.py` | ~6 × `eval(js, frame_id=fid)`（已有 keyword） + ~8 × 无 frame_id（不变） |
| `element_finder.py` | 2 × `eval(js, frame_id)` → keyword |
| `select_explorer.py` | ~5 × `eval(js, fid)` + ~2 × `click(sel)` → keyword |

### B 类：click/form 返回值迁移

| 文件 | 方法 | 变更 |
|------|------|------|
| `json_executor._smart_form` | 5 × `cdp.form()` | `.strip()` → `result.raw_output`；错误判断 → `result.ok` |
| `json_executor._execute_step` (click) | 2 × `cdp.click()` | `result.ok` → 重试或 CLICK_FAILED |
| `json_executor._select_option` | 3 × `cdp.click()` | `not result.ok` → 返回 False |
| `json_pipeline._pipeline_form` | 1 × `cdp.form()` | **补全 frame_id + check**，返回 `result.ok` |
| `select_explorer.SelectExplorer` | 2 × `cdp.click()` | `result.ok` → 返回错误 status |
| `select_explorer._try_native` | 1 × `cdp.form()` | `result.ok` → 返回 SELECTED 或 NOT_VERIFIED |

### C 类：数据归一化

| 调用 | 变更 |
|------|------|
| `cdp.snapshot()` | LegacyAdapter/BitCDPAdapter 已解析 JSON；调用方删除多余 `json.loads()` |
| `cdp.eval()` | 返回类型保持 `Any`（不变）；异常时抛 `CDPExecutionError` |

### D 类：删除

- `common.py` 末尾的 `click_checked()` 独立函数 — RadioStrategy 已改用 eval 激活
- 不要借这次迁移把 RadioStrategy 改回 `cdp.click()`

### E 类：composition roots

| 入口 | 注入 |
|------|------|
| `web_editor.py` | `LegacyAdapter(ws_url)` → `JSONExecutor(cdp=adapter)` |
| `json_pipeline.py` CLI | `LegacyAdapter(ws_url)` |
| mock runner | `LegacyAdapter(ws_url)` |
| newTaskTest runner | `BitCDPAdapter(lease, cdp_binary)` |

---

## 八、文件结构

```
lanuage_core/                            # 新包目录
├── pyproject.toml
├── src/lanuage_core/
│   ├── __init__.py
│   ├── cdp_protocol.py                  # CDPClient Protocol + CommandResult + 异常
│   ├── subprocess_cdp_client.py         # SubprocessCDPClient + CDPResultClassifier
│   └── legacy_adapter.py               # LegacyAdapter (静态 endpoint)
└── tests/
    ├── test_cdp_protocol.py             # FakeCDP 单元测试
    └── test_legacy_adapter.py           # frame_id + _pipeline_form 回归

company/newTaskTest/
├── src/
│   ├── browser_lease.py                 # BrowserLease + BrowserManager（新）
│   ├── bit_cdp_adapter.py               # BitCDPAdapter（新）
│   ├── browser.py                       # BrowserManager（已有，改造）
│   ├── config.py                        # 已有
│   └── logger.py                        # 已有
├── config.yaml
├── bit.sh
├── cdp
├── pyproject.toml                       # 依赖 lanuage_core
└── tests/
    └── test_bit_adapter.py              # Bit 浏览器 iframe 集成测试
```

---

## 九、测试

### test_cdp_protocol.py（FakeCDP）

```python
class FakeCDP:
    def __init__(self):
        self.calls: list[dict] = []
        self._eval_results: dict[str, Any] = {}
        self._click_ok = True
        self._raise_on_eval = None

    def eval(self, script: str, *, frame_id: str = "") -> Any:
        self.calls.append({"method": "eval", "script": script, "frame_id": frame_id})
        if self._raise_on_eval:
            raise self._raise_on_eval
        return self._eval_results.get(script, "")

    def click(self, selector: str, *, frame_id: str = "") -> CommandResult:
        self.calls.append({"method": "click", "selector": selector, "frame_id": frame_id})
        return CommandResult(ok=self._click_ok)

    # ... form/snapshot/get_page_info/wait_page_stable


def test_eval_decodes_json():
    """现有行为：eval 返回已解码的 list/dict"""
    cdp = FakeCDP()
    cdp._eval_results["[1,2,3]"] = [1, 2, 3]
    result = cdp.eval("[1,2,3]")
    assert result == [1, 2, 3]


def test_frame_id_passed_to_eval():
    cdp = FakeCDP()
    cdp.eval("1+1", frame_id="abc123")
    assert cdp.calls[-1]["frame_id"] == "abc123"


def test_pipeline_form_forwards_frame_id():
    """回归：_pipeline_form 必须把 frame_id 同时传给 eval 和 form"""
    cdp = FakeCDP()
    cdp._eval_results = {"e.tagName": "SELECT"}
    pipeline._pipeline_form("select", select="US", frame_id="iframe1")
    eval_call = [c for c in cdp.calls if c["method"] == "eval"][0]
    form_call = [c for c in cdp.calls if c["method"] == "form"][0]
    assert eval_call["frame_id"] == "iframe1"
    assert form_call["frame_id"] == "iframe1"


def test_pipeline_form_forwards_check():
    """回归：_pipeline_form 必须把 check 传给 form"""
    cdp = FakeCDP()
    cdp._eval_results = {"String(e.checked)": "false"}
    pipeline._pipeline_form("#cb", check="true", frame_id="main")
    form_call = [c for c in cdp.calls if c["method"] == "form"][0]
    assert form_call["check"] == "true"


def test_eval_error_raises_cdp_error():
    cdp = FakeCDP()
    cdp._raise_on_eval = CDPExecutionError("timeout")
    with pytest.raises(CDPExecutionError):
        cdp.eval("1+1")


def test_classifier_nonzero_returncode():
    rc = RawCommand(returncode=1, stdout="", stderr="cdp: connection refused")
    result = CDPResultClassifier.classify(rc)
    assert not result.ok
    assert result.error


def test_classifier_bugerror_in_stderr():
    rc = RawCommand(returncode=0, stdout="ok", stderr="BugError: no page target")
    result = CDPResultClassifier.classify(rc)
    assert not result.ok


def test_json_executor_iframe_form():
    """真实 JSONExecutor 流程：iframe 内 form fill"""
    cdp = FakeCDP()
    executor = JSONExecutor({"steps": [{"action": "form", ...}]}, {}, cdp)
    executor.run()
    form_call = [c for c in cdp.calls if c["method"] == "form"][0]
    assert form_call["frame_id"] == "iframe1"
```

### test_bit_adapter.py（集成测试，需要比特浏览器，环境变量控制）

```python
@pytest.mark.skipif(not os.environ.get("BIT_TEST_ENABLED"), reason="requires Bit browser")
def test_form_in_iframe():
    """form fill 的值实际进入 iframe 内控件。frame_id 从 snapshot 动态获取。"""
    with BrowserManager(config.browser) as bm:
        lease = bm.acquire("test-profile", device)
        cdp = BitCDPAdapter(lease, config.cdp_binary)
        cdp.eval("window.location.href = 'http://fixture/iframe-form'")
        # 从 snapshot 动态获取 opaque frame ID，不硬编码
        snap = cdp.snapshot()
        frame_id = get_frame_id_by_url(snap, "iframe-form")
        result = cdp.form("#name", value="John", frame_id=frame_id)
        assert result.ok
        val = cdp.eval("document.querySelector('#name').value", frame_id=frame_id)
        assert val == "John"
```

---

## 十、实施顺序

| Phase | 内容 |
|-------|------|
| 1 | `cdp_protocol.py` — Protocol + CommandResult + 异常 + `_decode_eval_output` |
| 2 | `subprocess_cdp_client.py` — SubprocessCDPClient + CDPResultClassifier + `_run()` |
| 3 | `CDPHelper._run_command()` 底层改造 — 给 LegacyAdapter 提供 RawCommand |
| 4 | `LegacyAdapter` — 继承 SubprocessCDPClient，静态 endpoint |
| 5 | 迁移 A — frame_id → keyword-only（机械搜索替换） |
| 6 | 迁移 B — click/form 返回值迁移（所有调用点检查 .ok） |
| 7 | 迁移 C — snapshot 解析归一化 + `_pipeline_form` frame_id/check 修复 |
| 8 | 迁移 D — 删除 `click_checked()` |
| 9 | 迁移 E — composition roots（web_editor/CLI/mock runner 注入 LegacyAdapter） |
| 10 | `BrowserLease` + `BrowserManager` 改造（newTaskTest） |
| 11 | `BitCDPAdapter`（newTaskTest） |
| 12 | 测试 — FakeCDP 单测 + Bit iframe 集成测试 |
| 13 | `pyproject.toml` — lanuage_core 包 + newTaskTest 依赖 |
