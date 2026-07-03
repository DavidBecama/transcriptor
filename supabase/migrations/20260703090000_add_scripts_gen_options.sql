-- P1 «elección siempre»: el reveal del robo (2 opciones + hooks + POV + chosen)
-- se persiste para poder reconstruirse tras recargar u otro día.
-- {"options":[{title,hooks,body,closing,script}...], "pov_text":..., "chosen":int|null}
-- chosen NULL = elección pendiente (robo terminado en background).
-- YA APLICADA en prod vía MCP (2026-07-03, «add_scripts_gen_options»). Idempotente.
alter table public.scripts add column if not exists gen_options jsonb;
