# Agent 稳定化 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 LLM 生成 JSON 后加 Python 硬校验层，拦截结构性错误并在失败时自动重试，确保 JSON 在进入执行前满足最低结构要求。

**Architecture:** 新增 `src/schema_validator.py`（10 条校验规则 + 反例库），修改 `src/json_pipeline.py` 的 generate() 方法加入意图分类→校验→重试循环（最多 2 次重试）。

**Tech Stack:** Python 3, dataclasses, json, re

## Global Constraints

- 校验是同步的，不增加 LLM 调用之外的延迟
- 重试最多 2 次（总共 3 次生成尝试）
- 校验失败的信息要明确、可执行，能直接喂给 LLM 修复
- auto_fixer 在校验之前运行（先打补丁再校验）

---

### Task 1: 创建 `src/schema_validator.py`

**Files:**
- Create: `src/schema_validator.py`

**Interfaces:**
- Produces: `ValidateResult` dataclass, `validate_json(config, description) -> ValidateResult`, `ERROR_EXAMPLES` constant

- [ ] **Step 1: 编写文件**

```python
"""Hard validation rules for LLM-generated JSON configs.

Catches structural errors before execution. Used by json_pipeline.generate()
in a retry loop: validate → fail → hint LLM → regenerate.
"""

from dataclasses import dataclass, field
import re


@dataclass
class ValidateResult:
    """Result of JSON config validation."""
    valid: bool
    errors: list = field(default_factory=list)
    hints: list = field(default_factory=list)


# ── Allowed actions ────────────────────────────────────────────────
ALLOWED_ACTIONS = {"wait", "click", "form", "select", "scroll", "eval", "wait_for", "if", "report"}

# ── eval 伪装检测模式 ──────────────────────────────────────────────
EVAL_MASQUERADE_PATTERNS = [
    # getElementById + .value =  (伪装 form)
    (r"getElementById\(['\"].+?['\"]\)", "禁止用 getElementById+value 代替 form action"),
    # querySelector + .value =  (伪装 form)
    (r"querySelector\(['\"].+?['\"]\)\s*\.\s*value\s*=", "禁止用 querySelector+value 代替 form action"),
    # querySelector + .click()  (伪装 click)
    (r"querySelector\(['\"].+?['\"]\).*\.click\(\)", "禁止用 querySelector+click() 代替 click action"),
]

# ── 反例库 (给 LLM 修复用) ─────────────────────────────────────────
ERROR_EXAMPLES = """## 禁止的写法 vs 正确写法

❌ {"action":"sleep","duration":3000}
✅ {"action":"wait","min":1,"max":3}

❌ {"action":"click","params":{"text":"Next"}}  — params 包裹
✅ {"action":"click","find":{"text":"Next"}}

❌ {"action":"eval","script":"document.getElementById('fn').value='test'"}  — eval 伪装 form
✅ {"action":"form","field":{"id":"fn","type":"text"},"value":"test"}

❌ {"action":"eval","script":"var el=document.querySelector('#btn');el.click()"}  — eval 伪装 click
✅ {"action":"click","find":{"selector":"#btn"}}

❌ {"when":{"field_exists":{"label":"Ohio","type":"button"}}}  — field_exists 不支持 button 类型
✅ 按钮检测用 when:{} 配合 click action

❌ loop_until 存在但 max_rounds<30
✅ "max_rounds":50

❌ 状态机模式 + form 步骤缺 field_exists 条件
✅ form 步骤加 "when":{"field_exists":{"label":"字段","type":"email"}}
"""


def validate_json(config: dict, description: str = "") -> ValidateResult:
    """Validate a JSON config against all hard rules.

    Args:
        config: LLM-generated JSON config (after auto_fixer)
        description: original natural language description (for context checks)

    Returns:
        ValidateResult with errors and hints
    """
    errors = []
    hints = []
    steps = config.get("steps", [])
    has_loop = bool(config.get("loop_until"))
    has_when_keywords = bool("when_" in description)

    # ── Rule 1: action 白名单 ──
    for i, step in enumerate(steps):
        action = step.get("action", "")
        if action not in ALLOWED_ACTIONS:
            errors.append(f"Step {i}: 未知 action '{action}'，允许: {sorted(ALLOWED_ACTIONS)}")
            hints.append(f"Step {i}: 将 action '{action}' 改为允许的类型")

    # ── Rule 2: click 必须有目标 ──
    for i, step in enumerate(steps):
        if step.get("action") == "click":
            find = step.get("find", {})
            if not find or not (find.get("text") or find.get("id") or find.get("selector")):
                errors.append(f"Step {i}: click 缺少 find 目标 (text/id/selector)")
                hints.append(f"Step {i}: 给 click 步骤加 find: {{'text':'按钮文字'}} 或 find: {{'id':'按钮id'}}")

    # ── Rule 3: form 必须有目标 ──
    for i, step in enumerate(steps):
        if step.get("action") == "form":
            field = step.get("field", {})
            find = step.get("find", {})
            if not field and not find:
                errors.append(f"Step {i}: form 缺少 field 或 find")
                hints.append(f"Step {i}: 给 form 步骤加 field: {{'label':'字段名','type':'email'}}")

    # ── Rule 4: eval 仅限 iframe ──
    for i, step in enumerate(steps):
        if step.get("action") == "eval":
            script = step.get("script", "")
            has_frame = step.get("frame_url") or step.get("frame") or "frame" in script.lower()
            if not has_frame:
                # Check if it looks like a form fill masquerade
                if "value" in script or "click()" in script:
                    errors.append(f"Step {i}: eval 只能在 iframe 场景使用，疑似伪装 form/click")
                    hints.append(f"Step {i}: 将 eval 改为 click 或 form action")
                else:
                    errors.append(f"Step {i}: eval 只能在 iframe 场景使用，非 iframe 用 click/form")
                    hints.append(f"Step {i}: 移除 eval，用 click/form 替代")

    # ── Rule 5: 检测 eval 伪装 ──
    for i, step in enumerate(steps):
        if step.get("action") == "eval":
            script = step.get("script", "")
            for pattern, hint in EVAL_MASQUERADE_PATTERNS:
                if re.search(pattern, script):
                    errors.append(f"Step {i}: {hint} (脚本: {script[:60]}...)")
                    hints.append(f"Step {i}: {hint}")
                    break

    # ── Rule 6: wait 参数平铺 ──
    for i, step in enumerate(steps):
        if "params" in step:
            errors.append(f"Step {i}: 参数不能放在 params 内，直接写在步骤顶层")
            hints.append(f"Step {i}: 把 params 里的 min/max/text 提升到步骤顶层")

    # ── Rule 7: max_rounds >= 30 ──
    if has_loop and config.get("max_rounds", 0) < 30:
        errors.append(f"max_rounds={config.get('max_rounds')} 小于 30，状态机可能跑不完")
        hints.append("设置 max_rounds: 50 或更大")

    # ── Rule 8: 随机选项必须用 select ──
    if "随机选" in description or "选项" in description:
        has_select = any(s.get("action") == "select" for s in steps)
        if not has_select:
            errors.append("描述含'随机选选项'但没有 select 步骤")
            hints.append('添加: {"when":{},"action":"select","selection_strategy":{"type":"random"},"optional":true}')

    # ── Rule 9: 状态机 form 步骤要有 field_exists ──
    if has_loop or has_when_keywords:
        for i, step in enumerate(steps):
            if step.get("action") == "form":
                when = step.get("when", {})
                if not when or "field_exists" not in when:
                    errors.append(f"Step {i}: 状态机 form 步骤缺少 when.field_exists")
                    hints.append(f"Step {i}: 添加 when.field_exists 指向同一个 field")

    # ── Rule 10: click 不能含 eval 脚本 ──
    for i, step in enumerate(steps):
        if step.get("action") == "click" and step.get("script"):
            errors.append(f"Step {i}: click 步骤不能含 script 字段")
            hints.append(f"Step {i}: 删除 script 字段，保留 find")

    return ValidateResult(
        valid=len(errors) == 0,
        errors=errors,
        hints=hints,
    )
```

- [ ] **Step 2: 验证语法**

```bash
python3 -c "from src.schema_validator import validate_json, ValidateResult; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: 快速冒烟测试**

```bash
python3 -c "
from src.schema_validator import validate_json

# Good config
good = {'site':'test','steps':[{'action':'wait','min':1,'max':2},{'action':'click','find':{'text':'OK'}}]}
r = validate_json(good)
assert r.valid, f'Expected valid, got: {r.errors}'
print('Good config: valid')

# Bad config
bad = {'site':'test','steps':[{'action':'sleep','duration':3000}]}
r = validate_json(bad)
assert not r.valid
assert 'sleep' in r.errors[0]
print('Bad config: invalid', r.errors)
print('ALL OK')
"
```
Expected: `Good config: valid` + `Bad config: invalid` + `ALL OK`

- [ ] **Step 4: 提交**

```bash
git add src/schema_validator.py
git commit -m "feat: add schema_validator with 10 hard rules"
```

---

### Task 2: 集成到 json_pipeline.py

**Files:**
- Modify: `src/json_pipeline.py` — generate() 方法重写

**Interfaces:**
- Consumes: `validate_json()` from `src/schema_validator.py` (Task 1)
- Produces: 修改后的 `JSONPipeline.generate()` — 分类+校验+重试

- [ ] **Step 1: 修改 generate() 方法**

在 `src/json_pipeline.py` 的 `generate()` 方法中，在 `from auto_fixer import fix; config = fix(config)` 和 `return config` 之间加入校验+重试逻辑。

```python
    def generate(self, description: str) -> dict:
        """LLM generates JSON config from natural language.
        
        Flow: LLM generate → auto_fix → schema_validate → retry if needed.
        """
        from schema_validator import validate_json, ERROR_EXAMPLES
        from auto_fixer import fix

        messages = [
            {'role': 'system', 'content': self.GENERATE_PROMPT},
            {'role': 'user', 'content': description}
        ]

        last_config = None
        for attempt in range(3):
            response = self.llm.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.1 if attempt == 0 else 0.2
            )
            content = response.choices[0].message.content.strip()
            if content.startswith('```'):
                lines = content.split('\n')
                content = '\n'.join(lines[1:])
                if content.rstrip().endswith('```'):
                    content = content.rstrip()[:-3]
            config = json.loads(content)
            config = fix(config)
            last_config = config

            # Schema validation
            result = validate_json(config, description)
            if result.valid:
                return config

            # Not valid — prepare retry
            if attempt < 2:
                self.log.warning(
                    f"Schema validation failed (attempt {attempt + 1}/3): "
                    f"{len(result.errors)} errors"
                )
                retry_prompt = (
                    f"上一轮生成的 JSON 校验不通过，根据以下错误修正：\n\n"
                    f"{chr(10).join('- ' + e for e in result.errors)}\n\n"
                    f"修复建议：\n{chr(10).join('- ' + h for h in result.hints)}\n\n"
                    f"{ERROR_EXAMPLES}\n\n"
                    f"原始描述：\n{description}\n\n"
                    f"上次生成的 JSON：\n{json.dumps(config, indent=2, ensure_ascii=False)}"
                )
                messages.append({'role': 'assistant', 'content': content})
                messages.append({'role': 'user', 'content': retry_prompt})

        # All attempts failed — return last config with warning
        self.log.warning(
            f"Schema validation failed after 3 attempts, "
            f"returning last config with errors"
        )
        return last_config
```

删除原 `generate()` 方法中 LLM 调用后的代码（lines 113-131），替换为上面完整的方法体。

- [ ] **Step 2: 验证语法并运行现有测试**

```bash
python3 -c "import py_compile; py_compile.compile('src/json_pipeline.py', doraise=True); print('Syntax OK')"
python3 test_fixer.py
python3 -m pytest test_diagnostics.py -v 2>&1 | tail -3
```
Expected: `Syntax OK` + `ALL FIXES PASSED` + `17 passed`

- [ ] **Step 3: 提交**

```bash
git add src/json_pipeline.py
git commit -m "feat: add schema validation retry loop to generate()"
```

---

### Task 3: 编写 test_schema_validator.py

**Files:**
- Create: `test_schema_validator.py`

- [ ] **Step 1: 编写测试文件**

```python
"""Schema validator unit tests — one test per rule."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.schema_validator import validate_json, ValidateResult


def test_action_whitelist():
    """Rule 1: 拒绝未知 action。"""
    config = {"steps": [{"action": "sleep", "duration": 3000}]}
    r = validate_json(config)
    assert not r.valid
    assert any("sleep" in e for e in r.errors)


def test_click_needs_find():
    """Rule 2: click 必须有 find 目标。"""
    config = {"steps": [{"action": "click"}]}
    r = validate_json(config)
    assert not r.valid
    assert any("find" in e for e in r.errors)


def test_form_needs_field():
    """Rule 3: form 必须有 field 或 find。"""
    config = {"steps": [{"action": "form"}]}
    r = validate_json(config)
    assert not r.valid
    assert any("field" in e for e in r.errors)


def test_eval_without_iframe():
    """Rule 4: eval 只能在 iframe 场景。"""
    config = {"steps": [{"action": "eval", "script": "document.querySelector('#x').click()"}]}
    r = validate_json(config)
    assert not r.valid


def test_eval_masquerade_form():
    """Rule 5: 检测 eval 伪装 form。"""
    config = {"steps": [{"action": "eval", "script": "document.getElementById('fn').value='test'"}]}
    r = validate_json(config)
    assert not r.valid
    assert any("getElementById" in e or "form" in e.lower() for e in r.errors)


def test_params_not_allowed():
    """Rule 6: params 包裹不被允许。"""
    config = {"steps": [{"action": "wait", "params": {"min": 1, "max": 2}}]}
    r = validate_json(config)
    assert not r.valid
    assert any("params" in e for e in r.errors)


def test_max_rounds_too_low():
    """Rule 7: max_rounds >= 30。"""
    config = {"loop_until": {"url_contains": ["/x"]}, "max_rounds": 20, "steps": []}
    r = validate_json(config)
    assert not r.valid
    assert any("max_rounds" in e for e in r.errors)


def test_missing_select_for_random():
    """Rule 8: 随机选选项必须有 select。"""
    config = {"steps": [{"action": "wait"}]}
    r = validate_json(config, "when_页面有选项: 随机选一个选项")
    assert not r.valid
    assert any("select" in e for e in r.errors)


def test_stateful_form_needs_field_exists():
    """Rule 9: 状态机 form 步骤必须有 field_exists。"""
    config = {
        "loop_until": {"url_contains": ["/x"]},
        "max_rounds": 30,
        "steps": [{"action": "form", "field": {"label": "x", "type": "text"}}]
    }
    r = validate_json(config)
    assert not r.valid
    assert any("field_exists" in e for e in r.errors)


def test_click_should_not_have_script():
    """Rule 10: click 不能含 script。"""
    config = {"steps": [{"action": "click", "script": "eval..."}]}
    r = validate_json(config)
    assert not r.valid


def test_valid_config_passes():
    """正常 config 全部通过。"""
    config = {
        "site": "test",
        "steps": [
            {"action": "wait", "min": 1, "max": 2},
            {"action": "click", "find": {"text": "OK"}},
            {"action": "form", "field": {"label": "email", "type": "email"}, "value": "x@x.com"},
        ]
    }
    r = validate_json(config)
    assert r.valid, f"Expected valid, got: {r.errors}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
```

- [ ] **Step 2: 运行测试**

```bash
python3 -m pytest test_schema_validator.py -v
```
Expected: 11 passed

- [ ] **Step 3: 运行全量测试确认无回归**

```bash
python3 test_fixer.py && python3 -m pytest test_diagnostics.py test_schema_validator.py -v 2>&1 | tail -5
```
Expected: `ALL FIXES PASSED` + `28 passed`

- [ ] **Step 4: 提交**

```bash
git add test_schema_validator.py
git commit -m "test: add schema_validator tests (11 cases)"
```

---

### Task 4: 验证集成效果 — 重跑 datewhirl

**Files:**
- 无新建文件 — 验证步骤

- [ ] **Step 1: 运行 datewhirl 集成测试**

```bash
WS_URL=ws://127.0.0.1:9222/... OPENAI_API_KEY=sk-xxx \
python3 -m pytest test_integration.py::test_datewhirl -v -s
```

Expected: PASSED（或至少有明显改善的校验日志）

- [ ] **Step 2: 检查校验日志**

查看是否有 schema validation retry 日志输出，确认校验生效。
