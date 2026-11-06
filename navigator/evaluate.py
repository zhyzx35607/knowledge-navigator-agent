"""Run the production retrieval API against an isolated synthetic golden corpus."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from time import perf_counter

from fastapi.testclient import TestClient

from .main import create_app
from .model import ModelClient, ModelConfig
from .retrieval import EmbeddingClient, EmbeddingConfig


def run_evaluation(dataset_path, method="all"):
    raw = Path(dataset_path).read_bytes()
    dataset = json.loads(raw)
    embedding = EmbeddingClient(EmbeddingConfig.from_env())
    try:
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], stderr=subprocess.DEVNULL, text=True).strip())
    except (OSError, subprocess.CalledProcessError):
        revision, dirty = "unavailable", None
    implementation = hashlib.sha256()
    for source in sorted(Path(__file__).parent.glob("*.py")):
        implementation.update(source.name.encode())
        implementation.update(source.read_bytes())
    report = {"dataset_version": dataset["version"], "dataset_sha256": hashlib.sha256(raw).hexdigest(),
              "provenance": dataset["provenance"], "created_at": datetime.now(timezone.utc).isoformat(),
              "code_revision": revision, "working_tree_dirty": dirty,
              "implementation_sha256": implementation.hexdigest(),
              "python": platform.python_version(), "platform": platform.platform(),
              "k": 6, "case_count": len(dataset["cases"]), "concurrency": 1,
              "embedding_model": embedding.config.model if embedding.config.configured else None,
              "embedding_config_fingerprint": embedding.config.fingerprint if embedding.config.configured else None,
              "modes": {}}
    methods = ["bm25", "hybrid"] if method == "all" else [method]
    with tempfile.TemporaryDirectory(prefix="navigator-eval-") as folder:
        app = create_app(Path(folder) / "evaluation.db", model_client=ModelClient(ModelConfig()), embedding_client=embedding)
        with TestClient(app) as client:
            store = app.state.store
            for doc in store.documents():
                store.delete(doc["id"])
            collection_ids = {name: store.save_collection(name)["id"] for name in sorted({doc["collection"] for doc in dataset["documents"]})}
            for doc in dataset["documents"]:
                store.put(doc["id"], doc["title"], doc["body"], [], collection_id=collection_ids[doc["collection"]])
            for mode in methods:
                if mode == "hybrid" and not embedding.config.configured:
                    report["modes"][mode] = {"status": "skipped", "reason": "No real embedding endpoint configured; no dense metrics fabricated."}
                    continue
                rows, latencies = [], []
                for case in dataset["cases"]:
                    scope = set(case.get("document_ids", [doc["id"] for doc in dataset["documents"]]))
                    if "collections" in case:
                        scope &= {doc["id"] for doc in dataset["documents"] if doc["collection"] in case["collections"]}
                    payload = {"query": case["query"], "mode": "local", "retrieval_method": mode,
                               "document_ids": case.get("document_ids"), "entry_ids": []}
                    if "collections" in case:
                        payload["collection_ids"] = [collection_ids[name] for name in case["collections"]]
                    started = perf_counter()
                    response = client.post("/api/search", json=payload)
                    elapsed = (perf_counter() - started) * 1000
                    latencies.append(elapsed)
                    result = response.json() if response.is_success else response.json().get("detail", {})
                    citations = result.get("citations", [])[:report["k"]]
                    hits = [item["document_id"] for item in citations]
                    ranks = [index for index, doc_id in enumerate(hits, 1) if doc_id in case["relevant"]]
                    rows.append({"id": case["id"], "http_status": response.status_code, "status": result.get("status"),
                                 "expected_document_ids": case["relevant"], "retrieved_document_ids": hits,
                                 "hit": bool(ranks), "reciprocal_rank": 1 / min(ranks) if ranks else 0,
                                 "section_matches": sum(any(section in item["quote"] for item in citations) for section in case["sections"]),
                                 "section_count": len(case["sections"]), "scope_leaks": len(set(hits) - scope),
                                 "abstained": response.is_success and result.get("status") == "not_found" and not hits,
                                 "elapsed_ms": round(elapsed, 3), "retrieval": result.get("retrieval")})
                answerable = [row for row in rows if row["expected_document_ids"]]
                unanswerable = [row for row in rows if not row["expected_document_ids"]]
                section_count = sum(row["section_count"] for row in rows)
                report["modes"][mode] = {"status": "completed", "case_count": len(rows),
                    "answerable_count": len(answerable), "unanswerable_count": len(unanswerable),
                    "hit_at_k": sum(row["hit"] for row in answerable) / len(answerable) if answerable else None,
                    "mrr_at_k": sum(row["reciprocal_rank"] for row in answerable) / len(answerable) if answerable else None,
                    "section_recall_at_k": sum(row["section_matches"] for row in rows) / section_count if section_count else None,
                    "scope_leak_count": sum(row["scope_leaks"] for row in rows),
                    "unanswerable_abstention_rate": sum(row["abstained"] for row in unanswerable) / len(unanswerable) if unanswerable else None,
                    "error_count": sum(row["http_status"] != 200 for row in rows),
                    "latency_p50_ms": round(median(latencies), 3),
                    "latency_p95_ms": round(sorted(latencies)[math.ceil(0.95 * len(latencies)) - 1], 3),
                    "cases": rows}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("evals/golden.json"))
    parser.add_argument("--mode", choices=["all", "bm25", "hybrid"], default="all")
    parser.add_argument("--output", type=Path, default=Path("evals/results/latest.json"))
    args = parser.parse_args()
    report = run_evaluation(args.dataset, args.mode)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "modes": {mode: {key: value for key, value in values.items() if key != "cases"}
                                                          for mode, values in report["modes"].items()}}, ensure_ascii=False, indent=2))
    return 1 if any(mode.get("error_count", 0) or mode.get("scope_leak_count", 0) for mode in report["modes"].values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
