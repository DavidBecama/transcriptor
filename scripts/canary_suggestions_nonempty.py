#!/usr/bin/env python3
"""Canary «SUGERENCIAS 100%» (David 08/07): para cada marca del usuario cuyo NICHO tiene pool,
/api/radar/suggestions NUNCA debe devolver vacío. Solo puede salir vacío si el nicho no tiene
reels scrapeados (populating/needs_niche). Corre logueado contra Supabase (no harness demo).

Uso: venv/bin/python scripts/canary_suggestions_nonempty.py <user_id>
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

def run(uid):
    c = app.app.test_client()
    with c.session_transaction() as s:
        s["user"] = {"id": uid}
    projs = (app.db.table("projects").select("id,name").eq("user_id", uid).execute()).data or []
    fails = []
    print("%-22s %-4s %-10s %-9s %s" % ("marca", "n", "pool_status", "recycled", "pool_nicho"))
    for p in [{"id": None, "name": "(default)"}] + projs:
        pid = p["id"]
        niche, subs = app._resolve_brand_niche(uid, pid)
        raw = app._niche_suggestion_reels(subs, niche, app._sugg_exclude(uid, pid),
                                          app.SUGG_MAX_TOTAL, day_seed=app._sugg_day_seed(uid, pid),
                                          seen_ids=set(), exclude_reel_ids=set()) if (niche or subs) else []
        q = "" if not pid else "?project_id=" + pid
        d = (c.get("/api/radar/suggestions" + q).get_json() or {})
        n = len(d.get("suggestions") or [])
        pool_n = len(raw)
        print("%-22s %-4d %-10s %-9s %d" % (p["name"][:22], n, d.get("pool_status"), str(d.get("recycled")), pool_n))
        if pool_n > 0 and n == 0:   # hay pool en el nicho pero 0 sugerencias = FALLO
            fails.append(p["name"])
    print("\n" + ("✓ CANARY OK — ninguna marca con pool queda sin sugerencias"
                  if not fails else "✗ CANARY FALLA — marcas con pool y 0 sugerencias: " + ", ".join(fails)))
    return 0 if not fails else 1

if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else "7c655ab0-f9cf-485b-97ad-4ecf2e1a8f6b"))
