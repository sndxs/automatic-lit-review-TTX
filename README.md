# TTX Lit Reviewer

Scans daily for new literature and reporting on **test-taker experiences**
in relation to **standardized testing**, across English, Spanish, and
French, and keeps a local archive.

## What it does

- Searches multiple free academic sources per language, requiring (a
  testing-related term) AND (an experience/reaction-related term) -- see
  `TOPIC_TERMS` / `EXPERIENCE_TERMS` in [`config.py`](config.py).
- Runs every candidate result through a client-side relevance check
  (`relevance.py`, using `config.RELEVANCE_KEYWORDS`) before keeping it --
  this is what actually enforces precision, since different search
  backends match/rank text inconsistently on their own.
- **Snowballs** from each newly-found paper that has a DOI or arXiv ID, via
  the Semantic Scholar API: its references (backward snowball), papers
  that cite it (forward snowball), and Semantic Scholar's recommendations
  (their equivalent of Google Scholar's "related articles" / "cited by"
  panel). Snowballed candidates go through the same relevance gate. See
  `ENABLE_SNOWBALL` / `SNOWBALL_MAX_SEEDS` in `config.py`.
- Skips anything already seen in a previous run (tracked in
  `seen_papers.db`).
- Downloads a copy of the paper when the source gives a direct,
  open-access file link. Otherwise it records title/authors/abstract/link
  only -- this is deliberate: full-text of paywalled journal articles or
  newspaper pieces is not scraped or downloaded.
- Writes everything found (downloaded or not) to `results.csv` and to
  `logs/<date>.log`.

## Sources (v1)

| Source | Key needed? | Coverage |
|---|---|---|
| [SHARE](https://share.osf.io) | No | Aggregates OSF's preprint servers (PsyArXiv, EdArXiv, SocArXiv) plus hundreds of other repositories worldwide -- main source of non-English results right now. |
| [arXiv](https://arxiv.org) | No | English-only, CS/physics/math-leaning. Low yield for this topic but free to query. |
| [CORE](https://core.ac.uk) | Yes (free) | Broad open-access aggregator with strong multilingual full-text coverage. Disabled by default -- see setup below. |
| [Semantic Scholar](https://www.semanticscholar.org) | No (works better with a free key) | Not a direct-search source -- used for snowballing (references/citations/recommendations) from papers already found. |

This only covers preprints/open repositories for now, not peer-reviewed
journal indexes, newspapers/magazines, or Google Scholar directly -- Google
Scholar has no official API and scraping it violates their ToS, which is
why snowballing goes through Semantic Scholar instead. Journal indexes and
news sources need different (often paid or key-gated) APIs and can be
added later if you want broader coverage.

## Setup

```bash
pip install -r requirements.txt
```

### Optional: enable CORE for better multilingual coverage

1. Get a free key at https://core.ac.uk/services/api
2. Either set an environment variable:
   ```powershell
   setx CORE_API_KEY "your-key-here"
   ```
   or paste it into `CORE_API_KEY` in `config.py`.
3. Set `ENABLE_CORE = True` in `config.py`.

### Optional: get a Semantic Scholar key for reliable snowballing

Snowballing (`ENABLE_SNOWBALL = True` by default) works without a key, but
the unauthenticated tier is rate-limited hard and can 429 quickly. Get a
free key at https://www.semanticscholar.org/product/api#api-key-form and
either `setx S2_API_KEY "your-key-here"` or paste it into `S2_API_KEY` in
`config.py`.

## Running manually

```bash
python main.py
```

Check `results.csv` for everything found so far, and `papers/` for
downloaded PDFs.

## Running daily automatically

From this folder, in PowerShell:

```powershell
.\register_daily_task.ps1
```

This registers a Windows Task Scheduler job ("TTX Lit Reviewer") that runs
`main.py` once a day at 07:00 (pass `-Time "06:30"` to change it). Your
machine needs to be on for it to fire. To remove it later:

```powershell
Unregister-ScheduledTask -TaskName "TTX Lit Reviewer" -Confirm:$false
```

## Editing search terms / precision

Open [`config.py`](config.py):

- `TOPIC_TERMS` / `EXPERIENCE_TERMS` -- one list per language code (`en`,
  `es`, `fr`). Each source builds a query requiring one topic term AND one
  experience term. Add a new language by adding a new key to both dicts;
  note that arXiv only supports English and SHARE's language mix depends
  on what's indexed, so non-English yield will vary.
- `RELEVANCE_KEYWORDS` -- the final say on whether a result (from any
  source, including snowballed ones) is kept. If you're seeing noise,
  tighten this list; if you're missing things, loosen it.

## Files

- `main.py` -- daily entry point, orchestrates everything below.
- `config.py` -- search terms, relevance keywords, source toggles, paths.
- `relevance.py` -- client-side relevance gate applied to every candidate.
- `sources/` -- one module per source (`arxiv.py`, `share_osf.py`, `core.py`, `semantic_scholar.py`).
- `snowball.py` -- backward/forward citation snowballing + related-papers, via Semantic Scholar.
- `storage.py` -- SQLite dedupe index + CSV export.
- `downloader.py` -- downloads open-access PDFs, skips paywalled/HTML links.
- `papers/` -- downloaded PDFs (gitignored).
- `logs/` -- one log file per run day (gitignored).
- `results.csv` -- full index of everything found (gitignored, regenerated each run).
- `seen_papers.db` -- SQLite dedupe index (gitignored).
