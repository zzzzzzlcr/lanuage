# CDPClient Protocol + Bit Browser Integration — Design Spec (v5)

## Context

lanuage `CDPHelper` 直连本地 Chrome，newTaskTest 通过 bit.sh 启动比特浏览器。
两条路径需共同 `CDPClient` Protocol。现有 11 个 test_common.py 通过。

---

## 一、CDPClient Protocol

```python
# lanuage_core/cdp_protocol.py

import subprocess  # module-level for except clauses
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class CDPError(RuntimeError): ...
class CDPTransportError(CDPError): ...
class CDPExecutionError(CDPError): ...
class CDPAmbiguousMutation(CDPError):
    """click/form timed out or connection lost mid-mutation — state unknown."""


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    raw_output: str = ""
    error: str | None = None
    returncode: int | None = None
    error_category: str | None = None
    # "transport" | "execution" | "ambiguous_mutation" | None


@dataclass
class RawCommand:
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

## 二、解码器 + 分类器

### decoder

```python
def _decode_eval_output(raw: str) -> Any:
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

### classifier — transport FIRST, regardless of returncode

```python
# ALL lowercase — matched against (stderr + stdout).lower()
_TRANSPORT_PATTERNS = (
    "connection refused",
    "no page target",
    "no target",
    "not connected",
    "failed to create client",
    "cannot connect",
    "browser has disconnected",
    "page has been closed",
    "target closed",
    "websocket disconnected",
)

_EXECUTION_STDERR_PREFIXES = (
    "error:",
    "exception:",
    "bugerror:",
    "protocol error:",
    "uncaught",
    "eval failed",
    "cannot find",
    "no such",
)


def _classify_raw(rc: "RawCommand", subcmd: str,
                  timeout_occurred: bool = False) -> "CommandResult":
    """Classify subprocess output.

    Rules (checked in order):
    1. Timeout on mutation (click/form) → ambiguous_mutation
    2. Timeout on read (eval/snapshot) → CDPTransportError (caller raises)
    3. Transport pattern found (stderr+stdout, rc ANY) → transport
    4. Non-zero returncode + no transport → execution
    5. rc=0 + stderr has execution prefix → execution
    6. rc=0 + no errors → OK
    """
    combined = (rc.stderr + rc.stdout).lower()

    # 1. Timeout
    if timeout_occurred:
        if subcmd in ("click", "form"):
            return CommandResult(ok=False, raw_output=rc.stdout + rc.stderr,
                                error="timeout", error_category="ambiguous_mutation")
        raise CDPTransportError(f"cdp {subcmd} timed out")

    # 2. Transport check FIRST — any returncode
    if any(p in combined for p in _TRANSPORT_PATTERNS):
        return CommandResult(ok=False, raw_output=rc.stdout + rc.stderr,
                            error=(rc.stderr or rc.stdout)[:200],
                            returncode=rc.returncode,
                            error_category="transport")

    # 3. Non-zero returncode → execution
    if rc.returncode != 0:
        return CommandResult(ok=False, raw_output=rc.stdout + rc.stderr,
                            error=(rc.stderr or f"exit {rc.returncode}")[:200],
                            returncode=rc.returncode,
                            error_category="execution")

    # 4. rc=0 — check stderr prefixes only (do NOT scan stdout for errors)
    stderr_lower = rc.stderr.lower()
    if any(stderr_lower.startswith(p) for p in _EXECUTION_STDERR_PREFIXES):
        return CommandResult(ok=False, raw_output=rc.stdout + rc.stderr,
                            error=rc.stderr[:200], returncode=0,
                            error_category="execution")

    return CommandResult(ok=True, raw_output=rc.stdout + rc.stderr, returncode=0)


def _classify_eval_snapshot(rc: "RawCommand") -> "CommandResult":
    result = _classify_raw(rc, "eval")
    if not result.ok:
        if result.error_category == "transport":
            raise CDPTransportError(result.error or "CDP transport failure")
        raise CDPExecutionError(result.error or f"CDP command failed (rc={rc.returncode})")
    return result
```

**关键修正**：
- transport 检查放在 returncode 判断之前，rc=0 也能被正确归类
- 所有 pattern 全部小写，匹配 `(stderr+stdout).lower()`
- stdout 只在 transport 检查中参与；execution 检查只扫描 stderr 前缀

---

## 三、SubprocessCDPClient

```python
class SubprocessCDPClient:
    def __init__(self, cdp_binary: str, host: str, port: str):
        self._binary = cdp_binary
        self._host = host
        self._port = port

    def _run(self, subcmd: str, args: list[str], timeout_s: float = 15) -> RawCommand:
        cmd = [self._binary, subcmd] + args + ["--host", self._host, "--port", self._port]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s, shell=False)
            return RawCommand(r.returncode, r.stdout, r.stderr)
        except subprocess.TimeoutExpired:
            raise CDPTimeout(subcmd)   # custom exception, no NameError
        except FileNotFoundError:
            raise CDPTransportError(f"cdp binary not found: {self._binary}")
        except OSError as e:
            raise CDPTransportError(f"cdp {subcmd} OS error: {e}")


class CDPTimeout(CDPError):
    """Raised by _run on timeout. Caller decides: ambiguous_mutation or transport."""
    def __init__(self, subcmd: str):
        self.subcmd = subcmd
        super().__init__(f"cdp {subcmd} timed out")


def eval(self, script: str, *, frame_id: str = "") -> Any:
    args = [script]
    if frame_id: args.extend(["--frame-id", frame_id])
    try:
        r = self._run("eval", args)
    except CDPTimeout:
        raise CDPTransportError("cdp eval timed out")
    _classify_eval_snapshot(r)
    return _decode_eval_output(r.stdout)


def click(self, selector: str, *, frame_id: str = "") -> CommandResult:
    args = ["--selector", selector]
    if frame_id: args.extend(["--frame-id", frame_id])
    try:
        r = self._run("click", args)
    except CDPTimeout:
        return _classify_raw(RawCommand(0, "", "timeout"), "click", timeout_occurred=True)
    return _classify_raw(r, "click")


def snapshot(self) -> dict[str, Any]:
    try:
        r = self._run("snapshot", [])
    except CDPTimeout:
        raise CDPTransportError("cdp snapshot timed out")
    _classify_eval_snapshot(r)
    import json
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise CDPExecutionError("snapshot: invalid JSON")


def form(self, selector: str, *, value=None, check=None, select=None,
         frame_id: str = "") -> CommandResult:
    args = [selector]
    if value is not None: args.extend(["--value", str(value)])
    if check is not None: args.extend(["--check", str(check)])
    if select is not None: args.extend(["--select", str(select)])
    if frame_id: args.extend(["--frame-id", frame_id])
    try:
        r = self._run("form", args)
    except CDPTimeout:
        return _classify_raw(RawCommand(0, "", "timeout"), "form", timeout_occurred=True)
    return _classify_raw(r, "form")


def get_page_info(self) -> dict[str, str]:
    return {"url": str(self.eval("window.location.href")),
            "title": str(self.eval("document.title"))}


def wait_page_stable(self, timeout: float = 15) -> bool:
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
            raise CDPTransportError("Connection lost during wait_page_stable")
        if state == "complete":
            stable_count += 1
            body_stable = body_stable + 1 if body_len == last_body_len else 0
            last_body_len = body_len
            if stable_count >= 3 and body_stable >= 2:
                return True
        else:
            stable_count = 0
            body_stable = 0
        time.sleep(0.8)
    return False
```

### 重试规则（冻结进入 executor 实现）

| 场景 | 行为 |
|------|------|
| `snapshot` transport error | 重试 1 次 |
| `get_page_info` | 不重试 |
| CDP readiness check | 重试 1 次 |
| 普通 `eval` | 不重试 |
| `click`/`form` **前** transport (command not issued) | 允许按 step.retry 重试 |
| `click`/`form` 返回 `ambiguous_mutation` | **禁止重试** — 状态未知，由执行器验证页面 |
| `click`/`form` 后 transport（连接在 mutation 后丢失） | **禁止重放** — 可能已生效 |
| `click`/`form` 返回 execution error | 允许按 step.retry 重试 |

**执行器规则**：
```python
# In _execute_step:
# retry=3 means: command_not_issued → up to 3 click attempts
# After first successful click dispatch, set clicks_issued += 1
# clicks_issued >= 1 and subsequent error → do NOT re-click, verify page state instead
```

---

## 四、BrowserLease 生命周期

### 数据结构

```python
@dataclass(frozen=True)
class DebugEndpoint:
    host: str
    port: int


@dataclass(frozen=True)
class BrowserLease:
    lease_id: str
    session_id: str          # links to SessionRecord, NOT profile_id
    profile_id: str
    generation: int
    ws_url: str
    debug_endpoint: DebugEndpoint
    owned: bool
    config_fingerprint: str


@dataclass
class SessionRecord:
    session_id: str          # primary key
    profile_id: str
    pid: int
    generation: int
    debug_endpoint: DebugEndpoint
    config_fingerprint: str
    managed: bool
    refcount: int
    active_lease_ids: set[str]


class BrowserConfigConflict(CDPError):
    """Existing session has different config fingerprint."""
    def __init__(self, profile_id, existing, requested):
        super().__init__(
            f"Config conflict for {profile_id}: existing={existing} requested={requested}")
```

### BrowserManager

```python
class BrowserManager:
    def __init__(self, config: "BrowserConfig"):
        self._sessions: dict[str, SessionRecord] = {}   # session_id → session
        self._profile_index: dict[str, str] = {}          # profile_id → session_id
        self._leases: dict[str, BrowserLease] = {}
        self._lock = threading.Lock()
        self._generation = 0

    def _next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def acquire(self, profile_id: str, device: DeviceConfig,
                proxy: ProxyConfig | None = None) -> BrowserLease:
        fingerprint = _make_fingerprint(profile_id, device, proxy)
        with self._lock:
            # 1. Check /browser/pids/alive for existing session
            existing_session_id = self._profile_index.get(profile_id)
            if existing_session_id:
                session = self._sessions[existing_session_id]
                alive_pids = self._list_alive_pids(profile_id)
                if session.pid in alive_pids:
                    if session.config_fingerprint != fingerprint:
                        if session.refcount > 0:
                            raise BrowserConfigConflict(
                                profile_id, session.config_fingerprint, fingerprint)
                        self._close_session_locked(session)
                    else:
                        session.refcount += 1
                        lease = BrowserLease(
                            lease_id=_new_id(),
                            session_id=session.session_id,
                            profile_id=profile_id,
                            generation=session.generation,
                            ws_url=f"ws://{session.debug_endpoint.host}:{session.debug_endpoint.port}/...",
                            debug_endpoint=session.debug_endpoint,
                            owned=False,
                            config_fingerprint=fingerprint,
                        )
                        session.active_lease_ids.add(lease.lease_id)
                        self._leases[lease.lease_id] = lease
                        return lease

            # 2. No usable existing session → external check
            if profile_id in self._profile_index:
                # We have a session record but PID dead → clean up then open new
                old_session = self._sessions.get(self._profile_index[profile_id])
                if old_session:
                    self._close_session_locked(old_session)

            # 3. Check for unknown external browsers
            ext_pids = self._list_alive_pids(profile_id)
            if ext_pids and profile_id not in self._profile_index:
                raise ExternalSessionConflict(
                    f"External browser running for {profile_id}: pids={ext_pids}")

            # 4. Open new browser — full rollback on any failure
            ws_url, http_endpoint = None, None
            try:
                ws_url, http_endpoint = self._bit_open(profile_id, device, proxy)
                endpoint = DebugEndpoint.from_http_url(http_endpoint)
                self._verify_cdp_readiness(ws_url)
                pid = self._get_pid(profile_id, endpoint)
            except Exception:
                # Rollback everything that was partially opened
                if ws_url or http_endpoint:
                    try: self._bit_close(profile_id)
                    except Exception: pass
                raise

            session_id = _new_id()
            generation = self._next_generation()
            session = SessionRecord(
                session_id=session_id, profile_id=profile_id,
                pid=pid, generation=generation,
                debug_endpoint=endpoint,
                config_fingerprint=fingerprint,
                managed=True, refcount=1,
                active_lease_ids=set(),
            )
            lease = BrowserLease(
                lease_id=_new_id(),
                session_id=session_id,
                profile_id=profile_id,
                generation=generation,
                ws_url=ws_url,
                debug_endpoint=endpoint,
                owned=True,
                config_fingerprint=fingerprint,
            )
            session.active_lease_ids.add(lease.lease_id)
            self._sessions[session_id] = session
            self._profile_index[profile_id] = session_id
            self._leases[lease.lease_id] = lease
            return lease

    def release(self, lease: BrowserLease) -> None:
        """Idempotent. Stale leases are no-ops."""
        with self._lock:
            if lease.lease_id not in self._leases:
                return
            stored = self._leases[lease.lease_id]
            # Verify lease matches stored record
            if stored.session_id != lease.session_id:
                return
            if stored.generation != lease.generation:
                return
            del self._leases[lease.lease_id]
            session = self._sessions.get(lease.session_id)
            if not session:
                return
            session.active_lease_ids.discard(lease.lease_id)
            session.refcount -= 1
            if session.refcount <= 0 and session.managed:
                self._close_session_locked(session)

    def _close_session_locked(self, session: SessionRecord) -> None:
        if session.managed:
            try:
                self._bit_close(session.profile_id)
            finally:
                pass  # always remove from registry
        self._sessions.pop(session.session_id, None)
        if self._profile_index.get(session.profile_id) == session.session_id:
            del self._profile_index[session.profile_id]
        # Clean any dangling leases
        for lid in list(session.active_lease_ids):
            self._leases.pop(lid, None)
```

**关键安全性**：
- `release()` 通过 `session_id` 查找 session，不会误关不同 generation 的浏览器
- stale lease（lease_id 不存在 / session_id 不匹配 / generation 不同）→ no-op
- config conflict + refcount>0 → `BrowserConfigConflict`，不强制关闭
- 打开全过程在 try/except 中，失败时回滚
- 未知外部 session → `ExternalSessionConflict`，不强制 `_bit_open`

---

## 五、包结构（一次性确定，不延后）

```
lanuage_core/
├── pyproject.toml
├── src/lanuage_core/
│   ├── __init__.py
│   ├── cdp_protocol.py
│   ├── subprocess_cdp_client.py
│   └── legacy_adapter.py
└── tests/

lanuage/                        # 现有仓库
├── src/                         # 不动，只做依赖注入
│   └── ...
├── pyproject.toml               # depends: lanuage_core
└── tests/

company/newTaskTest/
├── pyproject.toml               # depends: lanuage_core, lanuage (editable)
├── src/
│   ├── browser_manager.py
│   ├── bit_cdp_adapter.py
│   └── ...
└── tests/
```

**规则**：
- `pyproject.toml` 在各自包创建时立即添加，不留到最后
- lanuage 保持单一源码树，不 move 也不 symlink
- `run_automation()` 属于 lanuage（不是 lanuage_core），签名：
```python
def run_automation(description: str, profile: dict, cdp: CDPClient, *,
                   llm_client, model: str = "deepseek-v4-flash",
                   navigate_url: str = "") -> tuple[dict, "ValidationResult"]:
    ...
```

---

## 六、.ok 迁移 + AST 守卫

### AST 守卫（真实实现）

```python
# tests/test_call_convention.py

import ast, pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

_CLICK_FORM_METHODS = {
    "cdp.click", "self.cdp.click",
    "cdp.form", "self.cdp.form",
}


def _get_func_name(node: ast.Call) -> str | None:
    """Extract function name from call node. Returns 'self.cdp.click' etc."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        obj = _qualname(func.value)
        return f"{obj}.{func.attr}" if obj else func.attr
    return None


def _qualname(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _qualname(node.value)
        return f"{inner}.{node.attr}" if inner else node.attr
    return None


class _CallVisitor(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self.bare_calls: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call):
        name = _get_func_name(node)
        if name in _CLICK_FORM_METHODS:
            pos_args = [a for a in node.args if not isinstance(a, ast.keyword)]
            if len(pos_args) != 1:
                self.bare_calls.append(
                    (node.lineno, f"{name}: {len(pos_args)} positional args (expected 1)"))
            # Check parent is NOT an expression statement (must be assigned/returned/compared)
            if not hasattr(node, '_checked'):
                self.bare_calls.append(
                    (node.lineno, f"{name}: result not captured or .ok unchecked"))
        self.generic_visit(node)


def test_no_bare_click_form():
    violations = []
    for py_file in SRC.glob("**/*.py"):
        if py_file.name.startswith("__"): continue
        tree = ast.parse(py_file.read_text())
        v = _CallVisitor(str(py_file))
        v.visit(tree)
        for line, msg in v.bare_calls:
            violations.append(f"{py_file.name}:{line}: {msg}")
    assert not violations, "\n".join(violations)
```

### .ok 全量迁移表

| 文件 | 方法 | 调用 | 变更 |
|------|------|------|------|
| `json_executor._execute_step` | `cdp.click()` | 2 | `result.ok==False` → retry (command_not_issued only) |
| `json_executor._execute_step` | `cdp.click()` | 1 | `result.ok==False` → return False |
| `json_executor._smart_form` | `cdp.form()` | 5 | `.strip()` → `result.raw_output` |
| `json_executor._smart_form` | `cdp.click()` | 3 | `result.ok==False` → skip / return False |
| `json_executor._select_option` | `cdp.click()` | 3 | `not result.ok` → return False |
| `json_executor._run_stateful` | `cdp.click()` | 1 | `not result.ok` → skip |
| `json_executor._quiz_loop` | `cdp.click()` | 2 | `not result.ok` → skip |
| `json_executor._execute_step` | `cdp.form()` | 1 | fallback form → check `.ok` |
| `json_pipeline._pipeline_form` | `cdp.form()` | 1 | **补全 frame_id + check**; return `result.ok` |
| `json_pipeline._run_one_step` (click) | `cdp.click()` | 1 | `result.ok==False` → return error |
| `select_explorer.SelectExplorer` | `cdp.click()` | 2 | `not result.ok` → return error status |
| `select_explorer._try_native` | `cdp.form()` | 1 | `not result.ok` → return NOT_VERIFIED |
| `locator._try_label_for` | `cdp.form()` | 1 | docstring only, no code change |

---

## 七、测试

### 解码器

```python
def test_decoder_string_json():
    assert _decode_eval_output('"42"') == "42"

def test_decoder_number_json():
    assert _decode_eval_output("42") == 42

def test_decoder_plain_text():
    assert _decode_eval_output("hello") == "hello"

def test_decoder_empty():
    assert _decode_eval_output("") == ""

def test_decoder_nested_array():
    assert _decode_eval_output('"[1,2,3]"') == [1, 2, 3]

def test_decoder_object():
    assert _decode_eval_output('{"a":1}') == {"a": 1}
```

### 分类器

```python
def test_transport_rc_nonzero():
    r = _classify_raw(RawCommand(1, "", "connection refused"), "click")
    assert not r.ok and r.error_category == "transport"

def test_transport_rc_zero():
    """rc=0 + transport pattern → transport, not execution"""
    r = _classify_raw(RawCommand(0, "", "BugError: no page target"), "click")
    assert not r.ok and r.error_category == "transport"

def test_execution_rc_nonzero():
    r = _classify_raw(RawCommand(1, "", "something went wrong"), "click")
    assert not r.ok and r.error_category == "execution"

def test_execution_rc_zero_stderr_prefix():
    r = _classify_raw(RawCommand(0, "", "error: selector not found"), "eval")
    assert not r.ok and r.error_category == "execution"

def test_click_timeout_ambiguous():
    r = _classify_raw(RawCommand(0, "", "timeout"), "click", timeout_occurred=True)
    assert not r.ok and r.error_category == "ambiguous_mutation"

def test_stdout_not_scanned_for_execution():
    """Page content in stdout must not be classified as error."""
    r = _classify_raw(RawCommand(0, '<html>error: something</html>', ""), "snapshot")
    assert r.ok

def test_transport_before_returncode():
    """Transport check happens regardless of returncode."""
    r = _classify_raw(RawCommand(0, "", "browser has disconnected"), "click")
    assert not r.ok and r.error_category == "transport"

def test_lowercase_match():
    """Pattern 'browser has disconnected' matches 'Browser has disconnected'."""
    r = _classify_raw(RawCommand(0, "", "Browser has disconnected"), "click")
    assert not r.ok and r.error_category == "transport"
```

### argv

```python
def test_click_argv():
    cdp = SubprocessCDPClient("/bin/cdp", "127.0.0.1", "9999")
    captured = {}
    def fake_run(subcmd, args, timeout_s=15):
        captured["args"] = [subcmd] + args
        return RawCommand(0, "", "")
    cdp._run = fake_run
    cdp.click("#btn", frame_id="f1")
    assert captured["args"] == ["click", "--selector", "#btn", "--frame-id", "f1",
                                "--host", "127.0.0.1", "--port", "9999"]

def test_form_argv():
    cdp = SubprocessCDPClient("/bin/cdp", "127.0.0.1", "9999")
    captured = {}
    def fake_run(subcmd, args, timeout_s=15):
        captured["args"] = [subcmd] + args
        return RawCommand(0, "", "")
    cdp._run = fake_run
    cdp.form("select#country", value="US", frame_id="f1", check="true", select="CA")
    assert captured["args"][0:2] == ["form", "select#country"]
    assert "--value" in captured["args"]
    assert "--check" in captured["args"]
    assert "--select" in captured["args"]
    assert "--frame-id" in captured["args"]
```

### BrowserManager

```python
def test_old_lease_does_not_affect_new_session(mocker):
    """Lease A from dead session must not decrement new Session B's refcount."""
    bm = BrowserManager(config)
    
    # Session A opened, then lease A released, session A closed
    mocker.patch.object(bm, '_list_alive_pids', return_value=[])
    mocker.patch.object(bm, '_bit_open', return_value=("ws://h:1/dev/0", "http://h:1"))
    mocker.patch.object(bm, '_verify_cdp_readiness')
    mocker.patch.object(bm, '_get_pid', return_value=100)
    
    lease_a = bm.acquire("p1", device)
    bm.release(lease_a)  # session A refcount=0 → closed
    
    # Session B opened
    lease_b = bm.acquire("p1", device)
    assert lease_b.generation != lease_a.generation
    assert lease_b.owned
    
    # Release stale lease A — must be no-op
    bm.release(lease_a)
    
    # Session B still alive
    assert bm._profile_index.get("p1") is not None
    session_b = bm._sessions[lease_b.session_id]
    assert session_b.refcount == 1

def test_config_conflict_with_active_users_raises(mocker):
    bm = BrowserManager(config)
    mocker.patch.object(bm, '_list_alive_pids', return_value=[100])
    # Pre-register session with different config
    ...
    with pytest.raises(BrowserConfigConflict):
        bm.acquire("p1", device2)

def test_partial_open_rollback(mocker):
    bm = BrowserManager(config)
    mocker.patch.object(bm, '_list_alive_pids', return_value=[])
    mocker.patch.object(bm, '_bit_open', return_value=("ws://...", "http://..."))
    mocker.patch.object(bm, '_verify_cdp_readiness', side_effect=CDPTransportError("fail"))
    close_called = mocker.patch.object(bm, '_bit_close')
    
    with pytest.raises(CDPTransportError):
        bm.acquire("p1", device)
    assert close_called.called  # rollback executed
```

---

## 八、实施顺序

| Phase | 内容 |
|-------|------|
| 1 | `cdp_protocol.py` — Protocol + CommandResult + RawCommand + CDPTimeout |
| 2 | `subprocess_cdp_client.py` — SubprocessCDPClient + classifier (transport-first) + decoder |
| 3 | 解码器测试 + 分类器测试 + argv 测试 |
| **── Gate A: Protocol + Adapter 可实施 ──** | |
| 4 | `pyproject.toml` — lanuage_core 包 |
| 5 | `CDPHelper._run_command()` 底层 RawCommand |
| 6 | `LegacyAdapter` |
| 7 | 迁移 A — frame_id keyword-only (~93 处) |
| 8 | 迁移 B — click/form → check .ok |
| 9 | 迁移 C — snapshot/json.loads → Adapter 归一化 |
| 10 | 迁移 D — click_checked() 删除 |
| 11 | AST 守卫 — 禁止裸 click/form |
| 12 | 迁移 E — composition roots |
| **── Gate B: lanuage 内部迁移完成 ──** | |
| 13 | `BrowserLease` + `SessionRecord` + `BrowserManager`（含 ExternalSessionConflict） |
| 14 | BrowserManager 测试（ABA/rollback/config-conflict） |
| 15 | `BitCDPAdapter` |
| 16 | Bit 集成测试（iframe dynamic frame_id + finally release） |
