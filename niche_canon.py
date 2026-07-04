"""Nicho de TEXTO LIBRE → nicho canónico del pool (creators_global.niche).

Módulo compartido entre app.py (sugerencias: fallback por nicho amplio) y tasks.py
(refresh_suggestion_pools: qué nichos están ACTIVOS). Sin dependencias más allá de
`re` para que el worker Celery no cargue nada de Flask.
"""
import re

SEED_NICHE_ALIAS = {
    "tech": "tecnologia", "finance": "finanzas", "cooking": "cocina",
    "fashion": "moda", "beauty": "belleza", "travel": "viajes",
    "education": "educacion", "business": "negocios", "health": "salud",
    "real estate": "inmobiliaria",
}

# El onboarding y el modal dejan nichos libres ("inteligencia artificial", "marketing
# digital", "meditacion"…) que NO casan exacto con la taxonomía del pool → 0 sugerencias.
# Este alias los lleva a su nicho canónico para el FALLBACK por nicho amplio.
POOL_NICHE_ALIAS = {
    "ia": "tecnologia", "ai": "tecnologia", "inteligencia artificial": "tecnologia",
    "automatizacion": "tecnologia", "no-code": "tecnologia", "nocode": "tecnologia",
    "programacion": "tecnologia", "software": "tecnologia", "saas": "negocios",
    "marketing digital": "marketing", "growth": "marketing", "ads": "marketing",
    "publicidad": "marketing", "copywriting": "marketing", "redes sociales": "marketing",
    "creacion de contenido": "marketing", "creador de contenido": "marketing",
    "emprendimiento": "negocios", "emprender": "negocios", "ecommerce": "negocios", "ventas": "negocios",
    "meditacion": "espiritualidad y mindfulness", "mindfulness": "espiritualidad y mindfulness",
    "espiritualidad": "espiritualidad y mindfulness",
    "nutricion": "salud", "psicologia": "salud", "bienestar": "salud",
}


def norm_tag(s: str) -> str:
    s = (s or "").strip().lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z0-9 _-]", "", s)[:40].strip()


def pool_niche_canon(niche):
    """Nicho del usuario (texto libre) → nicho canónico de creators_global, para el
    fallback por nicho amplio. Devuelve "" si no hay nicho."""
    n = norm_tag(niche or "")
    if not n:
        return ""
    if n in POOL_NICHE_ALIAS:
        return POOL_NICHE_ALIAS[n]
    return SEED_NICHE_ALIAS.get(n, n)
