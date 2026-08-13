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

SYSTEM_PROMPT = """You are a proofreader for Digilatics time-entry CLIENT assignment.

A heuristic already assigned a client. Your ONLY job: say if that assignment is correct.

HARD RULES:
1. Reply ONLY JSON: {"ok":true|false,"client":"<exact allowed name>","confidence":0.0-1.0,"reason":"<short>"}
2. If ok=true, set client to the same assigned_client (must be from allowed_clients).
3. If ok=false, set client to the correct name from allowed_clients only — never invent names.
4. Internal / inter-department titles (team x team, sync, standup, HR Activity, Web Team, etc.)
   → Digilatics sub-brand or user_fallback — NOT an external client name collision.
5. External client meetings: only when the client is clearly the subject of the title.
6. Prefer keeping the heuristic assignment when unsure (ok=true, confidence moderate).
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


def claude_proofread_client(
    *,
    title: str,
    assigned_client: str,
    allowed_clients: list[str],
    user_fallback: str = "Digilatics",
    user_team: str = "",
    user_email: str = "",
    attendee_emails: Optional[list[str]] = None,
    description: str = "",
    source: str = "meeting",
    via: str = "",
) -> Optional[dict]:
    """Ask Claude only whether the heuristic client assignment looks correct."""
    if not ENABLE_CLAUDE_MATCH:
        return None
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY missing; skipping Claude proofread")
        return None

    assigned = (assigned_client or "").strip()
    if not assigned:
        return None

    allowed = _normalize_allowed(list(allowed_clients) + list(DIGILATICS_BRANDS))
    if user_fallback and user_fallback not in {a for a in allowed}:
        allowed.append(user_fallback)
    allowed_set = {a.lower(): a for a in allowed}
    if assigned.lower() not in allowed_set:
        allowed.append(assigned)
        allowed_set[assigned.lower()] = assigned

    payload = {
        "title": title or "",
        "description": (description or "")[:400],
        "source": source,
        "assigned_client": assigned,
        "assigned_via": via,
        "user_fallback": user_fallback,
        "user_team": user_team,
        "user_email": user_email,
        "attendee_emails": (attendee_emails or [])[:20],
        "allowed_clients": allowed[:300],
    }

    try:
        resp = _client().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=160,
            temperature=0,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": "Proofread this client assignment:\n"
                    + json.dumps(payload, ensure_ascii=False),
                }
            ],
        )
        text = "".join(
            getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        m = re.search(r"\{[\s\S]*\}", text)
        if not m:
            log.warning("Claude proofread non-JSON: %s", text[:200])
            return None
        data = json.loads(m.group(0))
        ok = bool(data.get("ok"))
        client = str(data.get("client") or "").strip()
        conf = float(data.get("confidence") or 0)
        reason = str(data.get("reason") or "")
        canon = allowed_set.get(client.lower()) if client else None
        if ok:
            return {
                "ok": True,
                "client": assigned,
                "confidence": conf,
                "reason": reason,
                "via": "claude_proofread_ok",
            }
        if not canon or conf < CLAUDE_MIN_CONFIDENCE:
            return {
                "ok": False,
                "client": assigned,
                "confidence": conf,
                "reason": reason,
                "via": "claude_proofread_keep",
                "claude_client": canon,
            }
        return {
            "ok": False,
            "client": canon,
            "confidence": conf,
            "reason": reason,
            "via": "claude_proofread_fix",
        }
    except Exception as e:
        log.exception("Claude proofread failed: %s", e)
        return None


# Legacy name — matching no longer uses Claude to pick clients from scratch.
def claude_pick_client(**kwargs) -> Optional[dict]:
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
    Heuristics own assignment. Claude ONLY proofreads the final client
    (is it correct?) and may correct high-confidence mistakes.
    Skips Claude for weak fallbacks and deterministic priority overrides.
    """
    result = dict(result or {})
    via = result.get("via") or ""

    # 1) Deterministic priority (beats exact match) — no Claude
    ov = priority_override(title, user_fallback)
    if ov:
        out = dict(result)
        out["client"] = ov["client"]
        out["via"] = ov["via"]
        out["prev_via"] = via
        out["prev_client"] = result.get("client")
        return out

    # 2) Do not call Claude to invent clients for weak/fallback rows (saves cost)
    weak = via in {"fallback", "internal_fallback", "digilatics_override", "claude_low_conf"}
    if weak and not looks_internal_meeting(title):
        return result

    # 3) Proofread only when we have a concrete assignment worth checking
    assigned = str(result.get("client") or "").strip()
    if not assigned:
        return result

    # Skip proofread on already-deterministic brand/interdept vias
    if via in {"title_brand_rule", "interdept_user_fallback"}:
        return result

    if not ENABLE_CLAUDE_MATCH:
        return result

    # Only proofread external-looking matches or suspicious internal collisions
    worth_proofread = via in {
        "title_exact",
        "title_starts_with",
        "legacy_split",
        "title_fuzzy",
        "domain_match",
        "custom_field",
        "description_exact",
    } or looks_internal_meeting(title)
    if not worth_proofread:
        return result

    reviewed = claude_proofread_client(
        title=title,
        assigned_client=assigned,
        allowed_clients=allowed_clients,
        user_fallback=user_fallback,
        user_team=user_team,
        user_email=user_email,
        attendee_emails=attendee_emails,
        description=description,
        source=source,
        via=via,
    )
    if not reviewed:
        return result

    out = dict(result)
    out["claude_reason"] = reviewed.get("reason")
    out["claude_confidence"] = reviewed.get("confidence")
    if reviewed.get("via") == "claude_proofread_fix" and reviewed.get("client"):
        out["prev_client"] = assigned
        out["prev_via"] = via
        out["client"] = reviewed["client"]
        out["via"] = reviewed["via"]
        out["confidence"] = reviewed.get("confidence")
        out["reason"] = reviewed.get("reason")
    else:
        out["via"] = via  # keep heuristic
        if reviewed.get("via"):
            out["proofread"] = reviewed.get("via")
    return out


# Back-compat alias
def refine_ambiguous_match(*args, **kwargs):
    return refine_match(*args, **kwargs)
