"""On-demand, country-by-country deep dive for standardized-testing news.

This is a separate, manually-run tool -- NOT part of the daily scheduled
pipeline (main.py). Use it when you want a broader, slower sweep for
test-taker-experience news coverage than the daily run's GDELT query
provides, including non-English-language coverage.

For each country in COUNTRIES (alphabetical), it:
  1. Gets (or translates, once per language, then caches) the topic/
     experience search terms into that country's dominant language, via
     the Claude API.
  2. Queries GDELT's DOC 2.0 API (free, no key) for candidate articles from
     that country's press, in that language.
  3. Fetches the full text of the top candidates and sends them ALL in one
     Claude API call per country, asking it to judge genuine relevance and,
     for anything relevant, produce an English translation/summary.
  4. Appends relevant findings to deep_dive_news/findings.csv and records
     the country as done in deep_dive_news/progress.json, so a long sweep
     can be stopped and resumed later without re-covering ground.

Usage:
    python deep_dive_news.py                  # resume, no country limit
    python deep_dive_news.py --limit 20        # do at most 20 countries this run
    python deep_dive_news.py --reset           # clear progress, start over from A
    python deep_dive_news.py --max-candidates 8

Cost/time note: this makes one Claude API call per unique language (cached)
plus up to one Claude API call per country that has candidates -- a full
~190-country sweep is on the order of 150-250 calls total, plus GDELT's
5-second-per-request courtesy throttle, so expect this to take a while.
Use --limit to run it in bounded chunks.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import requests

import config
from sources.gdelt import _TOPIC_TERMS as GDELT_TOPIC_TERMS, _EXPERIENCE_TERMS as GDELT_EXPERIENCE_TERMS

log = logging.getLogger("deep_dive_news")

OUTPUT_DIR = config.PROJECT_ROOT / "deep_dive_news"
FINDINGS_CSV = OUTPUT_DIR / "findings.csv"
PROGRESS_JSON = OUTPUT_DIR / "progress.json"

GDELT_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_MIN_REQUEST_INTERVAL_SECONDS = 5
GDELT_MAX_RETRIES = 3
GDELT_RETRY_BACKOFF_SECONDS = 10

DEFAULT_MAX_CANDIDATES_PER_COUNTRY = 5
ARTICLE_FETCH_TIMEOUT_SECONDS = 20
ARTICLE_HTML_MAX_CHARS = 20000  # per candidate, after stripping <script>/<style>
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TTXLitReviewer-DeepDive/1.0"

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


def setup_logging() -> None:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_file = config.LOGS_DIR / f"deep_dive_{datetime.now():%Y-%m-%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    )


def _load_progress() -> dict:
    if PROGRESS_JSON.exists():
        return json.loads(PROGRESS_JSON.read_text(encoding="utf-8"))
    return {"completed_countries": [], "findings_count": 0}


def _save_progress(progress: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROGRESS_JSON.write_text(json.dumps(progress, indent=2), encoding="utf-8")


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


_TERM_TRANSLATION_CACHE: dict = {}


def _get_terms_for_language(client: anthropic.Anthropic, language: str) -> dict:
    """Translate the compact EN topic/experience search terms into `language`,
    once per language, cached across all countries that share it."""
    if language == "english":
        return {"topic": GDELT_TOPIC_TERMS["en"], "experience": GDELT_EXPERIENCE_TERMS["en"]}
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
        terms = {"topic": GDELT_TOPIC_TERMS["en"], "experience": GDELT_EXPERIENCE_TERMS["en"]}

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


def _analyze_candidates(client: anthropic.Anthropic, country: str, candidates: list) -> list:
    """One Claude call for all of a country's candidates: judge relevance,
    translate/summarize anything genuinely about standardized testing or
    test-taker experience."""
    blocks = []
    for i, c in enumerate(candidates):
        html_note = c["html"] if c["html"] else "(could not fetch full article -- judge from title only)"
        blocks.append(
            f"--- Candidate {i} ---\n"
            f"Title: {c['title']}\n"
            f"Domain: {c['domain']}\n"
            f"Date: {c['date']}\n"
            f"URL: {c['url']}\n"
            f"Page content (HTML, possibly noisy/truncated):\n{html_note}"
        )
    prompt = (
        f"You are screening news candidates from {country}'s press for genuine relevance to "
        f"standardized testing and test-taker experience (test anxiety, fairness, exam stress, "
        f"testing policy, student wellbeing around exams, etc.) -- not generic use of the word "
        f"'test', not test-prep advertising, not unrelated medical/product testing.\n\n"
        f"For each candidate below, read the page content (if given) in its original language "
        f"and decide if it's genuinely relevant. Respond with ONLY a JSON array, one object per "
        f"candidate, in the same order, with this shape:\n"
        f'{{"index": 0, "relevant": true/false, "original_language": "<language of the article>", '
        f'"original_title": "<article\'s actual title, translated to English if you can>", '
        f'"english_summary": "<2-3 sentence English summary of what it actually reports, only if relevant, else empty>"}}\n\n'
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
        log.exception("Failed to analyze candidates for %s.", country)
        return []


def run(limit: int, max_candidates: int) -> None:
    setup_logging()
    _ensure_findings_csv()
    progress = _load_progress()
    completed = set(progress.get("completed_countries", []))

    api_key = os.environ.get("ANTHROPIC_API_KEY") or config.ANTHROPIC_API_KEY
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set -- cannot run (needed for translation and relevance filtering).")
        return
    client = anthropic.Anthropic(api_key=api_key, timeout=120.0)

    todo = [c for c in COUNTRIES if c[1] not in completed]
    if limit:
        todo = todo[:limit]

    log.info("Starting deep dive: %d countries this run (%d already done, %d total).", len(todo), len(completed), len(COUNTRIES))

    for fips_code, country, language in todo:
        log.info("=== %s (%s) ===", country, language or "no language filter")
        terms = _get_terms_for_language(client, language) if language else \
            {"topic": GDELT_TOPIC_TERMS["en"], "experience": GDELT_EXPERIENCE_TERMS["en"]}

        articles = _query_gdelt(fips_code, language, terms, max_candidates)
        log.info("  GDELT: %d candidate(s).", len(articles))

        if articles:
            candidates = []
            for a in articles[:max_candidates]:
                url = a.get("url")
                if not url:
                    continue
                candidates.append({
                    "url": url,
                    "title": (a.get("title") or "").strip(),
                    "domain": a.get("domain", ""),
                    "date": a.get("seendate", ""),
                    "html": _fetch_article_html(url),
                })

            if candidates:
                results = _analyze_candidates(client, country, candidates)
                for r in results:
                    idx = r.get("index")
                    if idx is None or not (0 <= idx < len(candidates)) or not r.get("relevant"):
                        continue
                    c = candidates[idx]
                    _append_finding(country, r, c["domain"], c["date"], c["url"])
                    progress["findings_count"] = progress.get("findings_count", 0) + 1
                    log.info("  RELEVANT: %s", r.get("original_title", "")[:80])

        completed.add(country)
        progress["completed_countries"] = sorted(completed)
        _save_progress(progress)

    log.info("Deep dive run complete. %d/%d countries done overall, %d finding(s) total.",
              len(completed), len(COUNTRIES), progress.get("findings_count", 0))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None, help="Process at most this many countries this run.")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES_PER_COUNTRY,
                         help="Max GDELT candidates to deep-analyze per country (default %(default)s).")
    parser.add_argument("--reset", action="store_true", help="Clear progress and start over from the first country.")
    args = parser.parse_args()

    if args.reset and PROGRESS_JSON.exists():
        PROGRESS_JSON.unlink()

    run(limit=args.limit, max_candidates=args.max_candidates)
