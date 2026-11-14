"""Full autonomy stops at the exchange. Asserted, not assumed.

The agents may repair data, seed aliases, run retention and refit calibration
on their own. They must never place an order. There is no API key anywhere in
the project, which makes it structurally impossible today -- but "structurally
impossible today" is one well-meaning commit from "possible". These tests fail
that commit.
"""

from __future__ import annotations

import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from thugsak.clients import kalshi  # noqa: E402

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "thugsak"
# `/orderbook` is a public read endpoint and the one legitimate "order"
# substring, so the path check is anchored to the write surfaces specifically.
FORBIDDEN_PATH_RE = re.compile(
    r"""["'][^"'\n]*/(portfolio|orders?(?!book)|positions?|fills?|batch)\b[^"'\n]*["']""",
    re.I,
)
FORBIDDEN_NAME_RE = re.compile(r"\b(orders?|portfolio|positions?|fills?|buy|sell|place)\b", re.I)
FORBIDDEN_VERBS = re.compile(r"\.(post|put|patch|delete)\(", re.I)


def test_kalshi_client_has_no_write_methods():
    public = [n for n, _ in inspect.getmembers(kalshi.KalshiClient, inspect.isfunction)
              if not n.startswith("_")]
    for name in public:
        assert not FORBIDDEN_NAME_RE.search(name), name
    # And the one read endpoint that contains the word is still there.
    assert "orderbook" in public


def test_kalshi_client_never_calls_a_write_verb():
    src = inspect.getsource(kalshi)
    assert not FORBIDDEN_VERBS.search(src), FORBIDDEN_VERBS.search(src)


def test_no_module_touches_a_portfolio_or_order_path():
    offenders = []
    for p in SRC_ROOT.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        # Prose may say "never touches an order endpoint"; a string literal
        # shaped like a URL path to one is what this forbids.
        for m in FORBIDDEN_PATH_RE.finditer(text):
            offenders.append((str(p.relative_to(SRC_ROOT)), m.group(0)))
    assert offenders == [], offenders


def test_no_api_key_plumbing_exists():
    # A signing key is the precondition for any write. Its absence is the
    # project's actual safety property; keep it visible.
    for p in SRC_ROOT.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        assert "KALSHI_API_KEY" not in text, p
        assert "KALSHI_PRIVATE_KEY" not in text, p
        assert "Authorization" not in text or "kalshi" not in p.name.lower(), p


def test_agents_hold_no_write_builtins():
    from thugsak.agents import runner
    for name in ("Bash", "Write", "Edit"):
        assert name in runner.WRITE_TOOLS
