import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homefull.bot import RentBot
from homefull.dates import age_text, set_published, timestamp
from homefull.llm import FakeLLM
from homefull.matcher import match
from homefull.models import Analysis, Criteria, Listing
from homefull.pipeline import CycleStats, Pipeline
from homefull.sources.flatfy import Flatfy, property_link
from homefull.sources.olx import Olx
from homefull.storage import Store
from homefull.ui import CRITERIA, FIND, MENU, PAUSE, RESUME, STATUS, progress_text


def flat(sid="1", **kwargs):
    return Listing("olx", sid, f"https://example.com/offer/{sid}", price_uah=17000, **kwargs)


def app_update(tmp_path, text):
    app = object.__new__(RentBot)
    app.store = Store(tmp_path / "db.sqlite")
    app.store.set_criteria(42, Criteria(max_price_uah=18000), "budget")
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=42),
                             message=SimpleNamespace(text=text, reply_text=AsyncMock(), reply_html=AsyncMock()))
    return app, update, SimpleNamespace(user_data={})


def test_menu_has_buttons_instead_of_command_text():
    texts = [button.text for row in MENU.keyboard for button in row]
    assert set(texts) == {FIND, STATUS, PAUSE, RESUME, CRITERIA}
    assert not any(text.startswith("/") for text in texts)


@pytest.mark.parametrize("text,action", [(FIND, "check"), (STATUS, "stats"), (PAUSE, "pause"),
                                        (RESUME, "resume"), (CRITERIA, "show_criteria")])
def test_menu_routes_without_parsing_or_changing_criteria(tmp_path, text, action):
    app, update, ctx = app_update(tmp_path, text)
    setattr(app, action, AsyncMock())
    asyncio.run(app.on_text(update, ctx))
    getattr(app, action).assert_awaited_once_with(update, ctx)
    assert app.store.user(42)[0].max_price_uah == 18000


def test_pause_resume_and_criteria_edits_preserve_individual_pause(tmp_path):
    app, update, ctx = app_update(tmp_path, PAUSE)
    asyncio.run(app.pause(update, ctx))
    assert not app.store.user(42)[2]
    app.store.set_criteria(42, Criteria(max_price_uah=16000), "new budget")
    assert not app.store.user(42)[2]
    asyncio.run(app.resume(update, ctx))
    assert app.store.user(42)[2] and app.store.user(42)[0].max_price_uah == 16000


def test_pause_callback_does_not_consume_pending_criteria(tmp_path):
    app, update, ctx = app_update(tmp_path, "")
    ctx.user_data["pending"] = ({"max_price_uah": 18000}, "budget")
    update.callback_query = SimpleNamespace(data="search:pause", answer=AsyncMock())
    asyncio.run(app.on_button(update, ctx))
    assert not app.store.user(42)[2] and "pending" in ctx.user_data


def test_all_paused_means_no_catalog_network_requests(tmp_path):
    class Source:
        name = "olx"

        def __init__(self, client):
            pass

        def fetch(self, pages):
            raise AssertionError("Paused search must not fetch")

    store = Store(tmp_path / "db")
    store.set_criteria(42, Criteria(), "any")
    store.set_active(42, False)
    stats = Pipeline(store, FakeLLM(), sources=[Source]).cycle(lambda *a: None)
    assert stats.paused and stats.fetched == {}


def test_paused_manual_search_does_not_process_or_send(tmp_path):
    app, update, ctx = app_update(tmp_path, FIND)
    app.store.set_active(42, False)
    asyncio.run(app.check(update, ctx))  # No pipeline/app attributes: must return first.
    assert not app.store.user(42)[2]


def test_pause_between_analysis_batches_stops_new_batches(tmp_path):
    store = Store(tmp_path / "db")
    store.set_criteria(42, Criteria(), "any")
    listings = [flat(str(i)) for i in range(10)]
    store.upsert(listings)

    def progress(phase, data):
        if data.get("analysis_done") == 8:
            store.set_active(42, False)

    stats = CycleStats()
    Pipeline(store, FakeLLM(), sources=[]).run_analysis(
        listings, stats, progress, lambda: not store.user(42)[2])
    assert stats.analyzed == 8 and store.analysis(listings[8].key) is None


def test_pause_during_notification_stops_subsequent_adverts(tmp_path):
    store = Store(tmp_path / "db")
    store.set_criteria(42, Criteria(), "any")
    listings = [flat("1"), flat("2")]
    store.upsert(listings)
    for listing in listings:
        store.save_analysis(listing.key, Analysis(), "test")
    sent = []

    def send(*args):
        sent.append(args)
        store.set_active(42, False)

    stats = CycleStats()
    Pipeline(store, FakeLLM(), sources=[]).notify(listings, send, stats)
    assert len(sent) == 1 and stats.sent == 1


def test_runs_are_persistent_and_private_to_their_chat(tmp_path):
    store = Store(tmp_path / "db")
    own = store.begin_run("manual", 42)
    store.update_run(own, "analysis", {"analysis_done": 8, "analysis_total": 16})
    other = store.begin_run("manual", 99)
    assert store.latest_run(42)["id"] == own
    assert store.latest_run(42)["id"] != other
    assert "8/16" in progress_text(store.latest_run(42))
    scheduled = store.begin_run("scheduled")
    assert store.latest_run(42, running=True)["id"] == own
    assert store.get_run(scheduled)["kind"] == "scheduled"
    store.close()
    store = Store(tmp_path / "db")
    assert store.latest_run(42)["status"] == "running"
    store.recover_runs()
    assert store.latest_run(42)["status"] == "interrupted"


def test_publication_age_is_not_bot_observation_age_or_refresh():
    listing = flat()
    now = datetime.fromisoformat("2026-11-05T12:00:00+02:00").timestamp()
    listing.first_seen_at = now - 60
    listing.refreshed_at = now - 30
    set_published(listing, "2026-11-03T10:00:00+02:00")
    assert "2 дн. 2 год." in age_text(listing, now)
    assert "03.11.2026 10:00" in age_text(listing, now)
    unknown = flat(first_seen_at=now - 3600)
    assert "публікації невідома" in age_text(unknown, now)
    assert "спостерігаю 1 год." in age_text(unknown, now)


def test_date_only_and_aggregator_age_are_explicit():
    listing = flat()
    set_published(listing, "2026-11-03", basis="aggregator")
    now = datetime.fromisoformat("2026-11-05T12:00:00+02:00").timestamp()
    text = age_text(listing, now)
    assert "У ЛУН" in text and "год." not in text
    assert timestamp("Сьогодні о 99:99") == (None, None)


def test_olx_uses_original_created_time_not_displayed_refresh_date():
    state = {"ad": {"ad": {"createdTime": "2026-10-08T15:08:35+03:00",
                             "lastRefreshTime": "2026-11-04T10:27:12+02:00"}}}
    html = ('<script>window.__PRERENDERED_STATE__=' + json.dumps(json.dumps(state)) + ';</script>'
            '<div data-testid="ad_description">Оренда квартири</div>'
            '<span data-testid="ad-posted-at">Опубліковано 04 листопада 2026 р.</span>')
    listing = Olx(None).parse_details(html, flat())
    assert listing.published_at == timestamp(state["ad"]["ad"]["createdTime"])[0]
    assert listing.refreshed_at == timestamp(state["ad"]["ad"]["lastRefreshTime"])[0]
    assert listing.publication_basis == "source"


def test_lun_reads_original_url_and_full_text_without_executing_js():
    item = {"id": 123456, "url_raw": "https://rieltor.ua/lvov/flats-rent/view/99999999/",
            "images": [{"image_id": "offers/777"}], "currency": "UAH", "price": 17000,
            "room_count": 1, "header": "Приклад", "text": "Газова плита. undefined у тексті.",
            "insert_time": "2026-11-01T10:00:00Z", "pets_allowed_full": "yes"}
    state = {"search": {"realties": {"list": [item]}}, "unused": None}
    js = json.dumps(state, ensure_ascii=False).replace('"unused": null', '"unused": undefined')
    html = '<script>window.INITIAL_STATE = ' + js + ';</script>'
    listing = Flatfy(None).parse_catalog(html)[0]
    assert listing.key == "flatfy:777" and listing.url == item["url_raw"]
    assert "undefined у тексті" in listing.description
    assert listing.publication_basis == "aggregator" and listing.published_at is not None
    assert property_link("https://lun.ua/rent/lviv/flats?q=address") is None
    assert property_link("http://127.0.0.1/realty/123") is None


def test_lun_original_olx_details_upgrade_age_without_changing_legacy_id(monkeypatch):
    listing = Listing("flatfy", "photo-777", "https://www.olx.ua/d/uk/obyavlenie/sample-IDabc.html")
    set_published(listing, "2026-11-05T10:00:00Z", basis="aggregator")

    def details(self, native):
        set_published(native, "2026-10-01T10:00:00Z", authoritative=True)
        native.description = "Original description"

    monkeypatch.setattr(Olx, "details", details)
    Flatfy(None).details(listing)
    assert listing.key == "flatfy:photo-777"
    assert listing.publication_basis == "source" and listing.details_version == 1


def test_schema_without_direct_link_is_not_sent_as_search_url():
    data = {"numberOfItems": 1, "itemListElement": [{"item": {
        "name": "Приклад", "offers": {"price": 17000, "priceCurrency": "UAH"}}}]}
    listing = Flatfy(None).parse_catalog('<script type="application/ld+json">' + json.dumps(data) + '</script>')[0]
    assert listing.link_kind == "unresolved" and not listing.url
    assert not match(Criteria(), listing, None).ok


def test_migrating_database_preserves_user_and_observation_times(tmp_path):
    path = tmp_path / "db"
    store = Store(path)
    store.set_criteria(42, Criteria(max_price_uah=18000, gas_stove=True), "mine")
    listing = Listing("flatfy", "1", "https://flatfy.ua/uk/search?q=old", price_uah=17000)
    store.upsert([listing])
    seen = store.get(listing.key).first_seen_at
    store.close()
    store = Store(path)
    assert store.user(42)[0].max_price_uah == 18000
    assert store.get(listing.key).first_seen_at == seen
    assert store.get(listing.key).link_kind == "unresolved"
    summary = store.search_summary(42)
    assert summary["unresolved_links"] == 1 and summary["eligible"] == 0


def test_store_serializes_parallel_updates(tmp_path):
    store = Store(tmp_path / "db")
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda i: store.upsert([flat(str(i))]), range(40)))
    assert len(store.listings()) == 40


def test_manual_search_finishes_with_persistent_progress_and_delivery(tmp_path, monkeypatch):
    app, update, ctx = app_update(tmp_path, FIND)
    listing = flat()
    app.store.upsert([listing])
    app.store.save_analysis(listing.key, Analysis(gas_stove=True), "test")
    app.pipeline = Pipeline(app.store, FakeLLM(), sources=[])
    app.app = SimpleNamespace(bot=object())
    app.fair = SimpleNamespace(verdict=lambda listing: None)
    sender = AsyncMock()
    monkeypatch.setattr("homefull.bot.send_listing", sender)
    asyncio.run(app.check(update, ctx))
    sender.assert_awaited_once()
    run = app.store.latest_run(42)
    assert run["status"] == "completed" and run["stats"]["sent"] == 1
    assert app.store.was_sent(42, listing.key) and not app._checks


def test_manual_search_pause_during_analysis_prevents_delivery(tmp_path, monkeypatch):
    app, update, ctx = app_update(tmp_path, FIND)
    listing = flat()
    app.store.upsert([listing])
    app.store.save_analysis(listing.key, Analysis(), "test")
    app.pipeline = Pipeline(app.store, FakeLLM(), sources=[])

    def paused(*args):
        app.store.set_active(42, False)

    app.pipeline.run_analysis = paused
    sender = AsyncMock()
    monkeypatch.setattr("homefull.bot.send_listing", sender)
    asyncio.run(app.check(update, ctx))
    sender.assert_not_awaited()
    assert app.store.latest_run(42)["status"] == "paused"


def test_old_confirmation_cannot_save_newer_pending_criteria(tmp_path):
    app, update, ctx = app_update(tmp_path, "")
    ctx.user_data.update(pending=({"max_price_uah": 16000}, "new budget"), pending_message_id=2)
    update.callback_query = SimpleNamespace(data="save", answer=AsyncMock(), edit_message_text=AsyncMock(),
                                            message=SimpleNamespace(chat_id=42, message_id=1))
    asyncio.run(app.on_button(update, ctx))
    assert app.store.user(42)[0].max_price_uah == 18000
    assert ctx.user_data["pending"][0]["max_price_uah"] == 16000
