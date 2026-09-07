"""On-demand, country-by-country deep dive for standardized-testing news.

This is a separate, manually-run tool -- NOT part of the daily scheduled
pipeline (main.py). Use it when you want a broader, slower sweep for
test-taker-experience news coverage than the daily run's GDELT query
provides, including non-English-language coverage.

Two separate phases, so collecting raw candidates (cheap: GDELT + one
translation call per language, cached) is decoupled from spending Claude
calls on judging/translating them:

  harvest   For each country (alphabetical) not yet harvested, translates
            the search terms into that country's dominant language
            (cached per language), queries GDELT's DOC 2.0 API for
            candidate articles from that country's press, and stores them
            in deep_dive_news/candidates.db. No article fetching, no
            relevance-judging Claude calls here.

  analyze   Weighted-randomly samples a batch of not-yet-analyzed
            candidates from the pool -- weighted toward countries with
            fewer already-analyzed candidates, so repeated `analyze` runs
            spread coverage across countries rather than exhausting
            whichever country was harvested first. Fetches each sampled
            candidate's full text, bundles a few per Claude call, and asks
            it to judge genuine relevance and translate/summarize anything
            relevant into English. Relevant findings are appended to
            deep_dive_news/findings.csv.

Usage:
    python deep_dive_news.py harvest                    # resume, all remaining countries
    python deep_dive_news.py harvest --limit 30          # only 30 more countries this run
    python deep_dive_news.py harvest --reset             # re-harvest every country (kept candidates aren't lost)
    python deep_dive_news.py analyze                     # analyze a batch (default 20) sampled from the pool
    python deep_dive_news.py analyze --batch-size 50

Cost/time note: harvesting all ~190 countries costs one Claude call per
unique language (cached, ~40-60 total) plus GDELT's 5-second-per-request
courtesy throttle even on zero-result countries (so a full harvest takes
15-30+ min regardless of findings). Analyzing costs roughly
batch_size / candidates_per_call Claude calls. Use --limit / --batch-size
to run either phase in bounded chunks across multiple sessions.
"""

import argparse
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

import anthropic
import requests

import config
import latex_compiler
from latex_utils import escape_latex
from sources.gdelt import _TOPIC_TERMS as GDELT_TOPIC_TERMS, _EXPERIENCE_TERMS as GDELT_EXPERIENCE_TERMS

log = logging.getLogger("deep_dive_news")

OUTPUT_DIR = config.PROJECT_ROOT / "deep_dive_news"
DB_PATH = OUTPUT_DIR / "candidates.db"
FINDINGS_CSV = OUTPUT_DIR / "findings.csv"
REPORT_TEX_PATH = config.PROJECT_ROOT / "deep_dive_report.tex"

GDELT_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_MIN_REQUEST_INTERVAL_SECONDS = 5
GDELT_MAX_RETRIES = 3
GDELT_RETRY_BACKOFF_SECONDS = 10

DEFAULT_MAX_CANDIDATES_PER_COUNTRY = 5   # per country, during harvest
DEFAULT_ANALYZE_BATCH_SIZE = 20          # candidates sampled per `analyze` run
DEFAULT_CANDIDATES_PER_CALL = 5          # candidates bundled into each Claude call during analyze
ARTICLE_FETCH_TIMEOUT_SECONDS = 20
ARTICLE_HTML_MAX_CHARS = 20000  # per candidate, after stripping <script>/<style>
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TTXLitReviewer-DeepDive/1.0"

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    uid TEXT PRIMARY KEY,
    country TEXT NOT NULL,
    fips_code TEXT,
    language TEXT,
    title TEXT,
    domain TEXT,
    date_published TEXT,
    url TEXT,
    harvested_at TEXT,
    analyzed INTEGER DEFAULT 0,
    relevant INTEGER,
    original_language TEXT,
    original_title TEXT,
    english_summary TEXT,
    analyzed_at TEXT
);
CREATE TABLE IF NOT EXISTS harvested_countries (
    country TEXT PRIMARY KEY,
    harvested_at TEXT
);
"""

# (FIPS 10-4 code, country name, GDELT sourcelang name or None if unsupported)
# Verified against GDELT's own LOOKUP-COUNTRIES.TXT and LOOKUP-LANGUAGES.TXT.
# None means: no sourcelang filter for this country (sourcecountry alone
# still restricts to that country's press) -- not a bug, just a gap in
# GDELT's own language coverage for that country's dominant language(s).
COUNTRIES = [
    ("AF", "Afghanistan", "persian"),
    ("AL", "Albania", "albanian"),
    ("AG", "Algeria", "arabic"),
    ("AN", "Andorra", "catalan"),
    ("AO", "Angola", "portuguese"),
    ("AC", "Antigua and Barbuda", "english"),
    ("AR", "Argentina", "spanish"),
    ("AM", "Armenia", "armenian"),
    ("AS", "Australia", "english"),
    ("AU", "Austria", "german"),
    ("AJ", "Azerbaijan", "azerbaijani"),
    ("BF", "Bahamas", "english"),
    ("BA", "Bahrain", "arabic"),
    ("BG", "Bangladesh", "bengali"),
    ("BB", "Barbados", "english"),
    ("BO", "Belarus", "russian"),
    ("BE", "Belgium", "french"),
    ("BH", "Belize", "english"),
    ("BN", "Benin", "french"),
    ("BT", "Bhutan", None),
    ("BL", "Bolivia", "spanish"),
    ("BK", "Bosnia-Herzegovina", "bosnian"),
    ("BC", "Botswana", "english"),
    ("BR", "Brazil", "portuguese"),
    ("BX", "Brunei", "malay"),
    ("BU", "Bulgaria", "bulgarian"),
    ("UV", "Burkina Faso", "french"),
    ("BY", "Burundi", "french"),
    ("CB", "Cambodia", None),
    ("CM", "Cameroon", "french"),
    ("CA", "Canada", "english"),
    ("CV", "Cape Verde", "portuguese"),
    ("CT", "Central African Republic", "french"),
    ("CD", "Chad", "french"),
    ("CI", "Chile", "spanish"),
    ("CH", "China", "chinese"),
    ("CO", "Colombia", "spanish"),
    ("CN", "Comoros", None),
    ("CF", "Congo (Republic of)", "french"),
    ("CG", "Congo (Democratic Republic of)", "french"),
    ("CS", "Costa Rica", "spanish"),
    ("IV", "Cote d'Ivoire", "french"),
    ("HR", "Croatia", "croatian"),
    ("CU", "Cuba", "spanish"),
    ("CY", "Cyprus", "greek"),
    ("EZ", "Czech Republic", "czech"),
    ("DA", "Denmark", "danish"),
    ("DJ", "Djibouti", "french"),
    ("DO", "Dominica", "english"),
    ("DR", "Dominican Republic", "spanish"),
    ("TT", "East Timor", None),
    ("EC", "Ecuador", "spanish"),
    ("EG", "Egypt", "arabic"),
    ("ES", "El Salvador", "spanish"),
    ("EK", "Equatorial Guinea", "spanish"),
    ("ER", "Eritrea", None),
    ("EN", "Estonia", "estonian"),
    ("ET", "Ethiopia", None),
    ("FJ", "Fiji", "english"),
    ("FI", "Finland", "finnish"),
    ("FR", "France", "french"),
    ("GB", "Gabon", "french"),
    ("GA", "Gambia", "english"),
    ("GG", "Georgia", "georgian"),
    ("GM", "Germany", "german"),
    ("GH", "Ghana", "english"),
    ("GR", "Greece", "greek"),
    ("GJ", "Grenada", "english"),
    ("GT", "Guatemala", "spanish"),
    # Guinea (Conakry) has no distinct code in GDELT's own country lookup --
    # GV and EK both resolve to Equatorial Guinea there (verified directly
    # against data.gdeltproject.org/api/v2/guides/LOOKUP-COUNTRIES.TXT), so
    # it's omitted here rather than risk silently querying the wrong country.
    ("PU", "Guinea-Bissau", "portuguese"),
    ("GY", "Guyana", "english"),
    ("HA", "Haiti", "french"),
    ("HO", "Honduras", "spanish"),
    ("HK", "Hong Kong", "chinese"),
    ("HU", "Hungary", "hungarian"),
    ("IC", "Iceland", "icelandic"),
    ("IN", "India", "hindi"),
    ("ID", "Indonesia", "indonesian"),
    ("IR", "Iran", "persian"),
    ("IZ", "Iraq", "arabic"),
    ("EI", "Ireland", "english"),
    ("IS", "Israel", "hebrew"),
    ("IT", "Italy", "italian"),
    ("JM", "Jamaica", "english"),
    ("JA", "Japan", "japanese"),
    ("JO", "Jordan", "arabic"),
    ("KZ", "Kazakhstan", "kazakh"),
    ("KE", "Kenya", "english"),
    ("KN", "North Korea", "korean"),
    ("KS", "South Korea", "korean"),
    ("KU", "Kuwait", "arabic"),
    ("KG", "Kyrgyzstan", "russian"),
    ("LA", "Laos", None),
    ("LG", "Latvia", "latvian"),
    ("LE", "Lebanon", "arabic"),
    ("LT", "Lesotho", "english"),
    ("LI", "Liberia", "english"),
    ("LY", "Libya", "arabic"),
    ("LS", "Liechtenstein", "german"),
    ("LH", "Lithuania", "lithuanian"),
    ("LU", "Luxembourg", "french"),
    ("MC", "Macau", "chinese"),
    ("MK", "Macedonia", "macedonian"),
    ("MA", "Madagascar", "french"),
    ("MI", "Malawi", "english"),
    ("MY", "Malaysia", "malay"),
    ("MV", "Maldives", None),
    ("ML", "Mali", "french"),
    ("MT", "Malta", "english"),
    ("MR", "Mauritania", "arabic"),
    ("MP", "Mauritius", "french"),
    ("MX", "Mexico", "spanish"),
    ("MD", "Moldova", "romanian"),
    ("MN", "Monaco", "french"),
    ("MG", "Mongolia", "mongolian"),
    ("MJ", "Montenegro", "serbian"),
    ("MO", "Morocco", "arabic"),
    ("MZ", "Mozambique", "portuguese"),
    ("BM", "Myanmar", None),
    ("WA", "Namibia", "english"),
    ("NP", "Nepal", "nepali"),
    ("NL", "Netherlands", "dutch"),
    ("NZ", "New Zealand", "english"),
    ("NU", "Nicaragua", "spanish"),
    ("NG", "Niger", "french"),
    ("NI", "Nigeria", "english"),
    ("NO", "Norway", "norwegian"),
    ("MU", "Oman", "arabic"),
    ("PK", "Pakistan", "urdu"),
    ("PM", "Panama", "spanish"),
    ("PP", "Papua New Guinea", "english"),
    ("PA", "Paraguay", "spanish"),
    ("PE", "Peru", "spanish"),
    ("RP", "Philippines", "english"),
    ("PL", "Poland", "polish"),
    ("PO", "Portugal", "portuguese"),
    ("QA", "Qatar", "arabic"),
    ("RO", "Romania", "romanian"),
    ("RS", "Russia", "russian"),
    ("RW", "Rwanda", "english"),
    ("SC", "Saint Kitts and Nevis", "english"),
    ("ST", "Saint Lucia", "english"),
    ("VC", "Saint Vincent and the Grenadines", "english"),
    ("WS", "Samoa", "english"),
    ("SM", "San Marino", "italian"),
    ("TP", "Sao Tome and Principe", "portuguese"),
    ("SA", "Saudi Arabia", "arabic"),
    ("SG", "Senegal", "french"),
    ("RI", "Serbia", "serbian"),
    ("SE", "Seychelles", "french"),
    ("SL", "Sierra Leone", "english"),
    ("SN", "Singapore", "english"),
    ("LO", "Slovakia", "slovak"),
    ("SI", "Slovenia", "slovenian"),
    ("SO", "Somalia", "somali"),
    ("SF", "South Africa", "english"),
    ("OD", "South Sudan", "english"),
    ("SP", "Spain", "spanish"),
    ("CE", "Sri Lanka", "sinhalese"),
    ("SU", "Sudan", "arabic"),
    ("NS", "Suriname", "dutch"),
    ("WZ", "Swaziland (Eswatini)", "english"),
    ("SW", "Sweden", "swedish"),
    ("SZ", "Switzerland", "german"),
    ("SY", "Syria", "arabic"),
    ("TW", "Taiwan", "chinese"),
    ("TI", "Tajikistan", "russian"),
    ("TZ", "Tanzania", "swahili"),
    ("TH", "Thailand", "thai"),
    ("TO", "Togo", "french"),
    ("TD", "Trinidad and Tobago", "english"),
    ("TS", "Tunisia", "arabic"),
    ("TU", "Turkey", "turkish"),
    ("TX", "Turkmenistan", "russian"),
    ("TV", "Tuvalu", "english"),
    ("UG", "Uganda", "english"),
    ("UP", "Ukraine", "ukrainian"),
    ("AE", "United Arab Emirates", "arabic"),
    ("UK", "United Kingdom", "english"),
    ("US", "United States", "english"),
    ("UY", "Uruguay", "spanish"),
    ("UZ", "Uzbekistan", "russian"),
    ("NH", "Vanuatu", "english"),
    ("VT", "Vatican City", "italian"),
    ("VE", "Venezuela", "spanish"),
    ("VM", "Vietnam", "vietnamese"),
    ("YM", "Yemen", "arabic"),
    ("ZA", "Zambia", "english"),
    ("ZI", "Zimbabwe", "english"),
]
COUNTRIES.sort(key=lambda c: c[1])

_ENGLISH_TERMS = {"topic": GDELT_TOPIC_TERMS["en"], "experience": GDELT_EXPERIENCE_TERMS["en"]}


def setup_logging(phase: str) -> None:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = config.LOGS_DIR / f"deep_dive_{phase}_{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def _get_connection() -> sqlite3.Connection:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def _ensure_findings_csv() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not FINDINGS_CSV.exists():
        FINDINGS_CSV.write_text(
            "date_found,country,original_language,original_title,english_summary,"
            "source_domain,published_date,url\n",
            encoding="utf-8",
        )


def _append_finding(country: str, item: dict, domain: str, published_date: str, url: str) -> None:
    import csv
    with open(FINDINGS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            country,
            item.get("original_language", ""),
            item.get("original_title", ""),
            item.get("english_summary", ""),
            domain,
            published_date,
            url,
        ])


def _get_client() -> anthropic.Anthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY") or config.ANTHROPIC_API_KEY
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set -- needed for translation and relevance filtering.")
    return anthropic.Anthropic(api_key=api_key, timeout=120.0)


_TERM_TRANSLATION_CACHE: dict = {}


def _get_terms_for_language(client: anthropic.Anthropic, language: str) -> dict:
    """Translate the compact EN topic/experience search terms into `language`,
    once per language, cached across all countries that share it."""
    if not language or language == "english":
        return _ENGLISH_TERMS
    if language in _TERM_TRANSLATION_CACHE:
        return _TERM_TRANSLATION_CACHE[language]

    prompt = (
        f"Translate these two short lists of English search phrases into {language}, "
        f"as they would naturally appear in {language}-language news writing about "
        f"standardized testing. Keep each translated phrase short (2-4 words).\n\n"
        f"Topic phrases: {GDELT_TOPIC_TERMS['en']}\n"
        f"Experience phrases: {GDELT_EXPERIENCE_TERMS['en']}\n\n"
        f'Respond with ONLY a JSON object: {{"topic": ["...", "..."], "experience": ["...", "..."]}}. '
        f"No commentary."
    )
    try:
        response = client.messages.create(
            model=config.PAPER_UPDATE_MODEL, max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        start, end = text.find("{"), text.rfind("}")
        terms = json.loads(text[start:end + 1])
    except Exception:
        log.exception("Failed to translate search terms into %s -- falling back to English terms.", language)
        terms = _ENGLISH_TERMS

    _TERM_TRANSLATION_CACHE[language] = terms
    return terms


_last_gdelt_request_time = 0.0


def _query_gdelt(fips_code: str, language: str, terms: dict, max_records: int) -> list:
    global _last_gdelt_request_time
    topic = " OR ".join(f'"{t}"' for t in terms["topic"])
    experience = " OR ".join(f'"{t}"' for t in terms["experience"])
    query = f"({topic}) ({experience}) sourcecountry:{fips_code}"
    if language:
        query += f" sourcelang:{language}"

    params = {"query": query, "mode": "artlist", "maxrecords": max_records, "format": "json", "sort": "hybridrel"}

    for attempt in range(GDELT_MAX_RETRIES + 1):
        elapsed = time.time() - _last_gdelt_request_time
        if elapsed < GDELT_MIN_REQUEST_INTERVAL_SECONDS:
            time.sleep(GDELT_MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        _last_gdelt_request_time = time.time()

        try:
            resp = requests.get(GDELT_API_URL, params=params, timeout=config.REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            log.warning("GDELT request failed for %r: %s", query, exc)
            return []

        if resp.status_code == 429:
            if attempt < GDELT_MAX_RETRIES:
                wait = GDELT_RETRY_BACKOFF_SECONDS * (attempt + 1)
                log.info("GDELT rate-limited, waiting %ds", wait)
                time.sleep(wait)
                continue
            return []

        try:
            resp.raise_for_status()
            return resp.json().get("articles", [])
        except (requests.RequestException, ValueError):
            return []
    return []


def _fetch_article_html(url: str) -> str:
    try:
        resp = requests.get(url, timeout=ARTICLE_FETCH_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        html = resp.text
    except requests.RequestException as exc:
        log.info("Could not fetch %s: %s", url, exc)
        return ""
    html = re.sub(r"<script.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    return html[:ARTICLE_HTML_MAX_CHARS]


def _analyze_candidates(client: anthropic.Anthropic, candidates: list) -> list:
    """One Claude call for a bundle of candidates (possibly from different
    countries): judge relevance, translate/summarize anything genuinely
    about standardized testing or test-taker experience."""
    blocks = []
    for i, c in enumerate(candidates):
        html_note = c["html"] if c["html"] else "(could not fetch full article -- judge from title only)"
        blocks.append(
            f"--- Candidate {i} ---\n"
            f"Country: {c['country']}\n"
            f"Title: {c['title']}\n"
            f"Domain: {c['domain']}\n"
            f"Date: {c['date']}\n"
            f"URL: {c['url']}\n"
            f"Page content (HTML, possibly noisy/truncated):\n{html_note}"
        )
    prompt = (
        "You are screening news candidates from various countries' press for genuine relevance to "
        "standardized testing and test-taker experience (test anxiety, fairness, exam stress, "
        "testing policy, student wellbeing around exams, etc.) -- not generic use of the word "
        "'test', not test-prep advertising, not unrelated medical/product testing.\n\n"
        "For each candidate below, read the page content (if given) in its original language "
        "and decide if it's genuinely relevant. Respond with ONLY a JSON array, one object per "
        "candidate, in the same order, with this shape:\n"
        '{"index": 0, "relevant": true/false, "original_language": "<language of the article>", '
        "\"original_title\": \"<article's actual title, translated to English if you can>\", "
        '"english_summary": "<2-3 sentence English summary of what it actually reports, only if relevant, else empty>"}\n\n'
        + "\n\n".join(blocks)
    )
    try:
        response = client.messages.create(
            model=config.PAPER_UPDATE_MODEL, max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        start, end = text.find("["), text.rfind("]")
        return json.loads(text[start:end + 1])
    except Exception:
        log.exception("Failed to analyze candidate bundle.")
        return []


def harvest(limit: int, max_candidates: int, reset: bool) -> None:
    setup_logging("harvest")
    conn = _get_connection()
    if reset:
        conn.execute("DELETE FROM harvested_countries")
        conn.commit()
        log.info("--reset: cleared harvested-country tracking (existing candidates are kept; re-harvesting just skips duplicates).")

    client = _get_client()

    done = {row[0] for row in conn.execute("SELECT country FROM harvested_countries")}
    todo = [c for c in COUNTRIES if c[1] not in done]
    if limit:
        todo = todo[:limit]

    log.info("Harvesting: %d countries this run (%d already harvested, %d total).", len(todo), len(done), len(COUNTRIES))

    new_candidates = 0
    for fips_code, country, language in todo:
        log.info("=== Harvesting %s (%s) ===", country, language or "no language filter")
        terms = _get_terms_for_language(client, language)
        articles = _query_gdelt(fips_code, language, terms, max_candidates)
        log.info("  GDELT: %d candidate(s).", len(articles))

        for a in articles:
            url = a.get("url")
            if not url:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO candidates "
                "(uid, country, fips_code, language, title, domain, date_published, url, harvested_at, analyzed) "
                "VALUES (?,?,?,?,?,?,?,?,?,0)",
                (url, country, fips_code, language, (a.get("title") or "").strip(),
                 a.get("domain", ""), a.get("seendate", ""), url, datetime.now(timezone.utc).isoformat()),
            )
            if cur.rowcount:
                new_candidates += 1

        conn.execute(
            "INSERT OR REPLACE INTO harvested_countries (country, harvested_at) VALUES (?, ?)",
            (country, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    done_now = conn.execute("SELECT COUNT(*) FROM harvested_countries").fetchone()[0]
    log.info(
        "Harvest run complete. %d new candidate(s) this run, %d candidate(s) in the pool total. "
        "%d/%d countries harvested overall.",
        new_candidates, total, done_now, len(COUNTRIES),
    )
    conn.close()


def _weighted_sample_without_replacement(rows: list, weight_by_country: dict, k: int) -> list:
    """Efraimidis-Spirakis weighted sampling without replacement: each row
    gets a random key raised to 1/weight, and we take the k largest keys.
    Higher weight -> higher chance of being picked, no duplicates."""
    keyed = []
    for row in rows:
        country = row[1]
        weight = weight_by_country.get(country, 1.0)
        key = random.random() ** (1.0 / weight)
        keyed.append((key, row))
    keyed.sort(key=lambda kv: kv[0], reverse=True)
    return [row for _, row in keyed[:k]]


def analyze(batch_size: int, candidates_per_call: int) -> None:
    setup_logging("analyze")
    conn = _get_connection()
    _ensure_findings_csv()
    client = _get_client()

    unanalyzed = conn.execute(
        "SELECT uid, country, language, title, domain, date_published, url "
        "FROM candidates WHERE analyzed = 0"
    ).fetchall()
    if not unanalyzed:
        log.info("No unanalyzed candidates in the pool -- run `python deep_dive_news.py harvest` first.")
        conn.close()
        return

    analyzed_counts = dict(conn.execute(
        "SELECT country, COUNT(*) FROM candidates WHERE analyzed = 1 GROUP BY country"
    ).fetchall())
    # weight favors countries with fewer already-analyzed candidates so far
    weight_by_country = {row[1]: 1.0 / (1 + analyzed_counts.get(row[1], 0)) for row in unanalyzed}

    sample = _weighted_sample_without_replacement(unanalyzed, weight_by_country, batch_size)
    log.info(
        "Analyzing %d candidate(s) sampled from a pool of %d unanalyzed (weighted toward under-covered countries).",
        len(sample), len(unanalyzed),
    )

    findings_count = 0
    for i in range(0, len(sample), candidates_per_call):
        chunk = sample[i:i + candidates_per_call]
        candidates = []
        for uid, country, language, title, domain, date_published, url in chunk:
            candidates.append({
                "uid": uid, "country": country, "title": title,
                "domain": domain, "date": date_published, "url": url,
                "html": _fetch_article_html(url),
            })

        results = _analyze_candidates(client, candidates)
        results_by_index = {r.get("index"): r for r in results if isinstance(r.get("index"), int)}

        for idx, c in enumerate(candidates):
            r = results_by_index.get(idx)
            relevant = bool(r and r.get("relevant"))
            conn.execute(
                "UPDATE candidates SET analyzed=1, relevant=?, original_language=?, "
                "original_title=?, english_summary=?, analyzed_at=? WHERE uid=?",
                (1 if relevant else 0, (r or {}).get("original_language", ""),
                 (r or {}).get("original_title", ""), (r or {}).get("english_summary", ""),
                 datetime.now(timezone.utc).isoformat(), c["uid"]),
            )
            if relevant:
                _append_finding(c["country"], r, c["domain"], c["date"], c["url"])
                findings_count += 1
                log.info("  RELEVANT [%s]: %s", c["country"], r.get("original_title", "")[:80])
        conn.commit()

    remaining = conn.execute("SELECT COUNT(*) FROM candidates WHERE analyzed = 0").fetchone()[0]
    log.info(
        "Analyze run complete. %d new finding(s) this run, %d candidate(s) still unanalyzed in the pool.",
        findings_count, remaining,
    )
    conn.close()


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
#
# Only the genuinely creative part -- spotting themes that recur across
# multiple countries -- goes through an LLM call, and its output is small
# (one short entry per theme, not one per finding) regardless of how many
# findings exist. Everything else (bibliography, per-country findings list,
# document skeleton) is built directly from the structured DB rows in
# Python. An earlier version of paper_updater.py asked a model to
# regenerate an entire ~100KB document on every update and that reliably
# failed once it grew past ~39k tokens (see git history) -- this design
# doesn't reproduce that mistake: nothing here asks the model to reproduce
# data it was just given.

REPORT_DOC_TEMPLATE = r"""\documentclass[11pt]{{article}}
\usepackage[utf8]{{inputenc}}
\usepackage[T1]{{fontenc}}
\usepackage[margin=1in]{{geometry}}
\usepackage{{hyperref}}
\usepackage{{natbib}}
\usepackage{{parskip}}

\hypersetup{{
    colorlinks=true, linkcolor=blue, urlcolor=blue, citecolor=blue,
    pdftitle={{Deep-Dive News Search: Test-Taker Experience Across Countries}},
    pdfauthor={{Sergio Araneda}}
}}

\title{{Deep-Dive News Search: Test-Taker Experience Across Countries\thanks{{This is an automatically machine-generated summary, produced by \texttt{{deep\_dive\_news.py}} from an automated, country-by-country news search. Findings here are leads to verify, not verified facts -- read the source before citing anything from this document elsewhere.}}}}
\author{{Sergio Araneda \\ Caveon \\ Correspondence: \texttt{{sondaxius@gmail.com}}}}
\date{{\today}}

\begin{{document}}
\maketitle

\section{{Methodology}}

This report summarizes findings from an automated, country-by-country news search (\texttt{{deep\_dive\_news.py}}), separate from this project's main academic literature pipeline. For each country, search terms are translated into that country's dominant language via the Claude API, GDELT's DOC~2.0 API is queried for candidate articles from that country's press, and candidates are fetched and screened for genuine relevance (not generic use of the word ``test'') by the same model, which also translates and summarizes anything relevant into English. As of this report, {country_count} of {total_countries} countries have been searched (harvested), {analyzed_count} candidate articles have been read and judged, and \textbf{{{finding_count} were found genuinely relevant}}, spanning {distinct_country_count} countries.

\section{{Cross-Country Themes}}

{themes_section}

\section{{Findings by Country}}

{findings_section}

\bibliographystyle{{plainnat}}
\begin{{thebibliography}}{{99}}

{bibliography}

\end{{thebibliography}}

\end{{document}}
"""

THEMES_PROMPT_TEMPLATE = """Below is a list of news findings about standardized testing / test-taker experience, from an automated multi-country search. Each has a citation key, country, and English summary.

Identify THEMES that genuinely recur across TWO OR MORE DIFFERENT COUNTRIES -- e.g. AI-proctoring concerns, exam-related student mental health, testing-policy reform debates, equity/fairness disputes, teacher/parent backlash, etc. Do not force a connection between findings that aren't really thematically related, and it is completely fine to return an empty array if nothing genuinely recurs across countries yet.

Respond with ONLY a JSON array, each object shaped exactly like this:
{{"theme": "short theme name", "description": "2-4 sentence description of the pattern across these countries", "keys": ["citation_key1", "citation_key2", ...]}}

`keys` must be copied EXACTLY from the list below (do not invent or alter them), and only include a key if that specific finding genuinely exemplifies the theme -- a theme needs keys from at least 2 different countries to qualify.

=== FINDINGS ({count}) ===
{findings_block}

Output ONLY the JSON array. No commentary, no markdown code fences.
"""


def _make_citation_key(domain: str, date_published: str, idx: int) -> str:
    base = re.sub(r"[^a-z0-9]", "", (domain or "source").lower())[:20] or "source"
    year_match = re.match(r"^\d{4}", date_published or "")
    year = year_match.group(0) if year_match else "nd"
    return f"{base}{year}n{idx}"


def _get_relevant_findings(conn: sqlite3.Connection) -> list:
    rows = conn.execute(
        "SELECT uid, country, original_language, original_title, english_summary, domain, date_published, url "
        "FROM candidates WHERE relevant = 1 ORDER BY country, date_published"
    ).fetchall()
    findings = []
    for i, (uid, country, language, title, summary, domain, date_published, url) in enumerate(rows):
        findings.append({
            "uid": uid, "country": country, "language": language,
            "original_title": title, "english_summary": summary,
            "domain": domain, "date_published": date_published, "url": url,
            "key": _make_citation_key(domain, date_published, i),
        })
    return findings


def _build_bibliography(findings: list) -> str:
    entries = []
    for f in findings:
        year_match = re.match(r"^\d{4}", f["date_published"] or "")
        year = year_match.group(0) if year_match else "n.d."
        domain = escape_latex(f["domain"] or "unknown source")
        title = escape_latex(f["original_title"] or "(untitled)")
        entries.append(
            f"\\bibitem[{domain}({year})]{{{f['key']}}}\n"
            f"{domain}. ({year}). {title}. \\url{{{f['url']}}}"
        )
    return "\n\n".join(entries) if entries else "% no relevant findings yet"


def _build_findings_by_country(findings: list) -> str:
    if not findings:
        return "No relevant findings yet -- run \\texttt{analyze} after harvesting to populate this section."
    by_country: dict = {}
    for f in findings:
        by_country.setdefault(f["country"], []).append(f)
    blocks = []
    for country in sorted(by_country):
        blocks.append(f"\\subsection{{{escape_latex(country)}}}")
        for f in by_country[country]:
            title = escape_latex(f["original_title"] or "(untitled)")
            summary = escape_latex(f["english_summary"] or "")
            lang = escape_latex(f["language"] or "unknown language")
            blocks.append(f"\\textbf{{{title}}} \\citep{{{f['key']}}} ({lang}). {summary}")
    return "\n\n".join(blocks)


def _generate_themes(client: anthropic.Anthropic, findings: list) -> list:
    if len(findings) < 2:
        return []
    blocks = [
        f"- key: {f['key']} | country: {f['country']} | summary: {f['english_summary']}"
        for f in findings
    ]
    prompt = THEMES_PROMPT_TEMPLATE.format(count=len(findings), findings_block="\n".join(blocks))
    try:
        response = client.messages.create(
            model=config.PAPER_UPDATE_MODEL, max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        start, end = text.find("["), text.rfind("]")
        themes = json.loads(text[start:end + 1])
    except Exception:
        log.exception("Failed to generate cross-country themes -- report will note none identified yet.")
        return []

    valid_keys = {f["key"] for f in findings}
    for t in themes:
        t["keys"] = [k for k in t.get("keys", []) if k in valid_keys]
    return [t for t in themes if len(t["keys"]) >= 1]


def _build_themes_section(themes: list) -> str:
    if not themes:
        return "No themes recurring across two or more countries have been identified yet -- check back as more findings accumulate."
    blocks = []
    for t in themes:
        keys = ", ".join(t.get("keys", []))
        blocks.append(
            f"\\subsection{{{escape_latex(t.get('theme', ''))}}}\n"
            f"{escape_latex(t.get('description', ''))} (see \\citep{{{keys}}})."
        )
    return "\n\n".join(blocks)


def report() -> None:
    setup_logging("report")
    conn = _get_connection()
    findings = _get_relevant_findings(conn)
    country_count = conn.execute("SELECT COUNT(*) FROM harvested_countries").fetchone()[0]
    analyzed_count = conn.execute("SELECT COUNT(*) FROM candidates WHERE analyzed = 1").fetchone()[0]
    distinct_countries = len({f["country"] for f in findings})
    conn.close()

    log.info("Generating report from %d relevant finding(s) across %d countries.", len(findings), distinct_countries)

    client = None
    themes = []
    if findings:
        try:
            client = _get_client()
            themes = _generate_themes(client, findings)
        except Exception:
            log.exception("Could not generate themes (e.g. missing API key) -- report will note none identified.")

    doc = REPORT_DOC_TEMPLATE.format(
        country_count=country_count,
        total_countries=len(COUNTRIES),
        analyzed_count=analyzed_count,
        finding_count=len(findings),
        distinct_country_count=distinct_countries,
        themes_section=_build_themes_section(themes),
        findings_section=_build_findings_by_country(findings),
        bibliography=_build_bibliography(findings),
    )

    if not doc.strip().startswith("\\documentclass") or "\\end{document}" not in doc:
        log.error("Assembled report doesn't look like valid LaTeX -- not writing it. This should not happen since the skeleton is fixed; check for a bad character in a finding's title/summary.")
        return

    REPORT_TEX_PATH.write_text(doc, encoding="utf-8")
    log.info("Wrote %s", REPORT_TEX_PATH.name)

    if latex_compiler.compile_pdf(REPORT_TEX_PATH):
        log.info("Compiled %s", REPORT_TEX_PATH.with_suffix(".pdf").name)
    else:
        log.warning("PDF compilation skipped or failed -- see above; the .tex was still written.")


# ---------------------------------------------------------------------------
# Overnight orchestrator: alternate harvest/analyze rounds for a time budget
# ---------------------------------------------------------------------------

def auto(hours: float, harvest_chunk: int, analyze_batch: int, candidates_per_call: int, max_candidates: int) -> None:
    setup_logging("auto")
    _get_client()  # fail fast here if the API key is missing, before looping

    deadline = time.time() + hours * 3600
    round_num = 0
    log.info("Starting overnight auto run: budget %.1f hour(s), ending around %s.",
              hours, datetime.fromtimestamp(deadline).strftime("%Y-%m-%d %H:%M"))

    while time.time() < deadline:
        round_num += 1
        conn = _get_connection()
        harvested_count = conn.execute("SELECT COUNT(*) FROM harvested_countries").fetchone()[0]
        unanalyzed_count = conn.execute("SELECT COUNT(*) FROM candidates WHERE analyzed = 0").fetchone()[0]
        conn.close()

        harvest_done = harvested_count >= len(COUNTRIES)
        if harvest_done and unanalyzed_count == 0:
            log.info("Auto: harvest complete and analyze pool drained -- nothing left to do, stopping early.")
            break

        log.info("=== Auto round %d (harvested %d/%d countries, %d unanalyzed candidates, %.0f min remaining) ===",
                  round_num, harvested_count, len(COUNTRIES), unanalyzed_count, (deadline - time.time()) / 60)

        if not harvest_done:
            try:
                harvest(limit=harvest_chunk, max_candidates=max_candidates, reset=False)
            except Exception:
                log.exception("Auto: harvest round failed, moving on to analyze anyway.")

        if time.time() >= deadline:
            break

        conn = _get_connection()
        unanalyzed_count = conn.execute("SELECT COUNT(*) FROM candidates WHERE analyzed = 0").fetchone()[0]
        conn.close()

        if unanalyzed_count:
            try:
                analyze(batch_size=min(analyze_batch, unanalyzed_count), candidates_per_call=candidates_per_call)
            except Exception:
                log.exception("Auto: analyze round failed, moving on to next round.")
        else:
            log.info("Auto: no unanalyzed candidates yet this round -- skipping analyze.")

    log.info("Auto run finished (time budget reached or nothing left to do). Generating final report.")
    try:
        report()
    except Exception:
        log.exception("Auto: final report generation failed -- findings/candidates.db are still intact, run `report` manually.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_harvest = sub.add_parser("harvest", help="Pull candidate articles from GDELT into the local pool.")
    p_harvest.add_argument("--limit", type=int, default=None, help="Harvest at most this many more countries this run.")
    p_harvest.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES_PER_COUNTRY,
                            help="Max GDELT candidates to pull per country (default %(default)s).")
    p_harvest.add_argument("--reset", action="store_true",
                            help="Re-harvest every country again (existing candidates are kept; duplicates are skipped).")

    p_analyze = sub.add_parser("analyze", help="Sample from the harvested pool and send to Claude for relevance filtering + translation.")
    p_analyze.add_argument("--batch-size", type=int, default=DEFAULT_ANALYZE_BATCH_SIZE,
                            help="How many candidates to analyze this run (default %(default)s), "
                                 "weighted-random-sampled to favor under-covered countries.")
    p_analyze.add_argument("--candidates-per-call", type=int, default=DEFAULT_CANDIDATES_PER_CALL,
                            help="How many candidates to bundle into each Claude call (default %(default)s).")

    sub.add_parser("report", help="Regenerate deep_dive_report.tex/.pdf from findings currently in the pool.")

    p_auto = sub.add_parser("auto", help="Alternate harvest/analyze rounds for a time budget (e.g. overnight), then generate the report.")
    p_auto.add_argument("--hours", type=float, default=7.0, help="Stop after roughly this many hours (default %(default)s).")
    p_auto.add_argument("--harvest-chunk", type=int, default=15, help="Countries harvested per round (default %(default)s).")
    p_auto.add_argument("--analyze-batch", type=int, default=DEFAULT_ANALYZE_BATCH_SIZE, help="Candidates analyzed per round (default %(default)s).")
    p_auto.add_argument("--candidates-per-call", type=int, default=DEFAULT_CANDIDATES_PER_CALL, help="Candidates bundled per Claude call during analyze rounds (default %(default)s).")
    p_auto.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES_PER_COUNTRY, help="Max GDELT candidates pulled per country during harvest rounds (default %(default)s).")

    args = parser.parse_args()
    if args.command == "harvest":
        harvest(limit=args.limit, max_candidates=args.max_candidates, reset=args.reset)
    elif args.command == "analyze":
        analyze(batch_size=args.batch_size, candidates_per_call=args.candidates_per_call)
    elif args.command == "report":
        report()
    elif args.command == "auto":
        auto(
            hours=args.hours, harvest_chunk=args.harvest_chunk, analyze_batch=args.analyze_batch,
            candidates_per_call=args.candidates_per_call, max_candidates=args.max_candidates,
        )
