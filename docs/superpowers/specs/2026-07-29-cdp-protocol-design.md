# CDPClient Protocol + Bit Browser Integration — Design Spec

## Context

当前 lanuage 的 pipeline/executor/locator 通过 `CDPHelper` 直连本地 Chrome（localhost:9222）。newTaskTest 项目需要通过 bit.sh 启动比特浏览器（带代理/指纹），拿到 WS URL 后再执行 CDP 命令。两条路径需要一个共同的 `CDPClient` Protocol 抽象。

同时 `CDPHelper` 有两个确定性问题：
1. `click`/`form` 丢弃 subprocess returncode，无法可靠判断成功/失败
2. `_pipeline_form` 给 `eval` 传了 `frame_id` 但 `form` 调用漏传

---

## 一、CDPClient Protocol

```python
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

@runtime_checkable
class CDPClient(Protocol):
    def eval(self, script: str, *, frame_id: str = "") -> str: ...
    def click(self, selector: str, *, frame_id: str = "") -> CommandResult: ...
    def snapshot(self) -> dict[str, Any]: ...
    def form(self, selector: str, *,
             value: str | None = None, check: str | None = None,
             select: str | None = None,
             frame_id: str = "") -> CommandResult: ...
    def get_page_info(self) -> dict[str, str]: ...
    def wait_page_stable(self, timeout: float = 15) -> bool: ...
```

**语义约束**：
- `eval`：正常执行返回字符串（含空串）；cdp 进程失败/超时/连接断开 → `CDPExecutionError`，不吞异常
- `click`/`form`：`CommandResult.ok` 表示进程 returncode=0 且无 CDP 协议错误；不代表页面状态已生效
- `snapshot`/`get_page_info`：解析失败 → `CDPExecutionError`，不返回空字典
- `wait_page_stable`：超时 → `False`；连接/进程错误 → `CDPTransportError`
- `frame_id`：所有方法 keyword-only

**不在 Protocol 内**：`open`/`close`/`ws_url`/`profile_id` — 属于 BrowserManager 生命周期。

---

## 二、LegacyAdapter

### 2.1 CDPHelper 底层修复

给 `CDPHelper` 增加统一的 `_run_command()` 入口：

```python
# common.py

@dataclass
class RawCommand:
    returncode: int
    stdout: str
    stderr: str

class CDPHelper:
    def _run_command(self, subcmd: str, args: list[str],
                     timeout_s: float = 15) -> RawCommand:
        cmd = [CDP_PATH, subcmd] + args + ["--host", self.host, "--port", self.port]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s, shell=False)
            return RawCommand(returncode=r.returncode, stdout=r.stdout, stderr=r.stderr)
        except subprocess.TimeoutExpired:
            raise CDPTransportError(f"CDP {subcmd} timed out after {timeout_s}s")
```

### 2.2 LegacyAdapter 归一化

```python
# lanuage_core/legacy_adapter.py

class LegacyAdapter:
    def __init__(self, cdp_helper: CDPHelper):
        self._cdp = cdp_helper

    def eval(self, script: str, *, frame_id: str = "") -> str:
        args = [script]
        if frame_id: args.extend(["--frame-id", frame_id])
        r = self._cdp._run_command("eval", args)
        if r.returncode != 0:
            raise CDPExecutionError(f"eval failed (rc={r.returncode}): {r.stderr[:200]}")
        return r.stdout

    def click(self, selector: str, *, frame_id: str = "") -> CommandResult:
        args = ["--selector", selector]
        if frame_id: args.extend(["--frame-id", frame_id])
        r = self._cdp._run_command("click", args)
        return CommandResult(ok=(r.returncode == 0), raw_output=r.stdout+r.stderr,
                            returncode=r.returncode,
                            error=r.stderr[:200] if r.returncode != 0 else None)

    def snapshot(self) -> dict:
        r = self._cdp._run_command("snapshot", [])
        if r.returncode != 0:
            raise CDPExecutionError(f"snapshot failed: {r.stderr[:200]}")
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError:
            raise CDPExecutionError("snapshot: invalid JSON from CDP")

    def form(self, selector: str, *, value=None, check=None, select=None,
             frame_id: str = "") -> CommandResult:
        args = ["--selector", selector]
        if value is not None: args.extend(["--value", str(value)])
        if check is not None: args.extend(["--check", str(check)])
        if select is not None: args.extend(["--select", str(select)])
        if frame_id: args.extend(["--frame-id", frame_id])
        r = self._cdp._run_command("form", args)
        return CommandResult(ok=(r.returncode == 0), raw_output=r.stdout+r.stderr,
                            returncode=r.returncode,
                            error=r.stderr[:200] if r.returncode != 0 else None)

    def get_page_info(self) -> dict:
        return {"url": self.eval("window.location.href"),
                "title": self.eval("document.title")}

    def wait_page_stable(self, timeout: float = 15) -> bool:
        try:
            r = self._cdp._run_command("wait", ["--stable", "--timeout", str(int(timeout))])
            return r.returncode == 0
        except CDPTransportError:
            raise
```

---

## 三、迁移清单

### A 类：frame_id → keyword-only（机械修改）

| 文件 | 调用点数量 | 模式 |
|------|-----------|------|
| `locator.py` | ~23 × `cdp.eval(js, fid)` | → `cdp.eval(js, frame_id=fid)` |
| `json_executor.py` | ~40 × `cdp.eval(js, fid)` | → `cdp.eval(js, frame_id=fid)` |
| `json_executor.py` | ~15 × `cdp.click(sel, fid)` | → `cdp.click(sel, frame_id=fid)` |
| `json_pipeline.py` | ~14 × `cdp.eval(js)` | 无 frame_id 的保持不变 |
| `element_finder.py` | 2 × `cdp.eval(js, frame_id)` | → keyword-only |
| `select_explorer.py` | ~5 × `cdp.eval(js, fid)` | → keyword-only |

搜索命令：
```bash
grep -rn "cdp\.eval(" src/ | grep -v "frame_id=" | grep -v "^.*:.*#" 
grep -rn "cdp\.click(" src/ | grep -v "frame_id="
grep -rn "cdp\.form(" src/ | grep -v "frame_id="
```

### B 类：click/form 返回值迁移

| 文件 | 函数/方法 | 变更 |
|------|----------|------|
| `json_executor._smart_form` | `cdp.form(sel, ...)` ×5 | 读 `result.raw_output` 替代 `.strip()` 字符串判断 |
| `json_executor._execute_step` (click) | `cdp.click(sel)` ×1 | 读 `result.ok`；`False` 时重试或返回 CLICK_FAILED |
| `json_executor._select_option` | `cdp.click()` ×3 | 忽略返回（随机点击不需要阻塞） |
| `json_pipeline._pipeline_form` | `cdp.form(sel, ...)` ×1 | **修复 frame_id 丢失** + 归一化返回值 |
| `select_explorer.SelectExplorer` | `cdp.click()` ×2 | 归一化 |
| `select_explorer.RadioStrategy` | `cdp.click()` → `click_checked()` | 删除 `click_checked`，直接用 `cdp.click()` 返回 `CommandResult` |

### C 类：数据归一化

| 调用 | 变更 |
|------|------|
| `cdp.snapshot()` | Adapter 已解析 JSON；调用方删除 `json.loads()` |
| `cdp.get_page_info()` | 保证返回 `{"url": str, "title": str}` |
| `cdp.eval()` | 异常时抛 `CDPExecutionError`，调用方不再需要空字符串判断失败 |

### 同步修复

- **`_pipeline_form` frame_id 丢失** — `cdp.form(selector, value=value, select=select)` → 补上 `frame_id=frame_id`
- **旧 `click_checked()` 删除** — `common.py` 末尾的独立函数删掉，`RadioStrategy` 直接用 `cdp.click()` 返回的 `CommandResult`

---

## 四、BitCDPAdapter（newTaskTest 侧）

```python
# newTaskTest/src/bit_cdp_adapter.py

class BitCDPAdapter:
    """Wraps bit.sh browser → CDPClient Protocol."""

    def __init__(self, browser_manager: BrowserManager, cdp_binary: str):
        self._bm = browser_manager
        self._cdp_path = cdp_binary
        self._host = ""
        self._port = ""

    def _ensure_connected(self):
        if not self._bm.ws_url:
            raise CDPTransportError("Browser not open")
        m = re.match(r'ws://([^:]+):(\d+)', self._bm.ws_url)
        self._host, self._port = m.group(1), m.group(2)

    def _run(self, subcmd: str, args: list[str], timeout_s=15) -> RawCommand:
        self._ensure_connected()
        cmd = [self._cdp_path, subcmd] + args + ["--host", self._host, "--port", self._port]
        r = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout_s, shell=False)
        return RawCommand(returncode=r.returncode, stdout=r.stdout, stderr=r.stderr)

    # eval/click/form/snapshot/get_page_info/wait_page_stable
    # 实现与 LegacyAdapter 的归一化逻辑完全一致
```

**BrowserManager 改造**：
- `open()` 连接成功后在 `finally` 保护下注入重试
- `ws_url` 不变时复用旧连接；页面 target 消失时自动重连
- `close()` 始终在 `finally` 执行

---

## 五、文件结构

```
lanuage_core/                          # 新目录（后续独立包）
├── cdp_protocol.py                    # CDPClient Protocol + CommandResult + 异常
├── legacy_adapter.py                  # CDPHelper → CDPClient 适配器
├── __init__.py

company/lanuage/src/                   # lanuage 原目录
├── common.py                          # CDPHelper + _run_command() + RawCommand
├── json_pipeline.py                   # 接收 CDPClient 实例（依赖注入）
├── json_executor.py                   # 接收 CDPClient 实例
├── ... (其他文件不变)

company/newTaskTest/
├── src/
│   ├── browser.py                     # BrowserManager（已有，加 finally 保护）
│   ├── bit_cdp_adapter.py             # BitCDPAdapter（新增）
│   ├── config.py                      # 已有
│   └── logger.py                      # 已有
├── config.yaml                        # bit.sh 配置
├── bit.sh                             # 已有
└── cdp                                # 已有

tests/
├── test_cdp_protocol.py               # FakeCDP 单元测试
├── test_legacy_adapter.py             # frame_id 传递 + _pipeline_form 回归
├── test_bit_adapter.py                # Bit 浏览器 iframe 集成测试
```

---

## 六、测试

### test_cdp_protocol.py（FakeCDP）

```python
class FakeCDP:
    """Records all calls, returns configurable responses."""
    def __init__(self):
        self.calls: list[dict] = []
        self._eval_results: dict[str, str] = {}
        self._click_ok = True
        ...

# 用例
def test_frame_id_passed_to_eval():
    cdp = FakeCDP()
    cdp.eval("1+1", frame_id="abc123")
    assert cdp.calls[-1]["frame_id"] == "abc123"

def test_pipeline_form_forwards_frame_id():
    """回归：_pipeline_form 必须把 frame_id 同时传给 eval 和 form"""
    cdp = FakeCDP()
    cdp.eval_results = {"e.tagName": "SELECT"}
    pipeline._pipeline_form("select", select="US", frame_id="iframe1")
    eval_call = [c for c in cdp.calls if c["method"] == "eval"][0]
    form_call = [c for c in cdp.calls if c["method"] == "form"][0]
    assert eval_call["frame_id"] == "iframe1"
    assert form_call["frame_id"] == "iframe1"

def test_eval_error_raises_cdp_error():
    cdp = FakeCDP(eval_raises=CDPExecutionError("timeout"))
    with pytest.raises(CDPExecutionError):
        cdp.eval("1+1")
```

### test_bit_adapter.py（集成测试，需要比特浏览器）

```python
def test_form_in_iframe():
    """form fill 的值实际进入 iframe 内控件"""
    with BrowserManager(config.browser) as bm:
        cdp = BitCDPAdapter(bm, config.cdp_binary)
        cdp.eval("window.location.href = 'http://test-page/iframe-form'")
        result = cdp.form("#name", value="John", frame_id="iframe1")
        assert result.ok
        val = cdp.eval("document.querySelector('#name').value", frame_id="iframe1")
        assert val == "John"
```

---

## 七、实施顺序

| Phase | 内容 |
|-------|------|
| 1 | `cdp_protocol.py` + `CommandResult` + 异常类 |
| 2 | `CDPHelper._run_command()` 底层修复 |
| 3 | `LegacyAdapter` |
| 4 | 迁移清单 A（frame_id keyword-only） |
| 5 | 迁移清单 B（click/form 返回值） |
| 6 | 迁移清单 C（数据归一化）+ `_pipeline_form` frame_id 修复 |
| 7 | 删除 `click_checked()` |
| 8 | `BitCDPAdapter`（newTaskTest 侧） |
| 9 | `BrowserManager` finally 保护 |
| 10 | 测试（FakeCDP 单测 + Bit iframe 集成测试） |
