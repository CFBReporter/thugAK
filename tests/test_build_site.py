"""The static site's file layout must match web/src/api.ts staticUrl()."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_site", ROOT / "scripts" / "build_site.py")
bs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bs)  # type: ignore[union-attr]

OUT = Path("out")


def rel(p: Path) -> str:
    return p.relative_to(OUT).as_posix()


def test_paths_match_the_frontend_scheme():
    assert rel(bs.data_path(OUT, "health")) == "data/health.json"
    assert rel(bs.data_path(OUT, "games", league="nba", date="2026-11-19")) == "data/games/nba/2026-11-19.json"
    assert rel(bs.data_path(OUT, "recommendation", league="nfl", game_id="nfl:espn:401")) == \
        "data/recommendation/nfl/nfl_espn_401.json"
    assert rel(bs.data_path(OUT, "model/log")) == "data/model-log/all.json"
    assert rel(bs.data_path(OUT, "model/accuracy", league="ncaamb")) == "data/model-accuracy/ncaamb.json"


def test_errors_carry_their_status():
    assert bs.as_response({"error": "unknown game_id 'x'"})["__status"] == 404
    r = bs.as_response({"game_id": "g", "error": "declined", "warnings": ["thin history"]})
    assert r["__status"] == 422 and "thin history" in r["detail"]
    assert bs.as_response({"ok": 1}) == {"ok": 1}
