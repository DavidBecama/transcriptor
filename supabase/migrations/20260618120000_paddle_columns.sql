-- ════════════════════════════════════════════════════════════════════════════
-- Paddle — columnas de suscripción en profiles (paralelas a stripe_subscription_id).
-- Stripe queda dormido; estas columnas guardan la suscripción/cliente de Paddle
-- para el portal de gestión y la cancelación por webhook.
-- Idempotente. profiles ya tiene RLS deny-all a clientes (solo backend service_role),
-- así que estas columnas NO quedan expuestas. Aplicar manualmente en Supabase.
-- ════════════════════════════════════════════════════════════════════════════

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS paddle_subscription_id TEXT,
  ADD COLUMN IF NOT EXISTS paddle_customer_id     TEXT;

-- Búsqueda por subscription_id en el webhook de cancelación.
CREATE INDEX IF NOT EXISTS idx_profiles_paddle_sub
  ON public.profiles (paddle_subscription_id);
