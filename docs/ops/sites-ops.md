# 真实站点操作描述

按 `OPS-DESCRIPTION-GUIDE.md` v5 规范编写，用于引擎自动执行测试。

---

## 1. spree — casino 注册（年龄门 + 弹窗）

```
页面URL: https://free.spree.com/maxbonus/
类型: casino
成功: URL不含 #signup-modal 或 页面出现 my account 或 页面出现 my rewards

操作:
1. 等待2-4秒
2. 滚动
3. 点击Continue
4. 等待5-10秒
5. 填邮箱（输入框id=email）
6. 等待0.5秒
7. 填密码（输入框id=password）
8. 等待0.5秒
9. 勾选 termsAndPrivacy
10. 点击Create Free Account
11. 等待45-55秒
12. 点击Enable Now
13. 等待3-6秒
```

---

## 2. entyrecare — iframe 多步表单（senior survey）

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
8. 等待1秒
9. 在iframe里点击Next按钮
10. 等待3-5秒
11. 随机选一个选项（在iframe里第3步）
12. 等待2秒
13. 随机选一个选项（在iframe里第4步）
14. 等待2秒
15. 随机选一个选项（在iframe里第5步）
16. 等待2秒
17. 随机选一个选项（在iframe里第6步）
18. 等待2秒
19. 随机选一个选项（在iframe里第7步）
20. 等待2秒
21. 随机选一个选项（在iframe里第8步）
22. 等待2秒
23. 随机选一个选项（在iframe里第9步）
24. 在iframe里点击Next按钮
25. 等待3-5秒
26. 等待13-17秒
27. 填写First Name（在iframe里，输入框name=firstName）
28. 等待1秒
29. 填写Last Name（在iframe里，输入框name=lastName）
30. 在iframe里点击Next按钮
31. 等待3-5秒
32. 填写Email（在iframe里，输入框name=email）
33. 等待0.5秒
34. 填写Phone（在iframe里，输入框name=phone）
35. 等待0.5秒
36. 随机选一个选项（在iframe里）
37. 勾选复选框（在iframe里）
38. 在iframe里点击Submit按钮
39. 等待4-7秒
```

---

## 3. tello — 手机套餐注册（多页面跳转）

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

---

## 4. datewhirl — 约会 quiz（SPA 动态标签）

```
页面URL: https://juliettdate.com/land/sp/xxx/
类型: dating
成功: URL包含 /news-feed 或 页面出现 Welcome to Datewhirl

操作:
1. 等待2-4秒
2. 点击Accept all（可选）
3. 等待1秒
4. 随机选一个选项（第1题：性别）
5. 等待0.5秒
6. 随机选一个选项（第2题：兴趣）
7. 等待0.5秒
8. 随机选一个选项（第3题：年龄）
9. 等待0.5秒
10. 随机选一个选项（第4题：关系目标）
11. 等待0.5秒
12. 随机选一个选项（第5题：体型）
13. 等待0.5秒
14. 随机选一个选项（第6题：子女）
15. 等待0.5秒
16. 随机选一个选项（第7题：饮酒）
17. 等待0.5秒
18. 随机选一个选项（第8题：教育）
19. 等待1秒
20. 填写First Name（输入框id=fn）
21. 点击Next
22. 填邮箱（输入框id=em）
23. 点击Next
24. 填密码（输入框id=pw）
25. 点击Next
26. 点击I Accept
27. 等待1秒
28. 点击Find matches
```

---

## 5. freedomdebt — 债务减免（SSN + loading）

```
页面URL: https://apply.freedomdebtrelief.com
类型: debt_relief
成功: 页面出现 thank you 或 页面出现 Congratulations

操作:
1. 等待2-4秒
2. 滚动
3. 随机选一个选项（债务金额）
4. 等待1秒
5. 选择州（下拉框）
6. 点击Continue
7. 等待1秒
8. 填写First Name（输入框id=firstname）
9. 填写Last Name（输入框id=lastname）
10. 填邮箱（输入框id=email）
11. 填写手机号（输入框id=phone）
12. 点击Continue
13. 等待1秒
14. 填写地址（输入框id=addr）
15. 填写城市（输入框id=city）
16. 填写ZIP（输入框id=zip）
17. 点击Continue
18. 等待1秒
19. 填写SSN（输入框id=ssn）
20. 勾选 TCPA同意
21. 点击Check My Eligibility
22. 等待4-8秒
```

---

## 6. irspenalty — IRS 罚金减免（iframe + 两步表单 + quiz）

```
页面URL: http://localhost:8080/irspenalty
类型: tax_relief
成功: 页面出现 Congratulations

操作:
1. 滚动
2. 等待2-3秒
3. 随机选一个选项（税收类型：Personal taxes / Business taxes）
4. 等待3-5秒
5. 填写First Name（在iframe里，placeholder=Jane）
6. 填写Last Name（在iframe里，placeholder=Smith）
7. 填邮箱（在iframe里）
8. 填写手机号（在iframe里）
9. 等待1秒
10. 点击Check My Eligibility（在iframe里）
11. 等待3-5秒
12. 填写SSN（在iframe里）
13. 填写地址（在iframe里，placeholder=123 Main St）
14. 填写城市（在iframe里，placeholder=Dallas）
15. 选择州（在iframe里，下拉框）
16. 填写邮编（在iframe里）
17. 点击Look Up Penalty Status（在iframe里）
18. 等待3-5秒
```

---

## 7. lilacworks — 账单节省估算（select 下拉 + 两步）

```
页面URL: https://lilacworkstomorrow.com
类型: bill_savings
成功: 页面出现 Thank you

操作:
1. 等待2-4秒
2. 滚动
3. 选择Home Type（下拉框）
4. 等待0.5秒
5. 选择Province（下拉框，name=province）
6. 等待0.5秒
7. 填写monthly_bills（输入框name=monthly_bills）
8. 等待1-3秒
9. 点击Estimate Savings
10. 等待4-7秒
11. 填写First Name（输入框id=form-first）
12. 等待1秒
13. 填写Last Name（输入框id=form-last）
14. 等待0.5秒
15. 填邮箱（输入框id=form-email）
16. 等待0.5秒
17. 填写手机号（输入框id=form-phone）
18. 等待0.5秒
19. 勾选 consent（输入框id=form-consent）
20. 点击Get My Results
21. 等待4-7秒
```
