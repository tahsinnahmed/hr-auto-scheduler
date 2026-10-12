"""
urgency.py — Classifies the urgency of an emergency leave request.

Two modes:
  • Rule-based (default):  Fast, free, deterministic. Covers ~85% of cases.
  • Groq LLM (optional):   Falls back to Groq if GROQ_API_KEY is set and
                           the rules can't confidently classify.

Returns a tuple: (urgency, source, rationale)
  urgency:   "LOW" | "MEDIUM" | "HIGH"
  source:    "rules" | "groq" | "default"
  rationale: Short human-readable reason
"""
from __future__ import annotations

import logging
import os
from typing import Tuple

import httpx

log = logging.getLogger("shift.urgency")

# Groq uses the same env var name as OpenAI for compatibility.
# Set GROQ_API_KEY in .env to enable the LLM fallback.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

# Groq's OpenAI-compatible endpoint
GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.1-8b-instant"   # fast, free, great for single-word classification


# --------------------------------------------------------------------------- #
# Keyword rules
# --------------------------------------------------------------------------- #
HIGH_KEYWORDS = [
    "accident", "hospital", "emergency", "urgent", "surgery",
    "died", "death", "passed away", "critical", "icu",
    "chest pain", "heart", "stroke", "bleeding",
    "fire", "flood", "evacuat", "police",
]

MEDIUM_KEYWORDS = [
    "sick", "fever", "flu", "unwell", "migraine", "food poisoning",
    "injury", "sprain", "fracture", "doctor", "clinic",
    "family issue", "childcare", "school", "hospital visit",
]

LOW_KEYWORDS = [
    "personal", "errand", "appointment", "traffic", "car trouble",
    "utility", "moving", "vacation", "planned",
]

HIGH_LEAVE_TYPES   = {"emergency", "bereavement", "medical emergency"}
MEDIUM_LEAVE_TYPES = {"sick", "medical", "casual"}


def _match_keywords(text: str, keywords: list) -> list:
    t = text.lower()
    return [kw for kw in keywords if kw in t]


def _classify_by_rules(leave_type: str, reason: str) -> Tuple[str, str] | None:
    leave_lower  = (leave_type or "").lower().strip()
    reason_lower = (reason or "").lower().strip()
    combined     = f"{leave_lower} {reason_lower}"

    high_hits   = _match_keywords(combined, HIGH_KEYWORDS)
    medium_hits = _match_keywords(combined, MEDIUM_KEYWORDS)
    low_hits    = _match_keywords(combined, LOW_KEYWORDS)

    if leave_lower in HIGH_LEAVE_TYPES:
        return "HIGH", f"Leave type '{leave_type}' indicates high urgency"
    if leave_lower in MEDIUM_LEAVE_TYPES and not high_hits:
        return "MEDIUM", f"Leave type '{leave_type}'"

    if high_hits:
        return "HIGH", f"High-urgency keywords: {', '.join(high_hits[:3])}"
    if medium_hits:
        return "MEDIUM", f"Medium-urgency keywords: {', '.join(medium_hits[:3])}"
    if low_hits:
        return "LOW", f"Low-urgency keywords: {', '.join(low_hits[:3])}"

    return None


# --------------------------------------------------------------------------- #
# Groq LLM fallback
# --------------------------------------------------------------------------- #
def _classify_by_groq(leave_type: str, reason: str) -> Tuple[str, str] | None:
    if not GROQ_API_KEY:
        return None

    prompt = f"""Classify the urgency of this emergency leave request as exactly one word: HIGH, MEDIUM, or LOW.

Leave type: {leave_type}
Reason: {reason}

Rules:
- HIGH: life-threatening, medical emergency, bereavement, or anything requiring immediate response
- MEDIUM: illness, injury, or family issue that prevents work today
- LOW: personal errands, transport issues, or planned absences

Answer with ONE word only."""

    try:
        resp = httpx.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "model":       GROQ_MODEL,
                "messages":    [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens":  5,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip().upper()
        if answer in ("HIGH", "MEDIUM", "LOW"):
            return answer, f"Groq classified as {answer}"
    except Exception:
        log.exception("Groq urgency classification failed")

    return None


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def classify_urgency(leave_type: str, reason: str) -> Tuple[str, str, str]:
    """Returns (urgency, source, rationale). Never raises."""
    rule_result = _classify_by_rules(leave_type or "", reason or "")
    if rule_result:
        urgency, rationale = rule_result
        return urgency, "rules", rationale

    groq_result = _classify_by_groq(leave_type or "", reason or "")
    if groq_result:
        urgency, rationale = groq_result
        return urgency, "groq", rationale

    return "MEDIUM", "default", "No keywords matched; defaulting to MEDIUM"