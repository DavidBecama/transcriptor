-- Índices para las queries calientes (auditoría 2026-07-05, ver ANALISIS.md).
-- ESTADO: aplicados en Supabase por Leo el 2026-07-05.
--
-- POST-MORTEM: schema.sql solo documenta 4 índices, pero la DB real tenía ~50 (David los
-- ha ido creando directamente sin actualizar schema.sql). Al aplicar estos 6, 3 resultaron
-- DUPLICADOS de índices que ya existían con otro nombre → se borraron (ver más abajo).
-- Aporte NETO: los 3 de la sección A. TODO: regenerar schema.sql desde la DB real.

-- ── A. APLICADOS Y ÚTILES (quedan) ───────────────────────────────────────────

-- Sugerencias/semilla: creadores por nicho + fuente. Supera a idx_creators_global_niche
-- (solo `niche`), que queda redundante — su borrado lo decide David.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cg_niche_source
  ON creators_global (niche, niche_source);

-- Guiones del usuario (listados y contadores por user + fecha).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_scripts_user_created
  ON scripts (user_id, created_at DESC);

-- Valoraciones del cerebro por usuario (gates diarios y anti-farm del levelup).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_brain_user_source
  ON brain_ratings (user_id, source);

-- ── B. CREADOS PERO DUPLICADOS → BORRADOS (dejar constancia, NO recrear) ──────
-- idx_crg_creator_posted  == idx_reels_creator_posted      (creator_reels_global: creator_id, posted_at DESC)
-- idx_cg_subniches_gin    == idx_creators_global_subniches (creators_global: GIN subniches)
-- idx_utc_user            ⊂  idx_tracked_user_active        (user_tracked_creators: user_id WHERE archived_at IS NULL — parcial, mejor)
-- DROP INDEX CONCURRENTLY IF EXISTS idx_crg_creator_posted;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_cg_subniches_gin;
-- DROP INDEX CONCURRENTLY IF EXISTS idx_utc_user;
