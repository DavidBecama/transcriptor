-- Ítem 6 (batch radar v0.25) — persistir COMPARTIDOS de los reels de competidores.
-- Apify ya los devuelve (payload con includeSharesCount:True) pero no se guardaban.
-- Additive + idempotente: seguro de aplicar antes o después del deploy del código
-- (el scrape y el feed degradan limpio si la columna aún no existe).
ALTER TABLE creator_reels_global
  ADD COLUMN IF NOT EXISTS shares BIGINT DEFAULT 0;
