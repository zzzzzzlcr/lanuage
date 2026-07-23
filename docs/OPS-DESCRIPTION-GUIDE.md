# 运营脚本描述规范 v5

运营用自然语言描述页面流程，引擎运行时自动定位元素。**不需要写任何 HTML/CSS/selector。**

---

## 模板

### 线性模式（推荐）

固定步骤流程，每一步编号：

```
页面URL: <网址>
类型: casino|dating|newsletter|health_insurance|home_improvement|...

成功: URL包含 <关键词> 或 页面出现 <文字>

操作:
<序号>. <动词> <对象>
```

### 状态机模式（quiz/动态页面）

流程不固定时使用，引擎每轮检查页面状态：

```
loop_until: URL包含 <关键词> 或 页面出现 <文字>

操作:
<序号>. <动词> <对象>（先执行的固定步骤）

when_<条件>: <做什么>
```

---

## 动词

| 写什么 | 引擎做的事 |
|--------|-----------|
| 填邮箱 / 填写邮箱 | 自动找邮箱输入框填入 |
| 填姓名 / 填写姓名 | 自动找姓名输入框填入 |
| 填密码 / 填写密码 | 自动找密码输入框填入 |
| 填手机号 / 填写手机号 | 自动找手机号输入框填入 |
| 填写XXX（输入框id=yyy） | 按 id 精确定位输入框填入 |
| 点击XXX | 找文字是"XXX"的按钮/链接点它 |
| 点击XXX按钮 | 同上，明确是按钮 |
| 等待 X-Y 秒 | 随机等 X-Y 秒 |
| 滚动 | 模拟人浏览页面 |
| 随机选一个选项 | 当前页面可见选项中随机点一个 |
| 随机选一个选项（第N题） | 同前，标注题号便于 LLM 理解 |
| 选择XXX（下拉框，选YYY） | 找 select 下拉框，选中值为 YYY 的 option |
| 选择XXX（下拉框id=zzz，选YYY） | 按 id 精确定位 select，选中 YYY |
| 勾选 XXX | 勾选复选框 |
| 拖动XXX（滚动条） | 滑块/range 设值 |

---

## 动作类型速查（给 LLM 用的，运营不用看）

| 运营动词 | JSON action | 说明 |
|---------|------------|------|
| 等待 | wait | `{"action":"wait","min":X,"max":Y}` 秒 |
| 点击 | click | `{"action":"click","find":{"text":"XXX"}}` |
| 填/填写 | form | `{"action":"form","field":{...},"value":"..."}` |
| 选择(下拉框) | form | `{"action":"form","field":{...},"select":"..."}` |
| 随机选 | select | `{"action":"select","selection_strategy":{"type":"random"}}` |
| 勾选 | form | `{"action":"form","field":{...},"check":"true"}` |
| 滚动 | scroll | `{"action":"scroll","min":100,"max":500}` |
| 拖动(滚动条) | eval | `{"action":"eval","script":"..."}` |

**可用 action：wait / click / form / select / scroll / eval / delay / goto / report**

## 关键规则

### 1. 推荐线性模式

**状态机（when_）虽然省字数，但 LLM 翻译不够稳定。推荐用编号步骤明确写出每一步。**

quiz 示例 — 推荐写法：
```
操作:
1. 等待2-4秒
2. 点击Accept & Continue（可选）
3. 等待1秒
4. 随机选一个选项（第1题）
5. 等待0.5秒
6. 随机选一个选项（第2题）
7. 等待0.5秒
8. 随机选一个选项（第3题）
...
15. 填写First Name（输入框id=fn）
16. 点击Next按钮
17. 填写Email Address（输入框id=em）
18. 点击Next按钮
```

每个 quiz 题目写一个"随机选一个选项 + 短等待"即可。如果有 N 道题就写 N 个。

### 2. 表单填完后加短等待

表单字段填完后建议加 0.5-1 秒等待，让页面 JS 验证有时间处理输入，否则点 Next 可能拿不到值：

```
13. 填写姓名（placeholder=Enter your first name）
14. 等待0.5秒
15. 点击Next按钮
```

### 3. 表单字段可加 id/placeholder 提示

引擎通过 label 文字、id、name、placeholder、type 等多种方式定位元素。如果知道输入框的 id，写上可以提升准确率：

```
填写邮箱（输入框id=email）
填写密码（输入框id=password）
```

### 3. 可选步骤

某些步骤（如 cookie 弹窗）可能不出现，加"（可选）"：

```
点击Accept all（可选）
```

---

## 成功条件

```
成功: URL包含 /success
成功: 页面出现 注册成功
成功: URL包含 /welcome 或 页面出现 Welcome back
```

支持多个条件（任一满足即成功）。

---

## 如果表单在 iframe 里

```
填写邮箱（在 iframe 里，URL 包含 "forms.example.com"）
```

运营只需要写 iframe URL 关键词，引擎自动定位。

---

## 填什么值

| 写什么 | 自动换成 |
|--------|---------|
| 填邮箱 | {{random.email}} |
| 填姓名 | {{random.name}} |
| 填密码 | {{random.password}} |
| 填手机号 | {{random.phone}} |
| 填生日 | {{random.dob}}（MM/DD/YYYY） |
| 填出生月 | {{random.dob_month}} |
| 填出生日 | {{random.dob_day}} |
| 填出生年 | {{random.dob_year}} |
| 填SSN | {{random.ssn}}（9位数字） |

---

## 示例

### 订阅表单（线性）

```
页面URL: https://auxx-lift.com/products/auxx-lift
类型: newsletter
成功: URL包含 customer_posted=true

操作:
1. 等待2-4秒
2. 滚动到底部
3. 填邮箱
4. 点击Subscribe
5. 等待5-8秒
```

### 多页注册（线性）

```
页面URL: https://free.spree.com/maxbonus/
类型: casino
成功: URL包含 /welcome

操作:
1. 等待2-4秒
2. 点击Continue
3. 等待5-10秒
4. 填邮箱
5. 填密码
6. 勾选服务条款
7. 点击Create Free Account
8. 等待5-8秒
```

### quiz 问答（推荐用线性）

```
页面URL: https://juliettdate.com/land/sp/xxx/
类型: dating
成功: URL包含 /news-feed

操作:
1. 等待2-4秒
2. 点击Accept all（可选）
3. 点击Let me see it
4. 等待2秒
5. 随机选一个选项（第1题）
6. 等待0.5秒
7. 随机选一个选项（第2题）
8. 等待0.5秒
9. 随机选一个选项（第3题）
10. 等待0.5秒
11. 随机选一个选项（第4题）
12. 等待0.5秒
13. 随机选一个选项（第5题）
14. 等待0.5秒
15. 随机选一个选项（第6题）
16. 等待0.5秒
17. 随机选一个选项（第7题）
18. 等待0.5秒
19. 随机选一个选项（第8题）
20. 等待1秒
21. 填姓名
22. 点击Next
23. 填邮箱
24. 点击Next
25. 填密码
26. 点击Next
27. 点击I Accept
28. 点击Explore now
```

### iframe 表单（状态机）

```
页面URL: https://entyrecare.com/caregiving/ohio/
类型: senior_survey
loop_until: URL包含 /verify

操作:
1. 等待2-4秒
2. 点击Check Eligibility
3. 等待5-8秒

when_页面有选项: 随机选一个选项
when_页面有Next按钮: 点击Next
when_页面有姓名输入框（在iframe里，URL含forms.entyrecare）: 填姓名
when_页面有邮箱输入框（在iframe里，URL含forms.entyrecare）: 填邮箱
```

---

## 要点

- **推荐线性模式** — 编号步骤比状态机更稳定
- **不写 selector** — 引擎自动找
- **可加 id 提示** — "输入框id=xxx" 提升定位精度
- **quiz 每题一个步骤** — "随机选一个选项（第N题）"
- **iframe 写 URL 关键词** — "在 iframe 里，URL 包含 xxx"
- **等待写范围** — "5-10秒"
- **可选步骤加标记** — "（可选）"
