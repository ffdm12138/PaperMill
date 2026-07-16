from pathlib import Path

from src.discovery.relevance import OpenAlexDoiVerifier, RawOpenAlexWorkCache


def test_openalex_verifier_caches_raw_work_and_replays_profile_local_scope(tmp_path: Path):
    calls: list[list[str]] = []

    def fetch(dois: list[str]):
        calls.append(dois)
        return {
            dois[0]: {
                "doi": f"https://doi.org/{dois[0]}",
                "topics": [{"subfield": {"id": "https://openalex.org/subfields/S1"}}],
            }
        }

    verifier = OpenAlexDoiVerifier(
        cache=RawOpenAlexWorkCache(tmp_path / "cache"), fetch_batch=fetch,
    )
    assert verifier.verify_doi("10.1/example", ["S1"]).status == "verified"
    assert verifier.verify_doi("10.1/example", ["S2"]).status == "mismatch"
    assert calls == [["10.1/example"]]
    envelope = verifier.cache.get("10.1/example")
    assert set(envelope) == {"normalized_doi", "retrieved_at", "work"}
    assert "status" not in envelope
