-- ═══════════════════════════════════════════════════════════════
-- Nicho EXPLÍCITO por proyecto/marca: cada marca tiene su propio nicho (broad) +
-- subniche-tags, fijado al crear y editable en ajustes. «Sugerencias de hoy» usa el
-- nicho del PROYECTO activo (no derivado de competidores ni del perfil compartido) →
-- funciona en TODAS las marcas y se mantiene on-niche.
--
-- Aplicar a Supabase (additive, seguro: columnas nuevas con default). Idempotente.
-- ═══════════════════════════════════════════════════════════════

ALTER TABLE public.projects ADD COLUMN IF NOT EXISTS niche text;
ALTER TABLE public.projects ADD COLUMN IF NOT EXISTS subniches text[] NOT NULL DEFAULT '{}';

-- Índice GIN para overlaps (mismo patrón que creators_global.subniches), por si se
-- consulta por subnichos del proyecto en el futuro.
CREATE INDEX IF NOT EXISTS idx_projects_subniches ON public.projects USING gin (subniches);
