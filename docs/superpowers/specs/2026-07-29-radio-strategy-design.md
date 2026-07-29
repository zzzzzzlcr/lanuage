# RadioStrategy — 通用 Radio Group 选择策略（v3 final）

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

### 1.1 Strategy Probe 接口（解决 classify↔locate 循环依赖）

每个 Strategy 先产生结构化 Probe，ChoiceExplorer 再基于所有 Probe 分类。解耦分类与定位：

```python
@dataclass
class StrategyProbe:
    kind: str              # "native_radio" | "aria_radio" | "native_select" | "combobox" | "unsupported"
    candidates: list       # radio: list of GroupRef; dropdown: list of CandidateRef
    frame_id: str
    confidence: float      # 0.0-1.0, 基于 DOM 正向证据
    evidence: dict

class RadioStrategy:
    @staticmethod
    def probe(intent: ChoiceIntent, frame_id: str) -> StrategyProbe | None:
        """Single JS eval — scan for input[type=radio]/[role=radio] groups.
        Returns None if no radio groups found. Does NOT call FieldLocator."""
        ...

class DropdownStrategy:
    @staticmethod
    def probe(intent: ChoiceIntent, frame_id: str) -> StrategyProbe | None:
        """Uses FieldLocator to find CandidateRef for native <select>/[role=combobox].
        Returns None if no candidates."""
        ...
```

### 1.2 ChoiceExplorer 分发流程

```python
class ChoiceExplorer:
    def execute(self, request: NormalizedChoice, frame_id: str = "") -> SelectOutcome:
        # Step 1: 收集所有 Strategy Probe
        probes = []
        for strategy in [RadioStrategy, DropdownStrategy]:
            p = strategy.probe(request.intent, frame_id)
            if p:
                probes.append(p)

        # Step 2: 仲裁
        resolution = self._arbitrate(probes, request)

        # Step 3: 分发
        if resolution.can_execute:
            outcome = resolution.strategy.execute(request.intent, resolution.probe, frame_id)
            self._cleanup_markers(frame_id)
            return outcome
        elif request.origin == "legacy":
            return self._fallback_legacy(request)
        else:
            return SelectOutcome(status=UNSUPPORTED_CONTROL, ok=False)
```

**仲裁规则**：
- 单一 Probe 且 confidence > 0 → 执行
- 多个 Probe → AMBIGUOUS_CONTROL，零点击
- 零 Probe → origin=canonical: UNSUPPORTED_CONTROL；origin=legacy: fallback

### 1.3 Request Origin 模型

```python
@dataclass
class NormalizedChoice:
    intent: ChoiceIntent
    origin: str           # "canonical" | "legacy"
    original_step: dict   # legacy 时保存原始 step，canonical 时为 None

def normalize_choice_request(step: dict) -> NormalizedChoice:
    action = step.get("action", "")
    if action == "select_option":
        # Canonical — new protocol
        mode = step["option"]["mode"]
        text = step["option"].get("text") if mode == "exact" else None
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
        # Legacy — old form+select (could be radio, dropdown, or rating)
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
            # Exact: "No", "$30k-$60k" — treat as exact intent
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
        # Legacy — action=select (quiz random)
        return NormalizedChoice(
            intent=ChoiceIntent(label="", mode="random", option=None),
            origin="legacy",
            original_step=step,
        )
    return None  # not a choice step
```

**回退规则**（硬编码，不可配置）：
- origin=canonical + unsupported → UNSUPPORTED_CONTROL，零点击，失败
- origin=canonical + RadioStrategy 执行后任何失败 → 禁止 legacy fallback，返回 Outcome
- origin=legacy + RadioStrategy 确认 radio 并执行后 → 禁止 legacy fallback
- origin=legacy + unsupported（checkbox/card/rating）→ 使用 original_step 回退旧路径
- 歧义（任何 origin）→ 零点击，禁止 fallback

### 1.4 RadioStrategy.execute(intent, probe, frame_id)

```
1. scope_candidates = locate_scope_candidates(field_label, option_text)
     field_label 主路径；失败时 option_text 反推（仅唯一匹配恢复）
     field={}: 跳过 field_label 定位

2. groups = discover_radio_groups(scope_candidates, frame_id)
     scope 逐层: fieldset/legend → 语义容器 → 共同祖先 → 外扩
     native: 按 form owner + name 分组
     ARIA: 按 [role=radiogroup] 分组
     可见 + enabled；排除 hidden step、aria-hidden、disabled
     隐藏 native input + 可见 label → 候选
     role wrapper + 隐藏 native input → 去重

3. group = score_and_select_group(groups, field_label, option_text)
     评分: option_text 在组内 > legend/aria-labelledby > DOM 距离 > name token
     并列 → AMBIGUOUS_GROUP，零点击

4. target = match_option(group, option_text, mode)
     normalized_exact: 空白/NBSP/大小写/连字符 → 全文相等
     random: 随机未选中项
     不匹配 → OPTION_NOT_FOUND
     random + 全部已选 → ALREADY_SELECTED

5. read selected_before

6. 已选中 → ALREADY_SELECTED（不重复点击）

7. activation = resolve_activation_target(target)
     input / 祖先 label / label[for]

8. click_and_verify(activation, target, group, frame_id)
     - 生成唯一 click_marker 和 verify_marker（均携带 frame_id token）
     - CDP click(click_marker)
     - 检测 click 返回码 → 失败则 CLICK_FAILED
     - poll_until(predicate, timeout)
       native: verify_marker.checked === true + 同组唯一 checked
       ARIA: verify_marker.ariaChecked === "true"
     - 超时/元素 detached → NOT_VERIFIED
     - finally: 逐 frame 清理所有 marker（不使用固定 data-probe 字符串）

9. return SelectOutcome(status, evidence)
```

**Radio 语义硬编码规则**：
- 只发现当前可见且 enabled 的组
- 隐藏 native input + 可见关联 label → 候选
- role wrapper + 隐藏 native input → 去重为一个逻辑选项
- Native: target radio checked + 同组仅一个 checked
- Random + 已有选择 → ALREADY_SELECTED
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

### 2.3 CDP Click Wrapper（新增 `common.py` 或 `click_utils.py`）

```python
class ClickResult:
    success: bool
    error: str | None      # None on success; "not_interactive" / "no_coordinates" / "cdp_error"

def safe_click(cdp, selector: str, frame_id: str = "") -> ClickResult:
    """CDP click that returns structured result instead of silently failing."""
    try:
        cdp.click(selector, frame_id)
        return ClickResult(success=True, error=None)
    except Exception as e:
        return ClickResult(success=False, error=str(e)[:200])
```

用于产生可靠的 CLICK_FAILED status。

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

在 `json_pipeline.py` `_diagnose_page()` 或 `_diagnose_snapshot()` 中新增：

```python
def _diagnose_choice_groups(cdp, frame_id="") -> list[dict]:
    """Scan page for radio groups, fieldset/legend, radiogroup with accessible names."""
    js = """(function(){
      var groups = [];
      // Native radio groups by name
      var seen = {};
      var radios = document.querySelectorAll('input[type=radio]');
      radios.forEach(function(r){
        if (r.disabled || r.closest('[aria-hidden=true]')) return;
        var name = r.name || r.id || '';
        if (!name || seen[name]) return;
        seen[name] = true;
        var form = r.closest('form');
        var owner = form ? form.id || '' : '';
        var fieldset = r.closest('fieldset');
        var legend = fieldset ? (fieldset.querySelector('legend')||{}).textContent || '' : '';
        var labels = document.querySelectorAll('label[for="'+r.id+'"]');
        var labelText = labels.length ? labels[0].textContent.trim() : '';
        var options = [];
        var siblings = (form||document).querySelectorAll('input[type=radio][name="'+name+'"]');
        siblings.forEach(function(s){
          var optLabel = s.closest('label');
          var optText = optLabel ? optLabel.textContent.trim() : '';
          if (!optText && s.id) {
            var forLabel = document.querySelector('label[for="'+s.id+'"]');
            optText = forLabel ? forLabel.textContent.trim() : '';
          }
          options.push({text: optText, value: s.value, checked: s.checked});
        });
        groups.push({
          label: legend || labelText || '',
          type: 'native_radio',
          name: name,
          owner: owner,
          options: options
        });
      });
      // ARIA radio groups
      var ariaGroups = document.querySelectorAll('[role=radiogroup]');
      ariaGroups.forEach(function(g){
        if (!g.offsetWidth) return;
        var label = g.getAttribute('aria-label') || g.getAttribute('aria-labelledby') || '';
        var ariaRadios = g.querySelectorAll('[role=radio]');
        var options = [];
        ariaRadios.forEach(function(r){
          options.push({
            text: (r.textContent||'').trim(),
            value: r.getAttribute('aria-checked')||'',
            checked: r.getAttribute('aria-checked')==='true'
          });
        });
        groups.push({
          label: label,
          type: 'aria_radio',
          options: options
        });
      });
      return JSON.stringify(groups.slice(0, 10));
    })()"""
    raw = cdp.eval(js, frame_id)
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except:
        return []
```

`page_diag` 新增 choice_groups 段：
```
选择组:
[{"label":"Annual Income Range","type":"native_radio","options":[...]},
 {"label":"Do you have existing cover?","type":"native_radio","options":[...]}]
```

### 5.2 GENERATE_PROMPT 变更

**删除**：
- `radio/按钮/选项类选择，直接用 click + find.text，不要用 form + select！`
- `只有真正的 <select> 下拉框才用 form + select`

**新增 select_option 章节**：
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
- field.label 和 option.text 必须从"当前页面可见元素"的"选择组"中复制原文
  不翻译、不改写、不补充、不修改标点/货币符号/数值范围
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
| 2 | `src/json_executor.py` | `_execute_step()`: 新增 `_normalize_choice_request()` + `action="select_option"` 路由（在 form 分支前）；RadioStrategy 正向命中短路 `_smart_form`（不删旧代码）；旧 `_select_option` "checked" substring 假阳性标记 FIXME |
| 3 | `src/json_pipeline.py` | 新增 `_diagnose_choice_groups()`；GENERATE_PROMPT: 删除 radio→click 规则，新增 select_option 章节，更新动作表、白名单；FIX_PROMPT: select_option 修复指引；`_post_fix`: 精确 `has_choice` 逻辑 |
| 4 | `src/auto_fixer.py` | L47: 跳过 `select_option` 步骤的 type 补全 |
| 5 | `src/common.py` | 新增 `ClickResult` + `safe_click()` — CDP click 结构化返回，支持 CLICK_FAILED |
| 6 | `tests/conftest.py` | **新增** — pytest fixtures: CDP mock, frame helpers, test page loaders |
| 7 | `tests/fixtures/radio-groups.html` | **新增** — 独立 radio 测试页（native/ARIA/label-for/hidden/disabled/YesNo×2/iframe） |
| 8 | `tests/test_choice_explorer.py` | **新增** — 20 个单元测试 |
| 9 | `tests/test_select_option_compat.py` | **新增** — 5 个兼容测试 |
| 10 | `tests/test_select_option_pipeline.py` | **新增** — Prompt/_post_fix/auto_fixer 集成测试 |
| 11 | `tests/test_radio_strategy_ctm.py` | **新增** — CTM 集成测试，使用 `MOCK_BASE_URL`/`WS_URL` 环境变量，每例 reload/reset |
| 12 | `requirements-dev.txt` | **新增** — pytest 及相关依赖 |

**不改变**：`locator.py`、`element_finder.py`、`wizard_explorer.py`、`_smart_form` 旧分支

---

## 八、验收用例

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
| U14 | disabled radio group | GROUP_NOT_FOUND |
| U15 | hidden wizard step radio | GROUP_NOT_FOUND |
| U16 | 点击后 checked 不变 | NOT_VERIFIED, reason=state_unchanged |
| U17 | CDP click 报错 | CLICK_FAILED |
| U18 | iframe 内 radio | SELECTED, frame_id 全链传递 |
| U19 | canonical select_option + 仅 checkbox | UNSUPPORTED_CONTROL, 零点击, 不回退 |
| U20 | canonical select_option + 仅 .chip 卡片 | UNSUPPORTED_CONTROL, 零点击, 不回退 |
| U21 | mixed radio + checkbox 页面 | AMBIGUOUS_CONTROL, 零点击 |
| U22 | invalid mode | 解析层报错 |
| U23 | exact 缺 text | 解析层报错 |
| U24 | random 带了 text | 解析层报错 |

### 兼容测试 (tests/test_select_option_compat.py)

| ID | 用例 | 期望 |
|----|------|------|
| C1 | legacy form+select `__random__` + DOM 确认 radio | normalize(legacy) → ChoiceExplorer → RadioStrategy → SELECTED |
| C2 | legacy form+select `__random__` + DOM 是 native `<select>` | normalize(legacy) → DropdownStrategy probe → SelectExplorer |
| C3 | legacy form+select `"No"` (exact) + DOM 确认 radio | normalize(legacy, exact) → RadioStrategy → SELECTED |
| C4 | legacy action=select random + DOM 确认 radio | normalize(legacy) → RadioStrategy → SELECTED |
| C5 | legacy action=select random + DOM 仅 checkbox | UNSUPPORTED_CONTROL → legacy fallback `_select_option()` |
| C6 | 连续两题 marker 不串（Q1→Q2） | Q2 不受 Q1 残留 marker 影响 |
| C7 | RadioStrategy 成功后不回退 legacy | 不再执行 `_smart_form` |

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
