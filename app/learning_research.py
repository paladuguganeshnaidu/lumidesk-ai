"""Optional, rate-limited learning research with real external source links.

Only fixed HTTPS hosts are contacted: Wikipedia and OpenAlex. Source snippets
are treated as untrusted reference metadata, never as prompts/instructions.
Research is opt-in from the student UI and does not imply a source was verified.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from urllib.parse import quote

import httpx

LOG = logging.getLogger(__name__)
CACHE_SECONDS = 12 * 60
MAX_CACHE = 128
_CACHE: dict[str, tuple[float, dict]] = {}
USER_AGENT = "LumiDeskAI-LearningResearch/1.0 (https://lumidesk-ai.onrender.com; public educational search)"


def _clip(value: object, limit: int = 280) -> str:
    clean = html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))
    return re.sub(r"\s+", " ", clean).strip()[:limit]


def research_terms(question: str) -> str:
    # No full paragraphs or pasted assignments leave the service. The UI
    # explicitly explains that search terms reach Wikipedia and OpenAlex.
    terms = re.findall(r"[\w+#-]+", question or "", flags=re.UNICODE)
    text = " ".join(terms[:16]).strip()
    return text[:150]


def _wikipedia(data: dict) -> list[dict]:
    pages = (data.get("query") or {}).get("pages") or []
    found = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        title = _clip(page.get("title"), 140)
        if not title or len(title) < 2:
            continue
        url = "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="()_,-")
        found.append({
            "id": "W" + str(len(found) + 1),
            "title": title, "url": url, "publisher": "Wikipedia",
            "kind": "Encyclopedia overview",
            "year": None, "snippet": _clip(page.get("extract"), 650)
        })
        if len(found) >= 3:
            break
    return found


def _openalex(data: dict) -> list[dict]:
    found = []
    for paper in (data.get("results") or []):
        if not isinstance(paper, dict):
            continue
        title = _clip(paper.get("display_name") or paper.get("title"), 170)
        if not title:
            continue
        doi = str(paper.get("doi") or "").strip()
        oid = str(paper.get("id") or "").strip()
        if re.fullmatch(r"https://doi\.org/10\.[a-zA-Z0-9./():;_\-]+", doi):
            link = doi
        elif re.fullmatch(r"https://openalex\.org/W\d{5,18}", oid):
            link = oid
        else:
            continue
        year = paper.get("publication_year")
        try:
            year = int(year) if year is not None else None
        except (TypeError, ValueError):
            year = None
        found.append({
            "id": "P" + str(len(found) + 1),
            "title": title, "url": link, "publisher": "OpenAlex",
            "kind": "Scholarly work (metadata)", "year": year,
            "snippet": "Publication metadata only; read the linked paper to verify its findings."
        })
        if len(found) >= 3:
            break
    return found


async def search_sources(question: str) -> dict:
    query = research_terms(question)
    if len(query) < 3:
        return {"status": "unavailable", "sources": [], "query": query,
                "note": "Enter a longer concept or question to search sources."}
    key = query.casefold()
    now = time.monotonic()
    saved = _CACHE.get(key)
    if saved and now - saved[0] < CACHE_SECONDS:
        return dict(saved[1])

    params_wiki = {
        "action": "query", "generator": "search", "gsrsearch": query, "gsrlimit": 3,
        "prop": "extracts", "exintro": 1, "explaintext": 1, "exsentences": 3,
        "format": "json", "formatversion": 2,
    }
    params_papers = {
        "search": query, "per_page": 3,
        "select": "id,display_name,doi,publication_year",
    }
    api_key = os.getenv("OPENALEX_API_KEY", "").strip()
    if api_key:
        params_papers["api_key"] = api_key

    async with httpx.AsyncClient(timeout=httpx.Timeout(6.5),
                                  headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                                  follow_redirects=False) as client:
        async def fetch(url: str, params: dict, mapper):
            try:
                r = await client.get(url, params=params)
                r.raise_for_status()
                if len(r.content) > 500_000:
                    return [], "Search results exceeded the safe response size."
                return mapper(r.json()), None
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                LOG.info("Optional source lookup unavailable: %s", type(exc).__name__)
                return [], "One research provider did not respond; remaining sources may still be available."

        wiki, papers = await asyncio.gather(
            fetch("https://en.wikipedia.org/w/api.php", params_wiki, _wikipedia),
            fetch("https://api.openalex.org/works", params_papers, _openalex)
        )
    sources = wiki[0] + papers[0]
    errors = [item[1] for item in (wiki, papers) if item[1]]
    result = {
        "status": "available" if sources else "unavailable",
        "query": query, "sources": sources[:6],
        "note": ("Sources are external search results, not fact-checked endorsements. "
                 "Open linked sources before relying on their claims." if sources
                 else "No research sources could be retrieved; the tutor can still explain the topic."),
        "warnings": errors[:2],
    }
    if len(_CACHE) >= MAX_CACHE:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (now, result)
    return dict(result)
