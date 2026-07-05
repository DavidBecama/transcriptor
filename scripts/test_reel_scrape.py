#!/usr/bin/env python3
"""Diagnóstico de cobertura del scrape de reels (NO toca producción).
Corre Apify async (sin TimeoutException de HTTP) y reporta reels/handle + coste real.

Uso:
  python scripts/test_reel_scrape.py step1   # actor reel-scraper + proxy residencial
  python scripts/test_reel_scrape.py step2   # apify/instagram-scraper + directUrls /reels/
"""
import os, sys, time, json, urllib.request, urllib.error

HANDLES = ["sergiopeinado", "patry_jordan", "marcvivobcn", "ismaelgalancho", "powerexplosive"]

def _load_token():
    # lee APIFY_TOKEN de .env sin depender de python-dotenv
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for line in open(os.path.join(here, ".env"), encoding="utf-8"):
        line = line.strip()
        if line.startswith("APIFY_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("APIFY_TOKEN no encontrado en .env")

TOKEN = _load_token()
RESIDENTIAL = {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]}

def _post(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())

def _get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read())

def run_actor(actor_id, run_input, memory=1024, run_timeout=600):
    """Inicia run async, espera a estado terminal, devuelve (status, items, cost_usd, secs)."""
    start_url = (f"https://api.apify.com/v2/acts/{actor_id}/runs"
                 f"?token={TOKEN}&memory={memory}&timeout={run_timeout}")
    run = _post(start_url, run_input)["data"]
    run_id, ds = run["id"], run["defaultDatasetId"]
    t0 = time.time()
    while True:
        info = _get(f"https://api.apify.com/v2/actor-runs/{run_id}?token={TOKEN}")["data"]
        st = info["status"]
        if st in ("SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"):
            break
        if time.time() - t0 > run_timeout + 120:
            st = "POLL-TIMEOUT"; break
        time.sleep(5)
    cost = info.get("usageTotalUsd") or 0
    secs = round(time.time() - t0, 1)
    items = _get(f"https://api.apify.com/v2/datasets/{ds}/items?token={TOKEN}&clean=true")
    return st, items, cost, secs

def _count_reels(items):
    n = 0
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if it.get("error"):
            continue
        if it.get("shortCode") or it.get("id") or it.get("url"):
            n += 1
    return n

def step0(handles=None):
    print("=== STEP 0: actor xMc5Ga1oCONPmWJIa (reel-scraper) SIN proxy = config PROD ===")
    for h in (handles or HANDLES):
        run_input = {"username": [h], "resultsLimit": 10, "includeSharesCount": True}
        st, items, cost, secs = run_actor("xMc5Ga1oCONPmWJIa", run_input)
        n = _count_reels(items)
        err = ""
        if items and isinstance(items[0], dict) and items[0].get("error"):
            err = " err=%s/%s" % (items[0].get("error"), (items[0].get("errorDescription") or "")[:40])
        print(f"  {h:18s} reels={n:2d}  status={st:10s} ${cost:.4f}  {secs}s{err}")
        TOTALS.append((h, n, cost))

def details(handles=None):
    print("=== DETAILS probe: apify/instagram-scraper resultsType=details (¿existe/privado?) ===")
    for h in (handles or ["sergiopeinado", "patry_jordan", "marcvivobcn", "ismaelgalancho"]):
        run_input = {"directUrls": [f"https://www.instagram.com/{h}/"],
                     "resultsType": "details", "resultsLimit": 1, "proxy": RESIDENTIAL}
        st, items, cost, secs = run_actor("apify~instagram-scraper", run_input)
        it = items[0] if items and isinstance(items[0], dict) else {}
        info = {k: it.get(k) for k in ("username", "private", "isBusinessAccount",
                "followersCount", "postsCount", "error", "errorDescription")}
        print(f"  {h:18s} ${cost:.4f} {secs}s :: {json.dumps(info, ensure_ascii=False, default=str)}")
        TOTALS.append((h, 0, cost))

def step1(handles=None):
    print("=== STEP 1: actor xMc5Ga1oCONPmWJIa (reel-scraper) + proxy RESIDENCIAL ===")
    for h in (handles or HANDLES):
        run_input = {"username": [h], "resultsLimit": 10,
                     "includeSharesCount": True, "proxy": RESIDENTIAL}
        st, items, cost, secs = run_actor("xMc5Ga1oCONPmWJIa", run_input)
        n = _count_reels(items)
        err = ""
        if items and isinstance(items[0], dict) and items[0].get("error"):
            err = " err=%s/%s" % (items[0].get("error"), (items[0].get("errorDescription") or "")[:40])
        print(f"  {h:18s} reels={n:2d}  status={st:10s} ${cost:.4f}  {secs}s{err}")
        TOTALS.append((h, n, cost))

def step2(handles=None):
    print("=== STEP 2: actor apify/instagram-scraper + directUrls /reels/ + proxy RESIDENCIAL ===")
    for h in (handles or HANDLES):
        run_input = {
            "directUrls": [f"https://www.instagram.com/{h}/reels/"],
            "resultsType": "posts", "resultsLimit": 8,
            "proxy": RESIDENTIAL,
        }
        st, items, cost, secs = run_actor("apify~instagram-scraper", run_input)
        n = _count_reels(items)
        err = ""
        if items and isinstance(items[0], dict) and items[0].get("error"):
            err = " err=%s/%s" % (items[0].get("error"), (items[0].get("errorDescription") or "")[:40])
        # muestra de campos del 1er item para mapear al formato común
        sample = {}
        if n and isinstance(items[0], dict):
            it = items[0]
            sample = {k: it.get(k) for k in
                      ("shortCode", "url", "videoPlayCount", "videoViewCount",
                       "likesCount", "commentsCount", "timestamp", "type", "productType")}
        print(f"  {h:18s} reels={n:2d}  status={st:10s} ${cost:.4f}  {secs}s{err}")
        if sample:
            print("      sample:", json.dumps(sample, ensure_ascii=False, default=str))
        TOTALS.append((h, n, cost))

TOTALS = []
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "step1"
    handles = sys.argv[2].split(",") if len(sys.argv) > 2 else None
    fn = {"step0": step0, "step1": step1, "step2": step2, "details": details}[mode]
    fn(handles) if handles else fn()
    ok = sum(1 for _, n, _ in TOTALS if n >= 5)
    total_cost = sum(c for _, _, c in TOTALS)
    print(f"\n  >=5 reels en {ok}/{len(TOTALS)} handles · coste total del test: ${total_cost:.4f}")
