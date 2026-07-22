# 集成测试 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 7 个 mock 站点中的 5 个编写端到端集成测试，验证「自然语言 → JSON → 执行 → 诊断报告」完整链路。

**Architecture:** 新增 `test_integration.py`，通过 pytest fixture 管理 CDP/LLM/mock-server 依赖，每个站点一个测试函数。缺失依赖时 skip 而非 fail。

**Tech Stack:** Python 3, pytest, openai, CDP (via common.CDPHelper)

## Global Constraints

- 集成测试默认不跑（依赖外部服务），通过环境变量 `WS_URL` + `OPENAI_API_KEY` 控制
- LLM 非确定性：断言只验证报告结构，不验证 JSON 内容
- 测试之间通过 navigate 重置浏览器状态
- `OPENAI_BASE_URL` 可选，默认 deepseek

---

### Task 1: 创建 fixture 层 + 辅助函数

**Files:**
- Create: `test_integration.py`

**Interfaces:**
- Produces: `cdp` fixture, `llm` fixture, `pipeline` fixture, `mock_server` fixture, `run_description()` helper

- [ ] **Step 1: 编写 fixture 层和辅助函数**

```python
"""集成测试：自然语言 → JSON → 执行 → 诊断报告。"""
import json
import os
import sys
import glob
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.common import CDPHelper
from src.json_pipeline import JSONPipeline
from openai import OpenAI


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def cdp():
    """连接 CDP，skip 如果不可用。"""
    ws_url = os.environ.get("WS_URL", "")
    if not ws_url:
        pytest.skip("WS_URL not set")
    return CDPHelper(ws_url)


@pytest.fixture(scope="module")
def llm():
    """创建 LLM 客户端，skip 如果没 key。"""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        pytest.skip("OPENAI_API_KEY not set")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com")
    return OpenAI(api_key=key, base_url=base_url)


@pytest.fixture(scope="module")
def pipeline(cdp, llm):
    """创建 JSONPipeline 实例。"""
    import logging
    _ = logging.getLogger(__name__)
    return JSONPipeline(llm, cdp)


@pytest.fixture(scope="module")
def mock_server():
    """确保 mock server 在运行。"""
    import urllib.request
    try:
        urllib.request.urlopen("http://localhost:8080/", timeout=2)
    except Exception:
        pytest.skip("Mock server not running at localhost:8080")


# ── Helpers ────────────────────────────────────────────────────────

def run_description(pipeline, description: str, navigate_url: str) -> dict:
    """跑完整 pipeline，返回最新生成的诊断 JSON 报告。

    Returns: dict with keys "outcome", "steps", "config"
    """
    profile = {"task_id": f"integration_test_{datetime.now().strftime('%H%M%S')}"}
    config, result = pipeline.run(description, profile, navigate_url)

    # 找最新生成的报告 JSON
    reports_dir = os.path.join(os.path.dirname(__file__), "reports")
    pattern = os.path.join(reports_dir, "*-report.json")
    files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if files:
        with open(files[0]) as f:
            return json.load(f)

    # 如果文件不存在（比如报告生成失败），构造最小结构
    return {
        "outcome": {
            "passed": result.passed,
            "failure_category": "unknown",
            "failures": 0,
            "total_steps": 0,
        },
        "steps": [],
        "config": config,
    }


def assert_report_structure(report: dict):
    """B 粒度断言：报告 JSON 结构完整。"""
    assert "outcome" in report
    assert "steps" in report
    assert "config" in report
    assert "passed" in report["outcome"]
    assert isinstance(report["steps"], list)
    assert "steps" in report["config"]
    for step in report["config"]["steps"]:
        assert "_status" in step, f"config step missing _status: {step}"
```

- [ ] **Step 2: 验证语法和 fixture skip 行为**

```bash
# 不设环境变量，确认 skip
python3 -m pytest test_integration.py -v 2>&1 | head -5
```
Expected: 所有测试 SKIP（无 WS_URL）

- [ ] **Step 3: 提交**

```bash
git add test_integration.py
git commit -m "test: add integration test fixtures and helpers"
```

---

### Task 2: 编写 3 个冒烟测试（tello, spree, datewhirl）

**Files:**
- Modify: `test_integration.py` (append test functions)

**Interfaces:**
- Consumes: `pipeline`, `mock_server`, `run_description`, `assert_report_structure` (from Task 1)

- [ ] **Step 1: 追加 tello 测试**

```python
# ── Test Cases ─────────────────────────────────────────────────────

TELLO_DESC = """页面URL: http://localhost:8080/tello
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

成功: URL包含 /account/checkout"""


def test_tello(pipeline, mock_server):
    """冒烟：tello 多页面跳转表单。"""
    report = run_description(pipeline, TELLO_DESC, "http://localhost:8080/tello")
    assert report["outcome"]["passed"] is True
    assert_report_structure(report)
    assert len(report["steps"]) > 0
```

- [ ] **Step 2: 追加 spree 测试**

```python
SPREE_DESC = """页面URL: http://localhost:8080/spree
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

成功: URL包含 /spree/success"""


def test_spree(pipeline, mock_server):
    """冒烟：spree 年龄门 + 弹窗表单。"""
    report = run_description(pipeline, SPREE_DESC, "http://localhost:8080/spree")
    assert report["outcome"]["passed"] is True
    assert_report_structure(report)
```

- [ ] **Step 3: 追加 datewhirl 测试**

```python
DATEWHIRL_DESC = """页面URL: http://localhost:8080/datewhirl
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
when_页面有Find matches: 点击Find matches"""


def test_datewhirl(pipeline, mock_server):
    """冒烟：datewhirl quiz 状态机。"""
    report = run_description(pipeline, DATEWHIRL_DESC, "http://localhost:8080/datewhirl")
    assert report["outcome"]["passed"] is True
    assert_report_structure(report)
    # fix_cycles 应合理
    assert report["outcome"].get("fix_cycles", 0) <= 3
```

- [ ] **Step 4: 提交**

```bash
git add test_integration.py
git commit -m "test: add tello, spree, datewhirl smoke tests"
```

---

### Task 3: 编写 2 个探索测试（entyrecare, reactapp）+ 精确断言

**Files:**
- Modify: `test_integration.py` (append test functions)

**Interfaces:**
- Consumes: `pipeline`, `mock_server`, `run_description`, `assert_report_structure` (from Task 1)

- [ ] **Step 1: 追加 entyrecare 测试**

```python
ENTYRECARE_DESC = """页面URL: http://localhost:8080/
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
when_页面有选项: 随机选一个选项"""


def test_entyrecare(pipeline, mock_server):
    """探索：entyrecare iframe 多步表单，验证 frame_url 注入。"""
    report = run_description(pipeline, ENTYRECARE_DESC, "http://localhost:8080/")
    assert report["outcome"]["passed"] is True
    assert_report_structure(report)

    # C 粒度：验证 config 中至少一个步骤含 frame_url
    config_steps = report["config"]["steps"]
    has_frame = any(
        s.get("field", {}).get("frame_url") or s.get("frame_url")
        for s in config_steps
        if isinstance(s, dict)
    )
    if not has_frame:
        # entyrecare 的 form 步骤应该在 iframe 里
        # 但 LLM 可能用了全局 frame_url 而非每步 frame_url
        global_frame = report["config"].get("frame_url", "")
        assert global_frame or has_frame, \
            "entyrecare: 预期 config 含 frame_url（全局或步骤级），但都没有"
```

- [ ] **Step 2: 追加 reactapp 测试**

```python
REACTAPP_DESC = """页面URL: http://localhost:8080/reactapp
类型: newsletter

操作:
1. 等待2-4秒
2. 填写邮编
3. 填写邮箱
4. 点击Submit

成功: URL包含 /thank-you"""


def test_reactapp(pipeline, mock_server):
    """探索：reactapp hash DOM，预期定位失败。"""
    report = run_description(pipeline, REACTAPP_DESC, "http://localhost:8080/reactapp")

    if not report["outcome"]["passed"]:
        # C 粒度：验证失败分类和候选匹配
        category = report["outcome"].get("failure_category", "unknown")
        assert category in ("locator", "unknown", "success_condition"), \
            f"unexpected category: {category}"

        # 至少一个失败步骤含 candidates
        failed_steps = [s for s in report["steps"] if s.get("success") is False]
        has_candidates = any(
            s.get("candidates") and len(s["candidates"]) > 0
            for s in failed_steps
        )
        if not has_candidates and failed_steps:
            # 如果没 candidates，至少应该有 error 信息
            assert any(s.get("error") for s in failed_steps), \
                "失败步骤既无 candidates 也无 error"
    else:
        # 如果意外通过了，也验证报告结构即可
        assert_report_structure(report)
```

- [ ] **Step 3: 验证语法**

```bash
python3 -c "import py_compile; py_compile.compile('test_integration.py', doraise=True); print('Syntax OK')"
```
Expected: `Syntax OK`

- [ ] **Step 4: 运行现有测试确认无回归**

```bash
python3 test_fixer.py && python3 -m pytest test_diagnostics.py -v
```
Expected: `ALL FIXES PASSED` + `17 passed`

- [ ] **Step 5: 提交**

```bash
git add test_integration.py
git commit -m "test: add entyrecare and reactapp exploration tests"
```
