"""Analytics layer — pure read-side computations over the bot's
accumulated DB. Each submodule exposes a few functions that the API
calls; no side effects, no DB writes, no LLM calls.

- ``hot``: composite "what should I look at right now" tickers
- ``calibration``: Brier + reliability of the call scorecard
- ``dedupe``: news fingerprinting / clustering
- ``attribution``: realised P&L attributed by source / conviction /
  ticker / direction
"""
