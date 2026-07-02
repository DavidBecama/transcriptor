-- «Ideas robadas»: notas del usuario en el workspace de cada idea robada.
-- El ancla es la fila de `ideas` con inspired_by_id == reel/transcripción de origen.
-- YA APLICADA en prod vía MCP (2026-07-02, migración «add_ideas_notes») — este
-- archivo es documentación-sync para entornos nuevos, idempotente.
alter table public.ideas add column if not exists notes text;
