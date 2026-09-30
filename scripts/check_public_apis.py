#!/usr/bin/env python3
"""Probe every keyless public-API Integration preset (src/public_api_presets.py) with one real request.

Run by .github/workflows/public-apis.yml weekly, and whenever the presets change, so a moved or retired API is
noticed before the agent trips over it. Presets that need your own server or credentials are skipped.
Exit code 1 if any probe fails.
"""
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.public_api_presets import PUBLIC_API_PRESETS  # noqa: E402

USER_AGENT = "Aurix/1.0 (self-hosted personal assistant; preset health check)"

# One cheap request per preset, taken from the endpoints its description documents.
PROBES = {
    "open_meteo": "/v1/forecast?latitude=39.74&longitude=-104.99&current=temperature_2m",
    "open_meteo_geocoding": "/v1/search?name=Denver&count=1",
    "nager_date": "/api/v3/NextPublicHolidays/US",
    "frankfurter": "/latest?from=USD&to=EUR",
    "coingecko": "/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
    "usgs_earthquakes": "/earthquakes/feed/v1.0/summary/significant_week.geojson",
    "sunrise_sunset": "/json?lat=39.74&lng=-104.99&formatted=0",
    "openalex": "/works?search=retrieval&per-page=1",
    "arxiv": "/api/query?search_query=all:retrieval&max_results=1",
    "semantic_scholar": "/graph/v1/paper/search?query=retrieval&limit=1",
    "wikipedia": "/api/rest_v1/page/summary/Denver",
    "free_dictionary": "/api/v2/entries/en/hello",
    "open_library": "/search.json?q=dune&limit=1",
    "hacker_news": "/v0/topstories.json",
}
RATE_LIMITED_OK = {"semantic_scholar", "coingecko", "openalex"}   # shared public quotas: a 429 means reachable, just busy


def probe(key: str) -> tuple:
    url = PUBLIC_API_PRESETS[key]["base_url"] + PROBES[key]
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read(2048)
                return True, str(r.status)
        except urllib.error.HTTPError as e:
            if e.code == 429 and key in RATE_LIMITED_OK:
                return True, "429 (rate-limited, reachable)"
            if e.code >= 500 and attempt < 2:
                time.sleep(3)
                continue
            return False, str(e.code)
        except Exception as e:                          # DNS, TLS, timeout
            if attempt < 2:
                time.sleep(3)
                continue
            return False, type(e).__name__
    return False, "?"


def main() -> int:
    keyless = [k for k, p in PUBLIC_API_PRESETS.items() if p.get("base_url") and p.get("auth_type") == "none"]
    missing = [k for k in keyless if k not in PROBES]
    failures = 0
    for key in keyless:
        if key in missing:
            print(f"FAIL {key:22} no probe defined in scripts/check_public_apis.py")
            failures += 1
            continue
        ok, detail = probe(key)
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {key:22} {detail}")
        time.sleep(1)                                   # be polite to free services
    print(f"\n{len(keyless) - failures}/{len(keyless)} keyless presets answered.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
