# 运营测试用例 — Mock 站点描述模板

每个测试模板对应一个 mock 站点，运营拿去就能用。引擎会自动定位元素执行。

**Mock Server 地址：** `http://192.168.1.51:8080`

---

## 全部站点速查

| # | 访问地址 | 站点说明 | 类型 |
|---|---------|---------|------|
| | **基础表单** | | |
| 1 | `http://192.168.1.51:8080/nexaralai/contact` | 标准联系表单（Name+Email+Select+Textarea） | contact_form |
| 2 | `http://192.168.1.51:8080/no-label-form` | 无 label 表单（只有 placeholder+aria-label） | newsletter |
| 3 | `http://192.168.1.51:8080/nested-buttons` | 嵌套按钮（button>span>span） | general |
| 4 | `http://192.168.1.51:8080/renttoown` | GHL 表单（data-q + 蜜罐字段） | home_improvement |
| | **下拉框 & 选择器** | | |
| 5 | `http://192.168.1.51:8080/mui-select` | MUI Select（div[role=combobox]） | general |
| 6 | `http://192.168.1.51:8080/react-select` | React Select（css-select__control + multi） | general |
| 7 | `http://192.168.1.51:8080/ant-design` | Ant Design（ant-select-dropdown） | general |
| 8 | `http://192.168.1.51:8080/dob-select` | 生日三下拉框（月/日/年 select） | general |
| | **Radio 单选** | | |
| 9 | `http://192.168.1.51:8080/radio-group` | 4种 Radio 模式（原生/MUI/div伪/图片卡） | general |
| 10 | `http://192.168.1.51:8080/province-select` | 按钮卡片省选择（button grid) | general |
| 11 | `http://192.168.1.51:8080/dob-buttons` | 按钮网格生日（月按钮 grid） | general |
| | **真实站点** | | |
| 12 | `https://lp2.canadianliferates.ca/` | 加拿大保险全流程（省/性别/保额/DOB/邮编/个人信息） | Life |
| 11 | `http://192.168.1.51:8080/ctm` | 保险多页（radio→checkbox→input） | health_insurance |
| | **Checkbox 勾选** | | |
| 12 | `http://192.168.1.51:8080/mui-checkbox` | MUI Toggle Switch（隐藏input+label在后） | general |
| | **多步 SPA / Wizard** | | |
| 13 | `http://192.168.1.51:8080/spa-steps` | SPA 三步（ZIP→Name→Email，异步切换） | newsletter |
| 14 | `http://192.168.1.51:8080/solarforall` | 太阳能 Wizard（省份→电费→联系信息） | home_improvement |
| 15 | `http://192.168.1.51:8080/removemenow/freescan` | 隐私扫描（fname+lname+email+age+zip） | newsletter |
| 16 | `http://192.168.1.51:8080/seniorbath/form` | Walk-in Tub（ZIP→Name→Terms） | home_improvement |
| | **Quiz 答题** | | |
| 17 | `http://192.168.1.51:8080/survey-form` | SurveyJS 风格（5题：radio/checkbox/textarea/rating/email） | survey |
| 18 | `http://192.168.1.51:8080/tabca` | 车辆融资 Quiz（2题 icon 按钮） | quiz |
| 19 | `http://192.168.1.51:8080/geminihealth` | 敏感度 Quiz（5题 funnel） | quiz |
| | **Slider 滑块** | | |
| 20 | `http://192.168.1.51:8080/range-slider` | Range 滑块（dispatchEvent） | general |
| | **MUI TextField** | | |
| 21 | `http://192.168.1.51:8080/mui-textfield` | MUI TextField ×4（label无for，_r_1_动态id） | general |
| | **组件库专项** | | |
| 22 | `http://192.168.1.51:8080/chakra-form` | Chakra UI（data-invalid, chakra-form__error） | general |
| 23 | `http://192.168.1.51:8080/shadcn-form` | shadcn/ui（data-slot=form-item, text-destructive） | general |
| 24 | `http://192.168.1.51:8080/modform` | Emotion CSS-in-JS（slotswise 1:1复刻） | general |
| | **iframe 嵌套** | | |
| 25 | `http://192.168.1.51:8080/` | entyrecare iframe 13步表单 | health_insurance |
| 26 | `http://192.168.1.51:8080/irspenalty` | IRS Penalty iframe 表单 | general |
| | **复杂综合** | | |
| 27 | `http://192.168.1.51:8080/ace` | 赌场注册（弹窗+exit popup） | casino |
| 28 | `http://192.168.1.51:8080/spree` | 年龄门+modal弹窗 | casino |
| 29 | `http://192.168.1.51:8080/livebeam` | DOM wizard（全部在同一页） | dating |
| 30 | `http://192.168.1.51:8080/datewhirl` | SPA quiz + 动态标签 | dating |
| 31 | `http://192.168.1.51:8080/tello` | 多页面跳转（5页注册） | general |
| 32 | `http://192.168.1.51:8080/reactapp` | React hash DOM（CSS Modules + data-field） | general |
| 33 | `http://192.168.1.51:8080/carwarranty` | 车辆保修（select 年/品牌/型号） | general |
| 34 | `http://192.168.1.51:8080/fishinvest` | 投资问卷（生日+密码） | general |
| 35 | `http://192.168.1.51:8080/mortgagequiz` | 房贷 Quiz（房价滑块） | general |
| 36 | `http://192.168.1.51:8080/garagefloor` | 家装 Quiz（楼梯数+ZIP） | home_improvement |
| 37 | `http://192.168.1.51:8080/gravityform` | WordPress GF 表单 | general |
| 38 | `http://192.168.1.51:8080/showerlead` | 家装 SMS opt-in | home_improvement |
| 39 | `http://192.168.1.51:8080/casinospin` | 幸运转盘（spin→signup→success） | casino |
| 40 | `http://192.168.1.51:8080/tarotcard` | 塔罗牌选卡 funnel | general |
| 41 | `http://192.168.1.51:8080/lilacworks` | 两步 select 表单 | general |
| 42 | `http://192.168.1.51:8080/freedomdebt` | 债务减免（SSN+spinner） | general |
| 43 | `http://192.168.1.51:8080/protectsav` | popup 表单 + name=* 选择器 | general |
| 44 | `http://192.168.1.51:8080/nexaralai` | Nexaral AI 首页 | landing |
| 45 | `http://192.168.1.51:8080/removemenow` | RemoveMe 首页+FAQ | landing |
| 46 | `http://192.168.1.51:8080/compareinsulation` | 隔热评估入口页 | landing |
| 47 | `http://192.168.1.51:8080/connecthearing` | 听力评估入口页 | landing |

---

## 一、基础表单类

### 1.1 标准联系表单（nexaralai）

```
页面URL: http://192.168.1.51:8080/nexaralai/contact
类型: contact_form

成功: 页面出现 Message Sent

操作:
1. 等待1-2秒
2. 填 Name(placeholder=Your name)
3. 填 Email(placeholder=you@example.co.uk)
4. 选择Subject(选General enquiry)
5. 填 Message(placeholder=Your message...)
6. 点 Send Message
```

### 1.2 无 label 表单（no-label-form）

```
页面URL: http://192.168.1.51:8080/no-label-form
类型: newsletter

成功: 页面出现 Thank you  

操作:
1. 填 ZIP Code
2. 填 Email
3. 填 Phone
4. 点击Get Started按钮
```

### 1.3 嵌套按钮表单（nested-buttons）

```
页面URL: http://192.168.1.51:8080/nested-buttons
类型: general

成功: 页面出现 Thank you  

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

成功: 页面出现 form submitted  

操作:
1. 选择State(选California)
2. 点 Submit
```

### 2.2 React Select 风格（react-select）

```
页面URL: http://192.168.1.51:8080/react-select
类型: general

成功: 页面出现 ✓ Country:  

操作:
1. 选择Country(选United States)
2. 选择Interests(选Technology)
3. 选择Interests(选Finance)
4. 点 Submit
```

### 2.3 Ant Design Select（ant-design）

```
页面URL: http://192.168.1.51:8080/ant-design
类型: general

成功: 页面出现 success  

操作:
1. 填 Full Name
2. 填 Email
3. 填 Phone Number
4. 选择Country(选United States)
5. 勾选Terms
6. 点 Submit
```

### 2.4 生日三下拉框（dob-select）

```
页面URL: http://192.168.1.51:8080/dob-select
类型: general

成功: 页面出现 Registered!  

操作:
1. 填 Email
2. 选择Month(选随机)
3. 选择Day(选随机)
4. 选择Year(选随机)
5. 点 Submit
```

---

## 三、Radio 单选类

### 3.1 四种 Radio 模式（radio-group）

```
页面URL: http://192.168.1.51:8080/radio-group
类型: general

成功: 页面出现 ✓ Plan:  

操作:
1. 选择Basic Plan（单选)
2. 选择Developer（单选)
3. 选择Intermediate（单选)
4. 选择Morning（单选)
5. 点 Submit
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
4. 填 DD
5. 填 MM
6. 填 YYYY
7. 随机选一个选项
8. 勾选I agree
9. 点 Next
10. 勾选至少一个benefit
11. 点 Next
12. 填 Name
13. 填 Email
14. 填 Phone
15. 点 Submit
```

---

## 四、Checkbox 勾选类

### 4.1 Toggle Switch Checkbox（mui-checkbox）

```
页面URL: http://192.168.1.51:8080/mui-checkbox
类型: general

成功: 页面出现 Thank you  

操作:
1. 勾选I agree to the Terms
2. 勾选I want to receive marketing emails
3. 点 Submit
```

### 4.2 同意条款 Checkbox（renttoown）

```
页面URL: http://192.168.1.51:8080/renttoown
类型: home_improvement

成功: 页面出现 Thank You  

操作:
1. 填 Postal Code
2. 填 First Name
3. 填 Last Name
4. 填 Phone
5. 填 Email
6. 勾选I consent
7. 点 Submit
```

---

## 五、多步 SPA 类

### 5.1 SPA 三步表单（spa-steps）

```
页面URL: http://192.168.1.51:8080/spa-steps
类型: newsletter

成功: 页面出现 Registration Complete  

操作:
1. 填 ZIP Code
2. 点 Continue
3. 等待1-2秒
4. 填 Full Name
5. 点 Continue
6. 等待1-2秒
7. 填 Email
8. 点 Submit
```

### 5.2 太阳能 Wizard（solarforall）

```
页面URL: http://192.168.1.51:8080/solarforall
类型: home_improvement

成功: 页面出现 Thank You  

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
10. 点 Continue
```

### 5.3 隐私扫描多步（removemenow）

```
页面URL: http://192.168.1.51:8080/removemenow/freescan
类型: newsletter

成功: 页面出现 Scan Complete  

操作:
1. 填 First Name
2. 填 Last Name
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

成功: 页面出现 Thank You for Your Feedback!

操作:
1. 随机选一个选项（第1题）
2. 点 Next
3. 随机选一个选项（第2题）
4. 点 Next
5. 填改进建议
6. 点 Next
7. 选择评分5
8. 点 Next
9. 填 Email
10. 点 Submit
```

### 6.2 车辆融资 Quiz（tabca）

```
页面URL: http://192.168.1.51:8080/tabca
类型: quiz

成功: 页面出现 Proposal sent  

操作:
1. 随机选一个选项（第1题）
2. 随机选一个选项（第2题）
3. 填 Email
4. 点击See my proposal按钮
```

---

## 七、Slider 滑块类

### 7.1 Range 滑块（range-slider）

```
页面URL: http://192.168.1.51:8080/range-slider
类型: general

成功: 页面出现 Thank you  

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

成功: 页面出现 success successfully  

操作:
1. 填 First Name
2. 填 Last Name
3. 填写Email
4. 填 Phone Number
5. 点 Submit
```

### 8.2 shadcn/ui 表单（shadcn-form）

```
页面URL: http://192.168.1.51:8080/shadcn-form
类型: general

成功: 页面出现 Account created  

操作:
1. 填 Full Name
2. 填写Email
3. 填 Phone Number
4. 勾选I accept the terms
5. 点 Submit
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
5. 点 Next
6. 随机选一个选项
7. 随机选一个选项
8. 随机选一个选项
9. 随机选一个选项
10. 随机选一个选项
11. 随机选一个选项
12. 随机选一个选项
13. 填写First name
14. 填写Last name
15. 点 Next
16. 填写Email
17. 填写Phone
18. 随机选一个选项
19. 勾选I accept
20. 点 Submit
```

---

## 十、复杂综合类

### 10.1 赌场注册 + 弹窗（ace）

```
页面URL: http://192.168.1.51:8080/ace
类型: casino

成功: 页面出现 Welcome to Ace

操作:
1. 等待1-2秒
2. 点击Continue
3. 等0.5秒
4. 填 Email
5. 填 Password
6. 点击Continue
7. 等待2-3秒
8. 点击Continue
9. 等0.5秒
10. 填 First Name
11. 填 Last Name
12. 填 Day
13. 选择Month(选随机)
14. 填 Year
15. Select Country(选Canada)
16. 点击Continue
17. 等待1-2秒
18. 点击Leave
```

### 10.2 太阳能 Lead Gen（solarforall）

```
页面URL: http://192.168.1.51:8080/solarforall
类型: home_improvement

成功: 页面出现 Thank You  

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
10. 点 Continue
```
