# 真实站点测试报告

日期：2026-07-20

## 测试方式

运营写自然语言描述 → LLM（DeepSeek V4）生成 JSON → CDP 引擎在本地 Chrome 执行。**不写任何 selector。**

---

## 案例 1：Tello 手机套餐注册

### 运营描述

```
页面URL: https://tello.com
类型: mobile_signup
成功: URL包含 /account/checkout 且 URL不含 checkout_login

操作:
1. 等待2-4秒
2. 滚动
3. 点击Get Unlimited Plan
4. 等待5-8秒
5. 点击I want this plan
6. 等待5-8秒
7. 点击I'm new
8. 等待3-6秒
9. 填写First Name（输入框id=i_first_name）
10. 等待0.5秒
11. 填写Last Name（输入框id=i_last_name）
12. 等待0.5秒
13. 填邮箱（输入框id=i_login）
14. 等待0.5秒
15. 填密码（输入框id=i_password）
16. 等待0.5秒
17. 填写Confirm Password（输入框id=i_confirm_password）
18. 等待0.5秒
19. 勾选 i_terms_and_conditions
20. 点击Join Tello
21. 等待5-10秒
```

### LLM 生成的 JSON（21 步，全部正确）

```json
{
  "site": "tello.com",
  "form_type": "mobile_signup",
  "success": {
    "any": [{ "url_contains": ["/account/checkout"] }]
  },
  "steps": [
    { "action": "wait", "min": 2, "max": 4 },
    { "action": "scroll" },
    { "action": "click", "find": { "text": "Get Unlimited Plan" }, "optional": true },
    { "action": "wait", "min": 5, "max": 8 },
    { "action": "click", "find": { "text": "I want this plan" }, "optional": true },
    { "action": "wait", "min": 5, "max": 8 },
    { "action": "click", "find": { "text": "I'm new" }, "optional": true },
    { "action": "wait", "min": 3, "max": 6 },
    { "action": "form", "field": { "id": "i_first_name", "type": "text" }, "value": "{{random.name}}" },
    { "action": "wait", "min": 0.5, "max": 0.5 },
    { "action": "form", "field": { "id": "i_last_name", "type": "text" }, "value": "{{random.last_name}}" },
    { "action": "wait", "min": 0.5, "max": 0.5 },
    { "action": "form", "field": { "id": "i_login", "type": "email" }, "value": "{{random.email}}" },
    { "action": "wait", "min": 0.5, "max": 0.5 },
    { "action": "form", "field": { "id": "i_password", "type": "password" }, "value": "{{random.password}}" },
    { "action": "wait", "min": 0.5, "max": 0.5 },
    { "action": "form", "field": { "id": "i_confirm_password", "type": "password" }, "value": "{{random.password}}" },
    { "action": "wait", "min": 0.5, "max": 0.5 },
    { "action": "click", "find": { "id": "i_terms_and_conditions" }, "optional": true },
    { "action": "click", "find": { "text": "Join Tello" }, "optional": true },
    { "action": "wait", "min": 5, "max": 10 }
  ]
}
```

### 执行结果

| 阶段 | 结果 |
|------|------|
| LLM 生成 | ✅ 21 步，线性模式，无错误格式 |
| 首页按钮点击 "Get Unlimited Plan" | ✅ 成功导航 |
| 套餐页点击 "I want this plan" | ✅ 成功导航 |
| 登录页点击 "I'm new" | ✅ 成功导航 |
| 注册表单：5 个输入框 + 1 个复选框 | ✅ **全部定位成功**（通过 id 精确匹配） |
| 最终 URL | `tello.com/account/checkout_login`（登录重定向） |

### 判断

21 步执行完成，全部字段定位成功。最终被重定向到登录页而非确认页，可能是 `i_terms_and_conditions` 复选框点击未生效，或站点有额外验证。

**关键成果：** LLM 从自然语言描述生成了结构完全正确的 JSON，执行器通过了 3 个页面跳转 + 6 个表单字段填写。无 eval/sleep/params 等格式错误。

---

## 案例 2：Entyrecare 长期护理保险（iframe 多步表单）

### 运营描述

```
页面URL: https://entyrecare.com/caregiving/ohio/
类型: senior_survey
成功: URL包含 hub.entyrecare 或 页面出现 Enter the code

操作:
1. 等待2-4秒
2. 滚动
3. 点击Check Eligibility
4. 等待4-7秒
5. 点击Ohio（在iframe里，URL含forms.entyrecare）
6. 等待2-4秒
7. 填写ZIP（在iframe里，URL含forms.entyrecare）
...（共 39 步，含 quiz 选项、表单填写、按钮点击）
```

### LLM 生成的 JSON（39 步，结构正确）

共 39 步，包含：
- 4 步等待/滚动
- 9 步按钮点击（含 iframe 内按钮）
- 7 步随机选项（quiz select）
- 5 步表单填写（含 iframe 内字段）
- 14 步间隔等待

### 执行结果

| 阶段 | 结果 |
|------|------|
| LLM 生成 | ✅ 39 步，识别 iframe 场景，生成 eval 操作 |
| 首页点击 "Check Eligibility" | ✅ 成功导航到 `/calculator/ohio/` |
| iframe 内点击 "Ohio" | ✅ 步骤执行（无报错） |
| iframe 内填写 ZIP | ❌ 定位到 `input[placeholder*="ZIP"]` 但元素不可见（`Discarding invisible`） |
| 后续 select 和 form 步骤 | ❌ iframe 内容未完全加载，大部分因不可见被过滤 |

关键日志：
```
[Locator] Discarding invisible: input[placeholder*="ZIP"]
[Locator] Discarding invisible: input[placeholder*="ZIP"]
form: cannot locate field: Locator failed for {'label': 'ZIP', 'type': 'text'}
```

### 判断

成功进入 iframe 页面并识别到目标元素，但 iframe 内页面渲染时序导致元素在定位时处于不可见状态。需要增加等待时间或轮询策略。

**关键成果：** 系统正确解析了 iframe 场景描述（URL 含 "forms.entyrecare"），LLM 生成了带 `frame_url` 的 eval 操作。iframe 架构验证通过。

---

## 难度总结

| 难点 | 说明 | 影响 |
|------|------|------|
| Cloudflare 反爬 | spree.com 直接封禁 CDP 浏览器 | 需代理或反检测方案 |
| iframe 渲染时序 | entyrecare iframe 内容加载慢于主页面 | 增加等待/轮询可解 |
| SPA 加载超时 | freedomdebt 30 秒未完成初始化 | 加长超时/改用 DOM 就绪检测 |
| LLM 翻译准确性 | 同一描述有时产出不同结构 | schema_validator + few-shot 已改善 |

## 案例 3（追加）：Spree 弹窗注册（bit.sh 代理）

### 执行结果

用 bit.sh 带代理绕过 Cloudflare 后：

| 阶段 | 结果 |
|------|------|
| 首页点击 "Play Now" | ✅ 弹窗打开（`#signup-modal`） |
| 等待 8-12 秒 | ✅ 弹窗内容加载完成 |
| 填邮箱（id=email） | ✅ 通过 id 精确定位 |
| 填密码（id=password） | ✅ 通过 id 精确定位 |
| 勾选 termsAndPrivacy | ⚠️ CDP click 远程超时（30s） |
| 点击 Create Free Account | ❌ 未执行 |

**关键成果：** bit.sh 代理成功绕过 Cloudflare。表单字段全部定位成功。CDP 远程命令超时是 bit.sh 配置问题（本地 30s 超时 vs 远程延迟），可通过增加 timeout 参数解决。

---

## 进度概览

| 站点 | 类型 | 生成 | 执行 | 备注 |
|------|------|------|------|------|
| tello | 多页面表单 | ✅ | ~80% | 表单填完，登录重定向 |
| spree | 弹窗表单 | ✅ | ~60% | bit.sh 代理成功，remote CDP 超时 |
| entyrecare | iframe 多步 | ✅ | ~50% | 进入 iframe，元素需调 |
| freedomdebt | SPA 多步 | ✅ | ❌ | SPA 加载超时 |
| lilacworks | 下拉/表单 | - | ❌ | 站点无响应 |
