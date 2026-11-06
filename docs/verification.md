# 本地验证记录

日期：2026-11-06。环境：Windows，Python 3.12.10，SQLite 3.49.1。

- `.venv\Scripts\python.exe -m pytest -q`：44 项通过。覆盖检索命中/未命中、范围限制、来源原文与哈希、白名单拒绝、文档更新删除、版本冲突、多轮搜索改写、真实 read、伪造/未读/过期片段、工具范围逃逸、最大步数、模型超时/HTTP/格式异常、静态页面和工作台 API。
- `python -m compileall -q navigator`：通过。
- `node --check navigator/static/app.js`：通过；PowerShell 启动脚本解析通过。
- `scripts/setup.ps1`：完整安装流程通过；`scripts/start.ps1 -Port 8793` 启动成功，健康检查为 `ok`，测试服务随后停止。
- FastAPI 已在 `127.0.0.1:8792` 成功启动并完成真实 HTTP 访问。
- Edge 界面验证：每周复盘真实检索；只选择复盘文档查询 RAG 返回未找到；选择未配置的模型模式显示明确错误。
- 页面截图保存在 `docs/screenshots/local-search.jpg`。截图中有真实命中的文档标题、版本、原文与来源标识。
- 复制的第三方 LICENSE 与来源仓库 LICENSE 的 SHA256 相同。

模型测试采用 `httpx.MockTransport` 确定性驱动 search/read/final，工具确实读取临时 SQLite 数据库；没有配置或调用真实外部模型，因此不声明模型联网成功。44 项包含并发版本冲突与非法模型地址回归测试。

测试有一条来自当前 Starlette/TestClient 的 httpx 弃用警告，不影响 44 项结果；后续迁移测试客户端可单独处理。

浏览器已完成“搜索每周复盘 → 查看真实引用 → 点击入口 → 页面内确认 → 到达 /workspace/journal 并展示关联文档”的实际验收。Agent 工具轨迹由协议和 API 测试覆盖，尚未用真实模型做浏览器端到端验收。

交叉审查修复了并发编辑的乐观锁竞态：版本读取之前执行 BEGIN IMMEDIATE，同一旧版本的两个真实并发 PUT 分别返回 200 和 409，获胜正文、版本、哈希与 FTS 索引保持一致。非法模型地址返回固定脱敏配置错误，不回显配置值或发起网络请求。

GitHub Actions 配置 Windows/Linux × Python 3.11/3.12，实际远程状态以仓库 Actions 为准。
