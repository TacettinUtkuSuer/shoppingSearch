# shoppingSearch: Autonomous Product Discovery and Audit Agent

Describe a product in vague words ("water thingy for bottle"). The Resolver agent asks
one clarifying question if the intent is ambiguous, then locks the scope into technical
search terms plus negative keywords. A background deep search discovers candidate pages
with DuckDuckGo, scrapes them with a Chrome TLS fingerprint, prunes each page to under
300 words, and has an adversarial Auditor agent score trust, detect seller/review
contradictions and compute the real per-unit price.

The LLM is reached only through the local Claude CLI (`claude -p`). No API keys are needed.

## Requirements

- Python 3.8+ on Windows, macOS or Linux (tested on 3.8 and 3.13)
- Claude Code CLI installed and logged in (`claude -p "hello"` must work in your shell)

A dedicated virtual environment is recommended so these packages do not mix with
unrelated projects (for example an Anaconda base environment with TensorFlow):

    python -m venv .venv
    .venv\Scripts\activate        (Windows)
    source .venv/bin/activate     (macOS/Linux)

On Windows the npm `claude.cmd` shim is found through PATH automatically, and prompts
are sent through stdin as UTF-8, so multi-line and Turkish text are passed safely. If
the CLI lives outside PATH, set CLAUDE_BIN to its full path.

## Run

    pip install -r requirements.txt
    uvicorn main:app --host 127.0.0.1 --port 8000

Open http://127.0.0.1:8000. Use a single uvicorn worker: deep searches run inside the
server process, so you can close the browser and come back later.

## How it works

1. Clarify scope (view 1). Only the original description and the question/answer pairs
   go to the Resolver; no chat history is accumulated. After 2 answers the Resolver is
   forced to commit. The Resolver also extracts must-have requirements ("19 L", "Type-C",
   "under 500 TL") from what you wrote. You can edit search terms, requirements and negative
   keywords before launching.
2. Discovery (0 tokens). Each search term goes to the DuckDuckGo HTML endpoint with
   `-keyword` operators. Results are filtered again in Python (negative keywords, social
   and video sites), tracking parameters are stripped, and URLs are deduplicated.
3. Fetch and prune (0 tokens). curl_cffi impersonates Chrome 124. Captcha pages and
   HTTP 403/429/503 trigger a 5 to 12 second jittered backoff and retry. The pruner
   reads JSON-LD Product data first, then meta tags and site heuristics, drops script,
   style, svg, nav, header, footer and noscript, and keeps title, price, stock, shipping,
   description and the top 5 reviews within 300 words. Pages that do not look like a
   product page never reach the LLM.
   If a result is a category or search page (many product links, no single buy box), it is
   never audited; its first product links are queued instead (one level deep).
4. Audit (1 CLI call per product). The Auditor returns JSON only. Python recomputes
   `normalized_unit_price = (price + shipping) / units_in_pack` from the scraped price, so
   the model only has to count units. The Auditor also scores how well the listing matches your
   request (`match_score`, 0 to 100) and lists the requirements it fails or cannot verify.
   Listings below MIN_MATCH_SCORE (default 50) are stored but hidden unless you tick "Show
   non-matching", and they do not count toward the 50-product cap.
5. Results (view 2). Live status badge, Pause/Resume/Stop, sorting by unit price or trust
   score, and filters built from the attribute keys the Auditor found.

## Task control and limits

- Status lives in SQLite (`agent.db`, WAL mode). The worker checks it before every URL.
- A task ends after `max_duration_seconds` of active (non-paused) time or 50 audited
  products, whichever comes first.
- Processed URLs are recorded per task in `visited_urls`, so a resumed task never fetches
  them again. Rate-limited URLs and failed audits are not recorded and are retried on
  resume. Tasks never share audits: a score is only valid for the request it was made for.
- If the search engine blocks every search, the task ends as FAILED with a clear message.
- Databases from older versions are migrated automatically on startup.
- Three consecutive Claude CLI failures pause the task (for example, CLI logged out).
- Restarting the server marks interrupted tasks as PAUSED; press Resume.

## Configuration

All settings are in `config.py` and can be overridden with environment variables.
Useful ones:

- CLAUDE_BIN: path to the Claude CLI binary (default `claude`)
- CLAUDE_EXTRA_ARGS: extra CLI flags, for example `--model claude-sonnet-5-5`
- LLM_TIMEOUT_SECONDS: per-call timeout (default 90)
- SEARCH_REGION: DuckDuckGo region code, for example `tr-tr` or `us-en` (default `wt-wt`)
- HTTP_ACCEPT_LANGUAGE: Accept-Language header, for example `tr-TR,tr;q=0.9,en;q=0.8`
- MAX_AUDITED_PRODUCTS: matching-product cap per task (default 50)
- MIN_MATCH_SCORE: minimum match score to show a product by default (default 50)
- MAX_LISTING_EXPANSIONS: how many category pages may be expanded per task (default 15)
- SEARCH_MAX_NEGATIVE_OPERATORS: how many negatives are sent to the search engine as -operators
  (default 3; all are still enforced in Python)
- AGENT_DB_PATH: SQLite file location

## API

- POST /api/resolve  body: {"description": "...", "clarifications": [{"question": "...", "answer": "..."}]}
- POST /api/tasks  body: {"description", "search_terms", "negative_keywords", "requirements", "detected_category", "max_duration_seconds"}
- GET /api/tasks, GET /api/tasks/{id}
- POST /api/tasks/{id}/pause, /resume, /stop
- GET /api/tasks/{id}/products?sort=unit_price|-unit_price|trust_score|-trust_score|-match_score|-created_at&include_rejected=false

## Verification

Offline tests (no network, Claude CLI mocked at the subprocess boundary):

    pip install pytest httpx
    python -m pytest tests

They cover pruning reduction over 90% with reviews kept intact, regex JSON extraction
from messy output plus the single corrective retry, bot-block backoff, pause/resume
without refetching visited URLs (including resuming from a fresh engine after a restart),
stop, duration and product limits.

Live check against real sites and the real CLI:

    python scripts/verify_live.py "type-c water gallon pump" --negative aquarium submersible --audit

Some marketplaces still block datacenter IPs regardless of TLS fingerprint. Run the live
check from the machine that will run the agent.
