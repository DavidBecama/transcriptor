-- brain_ratings — Brain "Entrenar": votos del usuario sobre hooks/guiones para afinar su voz.
-- (Fathom 18/06: David quiere que el 👍/👎 + "¿cómo lo dirías tú?" afine de verdad → estos
--  registros alimentan el prompt de generación, junto al voice_profile de la marca.)
-- El backend la lee/escribe con service_role (bypassa RLS); la policy "own ratings"
-- protege el acceso vía Data API por si lo usa un usuario autenticado.

CREATE TABLE IF NOT EXISTS public.brain_ratings (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  brand_id    TEXT NOT NULL DEFAULT '',         -- marca/cliente (multi-marca), como en voice_profiles
  kind        TEXT NOT NULL DEFAULT 'hook',      -- 'hook' | 'guion'
  content     TEXT NOT NULL,                     -- el texto que se valoró (el hook/guion mostrado)
  rating      SMALLINT NOT NULL,                 -- 1 = me gusta (👍) · -1 = no es mío (👎)
  suggestion  TEXT,                              -- "¿cómo lo dirías tú?" (opcional) → va al prompt
  source      TEXT,                              -- de dónde salió: 'nicho' | 'competidor' | 'idea'
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Lectura típica: los últimos votos de un usuario/marca para construir el bloque de prompt.
CREATE INDEX IF NOT EXISTS brain_ratings_user_brand_idx
  ON public.brain_ratings (user_id, brand_id, created_at DESC);

ALTER TABLE public.brain_ratings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "own ratings" ON public.brain_ratings;
CREATE POLICY "own ratings" ON public.brain_ratings
  FOR ALL TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);
