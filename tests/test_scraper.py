import re

import pytest

from conftest import fixture_text
from core import scraper
from core.scraper import (
    RateLimitError,
    Scraper,
    build_search_query,
    canonicalize_url,
    parse_price,
    prune_html,
)


def test_prune_reduces_size_over_90_percent_and_keeps_reviews():
    html = fixture_text("product_page.html")
    page = prune_html(html, "https://www.shop-a.com/pump-123")
    assert page.reduction_ratio > 0.90
    assert page.word_count <= 300
    assert page.title.startswith("AquaPro Automatic Rechargeable")
    assert page.price == 499.0
    assert page.currency == "TRY"
    assert page.availability == "In Stock"
    assert page.shipping_cost == 0.0
    assert len(page.reviews) == 5
    assert "Seventh review" not in page.text
    # Reviews fit the budget here, so every one survives pruning verbatim.
    for review in page.reviews:
        assert review in page.text
    assert "Micro-USB" in page.text and "chrome painted plastic" in page.text
    for junk in ("window.__STATE__", "color:red", "Category 12", "Copyright", "Enable JS"):
        assert junk not in page.text
    assert page.looks_like_product()


def test_prune_without_structured_data_uses_heuristics():
    html = """<html><body><nav>menu</nav><h1>Steel Hose Clamp 10 pcs</h1>
    <span itemprop="price" content="12.99">$12.99</span><div id="availability">Only 3 left in stock</div>
    <div id="deliveryBlockMessage">Shipping: $4.50 to your address</div>
    <div id="feature-bullets">Stainless steel clamps, 10 pieces, adjustable 8 to 12 mm diameter range for hoses.</div>
    <div data-hook="review-body">Clamps rusted after a month, not stainless.</div>
    <div data-hook="review-body">Good grip, fits my garden hose.</div>
    </body></html>"""
    page = prune_html(html, "https://amazon.com/dp/X")
    assert page.title == "Steel Hose Clamp 10 pcs"
    assert page.price == 12.99
    assert page.shipping_cost == 4.50
    assert "Only 3 left in stock" in page.text
    assert "rusted after a month" in page.text
    assert page.platform == "amazon.com"


def test_non_product_page_is_rejected_before_llm():
    page = prune_html("<html><body><h1>10 best pumps of 2026</h1><p>Blog text.</p></body></html>", "https://blog.example/x")
    assert not page.looks_like_product()


@pytest.mark.parametrize(
    "raw,expected",
    [("1.249,50 TL", 1249.50), ("$1,249.50", 1249.50), ("249", 249.0), ("₺ 499,00", 499.0),
     ("1.299 TL", 1299.0), ("12.99", 12.99), ("1 299,00 €", 1299.0), (None, None), ("free", None)],
)
def test_parse_price(raw, expected):
    assert parse_price(raw) == expected


def test_canonicalize_url_strips_tracking_and_fragment():
    assert canonicalize_url("https://WWW.Shop.com/p/1?utm_source=x&id=5#reviews") == "https://www.shop.com/p/1?id=5"


def test_build_search_query_adds_negative_operators():
    assert build_search_query("water pump", ["aquarium", "-bilge", "12v car"]) == 'water pump -aquarium -bilge -"12v car"'


class FakeResponse:
    def __init__(self, status=200, text="", url="https://x", ctype="text/html"):
        self.status_code, self.text, self.url = status, text, url
        self.headers = {"content-type": ctype}


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.responses.pop(0)

    def post(self, url, data=None, **kw):
        self.calls.append(("POST", url, data))
        return self.responses.pop(0)


def test_bot_block_triggers_jittered_backoff_then_succeeds():
    session = FakeSession([
        FakeResponse(200, "<form action='/errors/validateCaptcha'>"),
        FakeResponse(503, "Service Unavailable"),
        FakeResponse(200, "<html>ok</html>", url="https://shop/p"),
    ])
    sleeps = []
    s = Scraper(session=session, sleep=sleeps.append)
    result = s.fetch("https://shop/p")
    assert result.html == "<html>ok</html>"
    assert len(sleeps) == 2 and all(5 <= d <= 12 for d in sleeps)
    headers = session.calls[0][2]["headers"]
    assert headers["Sec-Fetch-Dest"] == "document" and "Accept-Language" in headers


def test_bot_block_exhausts_retries():
    session = FakeSession([FakeResponse(200, "<title>Robot Check</title>")] * 10)
    s = Scraper(session=session, sleep=lambda d: None)
    with pytest.raises(RateLimitError):
        s.fetch("https://shop/p")
    assert len(session.calls) == 1 + scraper.config.HTTP_MAX_RETRIES


def test_permanent_errors_are_not_retried():
    session = FakeSession([FakeResponse(404, "nope")])
    with pytest.raises(scraper.ScrapeError) as exc:
        Scraper(session=session, sleep=lambda d: None).fetch("https://shop/p")
    assert not isinstance(exc.value, RateLimitError)


def test_duckduckgo_search_parses_filters_and_dedupes():
    session = FakeSession([FakeResponse(200, fixture_text("ddg_results.html"))])
    results = Scraper(session=session, sleep=lambda d: None).search("gallon pump", ["aquarium", "submersible"])
    urls = [r.url for r in results]
    assert urls == ["https://www.shop-a.com/pump-123", "https://shop-c.com/item/77"]
    method, endpoint, data = session.calls[0]
    assert method == "POST" and "duckduckgo" in endpoint
    assert data["q"] == "gallon pump -aquarium -submersible"


def test_scraper_uses_curl_cffi_chrome_impersonation():
    session = Scraper._new_session()
    assert type(session).__module__.startswith("curl_cffi")
    source = open(scraper.__file__, encoding="utf-8").read()
    assert not re.search(r"^\s*(import requests|from requests|import urllib|from urllib\.request)", source, re.M)


def test_long_reviews_are_trimmed_fairly_within_word_budget():
    long_review = "This pump " + "really " * 150 + "broke."
    reviews = "".join(f'<div data-hook="review-body">{t}</div>' for t in
                      ["Short honest review, works.", long_review, long_review + " again"])
    html = f"<html><body><h1>Pump</h1><span itemprop='price' content='10'>10</span>" \
           f"<div id='feature-bullets'>{'Great pump. ' * 200}</div>{reviews}</body></html>"
    page = prune_html(html, "https://shop/p")
    assert page.word_count <= 300
    assert "Short honest review, works." in page.text
    assert page.text.count("really") > 40  # long reviews keep a fair share, not 5 words
