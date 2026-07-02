-- ============================================================
--  ReelScript – Supabase Schema (snapshot)
--
--  SNAPSHOT del esquema de PRODUCCIÓN a fecha 2026-06-03.
--  Verificado vía MCP contra la DB real.
--
--  Propósito: documentación-sync + setup canónico de una DB NUEVA
--  (todo CREATE TABLE IF NOT EXISTS, idempotente).
--
--  ⚠️  ESTE ARCHIVO NO SUSTITUYE A LAS MIGRACIONES.
--  La DB de prod YA tiene todo esto aplicado; reescribir este archivo
--  NO implica migración. Cambios de esquema en prod siguen yendo por
--  supabase/migrations/. Esto es el retrato fiel del estado actual
--  para arrancar entornos limpios y servir de fuente de verdad legible.
--
--  Pega esto en el SQL Editor de un proyecto Supabase NUEVO y ejecuta.
-- ============================================================

-- ════════════════════════════════════════════════════════════
--  profiles — perfil de usuario: saldo, plan, contadores gratuitos
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.profiles (
  id                          UUID    PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  credits_cents               INTEGER NOT NULL DEFAULT 0,
  free_used_today             INTEGER NOT NULL DEFAULT 0,
  free_reset_date             DATE    NOT NULL DEFAULT CURRENT_DATE,
  created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  free_adapt_used_today       INTEGER DEFAULT 0,
  free_adapt_reset_date       TEXT    DEFAULT '',
  plan                        TEXT    DEFAULT 'free',
  stripe_subscription_id      TEXT,
  plan_expires_at             TIMESTAMPTZ,
  monthly_usage               INTEGER DEFAULT 0,
  usage_reset_at              TIMESTAMPTZ DEFAULT (date_trunc('month', now()) + interval '1 month'),
  affiliate_ref               TEXT,
  avatar_seed                 TEXT    DEFAULT 'default',
  terms_accepted_at           TIMESTAMPTZ,
  default_idea_assistant      TEXT,
  metrics_analyses_this_week  INTEGER DEFAULT 0,
  metrics_week_reset_at       TIMESTAMPTZ,
  lang                        TEXT    NOT NULL DEFAULT 'es',
  email_marketing             BOOLEAN NOT NULL DEFAULT TRUE,
  unsubscribe_token           TEXT,
  free_lifetime_uses          INTEGER NOT NULL DEFAULT 0,
  is_admin                    BOOLEAN DEFAULT FALSE
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

-- ════════════════════════════════════════════════════════════
--  ip_usage — límite por IP para usuarios anónimos
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.ip_usage (
  ip         TEXT PRIMARY KEY,
  used_today INTEGER  NOT NULL DEFAULT 0,
  reset_date DATE     NOT NULL DEFAULT CURRENT_DATE
);

-- ════════════════════════════════════════════════════════════
--  transcriptions — caché/registro de transcripciones + métricas Apify
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.transcriptions (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id             UUID REFERENCES auth.users(id) ON DELETE SET NULL,
  ip                  TEXT,
  url                 TEXT        NOT NULL,
  platform            TEXT        NOT NULL,
  language            TEXT,
  text                TEXT        NOT NULL,
  cost_cents          INTEGER     NOT NULL DEFAULT 0,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  thumbnail_b64       TEXT,
  author_username     TEXT,        -- autor del reel (IG ownerUsername / TikTok @user) → "añadir como competidor"
  views               BIGINT,
  likes               BIGINT,
  comments            BIGINT,
  shares              BIGINT,
  published_at        TIMESTAMPTZ,
  metrics_updated_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_transcriptions_user_published
  ON public.transcriptions(user_id, published_at DESC);

-- ════════════════════════════════════════════════════════════
--  payments — pagos / recargas Stripe
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.payments (
  id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id               UUID REFERENCES auth.users(id) ON DELETE CASCADE NOT NULL,
  stripe_session_id     TEXT UNIQUE,
  stripe_payment_intent TEXT,
  amount_cents          INTEGER NOT NULL,
  status                TEXT    NOT NULL DEFAULT 'pending',
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ════════════════════════════════════════════════════════════
--  projects — proyectos/marcas del usuario (con estilo y asistente)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.projects (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         UUID,
  name            TEXT NOT NULL,
  style_prompt    TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  agency_owner_id UUID,
  assistant_id    UUID,
  color           TEXT
);

-- ════════════════════════════════════════════════════════════
--  assistants — asistentes/voces configurables del usuario
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.assistants (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID,
  name         TEXT NOT NULL,
  instructions TEXT NOT NULL,
  is_default   BOOLEAN DEFAULT FALSE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ════════════════════════════════════════════════════════════
--  agency_members — miembros invitados a una cuenta agency
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.agency_members (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  agency_owner_id UUID,
  member_id       UUID,
  invited_email   TEXT,
  status          TEXT DEFAULT 'pending',
  invite_token    TEXT DEFAULT gen_random_uuid()::text,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ════════════════════════════════════════════════════════════
--  ideas — ideas de contenido con desarrollo IA
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.ideas (
  id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id               UUID NOT NULL,
  project_id            UUID,
  raw_text              TEXT NOT NULL,
  assistant_id          TEXT,
  title                 TEXT,
  category              TEXT,
  script_draft          JSONB,
  status                TEXT DEFAULT 'developed',
  created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  recorded_at           TIMESTAMPTZ,
  hook                  TEXT,
  style                 TEXT,
  inspired_by_id        TEXT,
  inspired_by_type      TEXT,   -- 'reel' | 'transcription'
  source                TEXT NOT NULL DEFAULT 'manual',  -- 'manual' | 'suggestion'
  inspired_by_username  TEXT,
  generation_reasoning  TEXT,
  notes                 TEXT    -- notas del usuario en el workspace de «Ideas robadas»
);

-- ════════════════════════════════════════════════════════════
--  scripts — guiones del usuario (transcripción + adaptación + métricas)
--  (sustituye al legacy saved_scripts)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.scripts (
  id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id                  UUID,
  title                    TEXT NOT NULL,
  transcription            TEXT,
  script                   TEXT,
  reel_url                 TEXT,
  project_id               UUID,
  created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  performance_notes        TEXT,
  views_count              INTEGER,
  engagement_rate          NUMERIC,
  likes                    INTEGER DEFAULT 0,
  comments                 INTEGER DEFAULT 0,
  saves                    INTEGER DEFAULT 0,
  metrics_image_url        TEXT,
  published_at             TIMESTAMPTZ,
  idea_id                  UUID,
  transcription_id         BIGINT,
  from_competitor_reel_id  TEXT,
  from_competitor_username TEXT,
  recording_status         TEXT NOT NULL DEFAULT 'pending',
  assistant_name           TEXT,
  hook                     TEXT,
  alt_hooks                JSONB NOT NULL DEFAULT '[]'  -- migración 20260603_scripts_alt_hooks.sql (2026-06-03)
);

-- ════════════════════════════════════════════════════════════
--  voice_profiles — perfil de voz aprendido por marca (moat)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.voice_profiles (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  brand_id     TEXT NOT NULL DEFAULT '',
  tone         TEXT,
  phrases      JSONB NOT NULL DEFAULT '[]'::jsonb,
  structure    TEXT,
  avg_duration INTEGER,
  avoid        TEXT,
  confidence   INTEGER NOT NULL DEFAULT 0,
  source_count INTEGER NOT NULL DEFAULT 0,
  raw          JSONB,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, brand_id)
);

-- ════════════════════════════════════════════════════════════
--  plans — catálogo de planes (gestionado solo por backend / service_role)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.plans (
  key                TEXT PRIMARY KEY,
  name               TEXT NOT NULL,
  price_month_cents  INTEGER,
  price_year_cents   INTEGER,
  monthly_credits    INTEGER,
  stripe_price_month TEXT,
  stripe_price_year  TEXT,
  features           JSONB DEFAULT '{}'::jsonb,
  active             BOOLEAN DEFAULT TRUE,
  sort_order         INTEGER DEFAULT 0,
  updated_at         TIMESTAMPTZ DEFAULT NOW(),
  created_at         TIMESTAMPTZ DEFAULT NOW()
);

-- ════════════════════════════════════════════════════════════
--  topups — catálogo de recargas de créditos (service_role)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.topups (
  key             TEXT PRIMARY KEY,
  credits         INTEGER NOT NULL,
  price_cents     INTEGER NOT NULL,
  stripe_price_id TEXT,
  active          BOOLEAN DEFAULT TRUE,
  sort_order      INTEGER DEFAULT 0,
  updated_at      TIMESTAMPTZ DEFAULT NOW(),
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ════════════════════════════════════════════════════════════
--  app_settings — configuración global key/value (service_role)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.app_settings (
  key        TEXT PRIMARY KEY,
  value      JSONB NOT NULL,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ════════════════════════════════════════════════════════════
--  email_log — cola/registro de emails transaccionales (Resend)
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.email_log (
  id             UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id        UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  template_key   TEXT        NOT NULL,
  status         TEXT        NOT NULL DEFAULT 'queued',  -- queued | sent | failed | skipped
  scheduled_for  TIMESTAMPTZ NOT NULL,
  sent_at        TIMESTAMPTZ,
  resend_id      TEXT,
  opened_at      TIMESTAMPTZ,
  clicked_at     TIMESTAMPTZ,
  error          TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (user_id, template_key)
);

CREATE INDEX IF NOT EXISTS idx_email_log_pending
  ON public.email_log(status, scheduled_for)
  WHERE status = 'queued';

-- ════════════════════════════════════════════════════════════
--  saved_scripts — LEGACY (predecesor de scripts). Se conserva por
--  compatibilidad de datos históricos. El producto actual usa scripts.
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.saved_scripts (
  id          UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id     UUID        REFERENCES auth.users(id) ON DELETE CASCADE,
  style       TEXT,
  content     TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ═══════════════════════════════════════════════════════════════
-- v0.15.0 — Tracked Competitors
-- Aplicado vía supabase/migrations/20260515091500_tracked_competitors.sql
-- (Las RLS policies y el trigger lowercase viven en el archivo de migración.)
-- ═══════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS public.creators_global (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ig_username text UNIQUE NOT NULL,
  profile_data jsonb NOT NULL DEFAULT '{}'::jsonb,
  followers_count_cached integer,
  last_scraped_at timestamptz,
  next_scrape_due_at timestamptz,
  scrape_status text NOT NULL DEFAULT 'pending'
    CHECK (scrape_status IN ('pending','scraping','ok','failed','not_found','private')),
  last_error text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.creator_reels_global (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  creator_id uuid NOT NULL REFERENCES public.creators_global(id) ON DELETE CASCADE,
  ig_reel_id text NOT NULL,
  caption text,
  views bigint DEFAULT 0,
  likes bigint DEFAULT 0,
  comments bigint DEFAULT 0,
  posted_at timestamptz,
  thumb_url text,
  thumb_b64 text,
  video_url text,
  video_duration_sec numeric(6,2),
  fetched_at timestamptz NOT NULL DEFAULT now(),
  is_archived boolean NOT NULL DEFAULT false,
  UNIQUE(creator_id, ig_reel_id)
);

CREATE TABLE IF NOT EXISTS public.user_tracked_creators (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  creator_id uuid NOT NULL REFERENCES public.creators_global(id) ON DELETE CASCADE,
  project_id uuid REFERENCES public.projects(id) ON DELETE CASCADE,
  added_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz,
  weekly_digest_enabled boolean NOT NULL DEFAULT true
);

-- v0.15.8: locks de generación de guion competidor (PK compuesta, sweeper
-- de huérfanos vía Celery beat). Cierra 0.1% residual del guard 60s v0.15.5.
CREATE TABLE IF NOT EXISTS public.script_generation_locks (
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  reel_id uuid NOT NULL REFERENCES public.creator_reels_global(id) ON DELETE CASCADE,
  task_id text,
  started_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, reel_id)
);

-- v0.15.6: favoritos de reels de competencia (hard-delete, UNIQUE user+reel).
CREATE TABLE IF NOT EXISTS public.user_favorite_reels (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  reel_id uuid NOT NULL REFERENCES public.creator_reels_global(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, reel_id)
);

CREATE TABLE IF NOT EXISTS public.user_creator_credits (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  extra_slots integer NOT NULL DEFAULT 0,
  purchased_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  stripe_payment_intent_id text,
  consumed boolean NOT NULL DEFAULT false
);

-- ════════════════════════════════════════════════════════════
--  ROW LEVEL SECURITY
--
--  El backend usa la service_role key, que bypassa RLS. Por eso la
--  mayoría de tablas tienen RLS activado SIN policy = solo service_role
--  puede leer/escribir (cliente anon/authenticated queda bloqueado).
--  Excepciones con policy explícita: saved_scripts y voice_profiles.
-- ════════════════════════════════════════════════════════════
ALTER TABLE public.profiles       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.transcriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payments       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.ip_usage       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.saved_scripts  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.email_log      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.voice_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.plans          ENABLE ROW LEVEL SECURITY;  -- service_role only (sin policy)
ALTER TABLE public.topups         ENABLE ROW LEVEL SECURITY;  -- service_role only (sin policy)
ALTER TABLE public.app_settings   ENABLE ROW LEVEL SECURITY;  -- service_role only (sin policy)

-- saved_scripts (legacy): el usuario ve solo los suyos
CREATE POLICY "Users see own scripts" ON public.saved_scripts
  FOR ALL USING (auth.uid() = user_id);

-- voice_profiles: el usuario ve/gestiona solo su propia voz
CREATE POLICY "own voice" ON public.voice_profiles
  FOR ALL USING (auth.uid() = user_id);

-- ════════════════════════════════════════════════════════════
--  feedback — reportes de bug/idea (menú de cuenta). Insert vía POST /api/feedback;
--  revisión + recompensa en créditos vía /admin/api/feedback. service_role only.
-- ════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS public.feedback (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id         uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  type            text NOT NULL DEFAULT 'bug',
  text            text NOT NULL,
  page            text,
  plan            text,
  image_b64       text,
  status          text NOT NULL DEFAULT 'new',
  credits_awarded integer NOT NULL DEFAULT 0,
  admin_note      text,
  resolved_by     uuid REFERENCES auth.users(id),
  resolved_at     timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS feedback_status_idx ON public.feedback (status, created_at DESC);
CREATE INDEX IF NOT EXISTS feedback_user_idx   ON public.feedback (user_id, created_at DESC);
ALTER TABLE public.feedback ENABLE ROW LEVEL SECURITY;  -- service_role only (sin policy)
