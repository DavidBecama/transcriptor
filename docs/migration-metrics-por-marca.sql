-- ════════════════════════════════════════════════════════════════════════════
-- FIX1 · Métricas por MARCA (2026-06-20) — correr en Supabase ANTES de desplegar
-- ════════════════════════════════════════════════════════════════════════════
-- Cada marca (project) tiene su propia cuenta de Instagram conectada. El modelo:
--   ig_profiles.project_id  → 1 perfil de IG por (usuario, marca)
--   ig_videos               → cuelga de ig_profile_id, así que queda AUTO-scopeado
-- project_id NULL = cuenta por defecto (creadores de 1 marca y datos previos a esta
-- migración) → los usuarios existentes siguen funcionando sin tocar nada.
--
-- Tras correr esto: una AGENCIA conecta un IG por cliente (cada marca su cuenta) y la
-- pantalla de Métricas (y el "connected") muestran SOLO la marca activa. Los datos ya
-- existentes quedan como project_id NULL (la "cuenta por defecto" de cada usuario).

ALTER TABLE ig_profiles
  ADD COLUMN IF NOT EXISTS project_id UUID REFERENCES projects(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_ig_profiles_user_project
  ON ig_profiles (user_id, project_id);

-- La app ya NO asume "1 perfil de IG por usuario" (permite 1 por marca; la unicidad la
-- valida la app antes de insertar). Si tenías un UNIQUE sobre ig_profiles(user_id),
-- elimínalo para no bloquear el 2º perfil de una agencia:
--   ALTER TABLE ig_profiles DROP CONSTRAINT IF EXISTS ig_profiles_user_id_key;
