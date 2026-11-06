"""Scope/source/entry gate adapted from oiocns-react's auxiliary search protocol.

SPDX-License-Identifier: MulanPSL-2.0
See THIRD_PARTY_NOTICES.md for source revision and adaptation boundaries.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    answer: str = Field(min_length=1, max_length=8000)
    citation_ids: list[str] = Field(min_length=1, max_length=6)
    entry_ids: list[str] = Field(default_factory=list, max_length=10)


class InvalidResult(ValueError):
    pass


def validate_result(proposal: Proposal, evidence: list[dict], scope: list[str],
                    allowed_entries: list[dict], store) -> dict:
    """Reject the entire result on any invented, stale or out-of-scope item.

    The old UI matched source filenames and entry names. This backend instead uses
    server-issued citation IDs, current content hashes and document-entry links.
    """
    sources = {item["citation_id"]: item for item in evidence}
    allowed = {item["id"]: item for item in allowed_entries}
    citations = []
    supported_entries = set()
    for citation_id in dict.fromkeys(proposal.citation_ids):
        item = sources.get(citation_id)
        if not item or item["document_id"] not in scope:
            raise InvalidResult("引用不属于本次检索证据或授权文档范围")
        current = store.get(item["document_id"])
        if (not current or current["version"] != item["version"]
                or current["digest"] != item["digest"]
                or item["quote"] not in current["body"]):
            raise InvalidResult("引用文档已更新、删除或原文不匹配，请重新检索")
        supported_entries.update(current["entry_ids"])
        citations.append({key: item[key] for key in
                          ("citation_id", "document_id", "title", "version", "digest", "quote")})
    entries = []
    for entry_id in dict.fromkeys(proposal.entry_ids):
        if entry_id not in allowed or entry_id not in supported_entries:
            raise InvalidResult("推荐入口不在本次白名单中，或缺少引用文档支持")
        entries.append(allowed[entry_id])
    return {"answer": proposal.answer, "citations": citations, "entries": entries}
