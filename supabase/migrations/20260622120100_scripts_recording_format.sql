-- Ítem 10 (batch radar v0.25) — FORMATO DE GRABACIÓN sugerido del guion robado.
-- El LLM clasifica el formato del reel competidor (transcript/caption/duración) en un
-- set fijo: selfie · pizarra · podcast · escritorio · broll-vo. Se guarda con el guion.
-- Additive + idempotente: el insert/parse degrada a NULL si la columna no existe aún.
ALTER TABLE scripts
  ADD COLUMN IF NOT EXISTS recording_format TEXT;
