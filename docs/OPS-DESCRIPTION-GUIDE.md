# 运营脚本描述规范 v4

运营用自然语言描述页面流程，引擎运行时自动定位元素。**不需要写任何 HTML/CSS/selector。**

---

## 模板

```
页面URL: <网址>
类型: casino|dating|newsletter|health_insurance|home_improvement|...

操作:
<序号>. <动词> <对象>
```

如果流程有重复步骤（如 quiz），用状态机模式：

```
loop_until: <什么时候结束>

when_<条件>: <做什么>
when_<条件>: <做什么>
```

---

## 动词

| 写什么 | 引擎做的事 |
|--------|-----------|
| 填邮箱 | 自动找 type=email 的输入框 |
| 填姓名 | 自动找姓名输入框 |
| 填密码 | 自动找密码输入框 |
| 填手机号 | 自动找手机号输入框 |
| 点击 XXX | 找文字是"XXX"的按钮点它 |
| 等待 X-Y 秒 | 等一段时间 |
| 滚动 | 模拟人浏览页面 |
| 随机选一个选项 | 当前页面随机点一个选项 |
| 选择 XXX（下拉框） | 选 select 下拉框 |
| 勾选 XXX | 勾选复选框 |

---

## 如果表单在 iframe 里

```
填邮箱（在 iframe 里，iframe 的 URL 包含 "forms.example.com"）
```

运营只需要写 iframe URL 关键词，引擎自动定位。

---

## 多步骤 / quiz 模式

quiz 流程不固定，用状态机：

```
loop_until: 页面出现提交成功 或 URL 包含 /welcome

when_页面有选项: 随机选一个选项
when_页面有Next按钮: 点击Next
when_页面有Continue按钮: 点击Continue
when_页面有邮箱输入框: 填邮箱
when_页面有密码输入框: 填密码
when_页面有姓名输入框: 填姓名
when_页面有I Accept按钮: 点击I Accept
when_页面有Explore now按钮: 点击Explore now
```

不用写"先做这个再做那个"。引擎每轮检查页面上有什么，自动执行。

---

## 填什么值

| 写什么 | 自动换成 |
|--------|---------|
| 填邮箱 | {{random.email}} |
| 填姓名 | {{random.name}} |
| 填密码 | {{random.password}} |
| 填手机号 | {{random.phone}} |

---

## 简单示例

### 订阅表单

```
页面URL: https://auxx-lift.com/products/auxx-lift
类型: newsletter

操作:
1. 等待2-4秒
2. 滚动到底部
3. 填邮箱
4. 点击Subscribe
5. 等待5-8秒
```

### 多页注册

```
页面URL: https://free.spree.com/maxbonus/
类型: casino

操作:
1. 等待2-4秒
2. 点击Continue
3. 等待5-10秒跳转
4. 填邮箱
5. 填密码
6. 勾选服务条款
7. 等待45-55秒
8. 点击Create Free Account
9. 等待5-10秒
```

### quiz 问答（状态机）

```
页面URL: https://juliettdate.com/land/sp/xxx/
类型: dating

loop_until: 页面出现profile created 或 URL包含/news-feed

操作:
1. 等待2-4秒
2. 点击Accept all（可选）
3. 点击Let me see it
4. 等待3-5秒

when_页面有选项: 随机选一个选项
when_页面有Next按钮: 点击Next
when_页面有Continue按钮: 点击Continue
when_页面有姓名输入框: 填姓名
when_页面有邮箱输入框: 填邮箱
when_页面有密码输入框: 填密码
when_页面有I Accept: 点击I Accept
when_页面有Explore now: 点击Explore now
```

### iframe 表单

```
页面URL: https://entyrecare.com/caregiving/ohio/
类型: senior_survey

操作:
1. 等待2-4秒
2. 点击Check Eligibility
3. 等待5-8秒跳转

when_页面有选项: 随机选一个选项
when_页面有Next按钮: 点击Next
when_页面有姓名输入框（在iframe里，URL含forms.entyrecare）: 填姓名
when_页面有邮箱输入框（在iframe里，URL含forms.entyrecare）: 填邮箱
```

---

## 要点

- **不写 selector** — 引擎自动找
- **iframe 写 URL 关键词** — "在 iframe 里，URL 包含 xxx"
- **quiz 用状态机** — when_条件 代替固定顺序
- **等待写范围** — "5-10秒"
