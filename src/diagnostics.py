"""执行诊断系统 — 步进追踪、页面快照、失败分类、报告生成。"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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


class StepTracer:
    """收集每步执行结果，计算总耗时。"""

    def __init__(self):
        self._results: list[StepResult] = []
        self._start_time_total: float = 0
        self._start_time_step: Optional[float] = None

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
        if self._start_time_step is None:
            duration = 0
        else:
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
        return round((time.time() - self._start_time_total) * 1000, 3)

    @property
    def failure_count(self) -> int:
        return sum(1 for r in self._results if r.success is False)

    @property
    def total_count(self) -> int:
        return len(self._results)


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
            if not isinstance(child, dict):
                continue
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
        raw = self.cdp.eval(f"(function(){{{js}}})()", frame_id=frame_id)
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
        raw = self.cdp.eval(f"(function(){{{js}}})()", frame_id=frame_id)
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
            error_lower = str(s.error).lower()
            # iframe_miss: 步骤含 frame_url 但定位失败
            if "iframe" in error_lower or "frame" in error_lower:
                return "iframe_miss"

        for s in failed:
            error_lower = str(s.error).lower()
            if "locator" in error_lower or "cannot locate" in error_lower or "not found" in error_lower or "no candidates" in error_lower:
                return "locator"

        for s in failed:
            if "timeout" in str(s.error).lower() or "timed out" in str(s.error).lower():
                return "timeout"

        return "unknown"


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

        # 标注一次，两处复用
        annotated_config = self._annotate_config(steps, config)

        # 写 JSON 报告（使用 safe_serialize 处理不可序列化对象）
        json_data = self._build_json(run_info, outcome, steps, annotated_config)
        self._safe_write(json_path, self._safe_serialize(json_data))

        # 写 Markdown 报告
        md_content = self._build_markdown(run_info, outcome, steps, annotated_config)
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
                    steps: list, annotated_config: dict) -> dict:
        """构建 JSON 报告结构。"""
        return {
            "run": run_info,
            "outcome": outcome,
            "steps": [self._step_to_dict(s) for s in steps],
            "config": annotated_config
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
                        steps: list, annotated_config: dict) -> str:
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
            err = str(s.error)[:60] if s.error else ""
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
                snap = s.snapshot if isinstance(s.snapshot, dict) else {}
                if snap.get("url"):
                    lines.append(f"**页面 URL:** {snap['url']}")
                if snap.get("iframes"):
                    lines.append(f"**⚠️ 页面有 iframe:**")
                    for f in snap["iframes"]:
                        if isinstance(f, dict):
                            lines.append(f"  - `{f.get('src', '?')}` (visible={f.get('vis', False)})")
                        else:
                            lines.append(f"  - `{f}`")
                if s.candidates:
                    lines.append(f"**候选匹配:**")
                    for c in s.candidates[:10]:
                        if isinstance(c, dict):
                            lines.append(f"  - `{c.get('selector', '?')}` ({c.get('strategy', '?')}, conf={c.get('confidence', 0):.2f})")
                        else:
                            lines.append(f"  - `{c}`")
                if snap.get("inputs"):
                    lines.append(f"**可见输入框 ({len(snap['inputs'])}):**")
                    for inp in snap["inputs"][:15]:
                        if isinstance(inp, dict):
                            lines.append(f"  - tag={inp.get('t','?')} id={inp.get('id','?')} name={inp.get('n','?')}")
                        else:
                            lines.append(f"  - `{inp}`")
                if snap.get("buttons"):
                    lines.append(f"**可见按钮 ({len(snap['buttons'])}):**")
                    for btn in snap["buttons"][:15]:
                        if isinstance(btn, dict):
                            lines.append(f"  - tag={btn.get('t','?')} text={btn.get('text','?')}")
                        else:
                            lines.append(f"  - `{btn}`")
                lines.append("")

        # JSON 配置
        lines.append("## JSON 配置")
        lines.append("")
        lines.append("```json")
        lines.append(self._safe_serialize(annotated_config))
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
