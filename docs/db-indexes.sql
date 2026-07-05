-- Índices recomendados para las queries calientes (auditoría 2026-07-05, ver ANALISIS.md).
-- PARA DAVID: aplicar en Supabase (SQL editor) en horario valle. CONCURRENTLY no bloquea
-- escrituras pero no puede ir dentro de una transacción (ejecutar de una en una).
-- Antes de aplicar, validar con EXPLAIN ANALYZE que el plan actual hace seq scan.

-- Feed de señales: reels por creador ordenados por fecha (order posted_at desc, filtro creator_id)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_crg_creator_posted
  ON creator_reels_global (creator_id, posted_at DESC);

-- Sugerencias/semilla: creadores por nicho amplio + fuente
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cg_niche_source
  ON creators_global (niche, niche_source);

-- Semilla por overlap de subniches (array) — GIN
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cg_subniches_gin
  ON creators_global USING GIN (subniches);

-- Guiones del usuario (listados y contadores por user + fecha)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_scripts_user_created
  ON scripts (user_id, created_at DESC);

-- Competidores seguidos por usuario (se consulta en cada carga del radar)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_utc_user
  ON user_tracked_creators (user_id);

-- Valoraciones del cerebro por usuario (gates diarios y anti-farm del levelup)
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_brain_user_source
  ON brain_ratings (user_id, source);
