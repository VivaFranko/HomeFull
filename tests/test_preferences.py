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
from homefull.llm import FakeLLM, analysis_text, gas_stove_of, parse_criteria
from homefull.matcher import match
from homefull.models import Analysis, Criteria, Listing
from homefull.storage import Store


def preferences():
    return Criteria(max_price_uah=18000, pets=True, pet_kinds=["кіт"], gas_stove=True,
                    excluded_areas=["Левандівка"], extra="Проживатиму сам із котиком.")


def flat(**kwargs):
    return Listing("olx", "1", "https://example.com", price_uah=18000,
                   district="Залізничний", **kwargs)


def test_budget_cat_and_microdistrict_are_enforced_independently():
    c = preferences()
    good = Analysis(gas_stove=True, pets_allowed=True)
    assert match(c, flat(description="Скнилівок, газова плита"), good).ok
    assert not match(c, flat(description="Левандівка"), good).ok
    assert not match(c, flat(description="Левандовка"), good).ok
    assert not match(c, flat(), Analysis(gas_stove=True, pets_allowed=False)).ok
    assert not match(c, flat(), Analysis(gas_stove=True, pets_allowed=True, pets_excluded=["кіт"])).ok
    over = flat()
    over.price_uah = 18001
    assert not match(c, over, good).ok


def test_gas_stove_requires_confirmation_after_analysis():
    c = preferences()
    assert match(c, flat(), None).ok  # Still a candidate for analysis.
    assert not match(c, flat(), Analysis(gas_stove=None)).ok
    assert not match(c, flat(), Analysis(gas_stove=False)).ok
    assert match(c, flat(), Analysis(gas_stove=True)).ok


@pytest.mark.parametrize("text,expected", [
    ("Газова плита", True), ("Варильна поверхня: газова", True),
    ("Плита: електрична", False), ("Індукційна плита", False),
    ("Газове опалення, двоконтурний котел", None),
    ("Комунікації: газ. Побутова техніка: плита", None),
])
def test_heating_and_gas_connection_do_not_imply_gas_cooking(text, expected):
    assert gas_stove_of(text) is expected


def test_criteria_parse_and_long_ad_parameter_tail():
    c = parse_criteria(FakeLLM(), "Маю котика, до 18 тис грн, плита газова, не Левандівка")
    assert c.max_price_uah == 18000 and c.pet_kinds == ["кіт"]
    assert c.gas_stove and c.excluded_areas == ["Левандівка"] and c.districts == []
    assert "Газова плита" in analysis_text("Опис " * 1500 + "Газова плита")


def test_start_does_not_apply_global_preferences(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_module, "DATA", tmp_path)
    c = preferences()
    (tmp_path / "default_criteria.json").write_text(json.dumps({"criteria": c.to_dict()}), encoding="utf-8")
    app = object.__new__(bot_module.RentBot)
    app.store = Store(tmp_path / "db")
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=42),
                             message=SimpleNamespace(reply_html=AsyncMock()))
    asyncio.run(app.start(update, None))
    assert app.store.user(42) is None
    update.message.reply_html.assert_awaited_once_with(bot_module.HELLO, reply_markup=bot_module.MENU)
    # /start preserves criteria explicitly saved for this user.
    app.store.set_criteria(42, Criteria(max_price_uah=15000), "інший бюджет")
    asyncio.run(app.start(update, None))
    assert app.store.user(42)[0].max_price_uah == 15000
