-- ═══════════════════════════════════════════════════════════════
-- BACKFILL del nicho por proyecto (#223): los proyectos creados ANTES de #223 quedaron
-- con niche=NULL / subniches='{}' → «Sugerencias de hoy» pedía definir nicho aunque el
-- usuario YA dio el suyo en el onboarding (profiles.niche/subniches). Rellenamos cada
-- proyecto vacío con el nicho del onboarding de SU dueño.
--
-- Seguro/idempotente: solo toca proyectos sin nicho propio cuyo dueño SÍ tiene nicho en
-- el perfil. No pisa proyectos que ya fijaron su nicho. Re-ejecutable sin efecto.
-- ═══════════════════════════════════════════════════════════════

UPDATE public.projects p
SET niche     = COALESCE(NULLIF(p.niche, ''), pr.niche),
    subniches = CASE WHEN p.subniches IS NULL OR p.subniches = '{}'
                     THEN COALESCE(pr.subniches, '{}')
                     ELSE p.subniches END
FROM public.profiles pr
WHERE p.user_id = pr.id
  AND (p.niche IS NULL OR p.niche = '')
  AND (p.subniches IS NULL OR p.subniches = '{}')
  AND ((pr.niche IS NOT NULL AND pr.niche <> '')
       OR (pr.subniches IS NOT NULL AND pr.subniches <> '{}'));
