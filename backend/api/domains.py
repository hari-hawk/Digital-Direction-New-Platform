"""Domain pack discovery + detection API (Level B, commit 1).

Two endpoints:
  GET /api/domains         — list every registered pack with its metadata
  POST /api/domains/detect — score a sample of document text against every
                              pack, return the winner

This is purely *informational* in this commit — it lets us verify on prod
that the pack registry is live, and lets future UI work (e.g. a "domain"
badge on the Upload page) read what's available. The actual extraction
pipeline still routes everything through the telecom path; routing
through the picked pack is a follow-up commit, gated on the customer's
first non-telecom upload.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend.services.domain_packs import (
    all_packs,
    detect_domain,
    get_pack,
    pattern_detectors_for,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/domains", tags=["domains"])


class DetectRequest(BaseModel):
    text: str


@router.get("")
async def list_domains():
    """Return metadata for every registered domain pack. Used by the UI to
    render a domain selector and to surface "what industries does the
    platform support today?" without inspecting backend code."""
    packs = []
    for p in all_packs():
        packs.append({
            "key": p.key,
            "display": p.display,
            "field_count": len(p.field_set),
            "field_set": list(p.field_set),
            "prompt_namespace": p.prompt_namespace,
            "default_doc_types": list(p.default_doc_types),
            "content_signal_count": len(p.content_signals),
            "pattern_detector_count": len(pattern_detectors_for(p.key)),
        })
    return {"packs": packs, "count": len(packs)}


@router.get("/{key}")
async def get_domain(key: str):
    """Return one pack's full metadata, including the raw content_signals
    (useful for debugging detection misses)."""
    pack = get_pack(key)
    if not pack:
        return {"error": "not_found", "key": key}
    return {
        "key": pack.key,
        "display": pack.display,
        "field_set": list(pack.field_set),
        "prompt_namespace": pack.prompt_namespace,
        "default_doc_types": list(pack.default_doc_types),
        "content_signals": list(pack.content_signals),
        "pattern_detectors": [
            getattr(fn, "__name__", str(fn))
            for fn in pattern_detectors_for(pack.key)
        ],
    }


@router.post("/detect")
async def detect(req: DetectRequest):
    """Score every registered pack against the supplied text and return the
    winner + the full per-pack score breakdown. Lets us verify detection on
    prod by pasting a real document's first page text."""
    text = (req.text or "")[:5000]  # cap so we don't pay to embed 100-page books
    winner = detect_domain(text)
    breakdown = []
    for p in all_packs():
        breakdown.append({
            "key": p.key,
            "hits": p.matches_text(text),
            "signal_count": len(p.content_signals),
        })
    return {
        "winner": {
            "key": winner[0].key,
            "display": winner[0].display,
            "hits": winner[1],
        } if winner else None,
        "breakdown": breakdown,
        "text_length": len(text),
    }
