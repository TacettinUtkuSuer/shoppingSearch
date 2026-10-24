# shoppingSearch: Autonomous Product Discovery and Audit Agent

Describe a product in vague words ("water thingy for bottle"). The Resolver agent asks
one clarifying question if the intent is ambiguous, then locks the scope into technical
search terms plus negative keywords. A background deep search discovers candidate pages
with DuckDuckGo, scrapes them with a Chrome TLS fingerprint, prunes each page to under
300 words, and has an adversarial Auditor agent score trust, detect seller/review
contradictions and compute the real per-unit price.

The LLM is reached only through the local Claude CLI (`claude -p`). No API keys are needed.

## Requirements

- Python 3.10+
- Claude Code CLI installed and logged in (`claude -p "hello"` must work in your shell)

## Run

    pip install -r requirements.txt
    uvicorn main:app --host 127.0.0.1 --port 8000

Open http://127.0.0.1:8000. Use a single uvicorn worker: deep searches run inside the
server process, so you can close the browser and come back later.

## How it works

1. Clarify scope (view 1). Only the original description and the question/answer pairs
   go to the Resolver; no chat history is accumulated. After 2 answers the Resolver is
   forced to commit. You can edit search terms and negative keywords before launching.
2. Discovery (0 tokens). Each search term goes to the DuckDuckGo HTML endpoint with
   `-keyword` operators. Results are filtered again in Python (negative keywords, social
   and video sites), tracking parameters are stripped, and URLs are deduplicated.
3. Fetch and prune (0 tokens). curl_cffi impersonates Chrome 124. Captcha pages and
   HTTP 403/429/503 trigger a 5 to 12 second jittered backoff and retry. The pruner
   reads JSON-LD Product data first, then meta tags and site heuristics, drops script,
   style, svg, nav, header, footer and noscript, and keeps title, price, stock, shipping,
   description and the top 5 reviews within 300 words. Pages that do not look like a
   product page never reach the LLM.
4. Audit (1 CLI call per product). The Auditor returns JSON only. Python recomputes
   `normalized_unit_price = (price + shipping) / units_in_pack` from the scraped price, so
   the model only has to count units.
5. Results (view 2). Live status badge, Pause/Resume/Stop, sorting by unit price or trust
   score, and filters built from the attribute keys the Auditor found.

## Task control and limits

- Status lives in SQLite (`agent.db`, WAL mode). The worker checks it before every URL.
- A task ends after `max_duration_seconds` of active (non-paused) time or 50 audited
  products, whichever comes first.
- Successfully processed URLs are recorded in `visited_urls`, so a resumed task never
  fetches them again. Rate-limited URLs and failed audits are not recorded and are
  retried on resume. If a URL was already audited by another task, its audit is reused
  at zero cost.
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
- MAX_AUDITED_PRODUCTS: product cap per task (default 50)
- AGENT_DB_PATH: SQLite file location

## API

- POST /api/resolve  body: {"description": "...", "clarifications": [{"question": "...", "answer": "..."}]}
- POST /api/tasks  body: {"description", "search_terms", "negative_keywords", "detected_category", "max_duration_seconds"}
- GET /api/tasks, GET /api/tasks/{id}
- POST /api/tasks/{id}/pause, /resume, /stop
- GET /api/tasks/{id}/products?sort=unit_price|-unit_price|trust_score|-trust_score|-created_at

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
