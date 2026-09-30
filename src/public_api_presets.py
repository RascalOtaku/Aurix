"""src/public_api_presets.py - keyless public APIs as one-click Integration presets.

Picked from the owner's starred lists: public-apis/public-apis (Auth = No, HTTPS = Yes) and the free research
skills in VoltAgent/awesome-openclaw-skills (OpenAlex, arXiv digests), plus AdGuard Home for the homelab.
Each preset carries a working `base_url`, so adding one in Settings -> Integrations is a single click, and the
agent reaches it through the existing `api_call` tool (src/integrations.py) - no new code path, same limits.

Descriptions double as the agent's documentation: keep them to real endpoints with the parameters that matter.
"""
from __future__ import annotations

from typing import Any, Dict

PUBLIC_API_PRESETS: Dict[str, Dict[str, Any]] = {
    "open_meteo": {
        "name": "Open-Meteo (weather)",
        "base_url": "https://api.open-meteo.com",
        "auth_type": "none",
        "description": (
            "Open-Meteo weather forecast, no key (free for non-commercial use). Needs coordinates: get them from the "
            "Open-Meteo Geocoding integration first.\n"
            "  GET /v1/forecast?latitude=39.74&longitude=-104.99&current=temperature_2m,apparent_temperature,"
            "precipitation,weather_code,wind_speed_10m&daily=temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max&timezone=auto&temperature_unit=fahrenheit&wind_speed_unit=mph\n"
            "  hourly=... takes the same variable names; forecast_days=1..16; past_days=0..92.\n"
            "  weather_code is WMO: 0 clear, 1-3 cloudy, 45/48 fog, 51-67 drizzle/rain, 71-77 snow, 80-82 showers, 95-99 storms."
        ),
    },
    "open_meteo_geocoding": {
        "name": "Open-Meteo Geocoding",
        "base_url": "https://geocoding-api.open-meteo.com",
        "auth_type": "none",
        "description": (
            "Place name -> coordinates, no key.\n"
            "  GET /v1/search?name=Denver&count=3&language=en&format=json — results[].latitude/longitude/timezone/country"
        ),
    },
    "nager_date": {
        "name": "Nager.Date (public holidays)",
        "base_url": "https://date.nager.at",
        "auth_type": "none",
        "description": (
            "Public holidays for 100+ countries, no key.\n"
            "  GET /api/v3/PublicHolidays/{year}/{countryCode} — e.g. /api/v3/PublicHolidays/2026/US\n"
            "  GET /api/v3/NextPublicHolidays/{countryCode} — the coming year's holidays\n"
            "  GET /api/v3/IsTodayPublicHoliday/{countryCode} — 200 = yes, 204 = no"
        ),
    },
    "frankfurter": {
        "name": "Frankfurter (exchange rates)",
        "base_url": "https://api.frankfurter.app",
        "auth_type": "none",
        "description": (
            "ECB reference exchange rates, no key.\n"
            "  GET /latest?from=USD&to=EUR,GBP,JPY — today's rates\n"
            "  GET /latest?amount=250&from=USD&to=EUR — convert an amount\n"
            "  GET /2026-01-02?from=USD — a past day; GET /2026-01-01..2026-03-31?from=USD&to=EUR — a time series\n"
            "  GET /currencies — supported currency codes"
        ),
    },
    "coingecko": {
        "name": "CoinGecko (crypto prices)",
        "base_url": "https://api.coingecko.com",
        "auth_type": "none",
        "description": (
            "Crypto prices, no key (public rate limit, ~10-30 calls/min).\n"
            "  GET /api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true\n"
            "  GET /api/v3/search?query=solana — find the id for a coin name\n"
            "  GET /api/v3/coins/{id}/market_chart?vs_currency=usd&days=30 — price history"
        ),
    },
    "usgs_earthquakes": {
        "name": "USGS Earthquakes",
        "base_url": "https://earthquake.usgs.gov",
        "auth_type": "none",
        "description": (
            "Real-time earthquake data, no key.\n"
            "  GET /earthquakes/feed/v1.0/summary/significant_week.geojson (also: all_day, 4.5_week, all_hour)\n"
            "  GET /fdsnws/event/1/query?format=geojson&starttime=2026-09-01&minmagnitude=3&latitude=39.7"
            "&longitude=-105&maxradiuskm=300&orderby=time&limit=20"
        ),
    },
    "sunrise_sunset": {
        "name": "Sunrise-Sunset",
        "base_url": "https://api.sunrise-sunset.org",
        "auth_type": "none",
        "description": (
            "Sunrise, sunset, twilight and day length, no key. Times are UTC ISO-8601 with formatted=0.\n"
            "  GET /json?lat=39.74&lng=-104.99&date=today&formatted=0 (date=YYYY-MM-DD also works; add tzid=America/Denver for local)"
        ),
    },
    "openalex": {
        "name": "OpenAlex (scholarly works)",
        "base_url": "https://api.openalex.org",
        "auth_type": "none",
        "description": (
            "250M+ papers, authors and institutions, no key. Add mailto=you@example.com to join the faster polite pool.\n"
            "  GET /works?search=vectorless retrieval&per-page=5&sort=cited_by_count:desc&select=id,title,"
            "publication_year,cited_by_count,doi,open_access\n"
            "  GET /works?filter=publication_year:2026,concepts.id:C154945302&per-page=10 — filtered listing\n"
            "  GET /authors?search=Geoffrey Hinton — authors; GET /works/{W-id} — one work"
        ),
    },
    "arxiv": {
        "name": "arXiv",
        "base_url": "https://export.arxiv.org",
        "auth_type": "none",
        "description": (
            "arXiv preprints, no key. Returns Atom XML (read <entry><title>, <summary>, <id>). Wait ~3 s between calls.\n"
            "  GET /api/query?search_query=all:%22retrieval augmented generation%22&sortBy=submittedDate"
            "&sortOrder=descending&max_results=10\n"
            "  search_query fields: ti: (title) au: (author) abs: (abstract) cat: (e.g. cat:cs.CL); combine with AND/OR\n"
            "  GET /api/query?id_list=2401.12345 — a specific paper"
        ),
    },
    "semantic_scholar": {
        "name": "Semantic Scholar",
        "base_url": "https://api.semanticscholar.org",
        "auth_type": "none",
        "description": (
            "Paper search and citation graph, no key (shared rate limit; retry after a 429).\n"
            "  GET /graph/v1/paper/search?query=mixture of experts&limit=5&fields=title,year,citationCount,url,tldr\n"
            "  GET /graph/v1/paper/{paperId|DOI:...|arXiv:...}?fields=title,abstract,references.title,citations.title"
        ),
    },
    "wikipedia": {
        "name": "Wikipedia",
        "base_url": "https://en.wikipedia.org",
        "auth_type": "none",
        "description": (
            "Wikipedia, no key.\n"
            "  GET /api/rest_v1/page/summary/{Title_With_Underscores} — lead summary\n"
            "  GET /w/api.php?action=query&list=search&srsearch=...&format=json&srlimit=5 — search\n"
            "  GET /w/api.php?action=query&prop=extracts&explaintext=1&titles=...&format=json — full plain text"
        ),
    },
    "free_dictionary": {
        "name": "Free Dictionary",
        "base_url": "https://api.dictionaryapi.dev",
        "auth_type": "none",
        "description": (
            "English definitions, phonetics, synonyms, no key.\n"
            "  GET /api/v2/entries/en/{word}"
        ),
    },
    "open_library": {
        "name": "Open Library (books)",
        "base_url": "https://openlibrary.org",
        "auth_type": "none",
        "description": (
            "Books, authors and covers, no key.\n"
            "  GET /search.json?q=the+lord+of+the+rings&limit=5&fields=title,author_name,first_publish_year,key,isbn\n"
            "  GET /isbn/{isbn}.json — one edition; GET /works/{OL...W}.json — a work\n"
            "  Covers: https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg"
        ),
    },
    "hacker_news": {
        "name": "Hacker News",
        "base_url": "https://hacker-news.firebaseio.com",
        "auth_type": "none",
        "description": (
            "Hacker News, no key.\n"
            "  GET /v0/topstories.json (also beststories, newstories, askstories, showstories) — ids\n"
            "  GET /v0/item/{id}.json — a story or comment (title, url, score, kids)"
        ),
    },
    # Homelab: from awesome-openclaw-skills' "adguard" skill. Basic auth: api_key = "user:password".
    "adguard_home": {
        "name": "AdGuard Home",
        "auth_type": "basic",
        "description": (
            "AdGuard Home DNS filtering. Auth = Basic with 'user:password' as the key. Key endpoints:\n"
            "  GET /control/status — running, protection_enabled, version\n"
            "  GET /control/stats — queries, blocked, top domains/clients\n"
            "  GET /control/querylog?limit=50&search=example.com — recent queries\n"
            "  POST /control/protection {\"enabled\": false, \"duration\": 600000} — pause blocking for 10 min\n"
            "  GET /control/filtering/check_host?name=example.com — why a domain is (not) blocked"
        ),
    },
}
