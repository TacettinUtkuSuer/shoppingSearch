import json

import pytest

from core import agents
from core.scraper import prune_html
from conftest import fixture_text
from llm_adapter import LLMError


def test_resolver_clarification_path():
    raw = 'Here:\n```json\n{"needs_clarification": true, "clarification_question": "Kitchen gallon pump or well pump?", "search_terms": ["x"], "negative_keywords": ["y"], "detected_category": null}\n```'
    r = agents.resolve("water thingy for bottle", runner=lambda p: raw)
    assert r.needs_clarification and r.search_terms == [] and r.negative_keywords == []


def test_resolver_scope_is_normalized():
    out = {
        "needs_clarification": False, "clarification_question": "ignored",
        "search_terms": ["  automatic  water dispenser 19l", "type-c gallon pump", "Type-C gallon pump", "4th", "5th"],
        "negative_keywords": ["-Hydrophore", "aquarium", "aquarium", " "],
        "detected_category": "Home & Kitchen",
    }
    r = agents.resolve("pump", runner=lambda p: json.dumps(out))
    assert r.search_terms == ["automatic water dispenser 19l", "type-c gallon pump", "4th"]
    assert r.negative_keywords == ["hydrophore", "aquarium"]
    assert r.detected_category == "home_kitchen"
    assert r.clarification_question is None


def test_resolver_inconsistent_output_is_retried_then_fails():
    calls = []

    def runner(p):
        calls.append(p)
        return '{"needs_clarification": false, "search_terms": []}'

    with pytest.raises(LLMError):
        agents.resolve("pump", runner=runner)
    assert len(calls) == 2


def test_resolver_prompt_forces_decision_after_max_rounds():
    prompt = agents.build_resolver_prompt(
        "pump", [{"question": "a?", "answer": "kitchen"}, {"question": "b?", "answer": "19l"}]
    )
    assert '"clarification_rounds_remaining": 0' in prompt
    assert "MUST follow rule 4" in prompt


def test_auditor_computes_unit_price_in_python():
    page = prune_html(fixture_text("product_page.html"), "https://www.shop-a.com/pump-123")
    seen = {}

    def runner(prompt):
        seen["prompt"] = prompt
        return 'Audit done. {"units_in_pack": "2", "normalized_unit_price": 999, "in_stock": "true", "contradictions": "Seller states Type-C, but reviews report Micro-USB.", "trust_score": 142.6, "dynamic_attributes": {"Battery Capacity": "1200mAh", "port": null}}'

    r = agents.audit(page, runner=runner)
    assert r.units_in_pack == 2
    assert r.normalized_unit_price == 249.50  # (499 + 0) / 2, not the model's 999
    assert r.in_stock is True
    assert r.trust_score == 100
    assert r.contradictions == ["Seller states Type-C, but reviews report Micro-USB."]
    assert r.dynamic_attributes == {"battery_capacity": "1200mAh"}
    assert "parsed_price: 499.0" in seen["prompt"]
    assert len(seen["prompt"].split()) < 900  # isolated, minimal context
