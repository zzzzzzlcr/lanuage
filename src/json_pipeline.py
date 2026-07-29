"""Full pipeline: description → LLM generate → validate → auto-fix loop → human-ready JSON.

Flow:
  1. LLM generates JSON from natural language description
  2. Engine executes JSON in browser, collecting per-step results
  3. Failed steps → LLM receives failure report + page snapshot → generates fix
  4. Re-validate up to 3 fix cycles
  5. Return final JSON + validation report
"""

import json, time, logging, traceback
from pathlib import Path
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass, field
from json_executor import JSONExecutor


@dataclass
class StepResult:
    """Result of a single step execution."""
    index: int
    action: str
    find_spec: dict = field(default_factory=dict)
    success: bool = True
    error: str = ""
    page_url: str = ""
    page_body: str = ""


@dataclass
class ValidationResult:
    """Complete validation run result."""
    passed: bool
    total_steps: int
    success_steps: int
    failed_steps: List[StepResult] = field(default_factory=list)
    final_url: str = ""
    final_body: str = ""
    success_triggered: bool = False


class JSONPipeline:
    """Orchestrate the generate → validate → fix → review loop."""

    MAX_FIX_CYCLES = 3

    def __init__(self, llm_client, cdp, model: str = "deepseek-v4-flash"):
        self.llm = llm_client
        self.cdp = cdp
        self.model = model
        self.log = logging.getLogger(__name__)

    # ==================================================================
    # Step 1: Generate JSON from natural language
    # ==================================================================

    GENERATE_PROMPT = '''你是表单自动化 JSON 配置生成器。根据运营描述生成 JSON。

## 核心规则（以下每一条都来自真实测试发现）

### 0. 动词→动作速查（必读！）
运营描述中的动词对应唯一的 action 类型，不要自己编造新 action：
| 运营怎么写 | action | JSON 模板 |
|-----------|--------|----------|
| 等待 X-Y 秒 | wait | {"action":"wait","min":X,"max":Y} |
| 点击XXX | click | {"action":"click","find":{"text":"XXX"}} |
| - 如果XXX是agree/terms/privacy/consent/policy等checkbox文字 → 用勾选代替点击: {"action":"form","field":{"label":"XXX","type":"checkbox"},"check":"true"} |
| 填邮箱/填姓名/填密码/填写XXX | form | {"action":"form","field":{"label":"XXX","type":"推断"},"value":"{{变量}}"} |
| 选择XXX（下拉框，选YYY） | form | {"action":"form","field":{"label":"XXX"},"select":"YYY"} |
| 选择评分N（星级） | form | {"action":"form","field":{"label":"rating"},"select":"N"} |
| 随机选一个选项 | select | {"action":"select","selection_strategy":{"type":"random"}} |
| 勾选 XXX | form | {"action":"form","field":{"label":"XXX","type":"checkbox"},"check":"true"} |
| 滚动 | scroll | {"action":"scroll","min":100,"max":500} |
| 拖动XXX（滚动条） | eval | {"action":"eval","script":"var sl=document.querySelector('input[type=range]');..."} |
**可用 action：wait, click, form, select, scroll, eval, delay, goto, report**
**重要: field.label 和 find.text 必须用英文！页面上都是英文字，不要用中文描述里的词。**
**例: 运营写"填邮箱"→label:"email"; 运营写"点击提交"→find:{"text":"Submit"}**

### 1. 按钮点击
- 普通按钮: {"action":"click","find":{"text":"按钮文字"}} (不要加optional除非运营写了"(可选)")
- iframe里的按钮: 必须用eval！因为cdp click在iframe里点不动:
  {"action":"eval","script":"var bs=document.querySelectorAll('button');for(var i=0;i<bs.length;i++){if(bs[i].textContent.trim()==='按钮文字'&&bs[i].offsetWidth>0){bs[i].click();}}","optional":true}
  判断标准: 如果按钮在iframe里(运营说了"在iframe里"), 就用eval (不要加optional除非运营写"(可选)")

### 2. 填表单
- 有id提示: {"action":"form","find":{"id":"输入框id"},"value":"{{变量}}"} ← 运营写了id就必须用find.id！
- 普通: {"action":"form","field":{"label":"标签","type":"推断类型"},"value":"{{变量}}"}
- **运营写了placeholder必须原样保留**: 填 Email(placeholder=Email address) → {"field":{"label":"Email","type":"email","placeholder":"Email address"}}
- **运营写了name必须原样保留**: 填 Country(name=stateResidency) → {"field":{"label":"Country","name":"stateResidency"}}
- iframe里: 加 "frame_url":"URL关键词" (只用域名部分, 如"entyrecare"不用"forms.entyrecare")
- type推断: email→email, phone/手机→tel, password/密码→password, name/姓名→text
- 运营描述里有"输入框id=xxx"或"id=xxx"时，必须用find.id方式，不要用field.label

### 3. 滚动条/滑块 (range)
运营说"拖动XXX到YYY"或"选择XXX（滚动条）"时:
- 用eval直接设值+触发事件:
  {"action":"eval","script":"var sl=document.querySelector('input[type=range]');if(sl){sl.value=50000;sl.dispatchEvent(new Event('input',{bubbles:true}));sl.dispatchEvent(new Event('change',{bubbles:true}));}"}
- 数值从运营描述中取，如果运营写"债务金额"或"选择金额"，默认拖到较大值如50000

### 4. 下拉框选择 (select)
运营说"选择XXX（下拉框，选YYY）"或"Select XXX（下拉框，选YYY）"或带"（下拉框"字样时:
- 有id: {"action":"form","find":{"id":"下拉框id"},"select":"选项值"}
- 有label: {"action":"form","field":{"label":"下拉框的标签文字"},"select":"选项值"}
- 注意: "select"字段的值必须在下拉框的option中精确匹配
- 关键: 下拉框必须用 "select":"选项值"，绝对不能用 "value"！value是给输入框填文字用的
- 只要描述里有"（下拉框"字样，就必须用 "select" 而不是 "value"
- 如果运营写"选随机"或"选一个随机的"→ "select":"__random__"（引擎会打开下拉框随机选一个选项）
- 如果运营写"选择Birth Month(选随机)"→ "select":"__random__"
- 如果运营写"选择问题 <问题文字>"→ field.label 直接用<问题文字>原文。例:"选择问题 What Is Your Date of Birth?(选随机)"→ {"field":{"label":"What Is Your Date of Birth?"},"select":"__random__"}

### 5. quiz随机选项
{"action":"select","selection_strategy":{"type":"random"}}

### 6. 状态机模式 (运营用when_XXX格式)
输出:
{
  "site":"域名","form_type":"类型",
  "loop_until":{"url_contains":["/success"]},
  "max_rounds":20,
  "frame_url":"iframe域名关键词",
  "steps":[...]
}
如果整个流程都在iframe里(field和button都是), 加全局frame_url.
这样eval/select/click不用每个都写frame_url, 引擎自动在iframe里搜索.

步骤格式:
- 只执行一次(填表): {"id":"唯一id","when":{"field_exists":{"label":"标签","type":"text/email/tel/password","frame_url":"URL关键词"}},"action":"form","field":{"label":"标签","type":"text/email/tel/password","frame_url":"URL关键词"},"value":"{{变量}}","optional":true}
  重要: field_exists 和 field 都必须有 type 字段！没有 type 定位器找不到元素！
  frame_url 两处都要写（如果有iframe）
- 每轮都执行(quiz/导航): 不要id {"when":{},"action":"select","selection_strategy":{"type":"random"}}
  {"when":{},"action":"eval","script":"...Next...","optional":true}

when规则:
- field_exists: 字段在当前页**可见**时才执行 (填表步骤必须用)
  * 必须指定 type (email/tel/text) 或 id，不能只用 label
  * 系统会检查字段是否真实可见（offsetWidth>0），隐藏的表单字段不会触发
- {}: 总是尝试 (quiz选项、导航按钮)
- **quiz页标准模板（关键！）**:
  {"site":"...","loop_until":{"any":[{"body_contains":["成功文字"]}]},"max_rounds":20,"steps":[
    {"when":{},"action":"select","selection_strategy":{"type":"random"}},
    {"id":"fill","when":{"field_exists":{"id":"email-input","type":"email"}},"action":"form","field":{"id":"email-input","type":"email"},"value":"{{random.email}}"},
    {"when":{},"action":"click","find":{"text":"Submit"},"optional":true}
  ]}
  第一行select是quiz选项（每轮执行），后面是表单字段（visible才执行）
- loop_until 必须用 {"any":[{"body_contains":["文字"]}]} 格式，不能只用 {"body_contains":"文字"}
- 可选步骤加 "optional":true (只有运营写了"(可选)"才加！填表步骤不要加optional)

### 7. 线性模式 (运营用编号1.2.3.格式)
{"site":"完整URL去掉协议","form_type":"...","success":{...},"steps":[...]}
- site 示例: "free.spree.com/maxbonus/" 或 "tello.com"（保留路径部分）

### 变量
{{random.email}} {{random.password}} {{random.name}} {{random.last_name}} {{random.phone}} {{random.zip}}

### 成功条件
{"success":{"any":[{"url_contains":["/path"]},{"body_contains":["文字"]}]}}

## 输出要求
只输出JSON。不要任何解释。'''

    def generate(self, description: str, page_diag: str = "") -> dict:
        """LLM generates JSON config from natural language.
        page_diag: optional first-page DOM snapshot (visible fields, buttons, iframes)."""
        user_msg = description
        if page_diag:
            user_msg = f"## 当前页面可见元素（只能引用下面列出的按钮文字和字段，不要猜测）：\n{page_diag}\n\n## 运营描述：\n{description}"
        response = self.llm.chat.completions.create(
            model=self.model,
            messages=[
                {'role': 'system', 'content': self.GENERATE_PROMPT},
                {'role': 'user', 'content': user_msg}
            ],
            temperature=0.1
        )
        content = response.choices[0].message.content.strip()
        if content.startswith('```'):
            lines = content.split('\n')
            content = '\n'.join(lines[1:])
            if content.rstrip().endswith('```'):
                content = content.rstrip()[:-3]
        print(f"\n{'='*60}\n[LLM RAW OUTPUT]\n{content}\n{'='*60}\n", flush=True)
        config = json.loads(content)
        from auto_fixer import fix
        config = fix(config)
        import json as _j
        print(f"\n[AFTER FIX]\n{_j.dumps(config, indent=2, ensure_ascii=False)}\n{'='*60}\n", flush=True)
        return config

    # ==================================================================
    # Step 2: Validate JSON in browser
    # ==================================================================

    def validate(self, config: dict, profile: dict,
                 navigate_url: str = None) -> ValidationResult:
        """Execute JSON config in browser, collecting per-step results."""
        result = ValidationResult(passed=False, total_steps=0, success_steps=0)

        if navigate_url:
            self.log.info(f"Navigating to {navigate_url}")
            self.cdp.eval(
                f"(function(){{window.location.href='{navigate_url}';}})()")
            time.sleep(3)

        steps = config.get('steps', [])
        result.total_steps = len(steps)

        # Delegate to JSONExecutor — single execution path for both direct and pipeline
        executor = JSONExecutor(config, profile, self.cdp, log=self.log)
        ok = executor.run()
        result.success_steps = executor._steps_run
        result.passed = ok
        # Capture final state
        info = self.cdp.get_page_info()
        result.final_url = info.get('url', '')
        result.final_body = self.cdp.eval(
            "(function(){return document.body?document.body.innerText.substring(0,2000):'';})()")

        # Also check success at end
        succ_config = config.get('success', {})
        if self._check_success_static(succ_config, result.final_url, result.final_body):
            result.passed = True
            result.success_triggered = True

        return result

    def _run_one_step(self, i: int, step: dict, config: dict, profile: dict) -> StepResult:
        """Execute a single step and return its result.
        Delegates form/click/select to JSONExecutor for unified execution."""
        from element_finder import ElementFinder
        from variable_resolver import VariableResolver

        var = VariableResolver(profile, self.log)
        finder = ElementFinder(self.cdp, self.log)
        step = var.resolve_dict(step)

        action = step.get('action', '')
        # Delegate form/click/select to unified executor
        if action in ('form', 'click', 'select'):
            if not hasattr(self, '_executor'):
                self._executor = JSONExecutor.__new__(JSONExecutor)
                self._executor.config = config
                self._executor.profile = profile
                self._executor.cdp = self.cdp
                self._executor.log = self.log
                self._executor.var = var
                self._executor.finder = finder
                from locator import FieldLocator
                self._executor.locator = FieldLocator(self.cdp, log=self.log)
                self._executor._steps_run = 0
                self._executor._frame_id = ""
            ok = self._executor._execute_step(step)
            sr = StepResult(index=i, action=action, find_spec=step.get('find', {}))
            sr.success = ok
            if not ok:
                sr.error = f"Step {i} failed: {step}"
            return sr

        sr = StepResult(
            index=i,
            action=step.get('action', '?'),
            find_spec=step.get('find', {}),
            success=True
        )

        # Capture page state
        try:
            info = self.cdp.get_page_info()
            sr.page_url = info.get('url', '')
            sr.page_body = self.cdp.eval(
                "(function(){return document.body?document.body.innerText.substring(0,1000):'';})()")
        except Exception:
            pass

        action = step.get('action', '')
        optional = step.get('optional', False)

        try:
            if action == 'wait':
                import random
                t = random.uniform(float(step.get('min', 0.3)), float(step.get('max', 1.5)))
                time.sleep(t)

            elif action == 'scroll':
                import random as _r
                px = _r.randint(int(step.get('min', 100)), int(step.get('max', 500)))
                self.cdp.eval(f"(function(){{window.scrollBy(0,{px});}})()")

            elif action == 'eval':
                script = step.get('script', '')
                if script:
                    self.cdp.eval(f"(function(){{{script}}})()")
                else:
                    sr.success = False
                    sr.error = "eval action missing 'script' field"
                    return sr

            elif action == 'click':
                find = step.get('find', {})
                field = step.get('field')
                if field:
                    from locator import FieldLocator, LocatorError
                    loc = FieldLocator(self.cdp, log=self.log)
                    try:
                        result = loc.locate(field, frame_hint=field.get('frame_url',''))
                        selector = result.selector
                    except LocatorError as e:
                        if optional: return sr
                        sr.success = False
                        sr.error = f"Locator failed for {field}: {e}"
                        return sr
                else:
                    selector = finder.find(find)
                if not selector and step.get("selector"):
                    sel = step["selector"]
                    selector = sel.get("primary", sel) if isinstance(sel, dict) else sel
                if not selector:
                    if optional: return sr
                    sr.success = False
                    sr.error = f"Element not found: {find or field}"
                    return sr
                self.cdp.click(selector)

            elif action == 'form':
                find = step.get('find', {})
                field = step.get('field')
                if field:
                    from locator import FieldLocator, LocatorError
                    loc = FieldLocator(self.cdp, log=self.log)
                    try:
                        result = loc.locate(field, frame_hint=field.get('frame_url',''))
                        selector = result.selector
                    except LocatorError as e:
                        sr.success = False
                        sr.error = f"Locator failed for {field}: {e}"
                        return sr
                else:
                    selector = finder.find(find)
                if not selector and step.get("selector"):
                    sel = step["selector"]
                    selector = sel.get("primary", sel) if isinstance(sel, dict) else sel
                if not selector:
                    sr.success = False
                    sr.error = f"Element not found: {find or field}"
                    return sr
                # Auto-detect: if target is <select> and LLM used "value",
                # convert to "select" so the dropdown gets populated correctly.
                value = step.get('value')
                select = step.get('select')
                if not select and value:
                    esc = selector.replace("'", "\\'")
                    tag = self.cdp.eval(
                        f"(function(){{var e=document.querySelector('{esc}');"
                        f"return e?e.tagName:'';}})()")
                    if tag and tag.strip().strip('"').upper() == 'SELECT':
                        select = value
                        value = None
                        self.log.info(f"[auto-fix] Converted value→select for <select> {selector}")

                check = step.get('check')
                ok = self._pipeline_form(selector, value=value, select=select, check=check)
                if not ok:
                    sr.success = False
                    sr.error = f"Form failed for selector {selector}"
                    return sr

            elif action == 'wait_for':
                timeout = step.get('timeout', 30)
                start = time.time()
                while time.time() - start < timeout:
                    if 'find' in step:
                        if finder.find(step['find']):
                            return sr
                    if 'url_contains' in step:
                        info = self.cdp.get_page_info()
                        if step['url_contains'] in info.get('url', ''):
                            return sr
                    time.sleep(0.5)
                sr.success = False
                sr.error = f"wait_for timed out after {timeout}s"
                return sr

            elif action == 'if':
                cond = self._eval_if_condition(step)
                branch = step.get('then' if cond else 'else', [])
                for s in branch:
                    self._run_one_step(-1, s, config, profile)

            elif action == 'report':
                pass  # report steps are no-ops in validation

        except Exception as e:
            sr.success = False
            sr.error = f"{type(e).__name__}: {e}"

        return sr

    def _pipeline_form(self, selector, value=None, select=None, check=None, frame_id=""):
        """Handle form interaction — delegates custom selects to click-based approach."""
        # Checkbox: verify current state before toggling
        if check is not None:
            esc = selector.replace("'", "\\'")
            current = self.cdp.eval(
                f"(function(){{var e=document.querySelector('{esc}');"
                f"return e?String(e.checked):'';}})()", frame_id)
            current = (current or '').strip().strip('"')
            want_checked = (check == "true" or check is True)
            is_checked = (current == 'true')
            if want_checked == is_checked:
                self.log.info(f"[pipeline] checkbox already in desired state, skip")
                return True
        if select:
            esc = selector.replace("'", "\\'")
            tag = self.cdp.eval(
                f"(function(){{var e=document.querySelector('{esc}');"
                f"return e?e.tagName:'';}})()", frame_id)
            tag = (tag or '').strip().strip('"').upper()
            if tag and tag != 'SELECT':
                # Walk up to find the wrapper (cdp needs it for custom select)
                find_wrapper = (
                    f"(function(){{var e=document.querySelector('{esc}');"
                    f"var a=e;for(var i=0;i<4;i++){{a=a.parentElement;if(!a)break;"
                    f"if(a.querySelector('[onclick*=toggle],[onclick*=menu],.css-select__control,[role=combobox]'))"
                    f"{{a.setAttribute('data-csw','1');return'ref';}}"
                    f"}}return'none';}})()"
                )
                r = self.cdp.eval(find_wrapper, frame_id)
                if (r or '').strip().strip('"') == 'ref':
                    selector = '[data-csw="1"]'
                    self.log.info(f"[pipeline] Using wrapper for custom select")
        self.cdp.form(selector, value=value, select=select)
        return True

    def _post_fix(self, config: dict, result=None) -> dict:
        """Code-based post-processing after LLM fix. Fix common LLM mistakes."""
        # 0. If step has 'select', remove restrictive 'type' (selects aren't text inputs)
        for s in config.get("steps", []):
            if s.get("select") and s.get("field", {}).get("type") == "text":
                del s["field"]["type"]
                self.log.info("[post-fix] Removed type=text from select step: %s", s.get("field", {}).get("label"))
        # 1. Ensure loop_until has 'any' wrapper
        lu = config.get("loop_until")
        if lu and "any" not in lu:
            config["loop_until"] = {"any": [lu]}
            self.log.info("[post-fix] Wrapped loop_until in {any: [...]}")

        # 2. Fix field_exists: add semantic hints (placeholder/label) from page snapshot
        snap_inputs = []
        if result and hasattr(self, '_last_snapshot'):
            snap_inputs = self._last_snapshot
        for s in config.get("steps", []):
            fe = (s.get("when", {}) or {}).get("field_exists")
            if fe and not fe.get("id") and not fe.get("label") and not fe.get("placeholder"):
                ftype = fe.get("type", "") or s.get("field", {}).get("type", "")
                # Find semantic hints from page elements matching this type
                for inp in snap_inputs:
                    ph = inp.get("placeholder", "").lower()
                    iid = inp.get("id", "").lower()
                    aria = inp.get("aria", "").lower()
                    if ftype == "email" and ("email" in ph or "email" in iid):
                        fe["placeholder"] = inp.get("placeholder", "")
                        break
                    if ftype == "tel" and ("phone" in ph or "tel" in iid or "phone" in iid):
                        fe["placeholder"] = inp.get("placeholder", "")
                        break
                # Also fix the form field similarly
                if fe.get("placeholder") and s.get("field") and not s["field"].get("id") and not s["field"].get("label"):
                    s["field"]["placeholder"] = fe["placeholder"]
                    self.log.info("[post-fix] Added placeholder=%s from snapshot", fe['placeholder'])

        # 2b. For state machine: ensure field_exists steps have id (prevents re-execution)
        if config.get("loop_until"):
            for s in config.get("steps", []):
                if (s.get("when", {}) or {}).get("field_exists") and not s.get("id"):
                    s["id"] = "step_" + (s.get("field", {}).get("label") or s.get("field", {}).get("type") or "form")
                    self.log.info("[post-fix] Added id=%s to state machine step", s["id"])

        # 2c. For state machine: ensure click/eval steps have when:{}+optional (retry each round)
        if config.get("loop_until"):
            for s in config.get("steps", []):
                if s.get("action") in ("click", "eval") and "when" not in s:
                    s["when"] = {}
                    s["optional"] = True
                    self.log.info("[post-fix] Added when:{}+optional to state machine click step")

        # 2d. For state machine: fix click steps to use text from snapshot (not id)
        snap_buttons = getattr(self, '_last_buttons', [])
        if config.get("loop_until") and snap_buttons:
            for s in config.get("steps", []):
                fid = (s.get("find", {}) or {}).get("id", "")
                if fid and s.get("action") in ("click", "eval"):
                    for btn in snap_buttons:
                        if btn.get("id") == fid and btn.get("text"):
                            s["find"] = {"text": btn["text"]}
                            s["action"] = "click"
                            s["optional"] = True
                            s["when"] = {}
                            self.log.info("[post-fix] Fixed button id->text: %s -> %s", fid, btn["text"])
                            break

        # 3. For state machine, ensure select/random step exists
        if config.get("loop_until"):
            has_select = any(s.get("action") == "select" for s in config.get("steps", []))
            if not has_select:
                config.setdefault("steps", []).insert(0, {
                    "when": {}, "action": "select",
                    "selection_strategy": {"type": "random"}
                })
                self.log.info("[post-fix] Added missing select/random step")

        return config

    def _check_success_static(self, succ: dict, url: str, body: str) -> bool:
        """Check success conditions without executor (static version)."""
        if not succ:
            return False
        conditions = succ.get('any', [succ] if not isinstance(succ, dict) else [])
        if 'any' in succ:
            conditions = succ['any']
        for cond in conditions:
            if 'url_contains' in cond:
                patterns = cond['url_contains']
                if not isinstance(patterns, list): patterns = [patterns]
                if any(p in url for p in patterns):
                    return True
            if 'body_contains' in cond:
                patterns = cond['body_contains']
                if not isinstance(patterns, list): patterns = [patterns]
                if any(p.lower() in body.lower() for p in patterns):
                    return True
        return False

    def _eval_if_condition(self, step: dict) -> bool:
        """Evaluate if-condition."""
        info = self.cdp.get_page_info()
        url = info.get('url', '')
        body = self.cdp.eval(
            "(function(){return document.body?document.body.innerText.substring(0,2000):'';})()")
        if 'body_contains' in step:
            patterns = step['body_contains']
            if not isinstance(patterns, list): patterns = [patterns]
            if any(p in body for p in patterns):
                return True
        if 'url_contains' in step:
            if step['url_contains'] in url:
                return True
        return False

    # ==================================================================
    # Step 3: Auto-fix via LLM
    # ==================================================================

    FIX_PROMPT = '''你是表单自动化 JSON 修复器。根据执行失败的报告修正 JSON。

## 修复策略
- 元素找不到: 看Snapshot DOM分析里的真实元素
  * snap里有id/name→用精确id或name; 只有class没id/name→用selector; 完全没有→可能动态加载,加wait
  * 不要自己编造id/name, 用snapshot里的真实数据
- **重要: 只修复失败的步骤，不要删除或修改成功的步骤**
- **如果有select/quiz步骤在原始JSON中，必须保留，不能删除**
- **状态机模式(loop_until)修复规则**:
  * 不要将状态机改写成线性模式，保留 loop_until + when 结构
  * 如果 field_exists 找不到字段，改用 id 定位：{"field_exists":{"id":"实际id","type":"类型"}}
  * quiz选项步骤用 when:{} 表示每轮都执行
  * loop_until 必须用 {"any":[{"body_contains":["文字"]}]} 格式
- 超时: 增加wait时间
- 点错按钮: 换更精确的text匹配(加tag过滤)
- 页面还没加载: 在前面加wait步骤
- 所有步骤都执行了但成功条件没触发: 说明关键操作没生效
  * 检查是不是点错了按钮→换更精确的选择器
  * 表单可能没填进去→改用eval直接设置value+dispatchEvent
  * 需要更长的等待时间→增加wait
  * 页面可能有字段验证(邮箱格式/手机号检测): 填完每个字段后加2-4秒wait, 填完所有字段后在点提交前再加3-5秒wait
  * 可能有checkbox/consent没勾: 检查页面诊断里的checkbox, 加点击步骤
- 如果按钮找不到: 用eval执行document.querySelector('button').click()
- 如果页面诊断显示输入框和按钮都在iframe里面:
  * 看清楚iframe的src域名, 给步骤加 "frame":{"url_contains":"那个域名"}
  * 点击和填写都要带frame
- eval的script字段必须要有内容，不能为空
- **selector 格式规定（重要！）**:
  * click 步骤用 find: {"text": "按钮文字"} 或 find: {"id": "按钮id"}
  * form 步骤用 field: {"label": "标签文字", "type": "类型"} 或 field: {"id": "输入框id", "type": "类型"}
  * 绝对不要用 Playwright 语法（:has-text()、>>、nth= 等）
  * 绝对不要用 XPath（//button、contains 等）
  * 绝对不要用 "selector" 字段代替 find/field，除非是简单的 CSS selector 如 "#id" 或 ".class"
  * CSS selector 示例: "#fname"、".btn-primary"、"[name=phone]"、"[data-testid=submit]"

## 输出
只输出修正后的完整JSON，不要解释。格式和原JSON完全一致。'''

    def _diagnose_page(self) -> dict:
        """Capture all visible inputs/buttons/links on current page for LLM diagnosis."""
        js_inputs = (
            "var r=[];var els=document.querySelectorAll('input:not([type=hidden]),select,textarea');"
            "for(var i=0;i<els.length;i++){var e=els[i];"
            "if(e.offsetWidth>0){r.push({t:e.tagName,n:e.name||'',id:e.id||'',p:e.placeholder||''});}}"
            "return JSON.stringify(r.slice(0,20));")
        js_buttons = (
            "var r=[];var els=document.querySelectorAll('button,a[href]');"
            "for(var i=0;i<els.length;i++){var e=els[i];"
            "if(e.offsetWidth>0&&e.textContent.trim()){r.push({t:e.tagName,text:e.textContent.trim().substring(0,40),id:e.id||'',cls:e.className.substring(0,60)});}}"
            "return JSON.stringify(r.slice(0,20));")
        js_frames = (
            "var r=[];var fs=document.querySelectorAll('iframe');"
            "for(var i=0;i<fs.length;i++){var f=fs[i];"
            "r.push({src:f.src.substring(0,80),id:f.id||'',name:f.name||'',vis:f.offsetWidth>0});}"
            "return JSON.stringify(r.slice(0,10));")
        return {
            'url': self.cdp.eval("(function(){return window.location.href;})()"),
            'title': self.cdp.eval("(function(){return document.title;})()"),
            'inputs': self.cdp.eval(f"(function(){{{js_inputs}}})()"),
            'buttons': self.cdp.eval(f"(function(){{{js_buttons}}})()"),
            'iframes': self.cdp.eval(f"(function(){{{js_frames}}})()"),
        }

    def _diagnose_snapshot(self) -> dict:
        """Parse CDP snapshot to extract all form-relevant elements."""
        try:
            import json as _j
            snap = self.cdp.snapshot()
            data = _j.loads(snap) if isinstance(snap, str) else snap
        except Exception:
            return {}

        # Recursively walk DOM tree to collect elements
        def walk(node, depth=0):
            if depth > 50: return []
            results = []
            tag = node.get('tag', '')
            attr = node.get('attr', {})
            children = node.get('children', [])
            text = ''

            # Collect text from leaf text nodes
            if 'text' in node:
                text = node['text'][:80]

            if tag in ('INPUT','SELECT','TEXTAREA','BUTTON','A','LABEL'):
                results.append({
                    'tag': tag,
                    'id': attr.get('id', ''),
                    'name': attr.get('name', ''),
                    'type': attr.get('type', ''),
                    'placeholder': attr.get('placeholder', ''),
                    'class': (attr.get('class', '') or '')[:80],
                    'text': text,
                    'href': (attr.get('href', '') or '')[:80],
                })

            for child in children:
                results.extend(walk(child, depth + 1))
            return results

        elements = walk(data.get('frame', {}).get('body', {}))
        # Also walk childFrames
        for cf in data.get('childFrames', []):
            body = cf.get('frame', {}).get('body', {})
            elements.extend(walk(body))

        # Categorize
        inputs = [e for e in elements if e['tag'] in ('INPUT','SELECT','TEXTAREA')]
        buttons = [e for e in elements if e['tag'] in ('BUTTON','A') and e['text']]
        return {
            'inputs': inputs[:30],
            'buttons': buttons[:30],
            'total_elements': len(elements),
        }

    def fix(self, config: dict, result: ValidationResult) -> dict:
        """LLM fixes failed JSON based on validation results."""
        if result.passed:
            return config

        # Build failure report
        report_lines = [f"### 验证失败\n"]

        if not result.success_triggered:
            report_lines.append(f"**关键问题: 所有步骤执行完成但成功条件未触发！**")
            report_lines.append(f"  当前URL: {result.final_url}")
            report_lines.append(f"  期望的成功条件: {json.dumps(config.get('success',{}), ensure_ascii=False)}")
            report_lines.append(f"  说明: 可能某个点击/填写没生效，需要换selector或加等待时间")

        if result.failed_steps:
            report_lines.append(f"### 步骤执行失败 ({len(result.failed_steps)}/{result.total_steps})\n")
            for sr in result.failed_steps:
                report_lines.append(f"Step {sr.index}: {sr.action}")
                if sr.find_spec:
                    report_lines.append(f"  find: {json.dumps(sr.find_spec)}")
                report_lines.append(f"  错误: {sr.error}")
                report_lines.append(f"  页面URL: {sr.page_url}")
                report_lines.append("")

        # Always attach diagnostic: what elements ARE available on the page
        diag = self._diagnose_page()
        snap = self._diagnose_snapshot()
        self._last_snapshot = snap.get("inputs", [])
        self._last_buttons = snap.get("buttons", [])
        report_lines.append(f"### 当前页面诊断")
        report_lines.append(f"  URL: {diag['url']}")
        report_lines.append(f"  Title: {diag['title']}")
        report_lines.append(f"  可见输入框: {diag['inputs']}")
        report_lines.append(f"  可见按钮/链接: {diag['buttons']}")
        if diag.get('iframes'):
            report_lines.append(f"  ⚠️ 页面有iframe: {diag['iframes']}")
            report_lines.append(f"     如果主页面找不到元素，可能在iframe里，需要加frame")
        if snap:
            report_lines.append(f"### Snapshot DOM分析 ({snap.get('total_elements',0)} 个元素)")
            report_lines.append(f"  所有输入框: {json.dumps(snap.get('inputs',[]), ensure_ascii=False)}")
            report_lines.append(f"  所有按钮/链接: {json.dumps(snap.get('buttons',[]), ensure_ascii=False)}")
            # Suggest closest matches for failed steps
            if result.failed_steps:
                report_lines.append(f"### 失败步骤的候选匹配")
                for sr in result.failed_steps:
                    find_spec = sr.find_spec
                    report_lines.append(f"  要找: {json.dumps(find_spec)}")
                    # Find best match in snapshot
                    if 'id' in find_spec:
                        matching = [i for i in snap.get('inputs',[]) if find_spec['id'] in (i.get('name',''), i.get('id',''))]
                        if matching:
                            report_lines.append(f"    最接近的输入框: {json.dumps(matching[:3], ensure_ascii=False)}")
                        else:
                            report_lines.append(f"    页面上所有输入框: {json.dumps(snap.get('inputs',[]), ensure_ascii=False)}")
                    elif 'name' in find_spec:
                        matching = [i for i in snap.get('inputs',[]) if find_spec['name'].lower() in i.get('name','').lower() or find_spec['name'].lower() in i.get('id','').lower()]
                        if matching:
                            report_lines.append(f"    最接近的输入框: {json.dumps(matching[:3], ensure_ascii=False)}")
                        else:
                            report_lines.append(f"    页面上所有输入框: {json.dumps(snap.get('inputs',[]), ensure_ascii=False)}")
                    elif 'text' in find_spec:
                        matching = [b for b in snap.get('buttons',[]) if find_spec['text'].lower() in b.get('text','').lower()]
                        if matching:
                            report_lines.append(f"    最接近的按钮: {json.dumps(matching[:3], ensure_ascii=False)}")
                        else:
                            report_lines.append(f"    页面上所有按钮: {json.dumps(snap.get('buttons',[]), ensure_ascii=False)}")

        failure_report = '\n'.join(report_lines)
        original_json = json.dumps(config, indent=2, ensure_ascii=False)

        self.log.info(f"Fix attempt: {len(result.failed_steps)} failures")
        self.log.info(failure_report)

        response = self.llm.chat.completions.create(
            model=self.model,
            messages=[
                {'role': 'system', 'content': self.FIX_PROMPT},
                {'role': 'user', 'content': f"{failure_report}\n\n原始JSON:\n```json\n{original_json}\n```"}
            ],
            temperature=0.2
        )

        content = response.choices[0].message.content.strip()
        if content.startswith('```'):
            lines = content.split('\n')
            content = '\n'.join(lines[1:])
            if content.rstrip().endswith('```'):
                content = content.rstrip()[:-3]
        return json.loads(content)

    # ==================================================================
    # Full Pipeline
    # ==================================================================

    def run(self, description: str, profile: dict,
            navigate_url: str = None) -> Tuple[dict, ValidationResult]:
        """Full generate → validate → fix loop. Returns (final_json, last_result)."""

        # Capture first-page DOM before generating (helps LLM use real element text)
        page_diag = ""
        if navigate_url:
            try:
                self.cdp.eval(f"(function(){{window.location.href='{navigate_url}';}})()")
                time.sleep(2)
                diag = self._diagnose_page()
                snap = self._diagnose_snapshot()
                fields = json.dumps(snap.get("inputs", [])[:10], ensure_ascii=False)
                buttons = json.dumps(snap.get("buttons", [])[:10], ensure_ascii=False)
                page_diag = f"URL: {diag.get('url','')}\nTitle: {diag.get('title','')}\n可见输入框: {fields}\n可见按钮: {buttons}"
                if diag.get('iframes'):
                    page_diag += f"\niframe: {json.dumps(diag['iframes'], ensure_ascii=False)}"
            except Exception:
                pass

        self.log.info("=== Step 1: Generate ===")
        config = self.generate(description, page_diag)
        self.log.info(f"Generated: {len(config.get('steps',[]))} steps")

        max_cycles = 1 if config.get("loop_until") else self.MAX_FIX_CYCLES
        for cycle in range(max_cycles + 1):
            self.log.info(f"=== Step 2: Validate (cycle {cycle}) ===")
            result = self.validate(config, profile,
                                   navigate_url if cycle == 0 else None)

            if result.passed:
                self.log.info(f"PASSED after {cycle} fix cycles")
                self.log.info(f"  Steps: {result.success_steps}/{result.total_steps}")
                self.log.info(f"  URL: {result.final_url[:80]}")
                return config, result

            if cycle < max_cycles:
                self.log.info(f"=== Step 3: Fix (cycle {cycle}) ===")
                config = self.fix(config, result)
                config = self._post_fix(config, result)
            else:
                self.log.warning(f"FAILED after {max_cycles} fix cycles")
                self.log.warning(f"  Failed steps: {len(result.failed_steps)}")

        return config, result


# ==================================================================
# CLI for testing
# ==================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description='JSON pipeline test')
    parser.add_argument('--description', '-d', help='Natural language description (or file)')
    parser.add_argument('--ws-url', required=True, help='WebSocket URL from bit.sh')
    parser.add_argument('--navigate', '-n', help='URL to navigate before running')
    parser.add_argument('--profile', default='{}', help='Profile JSON or file')
    args = parser.parse_args()

    # Setup
    import logging as _log
    _log.basicConfig(level=_log.INFO)

    import sys as _sys, os as _os2
    _sys.path.insert(0, _os2.path.join(_os2.path.dirname(_os2.path.abspath(__file__)), '..', 'forms'))
    from common import CDPHelper
    cdp = CDPHelper(args.ws_url)

    # LLM client
    import os as _os
    from openai import OpenAI
    llm = OpenAI(
        api_key=_os.environ.get('OPENAI_API_KEY'),
        base_url=_os.environ.get('OPENAI_BASE_URL', 'https://api.deepseek.com')
    )

    # Read description
    desc = args.description
    if desc and Path(desc).exists():
        desc = Path(desc).read_text()

    # Read profile
    profile = json.loads(args.profile)
    if isinstance(profile, str) and Path(profile).exists():
        profile = json.loads(Path(profile).read_text())
    profile.setdefault('task_id', 'pipeline_test')

    # Run
    pipeline = JSONPipeline(llm, cdp)
    config, result = pipeline.run(desc or "页面: example.com 类型: newsletter...",
                                  profile, args.navigate)

    print(json.dumps({
        'passed': result.passed,
        'success_steps': result.success_steps,
        'total_steps': result.total_steps,
        'failed_count': len(result.failed_steps),
        'success_triggered': result.success_triggered,
        'final_url': result.final_url,
        'config': config if result.passed else {'_failed': True}
    }, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
