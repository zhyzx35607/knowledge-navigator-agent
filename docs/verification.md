# v0.2 本地验证记录

日期：2026-11-06，Windows / Python 3.12。以下来自实际运行。

- `.venv\Scripts\python.exe -m pytest -q`：71 passed，1条已有Starlette/httpx弃用警告；10.72秒。
- `node --check navigator/static/app.js`：通过。
- `node --test tests/ui_races.cjs`：4 passed。覆盖历史乱序响应、搜索与历史竞争、过期记录文本渲染、文件拖放进入导入审阅。
- `python -m navigator.evaluate --output evals/results/bm25-v02.json`：20题，BM25 Hit@6/MRR@6/SectionRecall@6均0.9333；范围泄漏0；5条无答案拒答全部通过；API错误0。语义改写q15保留未命中。真实embedding未配置，hybrid skipped。
- 评测p50/p95、语料哈希、代码revision和逐例结果见JSON。工作区改动尚未提交时revision仅表示基准提交；不能将其当成新代码已发布的证据。
- Edge真实操作：创建集合；通过文件选择器一次选择并导入examples两文件；按集合搜索“研究闭环”；自动保存研究；显式续问“有哪些步骤”，显示上一轮公开上下文和范围；编辑原文后显示3条引用已过期；下载Markdown且文件中包含“存在过期引用，请重新检索”。
- 浏览器截图：`docs/screenshots/v02-research.jpg`。全页截图接口超时，保存的是实际可见页面截图。
- 预览端口8794，数据库 `data/preview-v02.sqlite3`，含本次合成演示资料；默认启动端口仍8792、默认库仍data/knowledge.sqlite3。

后端新增回归包含旧库幂等迁移、导入编码/数量/字节/请求上限、事务写入故障回滚、集合生命周期与范围交集、旧客户端保存保留集合、历史跨重启/删除/导出、续问上下文范围隔离与过期拒绝、真实embedding HTTP契约的MockTransport测试、版本/模型缓存隔离、候选版本竞态与数值稳定。

模型和向量接口测试用明确的MockTransport；没有联网进行真实模型/向量效果评测。生产默认BM25，混合模式无配置时明确报告BM25。远程CI状态以Actions为准。
