import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from homefull import llm as module
from homefull.llm import ClaudeCLI, CodexCLI, FallbackLLM, LLMError, explicit_analysis
from homefull.matcher import match
from homefull.models import Criteria, Listing
from homefull.pipeline import CycleStats, Pipeline
from homefull.storage import Store


def item(description):
    return Listing("olx", "test", "https://example.com", price_uah=17000,
                   title="Оренда квартири", description=description)


@pytest.mark.parametrize("description,allowed", [
    ("Газова плита. Можна з котиком.", True),
    ("Газова плита. Домашні улюбленці: Так, котик", True),
    ("Газова плита. Домашні улюбленці: Ні", False),
    ("Газова плита. Домашні улюбленці: Так, невеличкий песик", False),
    ("Газове опалення. Можна з котиком.", False),
    ("Індукційна плита. Можна з котиком.", False),
    ("Газова плита.", False),
    ("Газова плита. Можна з тваринами, але без котів.", False),
    ("Газова плита. Можна з котиком. Левандівка.", False),
])
def test_rules_fallback_requires_explicit_gas_and_cat_permission(description, allowed):
    listing = item(description)
    analysis = explicit_analysis(listing)
    criteria = Criteria(max_price_uah=18000, pets=True, pet_kinds=["кіт"],
                        gas_stove=True, excluded_areas=["Левандівка"])
    assert analysis.rules_only
    assert match(criteria, listing, analysis).ok is allowed


def test_quota_error_does_not_retry_until_reset(monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda name: name)
    calls = []

    def run(*args, **kwargs):
        calls.append(True)
        return SimpleNamespace(stdout=json.dumps({"is_error": True,
            "result": "You've hit your session limit - resets 5:30pm (Europe/Kiev)"}), stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    provider = ClaudeCLI()
    for attempt in range(2):
        with pytest.raises(LLMError, match="ліміт Claude"):
            provider.extract("system", "user", {})
    assert len(calls) == 1 and provider._quota_until > time.time()


def test_codex_uses_subscription_isolation_and_structured_output(monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda name: name)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-used")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-be-used")

    def run(cmd, **kwargs):
        assert "--ignore-user-config" in cmd and "--ephemeral" in cmd
        assert cmd[cmd.index("--sandbox") + 1] == "read-only"
        assert "forced_login_method=chatgpt" in cmd
        assert "apps" in cmd and "shell_tool" in cmd and "multi_agent" in cmd
        assert "OPENAI_API_KEY" not in kwargs["env"] and "CODEX_API_KEY" not in kwargs["env"]
        out = Path(cmd[cmd.index("--output-last-message") + 1])
        out.write_text('{"ok":true}', encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    assert CodexCLI().extract("system", "untrusted text", {}) == {"ok": True}


def test_auto_switches_to_gpt_without_changing_request():
    class Failed:
        name = "claude"

        def extract(self, *args):
            raise LLMError("quota")

    class GPT:
        name = "codex"

        def extract(self, system, user, schema):
            assert (system, user, schema) == ("system", "user", {})
            return {"ok": True}

    provider = FallbackLLM([Failed(), GPT()])
    assert provider.extract("system", "user", {}) == {"ok": True}
    assert provider.name == "codex"


def test_both_providers_unavailable_produces_conservative_analysis(tmp_path):
    class Offline:
        name = "offline"

        def extract(self, *args):
            raise LLMError("No capacity")

    store = Store(tmp_path / "db")
    listing = item("Газова плита. Можна з котиком.")
    store.upsert([listing])
    stats = CycleStats()
    Pipeline(store, Offline(), sources=[]).run_analysis([listing], stats)
    assert stats.rules_analyzed == 1 and stats.warnings and not stats.errors
    assert store.analysis(listing.key).rules_only
    # A later available provider upgrades the fallback result.
    available = module.FakeLLM()
    Pipeline(store, available, sources=[]).run_analysis([listing], CycleStats())
    assert not store.analysis(listing.key).rules_only
