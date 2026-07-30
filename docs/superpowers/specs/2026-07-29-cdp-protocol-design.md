# CDPClient Protocol + Bit Browser Integration — Design Spec (v4)

## Context

lanuage 通过 `CDPHelper` 直连本地 Chrome，newTaskTest 通过 bit.sh 启动比特浏览器。
两条路径需要共同 `CDPClient` Protocol，同时修复 returncode 丢失和
`_pipeline_form` 漏 frame_id/check 的问题。现有 11 个 test_common.py 通过。

---

## 一、CDPClient Protocol（冻结）

```python
# lanuage_core/cdp_protocol.py

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class CDPError(RuntimeError): ...
class CDPTransportError(CDPError): ...
class CDPExecutionError(CDPError): ...
class CDPAmbiguousMutation(CDPError):
    """click/form completed with ambiguous result — caller must verify page state."""


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    raw_output: str = ""
    error: str | None = None
    returncode: int | None = None
    error_category: str | None = None   # "transport" | "execution" | "ambiguous_mutation"


@dataclass
class RawCommand:
    """Structured subprocess result — shared across all adapters."""
    returncode: int
    stdout: str
    stderr: str


@runtime_checkable
class CDPClient(Protocol):
    def eval(self, script: str, *, frame_id: str = "") -> Any: ...
    def click(self, selector: str, *, frame_id: str = "") -> CommandResult: ...
    def snapshot(self) -> dict[str, Any]: ...
    def form(self, selector: str, *,
             value: str | None = None, check: str | None = None,
             select: str | None = None,
             frame_id: str = "") -> CommandResult: ...
    def get_page_info(self) -> dict[str, str]: ...
    def wait_page_stable(self, timeout: float = 15) -> bool: ...
```

---

## 二、eval 解码器（精确契约）

```python
def _decode_eval_output(raw: str) -> Any:
    """Replicates existing CDPHelper.eval() behavior.
    
    11 test_common.py tests lock this contract.
    "42" → "42", "true" → "true", "null" → "null"
    Only objects/arrays get a second decode layer.
    """
    stripped = raw.strip()
    if not stripped:
        return stripped
    import json as _json
    try:
        decoded = _json.loads(stripped)
    except (_json.JSONDecodeError, ValueError):
        return stripped
    if isinstance(decoded, str) and decoded and decoded[0] in "{[":
        try:
            return _json.loads(decoded)
        except (_json.JSONDecodeError, ValueError):
            return decoded
    return decoded
```

---

## 三、统一错误分类器

```python
# SubprocessCDPClient 私有

_TRANSPORT_PATTERNS = (
    "connection refused",
    "no page target",
    "no target",
    "not connected",
    "failed to create client",
    "cannot connect",
    "Browser has disconnected",
)

_EXECUTION_STDERR_PREFIXES = (
    "Error:",
    "Exception:",
    "BugError:",
    "Protocol error:",
)

def _is_transport_error(stderr: str, stdout: str) -> bool:
    combined = (stderr + stdout).lower()
    return any(p in combined for p in _TRANSPORT_PATTERNS)


def _is_execution_error(stderr: str) -> bool:
    """Only classify stderr lines that start with known error prefixes.
    Never scan stdout — it may contain page content, form values, or JSON payloads."""
    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith(_EXECUTION_STDERR_PREFIXES):
            return True
    return False


def _classify_raw(rc: "RawCommand", subcmd: str, timeout_occurred: bool = False
                  ) -> "CommandResult":
    """Classify subprocess output into CommandResult.

    Rules:
    1. Timeout on mutation (click/form) → ambiguous_mutation
    2. Timeout on read (eval/snapshot) → CDPTransportError (raised by caller)
    3. Non-zero returncode + transport pattern → transport error
    4. Non-zero returncode + no transport pattern → execution error
    5. rc=0 + stderr has execution prefix → execution error
    6. rc=0 + no error indicators → OK
    """
    ok, error, category = True, None, None

    if timeout_occurred:
        if subcmd in ("click", "form"):
            return CommandResult(ok=False, raw_output=rc.stdout + rc.stderr,
                                error="timeout", returncode=None,
                                error_category="ambiguous_mutation")
        else:
            raise CDPTransportError(f"cdp {subcmd} timed out")

    if rc.returncode != 0:
        error_text = (rc.stderr or rc.stdout or f"exit {rc.returncode}")[:200]
        if _is_transport_error(rc.stderr, rc.stdout):
            ok, category = False, "transport"
        else:
            ok, category = False, "execution"
        return CommandResult(ok=ok, raw_output=rc.stdout + rc.stderr,
                            error=error_text, returncode=rc.returncode,
                            error_category=category)

    if _is_execution_error(rc.stderr):
        return CommandResult(ok=False, raw_output=rc.stdout + rc.stderr,
                            error=rc.stderr[:200], returncode=0,
                            error_category="execution")

    return CommandResult(ok=True, raw_output=rc.stdout + rc.stderr,
                        returncode=0, error_category=None)


def _classify_eval_snapshot(rc: "RawCommand") -> "CommandResult":
    """For eval/snapshot: if classified as transport, raise CDPTransportError.
    Otherwise raise CDPExecutionError on any failure."""
    result = _classify_raw(rc, "eval")
    if not result.ok:
        if result.error_category == "transport":
            raise CDPTransportError(result.error or "CDP transport failure")
        raise CDPExecutionError(result.error or f"CDP command failed (rc={rc.returncode})")
    return result
```

---

## 四、SubprocessCDPClient 实现

```python
class SubprocessCDPClient:
    """Shared implementation — called by LegacyAdapter and BitCDPAdapter."""

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
            return RawCommand(returncode=r.returncode, stdout=r.stdout,
                            stderr=r.stderr)
        except subprocess.TimeoutExpired:
            raise  # let caller catch and pass timeout_occurred=True
        except FileNotFoundError:
            raise CDPTransportError(f"cdp binary not found: {self._binary}")
        except OSError as e:
            raise CDPTransportError(f"cdp {subcmd} OS error: {e}")

    def eval(self, script: str, *, frame_id: str = "") -> Any:
        args = [script]
        if frame_id: args.extend(["--frame-id", frame_id])
        try:
            r = self._run("eval", args)
        except subprocess.TimeoutExpired:
            raise CDPTransportError("cdp eval timed out")
        _classify_eval_snapshot(r)
        return _decode_eval_output(r.stdout)

    def click(self, selector: str, *, frame_id: str = "") -> CommandResult:
        args = ["--selector", selector]
        if frame_id: args.extend(["--frame-id", frame_id])
        try:
            r = self._run("click", args)
        except subprocess.TimeoutExpired:
            return _classify_raw(RawCommand(0, "", "timeout"), "click", timeout_occurred=True)
        return _classify_raw(r, "click")

    def snapshot(self) -> dict[str, Any]:
        try:
            r = self._run("snapshot", [])
        except subprocess.TimeoutExpired:
            raise CDPTransportError("cdp snapshot timed out")
        _classify_eval_snapshot(r)
        import json
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            raise CDPExecutionError("snapshot: invalid JSON")

    def form(self, selector: str, *,
             value=None, check=None, select=None,
             frame_id: str = "") -> CommandResult:
        args = [selector]
        if value is not None: args.extend(["--value", str(value)])
        if check is not None: args.extend(["--check", str(check)])
        if select is not None: args.extend(["--select", str(select)])
        if frame_id: args.extend(["--frame-id", frame_id])
        try:
            r = self._run("form", args)
        except subprocess.TimeoutExpired:
            return _classify_raw(RawCommand(0, "", "timeout"), "form", timeout_occurred=True)
        return _classify_raw(r, "form")

    def get_page_info(self) -> dict[str, str]:
        return {"url": str(self.eval("window.location.href")),
                "title": str(self.eval("document.title"))}

    def wait_page_stable(self, timeout: float = 15) -> bool:
        """Poll until 3 consecutive 'complete' readings AND body text length
        stable for 2 consecutive readings. Replicates existing behavior."""
        import time
        deadline = time.time() + timeout
        stable_count = 0
        last_body_len = -1
        body_stable = 0
        while time.time() < deadline:
            try:
                state = self.eval("document.readyState")
                body_len = len(self.eval(
                    "(function(){return document.body?document.body.innerText||'':'';})()"))
            except CDPError:
                raise CDPTransportError("Connection lost")
            if state == "complete":
                stable_count += 1
                if body_len == last_body_len:
                    body_stable += 1
                else:
                    body_stable = 0
                last_body_len = body_len
                if stable_count >= 3 and body_stable >= 2:
                    return True
            else:
                stable_count = 0
                body_stable = 0
            time.sleep(0.8)
        return False
```

### 重试规则

| 操作 | 透明重试 | 说明 |
|------|---------|------|
| `snapshot` | 最多 1 次 | 在 `_run` 失败且为 transport 错误时重试 |
| `get_page_info` | 不重试 | 底层 eval 不重试 |
| CDP readiness | 最多 1 次 | 连接建立阶段 |
| `eval` | **不重试** | 可能是 mutation |
| `click` / `form` | **禁止自动重放** | ambiguous_mutation 时由执行器验证 |
| ambiguous_mutation | 不重放 | 即使 step.retry=3，底层也只执行 1 次 |

---

## 五、BrowserLease 生命周期

### 数据结构

```python
@dataclass(frozen=True)
class DebugEndpoint:
    host: str
    port: int

    @property
    def http_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_ws_url(cls, ws_url: str) -> "DebugEndpoint":
        import re
        m = re.match(r'ws://([^:]+):(\d+)', ws_url)
        if not m: raise ValueError(f"invalid ws_url: {ws_url}")
        return cls(host=m.group(1), port=m.group(2))

    @classmethod
    def from_http_url(cls, http_url: str) -> "DebugEndpoint":
        h = http_url.replace("http://", "").replace("https://", "").split(":")[0]
        p = http_url.rsplit(":", 1)[-1]
        return cls(host=h, port=int(p))


@dataclass(frozen=True)
class BrowserLease:
    lease_id: str
    profile_id: str
    generation: int
    ws_url: str
    debug_endpoint: DebugEndpoint     # structured, not string
    owned: bool
    config_fingerprint: str           # hash(proxy+device+profile)
```

### SessionRecord + BrowserManager

```python
@dataclass
class SessionRecord:
    profile_id: str
    pid: int
    generation: int
    debug_endpoint: DebugEndpoint
    config_fingerprint: str
    managed: bool         # true = opened by us, false = attached to existing
    refcount: int
    lease_ids: set[str]   # all active lease IDs for this session


class BrowserManager:
    def __init__(self, config: "BrowserConfig"):
        self._sessions: dict[str, SessionRecord] = {}  # profile_id → session
        self._leases: dict[str, BrowserLease] = {}      # lease_id → lease
        self._lock = threading.Lock()

    # ── acquire ──
    def acquire(self, profile_id: str, device: DeviceConfig,
                proxy: ProxyConfig | None = None) -> BrowserLease:
        fingerprint = _make_fingerprint(profile_id, device, proxy)
        with self._lock:
            # 1. Check /browser/pids/alive for existing session
            alive = self._check_alive_sessions(profile_id)
            for pid, endpoint in alive:
                session = self._sessions.get(profile_id)
                if session and session.pid == pid:
                    if session.config_fingerprint != fingerprint:
                        # Config mismatch → force reopen
                        self._close_session(session)
                        continue
                    # Valid existing → attach
                    session.refcount += 1
                    lease = BrowserLease(
                        lease_id=_new_id(),
                        profile_id=profile_id,
                        generation=session.generation,
                        ws_url=f"ws://{endpoint.host}:{endpoint.port}/...",
                        debug_endpoint=endpoint,
                        owned=False,
                        config_fingerprint=fingerprint,
                    )
                    session.lease_ids.add(lease.lease_id)
                    self._leases[lease.lease_id] = lease
                    return lease

            # 2. Open new browser
            ws_url, http_endpoint = self._bit_open(profile_id, device, proxy)
            endpoint = DebugEndpoint.from_http_url(http_endpoint)
            self._verify_cdp_readiness(ws_url)

            # 3. Create session
            pid = self._get_pid(profile_id, endpoint)
            generation = int(time.time())
            session = SessionRecord(
                profile_id=profile_id, pid=pid, generation=generation,
                debug_endpoint=endpoint, config_fingerprint=fingerprint,
                managed=True, refcount=1, lease_ids=set(),
            )
            lease = BrowserLease(
                lease_id=_new_id(),
                profile_id=profile_id,
                generation=generation,
                ws_url=ws_url,
                debug_endpoint=endpoint,
                owned=True,
                config_fingerprint=fingerprint,
            )
            session.lease_ids.add(lease.lease_id)
            self._sessions[profile_id] = session
            self._leases[lease.lease_id] = lease
            return lease

    # ── release (idempotent, stale-safe) ──
    def release(self, lease: BrowserLease) -> None:
        with self._lock:
            if lease.lease_id not in self._leases:
                return  # already released
            stored = self._leases[lease.lease_id]
            if stored.generation != lease.generation:
                return  # stale lease
            del self._leases[lease.lease_id]
            session = self._sessions.get(lease.profile_id)
            if not session:
                return
            session.lease_ids.discard(lease.lease_id)
            session.refcount -= 1
            if session.refcount <= 0 and session.managed:
                self._close_session(session)

    # ── close session (owned-only) ──
    def _close_session(self, session: SessionRecord) -> None:
        """Only close if managed. Remove from registry."""
        if session.managed:
            self._bit_close(session.profile_id)
        self._sessions.pop(session.profile_id, None)

    # ── partial-open rollback ──
    def _bit_open(self, ...) -> tuple[str, str]:
        try:
            return self._do_bit_open(...)
        except Exception:
            # Rollback: close if partially opened
            self._bit_close(profile_id)
            raise
```

### Phase 1 约束

- **不自动重连**：连接失效 → 结束 runtime → 重新 `acquire`
- **单进程独占**：同一 profile 的 BrowserManager 必须在同一进程内
- **跨进程锁**：Phase 2 引入文件锁，Phase 1 不处理
- **`/browser/pids/alive`** 只返回 PID 列表；与已知 session PID 匹配即可，不重建 endpoint

### Adapter 接口

```python
class BitCDPAdapter(SubprocessCDPClient):
    def __init__(self, lease: BrowserLease, cdp_binary: str):
        ep = lease.debug_endpoint  # structured, no regex parsing
        super().__init__(cdp_binary=cdp_binary, host=ep.host, port=str(ep.port))
```

---

## 六、包边界

```
lanuage_core/                   # 只放 Protocol + Adapter + Browser
├── pyproject.toml
├── src/lanuage_core/
│   ├── __init__.py             # exports Protocol types + LegacyAdapter + SubprocessCDPClient
│   ├── cdp_protocol.py         # CDPClient + CommandResult + RawCommand + 异常
│   ├── subprocess_cdp_client.py # SubprocessCDPClient + classifier + decoder
│   └── legacy_adapter.py       # LegacyAdapter
└── tests/

lanuage/                        # 现有仓库，保持不变
├── src/
│   ├── json_pipeline.py        # 接收 CDPClient 实例
│   ├── json_executor.py        # 接收 CDPClient 实例
│   └── ...
└── tests/

lanuage_automation/             # 新：Pipeline + Executor + Public API
├── pyproject.toml              # depends: lanuage_core
├── src/lanuage_automation/
│   ├── __init__.py
│   ├── pipeline.py             # JSONPipeline（从 lanuage/src 移入，或 symlink）
│   ├── executor.py             # JSONExecutor
│   ├── locator.py
│   ├── select_explorer.py
│   └── api.py                  # run_automation() public entry point
└── tests/

company/newTaskTest/            # 消费者
├── pyproject.toml              # depends: lanuage_automation, lanuage_core
├── src/
│   └── ...
└── tests/
```

**实施顺序**：
1. 先在 lanuage 内做依赖注入（不改包结构）
2. 提取 lanuage_core
3. newTaskTest 集成
4. 提取 lanuage_automation（可选，可延后）

---

## 七、.ok 迁移 + AST 守卫

### AST 守卫（Python 而非 grep）

```python
# tests/test_call_convention.py

import ast
import pathlib

SRC = pathlib.Path("lanuage/src")

FORBIDDEN = {
    "click": {"min_positional": 1, "max_positional": 1},
    "form":  {"min_positional": 1, "max_positional": 1},
    "eval":  {"min_positional": 1, "max_positional": 1},
}


def test_no_bare_click_form_calls():
    """Every cdp.click()/cdp.form() call must capture the return value."""
    violations = []
    for py_file in SRC.glob("**/*.py"):
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = _get_func_name(node)
                if func in ("cdp.click", "cdp.form", "self.cdp.click", "self.cdp.form"):
                    # Must be assigned or part of a return/comparison
                    parent = _find_parent(tree, node)
                    if isinstance(parent, ast.Expr):
                        violations.append(f"{py_file}:{node.lineno} bare {func}")
                    # Must have exactly one positional arg (selector)
                    pos_args = [a for a in node.args if not isinstance(a, ast.keyword)]
                    if len(pos_args) != 1:
                        violations.append(
                            f"{py_file}:{node.lineno} {func} has {len(pos_args)} positional args")
    assert not violations, "\n".join(violations)


def test_all_click_form_check_ok():
    """Every captured click/form result must be checked for .ok."""
    ...
```

### .ok 迁移全量表

| 文件 | 方法 | 调用次数 | 变更 |
|------|------|---------|------|
| `json_executor._execute_step` | `cdp.click()` | 2 | `result.ok==False` → retry |
| `json_executor._execute_step` (click text find) | `cdp.click()` | 1 | `result.ok==False` → return False |
| `json_executor._smart_form` | `cdp.form()` | 5 | `.strip()` → `result.raw_output`; fail → `not result.ok` |
| `json_executor._smart_form` | `cdp.click()` (combobox) | 2 | `result.ok==False` → skip |
| `json_executor._select_option` | `cdp.click()` | 3 | `not result.ok` → return False |
| `json_executor._run_stateful` | `cdp.click()` | 1 | `not result.ok` → continue |
| `json_executor._quiz_loop` | `cdp.click()` | 2 | `not result.ok` → continue |
| `json_pipeline._pipeline_form` | `cdp.form()` | 1 | **补全 frame_id + check**; return `result.ok` |
| `json_pipeline._run_one_step` (click) | `cdp.click()` | 1 | `result.ok==False` → return error |
| `select_explorer.SelectExplorer` | `cdp.click()` | 2 | `not result.ok` → return error status |
| `select_explorer._try_native` | `cdp.form()` | 1 | `not result.ok` → return NOT_VERIFIED |

---

## 八、迁移清单

### A: frame_id → keyword-only（~93 个调用点）

```bash
grep -rn "\.eval(" src/ | grep -v "frame_id=" | wc -l   # ~55
grep -rn "\.click(" src/ | grep -v "frame_id=" | wc -l  # ~19
grep -rn "\.form(" src/ | grep -v "frame_id=" | wc -l   # ~10
```

### B: .ok propagation（见上表）

### C: snapshot/json.loads 归一化

### D: 删除 click_checked()

### E: composition roots

| 入口 | 变更 |
|------|------|
| `web_editor.py` | `LegacyAdapter(ws_url)` → `JSONExecutor(cdp=adapter)` |
| `json_pipeline.py` CLI | `LegacyAdapter(ws_url)` |
| mock runner | `LegacyAdapter(ws_url)` |
| newTaskTest | `BitCDPAdapter(lease, cdp_binary)` |

---

## 九、测试

### 解码器测试

```python
def test_eval_string_preserved():
    assert _decode_eval_output('"42"') == "42"       # JSON string

def test_eval_numeric_not_converted():
    assert _decode_eval_output("42") == 42           # JSON number → int

def test_eval_plain_text_preserved():
    assert _decode_eval_output("hello") == "hello"   # not JSON

def test_eval_empty():
    assert _decode_eval_output("") == ""

def test_eval_nested_json_array():
    assert _decode_eval_output('"[1,2,3]"') == [1, 2, 3]

def test_eval_nested_json_object():
    assert _decode_eval_output('{"a":1}') == {"a": 1}
```

### 分类器测试

```python
def test_nonzero_rc_with_transport():
    rc = RawCommand(1, "", "connection refused")
    r = _classify_raw(rc, "click")
    assert not r.ok and r.error_category == "transport"

def test_nonzero_rc_without_transport():
    rc = RawCommand(1, "", "some error")
    r = _classify_raw(rc, "click")
    assert not r.ok and r.error_category == "execution"

def test_rc0_stderr_execution_prefix():
    rc = RawCommand(0, "", "BugError: no page target")
    r = _classify_raw(rc, "eval")
    assert not r.ok and r.error_category == "execution"

def test_click_timeout_ambiguous():
    r = _classify_raw(RawCommand(0, "", "timeout"), "click", timeout_occurred=True)
    assert not r.ok and r.error_category == "ambiguous_mutation"

def test_classifier_never_scans_stdout_payloads():
    """Page content in stdout must not trigger error detection."""
    rc = RawCommand(0, '<html>Error: page content</html>', "")
    r = _classify_raw(rc, "snapshot")
    assert r.ok  # stdout not scanned for execution errors

def test_transport_on_eval_raises():
    rc = RawCommand(1, "", "connection refused")
    with pytest.raises(CDPTransportError):
        _classify_eval_snapshot(rc)
```

### argv 测试

```python
def test_click_argv():
    cdp = SubprocessCDPClient("/fake/cdp", "127.0.0.1", "9222")
    # Monkey-patch _run to capture args
    captured = []
    cdp._run = lambda cmd, args: captured.extend(args) or RawCommand(0, "", "")
    cdp.click("#btn", frame_id="f1")
    assert captured[0] == "--selector"
    assert captured[1] == "#btn"
    assert "--frame-id" in captured

def test_form_argv():
    cdp = SubprocessCDPClient("/fake/cdp", "127.0.0.1", "9222")
    captured = []
    cdp._run = lambda cmd, args: captured.extend(args) or RawCommand(0, "", "")
    cdp.form("select#country", value="US", frame_id="f1")
    assert captured[0] == "select#country"  # positional selector
    assert "--value" in captured
    assert "--frame-id" in captured
```

### BrowserLease 测试

```python
def test_lease_refcount_and_release():
    bm = BrowserManager(config)
    lease = bm.acquire("p1", device)
    assert lease.owned and not lease.debug_endpoint is None
    bm.release(lease)
    assert "p1" not in bm._sessions  # refcount 0, managed, closed

def test_stale_lease_release_is_noop():
    bm = BrowserManager(config)
    lease1 = bm.acquire("p1", device)
    bm.release(BrowserLease(lease_id="stale", profile_id="p1",
                             generation=0, ws_url="ws://...",
                             debug_endpoint=DebugEndpoint("h", 1),
                             owned=False, config_fingerprint=""))
    assert "p1" in bm._sessions  # still alive

def test_mismatched_config_fingerprint_forces_reopen():
    ...

def test_partial_open_rollback():
    ...
```

### Bit 集成测试

```python
@pytest.mark.skipif(not os.environ.get("BIT_TEST_ENABLED"), reason="requires Bit browser")
class TestBitAdapter:
    @pytest.fixture(autouse=True)
    def browser(self):
        bm = BrowserManager(config)
        lease = bm.acquire("test-profile", device)
        yield lease, bm
        bm.release(lease)

    def test_iframe_form(self, browser):
        lease, bm = browser
        cdp = BitCDPAdapter(lease, CdpBinary)
        cdp.eval("window.location.href = f'{FIXTURE_URL}/iframe-form'")
        snap = cdp.snapshot()
        frame_id = _find_frame_id_by_url(snap, "iframe-form")
        assert frame_id, "iframe not found in snapshot"
        result = cdp.form("#name", value="John", frame_id=frame_id)
        assert result.ok
        val = cdp.eval("document.querySelector('#name').value", frame_id=frame_id)
        assert val == "John"
```

---

## 十、实施顺序

| Phase | 内容 | 可交付 |
|-------|------|--------|
| 1 | `cdp_protocol.py` — Protocol + CommandResult + RawCommand + 异常 | ✅ |
| 2 | `subprocess_cdp_client.py` — SubprocessCDPClient + classifier + decoder | ✅ |
| 3 | 解码器测试 + 分类器测试 + argv 测试 | ✅ |
| **── 冻结：Protocol + Adapter 可实施 ──** | | |
| 4 | `CDPHelper._run_command()` 底层改造 | |
| 5 | `LegacyAdapter` | |
| 6 | 迁移 A — frame_id keyword-only (~93 处) | |
| 7 | 迁移 B — click/form check .ok | |
| 8 | 迁移 C — snapshot/json.loads 归一化 | |
| 9 | 迁移 D — 删除 click_checked() | |
| 10 | AST 守卫 — 验证无裸 click/form 调用 | |
| 11 | 迁移 E — composition roots | |
| **── 冻结：lanuage 内部迁移完成 ──** | | |
| 12 | `BrowserLease` + `BrowserManager` | |
| 13 | BrowserLease 测试（refcount/stale/attach/rollback） | |
| 14 | `BitCDPAdapter` | |
| 15 | Bit 集成测试（iframe + dynamic frame_id + finally release） | |
| 16 | `pyproject.toml` — lanuage_core + lanuage_automation + newTaskTest | |
