# 集成测试 — 设计文档

日期：2026-07-18

## 问题

诊断报告系统（`src/diagnostics.py`）只有单元测试（17 个），没有端到端验证「自然语言 → JSON → 执行 → 诊断报告」这条完整链路在真实 mock 站点上是否正常工作。

## 目标

基于本地 mock server 的 7 个站点，为 pipeline 编写集成测试：
- **冒烟测试**（3 个站点）：验证已知可跑通的流程端到端通过
- **探索测试**（2 个站点）：验证复杂场景（iframe、hash DOM），含精确断言
- **诊断报告验证**：确认每次运行都生成结构完整的 JSON 报告

## 架构

新增 `test_integration.py`，通过 fixture 层管理 CDP 连接、LLM 客户端、mock server 可用性。每个站点一个测试函数。

```
test_integration.py    ← 新增：5 个集成测试用例 + fixture 层
```

### 测试覆盖矩阵

| 站点 | 类型 | 预期结果 | 断言粒度 | 验证重点 |
|------|------|---------|---------|---------|
| tello | 多页面跳转表单 | passed | B | 正常链路、报告结构完整 |
| spree | 年龄门 + 弹窗 | passed | B | 正常链路、modal 处理 |
| datewhirl | quiz 状态机 | passed | B | loop_until、随机选择 |
| entyrecare | iframe 多步表单 | passed | C | frame_url 是否正确注入 |
| reactapp | React hash DOM | failed (locator) | C | 失败分类、候选匹配 |

### 断言粒度

- **B 粒度（冒烟）：** `passed` 状态正确 + 报告 JSON 结构完整（有 steps、config、outcome）
- **C 粒度（精确）：** B + 特定步骤的错误信息或字段内容验证

## 数据流

```
describe (自然语言描述)
    → JSONPipeline.run(description, profile, url)
    → LLM 生成 JSON
    → CDP 浏览器执行
    → 诊断报告写入 reports/
    → 测试读取最新 JSON 报告
    → 断言
```

### Fixture 层

| Fixture | 作用 | 失败处理 |
|---------|------|---------|
| `cdp` | 从 `WS_URL` 连接 CDP | skip（无 URL） |
| `llm` | 从环境变量创建 OpenAI 客户端 | skip（无 key） |
| `pipeline` | `JSONPipeline(llm, cdp)` | 依赖以上两个 |
| `mock_server` | 确认 `localhost:8080` 可达 | skip（不可达） |

### 辅助函数

```python
def run_description(pipeline, description: str, navigate_url: str) -> dict:
    """跑完整 pipeline，返回最新生成的诊断 JSON 报告。"""
```

## 测试用例

### 1. test_tello — 冒烟（预期通过）

```
页面URL: http://localhost:8080/tello
类型: newsletter

操作:
1. 等待2-4秒
2. 点击Get Unlimited Plan
3. 等待3-5秒
4. 点击I want this plan
5. 等待3-5秒
6. 点击I'm new
7. 等待3-5秒
8. 填写姓名
9. 填写密码
10. 勾选服务条款
11. 点击Join Tello
12. 等待5-8秒

成功: URL包含 /account/checkout
```

断言：
- `report["outcome"]["passed"] is True`
- `report["steps"]` 非空
- `report["config"]["steps"]` 每步含 `_status`

### 2. test_spree — 冒烟（预期通过）

```
页面URL: http://localhost:8080/spree
类型: casino

操作:
1. 等待2-4秒
2. 点击Continue
3. 等待3-5秒
4. 填写邮箱
5. 填写密码
6. 勾选服务条款
7. 点击Create Free Account
8. 等待5-8秒

成功: URL包含 /spree/success
```

断言：同 tello。

### 3. test_datewhirl — 冒烟（quiz 状态机，预期通过）

```
页面URL: http://localhost:8080/datewhirl
类型: dating

loop_until: URL包含 /news-feed

操作:
1. 等待2-4秒
2. 点击Accept & Continue

when_页面有选项: 随机选一个选项
when_页面有Next按钮: 点击Next
when_页面有姓名输入框: 填写姓名
when_页面有邮箱输入框: 填写邮箱
when_页面有密码输入框: 填写密码
when_页面有I Accept: 点击I Accept
when_页面有Find matches: 点击Find matches
```

断言：同 tello + `outcome.fix_cycles` 合理（≤3）。

### 4. test_entyrecare — iframe 探索（预期通过）

```
页面URL: http://localhost:8080/
类型: senior_survey

操作:
1. 等待2-4秒
2. 点击Check Eligibility
3. 等待5-8秒

loop_until: URL包含 hub/auth/verify

when_页面有Ohio按钮: 点击Ohio
when_页面有Next按钮: 点击Next
when_页面有姓名输入框（在iframe里，URL含entyrecare）: 填写姓名
when_页面有邮箱输入框（在iframe里，URL含entyrecare）: 填写邮箱
when_页面有手机输入框（在iframe里，URL含entyrecare）: 填写手机号
when_页面有ZIP输入框（在iframe里，URL含entyrecare）: 填写邮编
when_页面有Submit按钮: 点击Submit
when_页面有选项: 随机选一个选项
```

精确断言：
- `passed=True`
- 报告中至少一个步骤的 config 含 `frame_url`（验证 iframe 定位生效）

### 5. test_reactapp — hash DOM（预期失败，locator 类别）

```
页面URL: http://localhost:8080/reactapp
类型: newsletter

操作:
1. 等待2-4秒
2. 填写邮编
3. 填写邮箱
4. 点击Submit

成功: URL包含 /thank-you
```

精确断言：
- `passed=False`
- `failure_category="locator"`
- 至少一个失败步骤含 `candidates` 列表（非空）

## 运行方式

```bash
# 前提：mock server + CDP 已启动
WS_URL=ws://192.168.1.51:9222/devtools/... \
OPENAI_API_KEY=xxx \
python3 -m pytest test_integration.py -v
```

| 环境变量 | 作用 | 缺失时 |
|---------|------|--------|
| `WS_URL` | CDP WebSocket 地址 | skip |
| `OPENAI_API_KEY` | LLM API key | skip |
| `OPENAI_BASE_URL` | LLM API 地址（可选） | 默认 deepseek |

## 与现有测试的关系

| 测试文件 | 类型 | 耗时 | 运行频率 |
|---------|------|------|---------|
| `test_fixer.py` | 单元 | <0.1s | 每次提交 |
| `test_diagnostics.py` | 单元 | <0.1s | 每次提交 |
| `test_integration.py` | 集成 | 30-120s | 功能变更时手动运行 |

集成测试默认不跑（依赖外部服务），通过环境变量控制。

## 注意事项

- **LLM 非确定性：** 同一描述可能生成不同 JSON，断言只验证报告结构，不验证 JSON 内容
- **执行时间：** 5 个站点预估 30-120 秒（取决于 LLM 响应和 fix 循环次数）
- **mock server 状态：** 测试之间重置浏览器到初始状态（navigate 到起始 URL）
- **报告目录：** 测试读取最新生成的 `reports/*-report.json`，不与现存报告冲突
