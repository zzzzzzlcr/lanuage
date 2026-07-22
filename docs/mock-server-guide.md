# Local Mock Server — 站点清单 & CDP 连接指南

**分支:** `feature/local-mock-server` | **工作区:** `.claude/worktrees/feature+local-mock-server`
**路径:** `/opt/skills/auto-farm-skill/mock_server/`

---

## 一、环境启动

```bash
# 全栈启动（Mock Server + Chrome + VNC）
cd /opt/skills/auto-farm-skill/.claude/worktrees/feature+local-mock-server
./start-mock-env.sh

# 仅 Mock Server
cd mock_server && python3 app.py &

# 查看 Chrome 操作
# 浏览器打开 http://192.168.1.51:6080/vnc.html
```

---

## 二、场景覆盖矩阵

| 测试场景 | 对应 mock | 复杂度 |
|---------|----------|--------|
| 正常表单（填表→提交→成功页） | tello, spree | ⭐⭐ |
| 字段名不匹配（data-field 不是 name） | reactapp | ⭐⭐⭐ |
| iframe 嵌套表单（childFrames） | entyrecare | ⭐⭐⭐ |
| 动态加载 / 需要 wait | datewhirl, livebeam | ⭐⭐ |
| radio / checkbox 非标准控件 | ctm, reactapp | ⭐⭐⭐ |
| 多页面跳转（text 找链接） | tello, ctm | ⭐⭐ |
| 年龄门 / 弹窗（modal 显示隐藏） | spree, livebeam | ⭐⭐ |
| React/Vue hash DOM（CSS modules） | reactapp | ⭐⭐⭐ |
| 随机选择（_click_random_label） | datewhirl | ⭐⭐ |

---

## 三、站点详情

### 1. entyrecare — iframe 多步表单 ⭐⭐⭐

```
入口: http://localhost:8080/
路由: / → /calculator → /forms/entyrecare → /forms/entyrecare/step/N → /hub/auth/verify
脚本: forms/sites/entyrecare.py
```

| 步骤 | URL | 操作 |
|------|-----|------|
| 首页 | `/` | 点击 "Check Eligibility" |
| 计算器 | `/calculator` | 含 `<iframe src="/forms/entyrecare">` |
| Step 1 | `/forms/entyrecare` | 点 "Ohio" |
| Step 2 | `step/2` | 填 ZIP + Next |
| Step 3-10 | `step/3`~`step/10` | 逐一点击选项 |
| Step 11 | `step/11` | firstName + lastName + Next |
| Step 12 | `step/12` | email + phone + 来源 + checkbox + Submit |
| 成功 | `/hub/auth/verify` | "Enter the code" |

**CDP 操作要点：**
- 必须 `cdp snapshot` 拿 `childFrames[0].frame.frameId`
- 所有 iframe 内操作传 `--frame-id`
- `_get_frame_id()` 每次操作前重新获取

```bash
cdp navi http://localhost:8080/
# 点 CTA 进入 /calculator
cdp eval "...click Check Eligibility..." --host 127.0.0.1 --port 9222
sleep 5
# 获取 frame_id
FID=$(cdp snapshot | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['childFrames'][0]['frame']['frameId'])")
# iframe 内操作
cdp eval "...click Ohio..." --frame-id "$FID"
cdp form "input" --value "44101" --frame-id "$FID"
```

---

### 2. datewhirl — SPA 动态标签 ⭐⭐

```
入口: http://localhost:8080/datewhirl
路由: /datewhirl → /news-feed
脚本: forms/sites/datewhirl.py
```

| 步骤 | 操作 |
|------|------|
| Cookie | 点 "Accept & Continue" |
| Quiz 1-8 | 随机点 `<label>` 选项（39 个可选） |
| 输入 1 | 填 firstname + Next |
| 输入 2 | 填 email + Next |
| 输入 3 | 填 password + Next |
| 最后 | "Thanks" → "I Accept" → "Find matches" |
| 成功 | `/news-feed`（"Welcome to Datewhirl"） |

**CDP 操作要点：**
- 全部在同一个页面，无页面跳转
- `_visible_labels()` 获取当前可见 `<label>`，随机选一个点
- 问题文本含 `?`，自动被 `_click_random_label` 跳过
- 输入框描述用 `<span>` 不是 `<label>`，防止误点

```bash
cdp navi http://localhost:8080/datewhirl
# 脚本自动处理所有交互
```

---

### 3. tello — 多页面跳转 ⭐⭐

```
入口: http://localhost:8080/tello
路由: /tello → /tello/plan → /tello/login → /tello/register → /account/checkout
脚本: forms/sites/tello.py
```

| 页面 | URL | 操作 |
|------|-----|------|
| 首页 | `/tello` | 点 "Get Unlimited Plan" |
| 套餐 | `/tello/plan` | 点 "I want this plan" |
| 登录 | `/tello/login` | 点 "I'm new" |
| 注册 | `/tello/register` | 填 `#i_first_name`, `#i_last_name`, `#i_login`, `#i_password`, `#i_confirm_password`, 勾 `#i_terms_and_conditions`, 点 "Join Tello" |
| 成功 | `/account/checkout` | |

```bash
cdp navi http://localhost:8080/tello
```

---

### 4. spree — 年龄门 + 弹窗 ⭐⭐

```
入口: http://localhost:8080/spree
路由: /spree → /spree/verify-gps → /spree/success
脚本: forms/sites/spree.py
```

| 步骤 | 操作 |
|------|------|
| 年龄门 | "Are you 21+" → 点 "Continue" |
| 弹窗 | modal 显示（初始 display:none） |
| 填表 | `#email` + `#password` + checkbox `termsAndPrivacy` |
| 提交 | 点 "Create Free Account" |
| GPS | "Enable Now" |
| 成功 | `/spree/success`（"my account" / "my rewards"） |

---

### 5. livebeam — DOM wizard ⭐⭐⭐

```
入口: http://localhost:8080/livebeam
路由: /livebeam → /auth/login
脚本: forms/sites/livebeam.py
```

| 步骤 | body text 关键词 | 操作 |
|------|-----------------|------|
| Cookie | | 点 "Accept all" |
| Age | "select your age" | 点年龄按钮（18-24 等） |
| Yes/No | "Yes" + "Skip" | 点第一个 button |
| Gender | "you want to meet" | 点 Man/Woman div |
| Multi | "choose all that apply" | 点 label，点 button |
| Name | "what name" / "call you" | 填 input，点 button |
| Email | "enter your email" | 填 input，点 button |
| Password | "create a secure password" | 填 input，点 button |
| Terms | "i accept" / "one last step" | 点 button |
| 成功 | `/auth/login` | |

**CDP 操作要点：**
- 所有步骤元素在 DOM 中，不显示/隐藏切换
- 通过 `document.body.innerText` 关键词判断当前步骤
- `_click_first_btn()` 点第一个可见 button

---

### 6. ctm — 保险 radio/checkbox 多页 ⭐⭐⭐

```
入口: http://localhost:8080/ctm
路由: /ctm → /ctm/health_quote_v4.jsp → /ctm/results
脚本: forms/sites/comparethemarket_health_v2.py
```

| 页面 | 操作 |
|------|------|
| 首页 `/ctm` | 点 "Compare Health Insurance" |
| About You | radio `#health_situation_healthCvr_*`, `#health_healthCover_income_*`, DOB 三字段, cover radio, T&C checkbox |
| Benefits | 8 个 checkbox `#cb0`~`#cb7` |
| Contact | name, email, phone |
| Results `#results` | 报价展示 |

**CDP 操作要点：**
- `_click_radio()` 通过 `dispatchEvent(new Event('change'))` 触发
- `_rand_check()` 随机勾选 checkbox
- `a.btn-next` 翻页

---

### 7. reactapp — React/Vue hash DOM ⭐⭐⭐

```
入口: http://localhost:8080/reactapp
路由: /reactapp → /reactapp/thank-you
脚本: 无（通用测试）
```

| 特点 | 示例 |
|------|------|
| hash ID | `jG8k2-mN4p-7xQw1`, `input-fN3vR-aG9tQ` |
| CSS module class | `_3kL9q_`, `_7mX2d_`, `_8tB4n_` |
| data-field 属性 | `data-field="postal_code"` |
| 无 `<label>` | 全用 `<span>` |
| styled div 替代 radio | `data-value="auto"` |
| hidden checkbox | `display:none` + wrapper toggle |
| Honeypot | `left:-9999px` 隐藏陷阱字段 |

```bash
cdp navi http://localhost:8080/reactapp
# 脚本只能靠 placeholder 文本、data-field 属性、元素文字内容来找元素
```

---

## 四、通用输入校验

所有站点均应用以下校验：

| 字段 | 规则 |
|------|------|
| ZIP | 只允许数字, 5 位, oninput 过滤 |
| 邮箱 | `/^[^\s@]+@[^\s@]+\.[^\s@]+$/` |
| 电话 | 只允许数字, ≥10 位, oninput 过滤 |
| 密码 | 6-8 位最小长度 |
| DOB | 月 1-12, 日 1-31, 年 1920-2010 |
| 必填 | 空字段拦截 + `#dc2626` 红色错误 |

---

## 五、设计原则

1. **`<label>` 只能用于可点击选项** — 输入框描述用 `<span>`，避免 `_visible_labels()` 误触
2. **问题文本含 `?`** — `_click_random_label` 自动跳过
3. **iframe 同源** — 全部 `localhost:8080`
4. **offsetWidth > 0** — 隐藏步骤元素自然被过滤
5. **body text 关键词匹配** — 每个 mock 的文本严格匹配对应脚本的检测条件
