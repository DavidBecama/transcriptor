-- ═══════════════════════════════════════════════════════════════
-- BACKFILL puntual de nicho+subniches de las marcas antiguas de David (creadas sin nicho;
-- el matching de «Sugerencias de hoy» casa por SUBNICHES → hay que rellenarlos).
-- One-off por UUID (cuenta davidmiragito@gmail.com). Idempotente. «IA» ya estaba bien.
-- NOTA: el pool creators_global hoy solo tiene reels frescos de tech/IA → marketing y
-- espiritualidad mostrarán el empty-state hasta que se scrapee el pool de esos nichos.
-- ═══════════════════════════════════════════════════════════════

UPDATE public.projects SET niche='marketing', subniches=ARRAY['marketing','software']
  WHERE id='26a4d677-20ca-48d0-8fa2-e4c75764938a';   -- Reelscript / «Reels Creed»

UPDATE public.projects SET niche='espiritualidad', subniches=ARRAY['espiritualidad']
  WHERE id='f3db7951-2e5b-4a9d-9c59-b3523d001a00';   -- ESPIRITUALIDAD
