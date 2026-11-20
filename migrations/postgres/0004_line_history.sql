-- Line movement: each sportsbook's line per game and market, as first seen
-- by the hourly job and as last seen. A big gap between the two (a
-- moneyline that went from -110 to +210) is flagged on the game page.
--
-- `side` is home / away (moneyline), home (spread, in the home team's book
-- convention) or over (total). `line` is the spread or total number, NULL
-- for a moneyline; `odds` is the American price.
CREATE TABLE IF NOT EXISTS line_history (
    game_id     TEXT NOT NULL,
    sportsbook  TEXT NOT NULL,
    market      TEXT NOT NULL,
    side        TEXT NOT NULL,
    open_line   DOUBLE PRECISION,
    open_odds   DOUBLE PRECISION,
    open_at     TEXT NOT NULL,
    last_line   DOUBLE PRECISION,
    last_odds   DOUBLE PRECISION,
    last_at     TEXT NOT NULL,
    PRIMARY KEY (game_id, sportsbook, market, side)
);
CREATE INDEX IF NOT EXISTS ix_line_history_game ON line_history(game_id);

ALTER TABLE line_history ENABLE ROW LEVEL SECURITY;
