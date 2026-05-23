"""Serverless LLM routing — which tier goes to an API vs local Ollama.

This is the switch that lets the bot run on a GPU-less VPS, so the
precedence rules (unified `LLM_API_*` wins; legacy `HEAVY_LLM_API_*`
still aliases the heavy tier; both-routed ⇒ no Ollama) are pinned here.
"""

from __future__ import annotations

import pytest

from sentinel import llm
from sentinel.config import settings

_VARS = [
    "LLM_API_BASE", "LLM_API_KEY", "LLM_API_MODEL_LIGHT",
    "LLM_API_MODEL_HEAVY",
    "LIGHT_LLM_API_BASE", "LIGHT_LLM_API_KEY", "LIGHT_LLM_API_MODEL",
    "HEAVY_LLM_API_BASE", "HEAVY_LLM_API_KEY", "HEAVY_LLM_API_MODEL",
    "LLM_API_PROVIDER_LIGHT", "LLM_API_PROVIDER_HEAVY",
]


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch):
    # start every test from "everything local"
    for v in _VARS:
        monkeypatch.setattr(settings, v, "")
    yield


def _set(monkeypatch, **kw):
    for k, v in kw.items():
        monkeypatch.setattr(settings, k, v)


def test_all_local_by_default():
    assert llm._api_route("light") is None
    assert llm._api_route("heavy") is None
    assert llm._all_api() is False


def test_unified_routes_both_tiers(monkeypatch):
    _set(monkeypatch,
         LLM_API_BASE="https://openrouter.ai/api/v1",
         LLM_API_KEY="sk-x",
         LLM_API_MODEL_LIGHT="meta-llama/llama-3.1-8b-instruct",
         LLM_API_MODEL_HEAVY="qwen/qwen3-30b-a3b")
    assert llm._api_route("light") == (
        "https://openrouter.ai/api/v1", "sk-x",
        "meta-llama/llama-3.1-8b-instruct",
    )
    assert llm._api_route("heavy")[2] == "qwen/qwen3-30b-a3b"
    assert llm._all_api() is True


def test_unified_can_route_one_tier_only(monkeypatch):
    # light remote, heavy stays local → not all-api
    _set(monkeypatch,
         LLM_API_BASE="https://api.novita.ai/v3/openai",
         LLM_API_KEY="k",
         LLM_API_MODEL_LIGHT="some/light-model")
    assert llm._api_route("light") is not None
    assert llm._api_route("heavy") is None
    assert llm._all_api() is False


def test_heavy_override_aliases_heavy_only(monkeypatch):
    _set(monkeypatch,
         HEAVY_LLM_API_BASE="https://openrouter.ai/api/v1",
         HEAVY_LLM_API_KEY="k",
         HEAVY_LLM_API_MODEL="anthropic/claude-3.5-sonnet")
    assert llm._api_route("light") is None       # heavy override ≠ light
    assert llm._api_route("heavy") == (
        "https://openrouter.ai/api/v1", "k",
        "anthropic/claude-3.5-sonnet",
    )


def test_mixed_providers_light_free_heavy_paid(monkeypatch):
    # the actual goal: free light on one provider, paid heavy on another
    _set(monkeypatch,
         LIGHT_LLM_API_BASE=(
             "https://generativelanguage.googleapis.com/v1beta/openai"
         ),
         LIGHT_LLM_API_KEY="goog",
         LIGHT_LLM_API_MODEL="gemma-3-12b-it",
         HEAVY_LLM_API_BASE="https://api.novita.ai/v3/openai",
         HEAVY_LLM_API_KEY="novita",
         HEAVY_LLM_API_MODEL="qwen/qwen3.6-35b-a3b")
    lb, lk, lm = llm._api_route("light")
    hb, hk, hm = llm._api_route("heavy")
    assert lk == "goog" and "googleapis" in lb
    assert hk == "novita" and hm == "qwen/qwen3.6-35b-a3b"
    assert llm._all_api() is True   # both routed → no Ollama needed


def test_provider_field_parses_openrouter_hints():
    # bare provider → order only
    assert llm._provider_field("deepinfra") == {"order": ["deepinfra"]}
    # provider/quant → order + quantizations
    assert llm._provider_field("io-net/fp8") == {
        "order": ["io-net"], "quantizations": ["fp8"],
    }
    # empty → no provider routing (None, so payload stays clean)
    assert llm._provider_field("") is None
    assert llm._provider_field("  ") is None


def test_api_provider_reads_per_tier_hint(monkeypatch):
    _set(monkeypatch,
         LLM_API_PROVIDER_LIGHT="deepinfra/fp8",
         LLM_API_PROVIDER_HEAVY="io-net/fp8")
    assert llm._api_provider("light") == "deepinfra/fp8"
    assert llm._api_provider("heavy") == "io-net/fp8"


def test_no_think_only_on_qwen_json_calls():
    # JSON + qwen → suppress thinking (works for local tag or API id)
    assert llm._maybe_no_think("p", "qwen3:30b-a3b", True).endswith("/no_think")
    assert llm._maybe_no_think(
        "p", "qwen/qwen3.6-35b-a3b", True
    ).endswith("/no_think")
    # prose (non-JSON) qwen → leave thinking on
    assert llm._maybe_no_think("p", "qwen/qwen3-30b-a3b", False) == "p"
    # non-qwen model → never inject
    assert llm._maybe_no_think("p", "meta-llama/llama-3.1-8b", True) == "p"
    # idempotent — don't double-append
    once = llm._maybe_no_think("p", "qwen3", True)
    assert llm._maybe_no_think(once, "qwen3", True) == once


def test_per_tier_override_beats_shared(monkeypatch):
    # shared routes heavy to Novita, but an explicit HEAVY_* override wins
    _set(monkeypatch,
         LLM_API_BASE="https://api.novita.ai/v3/openai",
         LLM_API_KEY="shared",
         LLM_API_MODEL_HEAVY="qwen/qwen3-30b-a3b",
         HEAVY_LLM_API_BASE="https://openrouter.ai/api/v1",
         HEAVY_LLM_API_KEY="override",
         HEAVY_LLM_API_MODEL="anthropic/claude-3.5-sonnet")
    base, key, model_id = llm._api_route("heavy")
    assert key == "override" and model_id == "anthropic/claude-3.5-sonnet"
