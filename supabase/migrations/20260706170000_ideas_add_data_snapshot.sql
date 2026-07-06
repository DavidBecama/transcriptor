-- «Guardar idea con datos» (feature David): snapshot de transcripción + métricas del reel EN la
-- idea, para que sobrevivan aunque el reel envejezca y salga del pool. Aditivo y nullable → seguro.
-- Aplicado en prod vía MCP el 2026-07-06 (kchhyvaypkhebjkqxhsr).
ALTER TABLE public.ideas ADD COLUMN IF NOT EXISTS transcript_snapshot TEXT;
ALTER TABLE public.ideas ADD COLUMN IF NOT EXISTS metrics_snapshot JSONB;
