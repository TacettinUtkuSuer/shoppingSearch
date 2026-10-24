"""Network fetcher, DuckDuckGo discovery and HTML pruning. Pure Python, zero LLM tokens.

`requests` and `urllib.request` are intentionally not used: every HTTP call goes
through curl_cffi so the TLS/HTTP2 fingerprint matches a real Chrome build.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

import config

log = logging.getLogger(__name__)

try:  # lxml is faster and more forgiving; html.parser is the stdlib fallback
    import lxml  # noqa: F401

    _PARSER = "lxml"
except ImportError:  # pragma: no cover
    _PARSER = "html.parser"

BROWSER_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": config.HTTP_ACCEPT_LANGUAGE,
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_TRACKING_PARAMS = re.compile(r"^(utm_|gclid$|fbclid$|msclkid$|ref$|ref_$|_ga$|spm$|srsltid$)")


class ScrapeError(RuntimeError):
    """Permanent fetch failure (404, DNS error, non-HTML)."""


class RateLimitError(ScrapeError):
    """Temporary block: captcha page, 403/429/503. Retried with jittered backoff."""


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    html: str


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str


def detect_bot_block(text: str) -> bool:
    lowered = text[:200_000].lower()
    return any(marker in lowered for marker in config.BOT_BLOCK_MARKERS)


def canonicalize_url(url: str) -> str:
    """Drop fragments and tracking parameters so the same product dedupes to one key."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False) if not _TRACKING_PARAMS.match(k)]
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower() or "https", netloc, path, urlencode(query), ""))


def platform_of(url: str) -> str:
    host = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _domain_excluded(url: str) -> bool:
    host = platform_of(url)
    return any(host == d or host.endswith("." + d) for d in config.EXCLUDED_DOMAINS)


def contains_negative(text: str, negative_keywords: Iterable[str]) -> str | None:
    """Return the first negative keyword found as a whole word/phrase, else None."""
    lowered = text.lower()
    for kw in negative_keywords:
        kw = kw.strip().lstrip("-").lower()
        if kw and re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", lowered):
            return kw
    return None


def build_search_query(term: str, negative_keywords: Iterable[str]) -> str:
    parts = [term.strip()]
    for kw in negative_keywords:
        kw = kw.strip().lstrip("-")
        if not kw:
            continue
        parts.append(f'-"{kw}"' if " " in kw else f"-{kw}")
    return " ".join(parts)


class Scraper:
    def __init__(
        self,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._owns_session = session is None
        self._session = session or self._new_session()
        self._sleep = sleep
        self._rng = rng or random.Random()

    @staticmethod
    def _new_session() -> Any:
        return curl_requests.Session(impersonate=config.HTTP_IMPERSONATE)

    def _request_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"headers": BROWSER_HEADERS, "timeout": config.HTTP_TIMEOUT_SECONDS}
        if config.HTTP_CA_BUNDLE:
            kwargs["verify"] = config.HTTP_CA_BUNDLE
        return kwargs

    def _once(self, method: str, url: str, data: dict[str, str] | None = None) -> FetchResult:
        try:
            if method == "POST":
                resp = self._session.post(url, data=data, allow_redirects=True, **self._request_kwargs())
            else:
                resp = self._session.get(url, allow_redirects=True, **self._request_kwargs())
        except RequestException as exc:
            # Connection resets and timeouts are usually transient; treat like a soft block.
            raise RateLimitError(f"Network error for {url}: {exc}") from exc
        text = resp.text or ""
        if resp.status_code in config.RATE_LIMIT_STATUS_CODES or detect_bot_block(text):
            raise RateLimitError(f"Bot validation or rate limit (HTTP {resp.status_code}) at {url}")
        if resp.status_code >= 400:
            raise ScrapeError(f"HTTP {resp.status_code} for {url}")
        content_type = (resp.headers.get("content-type") or "").lower()
        if content_type and "html" not in content_type and "xml" not in content_type:
            raise ScrapeError(f"Non-HTML content ({content_type}) at {url}")
        return FetchResult(url=url, final_url=str(resp.url or url), status_code=resp.status_code, html=text)

    def _with_retries(self, method: str, url: str, data: dict[str, str] | None = None) -> FetchResult:
        last: RateLimitError | None = None
        for attempt in range(config.HTTP_MAX_RETRIES + 1):
            try:
                return self._once(method, url, data)
            except RateLimitError as exc:
                last = exc
                if attempt >= config.HTTP_MAX_RETRIES:
                    break
                delay = self._rng.uniform(config.BACKOFF_MIN_SECONDS, config.BACKOFF_MAX_SECONDS)
                log.info("Temporary block (%s). Backing off %.1fs before retry %d.", exc, delay, attempt + 1)
                self._sleep(delay)
                if self._owns_session:
                    # A fresh session drops cookies that may have flagged us.
                    self._session = self._new_session()
        assert last is not None
        raise last

    def fetch(self, url: str) -> FetchResult:
        return self._with_retries("GET", url)

    def search(
        self,
        term: str,
        negative_keywords: Iterable[str] = (),
        max_results: int | None = None,
    ) -> list[SearchResult]:
        """Query the DuckDuckGo HTML endpoint and return filtered organic results."""
        negative_keywords = list(negative_keywords)
        query = build_search_query(term, negative_keywords)
        result = self._with_retries(
            "POST", config.SEARCH_ENDPOINT, data={"q": query, "kl": config.SEARCH_REGION}
        )
        results = parse_duckduckgo_html(result.html)
        filtered: list[SearchResult] = []
        seen: set[str] = set()
        for r in results:
            if _domain_excluded(r.url):
                continue
            if contains_negative(f"{r.title} {r.snippet} {r.url}", negative_keywords):
                continue
            key = canonicalize_url(r.url)
            if key in seen:
                continue
            seen.add(key)
            filtered.append(SearchResult(url=key, title=r.title, snippet=r.snippet))
            if len(filtered) >= (max_results or config.SEARCH_RESULTS_PER_QUERY):
                break
        return filtered


def _unwrap_ddg_href(href: str) -> str | None:
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if parts.netloc.endswith("duckduckgo.com") and parts.path.startswith("/l/"):
        target = parse_qs(parts.query).get("uddg", [None])[0]
        return target
    if parts.scheme in ("http", "https"):
        return href
    return None


def parse_duckduckgo_html(html: str) -> list[SearchResult]:
    soup = BeautifulSoup(html, _PARSER)
    results: list[SearchResult] = []
    for block in soup.select("div.result"):
        classes = block.get("class") or []
        if "result--ad" in classes:
            continue
        link = block.select_one("a.result__a")
        if not link:
            continue
        url = _unwrap_ddg_href(link.get("href", ""))
        if not url or "duckduckgo.com/y.js" in url:
            continue
        snippet_el = block.select_one(".result__snippet")
        results.append(
            SearchResult(
                url=url,
                title=_clean(link.get_text(" ")),
                snippet=_clean(snippet_el.get_text(" ")) if snippet_el else "",
            )
        )
    return results


# --- Pruning -------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_PRICE_TOKEN_RE = re.compile(r"\d[\d.,\s  ]*\d|\d")
_CURRENCY_RE = re.compile(r"(US\$|\$|€|£|₺|TL|TRY|USD|EUR|GBP|¥|₹|zł|kr)", re.IGNORECASE)
_CURRENCY_CODES = {"$": "USD", "us$": "USD", "€": "EUR", "£": "GBP", "₺": "TRY", "tl": "TRY", "¥": "JPY", "₹": "INR", "zł": "PLN"}
_FREE_SHIPPING_RE = re.compile(
    r"free (shipping|delivery)|ships free|kargo bedava|ücretsiz kargo|livraison gratuite|kostenloser versand",
    re.IGNORECASE,
)
_SHIPPING_SENTENCE_RE = re.compile(
    r"[^.!?\n]{0,80}\b(shipping|delivery|ships|kargo|teslimat)\b[^.!?\n]{0,120}", re.IGNORECASE
)
_STOCK_RE = re.compile(
    r"\b(in stock|out of stock|currently unavailable|sold out|only \d+ left|stokta|tükendi|available now)\b",
    re.IGNORECASE,
)
_REVIEW_SELECTORS = (
    '[data-hook="review-body"]',
    '[itemprop="reviewBody"]',
    ".review-text",
    ".review-body",
    ".comment-text",
    '[class*="review-text"]',
    '[class*="reviewText"]',
    '[class*="comment-text"]',
    '[class*="review"] p',
)
_DESCRIPTION_SELECTORS = (
    "#feature-bullets",
    "#productDescription",
    '[itemprop="description"]',
    ".product-description",
    '[class*="product-description"]',
    '[class*="productDescription"]',
    '[id*="description"]',
)


def _clean(text: str | None) -> str:
    return _WS_RE.sub(" ", text or "").strip()


def truncate_words(text: str, max_words: int) -> str:
    words = text.split()
    if len(words) <= max_words:
        return text
    if max_words <= 0:
        return ""
    return " ".join(words[:max_words]) + "…"


def parse_price(text: str | float | int | None) -> float | None:
    """Parse '1.249,50 TL', '$1,249.50', '249' into a float. Returns None when no number."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text >= 0 else None
    m = _PRICE_TOKEN_RE.search(str(text))
    if not m:
        return None
    token = re.sub(r"[\s  ]", "", m.group(0))
    if "," in token and "." in token:
        decimal = "," if token.rfind(",") > token.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        token = token.replace(thousands, "").replace(decimal, ".")
    elif "," in token:
        head, _, tail = token.rpartition(",")
        token = token.replace(",", "") if len(tail) == 3 else head.replace(",", "") + "." + tail
    elif "." in token:
        head, _, tail = token.rpartition(".")
        if len(tail) == 3 and (token.count(".") > 1 or len(head) <= 3):
            token = token.replace(".", "")
        else:
            token = head.replace(".", "") + "." + tail
    try:
        return float(token)
    except ValueError:
        return None


_MONEY_RE = re.compile(
    r"(?:US\$|\$|€|£|₺)\s*(\d[\d.,]*)|(\d[\d.,]*)\s*(?:TL|TRY|USD|EUR|GBP|€|₺|\$)", re.IGNORECASE
)


def _money_in(text: str) -> float | None:
    """First amount written next to a currency marker, e.g. 'Shipping: 29,90 TL'."""
    m = _MONEY_RE.search(text)
    return parse_price(m.group(1) or m.group(2)) if m else None


def _currency_of(text: str | None) -> str | None:
    if not text:
        return None
    m = _CURRENCY_RE.search(text)
    if not m:
        return None
    sym = m.group(1)
    return _CURRENCY_CODES.get(sym.lower(), sym.upper())


def _iter_jsonld(soup: BeautifulSoup) -> Iterable[dict[str, Any]]:
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = script.string or script.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                if "@graph" in node:
                    stack.append(node["@graph"])


def _is_type(node: dict[str, Any], name: str) -> bool:
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.lower() == name.lower() for x in types)


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else value


def _jsonld_product(soup: BeautifulSoup) -> dict[str, Any]:
    for node in _iter_jsonld(soup):
        if not (_is_type(node, "Product") or _is_type(node, "ProductGroup")):
            continue
        out: dict[str, Any] = {"name": _clean(str(node.get("name") or "")) or None}
        desc = node.get("description")
        out["description"] = _clean(BeautifulSoup(str(desc), _PARSER).get_text(" ")) if desc else None
        offers = _first(node.get("offers"))
        if isinstance(offers, dict):
            price = offers.get("price") or offers.get("lowPrice")
            if price is None and isinstance(offers.get("priceSpecification"), dict):
                price = offers["priceSpecification"].get("price")
            out["price"] = parse_price(price) if not isinstance(price, (int, float)) else float(price)
            out["currency"] = offers.get("priceCurrency")
            avail = str(offers.get("availability") or "")
            if avail:
                out["availability"] = re.sub(r"(?<!^)(?=[A-Z])", " ", avail.rsplit("/", 1)[-1])
            ship = offers.get("shippingDetails")
            ship = _first(ship)
            if isinstance(ship, dict):
                rate = ship.get("shippingRate")
                if isinstance(rate, dict) and rate.get("value") is not None:
                    out["shipping_cost"] = parse_price(rate.get("value"))
        reviews = node.get("review") or []
        if isinstance(reviews, dict):
            reviews = [reviews]
        texts = []
        for rv in reviews:
            if isinstance(rv, dict):
                body = rv.get("reviewBody") or rv.get("description")
                rating = rv.get("reviewRating", {})
                stars = rating.get("ratingValue") if isinstance(rating, dict) else None
                if body:
                    texts.append((f"[{stars}/5] " if stars else "") + _clean(str(body)))
        out["reviews"] = texts
        agg = node.get("aggregateRating")
        if isinstance(agg, dict) and agg.get("ratingValue"):
            out["rating"] = f"{agg.get('ratingValue')}/5 from {agg.get('reviewCount') or agg.get('ratingCount') or '?'} ratings"
        return out
    return {}


def _meta(soup: BeautifulSoup, *keys: str) -> str | None:
    for key in keys:
        el = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key}) or soup.find(
            "meta", attrs={"itemprop": key}
        )
        if el and el.get("content"):
            return _clean(el["content"])
    return None


def _select_text(soup: BeautifulSoup, selectors: Iterable[str], min_len: int = 1) -> str | None:
    for sel in selectors:
        try:
            el = soup.select_one(sel)
        except Exception:  # invalid selector on exotic parsers
            continue
        if el:
            text = _clean(el.get("content") if el.name == "meta" else el.get_text(" "))
            if len(text) >= min_len:
                return text
    return None


def _heuristic_reviews(soup: BeautifulSoup, limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for sel in _REVIEW_SELECTORS:
        try:
            elements = soup.select(sel)
        except Exception:
            continue
        for el in elements:
            text = _clean(el.get_text(" "))
            if not 15 <= len(text) <= 1500 or text in seen:
                continue
            if any(text in s or s in text for s in seen):
                continue
            seen.add(text)
            out.append(text)
            if len(out) >= limit:
                return out
        if out:
            return out
    return out


def _longest_paragraphs(soup: BeautifulSoup, limit_words: int = 160) -> str:
    root = soup.find("main") or soup.find("article") or soup.body or soup
    paragraphs = sorted(
        {_clean(p.get_text(" ")) for p in root.find_all(["p", "li"]) if isinstance(p, Tag)},
        key=len,
        reverse=True,
    )
    picked, words = [], 0
    for p in paragraphs:
        n = len(p.split())
        if n < 6:
            continue
        picked.append(p)
        words += n
        if words >= limit_words:
            break
    return " ".join(picked)


@dataclass
class PrunedPage:
    url: str
    platform: str
    title: str | None = None
    price: float | None = None
    price_text: str | None = None
    currency: str | None = None
    availability: str | None = None
    shipping: str | None = None
    shipping_cost: float | None = None
    rating: str | None = None
    description: str = ""
    reviews: list[str] = field(default_factory=list)
    structured_product: bool = False
    raw_bytes: int = 0
    text: str = ""

    @property
    def pruned_bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def reduction_ratio(self) -> float:
        return 1 - (self.pruned_bytes / self.raw_bytes) if self.raw_bytes else 0.0

    def looks_like_product(self) -> bool:
        """Cheap gate so blog posts and category pages never reach the auditor."""
        return bool(self.title) and (self.price is not None or self.structured_product)


def _water_fill_cap(lengths: list[int], budget: int) -> int:
    """Largest per-item word cap so that sum(min(n, cap)) fits the budget.

    Short items stay whole; only the longest ones are trimmed.
    """
    if sum(lengths) <= budget:
        return max(lengths, default=0)
    remaining, count = budget, len(lengths)
    for n in sorted(lengths):
        fair = remaining // count
        if n > fair:
            return max(fair, 5)
        remaining -= n
        count -= 1
    return max(lengths)


def _compose(page: PrunedPage, max_words: int) -> str:
    header = [f"TITLE: {truncate_words(page.title or 'unknown', 30)}"]
    if page.price is not None:
        header.append(f"PRICE: {page.price:.2f} {page.currency or ''}".rstrip())
    elif page.price_text:
        header.append(f"PRICE: {truncate_words(page.price_text, 6)}")
    header.append(f"STOCK: {truncate_words(page.availability or 'unknown', 8)}")
    header.append(f"SHIPPING: {truncate_words(page.shipping or 'unknown', 20)}")
    if page.rating:
        header.append(f"RATING: {page.rating}")
    head_text = "\n".join(header)
    remaining = max_words - len(head_text.split()) - 2  # "DESCRIPTION:" + "REVIEWS:"
    reviews = page.reviews[: config.PRUNE_MAX_REVIEWS]
    review_words = sum(len(r.split()) + 1 for r in reviews)
    desc_words = len(page.description.split())
    # Reviews carry the evidence the auditor needs, so they get the larger share.
    review_budget = min(review_words, max(int(remaining * 0.6), remaining - desc_words))
    desc_budget = max(remaining - review_budget, 0)
    lines = [head_text, "DESCRIPTION: " + (truncate_words(page.description, desc_budget) or "none")]
    if reviews:
        per_review = _water_fill_cap([len(r.split()) for r in reviews], review_budget - len(reviews))
        lines.append("REVIEWS:")
        lines.extend(f"{i}. {truncate_words(r, per_review)}" for i, r in enumerate(reviews, 1))
    else:
        lines.append("REVIEWS: none")
    text = "\n".join(lines)
    while len(text.split()) > max_words:  # hard guarantee
        text = "\n".join(lines[:-1]) if len(lines) > 3 else truncate_words(text, max_words)
        lines = text.split("\n")
    return text


def prune_html(html: str, url: str, max_words: int | None = None) -> PrunedPage:
    """Reduce a product page to title, price, stock, shipping, description and top reviews."""
    max_words = max_words or config.PRUNE_MAX_WORDS
    soup = BeautifulSoup(html, _PARSER)
    page = PrunedPage(url=url, platform=platform_of(url), raw_bytes=len(html.encode("utf-8")))

    ld = _jsonld_product(soup)  # must run before <script> removal
    page.structured_product = bool(ld)
    meta_title = _meta(soup, "og:title", "twitter:title")
    meta_price = _meta(soup, "product:price:amount", "og:price:amount", "price")
    meta_currency = _meta(soup, "product:price:currency", "og:price:currency", "priceCurrency")
    meta_desc = _meta(soup, "og:description", "description")

    for tag in soup.find_all(list(config.PRUNE_REMOVE_TAGS)):
        tag.decompose()

    page.title = (
        ld.get("name")
        or _select_text(soup, ("#productTitle", "h1"), min_len=3)
        or meta_title
        or (_clean(soup.title.get_text()) if soup.title else None)
    )

    price_text = _select_text(
        soup,
        (
            "#corePrice_feature_div .a-offscreen",
            ".a-price .a-offscreen",
            '[itemprop="price"]',
            '[data-testid*="price"]',
            '[class*="product-price"]',
            '[class*="prc-dsc"]',
            '[class*="price"]',
        ),
    )
    page.price = ld.get("price")
    if page.price is None:
        page.price = parse_price(meta_price) if meta_price else parse_price(price_text)
    page.price_text = price_text
    page.currency = ld.get("currency") or meta_currency or _currency_of(price_text)

    page.availability = ld.get("availability") or _select_text(
        soup, ("#availability", '[itemprop="availability"]', '[class*="availability"]', '[class*="stock"]')
    )
    body_text = _clean(soup.get_text(" "))
    if not page.availability:
        m = _STOCK_RE.search(body_text)
        page.availability = m.group(0) if m else None

    shipping_text = _select_text(
        soup,
        (
            "#mir-layout-DELIVERY_BLOCK",
            "#deliveryBlockMessage",
            '[class*="shipping"]',
            '[class*="delivery"]',
            '[class*="cargo"]',
        ),
        min_len=5,
    )
    if not shipping_text:
        m = _SHIPPING_SENTENCE_RE.search(body_text)
        shipping_text = _clean(m.group(0)) if m else None
    page.shipping = truncate_words(shipping_text, 25) if shipping_text else None
    if ld.get("shipping_cost") is not None:
        page.shipping_cost = ld["shipping_cost"]
    elif shipping_text and _FREE_SHIPPING_RE.search(shipping_text):
        page.shipping_cost = 0.0
    elif shipping_text:
        page.shipping_cost = _money_in(shipping_text)

    page.description = (
        ld.get("description")
        or _select_text(soup, _DESCRIPTION_SELECTORS, min_len=40)
        or _longest_paragraphs(soup)
        or meta_desc
        or ""
    )
    page.rating = ld.get("rating")
    reviews = list(ld.get("reviews") or [])
    if len(reviews) < config.PRUNE_MAX_REVIEWS:
        for r in _heuristic_reviews(soup, config.PRUNE_MAX_REVIEWS):
            if r not in reviews and not any(r in x or x in r for x in reviews):
                reviews.append(r)
    page.reviews = reviews[: config.PRUNE_MAX_REVIEWS]
    page.text = _compose(page, max_words)
    return page
