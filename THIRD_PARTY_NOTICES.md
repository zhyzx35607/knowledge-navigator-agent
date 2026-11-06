# 第三方来源与改造边界

本项目独立新增的存储、检索、API、模型适配、浏览器界面、测试和合成数据采用根目录 MIT 许可证。`navigator/protocol.py` 保留为 **Mulan PSL v2** 组件，不改署为 MIT。

## oiocns-react 的辅助检索协议

- 来源仓库：`https://gitee.com/Madeline35706/oiocns-react.git`
- 检查的本地 revision：`f13154e9`（完整 revision 见 `third_party/oiocns-react/SOURCE_REVISION.txt`）。
- 原始许可证：Mulan PSL v2；`third_party/oiocns-react/LICENSE` 从来源仓库原样保留。
- 参考源码：`src/pages/Home/components/Search/index.tsx` 的 `AssistantRecommendation`、`normalizeSources`、`parseRecommendation` 及提交后来源/入口白名单检查（检查时约 42–49、89–126、192–234、779–857 行）。
- 参考入口记录：`src/components/DataPreview/session/entryWhitelist/index.tsx` 的 `EntryWhitelistEntry` / `EntryWhitelistRecord`（检查时约 53–77 行）。
- 参考设计说明：`PR_RAG_INTERNAL_ARCHITECTURE.md`。

### 适配内容

保留“限定文件 → 证据检索 → 白名单入口 → 用户确认打开”的协议约束。将前端文件名/入口名匹配重新实现为后端稳定 ID、文档版本、内容 SHA256、原文片段与文档-入口关联校验。任一无效引用或越权入口导致整个结果被拒绝。模型只提交标准 JSON，不沿用原来的三行文本兼容解析。

没有复制原项目的 React 组件、业务文档、公司数据、私有依赖、内核认证、SignalR 连接、Git 历史或凭据。这个项目不依赖原平台服务，合成笔记均为新建。

MIT 与 Mulan PSL v2 的边界按文件标注；发布时应同时保留此说明及完整第三方许可证。
