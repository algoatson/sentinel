"""Local Ollama wrapper.

Sampling defaults are model-family-aware:
- Gemma 4: temperature=1.0, top_p=0.95, top_k=64 (Google DeepMind guidance)
- Qwen3:   temperature=0.7, top_p=0.8,  top_k=20, min_p=0 (Alibaba guidance)

The active heavy model is configurable via env (`LLM_MODEL_HEAVY`); whichever
family is selected, the right sampling is applied. JSON-mode requests still
rely on Ollama's native `format="json"` enforcement.
"""

import json
import re
import time
from typing import Any, Literal

import httpx
import ollama
from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import grounding
from .config import settings


LLM_ERROR_SENTINEL = "[LLM_ERROR]"

# When reasoning is ON, the model spends hidden chain-of-thought tokens
# BEFORE the visible answer, and they count against max_tokens. Measured
# live on deepseek-v4-flash: ~500 typical, spiking to ~900 on some runs.
# Every caller's max_tokens is its intended ANSWER budget (tuned per
# pipeline — 360 for a lounge quip, 2000 for a thesis). So when reasoning
# is on we ADD this headroom on TOP of the caller's budget rather than
# flooring to a flat value — a flat floor would silently starve the
# long-form calls (a 1300-token synthesis answer + 600 reasoning needs
# 1900, not 1500). Additive headroom preserves each call's answer length
# AND fits the thinking. It's a ceiling, not a target: short answers
# still stop early, so no wasted spend. Generous (1200) to absorb the
# worst-case reasoning spike + margin so nothing truncates. JSON
# classifiers never add it — they run reasoning-off.
_REASONING_HEADROOM_TOKENS = 1200

# Process-lifetime LLM health + spend counters, read by the daily
# diagnostic and the /system panel. Coarse by design (the GIL makes the
# increments good enough; a health metric does not need exactness or a
# lock). "errors" = a caller got the sentinel back even after any
# light-model fallback. Token counts come straight from each provider
# response's `usage` block (exact); the $ figures are those counts ×
# the configured per-million prices (an estimate only as good as the
# prices in LLM_PRICE_*).
_STATS = {
    "calls": 0,
    "errors": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "reasoning_tokens": 0,  # subset of completion, when the provider reports it
    "total_tokens": 0,
}
_BOOT_TS = time.monotonic()


def _record_usage(data: dict | None) -> None:
    """Fold one provider response's `usage` block into the running totals.
    Best-effort + GIL-coarse — never raises out of the hot path."""
    if not isinstance(data, dict):
        return
    try:
        u = data.get("usage") or {}
        pt = int(u.get("prompt_tokens") or 0)
        ct = int(u.get("completion_tokens") or 0)
        tt = int(u.get("total_tokens") or (pt + ct))
        _STATS["prompt_tokens"] += pt
        _STATS["completion_tokens"] += ct
        _STATS["total_tokens"] += tt
        details = u.get("completion_tokens_details") or {}
        _STATS["reasoning_tokens"] += int(details.get("reasoning_tokens") or 0)
    except Exception:
        pass


def llm_stats() -> dict:
    """Health + spend snapshot since process start.

    Returns the raw counters plus derived rate/cost so the daily digest
    and the /system panel don't each re-derive them. `$` figures use
    settings.LLM_PRICE_IN_PER_M / LLM_PRICE_OUT_PER_M (per-million-token
    prices) — exact token counts, estimated dollars.
    """
    s = dict(_STATS)
    hours = max((time.monotonic() - _BOOT_TS) / 3600.0, 1e-6)
    in_price = settings.LLM_PRICE_IN_PER_M or 0.0
    out_price = settings.LLM_PRICE_OUT_PER_M or 0.0
    cost = (
        s["prompt_tokens"] / 1_000_000 * in_price
        + s["completion_tokens"] / 1_000_000 * out_price
    )
    s["uptime_hours"] = round(hours, 3)
    s["tokens_per_hour"] = int(s["total_tokens"] / hours)
    s["est_cost_usd"] = round(cost, 4)
    s["est_cost_per_hour_usd"] = round(cost / hours, 4)
    s["est_cost_per_day_usd"] = round(cost / hours * 24, 3)
    s["priced"] = bool(in_price or out_price)
    return s


def parse_json_response(raw: str, *, expect: type = dict) -> Any | None:
    """Defensive JSON parser for LLM output.

    Returns None on any failure (sentinel, fence-only, invalid JSON, wrong
    top-level type). Callers should treat None as "skip this row" rather than
    raise — the LLM has already had its chance.
    """
    if not raw or raw == LLM_ERROR_SENTINEL:
        return None
    s = raw.strip()
    # The LLM class already strips fences, but be defensive in case a caller
    # forwarded a raw provider response.
    if s.startswith("```"):
        s = s.strip("`").strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError as e:
        logger.warning("parse_json_response failed: {} — raw: {}", e, s[:200])
        return None
    if not isinstance(parsed, expect):
        # Small models routinely answer an array-task with a single object.
        # Salvage that rather than discarding the whole batch.
        if expect is list and isinstance(parsed, dict):
            return [parsed]
        logger.warning(
            "parse_json_response: expected {}, got {} — raw: {}",
            expect.__name__,
            type(parsed).__name__,
            s[:200],
        )
        return None
    return parsed


_IMPORTANCE_RE = re.compile(
    r"^\s*IMPORTANCE:\s*([1-5])\s*[—:-]?\s*(.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_trailing_importance(text: str) -> tuple[str, int | None, str]:
    """Pull a model-emitted `IMPORTANCE: <1-5> — <reason>` line off the end of
    a completion. Returns (clean_text, level, reason). level is None if the
    model didn't emit one (callers fall back to a default).
    """
    if not text:
        return text, None, ""
    matches = list(_IMPORTANCE_RE.finditer(text))
    if not matches:
        return text.strip(), None, ""
    m = matches[-1]
    level = int(m.group(1))
    reason = (m.group(2) or "").strip().lstrip("—-:").strip()
    clean = (text[: m.start()] + text[m.end():]).strip()
    return clean, level, reason


# Strictly single-line: the documented contract is `CALL: $TICKER DIR <1-5>`
# on one line. Using [ \t] (not \s) for the inter-token gaps stops a
# conviction-less call from swallowing a digit off the *next* line (which
# would feed a fabricated conviction into scoring + fund sizing). `^` stays
# line-anchored via MULTILINE.
_CALL_RE = re.compile(
    r"^[ \t]*CALL:[ \t]*\$?([A-Za-z0-9.\-=^]{1,12})[ \t]+"
    r"(LONG|SHORT|FLAT|NONE)\b[ \t]*([1-5]?)",
    re.IGNORECASE | re.MULTILINE,
)


def parse_calls(text: str) -> tuple[str, list[dict]]:
    """Pull machine `CALL: $TICKER LONG|SHORT [1-5]` lines out of a
    completion. Returns (clean_text, calls). FLAT/NONE are dropped (an
    explicit "no call"). Used to log directional calls for scoring.
    """
    if not text:
        return text, []
    calls: list[dict] = []
    for m in _CALL_RE.finditer(text):
        direction = m.group(2).lower()
        if direction in ("flat", "none"):
            continue
        calls.append(
            {
                "ticker": m.group(1).upper(),
                "direction": direction,
                "conviction": int(m.group(3)) if m.group(3) else 3,
            }
        )
    clean = _CALL_RE.sub("", text).strip()
    return clean, calls


def _options_for(model_tag: str, max_tokens: int) -> dict:
    """Return Ollama generation options tuned to the model family."""
    tag = model_tag.lower()
    if tag.startswith("qwen"):
        return {
            "num_predict": max_tokens,
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "min_p": 0.0,
        }
    # Default to Gemma 4 published sampling defaults.
    return {
        "num_predict": max_tokens,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 64,
    }


def _api_route(model: Literal["light", "heavy"]) -> tuple[str, str, str] | None:
    """Resolve a tier to its serverless route as ``(base, key, model_id)``,
    or None when that tier should use local Ollama.

    Precedence: a per-tier override (``LIGHT_LLM_API_*`` / ``HEAVY_LLM_API_*``)
    wins — that's how you split tiers across providers (e.g. free light on
    one endpoint, paid heavy on another). Otherwise the shared
    ``LLM_API_*`` provider is used with the tier's ``LLM_API_MODEL_*`` id.
    """
    if model == "light":
        ob, ok, om = (settings.LIGHT_LLM_API_BASE, settings.LIGHT_LLM_API_KEY,
                      settings.LIGHT_LLM_API_MODEL)
    else:
        ob, ok, om = (settings.HEAVY_LLM_API_BASE, settings.HEAVY_LLM_API_KEY,
                      settings.HEAVY_LLM_API_MODEL)
    if ob and ok and om:
        return ob, ok, om

    shared_model = (
        settings.LLM_API_MODEL_LIGHT if model == "light"
        else settings.LLM_API_MODEL_HEAVY
    )
    if settings.LLM_API_BASE and settings.LLM_API_KEY and shared_model:
        return settings.LLM_API_BASE, settings.LLM_API_KEY, shared_model
    return None


def _all_api() -> bool:
    """True when both tiers are routed off-box — Ollama isn't needed at all
    (the CPU-VPS deployment shape)."""
    return _api_route("light") is not None and _api_route("heavy") is not None


def _api_provider(model: Literal["light", "heavy"]) -> str:
    """OpenRouter provider hint for a tier ('' = let the provider route)."""
    return (settings.LLM_API_PROVIDER_LIGHT if model == "light"
            else settings.LLM_API_PROVIDER_HEAVY)


def _provider_field(hint: str) -> dict | None:
    """Parse an OpenRouter provider hint into its routing object.

    ``"deepinfra"`` → prefer that provider; ``"deepinfra/fp8"`` → also
    require fp8 quantization. Returns None for an empty hint. `order`
    *prefers* the provider but leaves OpenRouter free to fall back if it's
    down (a hard pin would silently drop data when the provider hiccups).
    """
    hint = (hint or "").strip()
    if not hint:
        return None
    name, _, quant = hint.partition("/")
    prov: dict[str, Any] = {"order": [name.strip()]}
    if quant.strip():
        prov["quantizations"] = [quant.strip()]
    return prov


def _reasoning_field(mode: str | None) -> dict:
    """Build the OpenRouter `reasoning` control object for a given mode.

    Why this matters (it's load-bearing, not a tuning nicety): DeepSeek
    v4 Flash and most modern "flash"/hybrid models are REASONING models
    — they emit hidden chain-of-thought tokens that count against
    `max_tokens` BEFORE any visible content. Measured live, even a
    trivial "reply OK" burns ~77 reasoning tokens; a real classifier
    prompt burns 500+. At our 300–1000 caps the budget is exhausted
    mid-think, so the API returns empty content (`finish_reason=length`)
    or a JSON array truncated mid-element — the ~30% empty-completion
    failure rate seen in prod.

    Modes:
      "low"/"medium"/"high" → effort-graded reasoning kept ON. Only
        sensible when the caller also gives a generous `max_tokens` so
        thinking AND the answer both fit.
      anything else ("off"/"" /None) → `{"enabled": false}`: thinking
        disabled, the full budget goes to the visible answer. Verified
        to drop reasoning tokens to 0 on the live model. Models that
        don't support the toggle ignore it (no error).

    The bot's edge lives in the DATA it assembles + its structured
    prompts, not the model's hidden CoT — so reasoning-off is a
    reliability win here, not a lobotomy. Re-enable per-call (the
    `reasoning=` arg on complete/chat) or globally (LLM_REASONING) when
    a specific high-value read earns the extra tokens.
    """
    m = (mode or "off").strip().lower()
    if m in ("low", "medium", "high"):
        return {"effort": m}
    return {"enabled": False}


def _resolve_reasoning(reasoning: str | None, *, json_mode: bool) -> str:
    """Effective reasoning mode for a call. JSON/structured calls ALWAYS
    get 'off' — reasoning only truncates a JSON array and never improves
    a classification. Otherwise an explicit per-call `reasoning` wins,
    falling back to the global `LLM_REASONING` setting (default 'off')."""
    if json_mode:
        return "off"
    if reasoning:
        return reasoning
    return settings.LLM_REASONING or "off"


def _maybe_no_think(prompt: str, model_name: str, json_mode: bool) -> str:
    """Append Qwen's `/no_think` switch for structured (JSON) calls.

    Qwen3's thinking mode spends the token budget on reasoning traces (so a
    capped completion can run out before the answer → empty/truncated) and
    emits ``<think>`` blocks that break JSON parsing. `/no_think` is a
    prompt-level convention the model was trained on, so it works on BOTH
    the local Ollama path and any serverless API — not just locally.
    Prose (non-JSON) calls are left to think: there the reasoning improves
    synthesis/why-moved quality and the caps are generous.
    """
    if json_mode and "qwen" in model_name.lower() and "/no_think" not in prompt:
        return f"{prompt}\n\n/no_think"
    return prompt


def _api_chat(
    messages: list[dict],
    *,
    base: str,
    key: str,
    model_id: str,
    max_tokens: int,
    provider: str = "",
    tools: list[dict] | None = None,
    tool_choice: str | dict = "auto",
    temperature: float = 0.6,
    json_mode: bool = False,
    reasoning_mode: str = "off",
) -> dict:
    """OpenAI-compatible chat-completions call returning the raw assistant
    message dict. Lower-level than ``_api_complete``: lets callers pass
    full message history, optional tools, and inspect ``tool_calls``.

    Returns a dict shaped like::

        {
            "ok": True,
            "content": "...",                   # may be ""
            "tool_calls": [                      # zero or more
                {"id": "...", "name": "...",
                 "arguments": "<json-string>"},
                ...
            ],
            "finish_reason": "stop"|"tool_calls"|"length"|...,
            "raw": {...}                          # full provider response
        }

    On transport failure or empty response, returns
    ``{"ok": False, "error": str, "raw": ...}`` — callers handle either
    branch. Never raises.
    """
    url = base.rstrip("/") + "/chat/completions"
    rsn = _reasoning_field(reasoning_mode)
    # Floor the budget when reasoning is on so think+answer both fit —
    # matters most here: each tool-loop iteration reasons, and a
    # truncated iteration aborts the whole loop.
    reasoning_on = rsn.get("enabled") is not False
    eff_max = max_tokens + _REASONING_HEADROOM_TOKENS if reasoning_on else max_tokens
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": eff_max,
        "reasoning": rsn,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    # Only advertise tools when the model may actually call them. On the
    # tool-loop's forced-ANSWER turn (tool_choice="none") we MUST drop
    # the tools array entirely: deepseek-v4-flash returns an EMPTY
    # completion when reasoning is on AND tool schemas are present but
    # uncallable — the root cause of the "tool_loop empty → falling
    # back" failures. With tools omitted on the answer turn the model
    # returns a full read (verified live). Sending tools with
    # tool_choice="none" is pointless anyway — "none" already forbids
    # calling them.
    if tools and tool_choice != "none":
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    prov = _provider_field(provider)
    if prov is not None:
        payload["provider"] = prov
    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
            timeout=120.0,
        )
        r.raise_for_status()
        data = r.json()
        _record_usage(data)
    except Exception as e:
        logger.error("LLM API chat call failed ({}): {}", model_id, e)
        return {"ok": False, "error": str(e), "raw": None}

    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    raw_calls = msg.get("tool_calls") or []
    tcs: list[dict] = []
    for c in raw_calls:
        fn = c.get("function") or {}
        tcs.append(
            {
                "id": c.get("id") or "",
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "",
            }
        )
    content = (msg.get("content") or "").strip()
    finish = choice.get("finish_reason")
    if not content and not tcs:
        # Empty and no tool calls — surface as failure with diagnostics.
        logger.warning(
            "LLM API {} empty chat response (finish={}, has_reasoning={}, "
            "error={})",
            model_id,
            finish,
            bool(msg.get("reasoning") or msg.get("reasoning_content")),
            data.get("error"),
        )
        return {"ok": False, "error": "empty", "raw": data}
    # Truncation watchdog — same intent as in _api_complete. A
    # non-empty content with finish=length means the model was cut
    # off mid-output. We don't auto-retry here because the caller
    # (tool loop) tracks iterations on its own; we just surface it.
    if content and finish == "length":
        logger.warning(
            "LLM chat {} truncated at max_tokens={} (visible_chars={}) — "
            "answer was mid-output. Bump the cap or shorten the prompt.",
            model_id, max_tokens, len(content),
        )
    return {
        "ok": True,
        "content": content,
        "tool_calls": tcs,
        "finish_reason": finish,
        "raw": data,
    }


def _api_complete(
    prompt: str, *, base: str, key: str, model_id: str,
    json_mode: bool, max_tokens: int, provider: str = "",
    reasoning_mode: str = "off",
) -> str:
    """OpenAI-compatible chat-completions call against any serverless
    provider (OpenRouter / Novita / Together / Groq / OpenAI / vLLM …)."""
    url = base.rstrip("/") + "/chat/completions"
    rsn = _reasoning_field(reasoning_mode)
    # Floor the budget when reasoning is on so think+answer both fit.
    reasoning_on = rsn.get("enabled") is not False
    eff_max = max_tokens + _REASONING_HEADROOM_TOKENS if reasoning_on else max_tokens
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": eff_max,
        "reasoning": rsn,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    prov = _provider_field(provider)
    if prov is not None:
        payload["provider"] = prov
    try:
        r = httpx.post(
            url,
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
            timeout=120.0,
        )
        r.raise_for_status()
        data = r.json()
        _record_usage(data)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = (msg.get("content") or "").strip()
        finish = choice.get("finish_reason")
        if content:
            # Watchdog: a non-empty response with finish_reason=length
            # means the model was mid-sentence when the cap fired —
            # the caller gets a truncated body. Bump the cap or
            # tighten the prompt. We surface this loud at INFO so
            # operators can spot regressions in /system logs without
            # spelunking through provider responses.
            if finish == "length":
                logger.warning(
                    "LLM {} truncated at max_tokens={} (visible_chars={}) — "
                    "answer was mid-output. Bump the cap or shorten the prompt.",
                    model_id, max_tokens, len(content),
                )
            return content
        # 200 OK but no content. The useless "None/None/None" empty log on the
        # generic path can't see why on the API transport — surface the real
        # cause here: finish_reason=length ⇒ a reasoning model spent the whole
        # budget thinking (bump max_tokens or suppress reasoning); a present
        # `reasoning`/`error` field tells the same story.
        reasoning = bool(msg.get("reasoning") or msg.get("reasoning_content"))
        err = data.get("error")
        logger.warning(
            "LLM API {} returned empty content "
            "(finish_reason={}, reasoning_present={}, error={}, prompt_len={})",
            model_id, finish, reasoning, err, len(prompt),
        )
        return LLM_ERROR_SENTINEL
    except Exception as e:
        logger.error("LLM API call failed ({}): {}", model_id, e)
        return LLM_ERROR_SENTINEL


class LLM:
    def __init__(self) -> None:
        # When both tiers are routed to a serverless API, Ollama is never
        # touched — don't require it to be installed/reachable (this is
        # what lets the bot run on a GPU-less VPS).
        if _all_api():
            self.client = None
            logger.info(
                "LLM: both tiers routed to serverless API — Ollama not used"
            )
            return
        self.client = ollama.Client(host=settings.OLLAMA_BASE_URL, timeout=300)
        self._verify_models()

    def _verify_models(self) -> None:
        try:
            listed = self.client.list()
        except Exception as e:
            logger.error("cannot reach Ollama at {}: {}", settings.OLLAMA_BASE_URL, e)
            raise

        # ollama client returns either {'models': [...]} or a ListResponse object
        models = getattr(listed, "models", None) or listed.get("models", [])
        available: set[str] = set()
        for m in models:
            name = getattr(m, "model", None) or (m.get("model") if isinstance(m, dict) else None)
            if name:
                available.add(name)

        # Only pull the tiers that actually run locally — an API-routed tier's
        # model id is a remote name, not an Ollama tag.
        wanted = []
        if _api_route("light") is None:
            wanted.append(settings.LLM_MODEL_LIGHT)
        if _api_route("heavy") is None:
            wanted.append(settings.LLM_MODEL_HEAVY)
        for tag in wanted:
            if tag in available or f"{tag}:latest" in available:
                continue
            logger.info("pulling missing model {} (this may take a while)...", tag)
            self.client.pull(tag)
            logger.info("pulled {}", tag)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout)),
        reraise=True,
    )
    def _generate(self, model_tag: str, prompt: str, *, json_mode: bool, max_tokens: int):
        return self.client.generate(
            model=model_tag,
            prompt=prompt,
            format="json" if json_mode else "",
            options=_options_for(model_tag, max_tokens),
        )

    def complete(
        self,
        prompt: str,
        *,
        model: Literal["light", "heavy"],
        json_mode: bool = False,
        max_tokens: int = 800,
        fallback_light: bool = False,
        grounded: bool = True,
        reasoning: str | None = None,
    ) -> str:
        """Run a completion. With `fallback_light=True`, a failed *heavy* call
        (timeout / empty / API error) retries once on the local light model
        instead of returning the sentinel — a slow CPU heavy model degrades
        the answer rather than dropping the whole cycle (synthesis/why_moved).

        `grounded=True` (default) prepends the date-stamped trust-rules +
        world-anchor preamble (`grounding.prepend`) so the LLM doesn't
        dismiss real 2026 news as fake using its 2024 prior. Pass
        `grounded=False` for prompts where the preamble would only add
        noise (self-tests, pure structural extraction without world
        context).

        `reasoning` overrides the global LLM_REASONING for this call
        ("low"/"medium"/"high" to think, anything else off). JSON calls
        are always forced off (see `_resolve_reasoning`). Default None →
        global setting.
        """
        if grounded:
            prompt = grounding.prepend(prompt)
        out = self._complete_once(
            prompt, model=model, json_mode=json_mode, max_tokens=max_tokens,
            reasoning=reasoning,
        )
        if out == LLM_ERROR_SENTINEL and model == "heavy" and fallback_light:
            logger.warning("heavy LLM failed — falling back to light model")
            # `prompt` is already grounded if it was going to be; don't
            # re-prepend here or `prepend` (which is idempotent) would
            # still spend a few CPU cycles re-checking. Skip cleanly.
            out = self._complete_once(
                prompt, model="light", json_mode=json_mode, max_tokens=max_tokens,
                reasoning=reasoning,
            )
        _STATS["calls"] += 1
        if out == LLM_ERROR_SENTINEL:
            _STATS["errors"] += 1
        return out

    def _complete_once(
        self,
        prompt: str,
        *,
        model: Literal["light", "heavy"],
        json_mode: bool = False,
        max_tokens: int = 800,
        reasoning: str | None = None,
    ) -> str:
        model_tag = (
            settings.LLM_MODEL_LIGHT if model == "light" else settings.LLM_MODEL_HEAVY
        )

        resp = None  # ollama response; stays None on the API path
        # Per-tier serverless route when configured (else local Ollama).
        route = _api_route(model)
        # Suppress Qwen thinking on structured calls regardless of transport
        # (the effective model name is the remote id on the API path).
        effective_model = route[2] if route is not None else model_tag
        prompt = _maybe_no_think(prompt, effective_model, json_mode)
        if route is not None:
            base, key, model_id = route
            text = _api_complete(
                prompt, base=base, key=key, model_id=model_id,
                json_mode=json_mode, max_tokens=max_tokens,
                provider=_api_provider(model),
                reasoning_mode=_resolve_reasoning(reasoning, json_mode=json_mode),
            )
            if text == LLM_ERROR_SENTINEL:
                return LLM_ERROR_SENTINEL
            model_tag = model_id
        else:
            try:
                resp = self._generate(
                    model_tag, prompt, json_mode=json_mode, max_tokens=max_tokens
                )
            except Exception as e:
                logger.error("LLM call to {} failed: {}", model_tag, e)
                return LLM_ERROR_SENTINEL
            text = getattr(resp, "response", None)
            if text is None and isinstance(resp, dict):
                text = resp.get("response", "")
            text = (text or "").strip()

        # Strip Qwen <think>…</think> blocks defensively if /no_think was ignored.
        if "<think>" in text and "</think>" in text:
            text = text.split("</think>", 1)[1].strip()

        # Strip code fences defensively even in json_mode.
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith("json"):
                text = text[4:].strip()

        # An empty completion is a failure, not a valid answer — surface it
        # as the sentinel (callers already handle it) with enough metadata to
        # diagnose (truncation vs. refusal vs. context overflow).
        if not text:
            done_reason = getattr(resp, "done_reason", None)
            eval_count = getattr(resp, "eval_count", None)
            prompt_eval = getattr(resp, "prompt_eval_count", None)
            logger.warning(
                "LLM {} returned empty (done_reason={}, eval_count={}, "
                "prompt_tokens={}, prompt_len={})",
                model_tag,
                done_reason,
                eval_count,
                prompt_eval,
                len(prompt),
            )
            return LLM_ERROR_SENTINEL

        return text


    def chat(
        self,
        messages: list[dict],
        *,
        model: Literal["light", "heavy"] = "heavy",
        max_tokens: int = 800,
        tools: list[dict] | None = None,
        tool_choice: str | dict = "auto",
        temperature: float = 0.6,
        grounded: bool = True,
        reasoning: str | None = None,
    ) -> dict:
        """Lower-level chat interface that accepts a full messages array
        and optional ``tools``. Used by the tool-call loop. Returns the
        ``_api_chat`` result dict so callers can branch on
        ``tool_calls``.

        Only the serverless route supports tools today; Ollama path
        ignores ``tools`` and just returns content. Grounded preamble
        injected as a system message when requested.

        `reasoning` overrides the global LLM_REASONING for this call
        (default None → global, which defaults off so the model doesn't
        burn the token budget on hidden thinking — see `_reasoning_field`).
        """
        if grounded and messages:
            preamble = grounding.prepend("").strip()
            if preamble:
                if messages[0].get("role") == "system":
                    # The tool-loop callers (why_moved, convergence,
                    # copilot) already pre-load their own system
                    # instructions. Without this merge the grounding
                    # preamble was silently skipped on every tool-driven
                    # cycle, which is the entire path the bot uses for
                    # live reasoning. Prepend the date/world anchor INTO
                    # the existing system message instead.
                    first = dict(messages[0])
                    existing = first.get("content") or ""
                    first["content"] = f"{preamble}\n\n{existing}"
                    messages = [first, *messages[1:]]
                else:
                    messages = [
                        {"role": "system", "content": preamble},
                        *messages,
                    ]

        route = _api_route(model)
        if route is not None:
            base, key, model_id = route
            res = _api_chat(
                messages,
                base=base, key=key, model_id=model_id,
                max_tokens=max_tokens,
                provider=_api_provider(model),
                reasoning_mode=_resolve_reasoning(reasoning, json_mode=False),
                tools=tools,
                tool_choice=tool_choice,
                temperature=temperature,
            )
            _STATS["calls"] += 1
            if not res.get("ok"):
                _STATS["errors"] += 1
            return res

        # Ollama path — flatten messages into a prompt; ignore tools.
        # Tool-calling is API-only; the local-Ollama path is the
        # degraded fallback where the bot keeps reasoning one-shot.
        flat = "\n\n".join(
            f"[{m['role']}]\n{m.get('content') or ''}"
            for m in messages
            if m.get("content")
        )
        out = self._complete_once(
            flat, model=model, max_tokens=max_tokens, json_mode=False
        )
        if out == LLM_ERROR_SENTINEL:
            _STATS["calls"] += 1
            _STATS["errors"] += 1
            return {"ok": False, "error": "ollama_empty", "raw": None}
        _STATS["calls"] += 1
        return {
            "ok": True, "content": out, "tool_calls": [],
            "finish_reason": "stop", "raw": None,
        }


_singleton: LLM | None = None


def get_llm() -> LLM:
    global _singleton
    if _singleton is None:
        _singleton = LLM()
    return _singleton
