-- ============================================================
--  Transcriptor – Supabase Schema
--  Pega esto en el SQL Editor de tu proyecto Supabase y ejecuta
-- ============================================================

-- Perfiles: saldo + contador gratuito diario
CREATE TABLE IF NOT EXISTS public.profiles (
  id               UUID    PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  credits_cents    INTEGER NOT NULL DEFAULT 0,
  free_used_today  INTEGER NOT NULL DEFAULT 0,
  free_reset_date  DATE    NOT NULL DEFAULT CURRENT_DATE,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Trigger: crear perfil automáticamente al registrarse
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  INSERT INTO public.profiles (id) VALUES (NEW.id)
  ON CONFLICT (id) DO NOTHING;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

-- Límite por IP para usuarios anónimos
CREATE TABLE IF NOT EXISTS public.ip_usage (
  ip         TEXT PRIMARY KEY,
  used_today INTEGER  NOT NULL DEFAULT 0,
  reset_date DATE     NOT NULL DEFAULT CURRENT_DATE
);

-- Transcripciones
CREATE TABLE IF NOT EXISTS public.transcriptions (
  id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id     UUID REFERENCES auth.users(id) ON DELETE SET NULL,
  ip          TEXT,
  url         TEXT        NOT NULL,
  platform    TEXT        NOT NULL,
  language    TEXT,
  text        TEXT        NOT NULL,
  cost_cents  INTEGER     NOT NULL DEFAULT 0,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Pagos / recargas
CREATE TABLE IF NOT EXISTS public.payments (
  id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id               UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  stripe_session_id     TEXT UNIQUE,
  stripe_payment_intent TEXT,
  amount_cents          INTEGER NOT NULL,
  status                TEXT    NOT NULL DEFAULT 'pending',
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Guiones guardados por el usuario
CREATE TABLE IF NOT EXISTS public.saved_scripts (
  id          UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id     UUID        REFERENCES auth.users(id) ON DELETE CASCADE,
  style       TEXT,
  content     TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- RLS activado (el backend usa service role, así que bypassa)
ALTER TABLE public.profiles       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.transcriptions  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payments        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ip_usage        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.saved_scripts   ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users see own scripts" ON public.saved_scripts
  FOR ALL USING (auth.uid() = user_id);
