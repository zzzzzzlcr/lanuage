# CDPClient Protocol + Bit Browser Integration — Design Spec (v6)

## Context

lanuage `CDPHelper` 直连本地 Chrome，newTaskTest 通过 bit.sh 启动比特浏览器。
两条路径需共同 `CDPClient` Protocol。现有 11 个 test_common.py 通过。

---

## 一、CDPClient Protocol（冻结）

```python
# lanuage_core/cdp_protocol.py

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class CDPError(RuntimeError): ...
class CDPTransportError(CDPError): ...
class CDPExecutionError(CDPError): ...
class CDPAmbiguousMutation(CDPError):
    """click/form timed out or connection lost mid-mutation — state unknown."""


class CDPTimeout(CDPError):
    """Raised by _run() on subprocess.TimeoutExpired.
    Defined before SubprocessCDPClient to avoid forward-reference confusion.
    Caller decides: ambiguous_mutation (click/form) or transport (eval/snapshot)."""
    def __init__(self, subcmd: str):
        self.subcmd = subcmd
        super().__init__(f"cdp {subcmd} timed out")


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    raw_output: str = ""
    error: str | None = None
    returncode: int | None = None
    error_category: str | None = None


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

### 刻意排除的方法

| 方法 | 排除原因 |
|------|---------|
| `scroll` | 通过 `eval("window.scrollBy(0,px)")` 实现，逻辑更简单，无需单独子命令 |
| `navigate` | 通过 `eval("window.location.href='...'")` 实现 |
| `screenshot` | 截图走独立上报路径，不进入 Pipeline/Executor |
| `wait_for_element` | 调用方通过 eval 轮询实现，方式更灵活 |

---

## 二、解码器 + 分类器

### decoder（已知限制：eval 脚本不能返回长得像 JSON 的字符串）

```python
def _decode_eval_output(raw: str) -> Any:
    """Replicates existing CDPHelper.eval() behavior.

    IMPORTANT: If a page JS return value happens to look like a JSON string
    starting with { or [, it will be decoded a second time. This is inherited
    from Go CDP CLI's double-encoding behavior. Callers that need to receive
    raw strings that happen to look like JSON should wrap the return value in
    an extra encode step or use a structured sentinel. Future adapter versions
    may add a sentinel to distinguish CLI encoding from page content.
    """
    stripped = raw.strip()
    if not stripped:
        return stripped
    try:
        decoded = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return stripped
    if isinstance(decoded, str) and decoded and decoded[0] in "{[":
        try:
            return json.loads(decoded)
        except (json.JSONDecodeError, ValueError):
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
    # 1. Timeout
    if timeout_occurred:
        if subcmd in ("click", "form"):
            return CommandResult(ok=False, raw_output=rc.stdout + rc.stderr,
                                error="timeout", error_category="ambiguous_mutation")
        raise CDPTransportError(f"cdp {subcmd} timed out")

    # 2. Transport check FIRST — any returncode
    combined = (rc.stderr + rc.stdout).lower()
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

    # 4. rc=0 — check stderr prefixes only
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
            raise CDPTimeout(subcmd)
        except FileNotFoundError:
            raise CDPTransportError(f"cdp binary not found: {self._binary}")
        except OSError as e:
            raise CDPTransportError(f"cdp {subcmd} OS error: {e}")

    def eval(self, script: str, *, frame_id: str = "") -> Any:
        args = [script]
        if frame_id: args.extend(["--frame-id", frame_id])
        try: r = self._run("eval", args)
        except CDPTimeout: raise CDPTransportError("cdp eval timed out")
        _classify_eval_snapshot(r)
        return _decode_eval_output(r.stdout)

    def click(self, selector: str, *, frame_id: str = "") -> CommandResult:
        args = ["--selector", selector]
        if frame_id: args.extend(["--frame-id", frame_id])
        try: r = self._run("click", args)
        except CDPTimeout:
            return _classify_raw(RawCommand(0, "", "timeout"), "click", timeout_occurred=True)
        return _classify_raw(r, "click")

    def snapshot(self) -> dict[str, Any]:
        # Retry once on transport error
        for attempt in range(2):
            try: r = self._run("snapshot", [])
            except CDPTimeout:
                if attempt == 0: continue
                raise CDPTransportError("cdp snapshot timed out")
            try: _classify_eval_snapshot(r)
            except CDPTransportError:
                if attempt == 0: continue
                raise
            break
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
        try: r = self._run("form", args)
        except CDPTimeout:
            return _classify_raw(RawCommand(0, "", "timeout"), "form", timeout_occurred=True)
        return _classify_raw(r, "form")

    def get_page_info(self) -> dict[str, str]:
        url = str(self.eval("window.location.href"))
        title = str(self.eval("document.title"))
        return {"url": url, "title": title}

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
            except CDPTransportError:
                raise
            except CDPError:
                # Transient eval failure — not necessarily transport
                time.sleep(0.8)
                continue
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

### 重试规则

| 场景 | 行为 |
|------|------|
| `snapshot` transport error | 重试 1 次 |
| `get_page_info` | 不重试 |
| CDP readiness check | 重试 1 次 |
| 普通 `eval` | 不重试 |
| `click`/`form` **前** transport (command_not_issued) | 允许按 step.retry 重试 |
| `click`/`form` 返回 `ambiguous_mutation` | **禁止重试** |
| `click`/`form` 后 transport | **禁止重放** |
| `click`/`form` 返回 execution error | 允许按 step.retry 重试 |

执行器规则：
```python
# In _execute_step: clicks_issued counter prevents re-click after first dispatch
# retry=3 means up to 3 attempts only if command_not_issued
# After first command issued, transport/ambiguous → verify page state, don't re-click
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
    session_id: str
    profile_id: str
    generation: int
    ws_url: str
    debug_endpoint: DebugEndpoint
    owned: bool
    config_fingerprint: str


@dataclass
class SessionRecord:
    session_id: str
    profile_id: str
    pid: int
    generation: int
    debug_endpoint: DebugEndpoint
    config_fingerprint: str
    managed: bool
    refcount: int
    active_lease_ids: set[str]


class ExternalSessionConflict(CDPError):
    """Unknown external browser running for this profile."""
    def __init__(self, profile_id: str, pids: list[int]):
        super().__init__(f"External browser for {profile_id}: pids={pids}")


class BrowserConfigConflict(CDPError):
    """Existing session has different config fingerprint with active users."""
    def __init__(self, profile_id: str, existing: str, requested: str):
        super().__init__(
            f"Config conflict for {profile_id}: existing={existing} requested={requested}")


class BrowserConfig:
    """Typed config for BrowserManager. Loaded from YAML with schema validation."""
    profile_id: str
    executable: str          # path to bit.sh
    cdp_binary: str          # path to cdp binary
    startup_timeout: int = 30
    command_timeout: int = 15
    device: "DeviceConfig"
    proxy: "ProxyConfig | None" = None
```

### Bit API 错误契约

```python
# Signatures for the bit.sh / Local API integration surface.
# These are NOT CDPClient — they are BrowserManager's private integration layer.

def _bit_open(profile_id: str, device: DeviceConfig,
              proxy: ProxyConfig | None) -> tuple[str, str]:
    """Open browser via bit.sh. Returns (ws_url, http_endpoint).
    Raises BitAPIError on HTTP non-200, success:false, or timeout.
    Raises BitOpenTimeout if /browser/open takes > startup_timeout."""
    ...

def _bit_close(profile_id: str) -> None:
    """Close browser via bit.sh. Idempotent. Swallows failures if browser already gone."""
    ...

def _list_alive_pids(profile_id: str) -> list[int]:
    """GET /browser/pids/alive → list of PIDs for this profile.
    Returns empty list if no browsers running or API unavailable."""
    ...

def _verify_cdp_readiness(ws_url: str) -> None:
    """Connect to ws_url via CDP, call snapshot, verify frameId present.
    Raises CDPTransportError if not ready within timeout."""
    ...

def _get_pid(profile_id: str, endpoint: DebugEndpoint) -> int:
    """Get PID for a running browser session. Uses /browser/pids/alive filtered by endpoint."""
    ...


class BitAPIError(CDPError): ...
class BitOpenTimeout(CDPError): ...
```

### BrowserManager

```python
class BrowserManager:
    def __init__(self, config: BrowserConfig):
        self._sessions: dict[str, SessionRecord] = {}
        self._profile_index: dict[str, str] = {}
        self._leases: dict[str, BrowserLease] = {}
        self._lock = threading.Lock()
        self._gen_counter = 0

    def _next_generation(self) -> int:
        self._gen_counter += 1
        # Include timestamp to avoid collision on rapid reopen
        return int(f"{int(time.time()) % 100000}{self._gen_counter:04d}")

    # ── acquire ──
    def acquire(self, profile_id: str, device: DeviceConfig,
                proxy: ProxyConfig | None = None) -> BrowserLease:
        ...

    # ── release ──
    def release(self, lease: BrowserLease) -> None:
        """Idempotent. Stale leases are no-ops."""
        ...

    # ── close session ──
    def _close_session_locked(self, session: SessionRecord) -> None:
        """Close session. Registry cleanup ALWAYS happens in finally,
        even if _bit_close raises."""
        try:
            if session.managed:
                self._bit_close(session.profile_id)
        finally:
            # ALWAYS clean registry — dead session must not block future acquire
            self._sessions.pop(session.session_id, None)
            if self._profile_index.get(session.profile_id) == session.session_id:
                del self._profile_index[session.profile_id]
            for lid in list(session.active_lease_ids):
                self._leases.pop(lid, None)
```

**关键安全性**：
- `_close_session_locked` 的 registry 清理在 `finally` 中，`_bit_close` 失败不会留下僵尸 session
- `release()` 通过 `session_id` 查找，旧 lease 不会影响新 session
- stale lease 三重验证 → no-op
- config conflict + refcount>0 → `BrowserConfigConflict`
- 未知外部 session → `ExternalSessionConflict`
- full open→readiness 在 try/except 中，失败时回滚
- `generation` = timestamp + counter，快速重开不会碰撞

---

## 五、包结构（方案 A：单包，newTaskTest 为工具包）

```
/company/newTaskTest/              # 工具包 — CDP 层 + Bit 浏览器
├── src/
│   ├── cdp_protocol.py            # CDPClient Protocol + CommandResult + RawCommand + 异常
│   ├── subprocess_cdp_client.py   # SubprocessCDPClient + classifier + decoder
│   ├── legacy_adapter.py          # LegacyAdapter（本地 Chrome）
│   ├── bit_cdp_adapter.py         # BitCDPAdapter（Bit 浏览器）
│   ├── bit_api.py                 # _bit_open, _bit_close, _list_alive_pids ...
│   ├── browser_manager.py         # BrowserLease + SessionRecord + BrowserManager (Phase 2)
│   ├── browser.py                 # 已有
│   ├── config.py                  # 已有
│   └── logger.py                  # 已有
├── tests/
│   ├── test_cdp_protocol.py       # decoder + classifier + argv
│   ├── test_legacy_adapter.py     # frame_id + _pipeline_form 回归
│   └── test_bit_adapter.py        # Bit 集成测试
├── cdp                            # CDP 二进制
├── bit.sh                         # bit.sh 脚本
└── config.yaml                    # Bit 配置

/company/lanuage/                  # 现有仓库 — 表单自动化
├── src/
│   ├── json_pipeline.py           # 注入 CDPClient 实例
│   ├── json_executor.py           # 注入 CDPClient 实例
│   ├── locator.py                 # 注入 CDPClient 实例
│   └── ...
├── tests/
│   └── test_common.py             # 11 个现有测试，保持不变
└── ...

# lanuage 引用 newTaskTest:
# import sys; sys.path.insert(0, '/company/newTaskTest')
# from src.legacy_adapter import LegacyAdapter
# cdp = LegacyAdapter(ws_url)  # 替代 CDPHelper(ws_url)
```

**原理**：
- newTaskTest 提供 `cdp_protocol.py` + 两个 adapter + Bit 生命周期
- lanuage 通过 `sys.path` 引用，入口文件把 `CDPHelper(ws_url)` 替换为 `LegacyAdapter(ws_url)`
- 不建新仓库、不加 `pyproject.toml`、不 `pip install`
- mock 本地测试和 Bit 浏览器测试共享同一套 Protocol，adapter 不同

---

## 六、AST 守卫（真实父节点检查）

```python
# tests/test_call_convention.py

import ast, pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

_CLICK_FORM_METHODS = {
    "cdp.click", "self.cdp.click",
    "cdp.form", "self.cdp.form",
}


def _qualname(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = _qualname(node.value)
        return f"{inner}.{node.attr}" if inner else node.attr
    return None


def _result_is_used(call_node: ast.Call, parent: ast.AST) -> bool:
    """Was the return value captured (assignment, return, comparison, attribute access)?"""
    if isinstance(parent, (ast.Assign, ast.Return, ast.Compare, ast.Assert)):
        return True
    if isinstance(parent, ast.Expr):
        return False  # bare expression = unused
    if isinstance(parent, ast.Attribute) and parent.value is call_node:
        return True  # result.ok, result.raw_output etc.
    if isinstance(parent, ast.If) and parent.test is call_node:
        return True
    if isinstance(parent, ast.BoolOp) and call_node in ast.walk(parent):
        return True  # result.ok and ...
    return False


def _iter_nodes(tree: ast.AST):
    """Yield each (child, parent) pair exactly once.
    ast.walk handles depth — iter_child_nodes at each level gives the pairs."""
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            yield (child, parent)


def test_no_bare_click_form():
    violations = []
    for py_file in SRC.glob("*.py"):
        if py_file.name.startswith("__"): continue
        tree = ast.parse(py_file.read_text())
        for child, parent in _iter_nodes(tree):
            if isinstance(child, ast.Call):
                name = _qualname(child)
                if name in _CLICK_FORM_METHODS:
                    # Check positional arg count
                    pos_args = [a for a in child.args
                               if not isinstance(a, ast.keyword)]
                    if len(pos_args) != 1:
                        violations.append(
                            f"{py_file.name}:{child.lineno}: {name} "
                            f"has {len(pos_args)} pos args (expected 1)")
                    # Check result is used
                    if not _result_is_used(child, parent):
                        violations.append(
                            f"{py_file.name}:{child.lineno}: {name} "
                            f"return value not captured or .ok unchecked")
    assert not violations, "\n".join(violations)
```

---

## 六-B、.ok 迁移表（实施阶段 7-10 参考）

AST 守卫验证迁移完成，但实施时需要精确知道每个调用点的位置和改法：

| 文件 | 方法 | `cdp.click` | `cdp.form` | 变更 |
|------|------|:---:|:---:|------|
| `json_executor._execute_step` | click branch | 2 | 0 | `not result.ok` → retry or fail |
| `json_executor._execute_step` | click text-find | 1 | 0 | `not result.ok` → return False |
| `json_executor._execute_step` | form fallback | 0 | 1 | `.strip()` → `result.raw_output` |
| `json_executor._smart_form` | custom select open | 3 | 0 | `not result.ok` → skip |
| `json_executor._smart_form` | select/checkbox/fill | 0 | 5 | `.strip()` → `result.raw_output` |
| `json_executor._select_option` | scoped/global label click | 3 | 0 | `not result.ok` → return False |
| `json_executor._run_stateful` | auto-advance click | 1 | 0 | `not result.ok` → skip |
| `json_executor._quiz_loop` | option/next click | 2 | 0 | `not result.ok` → skip |
| `json_pipeline._pipeline_form` | form submit | 0 | 1 | **补全 frame_id + check**；return `result.ok` |
| `json_pipeline._run_one_step` | click branch | 1 | 0 | `not result.ok` → return error |
| `select_explorer.SelectExplorer` | trigger/option click | 2 | 0 | `not result.ok` → return error status |
| `select_explorer._try_native` | native form | 0 | 1 | `not result.ok` → return NOT_VERIFIED |
| **合计** | | **15** | **9** | |

搜索命令（实施时用）：
```bash
grep -rn "cdp\.click\|self\.cdp\.click\|cdp\.form\|self\.cdp\.form" src/*.py \
  | grep -v "\.ok\|result\|#\|test_"
```

---

## 七、测试

### 解码器

```python
def test_decoder_string_json():     assert _decode_eval_output('"42"') == "42"
def test_decoder_number_json():     assert _decode_eval_output("42") == 42
def test_decoder_plain_text():      assert _decode_eval_output("hello") == "hello"
def test_decoder_empty():           assert _decode_eval_output("") == ""
def test_decoder_nested_array():    assert _decode_eval_output('"[1,2,3]"') == [1,2,3]
def test_decoder_object():          assert _decode_eval_output('{"a":1}') == {"a":1}
def test_decoder_string_looks_like_json():
    """Known limitation: string starting with { is decoded as object."""
    assert _decode_eval_output('{"key":"value"}') == {"key": "value"}
```

### 分类器

```python
def test_transport_rc_nonzero():
    r = _classify_raw(RawCommand(1, "", "connection refused"), "click")
    assert not r.ok and r.error_category == "transport"

def test_transport_rc_zero():
    r = _classify_raw(RawCommand(0, "", "BugError: no page target"), "click")
    assert not r.ok and r.error_category == "transport"

def test_execution_rc_nonzero():
    r = _classify_raw(RawCommand(1, "", "something wrong"), "click")
    assert not r.ok and r.error_category == "execution"

def test_execution_rc_zero_stderr_prefix():
    r = _classify_raw(RawCommand(0, "", "error: selector not found"), "eval")
    assert not r.ok and r.error_category == "execution"

def test_ambiguous_mutation_timeout():
    r = _classify_raw(RawCommand(0, "", "timeout"), "click", timeout_occurred=True)
    assert not r.ok and r.error_category == "ambiguous_mutation"

def test_stdout_not_scanned_for_exec():
    r = _classify_raw(RawCommand(0, '<html>error: content</html>', ""), "snapshot")
    assert r.ok

def test_transport_before_returncode():
    r = _classify_raw(RawCommand(0, "", "browser has disconnected"), "click")
    assert not r.ok and r.error_category == "transport"

def test_case_insensitive_transport():
    r = _classify_raw(RawCommand(0, "", "Browser has disconnected"), "click")
    assert not r.ok and r.error_category == "transport"
```

### argv

```python
def test_click_argv():
    cdp = SubprocessCDPClient("/bin/cdp", "127.0.0.1", "9999")
    args_list = []
    cdp._run = lambda s, a: args_list.extend(a) or RawCommand(0, "", "")
    cdp.click("#btn", frame_id="f1")
    assert args_list[0] == "--selector"

def test_form_argv():
    cdp = SubprocessCDPClient("/bin/cdp", "127.0.0.1", "9999")
    args_list = []
    cdp._run = lambda s, a: args_list.extend(a) or RawCommand(0, "", "")
    cdp.form("select#c", value="US", check="true", select="CA", frame_id="f1")
    assert args_list[0] == "select#c"  # positional
    assert "--value" in args_list and "--check" in args_list and "--select" in args_list
```

### BrowserManager

```python
def test_old_lease_noop_on_new_session(mocker):
    bm = BrowserManager(config)
    mocker.patch.object(bm, '_list_alive_pids', return_value=[])
    mocker.patch.object(bm, '_bit_open', return_value=("ws://h/0", "http://h:1"))
    mocker.patch.object(bm, '_verify_cdp_readiness')
    mocker.patch.object(bm, '_get_pid', return_value=100)

    lease_a = bm.acquire("p1", device)
    bm.release(lease_a)
    lease_b = bm.acquire("p1", device)
    # Release stale lease A → no-op
    bm.release(lease_a)
    assert bm._profile_index.get("p1") is not None
    session_b = bm._sessions[lease_b.session_id]
    assert session_b.refcount == 1

def test_config_conflict_with_active_users(mocker):
    bm = BrowserManager(config)
    mocker.patch.object(bm, '_list_alive_pids', return_value=[200])
    # Pre-create session with different fingerprint
    ep = DebugEndpoint("h", 1)
    sid = "existing-session"
    bm._sessions[sid] = SessionRecord(
        session_id=sid, profile_id="p1", pid=200, generation=1,
        debug_endpoint=ep, config_fingerprint="old-fp",
        managed=True, refcount=2, active_lease_ids={"l1", "l2"})
    bm._profile_index["p1"] = sid
    # Acquire with different config → conflict
    with pytest.raises(BrowserConfigConflict):
        bm.acquire("p1", device2)

def test_partial_open_rollback(mocker):
    bm = BrowserManager(config)
    mocker.patch.object(bm, '_list_alive_pids', return_value=[])
    mocker.patch.object(bm, '_bit_open', return_value=("ws://...", "http://..."))
    mocker.patch.object(bm, '_verify_cdp_readiness',
                        side_effect=CDPTransportError("fail"))
    close_spy = mocker.patch.object(bm, '_bit_close')
    with pytest.raises(CDPTransportError):
        bm.acquire("p1", device)
    assert close_spy.called

def test_close_session_zombie_cleanup(mocker):
    """If _bit_close raises, registry is still cleaned in finally."""
    bm = BrowserManager(config)
    ep = DebugEndpoint("h", 1)
    session = SessionRecord(
        session_id="s1", profile_id="p1", pid=100, generation=1,
        debug_endpoint=ep, config_fingerprint="fp",
        managed=True, refcount=0, active_lease_ids=set())
    bm._sessions["s1"] = session
    bm._profile_index["p1"] = "s1"
    mocker.patch.object(bm, '_bit_close', side_effect=RuntimeError("boom"))
    bm._close_session_locked(session)
    assert "s1" not in bm._sessions
    assert "p1" not in bm._profile_index
```

---

## 八、实施顺序

| Phase | 内容 | 位置 |
|-------|------|------|
| 1 | `cdp_protocol.py` — Protocol + CommandResult + RawCommand + CDPTimeout | newTaskTest |
| 2 | `subprocess_cdp_client.py` — client + classifier + decoder | newTaskTest |
| 3 | 解码器测试 + 分类器测试 + argv 测试 | newTaskTest |
| **── Gate A: Protocol + Adapter 可实施 ──** | | |
| 4 | `CDPHelper._run_command()` 底层 RawCommand | lanuage |
| 5 | `LegacyAdapter` | newTaskTest |
| 6 | 迁移 A — frame_id keyword-only (~93 处) | lanuage |
| 7 | 迁移 B — click/form → check .ok | lanuage |
| 8 | 迁移 C — snapshot/json.loads → Adapter 归一化 | lanuage |
| 9 | 迁移 D — click_checked() 删除 | lanuage |
| 10 | AST 守卫 — 禁止裸 click/form | lanuage |
| 11 | 迁移 E — composition roots（注入 LegacyAdapter） | lanuage |
| **── Gate B: lanuage 内部迁移完成 ──** | | |
| 12 | `BrowserLease` + `SessionRecord` + `BrowserManager` | newTaskTest |
| 13 | `bit_api.py` — _bit_open/_bit_close/_list_alive_pids | newTaskTest |
| 14 | BrowserManager 测试 | newTaskTest |
| 15 | `BitCDPAdapter` | newTaskTest |
| 16 | Bit 集成测试 | newTaskTest |
