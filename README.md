# 自然语言 → 表单自动化

运营写纯中文描述 → LLM 生成 JSON → 引擎执行

## 使用

```bash
./run.sh 描述文件.txt
```

或

```bash
echo "页面URL: https://xxx.com
类型: newsletter
成功: URL包含success

操作:
1. 等待2-4秒
2. 填写邮箱
3. 点击Subscribe" | ./run.sh
```

## 文件

- `docs/OPS-DESCRIPTION-GUIDE.md` - 运营描述规范
- `src/json_pipeline.py` - 生成→验证→修复 CLI
- `src/json_executor.py` - JSON 执行引擎
- `src/locator.py` - 语义字段定位器
