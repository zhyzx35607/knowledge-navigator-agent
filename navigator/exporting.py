"""Readable exports of immutable research snapshots with live freshness labels."""
import json
import re


def block(text):
    text = str(text)
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
    return f"{fence}text\n{text}\n{fence}"


def research_markdown(record):
    labels = {"stale": "存在过期引用，请重新检索", "current": "引用与当前文档一致", "none": "没有引用证据"}
    lines = ["# 研究记录", "", f"记录 ID：{record['research_id']}", f"保存时间：{record['recorded_at']}",
             f"复核时间：{record['checked_at']}", f"引用状态：{labels[record['evidence_status']]}",
             "", "这是保存时的检索快照；复核仅检查引用版本、哈希和原文，不验证回答语义。", "", "## 问题", "",
             block(record["query"]), "", "## 检索设置", "", block(json.dumps({key: record.get(key) for key in (
                 "mode", "status", "scope_document_ids", "scope_collection_ids", "scope_entry_ids", "model_calls",
                 "retrieval", "parent_research_id", "continuation_context")},
                 ensure_ascii=False, indent=2)), "", "## 回答", "", block(record.get("answer", record.get("message", "本次未生成回答。")))]
    for index, item in enumerate(record.get("citations", []), 1):
        lines += ["", f"## 引用 {index}", "", "状态：" + (item["stale_reason"] if item["stale"] else "当前版本一致"),
                  block(f"{item['title']}\n{item['citation_id']}\nSHA256 {item['digest']}"), "", block(item["quote"])]
    lines += ["", "## 保存时的动作摘要", "", block(json.dumps(record.get("trace", []), ensure_ascii=False, indent=2)), ""]
    return "\n".join(lines)
