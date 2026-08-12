"""Claude Haiku + priority rules for Digilatics client assignment."""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache
from typing import Optional

log = logging.getLogger("claude_match")

ENABLE_CLAUDE_MATCH = os.getenv("ENABLE_CLAUDE_MATCH", "true").lower() == "true"
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_MIN_CONFIDENCE = float(os.getenv("CLAUDE_MIN_CONFIDENCE", "0.7"))

TEAM_TOKENS = (
    r"ads|seo|web|content|csm|bd|growth|design|hr|finance|ops|operations|"
    r"it|automation|lead\s*insight|marketing|sales|social(?:\s+media)?"
)

# Cheap deterministic fixes (title → Digilatics sub-brand) — run FIRST.
TITLE_BRAND_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bhr\s+activity\b", re.I), "Digilatics HR and Finance"),
    (re.compile(r"\bweb\s+case\s+stud", re.I), "Digilatics Web"),
    (re.compile(r"\bweb\s+team\b", re.I), "Digilatics Web"),
    (re.compile(r"\bseo\s+team\b", re.I), "Digilatics SEO"),
    (re.compile(r"\bads?\s+team\b", re.I), "Digilatics Ads"),
    (re.compile(r"\bcontent\s+team\b", re.I), "Digilatics Content"),
    (re.compile(r"\bcs[m]?\s+team\b|\bcustomer\s+success\b", re.I), "Digilatics CSM"),
    (re.compile(r"\bbd\s+team\b|\bgrowth\s+team\b|\bsales\s+team\b", re.I), "Digilatics BD"),
    (re.compile(r"\bdesign\s+team\b|\bsocial\s+media\s+team\b", re.I), "Digilatics Design"),
    (re.compile(r"\bhr\s+team\b", re.I), "Digilatics HR and Finance"),
    (re.compile(r"\bops?\s+team\b|\boperations\s+team\b", re.I), "Digilatics Operation"),
    (re.compile(r"\bit\s+team\b|\bautomation\s+team\b", re.I), "Digilatics IT"),
]

# "Ads x Lead Insight x CSM Process" style inter-department internals
INTERDEPT_RE = re.compile(
    rf"\b({TEAM_TOKENS})\b.*\bx\b.*\b({TEAM_TOKENS})\b",
    re.I,
)
INTERNAL_HINT_RE = re.compile(
    r"\b(sync\s*up|standup|stand-up|retro|retrospective|all[\s-]?hands|"
    r"1\s*:\s*1|one[\s-]?on[\s-]?one|internal|hr\s+activity|"
    r"case\s+stud|weekly\s+sync|team\s+sync|process)\b",
    re.I,
)

DIGILATICS_BRANDS = (
    "Digilatics",
    "Digilatics Web",
    "Digilatics SEO",
    "Digilatics Ads",
    "Digilatics Content",
    "Digilatics CSM",
    "Digilatics BD",
    "Digilatics Design",
    "Digilatics HR and Finance",
    "Digilatics Operation",
    "Digilatics IT",
    "Digilatics LeadInsight",
    "Digilatics Marketing",
    "Digilatics Sales",
)

SYSTEM_PROMPT = """You assign Digilatics time entries to the correct CLIENT from an allowed list.

You are fixing time-tracking client attribution. Be precise.

HARD RULES (in order):
1. Pick EXACTLY one client from allowed_clients. Never invent a name.
2. HR Activity / HR-related company events → "Digilatics HR and Finance"
   (NOT the employee's personal fallback, NOT Digilatics Marketing).
3. Titles about Web work (Web Case Studies, Web Team, Web sync) → "Digilatics Web"
   even if attendees are from Content/Ads/etc.
4. Inter-department INTERNAL meetings (titles like "Ads x Lead Insight x CSM Process",
   "SEO x Content", team x team) are COMPANY-INTERNAL, not billable to an external client.
   → assign EACH attendee's row to THAT person's Digilatics team brand = user_fallback
   (e.g. Muhammad Umer Ibrahim / Ads → Digilatics Ads; CSM person → Digilatics CSM).
   Do NOT pick an external client just because a word in the title overlaps a client name
   (e.g. "Lead Insight" must NOT become "Insight Home Inspections").
5. True external client meetings: client name is clearly the subject of the meeting
   (e.g. "GreenWorks Location page Layout Discussion") → that external client.
6. Generic internal meeting with no team named in title → user_fallback.
7. Never use Digilatics Marketing as a dumping ground when a better Digilatics sub-brand fits.

Examples:
- "HR Activity - Digilatics" + any user → Digilatics HR and Finance
- "Web Case Studies Discussion" + Content writer → Digilatics Web
- "Web Team Sync Up" + Web member → Digilatics Web
- "Ads x Lead Insight x CSM Process" + umar@ (Ads) → Digilatics Ads
- "Ads x Lead Insight x CSM Process" + haseeb@ (CSM) → Digilatics CSM
- "GreenWorks Location page Layout Discussion" → GreenWorks Inspections & Engineering

Return ONLY compact JSON:
{"client":"<exact allowed name>","confidence":0.0-1.0,"reason":"<short>"}
"""


def title_brand_rule(title: str) -> Optional[str]:
    if not title:
        return None
    for pat, brand in TITLE_BRAND_RULES:
        if pat.search(title):
            return brand
    return None


def is_interdepartment_meeting(title: str) -> bool:
    return bool(title and INTERDEPT_RE.search(title))


def looks_internal_meeting(title: str) -> bool:
    if not title:
        return False
    if is_interdepartment_meeting(title):
        return True
    if title_brand_rule(title):
        return True
    return bool(INTERNAL_HINT_RE.search(title))


def priority_override(title: str, user_fallback: str) -> Optional[dict]:
    """Deterministic overrides that beat exact/fuzzy client matches."""
    brand = title_brand_rule(title)
    if brand:
        return {"client": brand, "via": "title_brand_rule"}
    if is_interdepartment_meeting(title):
        return {
            "client": user_fallback or "Digilatics",
            "via": "interdept_user_fallback",
        }
    return None


def _normalize_allowed(allowed_clients: list[str]) -> list[str]:
    out: list[str] = []
    seen = set()
    for c in allowed_clients:
        name = (c or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


@lru_cache(maxsize=1)
def _client():
    from anthropic import Anthropic

    return Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


def claude_pick_client(
    *,
    title: str,
    allowed_clients: list[str],
    user_fallback: str = "Digilatics",
    user_team: str = "",
    user_email: str = "",
    attendee_emails: Optional[list[str]] = None,
    description: str = "",
    source: str = "meeting",
    heuristic_hint: str = "",
) -> Optional[dict]:
    if not ENABLE_CLAUDE_MATCH:
        return None
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY missing; skipping Claude match")
        return None

    allowed = _normalize_allowed(list(allowed_clients) + list(DIGILATICS_BRANDS))
    if user_fallback and user_fallback not in {a for a in allowed}:
        allowed.append(user_fallback)
    allowed_set = {a.lower(): a for a in allowed}

    payload = {
        "title": title or "",
        "description": (description or "")[:500],
        "source": source,
        "user_fallback": user_fallback,
        "user_team": user_team,
        "user_email": user_email,
        "attendee_emails": (attendee_emails or [])[:30],
        "heuristic_hint": heuristic_hint,
        "allowed_clients": allowed[:400],
    }

    try:
        resp = _client().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=220,
            temperature=0,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": "Classify this time entry:\n" + json.dumps(payload, ensure_ascii=False),
                }
            ],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            log.warning("Claude returned non-JSON: %s", text[:200])
            return None
        data = json.loads(m.group(0))
        client = str(data.get("client") or "").strip()
        conf = float(data.get("confidence") or 0)
        reason = str(data.get("reason") or "")
        if not client:
            return None
        canon = allowed_set.get(client.lower())
        if not canon:
            log.info("Claude picked non-allowed client %r — ignored", client)
            return None
        if conf < CLAUDE_MIN_CONFIDENCE:
            return {
                "client": user_fallback or "Digilatics",
                "confidence": conf,
                "reason": reason,
                "via": "claude_low_conf",
                "claude_client": canon,
            }
        return {
            "client": canon,
            "confidence": conf,
            "reason": reason,
            "via": "claude_haiku",
        }
    except Exception as e:
        log.exception("Claude match failed: %s", e)
        return None


def refine_match(
    result: dict,
    *,
    title: str,
    allowed_clients: list[str],
    user_fallback: str = "Digilatics",
    user_team: str = "",
    user_email: str = "",
    attendee_emails: Optional[list[str]] = None,
    description: str = "",
    source: str = "meeting",
) -> dict:
    """
    Apply priority overrides, then Claude for weak / internal-looking cases.
    Can override a false title_exact (e.g. Insight Home vs Lead Insight interdept).
    """
    result = dict(result or {})
    via = result.get("via") or ""

    # 1) Deterministic priority (beats exact match)
    ov = priority_override(title, user_fallback)
    if ov:
        out = dict(result)
        out["client"] = ov["client"]
        out["via"] = ov["via"]
        out["prev_via"] = via
        out["prev_client"] = result.get("client")
        return out

    weak = via in {"fallback", "internal_fallback", "digilatics_override"}
    # Re-ask Claude when an "exact" hit looks like an internal/interdept false positive
    suspicious_exact = via in {"title_exact", "title_starts_with", "legacy_split", "title_fuzzy"} and looks_internal_meeting(
        title
    )
    if not (weak or suspicious_exact):
        return result

    if not ENABLE_CLAUDE_MATCH:
        return result

    hint = ""
    if suspicious_exact:
        hint = (
            f"Previous heuristic said {result.get('client')} via {via}, but title looks "
            "internal/inter-department — prefer Digilatics sub-brand / user_fallback over "
            "external client name collision."
        )
    elif weak:
        hint = f"Previous heuristic was weak ({via} → {result.get('client')})."

    picked = claude_pick_client(
        title=title,
        allowed_clients=allowed_clients,
        user_fallback=user_fallback,
        user_team=user_team,
        user_email=user_email,
        attendee_emails=attendee_emails,
        description=description,
        source=source,
        heuristic_hint=hint,
    )
    if not picked:
        return result
    if picked.get("via") == "claude_low_conf":
        out = dict(result)
        out["claude_client"] = picked.get("claude_client")
        out["claude_confidence"] = picked.get("confidence")
        out["claude_reason"] = picked.get("reason")
        return out

    out = dict(result)
    out["client"] = picked["client"]
    out["via"] = picked["via"]
    out["confidence"] = picked.get("confidence")
    out["reason"] = picked.get("reason")
    out["prev_via"] = via
    out["prev_client"] = result.get("client")
    return out


# Back-compat alias
def refine_ambiguous_match(*args, **kwargs):
    return refine_match(*args, **kwargs)
