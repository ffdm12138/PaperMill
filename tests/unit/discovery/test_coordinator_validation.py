import pytest

from src.discovery.coordinator import DiscoveryOptions, run_discovery_batch
from src.discovery.providers.provider_page_fetcher import CallbackProviderPageFetcher
from tests.helpers.discovery_workspace import make_test_workspace


pytestmark = pytest.mark.unit


def _options(tmp_path, **overrides):
    data = dict(
        mode="refresh",
        refresh_pages=0,
        backfill_pages=1,
        max_candidates=0,
        workspace=make_test_workspace(tmp_path),
        output_dir=tmp_path / "out",
        paper_raw_dir=tmp_path / "paper_raw",
        papers_dir=tmp_path / "papers",
        ledger_path=tmp_path / "ledger.json",
    )
    data.update(overrides)
    return DiscoveryOptions(**data)


def _unused_fetcher() -> CallbackProviderPageFetcher:
    return CallbackProviderPageFetcher(
        lambda _spec, _cursor, _client: pytest.fail("validation must run before fetching"),
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"page_size": 0}, "page_size"),
        ({"backfill_pages": 0}, "backfill_pages"),
        ({"max_candidates": -1}, "max_candidates"),
        ({"max_pages_total": 0}, "max_pages_total"),
        ({"until_exhausted": True, "max_pages_total": None}, "until_exhausted"),
        ({"apply": True, "stage_to_paper_raw": False}, "apply=True"),
    ],
)
def test_run_discovery_batch_validates_service_options(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        run_discovery_batch(
            ["kw"], options=_options(tmp_path, **kwargs), max_workers=1,
            page_fetcher=_unused_fetcher(),
        )


def test_run_discovery_batch_rejects_blank_keywords(tmp_path):
    with pytest.raises(ValueError, match="keywords"):
        run_discovery_batch(
            ["  "], options=_options(tmp_path), max_workers=1,
            page_fetcher=_unused_fetcher(),
        )


def test_run_discovery_batch_rejects_zero_workers(tmp_path):
    with pytest.raises(ValueError, match="max_workers"):
        run_discovery_batch(
            ["kw"], options=_options(tmp_path), max_workers=0,
            page_fetcher=_unused_fetcher(),
        )
