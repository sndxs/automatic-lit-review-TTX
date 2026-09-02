"""
Central configuration for the TTX (Test-Taker eXperience) lit reviewer.

Search strategy: for each language, every source builds a query requiring
(a testing-related term) AND (an experience/reaction-related term) --
see TOPIC_TERMS / EXPERIENCE_TERMS below. This avoids the noise you get
from single long free-text phrases, where a document can rank highly by
matching just one common word (e.g. "student") and none of the ones that
actually make it on-topic.

Server-side query narrowing differs by backend and isn't fully reliable on
its own (some engines score by partial term overlap rather than enforcing
AND), so every result -- from every source, including snowballed ones --
is also run through the RELEVANCE_KEYWORDS gate in relevance.py before
being kept. Tune RELEVANCE_KEYWORDS to loosen or tighten precision.
"""

from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # pulls GMAIL_*, ANTHROPIC_API_KEY, etc. from a local .env (gitignored) into os.environ

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent
PAPERS_DIR = PROJECT_ROOT / "papers"
LOGS_DIR = PROJECT_ROOT / "logs"
DB_PATH = PROJECT_ROOT / "seen_papers.db"
RESULTS_CSV = PROJECT_ROOT / "results.csv"

# ---------------------------------------------------------------------------
# Search terms, by language
# ---------------------------------------------------------------------------

# "Is this about a standardized/high-stakes test at all?"
TOPIC_TERMS = {
    "en": [
        "standardized test", "standardised test", "standardized testing",
        "standardised testing", "high-stakes testing", "high-stakes test",
        "test taker", "test-taker",
    ],
    "es": [
        "prueba estandarizada", "pruebas estandarizadas",
        "examen estandarizado", "examenes estandarizados",
        "evaluacion estandarizada",
    ],
    "fr": [
        "test standardise", "tests standardises", "examen standardise",
        "examens standardises", "evaluation standardisee",
    ],
}

# "...and does it talk about the test-taker's experience of it?"
#
# The "assessment experience" / "construct-irrelevant" / "invalidity
# hypothesis" terms come from Araneda, Sireci, Crespo Cruz & Mena Serrano's
# (2025) conceptual framework for Test-Taker eXperience (TTX) and Araneda's
# (2023) dissertation on the Experiential Approach to test validation --
# see the "TTX Conceptual Framework" section of literature_review.tex for
# how this model is used to interpret the corpus.
EXPERIENCE_TERMS = {
    "en": [
        "test anxiety", "student experience", "test-taker experience",
        "wellbeing", "well-being", "perceptions", "stress", "fairness",
        "testing experience", "assessment experience", "construct-irrelevant",
        "invalidity hypothesis", "examinee experience", "test-taker voice",
        "response process", "caring assessment", "user experience",
    ],
    "es": [
        "ansiedad", "experiencia estudiantil", "experiencia de los examinados",
        "percepcion", "estres", "bienestar", "experiencia del examinado",
        "proceso de respuesta", "voz del examinado",
    ],
    "fr": [
        "anxiete", "experience etudiante", "experience des candidats",
        "perception", "stress", "bien-etre", "experience de l'examine",
        "processus de reponse",
    ],
}

# Final client-side relevance gate applied to every candidate record from
# every source (including snowballed references/citations/recommendations)
# before it's kept: title + abstract must contain at least one of these
# phrases (accent- and case-insensitive). This is what actually enforces
# precision -- the per-source queries above only narrow the search
# server-side to keep result counts and API usage sane.
RELEVANCE_KEYWORDS = [
    # English
    "test anxiety", "test-taker", "test taker", "standardized test",
    "standardised test", "high-stakes test", "high stakes test",
    "examination experience", "test wellbeing", "test well-being",
    "exam stress", "test fairness", "testing experience",
    "student experience", "test-taker experience", "assessment experience",
    "construct-irrelevant", "invalidity hypothesis",
    # Spanish
    "ansiedad ante examenes", "ansiedad ante los examenes",
    "prueba estandarizada", "pruebas estandarizadas", "examinado",
    "experiencia estudiantil", "experiencia de los examinados",
    "estres examen", "examen estandarizado",
    # French
    "anxiete examens", "anxiete liee aux examens", "test standardise",
    "examens standardises", "candidat examen", "experience etudiante",
    "experience des candidats", "stress examen", "examen standardise",
]

# ---------------------------------------------------------------------------
# Source toggles
# ---------------------------------------------------------------------------

# arXiv: free, no key, English-only, low relevance for this topic but cheap
# to query and occasionally turns up psychometrics/education-adjacent work.
ENABLE_ARXIV = True

# SHARE (share.osf.io): free, no key, aggregates OSF preprint servers
# (PsyArXiv, EdArXiv, SocArXiv, ...) plus hundreds of other repositories
# worldwide -- the main source for multilingual coverage right now.
ENABLE_SHARE = True

# CORE.ac.uk: free API key required. Broad open-access aggregator with
# strong multilingual full-text coverage. Sign up at
# https://core.ac.uk/services/api and put the key below or in the
# CORE_API_KEY environment variable.
ENABLE_CORE = False
CORE_API_KEY = ""  # or set the CORE_API_KEY environment variable instead

# OpenAlex: free, no key ("mailto" below just gets better rate limits via
# their "polite pool"). This is the "white literature" source -- restricted
# to type=article with a real journal host, i.e. formally published,
# peer-reviewed work, as distinct from the preprints SHARE/arXiv cover.
ENABLE_OPENALEX = True
OPENALEX_MAILTO = "sondaxius@gmail.com"

# GDELT DOC 2.0 API: free, no key. The newspaper/magazine source -- global
# news metadata (title, URL, date, outlet) in many languages, continuously
# updated. No abstract/snippet and no full text is ever available from
# this source, so results are always metadata + link only.
ENABLE_GDELT = True

# Which of the two broad categories each source belongs to, for reporting
# results separately by category in literature_review.tex (Section on
# "Findings by Source Category"). Add new sources here when you add them.
SOURCE_CATEGORY = {
    "arxiv": "academic-preprint",
    "share": "academic-preprint",
    "core": "academic-preprint",
    "openalex": "academic-peer-reviewed",
    "gdelt": "news",
    "snowball-reference": "academic-preprint",
    "snowball-citation": "academic-preprint",
    "snowball-recommendation": "academic-preprint",
}

# ---------------------------------------------------------------------------
# Snowballing (Semantic Scholar)
# ---------------------------------------------------------------------------

# For each newly-found record (up to SNOWBALL_MAX_SEEDS per run) that has a
# DOI or arXiv ID, pull:
#   - its references  (backward snowball: papers it cites)
#   - its citations    (forward snowball: papers that cite it)
#   - its recommendations (Semantic Scholar's "related papers", similar to
#     Google Scholar's "related articles" / "cited by" panel)
# Candidates go through the same RELEVANCE_KEYWORDS gate as everything else
# -- otherwise a single seed paper's reference list could pull in dozens of
# unrelated papers (methods textbooks, unrelated citing papers, etc).
#
# Works without an API key at a low rate limit; get a free key at
# https://www.semanticscholar.org/product/api#api-key-form for reliable
# daily use and set it below or via the S2_API_KEY environment variable.
ENABLE_SNOWBALL = True
S2_API_KEY = ""  # or set the S2_API_KEY environment variable instead
SNOWBALL_MAX_SEEDS = 15
SNOWBALL_MAX_PER_RELATION = 10

# Persistent seeds: snowballed every run regardless of whether they were
# found by this run's keyword search, on top of (not counted against)
# SNOWBALL_MAX_SEEDS. Use this to directly track citations of foundational
# papers -- e.g. the TTX conceptual framework this project builds on -- so
# future papers that adopt or critique it surface automatically via forward
# citations, even on days the keyword search finds nothing new.
SEED_PAPERS = [
    {
        "doi": "10.7275/33072110",
        "label": "Araneda (2023) - An Experiential Approach to Test Design and Validation",
    },
    # Araneda, Sireci, Crespo Cruz & Mena Serrano (2025), "A Conceptual
    # Framework for Test-Taker Experience in Educational Testing", is a
    # Dec 2025 preprint with no DOI/arXiv ID yet -- add one here once it's
    # assigned so it gets tracked the same way.
]

# ---------------------------------------------------------------------------
# Download behaviour
# ---------------------------------------------------------------------------

# Only attempt to download a file when the source gives us a direct,
# openly-accessible file URL (OSF /download links, CORE downloadUrl,
# arXiv PDF links). Everything else (paywalled journals, newspaper
# articles, or repository pages with no direct file) is recorded as
# metadata + link only -- see feedback from project setup.
MAX_DOWNLOAD_MB = 50  # skip files larger than this to avoid runaway downloads
REQUEST_TIMEOUT_SECONDS = 30
RESULTS_PER_QUERY = 30

# ---------------------------------------------------------------------------
# Two-track paper: official (manually validated) vs transitory (LLM-updated)
# ---------------------------------------------------------------------------

# `literature_review_official.tex` only changes when a human runs
# promote_transitory.py after reviewing the transitory draft -- nothing in
# this pipeline writes to it automatically.
OFFICIAL_PAPER_PATH = PROJECT_ROOT / "literature_review_official.tex"

# `literature_review_transitory.tex` is rewritten by paper_updater.py at the
# end of any run that found new relevant records, folding them in via the
# Claude API. Treat it as a draft -- review before promoting.
TRANSITORY_PAPER_PATH = PROJECT_ROOT / "literature_review_transitory.tex"

# A dated copy of the transitory file is saved here before every automated
# rewrite (and of the official file before every promotion), so a bad LLM
# update or promotion is always one copy away from undone. Gitignored.
TRANSITORY_BACKUPS_DIR = PROJECT_ROOT / "transitory_backups"

# Get a key at https://console.anthropic.com/settings/keys and put it in
# .env as ANTHROPIC_API_KEY (see .env.example) -- never paste a real key
# here. If unset, the transitory-paper update step is skipped (logged, not
# fatal) and the rest of the pipeline runs normally.
ANTHROPIC_API_KEY = ""
PAPER_UPDATE_MODEL = "claude-sonnet-5"
PAPER_UPDATE_MAX_TOKENS = 32000

# ---------------------------------------------------------------------------
# Email notification (sent at the end of every run, new papers or not)
# ---------------------------------------------------------------------------

# Gmail SMTP with an App Password: https://myaccount.google.com/apppasswords
# (needs 2-Step Verification enabled on the account first). Put real values
# in .env (see .env.example) -- never paste a real password here. If any of
# the three below are unset, the email step is skipped (logged, not fatal).
GMAIL_ADDRESS = ""
GMAIL_APP_PASSWORD = ""
NOTIFY_EMAIL_TO = "sondaxius@gmail.com"
