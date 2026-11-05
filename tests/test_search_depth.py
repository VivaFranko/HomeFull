import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from homefull.llm import FakeLLM
from homefull.matcher import match
from homefull.models import Analysis, Criteria, Listing
from homefull.pipeline import CycleStats, Pipeline
from homefull.sources import Rieltor
from homefull.storage import Store


def flat(sid, **kwargs):
    return Listing("rieltor", str(sid), f"https://example.com/{sid}", price_uah=17000, **kwargs)


def test_check_advances_past_40_rejected_and_already_sent(tmp_path):
    store = Store(tmp_path / "db")
    c = Criteria(max_price_uah=18000, gas_stove=True)
    store.set_criteria(42, c, "газова плита")
    store.upsert([flat(i) for i in range(50)])
    rejected = store.listings()[:40]
    for listing in rejected:
        store.save_analysis(listing.key, Analysis(gas_stove=False), "test")
    store.mark_sent(42, store.listings()[40].key)
    candidates = Pipeline(store, FakeLLM(), sources=[]).candidates(42, c)
    assert len(candidates) == 9
    assert not {l.key for l in rejected} & {l.key for l in candidates}


def test_rieltor_stove_is_read_from_full_characteristics(tmp_path):
    html = '''<div class="offer-view-section"><div class="offer-view-section-title">Опис</div>
    <div class="offer-view-section-text">Окрема квартира. Можна з котиком.</div></div>
    <div class="offer-view-communication-text"><b>Кухонна плита:</b> Газова</div>'''
    listing = flat(1)
    Rieltor(None).parse_details(html, listing)
    assert "Кухонна плита: Газова" in listing.description and listing.details_version == 1
    from homefull.llm import explicit_analysis
    assert explicit_analysis(listing).gas_stove is True


def test_details_upgrade_invalidates_incomplete_cached_analysis(tmp_path):
    class Client:
        def get(self, url):
            return '''<div class="offer-view-communication-text"><b>Кухонна плита:</b> Газова</div>'''

    store = Store(tmp_path / "db")
    listing = flat(1)
    store.upsert([listing])
    store.save_analysis(listing.key, Analysis(gas_stove=None), "test")
    pipeline = Pipeline(store, FakeLLM(), client=Client(), sources=[Rieltor])
    candidates = pipeline.candidates(42, Criteria(gas_stove=True))
    assert candidates
    pipeline.prepare_candidates(candidates, CycleStats())
    assert store.analysis(listing.key) is None
    assert store.get(listing.key).details_version == 1


def test_gone_adverts_are_removed_from_notifications(tmp_path):
    class Client:
        def get(self, url):
            response = requests.Response()
            response.status_code = 410
            raise requests.HTTPError(response=response)

    store = Store(tmp_path / "db")
    listing = flat(1)
    store.upsert([listing])
    store.enqueue(42, [listing])
    pipeline = Pipeline(store, FakeLLM(), client=Client(), sources=[Rieltor])
    pipeline.prepare_candidates([listing], CycleStats())
    assert not listing.available and not match(Criteria(), listing, None).ok
    assert store.pending(42) == []
    assert pipeline.candidates(42, Criteria()) == []


def test_details_requests_skip_adverts_above_all_users_budgets(tmp_path):
    detail_requests = []

    class Source:
        name, needs_details = "rieltor", True

        def __init__(self, client):
            pass

        def fetch(self, pages):
            cheap, expensive = flat(1), flat(2)
            expensive.price_uah = 50000
            return [cheap, expensive]

        def details(self, listing):
            detail_requests.append(listing.key)

    store = Store(tmp_path / "db")
    store.set_criteria(42, Criteria(max_price_uah=18000), "бюджет")
    pipeline = Pipeline(store, FakeLLM(), sources=[Source])
    pipeline.collect(CycleStats())
    assert detail_requests == ["rieltor:1"]
