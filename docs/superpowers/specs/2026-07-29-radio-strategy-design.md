# RadioStrategy — 通用 Radio Group 选择策略（redline v2）

## Context

当前 radio 选择分散在三条路径中，各自有确定性问题：

| 路径 | 位置 | 问题 |
|------|------|------|
| `_smart_form` radio/chip 分支 | `json_executor.py` ~L1051-1114 | 指定选项匹配失败后降级为 random；从 parentElement 随机搜 chip，作用域不可靠；点击后不验证 checked |
| `_select_option()` | `json_executor.py` L451-631 | 全局 random 跳过 CTA 词但不验证 radio state |
| `_try_quiz_group()` | `json_executor.py` L972-1007 | 仅处理 scoped quiz，不处理精确选项 |
| Shared verifier | `select_explorer.py` L227 | `"verified" in "not verified"` → True（substring 假阳性） |

CTM 表单的 "Annual Income Range → $30k-$60k" 和 "Do you have existing cover? → No" 是标准 `<label><input type="radio">text</label>` 结构，当前全部失败。

本设计新增 `RadioStrategy` 作为已有 SelectExplorer 架构下的交互策略，由 `ChoiceExplorer.classify()` 基于 DOM 正向证据分发。RadioStrategy 有正向证据时短路旧逻辑；未覆盖形态（checkbox/card/rating/chip）保留 legacy。

---

## 一、路由架构

### 1.1 入口：normalize → classify → dispatch

```
_execute_step(step)
  → step = _normalize_choice_step(step)     // 旧协议 → ChoiceIntent
  → if step.action == "select_option":
      → ChoiceExplorer.execute(intent, frame_id)
        → classify(intent, frame_id)
           返回: RADIO_GROUP | NATIVE_SELECT | COMBOBOX | UNSUPPORTED
        → dispatch:
            RADIO_GROUP    → RadioStrategy
            NATIVE_SELECT  → SelectExplorer (existing)
            COMBOBOX       → SelectExplorer (existing)
            UNSUPPORTED    → fall through to legacy paths
        → return SelectOutcome
  → else if step.action == "form" + select key:
      → existing _classify_select_intent → Explorer / legacy
  → else if step.action == "select":
      → existing _select_option (legacy, unchanged this round)
```

**关键约束**：
- `select_option` 在旧 `form` 的 FieldLocator 之前独立路由 — CTM 问题标题 label 没有 `for`，先走 locator 会直接失败
- 只有 `ChoiceExplorer.classify()` 一个分类器，不与 `_classify_select_intent()` 双重分类
- classify 基于 DOM 正向证据（实际发现了 `input[type=radio]` / `[role=radio]`），不是静态推断

### 1.2 _normalize_choice_step 兼容协议

真实仓库中旧格式（仅这两种）：

| 旧格式 | 转换结果 |
|--------|---------|
| `{"action":"form","field":{"label":"Coverage Type"},"select":"__random__"}` | `ChoiceIntent(mode="random")` — 必须由 classify DOM 证据决定走 RadioStrategy 还是 dropdown |
| `{"action":"select","selection_strategy":{"type":"random"}}` | `ChoiceIntent(mode="random")` — 仅 classify 确认 radio 时进入 RadioStrategy；checkbox/card 保留 legacy |

**不能**静态转换旧 `form+select` 为 `select_option`，因为它同时用于 dropdown、radio、rating。必须进入 ChoiceExplorer 由 DOM 决定。

### 1.3 RadioStrategy.execute(intent, frame_id)

```
1. locate_scope_candidates(field_label, option_text)
     双路径: field_label 定位问题区域 → 失败时 option_text 反推（仅唯一匹配时恢复）
2. discover_radio_groups(scopes)
     scope 逐层: fieldset/legend → 语义容器 → 共同祖先 → 外扩
     native: 按 form owner + name 分组
     ARIA: 按 [role=radiogroup] 分组
     仅当前可见且 enabled；排除 hidden wizard step、aria-hidden、disabled/aria-disabled
     隐藏 native input 但关联 label 可见 → 仍是候选
     role wrapper + 隐藏 native input → 去重为一个逻辑选项
3. score_and_select_group()
     评分: option_text 在组内 > legend/aria-labelledby > DOM 距离 > name token（弱证据）
     并列 → AMBIGUOUS_GROUP，不猜测，零点击
4. match_option(normalized_exact)
     规范化: 空白/NBSP/大小写/连字符 → 全文相等
     不匹配 → OPTION_NOT_FOUND，不退化
     random 模式跳过此步
5. read current state → selected_before
     random 模式已有选择 → ALREADY_SELECTED，不重新随机
     field={} + exact: 仅通过唯一选项反查
     field={} + random: 仅选择唯一可见 group
     多个 Yes/No 组不能仅靠 "No" 猜组
6. 已选中 → ALREADY_SELECTED（不重复点击）
7. resolve_activation_target()
     input / 祖先 label / label[for]
8. activate
     使用唯一 marker（携带 frame_id），click target 与 verify control 使用不同 marker
     CDP 点击失败 → CLICK_FAILED
9. shared_verifier.poll(predicate, timeout)
     native: target input.checked === true + 同组仅一个 checked
     ARIA: aria-checked === "true"
     超时 → NOT_VERIFIED
     finally 中逐 frame 清理所有 marker
10. return SelectOutcome + evidence
```

**Radio 语义硬编码规则**（不依赖 prompt）：
- 只发现当前可见且 enabled 的组
- 隐藏 native input + 可见关联 label → 候选
- role wrapper + 隐藏 native input → 去重
- Native: 最终验证 target radio checked + 同组只有一个 checked
- Random 已有选择 → ALREADY_SELECTED
- field={}: exact 唯一反查；random 唯一 visible group
- DOM remount（控件点击后消失）→ 由 shared verifier 检测元素 detached → NOT_VERIFIED

---

## 二、Shared Verifier 修复

### 2.1 当前假阳性

```python
// select_explorer.py L227
"verified" in "not verified"   # → True  ← 错误
// quiz verify 路径
"checked" in "not checked"     # → True  ← 错误
```

### 2.2 修正为结构化返回

```python
@dataclass
class VerifyResult:
    verified: bool
    reason: str          # "state_matched" | "state_unchanged" | "element_detached" | "timeout" | "cdp_error"
    elapsed_ms: int
    evidence: dict       # {before, after, signal_type, signal_value}
```

- click target marker 与 verify control marker 不同
- marker 携带 frame_id token 防串
- finally 中逐 frame 清理，不使用固定 `data-probe` 字符串
- 不再使用 `in` 做字符串包含判断

---

## 三、SelectOutcome 模型（统一）

### 3.1 Status 全集

```
SELECTED            — selected_before=false, selected_after=true
ALREADY_SELECTED    — selected_before=true, 不重复点击
FIELD_NOT_FOUND     — 问题文字找不到，且无法通过选项反推
GROUP_NOT_FOUND     — 区域内无可见 radio group
OPTION_NOT_FOUND    — 指定 option.text 在 group 内不匹配
AMBIGUOUS_GROUP     — 多个 group 同分，无法确定
AMBIGUOUS_CONTROL   — 同一 scope 内存在多种控件类型且无法区分
CLICK_FAILED        — CDP 点击未发出（不可交互/无坐标/报错）
NOT_VERIFIED        — 点击已发出，超时后状态未变化或元素 detached
NO_CANDIDATE        — 旧 Explorer 保留，映射至此
NO_SAFE_TRIGGER     — 旧 Explorer 保留，映射至此
OPEN_FAILED         — 旧 Explorer 保留，映射至此
UNSUPPORTED_CONTROL — classify 返回非 radio/native-select/combobox 的控件类型
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
- 所有歧义状态（AMBIGUOUS_GROUP、AMBIGUOUS_CONTROL）零点击

---

## 四、交付范围与 Legacy 保留

| RadioStrategy 短路（本轮新增） | Legacy 保留（本轮不动） |
|---|---|
| `input[type=radio]` — native radio group | `.chip` / `.pic` — 视觉 chip 卡片 |
| `[role=radio]` — ARIA radio group | `[data-value]` — 自定义卡片 |
| label[for] 关联 radio | rating/star 组件 |
| 隐藏 native input + 可见 label | 图片卡 / 伪 radio（div 模拟） |
| | checkbox（留待 CheckboxStrategy） |
| | Native `<select>` random 分支 |
| | `_select_option()` 全局 random（留待后续迁移） |
| | `_try_quiz_group()` 非 radio 路径 |
| | `quiz_loop()` |

**删除**：`_smart_form` 中仅 native radio + ARIA radio 的 parentElement 随机搜索代码。不删除 chip/card/rating/checkbox 路径。

---

## 五、Prompt 全量变更

### 5.1 GENERATE_PROMPT

**删除**：
- L62-63: `radio/按钮/选项类选择，直接用 click + find.text...`
- L68-69: `只有真正的 <select> 下拉框才用 form + select`

**新增 select_option 章节**（放在动作速查表之后）：
```
### 语义选择 (select_option)

当运营写"选择问题 XXX(选YYY)"时, 生成:
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
- field.label 和 option.text 必须复制运营描述或"当前页面可见元素"原文
  不翻译、不改写、不补充、不修改标点/货币符号/数值范围
- 没有可靠问题文字时 "field":{}
  执行器只能通过选项唯一反查或页面唯一可见 choice group 执行
  否则返回歧义，不猜测
- 本动作支持: radio group、原生 <select> 下拉框、combobox 下拉框
- 不支持: checkbox、卡片选择、星级评分（这些继续用 form+check / form+select）
```

**更新动作速查表**：
- `选择XXX（选YYY）` → action 从 `form` 改为 `select_option`

**更新可用 action 白名单**：添加 `select_option`

**更新 quiz 章节**：quiz 随机选项继续用 `action: "select"` + `selection_strategy`

**更新状态机示例**：select 步骤保留，不改为 select_option

### 5.2 FIX_PROMPT

- 新增 select_option 修复指引：option.text 不匹配时检查 snapshot 中的真实选项文字
- field.label 不匹配时尝试 snapshot 中的问题标题

### 5.3 _post_fix (json_pipeline.py)

- `has_select` 逻辑：跳过 `action="select_option"` 步骤，不自动插入旧全局 random select

### 5.4 auto_fixer.py

- L47: `field.type` 自动补全跳过 `action="select_option"` 步骤

### 5.5 wizard_explorer.py

- 保留 legacy adapter，不迁移。RadioStrategy 不调用 wizard_explorer。

---

## 六、硬约束

- exact 失败 → OPTION_NOT_FOUND，不退化 random
- 多控件/多分组同分 → AMBIGUOUS_GROUP 或 AMBIGUOUS_CONTROL，零点击
- `selected_after ≠ true` → 不能返回 SELECTED
- random 仅由 `option.mode="random"` 触发（或旧协议经 classify 确认 radio 后）
- ALREADY_SELECTED 不重复点击
- `ok` 仅由 status 派生
- 所有歧义状态零点击

---

## 七、文件变更清单

| # | 文件 | 变更 |
|---|------|------|
| 1 | `src/select_explorer.py` | 新增 RadioStrategy 类；新增 ChoiceExplorer.classify()；扩展 SelectOutcome status 枚举；新增 shared_verifier（结构化 VerifyResult）；修复 L227 substring 假阳性 |
| 2 | `src/json_executor.py` | `_execute_step()`: 新增 `_normalize_choice_step()` + `action="select_option"` 路由（在 form 分支之前）；删除 `_smart_form()` 中 native radio + ARIA radio 的随机搜索代码；checkbox/card/rating 保留 legacy；旧协议兼容解析 |
| 3 | `src/json_pipeline.py` | GENERATE_PROMPT: 删除 radio→click 规则，新增 select_option 章节，更新动作表、action 白名单、quiz 章节、状态机示例；FIX_PROMPT: 新增 select_option 修复指引；_post_fix: has_select 跳过 select_option |
| 4 | `src/auto_fixer.py` | L47: type 自动补全跳过 select_option |
| 5 | `tests/test_choice_explorer_unit.py` | **新增** — RadioStrategy 单元测试（详见验收用例） |
| 6 | `tests/test_select_option_compat.py` | **新增** — 旧协议兼容测试 |
| 7 | `tests/test_radio_strategy_ctm.py` | **新增** — CTM 集成测试（直接用 JSONExecutor，不走 web_editor） |

**不改变**：`locator.py`、`element_finder.py`、`wizard_explorer.py`

---

## 八、验收用例

### 单元测试 (test_choice_explorer_unit.py)

| ID | 用例 | 输入 | 期望 status |
|----|------|------|-------------|
| U1 | native radio exact 匹配 | `field={label:"Annual Income Range"}, option={mode:"exact",text:"$30k-$60k"}` | SELECTED |
| U2 | native radio exact 不匹配 | 同上, text="$100k+" | OPTION_NOT_FOUND |
| U3 | native radio random | `field={label:"Coverage Type"}, option={mode:"random"}` | SELECTED |
| U4 | native radio random 已有选择 | 同上，group 内已有 checked radio | ALREADY_SELECTED |
| U5 | ARIA radio exact 匹配 | `[role=radio]` 结构 | SELECTED, verification_signal=aria_checked |
| U6 | label[for] 关联 radio | label htmlFor 指向 radio id | SELECTED, activation_target=label_for |
| U7 | 隐藏 native input + 可见 label | input offsetWidth=0, label 可见 | SELECTED |
| U8 | role wrapper + 隐藏 native input 去重 | 同时存在 [role=radio] 和隐藏 input | 去重为一个逻辑选项，SELECTED |
| U9 | 两个 Yes/No 组（仅 option.text="No"） | 两个 group 都有 "No" 选项 | AMBIGUOUS_GROUP |
| U10 | field={} + option 唯一反查 | 整个页面仅一个 radio group 包含此选项 | SELECTED, discovery_method=option_unique_reverse |
| U11 | field={} + 多个 group | 多个 group 都含此选项 | AMBIGUOUS_GROUP |
| U12 | field={} + random + 唯一 group | 页面仅一个可见 radio group | SELECTED |
| U13 | field={} + random + 多个 group | 多个可见 radio group | AMBIGUOUS_GROUP |
| U14 | disabled radio group | 所有 input 带 disabled | GROUP_NOT_FOUND |
| U15 | 隐藏 wizard step radio | 不可见 step 内的 radio | GROUP_NOT_FOUND（不被发现） |
| U16 | 点击无状态变化 | CDP click 成功，checked 不变 | NOT_VERIFIED |
| U17 | CDP click 报错 | 元素不可交互 | CLICK_FAILED |
| U18 | iframe 内 radio | radio 在 childFrame 中 | SELECTED（frame_id 全链传递） |
| U19 | checkbox 不被拦截 | action=select_option, 页面只有 checkbox | UNSUPPORTED_CONTROL，fall through legacy |
| U20 | card/rating 不被拦截 | 页面只有 .chip 卡片 | UNSUPPORTED_CONTROL，fall through legacy |

### 兼容测试 (test_select_option_compat.py)

| ID | 用例 | 旧协议输入 | 期望 |
|----|------|-----------|------|
| C1 | form+select __random__ → classify radio | `{"action":"form","field":{"label":"Coverage Type"},"select":"__random__"}`, DOM 有 radio | normalize → ChoiceIntent(random) → classify → RADIO_GROUP → RadioStrategy → SELECTED |
| C2 | form+select __random__ → classify dropdown | 同上, DOM 是 native `<select>` | normalize → ChoiceIntent(random) → classify → NATIVE_SELECT → SelectExplorer |
| C3 | action=select random → radio | `{"action":"select","selection_strategy":{"type":"random"}}`, DOM 有 radio | classify → RADIO_GROUP → RadioStrategy |
| C4 | action=select random → checkbox | 同上, DOM 是 checkbox | UNSUPPORTED_CONTROL → legacy _select_option |
| C5 | 连续两题 marker 不串 | 先 select_option Q1, 再 select_option Q2 | Q1 marker 在 Q2 执行前已清理；Q2 不受 Q1 残留 marker 影响 |

### 集成测试 (test_radio_strategy_ctm.py)

直接用 `JSONExecutor` 执行固定 JSON，不走 web_editor：

| ID | 用例 | 断言 |
|----|------|------|
| I1 | Annual Income Range → $30k-$60k | target radio.checked === true |
| I2 | Do you have existing cover? → No | target radio.checked === true |
| I3 | Coverage Type → random | 组内有且仅有一个 radio checked |

CTM mock 在 `192.168.1.51:8080`，radio 页在 `/ctm/health_quote_v4.jsp`。

### 人工 smoke

VNC 观察 CTM 全流程：Coverage Type → Annual Income Range → DOB → Existing Cover → ... → results 页。

---

## 九、验证步骤

1. `python -m pytest tests/test_choice_explorer_unit.py -v`
2. `python -m pytest tests/test_select_option_compat.py -v`
3. `python -m pytest tests/test_radio_strategy_ctm.py -v`
4. 人工：web_editor 5000 跑 CTM description，VNC 确认 radio 选中生效
