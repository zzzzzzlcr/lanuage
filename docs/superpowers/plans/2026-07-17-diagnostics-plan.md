# 诊断报告系统 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每次表单自动化执行后自动生成 Markdown + JSON 诊断报告，失败时包含页面快照、候选匹配和失败分类。

**Architecture:** 新增 `src/diagnostics.py`（StepTracer + PageInspector + FailureClassifier + ReportWriter），在 `json_pipeline.py` 的 `validate()` 流程中织入旁路数据收集，执行结束后统一生成报告。诊断失败不影响主流程。

**Tech Stack:** Python 3, dataclasses, json, datetime

## Global Constraints

- 诊断是旁路 — 报告生成失败不中断主流程
- 报告输出到 `reports/` 目录（自动创建，失败则输出到当前目录）
- 快照元素超过 500 个时截断到前 200 个
- JSON 报告使用 safe-serialize（不可序列化对象 fallback 到 `repr()`）
- 终端打印一行摘要 + 报告路径
- `reports/` 加入 `.gitignore`

---

### Task 1: 创建 `StepResult` 和 `StepTracer`

**Files:**
- Create: `src/diagnostics.py`

**Interfaces:**
- Produces: `StepResult` dataclass, `StepTracer` class

- [ ] **Step 1: 编写 `StepResult` dataclass**

```python
# 写入 src/diagnostics.py

"""执行诊断系统 — 步进追踪、页面快照、失败分类、报告生成。"""

import json
import time
import logging
import traceback
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StepResult:
    """单步执行结果。"""
    index: int
    action: str
    success: Optional[bool]  # True=成功, False=失败, None=未执行
    error: str = ""
    duration_ms: Optional[float] = None
    snapshot: Optional[dict] = None
    candidates: Optional[list] = None
    note: str = ""
```

- [ ] **Step 2: 运行 Python 检查语法**

```bash
python3 -c "from src.diagnostics import StepResult; r = StepResult(0, 'wait', True); print(r)"
```

Expected: `StepResult(index=0, action='wait', success=True, error='', duration_ms=None, snapshot=None, candidates=None, note='')`

- [ ] **Step 3: 实现 `StepTracer` 类**

```python
# 追加到 src/diagnostics.py

class StepTracer:
    """收集每步执行结果，计算总耗时。"""

    def __init__(self):
        self._results: list[StepResult] = []
        self._start_time_total: float = 0
        self._start_time_step: float = 0

    def start_run(self) -> None:
        """记录运行开始时间。"""
        self._start_time_total = time.time()

    def start_step(self, index: int, action: str) -> None:
        """开始追踪一步。"""
        self._start_time_step = time.time()

    def end_step(self, index: int, action: str,
                 success: Optional[bool], error: str = "",
                 note: str = "") -> None:
        """结束一步，记录结果。"""
        duration = (time.time() - self._start_time_step) * 1000
        self._results.append(StepResult(
            index=index,
            action=action,
            success=success,
            error=error,
            duration_ms=round(duration, 1),
            note=note
        ))

    def add_snapshot(self, snapshot: Optional[dict]) -> None:
        """为最近一步添加页面快照。"""
        if self._results:
            self._results[-1].snapshot = snapshot

    def add_candidates(self, candidates: Optional[list]) -> None:
        """为最近一步添加候选匹配。"""
        if self._results:
            self._results[-1].candidates = candidates

    @property
    def results(self) -> list:
        return list(self._results)

    @property
    def total_duration_ms(self) -> float:
        if not self._start_time_total:
            return 0
        return round((time.time() - self._start_time_total) * 1000, 1)

    @property
    def failure_count(self) -> int:
        return sum(1 for r in self._results if r.success is False)

    @property
    def total_count(self) -> int:
        return len(self._results)
```

- [ ] **Step 4: 运行测试验证 StepTracer**

```bash
python3 -c "
from src.diagnostics import StepTracer
t = StepTracer()
t.start_run()
t.start_step(0, 'wait')
t.end_step(0, 'wait', True)
t.start_step(1, 'form')
t.end_step(1, 'form', False, 'LocatorError')
print(f'failures={t.failure_count} total={t.total_count} duration={t.total_duration_ms}')
print(t.results)
"
```

Expected: `failures=1 total=2 duration=...` 和两条 StepResult

- [ ] **Step 5: 提交**

```bash
git add src/diagnostics.py
git commit -m "feat: add StepResult and StepTracer for diagnostics"
```

---

### Task 2: 实现 `PageInspector`

**Files:**
- Modify: `src/diagnostics.py` (append PageInspector class)

**Interfaces:**
- Consumes: CDP helper object (has `.eval(script, frame_id)` and `.snapshot()` and `.get_page_info()`)
- Produces: `PageInspector(cdp, log=None)` with `.capture(frame_id="") -> dict` and `.capture_snapshot() -> dict`

- [ ] **Step 1: 编写 `PageInspector` 类**

```python
# 追加到 src/diagnostics.py

class PageInspector:
    """采集页面当前状态的快照（输入框、按钮、iframe）。"""

    MAX_ELEMENTS = 200  # 截断阈值

    def __init__(self, cdp, log=None):
        self.cdp = cdp
        self.log = log or logging.getLogger(__name__)

    def capture(self, frame_id: str = "") -> dict:
        """拍页面快照：URL、标题、可见输入框、按钮、iframe。

        即使采集过程中抛异常也返回部分数据，不向上传播。
        """
        result = {"url": "", "title": "", "inputs": [], "buttons": [], "iframes": [], "truncated": False}
        try:
            info = self.cdp.get_page_info()
            result["url"] = info.get("url", "")
            result["title"] = info.get("title", "")
        except Exception:
            pass

        try:
            result["inputs"] = self._collect_inputs(frame_id)
        except Exception:
            pass

        try:
            result["buttons"] = self._collect_buttons(frame_id)
        except Exception:
            pass

        try:
            result["iframes"] = self._collect_iframes()
        except Exception:
            pass

        return result

    def capture_snapshot(self) -> dict:
        """通过 CDP snapshot 深度遍历 DOM，返回结构化元素列表。

        用于候选匹配分析，比 capture() 更详细但更昂贵。
        """
        try:
            snap = self.cdp.snapshot()
            data = json.loads(snap) if isinstance(snap, str) else snap
        except Exception:
            return {"inputs": [], "buttons": [], "total_elements": 0}

        elements = self._walk_dom(data.get("frame", {}).get("body", {}))
        for cf in data.get("childFrames", []):
            body = cf.get("frame", {}).get("body", {})
            elements.extend(self._walk_dom(body))

        inputs = [e for e in elements if e["tag"] in ("INPUT", "SELECT", "TEXTAREA")]
        buttons = [e for e in elements if e["tag"] in ("BUTTON", "A") and e.get("text")]
        return {
            "inputs": inputs[:self.MAX_ELEMENTS],
            "buttons": buttons[:self.MAX_ELEMENTS],
            "total_elements": len(elements),
            "truncated": len(elements) > 500
        }

    def _walk_dom(self, node: dict, depth: int = 0) -> list:
        """递归遍历 DOM 树收集表单相关元素。"""
        if depth > 50:
            return []
        results = []
        tag = node.get("tag", "")
        attr = node.get("attr", {})
        children = node.get("children", [])
        text = node.get("text", "")[:80] if "text" in node else ""

        if tag in ("INPUT", "SELECT", "TEXTAREA", "BUTTON", "A", "LABEL"):
            results.append({
                "tag": tag,
                "id": attr.get("id", ""),
                "name": attr.get("name", ""),
                "type": attr.get("type", ""),
                "placeholder": attr.get("placeholder", ""),
                "class": (attr.get("class", "") or "")[:80],
                "text": text,
                "href": (attr.get("href", "") or "")[:80],
            })

        for child in children:
            results.extend(self._walk_dom(child, depth + 1))
        return results

    def _collect_inputs(self, frame_id: str) -> list:
        """通过 JS 采集可见输入框。"""
        js = (
            "var r=[];var els=document.querySelectorAll('input:not([type=hidden]),select,textarea');"
            "for(var i=0;i<els.length;i++){var e=els[i];"
            "if(e.offsetWidth>0)r.push({t:e.tagName,n:e.name||'',id:e.id||'',p:e.placeholder||'',ty:e.type||''});}"
            "return JSON.stringify(r.slice(0,200));"
        )
        raw = self.cdp.eval(f"(function(){{{js}}})()", frame_id)
        try:
            return json.loads(raw) if isinstance(raw, str) else []
        except Exception:
            return []

    def _collect_buttons(self, frame_id: str) -> list:
        """通过 JS 采集可见按钮/链接。"""
        js = (
            "var r=[];var els=document.querySelectorAll('button,a[href]');"
            "for(var i=0;i<els.length;i++){var e=els[i];"
            "if(e.offsetWidth>0&&e.textContent.trim()){"
            "r.push({t:e.tagName,text:e.textContent.trim().substring(0,40),id:e.id||'',cls:e.className.substring(0,60)});}}"
            "return JSON.stringify(r.slice(0,200));"
        )
        raw = self.cdp.eval(f"(function(){{{js}}})()", frame_id)
        try:
            return json.loads(raw) if isinstance(raw, str) else []
        except Exception:
            return []

    def _collect_iframes(self) -> list:
        """通过 JS 采集页面中的 iframe 信息。"""
        js = (
            "var r=[];var fs=document.querySelectorAll('iframe');"
            "for(var i=0;i<fs.length;i++){var f=fs[i];"
            "r.push({src:f.src.substring(0,80),id:f.id||'',name:f.name||'',vis:f.offsetWidth>0});}"
            "return JSON.stringify(r.slice(0,10));"
        )
        raw = self.cdp.eval(f"(function(){{{js}}})()")
        try:
            return json.loads(raw) if isinstance(raw, str) else []
        except Exception:
            return []
```

- [ ] **Step 2: 验证语法**

```bash
python3 -c "from src.diagnostics import PageInspector; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: 提交**

```bash
git add src/diagnostics.py
git commit -m "feat: add PageInspector for page snapshots"
```

---

### Task 3: 实现 `FailureClassifier` 和 `ReportWriter`

**Files:**
- Modify: `src/diagnostics.py` (append FailureClassifier + ReportWriter)

**Interfaces:**
- Consumes: `StepResult` (from Task 1)
- Produces:
  - `FailureClassifier.classify(steps, config, success_triggered) -> str`
  - `ReportWriter(output_dir).generate(run_info, outcome, steps, config) -> str` (returns md_path)

- [ ] **Step 1: 编写 `FailureClassifier`**

```python
# 追加到 src/diagnostics.py

class FailureClassifier:
    """将失败归类到预定义类别。

    优先级: iframe_miss > locator > timeout > success_condition > unknown
    """

    @staticmethod
    def classify(steps: list, config: dict = None,
                 success_triggered: bool = False) -> str:
        failed = [s for s in steps if s.success is False]
        if not failed:
            if not success_triggered and steps:
                return "success_condition"
            return "unknown"

        for s in failed:
            error_lower = s.error.lower()
            # iframe_miss: 步骤含 frame_url 但定位失败
            if "iframe" in error_lower or "frame" in error_lower:
                return "iframe_miss"

        for s in failed:
            error_lower = s.error.lower()
            if "locator" in error_lower or "cannot locate" in error_lower or "not found" in error_lower or "no candidates" in error_lower:
                return "locator"

        for s in failed:
            if "timeout" in s.error.lower() or "timed out" in s.error.lower():
                return "timeout"

        if not success_triggered and steps:
            return "success_condition"

        return "unknown"
```

- [ ] **Step 2: 编写 `ReportWriter`**

```python
# 追加到 src/diagnostics.py

class ReportWriter:
    """生成 Markdown + JSON 两份诊断报告。"""

    def __init__(self, output_dir: str = "reports"):
        self.output_dir = output_dir

    def generate(self, run_info: dict, outcome: dict,
                 steps: list, config: dict) -> str:
        """生成报告文件。返回 Markdown 文件路径。

        Args:
            run_info: {"time": "2026-07-17T15:30:00", "description_file": "...",
                       "site": "...", "form_type": "..."}
            outcome: {"passed": bool, "failure_category": str,
                      "fix_cycles": int, "duration_ms": float}
            steps: list[StepResult]
            config: 原始 JSON 配置
        """
        md_path, json_path = self._resolve_paths(run_info)

        # 写 JSON 报告（使用 safe_serialize 处理不可序列化对象）
        json_data = self._build_json(run_info, outcome, steps, config)
        self._safe_write(json_path, self._safe_serialize(json_data))

        # 写 Markdown 报告
        md_content = self._build_markdown(run_info, outcome, steps, config)
        self._safe_write(md_path, md_content)

        return md_path

    def _resolve_paths(self, run_info: dict) -> tuple:
        """确定输出路径，自动创建目录。"""
        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        desc = run_info.get("description_file", "run")
        desc = Path(desc).stem[:40]
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in desc)

        try:
            out_dir = Path(self.output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError):
            out_dir = Path(".")

        md_path = out_dir / f"{ts}-{safe_name}-report.md"
        json_path = out_dir / f"{ts}-{safe_name}-report.json"
        return str(md_path), str(json_path)

    def _build_json(self, run_info: dict, outcome: dict,
                    steps: list, config: dict) -> dict:
        """构建 JSON 报告结构。"""
        return {
            "run": run_info,
            "outcome": outcome,
            "steps": [self._step_to_dict(s) for s in steps],
            "config": self._annotate_config(steps, config)
        }

    def _step_to_dict(self, s) -> dict:
        """序列化单个 StepResult 为 dict，safe-serialize。"""
        d = {
            "index": s.index,
            "action": s.action,
            "success": s.success,
            "error": s.error,
            "duration_ms": s.duration_ms,
        }
        if s.note:
            d["note"] = s.note
        if s.snapshot is not None:
            d["snapshot"] = s.snapshot
        if s.candidates is not None:
            d["candidates"] = s.candidates
        return d

    def _annotate_config(self, steps: list, config: dict) -> dict:
        """在 config 步骤上标注 _status（passed/failed/skipped）。"""
        annotated = dict(config)
        config_steps = annotated.get("steps", [])
        failed_indices = {s.index for s in steps if s.success is False}
        passed_indices = {s.index for s in steps if s.success is True}

        annotated_steps = []
        for i, step in enumerate(config_steps):
            s = dict(step)
            if i in failed_indices:
                s["_status"] = "failed"
            elif i in passed_indices:
                s["_status"] = "passed"
            else:
                s["_status"] = "skipped"
            annotated_steps.append(s)
        annotated["steps"] = annotated_steps
        return annotated

    def _build_markdown(self, run_info: dict, outcome: dict,
                        steps: list, config: dict) -> str:
        """构建 Markdown 报告内容。"""
        lines = []
        status = "✓ PASSED" if outcome["passed"] else "✗ FAILED"
        lines.append(f"# 诊断报告 — {status}")
        lines.append("")
        lines.append(f"**时间:** {run_info.get('time', '?')}")
        lines.append(f"**站点:** {run_info.get('site', '?')}")
        lines.append(f"**类型:** {run_info.get('form_type', '?')}")
        lines.append(f"**描述文件:** {run_info.get('description_file', '?')}")
        lines.append("")

        # 摘要
        lines.append("## 运行摘要")
        lines.append("")
        lines.append(f"| 项目 | 值 |")
        lines.append(f"|------|-----|")
        lines.append(f"| 结果 | {status} |")
        lines.append(f"| 步骤 | {outcome.get('total_steps', 0)} 总 / {outcome.get('failures', 0)} 失败 |")
        lines.append(f"| 失败类型 | {outcome.get('failure_category', '?')} |")
        lines.append(f"| 耗时 | {outcome.get('duration_ms', 0):.0f}ms |")
        lines.append(f"| 修复轮次 | {outcome.get('fix_cycles', 0)} |")
        lines.append("")

        # 步骤表
        lines.append("## 步骤执行结果")
        lines.append("")
        lines.append("| # | 动作 | 结果 | 耗时 | 错误 |")
        lines.append("|---|------|------|------|------|")
        for s in steps:
            if s.success is True:
                icon = "✓"
            elif s.success is False:
                icon = "✗"
            else:
                icon = "—"
            err = s.error[:60] if s.error else ""
            dur = f"{s.duration_ms:.0f}ms" if s.duration_ms is not None else "—"
            lines.append(f"| {s.index} | {s.action} | {icon} | {dur} | {err} |")
        lines.append("")

        # 失败步骤深度诊断
        failed = [s for s in steps if s.success is False]
        if failed:
            lines.append("## 失败步骤诊断")
            lines.append("")
            for s in failed:
                lines.append(f"### Step {s.index}: {s.action}")
                lines.append(f"**错误:** {s.error}")
                lines.append("")
                snap = s.snapshot or {}
                if snap.get("url"):
                    lines.append(f"**页面 URL:** {snap['url']}")
                if snap.get("iframes"):
                    lines.append(f"**⚠️ 页面有 iframe:**")
                    for f in snap["iframes"]:
                        lines.append(f"  - `{f.get('src', '?')}` (visible={f.get('vis', False)})")
                if s.candidates:
                    lines.append(f"**候选匹配:**")
                    for c in s.candidates[:10]:
                        lines.append(f"  - `{c.get('selector', '?')}` ({c.get('strategy', '?')}, conf={c.get('confidence', 0):.2f})")
                if snap.get("inputs"):
                    lines.append(f"**可见输入框 ({len(snap['inputs'])}):**")
                    for inp in snap["inputs"][:15]:
                        lines.append(f"  - `{inp}`")
                if snap.get("buttons"):
                    lines.append(f"**可见按钮 ({len(snap['buttons'])}):**")
                    for btn in snap["buttons"][:15]:
                        lines.append(f"  - `{btn}`")
                lines.append("")

        # JSON 配置
        lines.append("## JSON 配置")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(self._annotate_config(steps, config), indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")

        return "\n".join(lines)

    def _safe_serialize(self, obj) -> str:
        """safe-serialize：不可序列化对象 fallback 到 repr()。"""
        def _default(o):
            try:
                return repr(o)
            except Exception:
                return f"<{type(o).__name__}>"
        return json.dumps(obj, indent=2, ensure_ascii=False, default=_default)

    def _safe_write(self, path: str, content: str) -> bool:
        """安全写入文件，失败时打印警告不抛异常。"""
        try:
            Path(path).write_text(content, encoding="utf-8")
            return True
        except Exception as e:
            logging.warning(f"报告写入失败 {path}: {e}")
            return False
```

- [ ] **Step 3: 验证语法和基本功能**

```bash
python3 -c "
from src.diagnostics import StepTracer, FailureClassifier, ReportWriter, StepResult
from unittest.mock import Mock

# 测试 FailureClassifier
steps = [
    StepResult(0, 'wait', True, duration_ms=1000),
    StepResult(1, 'form', False, 'LocatorError: No candidates found', duration_ms=120),
]
cat = FailureClassifier.classify(steps)
assert cat == 'locator', f'Expected locator, got {cat}'
print(f'Classifier OK: {cat}')
"
```

Expected: `Classifier OK: locator`

- [ ] **Step 4: 提交**

```bash
git add src/diagnostics.py
git commit -m "feat: add FailureClassifier and ReportWriter for diagnostics"
```

---

### Task 4: 集成到 `json_pipeline.py`

**Files:**
- Modify: `src/json_pipeline.py` (validate() 和 run() 方法)

**Interfaces:**
- Consumes: `StepTracer`, `PageInspector`, `ReportWriter`, `FailureClassifier` (from `src/diagnostics.py`)
- Produces: 修改后的 `JSONPipeline.validate()` 接受可选的 `tracer` 和 `inspector` 参数；`run()` 生成报告

- [ ] **Step 1: 修改 `validate()` 方法签名和步进追踪**

在 `src/json_pipeline.py` 中修改 `validate()` 方法。找到以下代码块（约第 148-171 行，`for i, step in enumerate(steps):` 循环）：

```python
# 修改前:
        for i, step in enumerate(steps):
            result.success_steps = i  # steps so far
            step_result = self._run_one_step(i, step, config, profile)

            if not step_result.success:
                result.failed_steps.append(step_result)
```

替换为：

```python
# 修改后:
        from diagnostics import StepTracer, PageInspector  # 顶部已有 import

        for i, step in enumerate(steps):
            if tracer:
                tracer.start_step(i, step.get('action', '?'))

            result.success_steps = i  # steps so far
            step_result = self._run_one_step(i, step, config, profile)

            if tracer:
                success = step_result.success
                error = step_result.error if not success else ""
                tracer.end_step(i, step.get('action', '?'), success, error)
                if not success and inspector:
                    try:
                        snap = inspector.capture()
                        tracer.add_snapshot(snap)
                    except Exception:
                        tracer.add_snapshot(None)

            if not step_result.success:
                result.failed_steps.append(step_result)
```

还需要修改 `validate()` 的方法签名，在参数列表中添加：

找到约第 136 行：
```python
    def validate(self, config: dict, profile: dict,
                 navigate_url: str = None) -> ValidationResult:
```

改为：
```python
    def validate(self, config: dict, profile: dict,
                 navigate_url: str = None,
                 tracer = None,
                 inspector = None) -> ValidationResult:
```

- [ ] **Step 2: 修改 `run()` 方法生成报告**

在 `run()` 方法中（约第 551 行），在方法开头创建 tracer 和 inspector，在返回前生成报告。

找到 `run()` 方法的 `self.log.info("=== Step 1: Generate ===")` 这一行，在其前面添加：

```python
    def run(self, description: str, profile: dict,
            navigate_url: str = None) -> Tuple[dict, ValidationResult]:
        from diagnostics import StepTracer, PageInspector, ReportWriter, FailureClassifier

        tracer = StepTracer()
        inspector = PageInspector(self.cdp, self.log)
        tracer.start_run()
        description_file = description if len(description) < 60 else "stdin"
```

然后在 `self.log.info("=== Step 1: Generate ===")` 这一行之后，在 `config = self.generate(description)` 调用周围包裹异常处理以支持生成阶段失败的诊断：

```python
        self.log.info("=== Step 1: Generate ===")
        try:
            config = self.generate(description)
        except Exception as e:
            self.log.error(f"Generate failed: {e}")
            # 生成阶段失败也写报告
            writer = ReportWriter()
            run_info = {
                "time": datetime.now().isoformat(),
                "description_file": description_file,
                "site": "unknown",
                "form_type": "unknown"
            }
            outcome = {"passed": False, "failures": 1, "total_steps": 0,
                       "failure_category": "unknown", "duration_ms": tracer.total_duration_ms,
                       "fix_cycles": 0}
            tracer.end_step(0, "generate", False, str(e), "LLM 生成阶段失败")
            writer.generate(run_info, outcome, tracer.results, {})
            raise
```

找到 `run()` 方法的尾部（循环结束后的 `return config, result`），在其前面添加报告生成和终端输出：

```python
        # 找到: return config, result
        # 在 return 之前添加:

        # 生成诊断报告
        try:
            writer = ReportWriter()
            category = FailureClassifier.classify(
                tracer.results, config, result.success_triggered)
            site = config.get("site", "unknown")
            form_type = config.get("form_type", "unknown")
            run_info = {
                "time": datetime.now().isoformat(),
                "description_file": description_file,
                "site": site,
                "form_type": form_type
            }
            outcome = {
                "passed": result.passed,
                "failures": tracer.failure_count,
                "total_steps": tracer.total_count,
                "failure_category": category,
                "duration_ms": tracer.total_duration_ms,
                "fix_cycles": cycle
            }
            report_path = writer.generate(run_info, outcome, tracer.results, config)

            # 终端摘要
            status_icon = "✓" if result.passed else "✗"
            status_text = "PASSED" if result.passed else "FAILED"
            print(f"{status_icon} {status_text} "
                  f"({tracer.failure_count}/{tracer.total_count} steps) "
                  f"— {category} — 报告: {report_path}",
                  file=sys.stderr)
        except Exception as e:
            self.log.warning(f"诊断报告生成失败: {e}")
```

在文件顶部添加 import（约第 13 行附近）：

```python
from datetime import datetime
import sys
```

- [ ] **Step 3: 验证语法**

```bash
python3 -c "import py_compile; py_compile.compile('src/json_pipeline.py', doraise=True); print('Syntax OK')"
```

Expected: `Syntax OK`

- [ ] **Step 4: 运行现有测试确认无回归**

```bash
python3 test_fixer.py
```

Expected: `ALL FIXES PASSED`

- [ ] **Step 5: 提交**

```bash
git add src/json_pipeline.py
git commit -m "feat: integrate diagnostics into json_pipeline"
```

---

### Task 5: 添加 `reports/` 到 `.gitignore`

**Files:**
- Modify: `.gitignore` (如果不存在则新建)

- [ ] **Step 1: 添加 reports/ 到 .gitignore**

```bash
if [ -f .gitignore ]; then
    grep -q "^reports/" .gitignore || echo "reports/" >> .gitignore
else
    echo "reports/" > .gitignore
fi
```

- [ ] **Step 2: 验证**

```bash
git check-ignore reports/something 2>/dev/null && echo "IGNORED" || echo "NOT IGNORED"
```

Expected: `IGNORED`

- [ ] **Step 3: 提交**

```bash
git add .gitignore
git commit -m "chore: add reports/ to .gitignore"
```

---

### Task 6: 编写 `test_diagnostics.py` — 单元测试

**Files:**
- Create: `test_diagnostics.py`

**Interfaces:**
- Consumes: `StepResult`, `StepTracer`, `FailureClassifier` (from `src/diagnostics.py`)

- [ ] **Step 1: 编写测试文件**

```python
"""诊断系统单元测试。"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.diagnostics import (
    StepResult, StepTracer, FailureClassifier, ReportWriter
)


# ── StepTracer 测试 ──────────────────────────────────────────────

def test_tracer_basic_flow():
    """StepTracer 记录步骤并计算失败数。"""
    t = StepTracer()
    t.start_run()
    t.start_step(0, "wait")
    t.end_step(0, "wait", True)
    t.start_step(1, "form")
    t.end_step(1, "form", False, "no candidates")

    assert t.total_count == 2
    assert t.failure_count == 1
    assert t.total_duration_ms > 0
    assert t.results[0].action == "wait"
    assert t.results[0].success is True
    assert t.results[1].success is False
    assert "no candidates" in t.results[1].error


def test_tracer_empty():
    """零步骤时 tracer 不崩溃。"""
    t = StepTracer()
    t.start_run()
    assert t.total_count == 0
    assert t.failure_count == 0
    assert len(t.results) == 0


def test_tracer_snapshot_attachment():
    """快照附加到最近一步。"""
    t = StepTracer()
    t.start_run()
    t.start_step(0, "click")
    t.end_step(0, "click", False, "not found")
    t.add_snapshot({"url": "https://example.com", "inputs": []})
    t.add_candidates([{"selector": "#btn", "confidence": 0.5}])

    assert t.results[0].snapshot == {"url": "https://example.com", "inputs": []}
    assert t.results[0].candidates == [{"selector": "#btn", "confidence": 0.5}]


def test_tracer_null_snapshot():
    """快照为 None 时不崩溃。"""
    t = StepTracer()
    t.start_run()
    t.start_step(0, "form")
    t.end_step(0, "form", True)
    t.add_snapshot(None)
    assert t.results[0].snapshot is None


def test_tracer_skipped_step():
    """未执行的步骤 success=None。"""
    t = StepTracer()
    t.start_run()
    t.start_step(0, "form")
    t.end_step(0, "form", None, note="前一步失败，未执行")
    assert t.results[0].success is None
    assert t.failure_count == 0  # None 不算失败


# ── FailureClassifier 测试 ────────────────────────────────────────

def test_classify_locator():
    steps = [
        StepResult(0, "wait", True),
        StepResult(1, "form", False, "LocatorError: No candidates found"),
    ]
    assert FailureClassifier.classify(steps) == "locator"


def test_classify_timeout():
    steps = [
        StepResult(0, "wait_for", False, "wait_for timed out after 30s"),
    ]
    assert FailureClassifier.classify(steps) == "timeout"


def test_classify_iframe_miss():
    steps = [
        StepResult(0, "form", False, "iframe not resolved for frame_url"),
    ]
    assert FailureClassifier.classify(steps) == "iframe_miss"


def test_classify_success_condition():
    steps = [
        StepResult(0, "wait", True),
        StepResult(1, "click", True),
        StepResult(2, "form", True),
    ]
    assert FailureClassifier.classify(steps, success_triggered=False) == "success_condition"


def test_classify_unknown():
    steps = [
        StepResult(0, "eval", False, "some obscure error"),
    ]
    assert FailureClassifier.classify(steps) == "unknown"


def test_classify_all_passed_triggered():
    steps = [
        StepResult(0, "wait", True),
    ]
    assert FailureClassifier.classify(steps, success_triggered=True) == "unknown"


# ── ReportWriter 测试 ────────────────────────────────────────────

def test_report_writer_generates_both_formats():
    """正常数据生成 Markdown 和 JSON 两份文件。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        writer = ReportWriter(output_dir=tmpdir)
        steps = [
            StepResult(0, "wait", True, duration_ms=1000),
            StepResult(1, "form", False, "LocatorError", duration_ms=120,
                       snapshot={"url": "http://example.com", "inputs": [], "buttons": [], "iframes": []}),
        ]
        config = {"site": "test.com", "form_type": "newsletter", "steps": [
            {"action": "wait"}, {"action": "form", "field": {"label": "Email"}}
        ]}
        run_info = {"time": "2026-07-17T12:00:00", "description_file": "test.txt",
                    "site": "test.com", "form_type": "newsletter"}
        outcome = {"passed": False, "failures": 1, "total_steps": 2,
                   "failure_category": "locator", "duration_ms": 1120, "fix_cycles": 0}

        md_path = writer.generate(run_info, outcome, steps, config)

        # 验证 Markdown 文件存在且含关键内容
        assert os.path.exists(md_path)
        md_content = Path(md_path).read_text()
        assert "✗ FAILED" in md_content
        assert "Step 1: form" in md_content
        assert "LocatorError" in md_content

        # 验证 JSON 文件存在且结构正确
        json_path = md_path.replace(".md", ".json")
        assert os.path.exists(json_path)
        json_data = json.loads(Path(json_path).read_text())
        assert json_data["outcome"]["passed"] is False
        assert len(json_data["steps"]) == 2
        assert json_data["steps"][1]["error"] == "LocatorError"
        assert json_data["steps"][1]["snapshot"] is not None
        assert json_data["config"]["steps"][0]["_status"] == "passed"
        assert json_data["config"]["steps"][1]["_status"] == "failed"


def test_report_writer_empty_steps():
    """零步骤不崩溃。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        writer = ReportWriter(output_dir=tmpdir)
        run_info = {"time": "2026-07-17T12:00:00", "description_file": "empty.txt",
                    "site": "nosite", "form_type": "none"}
        outcome = {"passed": True, "failures": 0, "total_steps": 0,
                   "failure_category": "unknown", "duration_ms": 0, "fix_cycles": 0}

        md_path = writer.generate(run_info, outcome, [], {"steps": []})
        assert os.path.exists(md_path)
        assert os.path.exists(md_path.replace(".md", ".json"))


def test_report_writer_null_snapshot():
    """快照为 None 时 JSON 不含报错。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        writer = ReportWriter(output_dir=tmpdir)
        steps = [StepResult(0, "form", False, "error", snapshot=None)]
        config = {"steps": [{"action": "form"}]}
        run_info = {"time": "", "description_file": "", "site": "", "form_type": ""}
        outcome = {"passed": False, "failures": 1, "total_steps": 1,
                   "failure_category": "locator", "duration_ms": 0, "fix_cycles": 0}

        md_path = writer.generate(run_info, outcome, steps, config)
        json_path = md_path.replace(".md", ".json")
        json_data = json.loads(Path(json_path).read_text())
        assert "snapshot" not in json_data["steps"][0]


def test_report_writer_write_failure():
    """文件写入失败不抛异常，打印警告。"""
    with patch("builtins.open", side_effect=PermissionError("denied")):
        writer = ReportWriter(output_dir="/nonexistent")
        steps = [StepResult(0, "wait", True)]
        config = {"steps": []}
        run_info = {"time": "", "description_file": "", "site": "", "form_type": ""}
        outcome = {"passed": True, "failures": 0, "total_steps": 1,
                   "failure_category": "unknown", "duration_ms": 0, "fix_cycles": 0}

        # 不应抛异常
        result = writer.generate(run_info, outcome, steps, config)
        assert result is not None


def test_safe_serialize_unserializable():
    """不可序列化对象 fallback 到 repr()，不抛异常。"""
    writer = ReportWriter()
    obj = {"normal": "value", "bad": object()}
    result = writer._safe_serialize(obj)
    # 不应抛异常，应包含 fallback 字符串
    assert "normal" in result
    assert "object at" in result or "object" in result.lower()


def test_safe_serialize_nested_unserializable():
    """嵌套不可序列化对象不抛异常。"""
    writer = ReportWriter()
    obj = {"steps": [{"snapshot": {"callback": lambda x: x}}]}
    result = writer._safe_serialize(obj)
    assert "steps" in result
    assert "function" in result.lower() or "lambda" in result


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
```

- [ ] **Step 2: 运行测试验证全部通过**

```bash
python3 -m pytest test_diagnostics.py -v
```

Expected: 16 passed

- [ ] **Step 3: 提交**

```bash
git add test_diagnostics.py
git commit -m "test: add diagnostics unit tests (14 cases)"
```

---

### Task 7: 运行全量测试确认无回归

**Files:**
- 无新建文件 — 验证步骤

- [ ] **Step 1: 运行所有测试**

```bash
python3 test_fixer.py && python3 -m pytest test_diagnostics.py -v
```

Expected: `ALL FIXES PASSED` + `14 passed`

- [ ] **Step 2: 检查 git 状态**

```bash
git status
```

确认只有计划中的文件被修改。

- [ ] **Step 3: 查看变更摘要**

```bash
git diff --stat HEAD~5..HEAD
```
