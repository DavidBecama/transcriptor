-- brain_progress — % del Cerebro gamificado (Fathom 18/06): el onboarding lo deja en
-- 35% y sube +3-6% cada día al completar EL ejercicio diario (1/día, alterna hooks/
-- guiones). Columnas en profiles (per-usuario; multi-marca = follow-up).
--   · brain_progress      : % actual (0-100). NULL hasta que el onboarding lo siembra a 35.
--   · brain_exercise_date : fecha (UTC) del último ejercicio completado → candado diario.
--   · brain_last_gain     : cuánto subió en el último ejercicio (para el copy "+X%").
-- El backend escribe con service_role (bypassa RLS); profiles ya tiene RLS activado.

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS brain_progress      SMALLINT,
  ADD COLUMN IF NOT EXISTS brain_exercise_date DATE,
  ADD COLUMN IF NOT EXISTS brain_last_gain     SMALLINT;
