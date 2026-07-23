"""Automated coverage tests — NL descriptions against mock pages. Run:
   WS_URL=ws://127.0.0.1:9222/... OPENAI_API_KEY=sk-xxx pytest test_mock_coverage.py -v
"""
import pytest, json, os, time, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
from common import CDPHelper
from json_pipeline import JSONPipeline

# ── Test cases: (name, description, expected_success) ──

TEST_CASES = [
    # ─── Tier 1: Core fixes ───
    ("mui-textfield", """
页面URL: http://localhost:8080/mui-textfield
类型: newsletter
成功: 页面出现 Thank you

操作:
1. 等待2-3秒
2. 填写First Name
3. 等待0.5秒
4. 填写Last Name
5. 等待0.5秒
6. 填写Email
7. 等待0.5秒
8. 填写Phone
9. 等待0.5秒
10. 点击Submit
11. 等待3-5秒
""", True),

    ("mui-select", """
页面URL: http://localhost:8080/mui-select
类型: newsletter
成功: 页面出现 Selected: California

操作:
1. 等待2-3秒
2. 选择State（下拉框，选California）
3. 等待0.5秒
4. 点击Submit
5. 等待3-5秒
""", True),

    ("mui-checkbox", """
页面URL: http://localhost:8080/mui-checkbox
类型: newsletter
成功: 页面出现 Thank you

操作:
1. 等待2-3秒
2. 勾选同意条款
3. 等待0.5秒
4. 点击Submit
5. 等待3-5秒
""", True),

    ("spa-steps", """
页面URL: http://localhost:8080/spa-steps
类型: newsletter
成功: 页面出现 Complete

操作:
1. 等待2-3秒
2. 填写ZIP
3. 等待1秒
4. 点击Continue
5. 等待3-5秒
6. 填写Name
7. 等待1秒
8. 点击Continue
9. 等待3-5秒
10. 填写Email
11. 等待1秒
12. 点击Submit
13. 等待3-5秒
""", True),

    # ─── Tier 2: Real-site mock replicas ───
    ("removemenow", """
页面URL: http://localhost:8080/removemenow/freescan
类型: newsletter
成功: 页面出现 Scan Complete

操作:
1. 等待2-3秒
2. 填写First Name
3. 等待0.5秒
4. 填写Last Name
5. 等待0.5秒
6. 填写Email
7. 等待0.5秒
8. 填写Age
9. 等待0.5秒
10. 填写ZIP Code
11. 等待0.5秒
12. 点击Scan Now for Free
13. 等待3-5秒
""", True),

    ("modform", """
页面URL: http://localhost:8080/modform
类型: newsletter
成功: 页面出现 Thank you

操作:
1. 等待2-4秒
2. 填写First Name
3. 等待0.5秒
4. 填写Last Name
5. 等待0.5秒
6. 填写Email
7. 等待0.5秒
8. 填写Phone
9. 等待0.5秒
10. 点击Submit
11. 等待3-5秒
""", True),
]


class TestMockCoverage:
    """Run NL descriptions against mock sites, verify pass/fail."""

    @pytest.fixture(scope="class")
    def cdp(self):
        ws_url = os.environ.get("WS_URL")
        if not ws_url:
            pytest.skip("WS_URL not set")
        return CDPHelper(ws_url)

    @pytest.fixture(scope="class")
    def llm(self):
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            pytest.skip("OPENAI_API_KEY not set")
        from openai import OpenAI
        return OpenAI(api_key=key, base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"))

    @pytest.fixture(scope="class")
    def pipeline(self, llm, cdp):
        return JSONPipeline(llm, cdp)

    @pytest.mark.parametrize("name,desc,expected", TEST_CASES)
    def test_mock_page(self, pipeline, cdp, name, desc, expected):
        # Extract URL from description first line
        url = "http://localhost:8080/"
        for line in desc.strip().split("\n"):
            if "URL:" in line or "http" in line:
                url = line.split("URL:")[-1].strip() if "URL:" in line else line.strip()
                url = url.split()[0] if url else url
                break
        # Use eval for fast navigation (cdp navi can timeout on localhost)
        cdp.eval(f"(function(){{window.location.href='{url}';}})()")
        import time; time.sleep(2)
        # Suppress alert/confirm/prompt on mock pages (they block CDP)
        cdp.eval("window.alert=function(){};window.confirm=function(){return true;};window.prompt=function(){return'';};")
        config, result = pipeline.run(desc.strip(), {"task_id": f"test_{name}"})
        passed = result.passed
        assert passed == expected, (
            f"{name}: expected {'PASS' if expected else 'FAIL'}, got {'PASS' if passed else 'FAIL'}\n"
            f"URL: {result.final_url}\nBody: {(result.final_body or '')[:200]}"
        )


if __name__ == "__main__":
    # Quick manual run
    ws = os.environ.get("WS_URL")
    key = os.environ.get("OPENAI_API_KEY")
    if not ws or not key:
        print("Set WS_URL and OPENAI_API_KEY")
        sys.exit(1)

    from openai import OpenAI
    cdp = CDPHelper(ws)
    llm = OpenAI(api_key=key, base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"))
    pipeline = JSONPipeline(llm, cdp)

    passed = 0
    for name, desc, expected in TEST_CASES:
        print(f"\n{'='*40}\n{name}\n{'='*40}")
        try:
            config, result = pipeline.run(desc.strip(), {"task_id": f"test_{name}"})
            ok = result.passed
            status = "✅" if ok == expected else "❌"
            print(f"{status} {name}: {'PASS' if ok else 'FAIL'} (expected {'PASS' if expected else 'FAIL'})")
            if ok == expected:
                passed += 1
        except Exception as e:
            print(f"❌ {name}: ERROR - {e}")

    print(f"\n{'='*40}\nTotal: {passed}/{len(TEST_CASES)} passed")
