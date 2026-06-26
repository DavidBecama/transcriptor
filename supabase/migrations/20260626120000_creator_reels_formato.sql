-- Referencias visuales de formato (feature/guiones-formatos) — clasifica el FORMATO de
-- grabación de cada reel del pool (selfie/pizarra/podcast/escritorio/broll-vo) para
-- mostrar EJEMPLOS reales del mismo formato en la tarjeta «Cómo grabarlo».
-- Additive + idempotente: el clasificador (background) y el feed degradan limpio si la
-- columna aún no existe; se rellena al transcribir cada reel.
ALTER TABLE creator_reels_global
  ADD COLUMN IF NOT EXISTS formato TEXT;

-- Índice parcial para el feed de ejemplos por formato (solo filas ya clasificadas).
CREATE INDEX IF NOT EXISTS idx_creator_reels_formato
  ON creator_reels_global (formato)
  WHERE formato IS NOT NULL;
