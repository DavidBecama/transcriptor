-- ═══════════════════════════════════════════════════════════════
-- BACKFILL de subniches en cuentas/proyectos ANTIGUOS con niche pero subniches vacíos.
-- El matching de «Sugerencias de hoy» casa por SUBNICHE overlap → con subniches vacíos da 0.
-- Derivamos al menos el NICHO AMPLIO como subniche-tag (normalizado igual que _norm_tag:
-- minúsculas, sin acentos, sin caracteres raros) → el propio nicho ("tecnologia",
-- "inteligencia artificial"…) es un subniche válido en la taxonomía rica del pool.
--
-- Seguro/idempotente: solo toca filas con niche no vacío y subniches vacíos. Re-ejecutable.
-- ═══════════════════════════════════════════════════════════════

UPDATE public.projects p
SET subniches = ARRAY[ trim(regexp_replace(lower(translate(p.niche, 'áéíóúñ', 'aeioun')), '[^a-z0-9 _-]', '', 'g')) ]
WHERE p.niche IS NOT NULL AND p.niche <> ''
  AND (p.subniches IS NULL OR p.subniches = '{}')
  AND trim(regexp_replace(lower(translate(p.niche, 'áéíóúñ', 'aeioun')), '[^a-z0-9 _-]', '', 'g')) <> '';

UPDATE public.profiles pr
SET subniches = ARRAY[ trim(regexp_replace(lower(translate(pr.niche, 'áéíóúñ', 'aeioun')), '[^a-z0-9 _-]', '', 'g')) ]
WHERE pr.niche IS NOT NULL AND pr.niche <> ''
  AND (pr.subniches IS NULL OR pr.subniches = '{}')
  AND trim(regexp_replace(lower(translate(pr.niche, 'áéíóúñ', 'aeioun')), '[^a-z0-9 _-]', '', 'g')) <> '';
