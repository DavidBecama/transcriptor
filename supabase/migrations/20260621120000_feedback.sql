-- feedback — reportes de bug / ideas del usuario (menú de cuenta → "Enviar feedback").
-- El front inserta vía POST /api/feedback (texto + tipo + captura opcional comprimida
-- en cliente y enviada base64). Un admin revisa en /admin/api/feedback y, si el bug es
-- REAL, abona créditos al autor (credits_awarded → suma a profiles.credits_cents).
-- Recompensa por defecto: bug confirmado = 25 cr · idea implementada = 10 cr (overrideable).
-- El backend la lee/escribe con service_role (bypassa RLS); RLS activado SIN policy =
-- solo service_role accede (el usuario nunca lee feedback ajeno por la Data API).

CREATE TABLE IF NOT EXISTS public.feedback (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  type            TEXT NOT NULL DEFAULT 'bug',     -- 'bug' | 'idea'
  text            TEXT NOT NULL,                   -- descripción del usuario
  page            TEXT,                            -- pestaña/contexto desde donde se reportó
  plan            TEXT,                            -- plan del usuario al reportar
  image_b64       TEXT,                            -- captura opcional (JPEG comprimido, dataURL)
  status          TEXT NOT NULL DEFAULT 'new',     -- 'new' | 'confirmed' | 'implemented' | 'rejected'
  credits_awarded INTEGER NOT NULL DEFAULT 0,      -- créditos ya abonados (idempotencia del premio)
  admin_note      TEXT,                            -- nota interna de la resolución
  resolved_by     UUID REFERENCES auth.users(id), -- admin que lo resolvió
  resolved_at     TIMESTAMPTZ,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Listado típico del panel admin: cola por estado, más recientes primero.
CREATE INDEX IF NOT EXISTS feedback_status_idx
  ON public.feedback (status, created_at DESC);
-- "Mis reportes" de un usuario (si algún día se muestran en su cuenta).
CREATE INDEX IF NOT EXISTS feedback_user_idx
  ON public.feedback (user_id, created_at DESC);

ALTER TABLE public.feedback ENABLE ROW LEVEL SECURITY;  -- service_role only (sin policy)
