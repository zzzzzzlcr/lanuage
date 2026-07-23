"""Batch test mock pages with verified success conditions."""
import sys,os,time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
from common import CDPHelper
from json_pipeline import JSONPipeline
from openai import OpenAI

CASES = [
    # ─── Tier 1: Core component tests (verified passing) ───
    ("mui-textfield", "http://localhost:8080/mui-textfield",
     "成功: 页面出现 Thank you",
     "1. 等待2-3秒\n2. 填写First Name\n3. 等待0.5秒\n4. 填写Last Name\n5. 等待0.5秒\n6. 填写Email\n7. 等待0.5秒\n8. 填写Phone\n9. 等待0.5秒\n10. 点击Submit\n11. 等待3-5秒"),

    ("mui-select", "http://localhost:8080/mui-select",
     "成功: 页面出现 Selected",
     "1. 等待2-3秒\n2. 选择State（下拉框，选California）\n3. 等待0.5秒\n4. 点击Submit\n5. 等待3-5秒"),

    ("mui-checkbox", "http://localhost:8080/mui-checkbox",
     "成功: 页面出现 Thank you",
     "1. 等待2-3秒\n2. 勾选同意条款\n3. 等待0.5秒\n4. 点击Submit\n5. 等待3-5秒"),

    ("spa-steps", "http://localhost:8080/spa-steps",
     "成功: 页面出现 Complete",
     "1. 等待2-3秒\n2. 填写ZIP\n3. 等待1秒\n4. 点击Continue\n5. 等待3-5秒\n6. 填写Name\n7. 等待1秒\n8. 点击Continue\n9. 等待3-5秒\n10. 填写Email\n11. 等待1秒\n12. 点击Submit\n13. 等待3-5秒"),

    ("removemenow", "http://localhost:8080/removemenow/freescan",
     "成功: 页面出现 Scan Complete",
     "1. 等待2-3秒\n2. 填写First Name\n3. 等待0.5秒\n4. 填写Last Name\n5. 等待0.5秒\n6. 填写Email\n7. 等待0.5秒\n8. 填写Age\n9. 等待0.5秒\n10. 填写ZIP Code\n11. 等待0.5秒\n12. 点击Scan Now for Free\n13. 等待3-5秒"),

    # ─── Tier 2: Simple single-page forms ───
    ("range-slider", "http://localhost:8080/range-slider",
     "成功: 页面出现 Thank you",
     "1. 等待2-3秒\n2. 拖动债务金额到最大值\n3. 等待0.5秒\n4. 点击Submit\n5. 等待3-5秒"),

    ("no-label-form", "http://localhost:8080/no-label-form",
     "成功: 页面出现 Form submitted",
     "1. 等待2-3秒\n2. 填写ZIP Code\n3. 等待0.5秒\n4. 填写Email Address\n5. 等待0.5秒\n6. 填写Phone Number\n7. 等待0.5秒\n8. 点击Submit\n9. 等待3-5秒"),

    ("casinospin", "http://localhost:8080/casinospin",
     "成功: 页面出现 You won",
     "1. 等待2-3秒\n2. 填写Email Address\n3. 等待0.5秒\n4. 填写Password\n5. 等待0.5秒\n6. 勾选同意条款\n7. 等待0.5秒\n8. 点击Spin Now\n9. 等待3-5秒"),

    ("fishinvest", "http://localhost:8080/fishinvest",
     "成功: 页面出现 Your Guide is Ready",
     "1. 等待2-3秒\n2. 填写Full Name\n3. 等待0.5秒\n4. 填写生日\n5. 等待0.5秒\n6. 填写密码\n7. 等待0.5秒\n8. 点击Get My Free Guide\n9. 等待3-5秒"),

    ("garagefloor", "http://localhost:8080/garagefloor",
     "成功: 页面出现 Your Estimate is Ready",
     "1. 等待2-3秒\n2. 填写ZIP\n3. 等待0.5秒\n4. 点击Get Estimate\n5. 等待3-5秒\n6. 填写Full Name\n7. 等待0.5秒\n8. 填写Email\n9. 等待0.5秒\n10. 填写Phone\n11. 等待0.5秒\n12. 点击Submit\n13. 等待3-5秒"),

    ("geminihealth", "http://localhost:8080/geminihealth",
     "成功: 页面出现 Results sent",
     "1. 等待2-3秒\n2. 填写邮箱\n3. 等待0.5秒\n4. 点击Get My Results\n5. 等待3-5秒"),

    ("protectsav", "http://localhost:8080/protectsav",
     "成功: 页面出现 Thank You",
     "1. 等待2-3秒\n2. 填写First Name\n3. 等待0.5秒\n4. 填写Last Name\n5. 等待0.5秒\n6. 填写Phone Number\n7. 等待0.5秒\n8. 填写Email Address\n9. 等待0.5秒\n10. 点击Get My Free Quote\n11. 等待3-5秒"),

    ("modform", "http://localhost:8080/modform",
     "成功: 页面出现 Thank You",
     "1. 等待2-3秒\n2. 填写邮箱\n3. 等待0.5秒\n4. 勾选同意条款\n5. 等待0.5秒\n6. 点击Subscribe\n7. 等待3-5秒"),

    ("carwarranty", "http://localhost:8080/carwarranty",
     "成功: 页面出现 Your quotes are ready",
     "1. 等待2-3秒\n2. 选择年份（下拉框，选2020）\n3. 等待0.5秒\n4. 点击Continue\n5. 等待2-3秒\n6. 选择品牌（下拉框，选Toyota）\n7. 等待0.5秒\n8. 点击Continue\n9. 等待2-3秒\n10. 选择型号（下拉框，选Camry）\n11. 等待0.5秒\n12. 点击Continue\n13. 等待2-3秒\n14. 填写Mileage\n15. 等待0.5秒\n16. 点击Continue\n17. 等待3-5秒\n18. 填写Full Name\n19. 等待0.5秒\n20. 填邮箱\n21. 等待0.5秒\n22. 填写Phone\n23. 等待0.5秒\n24. 点击Get My Quote\n25. 等待3-5秒"),

    # ─── Tier 3: Additional single-page forms ───
    ("solarforall", "http://localhost:8080/solarforall",
     "成功: 页面出现 Thank You",
     "1. 等待2-3秒\n2. 填写First Name\n3. 等待0.5秒\n4. 填写Last Name\n5. 等待0.5秒\n6. 填写Email\n7. 等待0.5秒\n8. 填写Phone\n9. 等待0.5秒\n10. 填写Address\n11. 等待0.5秒\n12. 勾选同意条款\n13. 等待0.5秒\n14. 点击Submit\n15. 等待3-5秒"),

    ("showerlead", "http://localhost:8080/showerlead",
     "成功: 页面出现 Your Estimate is Ready",
     "1. 等待2-3秒\n2. 选择房主身份\n3. 等待0.5秒\n4. 填写ZIP\n5. 等待0.5秒\n6. 点击Get My Estimate\n7. 等待3-5秒\n8. 填写Full Name\n9. 等待0.5秒\n10. 填写Phone\n11. 等待0.5秒\n12. 点击Submit\n13. 等待3-5秒"),

    ("nexaralai", "http://localhost:8080/nexaralai/contact",
     "成功: 页面出现 Message Sent",
     "1. 等待2-3秒\n2. 填写姓名\n3. 等待0.5秒\n4. 填写邮箱\n5. 等待0.5秒\n6. 填写留言\n7. 等待0.5秒\n8. 点击Send Message\n9. 等待3-5秒"),

    ("tarotcard", "http://localhost:8080/tarotcard",
     "成功: 页面出现 Your Reading is Complete",
     "1. 等待2-3秒\n2. 填写姓名\n3. 等待0.5秒\n4. 点击Get My Reading\n5. 等待3-5秒"),

    ("freedomdebt", "http://localhost:8080/freedomdebt",
     "成功: 页面出现 Congratulations",
     "1. 等待2-3秒\n2. 选择债务金额（滚动条）\n3. 等待0.5秒\n4. 点击Continue\n5. 等待3-5秒\n6. 填写First Name\n7. 等待0.5秒\n8. 填写Last Name\n9. 等待0.5秒\n10. 填写Email\n11. 等待0.5秒\n12. 填写Phone\n13. 等待0.5秒\n14. 点击Continue\n15. 等待3-5秒\n16. 填写Address\n17. 等待0.5秒\n18. 填写City\n19. 等待0.5秒\n20. 填写ZIP\n21. 等待0.5秒\n22. 点击Continue\n23. 等待3-5秒\n24. 填写SSN\n25. 等待0.5秒\n26. 勾选同意条款\n27. 等待0.5秒\n28. 点击Check My Eligibility\n29. 等待3-5秒"),

    ("healthwindow", "http://localhost:8080/healthwindow",
     "成功: 页面出现 DONE",
     "1. 等待2-3秒\n2. 填写First Name\n3. 等待0.5秒\n4. 填写Last Name\n5. 等待0.5秒\n6. 填写Email\n7. 等待0.5秒\n8. 填写Phone\n9. 等待0.5秒\n10. 填写Address\n11. 等待0.5秒\n12. 填写City\n13. 等待0.5秒\n14. 填写ZIP\n15. 等待0.5秒\n16. 点击Submit\n17. 等待3-5秒"),

    ("lilacworks", "http://localhost:8080/lilacworks",
     "成功: 页面出现 Thank you",
     "1. 等待2-3秒\n2. 选择Home Type（下拉框）\n3. 等待0.5秒\n4. 选择Province（下拉框）\n5. 等待0.5秒\n6. 填写Monthly Bills\n7. 等待0.5秒\n8. 点击Estimate Savings\n9. 等待3-5秒\n10. 填写First Name\n11. 等待0.5秒\n12. 填写Last Name\n13. 等待0.5秒\n14. 填写Email\n15. 等待0.5秒\n16. 填写Phone\n17. 等待0.5秒\n18. 勾选同意条款\n19. 等待0.5秒\n20. 点击Get My Results\n21. 等待3-5秒"),

    ("seniorbath", "http://localhost:8080/seniorbath/form",
     "成功: 页面出现 Thank You",
     "1. 等待2-3秒\n2. 填写ZIP\n3. 等待0.5秒\n4. 点击Get Estimate\n5. 等待3-5秒\n6. 填写Full Name\n7. 等待0.5秒\n8. 填写Phone\n9. 等待0.5秒\n10. 点击Submit\n11. 等待3-5秒"),

    ("renttoown", "http://localhost:8080/renttoown",
     "成功: 页面出现 Thank You",
     "1. 等待2-3秒\n2. 填写First Name\n3. 等待0.5秒\n4. 填写Last Name\n5. 等待0.5秒\n6. 填写Email\n7. 等待0.5秒\n8. 填写Phone\n9. 等待0.5秒\n10. 填写Address\n11. 等待0.5秒\n12. 填写City\n13. 等待0.5秒\n14. 填写ZIP\n15. 等待0.5秒\n16. 点击Submit\n17. 等待3-5秒"),
]

def run():
    ws = os.environ.get("WS_URL", "ws://127.0.0.1:9222/devtools/browser/a650ef37-8c06-4bd1-a8a2-9b4c52a455f9")
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        print("Set OPENAI_API_KEY"); return

    cdp = CDPHelper(ws)
    llm = OpenAI(api_key=key, base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"))
    p = JSONPipeline(llm, cdp)

    passed = 0
    for name, url, success, ops in CASES:
        desc = f"页面URL: {url}\n类型: newsletter\n{success}\n\n操作:\n{ops}"
        try:
            cdp.eval("window.alert=function(){};window.confirm=function(){return true;};window.prompt=function(){return '';};")
            time.sleep(0.2)
            cdp.eval(f"(function(){{window.location.href='{url}';}})()")
            time.sleep(2)
            cdp.eval("window.alert=function(){};window.confirm=function(){return true;};window.prompt=function(){return '';};")
            config, result = p.run(desc.strip(), {'task_id': name}, url)
            ok = result.passed
            if ok: passed += 1
            print(f"{'✅' if ok else '❌'} {name} ({passed}/{len(CASES)})")
            if not ok:
                for s in getattr(result, 'failed_steps', [])[:2]:
                    print(f"    Step {s.index}: {s.error[:100]}")
            sys.stdout.flush()
        except Exception as e:
            print(f"❌ {name}: ERROR {e}")
            sys.stdout.flush()

    print(f"\n{'='*50}")
    print(f"Total: {passed}/{len(CASES)} passed ({passed*100//len(CASES)}%)")

if __name__ == "__main__":
    run()
