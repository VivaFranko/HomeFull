import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from homefull import bot as bot_module
from homefull.intents import is_search_question
from homefull.llm import parse_criteria
from homefull.models import Criteria, Listing
from homefull.storage import Store


def preferences():
    return Criteria(max_price_uah=18000, pets=True, pet_kinds=["кіт"], gas_stove=True,
                    excluded_areas=["Левандівка"], room_ok=False)


def app_and_update(tmp_path, text):
    app = object.__new__(bot_module.RentBot)
    app.store = Store(tmp_path / "db")
    app.store.set_criteria(42, preferences(), "мої вимоги")
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=42),
                             message=SimpleNamespace(text=text, reply_text=AsyncMock(),
                                                     reply_html=AsyncMock()))
    return app, update, SimpleNamespace(user_data={})


@pytest.mark.parametrize("text", ["Знайшов щось", "Ти щось знайшов?", "Є варіанти?",
                                   "що нового", "Покажи знайдене!", "Є якісь варіанти?"])
def test_status_question_runs_check_and_preserves_personal_criteria(tmp_path, text):
    app, update, ctx = app_and_update(tmp_path, text)
    app.check = AsyncMock()
    asyncio.run(app.on_text(update, ctx))
    app.check.assert_awaited_once_with(update, ctx)
    assert app.store.user(42)[0] == preferences()
    assert "pending" not in ctx.user_data
    update.message.reply_html.assert_not_called()


@pytest.mark.parametrize("text", ["до 17 тисяч", "Шукаю з котиком", "Будь-який район",
                                   "Газова плита обов'язково"])
def test_real_preferences_are_not_treated_as_status(text):
    assert not is_search_question(text)


def test_empty_llm_response_cannot_replace_existing_criteria(tmp_path, monkeypatch):
    app, update, ctx = app_and_update(tmp_path, "Дякую")
    app.llm = object()
    monkeypatch.setattr(bot_module.fx, "rates", lambda: {"USD": 41})
    monkeypatch.setattr(bot_module, "parse_criteria", lambda *args: Criteria())
    asyncio.run(app.on_text(update, ctx))
    assert app.store.user(42)[0] == preferences()
    assert "pending" not in ctx.user_data
    update.message.reply_html.assert_not_called()


def test_stale_empty_save_is_rejected(tmp_path):
    app, update, ctx = app_and_update(tmp_path, "")
    query = SimpleNamespace(data="save", answer=AsyncMock(), edit_message_text=AsyncMock(),
                            message=SimpleNamespace(chat_id=42))
    update.callback_query = query
    ctx.user_data["pending"] = (Criteria().to_dict(), "Знайшов щось")
    asyncio.run(app.on_button(update, ctx))
    assert app.store.user(42)[0] == preferences()


def test_partial_change_receives_full_current_criteria():
    current = preferences()

    class LLM:
        def extract(self, system, user, schema):
            assert json.dumps(current.to_dict(), ensure_ascii=False) in user
            assert "решту вимог збережи" in user
            return {**current.to_dict(), "max_price_uah": 17000}

    changed = parse_criteria(LLM(), "Бюджет тепер до 17 тисяч", current=current)
    assert changed.max_price_uah == 17000
    assert changed.gas_stove and changed.pet_kinds == ["кіт"]
    assert changed.excluded_areas == ["Левандівка"]


def test_check_never_sends_unanalyzed_advert(tmp_path, monkeypatch):
    app, update, ctx = app_and_update(tmp_path, "/check")
    listing = Listing("olx", "1", "https://example.com", price_uah=17000)
    app.store.upsert([listing])

    def failed_analysis(listings, stats, *args):
        stats.errors.append("LLM unavailable")

    app.pipeline = SimpleNamespace(run_analysis=failed_analysis,
                                   candidates=lambda *a: [listing],
                                   prepare_candidates=lambda *a: None)
    app.app = SimpleNamespace(bot=object())
    send = AsyncMock()
    monkeypatch.setattr(bot_module, "send_listing", send)
    asyncio.run(app.check(update, ctx))
    send.assert_not_awaited()
    assert not app.store.was_sent(42, listing.key)
