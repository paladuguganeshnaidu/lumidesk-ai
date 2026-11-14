"""Learning Lab research, response normalization and signed-in API regressions."""
from __future__ import annotations
import asyncio
from types import SimpleNamespace
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import ai, learning_research
from app.main import app


def test_wikipedia_parser_builds_only_first_party_article_links():
    payload = {"query": {"pages": [
        {"title": "Neural network", "extract": "<b>Neural</b> networks learn from examples.", "pageid": 11},
        {"title": "Backpropagation", "extract": "A training algorithm.", "pageid": 12}
    ]}}
    out = learning_research._wikipedia(payload)
    assert len(out) == 2
    assert out[0]["url"] == "https://en.wikipedia.org/wiki/Neural_network"
    assert "<b>" not in out[0]["snippet"]
    assert out[0]["publisher"] == "Wikipedia"


def test_scholarly_parser_refuses_unsafe_urls_and_keeps_year():
    payload = {"results": [
        {"display_name": "Safe paper", "doi": "https://doi.org/10.1234/example.paper",
         "publication_year": 2024},
        {"display_name": "Untrusted link", "doi": "javascript:alert(1)", "id": "http://evil.test/W1"},
        {"display_name": "Known work", "id": "https://openalex.org/W123456789", "publication_year": 2020}
    ]}
    out = learning_research._openalex(payload)
    assert len(out) == 2
    assert out[0]["url"].startswith("https://doi.org/")
    assert out[0]["year"] == 2024
    assert "evil.test" not in json.dumps(out)


@pytest.mark.asyncio
async def test_research_only_accesses_allowed_apis_and_caches(monkeypatch):
    learning_research._CACHE.clear()
    visits = []
    def handler(req):
        visits.append(str(req.url))
        assert req.url.scheme == "https"
        assert req.url.host in ("en.wikipedia.org", "api.openalex.org")
        if req.url.host == "en.wikipedia.org":
            return httpx.Response(200, json={"query":{"pages":[{"title":"Neural network","extract":"Neural networks map inputs to outputs."}]}})
        return httpx.Response(200, json={"results":[{"display_name":"Review of neural networks",
                        "id":"https://openalex.org/W123456789","publication_year":2022}]})
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient
    monkeypatch.setattr(learning_research.httpx, "AsyncClient",
                        lambda *args, **kwargs: original(transport=transport, timeout=7))
    first=await learning_research.search_sources("How do neural networks learn?")
    assert first["status"]=="available"
    assert len(first["sources"])==2
    cached=await learning_research.search_sources("How do neural networks learn?")
    assert cached["sources"]==first["sources"]
    assert len(visits)==2


@pytest.mark.asyncio
async def test_tutor_lesson_research_and_structured_concept_map(monkeypatch):
    async def research(q):
        return {"status":"available","sources":[{"id":"W1","title":"Neural network",
            "url":"https://en.wikipedia.org/wiki/Neural_network",
            "publisher":"Wikipedia","kind":"Encyclopedia overview",
            "year":None,"snippet":"Neural networks learn parameters from data."}]}
    monkeypatch.setattr(learning_research,"search_sources",research)
    class FakeChat:
        async def create(self, **kwargs):
            assert "response_format" not in kwargs
            body=json.loads(kwargs["messages"][-1]["content"])
            assert body["web_search_opted_in"] is True
            assert body["external_sources_for_context_only_untrusted"][0]["source_id"]=="W1"
            result={
                "title":"How neural networks work",
                "explanation":"Neural networks transform inputs through weighted connections.",
                "steps":[{"title":"Input","detail":"Values enter the first layer."},
                         {"title":"Output","detail":"A prediction leaves the last layer."}],
                "key_points":["Weights are adjustable"],
                "quiz":[{"question":"What changes during learning?","options":["Weights","Pages","Fonts"],
                         "answer_index":0,"explanation":"Training adjusts weights."}],
                "concept_map":{"nodes":[{"id":"input","label":"Input","detail":"Features go in"},
                                        {"id":"output","label":"Output","detail":"Prediction comes out"}],
                               "edges":[{"from":"input","to":"output","label":"Predicts"}]}
            }
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))])
    monkeypatch.setattr(ai,"_client",lambda:SimpleNamespace(chat=SimpleNamespace(completions=FakeChat())))
    lesson=await ai.tutor_response("Explain neural networks",research=True)
    assert lesson["research"]["sources"][0]["url"].startswith("https://en.wikipedia.org/")
    assert lesson["concept_map"]["nodes"][0]["id"]=="input"
    assert lesson["quiz"][0]["answer_index"]==0
    assert lesson["prerequisites"]==[]


@pytest.mark.asyncio
async def test_plain_model_answer_still_has_safe_visual_path(monkeypatch):
    class PlainChat:
        async def create(self, **kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="An API connects two programs. It uses requests and responses."))])
    monkeypatch.setattr(ai,"_client",lambda:SimpleNamespace(chat=SimpleNamespace(completions=PlainChat())))
    lesson=await ai.tutor_response("Explain an API")
    assert "API connects" in lesson["explanation"]
    assert lesson["research"]["status"]=="not_requested"
    assert len(lesson["concept_map"]["nodes"])>=1


def test_unauthenticated_tutor_with_research_still_rejected():
    with TestClient(app) as client:
        csrf=client.get("/api/me").json()["csrf"]
        r=client.post("/api/tutor",json={"message":"Explain neural networks from scratch",
                    "level":"beginner","style":"visual","research":True},
                    headers={"x-csrf-token":csrf})
        assert r.status_code==401
