from pathlib import Path

from navigator.evaluate import run_evaluation


def test_golden_evaluation_uses_production_scope_and_skips_unconfigured_dense(monkeypatch):
    for name in ("EMBEDDING_BASE_URL", "EMBEDDING_NAME", "EMBEDDING_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    report = run_evaluation(Path(__file__).parents[1] / "evals/golden.json")
    baseline = report["modes"]["bm25"]
    assert report["case_count"] == 20 and report["k"] == 6
    assert baseline["answerable_count"] == 15 and baseline["unanswerable_count"] == 5
    assert baseline["error_count"] == 0 and baseline["scope_leak_count"] == 0
    assert baseline["hit_at_k"] == sum(item["hit"] for item in baseline["cases"] if item["expected_document_ids"]) / 15
    assert report["modes"]["hybrid"]["status"] == "skipped"
    assert "hit_at_k" not in report["modes"]["hybrid"]
