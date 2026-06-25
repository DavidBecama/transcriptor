#!/usr/bin/env python
"""Siembra de competidores por nicho (cold-start del radar).

Lee un CSV curado de creadores (nombre,handle,...,nicho,subnicho,idioma,pais,...)
y los UPSERTEA en `creators_global` con niche_source='seed', mapeando cada nicho
del CSV a uno de los 12 nichos amplios del onboarding (y guardando el nicho
granular + el subnicho como tags en `subniches`). Así un usuario nuevo recibe
competidores reales de su nicho aunque todavía no haya grafo de co-ocurrencia.

Hygiene de PROD (idempotente, no pisa datos reales):
  · subniches  → UNIÓN con lo existente (nunca reemplaza).
  · niche      → solo se setea si la fila no tenía uno (respeta el del usuario).
  · niche_source → 'seed' solo si no era ya 'user' (no degrada a un creador real).
  · NO scrapea (cero Apify): el scrape ocurre cuando un usuario lo trackea.

Uso:
  python scripts/seed_creators.py "ruta/al.csv"            # DRY-RUN (no escribe)
  python scripts/seed_creators.py "ruta/al.csv" --commit   # escribe en Supabase
"""
import csv
import os
import re
import sys

HANDLE_RE = re.compile(r"^[a-z0-9._]{1,30}$")

# Nicho granular del CSV (normalizado) → nicho amplio del onboarding (normalizado).
# Los que no aparecen aquí conservan su propio nicho normalizado como `niche`
# (un usuario los alcanza escribiéndolo a mano o por overlap de subniches).
NICHE_MAP = {
    "fitness": "fitness",
    "inteligencia artificial": "tecnologia",
    "tecnologia": "tecnologia",
    "diseno y ux": "tecnologia",
    "nutricion": "salud",
    "salud mental y psicologia": "salud",
    "finanzas personales": "finanzas",
    "cripto y web3": "finanzas",
    "emprendimiento y negocios": "negocios",
    "marketing digital": "marketing",
    "viajes": "viajes",
    "cocina y recetas": "cocina",
    "moda y estilo": "moda",
    "belleza y skincare": "belleza",
    "inmobiliario": "inmobiliaria",
    "educacion e idiomas": "educacion",
    "ciencia y divulgacion": "educacion",
}


def load_env(root):
    env = {}
    path = os.path.join(root, ".env")
    for line in open(path, encoding="utf-8"):
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line.strip())
        if m:
            env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


def fix_mojibake(s):
    """El CSV viene UTF-8 mal decodificado como latin-1 (Ã±→ñ). Lo revierte."""
    if not s:
        return s
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s


def norm_tag(s):
    """Mismo esquema que app._norm_tag: minúsculas, sin acentos, alfanumérico."""
    s = (s or "").strip().lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9 _-]", "", s)[:40].strip()


def parse_csv(path):
    """Devuelve {handle: {'niche': broad, 'subniches': set}} deduplicado."""
    out = {}
    skipped = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            handle = (r.get("handle") or "").strip().lstrip("@").lower()
            if not HANDLE_RE.match(handle):
                if handle:
                    skipped.append(handle)
                continue
            niche_norm = norm_tag(fix_mojibake(r.get("nicho") or ""))
            sub_norm = norm_tag(fix_mojibake(r.get("subnicho") or ""))
            broad = NICHE_MAP.get(niche_norm, niche_norm)
            tags = set()
            if sub_norm:
                tags.add(sub_norm)
            if niche_norm:
                tags.add(niche_norm)  # nicho granular como tag → matcheable a mano
            if handle in out:
                out[handle]["subniches"] |= tags
                # conserva el primer nicho amplio visto (estable entre corridas)
            else:
                out[handle] = {"niche": broad, "subniches": tags}
    return out, skipped


def main():
    if len(sys.argv) < 2:
        print("uso: python scripts/seed_creators.py <csv> [--commit]")
        sys.exit(2)
    csv_path = sys.argv[1]
    commit = "--commit" in sys.argv[2:]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    desired, skipped = parse_csv(csv_path)
    print(f"CSV: {len(desired)} handles únicos válidos  ·  {len(skipped)} descartados")
    by_broad = {}
    for d in desired.values():
        by_broad[d["niche"]] = by_broad.get(d["niche"], 0) + 1
    print("Por nicho amplio:")
    for k, v in sorted(by_broad.items(), key=lambda kv: -kv[1]):
        print(f"   {v:4d}  {k}")

    env = load_env(root)
    from supabase import create_client
    db = create_client(env["SUPABASE_URL"], env["SUPABASE_SERVICE_KEY"])

    # Lee lo existente (merge, no pisa).
    handles = list(desired.keys())
    existing = {}
    for i in range(0, len(handles), 100):
        chunk = handles[i:i + 100]
        try:
            rows = (db.table("creators_global")
                    .select("id,ig_username,niche,subniches,niche_source")
                    .in_("ig_username", chunk).execute()).data or []
            for x in rows:
                existing[(x.get("ig_username") or "").lower()] = x
        except Exception as e:
            print("  ! lectura existentes falló:", str(e)[:120])

    inserts, updates = [], []
    for h, d in desired.items():
        ex = existing.get(h)
        if ex:
            merged = sorted(set(ex.get("subniches") or []) | d["subniches"])
            niche = ex.get("niche") or d["niche"]
            src = ex.get("niche_source")
            src = src if src == "user" else "seed"
            # solo escribe si algo cambia
            if (merged != sorted(ex.get("subniches") or [])
                    or niche != ex.get("niche") or src != ex.get("niche_source")):
                updates.append({"ig_username": h, "niche": niche,
                                "subniches": merged, "niche_source": src})
        else:
            inserts.append({"ig_username": h, "niche": d["niche"],
                            "subniches": sorted(d["subniches"]), "niche_source": "seed"})

    print(f"\nPlan: {len(inserts)} nuevos  ·  {len(updates)} enriquecidos  ·  "
          f"{len(desired) - len(inserts) - len(updates)} sin cambios")

    if not commit:
        print("\nDRY-RUN (sin escribir). Añade --commit para aplicar.")
        if inserts[:3]:
            print("Ejemplo nuevos:", [(x["ig_username"], x["niche"], x["subniches"]) for x in inserts[:3]])
        return

    n = 0
    payload = inserts + updates
    for i in range(0, len(payload), 200):
        batch = payload[i:i + 200]
        db.table("creators_global").upsert(batch, on_conflict="ig_username").execute()
        n += len(batch)
        print(f"  upserted {n}/{len(payload)}")
    print(f"\n✅ Semilla aplicada: {len(inserts)} nuevos + {len(updates)} enriquecidos.")


if __name__ == "__main__":
    main()
