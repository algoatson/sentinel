"""LLM tool-call infrastructure.

A small, deliberately generic interface for letting an LLM call Python
functions during reasoning. The design goals:

  * Adding a new tool is a single ``@register_tool`` decorator + a docstring.
    The function's signature + the JSON schema in the decorator drive
    everything the LLM sees and the dispatcher needs.
  * Tools are *just functions* — they have no LLM dependency, no special
    state, no async requirement. Every tool is unit-testable in isolation.
  * Pipelines pick the registry they want and pass it to ``tool_loop`` —
    no global state. A research-desk pipeline can expose more tools than
    a 5-minute autonomous loop; a copilot panel can expose user-facing
    tools that the autonomous loop must not be allowed to call.
  * The loop falls back gracefully: if the LLM tier doesn't support tool
    calling (Ollama path, or a model that refuses), the loop runs one
    shot with the same prompt and returns plain text. Callers don't need
    a separate code path.

Wire-level interop matches OpenAI's chat-completions tool schema. That's
the format OpenRouter, OpenAI, Anthropic-via-OpenRouter, DeepSeek, and
most modern providers speak natively.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from loguru import logger

from . import llm_tool_log
from .llm import LLM_ERROR_SENTINEL, get_llm


# ── Tool definition ────────────────────────────────────────────────────


@dataclass
class Tool:
    """A function the LLM may call. ``parameters`` is JSON Schema —
    keep it small and concrete; the LLM uses it both to pick the tool
    AND to construct the call arguments."""
    name: str
    description: str
    parameters: dict
    fn: Callable[..., Any]
    # When True the tool result is collapsed to a short JSON string in
    # the conversation. Set False for tools that return prose the
    # model should reason over verbatim (rare).
    json_result: bool = True

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Bag of tools. Created per-pipeline. Decorator-friendly."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} already registered")
        self._tools[tool.name] = tool
        return tool

    def tool(
        self,
        *,
        description: str,
        parameters: dict,
        json_result: bool = True,
    ) -> Callable[[Callable[..., Any]], Tool]:
        """Decorator form: ``@registry.tool(description=…, parameters=…)``.

        The wrapped function's ``__name__`` becomes the tool name.
        """

        def deco(fn: Callable[..., Any]) -> Tool:
            return self.register(
                Tool(
                    name=fn.__name__,
                    description=description,
                    parameters=parameters,
                    fn=fn,
                    json_result=json_result,
                )
            )

        return deco

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def openai_schemas(self) -> list[dict]:
        return [t.to_openai() for t in self._tools.values()]

    def call(self, name: str, args: dict) -> Any:
        """Dispatch a tool call. Returns the tool's return value (or an
        ``{"error": …}`` payload on failure). Never raises — the model
        needs *something* to react to."""
        t = self._tools.get(name)
        if t is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            res = t.fn(**args)
        except TypeError as e:
            # Bad argument shape — surface as a model-correctable error
            # rather than nuking the loop.
            return {"error": f"bad arguments to {name}: {e}"}
        except Exception as e:
            logger.exception("tool {} raised: {}", name, e)
            return {"error": f"{name} failed: {e}"}
        return res

    def empty(self) -> bool:
        return not self._tools


# ── Tool loop ──────────────────────────────────────────────────────────


@dataclass
class LoopResult:
    """Return shape of ``tool_loop``."""
    text: str
    iterations: int
    tool_calls: list[dict] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    # The full message trace so callers can log / replay. Trimmed of the
    # system grounding preamble.
    transcript: list[dict] = field(default_factory=list)


def _stringify(result: Any, *, as_json: bool) -> str:
    if isinstance(result, str):
        return result
    if as_json:
        try:
            return json.dumps(result, default=str)
        except (TypeError, ValueError):
            return str(result)
    return str(result)


def tool_loop(
    *,
    user_prompt: str,
    system_prompt: str = "",
    registry: ToolRegistry,
    model: Literal["light", "heavy"] = "heavy",
    max_tokens: int = 600,
    max_iterations: int = 3,
    grounded: bool = True,
    temperature: float = 0.6,
    pipeline: str = "unknown",
    ticker: str | None = None,
) -> LoopResult:
    """Run a tool-calling conversation. Stops when the model returns a
    plain content message OR ``max_iterations`` is reached OR the API
    can't handle tools (graceful one-shot fallback).

    * ``max_iterations`` counts assistant turns. Each iteration may
      contain multiple tool calls. Default 3 keeps the cost modest.
    * ``max_tokens`` per iteration. The model is told it's running on
      a budget so it doesn't loop just to explore.
    """
    if registry.empty():
        # Nothing to call — degrade to a one-shot completion.
        llm = get_llm()
        text = llm.complete(
            user_prompt, model=model, max_tokens=max_tokens, grounded=grounded
        )
        ok = text and text != LLM_ERROR_SENTINEL
        return LoopResult(
            text=text if ok else "",
            iterations=1,
            ok=bool(ok),
            error=None if ok else "llm_failed",
        )

    llm = get_llm()
    messages: list[dict] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    schemas = registry.openai_schemas()
    all_calls: list[dict] = []
    iterations = 0

    while iterations < max_iterations:
        iterations += 1
        res = llm.chat(
            messages,
            model=model,
            max_tokens=max_tokens,
            tools=schemas,
            tool_choice="auto" if iterations < max_iterations else "none",
            temperature=temperature,
            grounded=grounded,
        )
        if not res.get("ok"):
            # First-iteration failure → graceful one-shot fallback so a
            # tool-incapable model still gets a chance to answer.
            if iterations == 1:
                fallback = llm.complete(
                    user_prompt, model=model, max_tokens=max_tokens,
                    grounded=grounded,
                )
                ok = fallback and fallback != LLM_ERROR_SENTINEL
                return LoopResult(
                    text=fallback if ok else "",
                    iterations=1,
                    ok=bool(ok),
                    error=res.get("error") or "tools_unsupported",
                )
            return LoopResult(
                text="",
                iterations=iterations,
                ok=False,
                error=res.get("error") or "llm_failed",
                tool_calls=all_calls,
                transcript=messages,
            )

        content = res.get("content") or ""
        tcs = res.get("tool_calls") or []
        # Echo the assistant turn back into messages so a follow-up
        # call has the full conversation. OpenAI's spec is happy with
        # both content + tool_calls in the same message.
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if content:
            assistant_msg["content"] = content
        if tcs:
            assistant_msg["tool_calls"] = [
                {
                    "id": c["id"] or f"call_{iterations}_{i}",
                    "type": "function",
                    "function": {
                        "name": c["name"],
                        "arguments": c["arguments"] or "{}",
                    },
                }
                for i, c in enumerate(tcs)
            ]
        messages.append(assistant_msg)

        if not tcs:
            # No tool requests — we're done.
            return LoopResult(
                text=content,
                iterations=iterations,
                ok=bool(content),
                tool_calls=all_calls,
                transcript=messages,
            )

        # Execute each tool call and append the result.
        for c in tcs:
            name = c["name"]
            raw_args = c.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if raw_args else {}
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError as e:
                args_err = {"error": f"bad JSON args: {e}"}
                result_str = json.dumps(args_err)
                all_calls.append(
                    {"name": name, "arguments": raw_args,
                     "result": args_err, "iteration": iterations}
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": c["id"] or f"call_{iterations}",
                    "name": name,
                    "content": result_str,
                })
                continue
            tool = registry._tools.get(name)
            t0 = time.monotonic()
            result = registry.call(name, args)
            took_ms = (time.monotonic() - t0) * 1000
            all_calls.append(
                {"name": name, "arguments": args,
                 "result": result, "iteration": iterations}
            )
            # Telemetry — best-effort; never raises.
            llm_tool_log.record(
                pipeline=pipeline,
                tool=name,
                arguments=args,
                result=result,
                ticker=ticker,
                iteration=iterations,
                took_ms=took_ms,
            )
            messages.append({
                "role": "tool",
                "tool_call_id": c["id"] or f"call_{iterations}",
                "name": name,
                "content": _stringify(
                    result,
                    as_json=tool.json_result if tool else True,
                ),
            })

    # Iteration cap reached without a plain-text answer. Try one last
    # call with tool_choice="none" to force the model to summarise
    # what it has.
    res = llm.chat(
        messages,
        model=model,
        max_tokens=max_tokens,
        tools=schemas,
        tool_choice="none",
        temperature=temperature,
        grounded=False,  # already in the chain
    )
    text = (res.get("content") or "") if res.get("ok") else ""
    return LoopResult(
        text=text,
        iterations=iterations + 1,
        ok=bool(text),
        tool_calls=all_calls,
        transcript=messages,
        error=None if text else (res.get("error") or "no_final_answer"),
    )
