# RadioStrategy — 通用 Radio Group 选择策略（v4.1 implementation-ready）

## Context

当前 radio 选择分散在三条路径中，各自有确定性问题：

| 路径 | 位置 | 问题 |
|------|------|------|
| `_smart_form` radio/chip 分支 | `json_executor.py` ~L1051-1114 | 指定选项降级 random；parentElement 随机搜 chip 作用域不可靠；不验证 checked |
| `_select_option()` | `json_executor.py` L451-631 | 两处 `"checked" in "not checked"` 假成功；全局 random 不验证 radio state |
| `_try_quiz_group()` | `json_executor.py` L972-1007 | 仅 scoped quiz，不支持精确选项 |
| Shared verifier | `select_explorer.py` L227 | 已修（精确比较）；但 ProbeSession 不记录 marker frame_id，SelectExplorer 后续操作丢失 frame_id |
| CDP click | `common.py` | `click()` 不检查返回码、不抛点击错误，无法可靠产生 CLICK_FAILED |

CTM 的 "Annual Income Range → $30k-$60k"、 "Do you have existing cover? → No" 是标准 `<label><input type="radio">text</label>`，当前全部失败。

---

## 一、核心架构：Strategy Probe + Origin-Aware Routing

**关键设计决策**：
- 本轮 canonical `select_option` **仅支持 radio**；dropdown 继续走 `form+select`
- RadioStrategy 正向命中后短路旧逻辑；**不删除** `_smart_form` 旧分支（等其他 Strategy 迁完再删）

### 1.1 Execution Context（统一接口入口）

```python
@dataclass
class ExecutionContext:
    cdp: CDPHelper
    locator: FieldLocator
    frame_id: str
    log: logging.Logger
    marker_session: MarkerSession  # per-request unique token, tracks markers per frame
```

### 1.2 Strategy Probe 接口（解决 classify↔locate 循环依赖）

每个 Strategy 先产生结构化 Probe，ChoiceExplorer 再基于所有 Probe 分类。所有方法消费同一 context：

```python
@dataclass
class StrategyProbe:
    kind: str              # "native_radio" | "aria_radio" | "native_select" | "combobox"
    candidates: list       # radio: list[GroupRef]; dropdown: list[CandidateRef]
    frame_id: str
    confidence: float      # 0.0-1.0, 基于 DOM 正向证据
    evidence: dict

@dataclass
class GroupRef:
    """A discovered radio group — logical reference, not a marker selector.
    Probe does NOT modify DOM. Unique markers are injected later by execute()."""
    group_type: str        # "native_radio" | "aria_radio"
    name: str              # native: input.name; ARIA: group label
    owner_form_id: str     # native: closest form id (grouping key)
    scope_selector: str    # CSS selector for the minimal container (structural, no data- attr)
    options: list[OptionRef]
    frame_id: str
    conflicting_control_types: list[str]  # e.g. ["checkbox"] within same field scope

@dataclass
class OptionRef:
    text: str              # accessible name
    value: str             # input.value or aria value
    index: int             # 0-based index within group (for execute() re-resolution)
    checked: bool
    enabled: bool
    visible: bool
    # Activation target resolved at execute() time, not probe time

class RadioStrategy:
    @staticmethod
    def probe(intent: ChoiceIntent, ctx: ExecutionContext) -> StrategyProbe | None:
        """Single JS eval — scan for radio groups. Returns None if:
        - No visible enabled radio groups found anywhere on page
        - Radio groups exist but NONE can be associated with this intent:
          * field.label doesn't match any group's heading/label
          * exact option text doesn't uniquely reverse-match any group
          * field={} and random but page has multiple visible groups
        - The matched field scope also contains conflicting controls (checkbox)
          → return StrategyProbe with conflicting_control_types signal
        
        Read-only. Does NOT modify DOM. Does NOT call FieldLocator.
        Returns logical refs, not marker selectors."""

    @staticmethod
    def _associate_groups_with_intent(
        groups: list[GroupRef],
        intent: ChoiceIntent,
    ) -> list[GroupRef] | str:
        """Return matched groups, or a reason string for non-match.
        
        Scoring:
        1. field.label matches group label/legend/heading → matched
        2. field={} + exact: option text uniquely present in one group → option_unique_reverse
        3. field={} + random: only one visible group on page → single_visible_group
        4. Otherwise → empty list (caller returns None → DEFER_LEGACY)
        """

class DropdownStrategy:
    @staticmethod
    def probe(intent: ChoiceIntent, ctx: ExecutionContext) -> StrategyProbe | None:
        """Uses ctx.locator to find CandidateRef for native <select>/[role=combobox].
        Returns None if no candidates."""
        ...

    @staticmethod
    def execute(intent: ChoiceIntent, probe: StrategyProbe, ctx: ExecutionContext) -> SelectOutcome:
        """Delegates to SelectExplorer with probe's pre-discovered candidates."""
        ...
```

### 1.3 ChoiceExplorer 分发流程（只仲裁，不亲自执行 legacy）

```python
class ChoiceResult(Enum):
    HANDLED = "handled"           # Strategy executed, outcome returned
    DEFER_LEGACY = "defer_legacy" # RadioStrategy not applicable, caller continues old path
    AMBIGUOUS = "ambiguous"       # Multiple conflicting probes, zero click
    INVALID = "invalid"           # Malformed intent (parse error)
    UNSUPPORTED = "unsupported"   # No matching probe, canonical origin

class ChoiceExplorer:
    def execute(self, request: NormalizedChoice, ctx: ExecutionContext) -> tuple[ChoiceResult, SelectOutcome | None]:
        # Step 1: strict validation
        if not self._validate_intent(request.intent):
            return (ChoiceResult.INVALID, SelectOutcome(status=INVALID_INTENT, ok=False))

        # Step 2: collect Probes (only RadioStrategy this round)
        probes = []
        rp = RadioStrategy.probe(request.intent, ctx)
        if rp:
            probes.append(rp)

        # Step 3: arbitrate
        if len(probes) == 0:
            if request.origin == "canonical":
                return (ChoiceResult.UNSUPPORTED, SelectOutcome(status=UNSUPPORTED_CONTROL))
            else:
                return (ChoiceResult.DEFER_LEGACY, None)

        probe = probes[0]

        # Conflicting controls within matched field scope → AMBIGUOUS_CONTROL
        for group in probe.candidates:
            if group.conflicting_control_types:
                return (ChoiceResult.AMBIGUOUS,
                        SelectOutcome(status=AMBIGUOUS_CONTROL,
                                      evidence={"conflicting_controls": group.conflicting_control_types,
                                                "scope": group.scope_selector}))

        # Step 4: execute the single matching strategy
        outcome = RadioStrategy.execute(request.intent, probe, ctx)
        ctx.marker_session.cleanup_all_frames()
        return (ChoiceResult.HANDLED, outcome)

    def _validate_intent(self, intent: ChoiceIntent) -> bool:
        """Discriminated union validation — checks raw step BEFORE constructing ChoiceIntent."""
        if not isinstance(intent, ChoiceIntent):
            return False
        if intent.mode == "exact":
            return isinstance(intent.option, str) and bool(intent.option.strip())
        elif intent.mode == "random":
            return intent.option is None
        return False  # unknown mode


def parse_and_validate_choice(step: dict) -> ChoiceIntent | None:
    """Validate raw step dict before constructing ChoiceIntent.
    Returns None if malformed (caller produces INVALID_INTENT outcome).
    """
    if not isinstance(step, dict):
        return None
    opt = step.get("option")
    if not isinstance(opt, dict):
        return None
    field = step.get("field")
    if field is not None and not isinstance(field, dict):
        return None
    mode = opt.get("mode", "")
    if mode not in ("exact", "random"):
        return None
    text = opt.get("text")
    if mode == "exact":
        if not isinstance(text, str) or not text.strip():
            return None
    elif mode == "random":
        if "text" in opt:
            return None  # random MUST NOT carry text
    return ChoiceIntent(
        label=(field or {}).get("label", "") if field else "",
        mode=mode,
        option=text if mode == "exact" else None,
    )
```

`normalize_choice_request` 调用 `parse_and_validate_choice`；返回 None 时 ChoiceExplorer 返回 `(INVALID, SelectOutcome(status=INVALID_INTENT))`。

**同时修复** `SelectOutcome`：`ok` 是只读 `@property`，移除所有手动传入的 `ok=False`。`INVALID_INTENT` 加入 status 全集。

### 1.4 路由表（_execute_step 中的固定分支）

```
step.action == "select_option":
    request = normalize_choice_request(step)     # origin=canonical
    result, outcome = ChoiceExplorer.execute(request, ctx)
    if result == ChoiceResult.HANDLED:
        return outcome.ok        # success or fail — no fallback
    elif result == ChoiceResult.UNSUPPORTED:
        return False             # canonical unsupported → fail closed
    elif result == ChoiceResult.INVALID:
        return False             # malformed protocol → fail closed
    elif result == ChoiceResult.AMBIGUOUS:
        return False             # ambiguous → fail closed

step.action == "form" and "select" in step:
    request = normalize_choice_request(step)     # origin=legacy
    result, outcome = ChoiceExplorer.execute(request, ctx)
    if result == ChoiceResult.HANDLED:
        return outcome.ok        # RadioStrategy handled it
    elif result == ChoiceResult.DEFER_LEGACY:
        pass  # fall through to existing form handler below
    elif result == ChoiceResult.AMBIGUOUS:
        return False             # ambiguous → fail closed
    elif result == ChoiceResult.INVALID:
        pass  # fall through to legacy (old protocol may still work)
    # Continue to existing form handler (FieldLocator → _smart_form)
    ...

step.action == "select":
    strategy = step.get("selection_strategy", {}).get("type", "")
    if strategy == "random":
        request = normalize_choice_request(step)   # origin=legacy
        # Skip probe if container / control_types present (quiz/Q2 legacy)
        if not step.get("container") and not step.get("control_types"):
            result, outcome = ChoiceExplorer.execute(request, ctx)
            if result == ChoiceResult.HANDLED:
                return outcome.ok
            elif result == ChoiceResult.DEFER_LEGACY:
                pass  # fall through to _select_option
    # first / match_text / container / control_types → never probe, stay legacy
    # Continue to existing _select_option(step)
    ...
```

### 1.5 Request Origin 模型

```python
@dataclass
class NormalizedChoice:
    intent: ChoiceIntent
    origin: str           # "canonical" | "legacy"
    original_step: dict   # legacy 时保存原始 step，canonical 时为 None

def normalize_choice_request(step: dict) -> NormalizedChoice | None:
    action = step.get("action", "")
    if action == "select_option":
        # Canonical — new protocol. Strict parse: fail on malformed.
        opt = step.get("option", {})
        mode = opt.get("mode", "")
        text = opt.get("text") if mode == "exact" else None
        return NormalizedChoice(
            intent=ChoiceIntent(
                label=step.get("field", {}).get("label", ""),
                mode=mode,
                option=text,
            ),
            origin="canonical",
            original_step=None,
        )
    elif action == "form" and "select" in step:
        # Legacy — old form+select. Only probe RadioStrategy; if not radio,
        # DEFER_LEGACY continues to existing form handler (FieldLocator → _smart_form).
        sel = step["select"]
        if sel == "__random__":
            return NormalizedChoice(
                intent=ChoiceIntent(
                    label=step.get("field", {}).get("label", ""),
                    mode="random",
                    option=None,
                ),
                origin="legacy",
                original_step=step,
            )
        else:
            return NormalizedChoice(
                intent=ChoiceIntent(
                    label=step.get("field", {}).get("label", ""),
                    mode="exact",
                    option=sel,
                ),
                origin="legacy",
                original_step=step,
            )
    elif action == "select":
        # Legacy — action=select. Only "random" strategy probes RadioStrategy.
        # "first" / "match_text" / container / control_types → never probe, stay legacy.
        strategy_type = step.get("selection_strategy", {}).get("type", "")
        if strategy_type == "random":
            return NormalizedChoice(
                intent=ChoiceIntent(label="", mode="random", option=None),
                origin="legacy",
                original_step=step,
            )
        return None  # non-random → skip ChoiceExplorer entirely
    return None  # not a choice step
```

**回退规则**（硬编码，不可配置）：
- canonical + UNSUPPORTED/INVALID/AMBIGUOUS → 失败关死，零点击
- canonical + RadioStrategy 执行后任何失败 → 禁止 legacy fallback
- legacy + HANDLED → 不再走旧路径
- legacy + DEFER_LEGACY → 继续原 form handler / _select_option
- legacy + AMBIGUOUS → 零点击，失败（不 fallback）
- legacy + INVALID → fall through legacy（旧协议可能仍可工作）
- 歧义（任何 origin）→ 零点击，禁止 fallback

### 1.6 RadioStrategy.execute(intent, probe, ctx)

```
Precondition: probe already confirmed that intent is associated with specific groups,
and groups have no conflicting_control_types (checked in arbitrate).

1. groups = probe.candidates  (list[GroupRef] — pre-discovered logical refs)

2. Inject unique markers into the confirmed group scope ONLY:
     ctx.marker_session.mark_scope(probe.candidates[0].scope_selector)
     → execute re-resolves options within this scope by index
     → does NOT re-scan the full page

3. group = score_and_select_group(groups, field_label, option_text)
     评分: option_text 在组内 > legend/aria-labelledby > DOM 距离 > name token
     并列 → AMBIGUOUS_GROUP，零点击

4. target = match_option(group, option_text, mode)
     normalized_exact: 空白/NBSP/大小写/连字符 → 全文相等
     random: 随机未选中项
     不匹配 → OPTION_NOT_FOUND
     random 已有任何选择 → ALREADY_SELECTED（不重新随机）

5. read selected_before

6. 已选中 → ALREADY_SELECTED

7. resolve activation target: input / 祖先 label / label[for]
     → unique markers for click target + verify control

8. result = click_checked(cdp, click_marker, ctx.frame_id)
     → CLICK_FAILED if click fails

9. poll verify predicate:
     native: target input.checked === true + 同组唯一 checked
     ARIA: aria-checked === "true"
     element detached → NOT_VERIFIED (reason=element_detached)
     超时 → NOT_VERIFIED (reason=timeout)

10. finally: ctx.marker_session.cleanup_all_frames()

11. return SelectOutcome(status, evidence)
```
**Radio 语义硬编码规则**：
- 只发现当前可见且 enabled 的组
- 隐藏 native input + 可见关联 label → 候选
- role wrapper + 隐藏 native input → 去重为一个逻辑选项
- Native: target radio checked + 同组仅一个 checked
- Random + 已有任何选择 → ALREADY_SELECTED（不等全部选完）
- field={}: exact 唯一反查；random 唯一 visible group
- 多个 Yes/No 组：仅靠 "No" 不能猜组
- DOM remount（元素 detached）→ shared verifier 检测 → NOT_VERIFIED

---

## 二、Shared Verifier + CDP Click 修复

### 2.1 当前假阳性

```python
# _select_option() 两处
"checked" in "not checked"     # → True  ← 错误（保留 legacy，本轮不改 _select_option）

# select_explorer.py L227
# 已修：精确比较，不再 substring
```

### 2.2 Structured VerifyResult

```python
@dataclass
class VerifyResult:
    verified: bool
    reason: str          # "state_matched" | "state_unchanged" | "element_detached" | "timeout" | "cdp_error"
    elapsed_ms: int
    evidence: dict       # {before, after, signal_type, signal_value}
```

**Marker 规则**：
- click target marker 与 verify control marker 使用不同的属性名（防串）
- marker value 携带 frame_id token
- 每个 frame 独立分配 token
- finally 块逐 frame 清理，不留残留

### 2.3 CDP Click 底层修复（common.py — 直接修改 CDPHelper）

**不能在包装层绕过** — `CDPHelper.click()` 当前签名 `click(selector, frame_id="")` 没有 timeout 参数，不检查 subprocess returncode，丢掉 stderr。所有包装都会因 TypeError 或静默失败。

**必须直接修改 CDPHelper.click()**：

```python
# common.py — CDPHelper.click() 修改后签名
def click(self, selector: str, frame_id: str = "", timeout_ms: int = 3000) -> ClickResult:
    """Structured CDP click returning ClickResult.
    
    Checks: subprocess returncode, timeout, stderr content.
    Does NOT silently swallow failures.
    Returns ClickResult(success, error_code, error_detail).
    """
    # 1. Resolve element coordinates via DOM.getBoundingClientRect
    # 2. Dispatch Input.dispatchMouseEvent
    # 3. Check subprocess returncode != 0 → CLICK_FAILED
    # 4. Check timeout → CLICK_FAILED
    # 5. Check stderr for CDP error → CLICK_FAILED
    # 6. DOM detached is NOT a click failure — it's detected by verifier as NOT_VERIFIED
```

**同时立即修复** `json_executor.py` 两处现有假成功（不保留 FIXME）：
- L512: `"checked" in "not checked"` → 改为 `json.loads(raw)["checked"] is True`
- L542: 同上

两处都在 `_select_option()` 的 quiz-scoped / control_types 路径。

### 2.4 ProbeSession — frame_id 跟踪

`select_explorer.py` 的 `ProbeSession` 需记录每个 marker 属于哪个 frame，`cleanup()` 逐 frame 清理。本条本轮可仅保留 open issue，不影响 RadioStrategy（RadioStrategy 自己管理 marker）。

---

## 三、SelectOutcome 模型（统一）

### 3.1 Status 全集

```
SELECTED            — selected_before=false, selected_after=true
ALREADY_SELECTED    — selected_before=true, 不重复点击
FIELD_NOT_FOUND     — 问题文字找不到，且无法通过选项反推
GROUP_NOT_FOUND     — 区域内无可见 enabled radio group
OPTION_NOT_FOUND    — 指定 option.text 在 group 内不匹配
AMBIGUOUS_GROUP     — 多个 group 同分，无法确定
AMBIGUOUS_CONTROL   — 多个 control type probe 并存且无法区分
CLICK_FAILED        — CDP 点击未发出（safe_click 返回 error）
NOT_VERIFIED        — 点击已发出，超时后状态未变化或元素 detached
NO_CANDIDATE        — 旧 Explorer 状态，映射保留
NO_SAFE_TRIGGER     — 旧 Explorer 状态，映射保留
OPEN_FAILED         — 旧 Explorer 状态，映射保留
UNSUPPORTED_CONTROL — canonical: 无匹配 Strategy Probe
```

### 3.2 Evidence

```json
{
  "field_text": "Annual Income Range",
  "group_name": "health_healthCover_income",
  "group_type": "native_radio",
  "option_text": "$30k-$60k",
  "input_value": "1",
  "selected_before": false,
  "selected_after": true,
  "verification_signal": "checked",
  "activation_target": "label",
  "discovery_method": "field_scope",
  "option_match": "normalized_exact"
}
```

- `ok` 仅由 status 派生：`ok = status in {SELECTED, ALREADY_SELECTED}`
- 所有歧义状态零点击

---

## 四、交付范围与 Legacy 保留

| RadioStrategy 短路（本轮新增） | Legacy 保留（本轮不动） |
|---|---|
| `input[type=radio]` — native radio group | `.chip` / `.pic` — 视觉 chip 卡片 |
| `[role=radio]` — ARIA radio group | `[data-value]` — 自定义卡片 |
| label[for] 关联 radio | rating/star 组件 |
| 隐藏 native input + 可见 label | 图片卡 / 伪 radio（div 模拟） |
| | checkbox（留待 CheckboxStrategy） |
| | Native `<select>` random |
| | `_select_option()` 全局 random |
| | `_try_quiz_group()` 非 radio 路径 |
| | `quiz_loop()` |
| | `_smart_form` 全部旧分支（不删，只短路） |

**不删除旧分支**。RadioStrategy 正向命中后 `return`，旧代码不执行。

---

## 五、Prompt + 诊断数据

### 5.1 新增结构化诊断 `choice_groups`

在 `json_pipeline.py` `_diagnose_snapshot()` 或独立方法中新增：

```python
def _diagnose_choice_groups(cdp, frame_id="") -> list[dict]:
    """Scan page for radio groups with their accessible labels and options.

    Handles CTM-like DOM where question title is a plain <label> without 'for',
    inside a .form-section container — NOT a <legend> or label[for].
    """
    js = """(function(){
      var groups = [];
      var seen = {};
      var radios = document.querySelectorAll('input[type=radio]');
      radios.forEach(function(r){
        if (r.disabled || r.closest('[aria-hidden=true]')) return;
        // Group by (form owner, name) — not just name
        var form = r.closest('form');
        var ownerId = form ? (form.id || form.getAttribute('data-form-id') || '') : '';
        var key = ownerId + '::' + (r.name || '');
        if (!key || seen[key]) return;
        seen[key] = true;

        // Find question title: text node / label WITHOUT nested input in the
        // minimal semantic container (.form-section, fieldset, .question, etc.)
        var container = r.closest(
          '[class*=form-section],[class*=question],[class*=field],fieldset,.form-group,.form-row');
        var heading = '';
        if (container) {
          // Walk direct children / labels looking for text that doesn't contain an input
          var children = container.querySelectorAll('label,span,p,h3,h4,.label,.title,.heading');
          for (var c=0;c<children.length;c++){
            var inp = children[c].querySelector('input,select,textarea');
            if (!inp && children[c].offsetWidth > 0) {
              heading = children[c].textContent.trim();
              if (heading.length >= 2) break;
            }
          }
          // Fallback: legend
          if (!heading) {
            var leg = container.querySelector('legend');
            heading = leg ? leg.textContent.trim() : '';
          }
          // Last resort: aria-labelledby
          if (!heading) {
            var labelledById = container.getAttribute('aria-labelledby');
            if (labelledById) {
              var ref = document.getElementById(labelledById);
              heading = ref ? ref.textContent.trim() : '';
            }
          }
        }

        // Collect options
        var options = [];
        var selector = 'input[type=radio][name="'+r.name+'"]';
        var siblings = (form||document).querySelectorAll(selector);
        siblings.forEach(function(s){
          if (s.closest('[aria-hidden=true]')) return;  // exclude hidden wizard steps
          var optLabel = s.closest('label');
          var optText = '';
          if (optLabel) {
            optText = optLabel.textContent.replace(/\\s+/g,' ').trim();
          } else if (s.id) {
            var forLabel = document.querySelector('label[for="'+s.id+'"]');
            optText = forLabel ? forLabel.textContent.replace(/\\s+/g,' ').trim() : '';
          }
          if (!optText && s.value) optText = s.value;
          options.push({
            text: optText,
            value: s.value,
            checked: s.checked
          });
        });

        groups.push({
          label: heading,
          type: 'native_radio',
          name: r.name,
          owner: ownerId,
          frame_id: '',  // filled by caller if in iframe
          options: options
        });
      });
      return JSON.stringify(groups.slice(0, 10));
    })()"""
    raw = cdp.eval(js, frame_id)
    try:
        result = json.loads(raw) if isinstance(raw, str) else raw
        # Tag frame_id
        for g in result:
            g["frame_id"] = frame_id
        return result
    except:
        return []
```

**诊断输出格式**（追加到 page_diag 末尾）：
```
选择组:
[{"label":"Annual Income Range","type":"native_radio","name":"healthCover_income",
  "owner":"","options":[
    {"text":"$30k-$60k","value":"1","checked":false},
    {"text":"$60k-$100k","value":"2","checked":false}
  ]}]
```

**CTM-like diagnostic 单测**：使用包含 `.form-section > label`（无 for）+ radio input 的 fixture HTML 验证 heading 提取正确。

### 5.2 GENERATE_PROMPT 变更

**删除**：
- `radio/按钮/选项类选择，直接用 click + find.text，不要用 form + select！`
- `只有真正的 <select> 下拉框才用 form + select`

**新增 select_option 章节**：
```
### 语义选择 (select_option)

当运营写"选择问题 XXX(选YYY)"（明确的单选题）时, 生成:
{
  "action": "select_option",
  "field": {"label": "问题文字"},
  "option": {"mode": "exact", "text": "选项文字"}
}

随机选择:
{
  "action": "select_option",
  "field": {"label": "问题文字"},
  "option": {"mode": "random"}
}

规则:
- field.label 和 option.text 来源优先级:
  1. choice_groups 中对应组/选项的原文（首选）
  2. 运营描述中明确的原文（choice_groups 无此组时，如后续 wizard 页尚未出现在快照中）
  3. field={}（两步都不适用时）
  始终禁止翻译、改写、补充或修改标点/货币符号/数值范围
- 没有可靠问题文字时 "field":{}
- 本动作支持: radio group
- 不支持: checkbox、卡片选择、星级评分、下拉框
  （这些继续用 form+check / form+select）
```

**更新动作速查表**：`选择XXX（选YYY）` → `select_option`
**更新可用 action 白名单**：添加 `select_option`
**quiz 章节**：保留 `action: "select"` + `selection_strategy`，不改
**状态机示例**：保留，不改

### 5.3 FIX_PROMPT

新增 select_option 修复指引：option.text 不匹配时检查 choice_groups 中的真实选项文字。

### 5.4 _post_fix (json_pipeline.py)

```python
has_choice = any(
    step.get("action") in {"select", "select_option"}
    for step in steps
)
# 有 select 或 select_option 时不自动插入旧全局 random select
```

### 5.5 auto_fixer.py

L47: `field.type` 自动补全跳过 `action="select_option"`。

---

## 六、硬约束

- exact 失败 → OPTION_NOT_FOUND，不退化 random
- 多 Probe/多 group 同分 → AMBIGUOUS，零点击，零 fallback
- `selected_after ≠ true` → 不能返回 SELECTED
- random 仅由 `option.mode="random"` 触发（或 legacy 经 classify 确认 radio 后）
- ALREADY_SELECTED 不重复点击
- `ok` 仅由 status 派生
- canonical unsupported → UNSUPPORTED_CONTROL + 失败，不回退 legacy
- RadioStrategy 执行后任何失败 → 不回退 legacy
- 所有歧义状态零点击

---

## 七、文件变更清单

| # | 文件 | 变更 |
|---|------|------|
| 1 | `src/select_explorer.py` | 新增 `StrategyProbe`、`NormalizedChoice`、`RadioStrategy`（含 `.probe()` + `.execute()`）、`DropdownStrategy.probe()`、`ChoiceExplorer`（probe→arbitrate→dispatch）；扩展 `SelectOutcome`；`VerifyResult` |
| 2 | `src/json_executor.py` | `_execute_step()`: 新增 `_normalize_choice_request()` + `action="select_option"` 路由（在 form 分支前）；RadioStrategy 正向命中短路 `_smart_form`（不删旧代码）；立即修复 L512/L542 `"checked" in "not checked"` 假阳性 → `json.loads(raw)["checked"] is True` |
| 3 | `src/json_pipeline.py` | 新增 `_diagnose_choice_groups()`；GENERATE_PROMPT: 删除 radio→click 规则，新增 select_option 章节，更新动作表、白名单；FIX_PROMPT: select_option 修复指引；`_post_fix`: 精确 `has_choice` 逻辑 |
| 4 | `src/auto_fixer.py` | L47: 跳过 `select_option` 步骤的 type 补全 |
| 5 | `src/common.py` | 直接修改 `CDPHelper.click()`：新增 `timeout_ms` 参数、检查 subprocess returncode/stdout/stderr、返回 `ClickResult` 结构体 |
| 6 | `tests/conftest.py` | **新增** — pytest fixtures: CDP mock, frame helpers, test page loaders |
| 7 | `tests/fixtures/radio-groups.html` | **新增** — 独立 radio 测试页（native/ARIA/label-for/hidden/disabled/YesNo×2/iframe） |
| 8 | `tests/test_choice_explorer.py` | **新增** — U1-U25 单元测试（含 parser、probe、execute、diagnostic） |
| 9 | `tests/test_select_option_compat.py` | **新增** — 5 个兼容测试 |
| 10 | `tests/test_select_option_pipeline.py` | **新增** — Prompt/_post_fix/auto_fixer 集成测试 |
| 11 | `tests/test_radio_strategy_ctm.py` | **新增** — CTM 集成测试，使用 `MOCK_BASE_URL`/`WS_URL` 环境变量，每例 reload/reset |
| 12 | `requirements-dev.txt` | **新增** — pytest 及相关依赖 |

**不改变**：`locator.py`、`element_finder.py`、`wizard_explorer.py`、`_smart_form` 旧分支

---

## 八、验收用例

### 测试环境约束

- DOM 选择测试（U1-U18, U21）需要真实浏览器/CDP；普通 mock 无法执行注入 JS
- 使用 `tests/fixtures/radio-groups.html` 独立加载每个场景，避免同一 fixture 多 radio 组互相干扰
- 所有 CDP 测试共享一个浏览器 session，但每个用例执行前 reload 页面 + cleanup markers
- 固定 JSON 单步测试**不能**用 `JSONExecutor.run()` 的最终布尔值 — 需暴露 `execute_choice_step()` 或读取 `last_outcome`

```python
# JSONExecutor 新增公开方法
def execute_choice_step(self, step: dict) -> SelectOutcome:
    """Execute a single select_option step and return the structured outcome.
    Does NOT go through the full run() pipeline — callers assert on status/evidence."""
    ...
```

### 调试 Flag — 假成功立即修复

两个 `_select_option()` 中的 `"checked" in "not checked"` 假成功**本轮立即修**：

- `json_executor.py` L512: JS verify 改为返回 `{"checked": true/false}`，Python 侧 `json.loads()` 后精确比较 `result["checked"] is True`
- `json_executor.py` L542: 同上

不保留 FIXME，与 RadioStrategy 同批次提交。

### 单元测试 (tests/test_choice_explorer.py)

使用 `tests/fixtures/radio-groups.html`：

| ID | 用例 | 期望 status |
|----|------|-------------|
| U1 | native radio exact 匹配 | SELECTED, discovery=field_scope, match=normalized_exact |
| U2 | native radio exact 不匹配 | OPTION_NOT_FOUND, 选择前后 DOM 状态完全不变 |
| U3 | native radio random | SELECTED |
| U4 | native radio random 已有选择 | ALREADY_SELECTED |
| U5 | ARIA radio exact 匹配 | SELECTED, verification_signal=aria_checked |
| U6 | label[for] 关联 radio | SELECTED, activation_target=label_for |
| U7 | 隐藏 native input + 可见 label | SELECTED |
| U8 | role wrapper + 隐藏 native input 去重 | SELECTED，去重为一个逻辑选项 |
| U9 | 两个 Yes/No 组, option.text="No" | AMBIGUOUS_GROUP, 零点击 |
| U10 | field={} + 选项唯一反查 | SELECTED, discovery=option_unique_reverse |
| U11 | field={} + 多个 group 含此选项 | AMBIGUOUS_GROUP |
| U12 | field={} + random + 唯一 group | SELECTED |
| U13 | field={} + random + 多个 group | AMBIGUOUS_GROUP |
| U14 | disabled radio group — no enabled groups on page | Probe returns None → DEFER_LEGACY（legacy 路径自行处理）或 canonical 下 UNSUPPORTED_CONTROL |
| U15 | hidden wizard step radio — 不可见组 | Probe 排除 hidden，同 U14 |
| U16 | 点击后 checked 不变 | NOT_VERIFIED, reason=state_unchanged |
| U17 | CDP click 报错 | CLICK_FAILED |
| U18 | iframe 内 radio | SELECTED, frame_id 全链传递 |
| U19 | canonical select_option + 仅 checkbox | UNSUPPORTED_CONTROL, 零点击, 不回退 |
| U20 | canonical select_option + 仅 .chip 卡片 | UNSUPPORTED_CONTROL, 零点击, 不回退 |
| U21 | 同一 field scope 内 radio + checkbox 并存 | AMBIGUOUS_CONTROL, 零点击（冲突控件检测限定同一最小语义容器，不扫描页面其他位置的 consent checkbox） |
| U22 | exact 模式缺 text（`option.mode=exact, 无text字段`） | INVALID_INTENT, 零 DOM 探测 |
| U23 | random 模式带了 text（`option.mode=random, text="$30k"`） | INVALID_INTENT, 零 DOM 探测 |
| U24 | 未知 mode（`option.mode="fuzzy"`） | INVALID_INTENT, 零 DOM 探测 |
| U25 | CTM-like diagnostic: .form-section > label(无for) 标题提取 | choice_groups label = "Annual Income Range"，非空，非选项文字 |

### 兼容测试 (tests/test_select_option_compat.py)

| ID | 用例 | 期望 |
|----|------|------|
| C1 | legacy form+select `__random__` + DOM 确认 radio | normalize(legacy) → RadioStrategy.probe matches → ChoiceExplorer → HANDLED → SELECTED |
| C2 | legacy form+select `__random__` + DOM 是 native `<select>`（无 radio） | RadioStrategy.probe returns None → DEFER_LEGACY → 原 form handler → FieldLocator → SelectExplorer |
| C3 | legacy form+select `"No"` (exact) + DOM 确认 radio | normalize(legacy, exact) → RadioStrategy.probe matches → HANDLED → SELECTED |
| C4 | legacy action=select random + DOM 确认 radio | normalize(legacy) → RadioStrategy.probe matches → HANDLED → SELECTED |
| C5 | legacy action=select random + DOM 仅 checkbox（无 radio） | RadioStrategy.probe returns None → DEFER_LEGACY → `_select_option()` 原路径 |
| C6 | 连续两题 marker 不串（Q1→Q2） | Q2 不受 Q1 残留 marker 影响；marker_session.cleanup_all_frames 在每次 execute 的 finally 中执行 |
| C7 | RadioStrategy.probe 后 HANDLED → 不回退 legacy | `_execute_step` 在 HANDLED 后直接 return，不执行 `_smart_form` |
| C8 | 页面有无关 radio（Gender 组），但目标是 legacy form+select Country 下拉框 | RadioStrategy.probe 发现 radio 但与 Country intent 无关联 → returns None → DEFER_LEGACY → 原 form handler |

### Pipeline 测试 (tests/test_select_option_pipeline.py)

| ID | 用例 | 期望 |
|----|------|------|
| P1 | LLM 对"选择问题 XXX(选YYY)"生成 select_option | action=select_option, option.mode=exact |
| P2 | LLM 对"选随机"生成 select_option random | option.mode=random |
| P3 | LLM 保留 linear/no-translate 规则 | 编号步骤仍用线性模式，不翻译 |
| P4 | _post_fix 有 select_option 时不插入全局 random select | has_choice=True, 不触发补丁 |
| P5 | auto_fixer 跳过 select_option 的 type 补全 | field 无 type 字段 |

### 集成测试 (tests/test_radio_strategy_ctm.py)

使用 `MOCK_BASE_URL` / `WS_URL` / `CDP_PATH` 环境变量，每例 reload/reset：

| ID | 用例 | 断言 |
|----|------|------|
| I1 | Annual Income Range → `$30k-$60k` | target radio.checked === true |
| I2 | Do you have existing cover? → `No` | target radio.checked === true |
| I3 | Coverage Type → random | 组内有且仅有一个 radio checked |
| I4 | 不存在选项 `$100k+` | 返回失败，DOM 状态完全不变 |
| I5 | Ant Design / React Select / MUI Select 回归 | 下拉框选择不受影响 |

### 人工 smoke

VNC 观察 CTM 全流程跑通到 results 页。

---

## 九、验证步骤

```bash
pip install -r requirements-dev.txt
python -m pytest tests/test_choice_explorer.py -v
python -m pytest tests/test_select_option_compat.py -v
python -m pytest tests/test_select_option_pipeline.py -v
MOCK_BASE_URL=http://192.168.1.51:8080 WS_URL=ws://127.0.0.1:9222/... \
  python -m pytest tests/test_radio_strategy_ctm.py -v
```
