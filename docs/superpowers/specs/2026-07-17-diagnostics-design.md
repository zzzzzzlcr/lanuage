# 诊断报告系统 — 设计文档

日期：2026-07-17

## 问题

表单自动化脚本执行失败时，排查需要跨三个地方拼凑信息：终端日志、bit.sh 浏览器回放、CDP JSON 配置。没有一处给出完整的失败现场，费时费力。

## 目标

每次执行（成功或失败）自动生成一份诊断报告，把分散的信息整合到一起：
- **人读**：结构化的 Markdown 报告
- **机读**：JSON 格式，方便后续自动分析

## 架构

新增 `src/diagnostics.py`，包含三个核心类。报告在 `json_pipeline.py` 的 `validate()` 流程旁路收集，执行结束后统一生成。

```
src/diagnostics.py     ← 新增：StepTracer + PageInspector + ReportWriter
src/json_pipeline.py   ← 修改：validate() 织入诊断钩子
test_diagnostics.py    ← 新增：6 个测试用例
reports/               ← 新增：报告输出目录（.gitignore）
```

### 组件

| 类 | 职责 |
|---|---|
| `StepTracer` | 执行时收集每一步的 `StepResult`（索引、action、成功/失败、错误、耗时） |
| `PageInspector` | 拍页面快照：可见输入框、按钮、iframe 列表（复用现有 `_diagnose_page()` / `_diagnose_snapshot()` 逻辑） |
| `ReportWriter` | 将收集到的所有信息渲染为 Markdown 和 JSON 两份报告文件 |

### 数据流

```
generate → validate ──┬── StepTracer（每步收集结果）
                      ├── PageInspector（失败时拍快照）
                      └── (fix → validate 循环)
                           │
                           ▼
                      ReportWriter
                      ├── reports/YYYY-MM-DD-HHMMSS-<name>-report.md
                      └── reports/YYYY-MM-DD-HHMMSS-<name>-report.json
```

### 钩子接入点

| 位置 | 时机 | 收集内容 |
|------|------|---------|
| `_run_one_step()` 入口 | 每步开始前 | 步骤索引、action 类型 |
| `_run_one_step()` 返回 | 每步结束后 | 成功/失败、错误信息、耗时 |
| `_run_one_step()` 异常 | 步骤失败时 | 触发 `PageInspector` 拍页面快照 |
| `validate()` 返回前 | 整次执行结束 | 汇总生成报告 |

## 报告内容

### 2.1 运行摘要（Markdown + JSON）
成功/失败、步骤通过率、失败类型分类、总耗时、最终 URL。

### 2.2 步骤执行结果表
每步编号、动作类型、成功/失败、耗时、错误信息。

### 2.3 失败步骤深度诊断
每个失败步骤附带：
- 当时的页面 URL
- 候选匹配：要找的字段 vs 页面上实际存在的相似元素（复用 `locator.py` 中 `_find_all_candidates()` 的现有逻辑，按 confidence 降序排列）
- 可见按钮列表
- iframe 存在情况及 URL

### 2.4 失败原因分类
自动归类到：`locator` / `timeout` / `iframe_miss` / `success_condition` / `unknown`

**优先级规则：** 当一个失败符合多个类别时，取第一个匹配的：
1. `iframe_miss` — 步骤指定了 `frame_url` 但元素在目标 iframe 中未找到（优先于 `locator`）
2. `locator` — 元素定位失败（`LocatorError` 或无候选匹配）
3. `timeout` — `wait_for` 超时
4. `success_condition` — 所有步骤执行完但成功条件未触发
5. `unknown` — 以上都不匹配

### 2.5 JSON 配置引用
完整配置，失败步骤高亮标注。

## JSON 报告结构

```json
{
  "run": {
    "time": "2026-07-17T15:30:00",
    "description_file": "subscribe.txt",
    "site": "auxx-lift.com",
    "form_type": "newsletter"
  },
  "outcome": {
    "passed": false,
    "failures": 3,
    "total_steps": 8,
    "failure_category": "locator",
    "duration_ms": 45200,
    "fix_cycles": 2
  },
  "steps": [
    {
      "index": 0,
      "action": "wait",
      "success": true,
      "duration_ms": 2340
    },
    {
      "index": 3,
      "action": "form",
      "success": false,
      "error": "LocatorError: No candidates found",
      "duration_ms": 120,
      "snapshot": {
        "url": "https://auxx-lift.com/products/auxx-lift",
        "inputs": [
          {"tag": "INPUT", "type": "text", "name": "name", "id": "name-field", "placeholder": "Your name"}
        ],
        "buttons": [
          {"tag": "BUTTON", "text": "Subscribe", "id": "sub-btn"}
        ],
        "iframes": []
      },
      "candidates": [
        {"selector": "#name-field", "strategy": "label_for", "confidence": 0.3}
      ]
    },
    {
      "index": 4,
      "action": "click",
      "success": null,
      "note": "未执行（前一步失败）",
      "duration_ms": null
    }
  ],
  "config": {
    "site": "auxx-lift.com",
    "form_type": "newsletter",
    "success": {"any": [{"url_contains": ["success"]}]},
    "steps": [
      {"_status": "passed", "action": "wait", "min": 2, "max": 4},
      {"_status": "failed", "action": "form", "field": {"label": "Email", "type": "email"}},
      {"_status": "skipped", "action": "click", "find": {"text": "Subscribe"}}
    ]
  }
}
```

## 错误处理

诊断系统是旁路，不影响主流程。原则：**报告生成失败不中断执行。**

| 场景 | 处理方式 |
|------|---------|
| 快照采集抛异常（CDP 断开） | 捕获，`snapshot: null`，继续执行 |
| 报告写入失败（磁盘满、权限） | 打印警告到 stderr，不中断主流程 |
| `reports/` 目录不存在 | 自动创建，创建失败则输出到当前目录 |
| 步骤数据含不可序列化对象 | safe-serialize，fallback 到 `repr()` |
| 快照元素过多（>500 个） | 截断到前 200 个，标记 `truncated: true` |
| 流水线在生成阶段就挂了 | 报告含 `phase: "generation"` + LLM 返回原始内容 |

## 测试计划

`test_diagnostics.py` — 6 个用例：

| # | 用例 | 验证点 |
|---|------|--------|
| 1 | ReportWriter 正常生成 | 给定完整数据，Markdown + JSON 正确输出 |
| 2 | 空数据不崩溃 | 零步骤、无失败，报告正常生成 |
| 3 | 快照缺失降级 | `snapshot: null` 时报告不含报错 |
| 4 | 不可序列化对象 | safe-serialize 不抛异常 |
| 5 | 文件写入失败 | mock 写不进的目录，验证不崩溃且不中断 |
| 6 | StepTracer 字段完整性 | 收集的字段齐全（index, action, success, error, duration_ms） |

## 用户可见变化

**终端输出：** 执行结束后打印一行摘要 + 报告文件路径。
```
✗ FAILED (3/8 steps) — locator — 报告: reports/2026-07-17-153000-subscribe-report.md
```

**`run.sh` 行为不变**，自动输出报告。

**`reports/` 加入 `.gitignore`**。
