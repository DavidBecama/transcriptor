-- community_waitlist — lista de espera de la Comunidad (Pushear reels→regalo + Grupos
-- opt-in, Fathom 18/06). El "Avísame cuando llegue" del teaser marca aquí el interés
-- del usuario → cuando lancemos, se les puede emailear. NULL = no apuntado.
-- El backend escribe con service_role; profiles ya tiene RLS activado.

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS community_waitlist_at TIMESTAMPTZ;
