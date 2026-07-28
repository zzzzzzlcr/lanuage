# Select Explorer — 验证型行为探索器

## 动机

当前 `_smart_form` 对自定义下拉框的处理依赖组件库类名（`.ant-select-item`、`.css-select__option`、`.MuiMenuItem-root`），每遇到新组件库就要加 selector，本质是写死。

通用方案：**不需要知道是什么组件库，只靠"点击前后可见 DOM 差异"完成选择。**

## 架构

```
_execute_step                          ← 路由 + 定位 + 重试
  → _classify_select_intent()          ← DROPDOWN | CHOICE_GROUP | UNKNOWN
  → SelectExplorer.execute()           ← 新模块
      → normalize candidates
      → check already selected
      → try native <select>
      → discover safe trigger
      → snapshot BEFORE
      → CDP click trigger
      → snapshot AFTER
      → became_visible = AFTER - BEFORE
      → exact match option
      → CDP click option
      → verify state change
      → SelectOutcome
  → outcome.ok
_smart_form                             ← fill/checkbox/radio/rating/range
```

**核心约束：Explorer 返回 NOT_FOUND/NOT_VERIFIED 时，不允许回退旧 select 分支。**

## 三层接口

### SelectIntent

```python
@dataclass
class SelectIntent:
    label: str          # "Country"
    mode: str           # "exact" | "random"
    option: str | None  # "United States" (None when random)
    scope: dict | None
```

### CandidateRef

```python
@dataclass
class CandidateRef:
    selector: str       # unique marker injected into DOM
    frame_id: str
    source: str         # "label_for" | "adjacent_text" | "aria" | ...
    confidence: float
```

### SelectOutcome

```python
@dataclass
class SelectOutcome:
    status: str         # SELECTED | ALREADY_SELECTED | OPTION_NOT_FOUND
                        # | NOT_VERIFIED | AMBIGUOUS | NO_SAFE_TRIGGER
                        # | OPEN_FAILED | NO_CANDIDATE | UNSAFE_STATE_CHANGE
    evidence: dict      # trace of what happened
    attempts: list      # per-trigger details
    selected_text: str | None

    @property
    def ok(self) -> bool:
        return self.status in ("SELECTED", "ALREADY_SELECTED")
```

## 核心执行循环（Phase 1 MV）

```python
def execute_mv(intent: SelectIntent, candidates: list[CandidateRef]) -> SelectOutcome:
    with ProbeSession() as sess:

        # 1. Normalize candidates — inject unique markers into DOM
        refs = normalize_and_mark(candidates, sess)
        if not refs:
            return SelectOutcome("NO_CANDIDATE")

        # 2. Check current state — already selected?
        if detect_current_selection(refs, intent):
            return SelectOutcome("ALREADY_SELECTED")

        # 3. Native <select> fast path
        native = try_native_select(refs, intent)
        if native.applicable:
            return native

        # 4. Discover safe trigger
        trigger = find_single_safe_trigger(refs[0])
        if not trigger:
            return SelectOutcome("NO_SAFE_TRIGGER")

        if has_visible_choice_group(refs[0]):
            return SelectOutcome("NOT_APPLICABLE")

        # 5. Snapshot BEFORE
        before = snapshot_visible_option_candidates()
        trigger_text_before = visible_text(trigger)
        input_values_before = related_input_values(refs[0])

        # 6. CDP click trigger
        self.cdp.click(trigger.marker)

        # 7. Snapshot AFTER — poll up to 1.5s
        after = poll_until_visibility_changes(timeout=1.5)
        became_visible = set(after.visible_nodes) - set(before.visible_nodes)

        if not became_visible:
            return SelectOutcome("OPEN_FAILED")

        # 8. Exact match option in became_visible
        if intent.mode == "random":
            opts = [n for n in became_visible if n.is_option_like]
            match = random.choice(opts) if opts else None
        else:
            matches = exact_text_matches(became_visible, intent.option)
            if len(matches) == 0:
                return SelectOutcome("OPTION_NOT_FOUND")
            if len(matches) > 1:
                return SelectOutcome("AMBIGUOUS")
            match = matches[0]

        # 9. CDP click option
        self.cdp.click(match.marker)

        # 10. Verify
        verified = poll_until(
            lambda: (
                visible_text(trigger) == intent.option
                or related_input_values(refs[0]) != input_values_before
            ),
            timeout=1.5,
        )
        return SelectOutcome("SELECTED" if verified else "NOT_VERIFIED")
```

## 意图分类

```python
def _classify_select_intent(step, probe) -> str:
    if step.get("action") != "form" or "select" not in step:
        return "NOT_APPLICABLE"

    # 1. DOM hard evidence first
    if probe.has_native_choice_control or probe.has_visible_choice_group:
        return "CHOICE_GROUP"

    if probe.has_native_select or probe.has_aria_combobox:
        return "DROPDOWN"

    if probe.has_single_trigger_and_hidden_option_group:
        return "DROPDOWN"

    # 2. field.type as weak hint only
    ft = normalize(step.get("field", {}).get("type", ""))
    if ft in ("radio", "checkbox", "rating", "range"):
        return "CHOICE_GROUP"
    if ft in ("select", "dropdown", "combobox"):
        if probe.has_safe_single_trigger:
            return "DROPDOWN"

    # 3. Default: form+select = dropdown, with safety check
    if probe.has_safe_single_trigger and not probe.has_visible_choice_group:
        return "DROPDOWN"

    return "UNKNOWN"  # fail closed
```

## Phase 1 MV 验收

### 测试页面

| 页面 | 验证能力 |
|------|---------|
| `/ant-design` | 无 ARIA，option hidden→visible |
| `/react-select` | 不同 DOM 包装结构，单选用 Country |
| `/mui-select` | 标准 ARIA combobox + hidden input |

### 固定 JSON（不走 LLM）

```json
{"action":"form","field":{"label":"Country","type":"select"},"select":"United States"}
{"action":"form","field":{"label":"Country","type":"select"},"select":"Canada"}
{"action":"form","field":{"label":"State","type":"select"},"select":"California"}
```

### 验收标准

- 每个页面连续 10 次全部成功
- Explorer 核心代码不含 `ant-`、`Mui`、`react-select`、`css-select__` 字符串
- 页面真实显示值或隐藏 input 值正确
- `select: "Not Existing"` 必须 `OPTION_NOT_FOUND`
- 假成功 = 0
- 不经过 LLM，不点击 Submit

## 后续 Phase

| Phase | 内容 |
|-------|------|
| 1 | MV：单选自定下拉，before/after 可见性快照 |
| 2 | MutationObserver、portal 绑定、多 trigger 尝试 |
| 3 | random 模式、异步 option、键盘型下拉 |
| 4 | 负向 mock、通用随机 mock 100/100 |
| 5 | checkbox/radio/rating 迁移，移除 _smart_form 旧 select 分支 |

## 不做什么

- 不给 LLM 生成 selector
- 不依赖组件库类名做核心逻辑
- 旧 select 分支只在 feature flag 下保留，Explorer 失败不回退

---

## Phase 1 MV 验证结论

三个页面 10/10 通过，证明了架构假设而非仅修通组件：

```
ant-design: 无 ARIA，div hidden→visible          10/10
react-select: 隐藏 input anchor，trigger 在父级    10/10
mui-select:   标准 ARIA combobox + hidden input   10/10
```

核心代码零组件库类名，同一条 before/after 可见性探索链路覆盖三种形态。

## 后续架构边界

### 共享核心（所有 Strategy 复用）

```
├─ label/区域定位         ← FieldLocator 现有能力
├─ ProbeSession           ← 唯一 marker 注入与清理
├─ before/after 状态采集   ← snapshot_visible_text_nodes
├─ 真实 CDP 交互          ← cdp.click，非 element.click()
├─ trace、超时和清理       ← 每次探索可审计
└─ InteractionOutcome     ← 统一结果类型
```

### 独立 Strategy（每种交互形态各一个）

```
├─ NativeSelectStrategy      原生 <select>
├─ PopupChoiceStrategy       需要先展开的下拉框（Ant/React/MUI）
├─ VisibleChoiceStrategy     radio / button 卡片 / div 芯片 / 图片卡
├─ MultiChoiceStrategy       checkbox、多选 chip
├─ RatingStrategy            星级或数字评分
```

每个 Strategy 自决：
- 如何识别这种交互形态（DOM 探针）
- 候选如何评分（trigger scoring）
- 应该点击谁（anchor → trigger discovery）
- 目标选项如何匹配（text exact / value / data-attr）
- 什么状态变化才算成功（VerificationContract）

### VerificationContract（共享验证框架，不共享写死条件）

```
select:    value 或 trigger 文本变化
radio:     checked / aria-checked
图片卡:    隐藏 input、选中 class 或页面进入下一步
rating:    评分值、aria-valuenow 或对应项选中
checkbox:  checked + 数量 ≥ 最低要求
```

核心只负责采集证据和执行验证，成功条件由 Strategy 定义。

### 不做什么

- 不让 _smart_form 长成新的巨型类
- 不给 LLM 生成 selector 或组件库类名
- 不把"点击没报错"当成功
- 不在核心放 `ant-`、`Mui`、`react-select`、`css-select__`
