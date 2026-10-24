"""Live verification for checklist items that need the real network and the real Claude CLI.

Usage:
    python scripts/verify_live.py "type-c water gallon pump" [--audit]

1. Searches DuckDuckGo with curl_cffi and fetches up to 5 product pages, reporting
   HTTP status codes (checklist item 1: no 403 Forbidden).
2. Prunes each page and reports size reduction, word count and review count (item 2).
3. With --audit, sends the first product page through the real Claude CLI (item 3).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import agents  # noqa: E402
from core.scraper import RateLimitError, ScrapeError, Scraper, prune_html  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--negative", nargs="*", default=[])
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--audit", action="store_true")
    args = parser.parse_args()

    scraper = Scraper()
    results = scraper.search(args.query, args.negative)
    print(f"search: {len(results)} results")
    failures, audited = 0, False
    for r in results[: args.limit]:
        try:
            fetched = scraper.fetch(r.url)
        except RateLimitError as exc:
            failures += 1
            print(f"BLOCKED  {r.url}  ({exc})")
            continue
        except ScrapeError as exc:
            print(f"SKIP     {r.url}  ({exc})")
            continue
        page = prune_html(fetched.html, fetched.final_url)
        print(
            f"HTTP {fetched.status_code}  {r.url}\n"
            f"         product={page.looks_like_product()} raw={page.raw_bytes}B pruned={page.pruned_bytes}B "
            f"reduction={page.reduction_ratio:.1%} words={page.word_count} reviews={len(page.reviews)} "
            f"price={page.price} {page.currency or ''}"
        )
        if args.audit and not audited and page.looks_like_product():
            print("--- pruned text sent to the auditor ---\n" + page.text)
            print("--- audit ---\n" + json.dumps(agents.audit(page).model_dump(), indent=2))
            audited = True
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
