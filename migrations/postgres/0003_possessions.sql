-- Possessions per game, for the efficiency (KenPom-style) model.
--
-- Points per 100 possessions is the unit of college basketball ratings: a
-- 70-65 win in a 60-possession game is a far better performance than the
-- same score in 75. Filled from Barttorvik's season file, which records
-- each game's possession count. NULL where no source supplied one.
ALTER TABLE games ADD COLUMN IF NOT EXISTS possessions DOUBLE PRECISION;
