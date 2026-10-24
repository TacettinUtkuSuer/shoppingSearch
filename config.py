"""Global configuration. Every value can be overridden with an environment variable."""

import os
import shlex
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DB_PATH = Path(os.getenv("AGENT_DB_PATH", str(BASE_DIR / "agent.db")))
LOG_LEVEL = os.getenv("AGENT_LOG_LEVEL", "INFO")

# --- LLM CLI bridge -------------------------------------------------------
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
# Extra CLI flags appended after "-p", e.g. CLAUDE_EXTRA_ARGS="--model claude-sonnet-5-5"
CLAUDE_EXTRA_ARGS = shlex.split(os.getenv("CLAUDE_EXTRA_ARGS", ""))
LLM_TIMEOUT_SECONDS = _env_int("LLM_TIMEOUT_SECONDS", 90)
LLM_JSON_RETRIES = _env_int("LLM_JSON_RETRIES", 1)
# Prompts longer than this are piped through stdin instead of argv (ARG_MAX safety).
LLM_ARGV_MAX_CHARS = _env_int("LLM_ARGV_MAX_CHARS", 100_000)
# Consecutive LLM failures that pause a deep search (e.g. CLI missing or logged out).
LLM_MAX_CONSECUTIVE_FAILURES = _env_int("LLM_MAX_CONSECUTIVE_FAILURES", 3)
# Resolver: after this many clarification answers the agent must commit to a scope.
RESOLVER_MAX_CLARIFICATIONS = _env_int("RESOLVER_MAX_CLARIFICATIONS", 2)

# --- Network / scraping ---------------------------------------------------
HTTP_IMPERSONATE = os.getenv("HTTP_IMPERSONATE", "chrome124")
HTTP_TIMEOUT_SECONDS = _env_int("HTTP_TIMEOUT_SECONDS", 25)
HTTP_MAX_RETRIES = _env_int("HTTP_MAX_RETRIES", 3)
# Optional CA bundle for TLS-intercepting proxies. None means curl_cffi's default bundle.
HTTP_CA_BUNDLE = os.getenv("HTTP_CA_BUNDLE") or os.getenv("CURL_CA_BUNDLE") or None
HTTP_ACCEPT_LANGUAGE = os.getenv("HTTP_ACCEPT_LANGUAGE", "en-US,en;q=0.9")
BACKOFF_MIN_SECONDS = _env_float("BACKOFF_MIN_SECONDS", 5.0)
BACKOFF_MAX_SECONDS = _env_float("BACKOFF_MAX_SECONDS", 12.0)
# Polite random delay between two product page fetches.
FETCH_DELAY_MIN_SECONDS = _env_float("FETCH_DELAY_MIN_SECONDS", 1.5)
FETCH_DELAY_MAX_SECONDS = _env_float("FETCH_DELAY_MAX_SECONDS", 4.0)
BOT_BLOCK_MARKERS = (
    "validatecaptcha",
    "robot check",
    "captcha-delivery",
    "are you a robot",
    "anomaly-modal",
    "unusual traffic from your computer",
    "/cdn-cgi/challenge-platform",
)
RATE_LIMIT_STATUS_CODES = (403, 429, 503)

# --- Search discovery -----------------------------------------------------
SEARCH_ENDPOINT = os.getenv("SEARCH_ENDPOINT", "https://html.duckduckgo.com/html/")
SEARCH_REGION = os.getenv("SEARCH_REGION", "wt-wt")
SEARCH_RESULTS_PER_QUERY = _env_int("SEARCH_RESULTS_PER_QUERY", 25)
# Only the first N negatives become -operators (more starves the result list); all of them
# are still enforced in Python on every result.
SEARCH_MAX_NEGATIVE_OPERATORS = _env_int("SEARCH_MAX_NEGATIVE_OPERATORS", 3)
EXCLUDED_DOMAINS = (
    "duckduckgo.com",
    "youtube.com",
    "wikipedia.org",
    "reddit.com",
    "pinterest.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "x.com",
    "twitter.com",
    "quora.com",
    "medium.com",
)

# --- Listing expansion (category / search pages found by the search engine) --
LISTING_MIN_LINKS = _env_int("LISTING_MIN_LINKS", 8)
LISTING_LINKS_PER_PAGE = _env_int("LISTING_LINKS_PER_PAGE", 8)
MAX_LISTING_EXPANSIONS = _env_int("MAX_LISTING_EXPANSIONS", 15)
MAX_CANDIDATES = _env_int("MAX_CANDIDATES", 150)

# --- Relevance ------------------------------------------------------------
# Products scoring below this against the user's request are stored but hidden by default.
MIN_MATCH_SCORE = _env_int("MIN_MATCH_SCORE", 50)

# --- Pruning --------------------------------------------------------------
PRUNE_MAX_WORDS = _env_int("PRUNE_MAX_WORDS", 300)
PRUNE_MAX_REVIEWS = _env_int("PRUNE_MAX_REVIEWS", 5)
PRUNE_REMOVE_TAGS = ("script", "style", "svg", "nav", "footer", "header", "noscript", "iframe", "template")

# --- Task engine ----------------------------------------------------------
DEFAULT_MAX_DURATION_SECONDS = _env_int("DEFAULT_MAX_DURATION_SECONDS", 3600)
MAX_AUDITED_PRODUCTS = _env_int("MAX_AUDITED_PRODUCTS", 50)
STATUS_POLL_SECONDS = _env_float("STATUS_POLL_SECONDS", 1.0)
