-- ═══════════════════════════════════════════════════════════════
-- v1 feed diario del radar — anti-repetición de sugerencias de competidor.
-- Tabla: user_dismissed_creators (creadores que el usuario DESCARTÓ del feed →
-- no volver a sugerir). El cooldown del refresco manual y el anti-repeat blando
-- viven en Redis (sin schema). Aísla por marca (project_id, NULL = marca "default").
--
-- Aplicar a Supabase (additive, seguro: tabla nueva aislada) vía MCP apply_migration
-- o SQL Editor. Idempotente.
-- ═══════════════════════════════════════════════════════════════

BEGIN;

CREATE TABLE IF NOT EXISTS public.user_dismissed_creators (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  creator_id   uuid NOT NULL REFERENCES public.creators_global(id) ON DELETE CASCADE,
  project_id   uuid,            -- NULL = marca "default"; sin FK (limpieza no crítica)
  dismissed_at timestamptz NOT NULL DEFAULT now()
);

-- Unicidad por (user, creator): PRIMARY KEY no vale con project_id nullable (forzaría
-- NOT NULL). Dos índices únicos PARCIALES cubren marca default (NULL) y por-marca.
CREATE UNIQUE INDEX IF NOT EXISTS uq_dismissed_default
  ON public.user_dismissed_creators (user_id, creator_id)
  WHERE project_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_dismissed_project
  ON public.user_dismissed_creators (user_id, creator_id, project_id)
  WHERE project_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_dismissed_user
  ON public.user_dismissed_creators (user_id);

-- RLS: solo el dueño (el backend usa service_role → bypassa; esto protege accesos directos).
ALTER TABLE public.user_dismissed_creators ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS dismissed_read ON public.user_dismissed_creators;
CREATE POLICY dismissed_read ON public.user_dismissed_creators FOR SELECT TO authenticated
  USING (user_id = auth.uid());
DROP POLICY IF EXISTS dismissed_write ON public.user_dismissed_creators;
CREATE POLICY dismissed_write ON public.user_dismissed_creators FOR ALL TO authenticated
  USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

COMMIT;
