"""The agent layer: deterministic tools that an LLM loop orchestrates.

Split on purpose. Everything in `*_tools.py` is plain async Python that fetches
and stores through the existing clients, is unit-tested with fake sessions,
and produces the same rows every run. The Claude Agent SDK loops in
`runner.py` decide *what* to call, notice anomalies, resolve ambiguous names,
and write reports -- judgement, not fetching. Numbers the model trusts must
not vary with an LLM's mood.
"""
