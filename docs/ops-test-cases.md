# 运营测试用例 — Mock 站点描述模板

每个测试模板对应一个 mock 站点，运营拿去就能用。引擎会自动定位元素执行。

**Mock Server 地址：** `http://192.168.1.51:8080`

---

## 一、基础表单类

### 1.1 标准联系表单（nexaralai）

```
页面URL: http://192.168.1.51:8080/nexaralai/contact
类型: contact_form

成功: 页面出现 "Message Sent"

操作:
1. 填写姓名
2. 填写邮箱
3. 选择Subject（下拉框，选General enquiry）
4. 填写Message
5. 点击Send Message按钮
```

### 1.2 无 label 表单（no-label-form）

```
页面URL: http://192.168.1.51:8080/no-label-form
类型: newsletter

成功: 页面出现 "Thank you"

操作:
1. 填写ZIP Code
2. 填写邮箱
3. 填写手机号
4. 点击Get Started按钮
```

### 1.3 嵌套按钮表单（nested-buttons）

```
页面URL: http://192.168.1.51:8080/nested-buttons
类型: general

成功: 页面出现 "Thank you"

操作:
1. 点击Next
2. 点击Continue
3. 点击Submit
```

---

## 二、下拉框 & 选择器类

### 2.1 原生 Select + 下拉框（mui-select）

```
页面URL: http://192.168.1.51:8080/mui-select
类型: general

成功: 页面出现 "Selected:"

操作:
1. 选择State（下拉框，选California）
2. 点击Submit按钮
```

### 2.2 React Select 风格（react-select）

```
页面URL: http://192.168.1.51:8080/react-select
类型: general

成功: 页面出现 "✓ Country:"

操作:
1. 选择Country（下拉框，选United States）
2. 选择Interests（下拉框，选Technology）
3. 选择Interests（下拉框，选Finance）
4. 点击Submit按钮
```

### 2.3 Ant Design Select（ant-design）

```
页面URL: http://192.168.1.51:8080/ant-design
类型: general

成功: 页面出现 "Form submitted"

操作:
1. 填写Full Name
2. 填写Email Address
3. 填写Phone Number
4. 选择Country（下拉框，选United States）
5. 勾选Terms
6. 点击Submit按钮
```

### 2.4 生日三下拉框（dob-select）

```
页面URL: http://192.168.1.51:8080/dob-select
类型: general

成功: 页面出现 "Registered!"

操作:
1. 填写Email Address
2. 选择Month（下拉框，选6）
3. 选择Day（下拉框，选15）
4. 选择Year（下拉框，选1990）
5. 点击Submit按钮
```

---

## 三、Radio 单选类

### 3.1 四种 Radio 模式（radio-group）

```
页面URL: http://192.168.1.51:8080/radio-group
类型: general

成功: 页面出现 "✓ Plan:"

操作:
1. 选择Basic Plan（单选）
2. 选择Developer（单选）
3. 选择Intermediate（单选）
4. 选择Morning（单选）
5. 点击Submit按钮
```

### 3.2 保险表单 Radio + DOB（ctm）

```
页面URL: http://192.168.1.51:8080/ctm
类型: health_insurance

成功: URL包含 results

操作:
1. 点击Compare Health Insurance
2. 随机选一个选项
3. 随机选一个选项
4. 填写DD
5. 填写MM
6. 填写YYYY
7. 随机选一个选项
8. 勾选I agree
9. 点击Next按钮
10. 勾选至少一个benefit
11. 点击Next按钮
12. 填写姓名
13. 填写邮箱
14. 填写手机号
15. 点击Submit按钮
```

---

## 四、Checkbox 勾选类

### 4.1 Toggle Switch Checkbox（mui-checkbox）

```
页面URL: http://192.168.1.51:8080/mui-checkbox
类型: general

成功: 页面出现 "Thank you"

操作:
1. 勾选I agree to the Terms
2. 勾选I want to receive marketing emails
3. 点击Submit按钮
```

### 4.2 同意条款 Checkbox（renttoown）

```
页面URL: http://192.168.1.51:8080/renttoown
类型: home_improvement

成功: 页面出现 "Thank You"

操作:
1. 填写Postal Code
2. 填写First Name
3. 填写Last Name
4. 填写手机号
5. 填写邮箱
6. 勾选I consent
7. 点击Submit按钮
```

---

## 五、多步 SPA 类

### 5.1 SPA 三步表单（spa-steps）

```
页面URL: http://192.168.1.51:8080/spa-steps
类型: newsletter

成功: 页面出现 "Registration Complete"

操作:
1. 填写ZIP Code
2. 点击Continue按钮
3. 等待1-2秒
4. 填写Full Name
5. 点击Continue按钮
6. 等待1-2秒
7. 填写Email Address
8. 点击Submit按钮
```

### 5.2 太阳能 Wizard（solarforall）

```
页面URL: http://192.168.1.51:8080/solarforall
类型: home_improvement

成功: 页面出现 "Thank You"

操作:
1. 点击Ontario按钮
2. 等待1秒
3. 点击$150 – $250按钮
4. 等待1秒
5. 填写First name
6. 填写Last name
7. 填写Email address
8. 填写Phone number
9. 勾选I agree
10. 点击Continue按钮
```

### 5.3 隐私扫描多步（removemenow）

```
页面URL: http://192.168.1.51:8080/removemenow/freescan
类型: newsletter

成功: 页面出现 "Scan Complete"

操作:
1. 填写First Name
2. 填写Last Name
3. 填写Email
4. 填写Age
5. 填写Zip Code
6. 点击Scan Now for Free按钮
```

---

## 六、Quiz 答题类

### 6.1 Quiz 答题（survey-form）

```
页面URL: http://192.168.1.51:8080/survey-form
类型: survey

成功: 页面出现 "Thank You for Your Feedback"

操作:
1. 随机选一个选项（第1题）
2. 点击Next按钮
3. 随机选一个选项（第2题）
4. 点击Next按钮
5. 填写改进建议
6. 点击Next按钮
7. 选择评分5星
8. 点击Next按钮
9. 填写邮箱
10. 点击Submit按钮
```

### 6.2 车辆融资 Quiz（tabca）

```
页面URL: http://192.168.1.51:8080/tabca
类型: quiz

成功: 页面出现 "Proposal sent"

操作:
1. 随机选一个选项（第1题）
2. 随机选一个选项（第2题）
3. 填写邮箱
4. 点击See my proposal按钮
```

---

## 七、Slider 滑块类

### 7.1 Range 滑块（range-slider）

```
页面URL: http://192.168.1.51:8080/range-slider
类型: general

成功: 页面出现 "Thank you"

操作:
1. 拖动债务金额到75000
2. 点击Get Relief Options按钮
```

---

## 八、组件库专项

### 8.1 Chakra UI 表单（chakra-form）

```
页面URL: http://192.168.1.51:8080/chakra-form
类型: general

成功: 页面出现 "Form submitted successfully"

操作:
1. 填写First Name
2. 填写Last Name
3. 填写Email
4. 填写Phone Number
5. 点击Submit按钮
```

### 8.2 shadcn/ui 表单（shadcn-form）

```
页面URL: http://192.168.1.51:8080/shadcn-form
类型: general

成功: 页面出现 "Account created"

操作:
1. 填写Full Name
2. 填写Email
3. 填写Phone Number
4. 勾选I accept the terms
5. 点击Submit按钮
```

---

## 九、iframe 嵌套类

### 9.1 iframe 多步表单（entyrecare）

```
页面URL: http://192.168.1.51:8080/
类型: health_insurance

成功: URL包含 verify

操作:
1. 点击Check Eligibility
2. 等待3秒
3. 点击Ohio
4. 填写ZIP
5. 点击Next按钮
6. 随机选一个选项
7. 随机选一个选项
8. 随机选一个选项
9. 随机选一个选项
10. 随机选一个选项
11. 随机选一个选项
12. 随机选一个选项
13. 填写First name
14. 填写Last name
15. 点击Next按钮
16. 填写Email
17. 填写Phone
18. 随机选一个选项
19. 勾选I accept
20. 点击Submit按钮
```

---

## 十、复杂综合类

### 10.1 赌场注册 + 弹窗（ace）

```
页面URL: http://192.168.1.51:8080/ace
类型: casino

成功: 页面出现 "Welcome"

操作:
1. 填写Email
2. 填写Password
3. 点击Continue按钮
4. 填写First Name
5. 填写Last Name
6. 填写Day
7. 选择Month（下拉框，选June）
8. 填写Year
9. 点击Continue按钮
10. 点击Continue按钮 关闭弹窗
11. 点击Continue按钮 关闭退出弹窗
```

### 10.2 太阳能 Lead Gen（solarforall）

```
页面URL: http://192.168.1.51:8080/solarforall
类型: home_improvement

成功: 页面出现 "Thank You"

操作:
1. 点击Ontario按钮
2. 等待1秒
3. 点击$150 – $250按钮
4. 等待1秒
5. 填写First name
6. 填写Last name
7. 填写Email address
8. 填写Phone number
9. 勾选I agree
10. 点击Continue按钮
```
