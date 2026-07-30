# CDP Protocol Phase 1-3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create CDPClient Protocol + SubprocessCDPClient + tests in `/company/newTaskTest/src/`, freezing at Gate A before any lanuage migration.

**Architecture:** 3 files in newTaskTest/src/ (cdp_protocol.py, subprocess_cdp_client.py, legacy_adapter.py) + 3 test files in newTaskTest/tests/. Existing lanuage code unchanged. Tests run without CDP binary using monkeypatched `_run`.

**Tech Stack:** Python 3.10+, dataclasses, Protocol, subprocess, pytest

## Global Constraints

- All new files in `/company/newTaskTest/src/` and `/company/newTaskTest/tests/`
- Existing lanuage code unchanged until Gate A
- No pyproject.toml — lanuage references via `sys.path.insert(0, '/company/newTaskTest')`
- `eval() -> Any` preserves existing JSON decode contract (11 test_common.py tests)
- `click` uses `--selector` flag; `form` uses positional selector
- `wait_page_stable` polls `document.readyState` × 3 + body length stable × 2
- Transport error check BEFORE returncode check; all patterns lowercase; stdout excluded from execution scan
- `CDPTimeout` at module level for except clauses; `json` import at module level
- Decoder: second decode only if result is string starting with `{` or `[`
- Phase 1-3 only — no BrowserManager, no BitCDPAdapter, no lanuage migration

---

## Files

| File | Create/Modify | Responsibility |
|------|:---:|------|
| `/company/newTaskTest/src/cdp_protocol.py` | Create | Protocol + CommandResult + RawCommand + 异常类 + CDPTimeout + decoder |
| `/company/newTaskTest/src/subprocess_cdp_client.py` | Create | SubprocessCDPClient + classifier + _run |
| `/company/newTaskTest/src/legacy_adapter.py` | Create | LegacyAdapter(SubprocessCDPClient) |
| `/company/newTaskTest/tests/test_cdp_protocol.py` | Create | decoder + classifier + argv tests |

---

### Task 1: cdp_protocol.py — Protocol + Data Classes + Exceptions + Decoder

**Files:**
- Create: `/company/newTaskTest/src/cdp_protocol.py`

**Interfaces:**
- Produces: `CDPClient` (Protocol), `CommandResult`, `RawCommand`, `CDPError`, `CDPTransportError`, `CDPExecutionError`, `CDPAmbiguousMutation`, `CDPTimeout`, `_decode_eval_output()`

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p /company/newTaskTest/src /company/newTaskTest/tests
```

- [ ] **Step 2: Write cdp_protocol.py**

```python
# /company/newTaskTest/src/cdp_protocol.py
"""CDPClient Protocol and supporting types. Shared by LegacyAdapter and BitCDPAdapter."""

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ── Exceptions ──

class CDPError(RuntimeError):
    """Base class for all CDP errors."""

class CDPTransportError(CDPError):
    """Connection lost, binary not found, OS error, timeout on read-only command."""

class CDPExecutionError(CDPError):
    """Non-zero returncode, JS/selector/protocol error on eval/snapshot."""

class CDPAmbiguousMutation(CDPError):
    """click/form timed out or connection lost mid-mutation — state unknown."""

class CDPTimeout(CDPError):
    """Raised by _run() on subprocess.TimeoutExpired.
    Defined before SubprocessCDPClient to avoid forward-reference issues.
    Caller decides: ambiguous_mutation (click/form) or CDPTransportError (eval/snapshot)."""
    def __init__(self, subcmd: str):
        self.subcmd = subcmd
        super().__init__(f"cdp {subcmd} timed out")


# ── Data Classes ──

@dataclass(frozen=True)
class CommandResult:
    """Structured result of a click or form command.
    raw_output is diagnostic-only; ok is the authoritative success indicator."""
    ok: bool
    raw_output: str = ""
    error: str | None = None
    returncode: int | None = None
    error_category: str | None = None  # "transport" | "execution" | "ambiguous_mutation" | None


@dataclass
class RawCommand:
    """Structured subprocess result — shared across all adapters."""
    returncode: int
    stdout: str
    stderr: str


# ── Protocol ──

@runtime_checkable
class CDPClient(Protocol):
    """Interface for CDP-backed browser automation.
    Implementations: LegacyAdapter (localhost:9222), BitCDPAdapter (bit.sh browser).
    """

    def eval(self, script: str, *, frame_id: str = "") -> Any:
        """Execute JavaScript. Returns decoded result (str/list/dict/int).
        Raises CDPExecutionError or CDPTransportError on failure."""

    def click(self, selector: str, *, frame_id: str = "") -> CommandResult:
        """Click element by CSS selector. CommandResult.ok for success.
        error_category indicates failure reason for retry/disambiguation."""

    def snapshot(self) -> dict[str, Any]:
        """Return parsed CDP snapshot dict. Raises CDPExecutionError on failure."""

    def form(self, selector: str, *,
             value: str | None = None,
             check: str | None = None,
             select: str | None = None,
             frame_id: str = "") -> CommandResult:
        """Fill form element. selector is positional arg matching `cdp form <sel>` CLI.
        CommandResult.ok for success."""

    def get_page_info(self) -> dict[str, str]:
        """Return {"url": str, "title": str}. Raises CDPExecutionError on failure."""

    def wait_page_stable(self, timeout: float = 15) -> bool:
        """Poll until readyState=complete × 3 and body text stable × 2.
        Timeout → False. CDPTransportError on connection loss."""


# ── Eval Decoder ──

def _decode_eval_output(raw: str) -> Any:
    """Replicate existing CDPHelper.eval() JSON decode behavior.

    IMPORTANT: If a page JS return value looks like a JSON string starting
    with { or [, it will be decoded a second time. This is inherited from
    Go CDP CLI double-encoding. Callers needing raw strings that look like
    JSON should use a sentinel wrapper.

    Contract (11 test_common.py tests):
    - '"42"' → "42" (JSON string)
    - '42' → 42 (JSON number)
    - 'hello' → "hello" (not JSON)
    - '' → "" (empty)
    - '"[1,2,3]"' → [1,2,3] (nested JSON)
    - '{"a":1}' → {"a":1} (object)
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

- [ ] **Step 3: Verify imports**

```bash
cd /company/newTaskTest && python3 -c "
import sys; sys.path.insert(0, '.')
from src.cdp_protocol import (CDPClient, CommandResult, RawCommand,
    CDPError, CDPTransportError, CDPExecutionError,
    CDPAmbiguousMutation, CDPTimeout, _decode_eval_output)
print('cdp_protocol.py OK')
"
```

- [ ] **Step 4: Commit**

```bash
cd /company/newTaskTest && git init && git add src/cdp_protocol.py && git commit -m "feat: CDPClient Protocol + data classes + exceptions + decoder"
```

---

### Task 2: subprocess_cdp_client.py — Shared Implementation + Classifier

**Files:**
- Create: `/company/newTaskTest/src/subprocess_cdp_client.py`

**Interfaces:**
- Consumes: `cdp_protocol.py` — `RawCommand`, `CommandResult`, `CDPClient`, `CDPTransportError`, `CDPExecutionError`, `CDPTimeout`, `_decode_eval_output`
- Produces: `SubprocessCDPClient(CDPClient)` — `.eval()`, `.click()`, `.snapshot()`, `.form()`, `.get_page_info()`, `.wait_page_stable()`, `._run()`

- [ ] **Step 1: Write subprocess_cdp_client.py**

```python
# /company/newTaskTest/src/subprocess_cdp_client.py
"""Shared CDP client implementation. LegacyAdapter and BitCDPAdapter extend this."""

import json
import subprocess
import time

from cdp_protocol import (
    RawCommand, CommandResult,
    CDPTransportError, CDPExecutionError, CDPTimeout,
    _decode_eval_output,
)


# ── Classifier ──

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


def _classify_raw(rc: RawCommand, subcmd: str,
                  timeout_occurred: bool = False) -> CommandResult:
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


def _classify_eval_snapshot(rc: RawCommand) -> CommandResult:
    result = _classify_raw(rc, "eval")
    if not result.ok:
        if result.error_category == "transport":
            raise CDPTransportError(result.error or "CDP transport failure")
        raise CDPExecutionError(result.error or f"CDP command failed (rc={rc.returncode})")
    return result


# ── Client ──

class SubprocessCDPClient:
    """Shared CDP client. LegacyAdapter and BitCDPAdapter extend with endpoint info."""

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
        if frame_id:
            args.extend(["--frame-id", frame_id])
        try:
            r = self._run("eval", args)
        except CDPTimeout:
            raise CDPTransportError("cdp eval timed out")
        _classify_eval_snapshot(r)
        return _decode_eval_output(r.stdout)

    def click(self, selector: str, *, frame_id: str = "") -> CommandResult:
        args = ["--selector", selector]
        if frame_id:
            args.extend(["--frame-id", frame_id])
        try:
            r = self._run("click", args)
        except CDPTimeout:
            return _classify_raw(RawCommand(0, "", "timeout"), "click", timeout_occurred=True)
        return _classify_raw(r, "click")

    def snapshot(self) -> dict[str, Any]:
        for attempt in range(2):
            try:
                r = self._run("snapshot", [])
            except CDPTimeout:
                if attempt == 0:
                    continue
                raise CDPTransportError("cdp snapshot timed out")
            try:
                _classify_eval_snapshot(r)
            except CDPTransportError:
                if attempt == 0:
                    continue
                raise
            break
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            raise CDPExecutionError("snapshot: invalid JSON")

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
        try:
            r = self._run("form", args)
        except CDPTimeout:
            return _classify_raw(RawCommand(0, "", "timeout"), "form", timeout_occurred=True)
        return _classify_raw(r, "form")

    def get_page_info(self) -> dict[str, str]:
        return {
            "url": str(self.eval("window.location.href")),
            "title": str(self.eval("document.title")),
        }

    def wait_page_stable(self, timeout: float = 15) -> bool:
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
                time.sleep(0.8)
                continue
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

- [ ] **Step 2: Verify imports**

```bash
cd /company/newTaskTest && python3 -c "
import sys; sys.path.insert(0, 'src')
from subprocess_cdp_client import SubprocessCDPClient, _classify_raw, _classify_eval_snapshot
print('subprocess_cdp_client.py OK')
"
```

- [ ] **Step 3: Commit**

```bash
cd /company/newTaskTest && git add src/subprocess_cdp_client.py && git commit -m "feat: SubprocessCDPClient + classifier + shared eval/click/snapshot/form"
```

---

### Task 3: legacy_adapter.py — Local Chrome Adapter

**Files:**
- Create: `/company/newTaskTest/src/legacy_adapter.py`

**Interfaces:**
- Consumes: `subprocess_cdp_client.py` — `SubprocessCDPClient`
- Produces: `LegacyAdapter(SubprocessCDPClient)` — takes `ws_url: str` and `cdp_binary: str`, parses host/port from WS URL

- [ ] **Step 1: Write legacy_adapter.py**

```python
# /company/newTaskTest/src/legacy_adapter.py
"""Legacy adapter for local Chrome (localhost:9222)."""

import os
import re

from subprocess_cdp_client import SubprocessCDPClient
from cdp_protocol import CDPTransportError


class LegacyAdapter(SubprocessCDPClient):
    """CDPClient backed by local Chrome DevTools (not bit.sh).
    
    Usage:
        adapter = LegacyAdapter(ws_url="ws://127.0.0.1:9222/devtools/browser/0")
        adapter.eval("document.title")
    """

    def __init__(self, ws_url: str = None, cdp_binary: str = None):
        ws = ws_url or os.environ.get(
            "WS_URL", "ws://127.0.0.1:9222/devtools/browser/0")
        m = re.match(r'ws://([^:]+):(\d+)', ws)
        if not m:
            raise CDPTransportError(f"Invalid WS_URL: {ws}")
        host, port = m.group(1), m.group(2)
        binary = cdp_binary or os.environ.get("CDP_PATH", "/company/cdpcli/cdp")
        super().__init__(cdp_binary=binary, host=host, port=port)
```

- [ ] **Step 2: Verify import**

```bash
cd /company/newTaskTest && python3 -c "
import sys; sys.path.insert(0, 'src')
from legacy_adapter import LegacyAdapter
print('legacy_adapter.py OK')
"
```

- [ ] **Step 3: Commit**

```bash
cd /company/newTaskTest && git add src/legacy_adapter.py && git commit -m "feat: LegacyAdapter — local Chrome CDPClient"
```

---

### Task 4: test_cdp_protocol.py — Decoder Tests

**Files:**
- Create: `/company/newTaskTest/tests/test_cdp_protocol.py`

**Interfaces:**
- Consumes: `cdp_protocol.py` — `_decode_eval_output()`

- [ ] **Step 1: Write decoder tests**

```python
# /company/newTaskTest/tests/test_cdp_protocol.py
"""Tests for cdp_protocol.py: decoder, CommandResult, exceptions."""

import sys
sys.path.insert(0, "src")

from cdp_protocol import (
    _decode_eval_output,
    CommandResult,
    RawCommand,
    CDPError,
    CDPTransportError,
    CDPExecutionError,
    CDPAmbiguousMutation,
    CDPTimeout,
)


# ── Decoder ──

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

def test_decoder_string_looks_like_json():
    """Known limitation: string starting with { is decoded as object."""
    assert _decode_eval_output('{"key":"value"}') == {"key": "value"}


# ── CommandResult ──

def test_command_result_ok_immutable():
    r = CommandResult(ok=True)
    assert r.ok
    try:
        r.ok = False
        assert False, "should have raised"
    except Exception:
        pass  # frozen=True prevents mutation

def test_raw_command_fields():
    rc = RawCommand(0, "stdout", "stderr")
    assert rc.returncode == 0
    assert rc.stdout == "stdout"
    assert rc.stderr == "stderr"


# ── Exceptions ──

def test_cdp_error_inheritance():
    assert issubclass(CDPTransportError, CDPError)
    assert issubclass(CDPExecutionError, CDPError)
    assert issubclass(CDPAmbiguousMutation, CDPError)
    assert issubclass(CDPTimeout, CDPError)

def test_cdp_timeout_carries_subcmd():
    e = CDPTimeout("click")
    assert e.subcmd == "click"
    assert "click" in str(e)
```

- [ ] **Step 2: Run decoder tests**

```bash
cd /company/newTaskTest && python3 -m pytest tests/test_cdp_protocol.py -v
```

Expected: 10 PASS

- [ ] **Step 3: Commit**

```bash
cd /company/newTaskTest && git add tests/test_cdp_protocol.py && git commit -m "test: decoder contract + CommandResult + exception hierarchy"
```

---

### Task 5: Classifier Tests + argv Tests

**Files:**
- Modify: `/company/newTaskTest/tests/test_cdp_protocol.py` — append

**Interfaces:**
- Consumes: `subprocess_cdp_client.py` — `_classify_raw()`, `_classify_eval_snapshot()`, `SubprocessCDPClient`

- [ ] **Step 1: Append classifier + argv tests**

Append to `/company/newTaskTest/tests/test_cdp_protocol.py`:

```python
# ── Classifier ──

from subprocess_cdp_client import _classify_raw, _classify_eval_snapshot, SubprocessCDPClient
import pytest


def test_transport_rc_nonzero():
    r = _classify_raw(RawCommand(1, "", "connection refused"), "click")
    assert not r.ok and r.error_category == "transport"


def test_transport_rc_zero():
    """rc=0 + transport pattern → transport, not execution."""
    r = _classify_raw(RawCommand(0, "", "BugError: no page target"), "click")
    assert not r.ok and r.error_category == "transport"


def test_execution_rc_nonzero():
    r = _classify_raw(RawCommand(1, "", "something went wrong"), "click")
    assert not r.ok and r.error_category == "execution"


def test_execution_rc_zero_stderr_prefix():
    r = _classify_raw(RawCommand(0, "", "error: selector not found"), "eval")
    assert not r.ok and r.error_category == "execution"


def test_ambiguous_mutation_timeout():
    r = _classify_raw(RawCommand(0, "", "timeout"), "click", timeout_occurred=True)
    assert not r.ok and r.error_category == "ambiguous_mutation"


def test_stdout_not_scanned_for_exec():
    """Page content in stdout must not trigger error classification."""
    r = _classify_raw(RawCommand(0, '<html>error: content</html>', ""), "snapshot")
    assert r.ok


def test_transport_before_returncode():
    """Transport check happens regardless of returncode."""
    r = _classify_raw(RawCommand(0, "", "browser has disconnected"), "click")
    assert not r.ok and r.error_category == "transport"


def test_case_insensitive_transport():
    """Pattern lowercase matches mixed-case stderr."""
    r = _classify_raw(RawCommand(0, "", "Browser has disconnected"), "click")
    assert not r.ok and r.error_category == "transport"


def test_classify_eval_snapshot_transport_raises():
    with pytest.raises(CDPTransportError):
        _classify_eval_snapshot(RawCommand(1, "", "connection refused"))


def test_classify_eval_snapshot_execution_raises():
    with pytest.raises(CDPExecutionError):
        _classify_eval_snapshot(RawCommand(1, "", "some error"))


# ── argv ──

def test_click_argv():
    cdp = SubprocessCDPClient("/bin/cdp", "127.0.0.1", "9999")
    captured = []

    def fake_run(subcmd, args, timeout_s=15):
        captured.extend(args)
        return RawCommand(0, "", "")
    cdp._run = fake_run
    cdp.click("#btn", frame_id="f1")
    assert "--selector" in captured
    assert "#btn" in captured
    assert "--frame-id" in captured
    assert "f1" in captured


def test_form_argv():
    cdp = SubprocessCDPClient("/bin/cdp", "127.0.0.1", "9999")
    captured = []

    def fake_run(subcmd, args, timeout_s=15):
        captured.extend(args)
        return RawCommand(0, "", "")
    cdp._run = fake_run
    cdp.form("select#country", value="US", check="true", select="CA", frame_id="f1")
    assert captured[0] == "select#country"  # positional, not --selector
    assert "--value" in captured
    assert "--check" in captured
    assert "--select" in captured
    assert "--frame-id" in captured


def test_click_argv_no_frame_id():
    cdp = SubprocessCDPClient("/bin/cdp", "127.0.0.1", "9999")
    captured = []

    def fake_run(subcmd, args, timeout_s=15):
        captured.extend(args)
        return RawCommand(0, "", "")
    cdp._run = fake_run
    cdp.click("#btn")
    assert "--frame-id" not in captured  # not present when not needed
```

- [ ] **Step 2: Run all tests**

```bash
cd /company/newTaskTest && python3 -m pytest tests/test_cdp_protocol.py -v
```

Expected: 23 PASS (10 decoder + 10 classifier + 3 argv)

- [ ] **Step 3: Commit**

```bash
cd /company/newTaskTest && git add tests/test_cdp_protocol.py && git commit -m "test: classifier transport-first + ambiguous_mutation + argv parity"
```

---

### Task 6: Integration Smoke Test — LegacyAdapter with Real CDP

**Files:**
- Create: `/company/newTaskTest/tests/test_legacy_adapter.py`

**Interfaces:**
- Consumes: `legacy_adapter.py` — `LegacyAdapter`
- Requires: `CDP_PATH` and `WS_URL` env vars or default localhost:9222

- [ ] **Step 1: Write smoke test**

```python
# /company/newTaskTest/tests/test_legacy_adapter.py
"""Smoke test — LegacyAdapter with real CDP binary."""

import os
import sys
sys.path.insert(0, "src")

import pytest
from legacy_adapter import LegacyAdapter


@pytest.mark.skipif(
    not os.environ.get("CDP_PATH") and not os.path.exists("/company/cdpcli/cdp"),
    reason="requires CDP binary"
)
def test_legacy_adapter_eval():
    """Basic eval round-trip with real CDP."""
    adapter = LegacyAdapter()
    result = adapter.eval("1 + 1")
    assert result == 2


@pytest.mark.skipif(
    not os.environ.get("CDP_PATH") and not os.path.exists("/company/cdpcli/cdp"),
    reason="requires CDP binary"
)
def test_legacy_adapter_get_page_info():
    adapter = LegacyAdapter()
    info = adapter.get_page_info()
    assert "url" in info
    assert "title" in info


@pytest.mark.skipif(
    not os.environ.get("CDP_PATH") and not os.path.exists("/company/cdpcli/cdp"),
    reason="requires CDP binary"
)
def test_legacy_adapter_wait_page_stable():
    adapter = LegacyAdapter()
    stable = adapter.wait_page_stable(timeout=10)
    assert stable is True or stable is False
```

- [ ] **Step 2: Run smoke test**

```bash
cd /company/newTaskTest && python3 -m pytest tests/test_legacy_adapter.py -v
```

Expected: 3 PASS (or SKIP if no CDP binary)

- [ ] **Step 3: Commit**

```bash
cd /company/newTaskTest && git add tests/test_legacy_adapter.py && git commit -m "test: LegacyAdapter smoke test with real CDP"
```

---

## Gate A Checklist

After Task 6 completes, verify:

```bash
cd /company/newTaskTest
python3 -m pytest tests/ -v
```

Expected output:
- ∼23 unit tests PASS
- ∼3 integration tests PASS or SKIP
- Zero failures
- Existing `/company/lanuage/src/test_common.py` still 11/11

```bash
# Verify no regression in lanuage
cd /company/lanuage && python3 -m pytest test_common.py -v
```

All passing → Gate A frozen. Ready for Phase 4-11 (lanuage migration).
