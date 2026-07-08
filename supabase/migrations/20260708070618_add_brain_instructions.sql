-- Instrucciones libres del creador por marca (Cerebro). Aditiva, nullable.
-- Aplicada a prod vía MCP apply_migration (add_brain_instructions_to_voice_profiles).
ALTER TABLE voice_profiles
  ADD COLUMN IF NOT EXISTS brain_instructions text,
  ADD COLUMN IF NOT EXISTS brain_instructions_updated_at timestamptz;
