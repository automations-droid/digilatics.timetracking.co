"""Predict user identities for time-log matching (heuristic + optional local LLM)."""

from __future__ import annotations

import re

from local_llm import chat, extract_json_array, is_enabled, ollama_available

ALLOWED_DOMAIN = __import__("os").getenv("ALLOWED_DOMAIN", "digilatics.com").lower().strip()


def _norm_email(email: str) -> str:
    email = (email or "").strip().lower()
    if email and "@" not in email:
        email = f"{email}@{ALLOWED_DOMAIN}"
    return email


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        v = str(item or "").strip()
        if not v:
            continue
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def predict_identities_heuristic(*, name: str, email: str, username: str) -> list[str]:
    """Rule-based identities — matches patterns in users.json."""
    email = _norm_email(email)
    username = (username or email.split("@")[0] if email else "").strip().lower()
    name = (name or "").strip()
    parts = [p for p in re.split(r"\s+", name) if p]

    items: list[str] = []
    if email:
        items.append(email)
    if username:
        items.append(username)
    if name:
        items.append(name)

    if parts:
        items.append(parts[0])
        if len(parts) > 1:
            items.append(parts[-1])
            items.append(f"{parts[0]} {parts[-1]}")
        if len(parts) >= 3:
            # e.g. Muhammad Omar Shabbir → also "Omar Shabbir"
            items.append(" ".join(parts[1:]))

    if username:
        items.append(username.replace(".", ""))
        if "." in username:
            items.append(username.replace(".", " "))

    # Title-case variants for calendar / ClickUp display names
    if name:
        items.append(name.title())
    if parts:
        items.append(parts[0].title())

    return _dedupe(items)


def predict_identities_llm(*, name: str, email: str, username: str, seed: list[str]) -> list[str]:
    if not ollama_available():
        return []
    email = _norm_email(email)
    username = username or (email.split("@")[0] if email else "")
    prompt = f"""You help match employee time-tracking logs (ClickUp + Google Calendar).
Given:
- Full name: {name}
- Email: {email}
- Username: {username}
- Already known variants: {", ".join(seed[:8])}

Suggest 0-4 ADDITIONAL name strings that might appear as the person's display name in ClickUp tasks or calendar events (nicknames, shortened names, alternate spellings).
Do NOT repeat items already in the known list.
Return ONLY a JSON array of strings, no markdown."""

    text = chat(
        [
            {"role": "system", "content": "You output valid JSON arrays only."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=180,
        temperature=0.1,
    )
    extras = extract_json_array(text or "")
    return _dedupe([x for x in extras if x.lower() not in {s.lower() for s in seed}])


def predict_identities(
    *,
    name: str,
    email: str,
    username: str = "",
    use_llm: bool = True,
) -> dict:
    base = predict_identities_heuristic(name=name, email=email, username=username)
    source = "heuristic"
    extras: list[str] = []

    if use_llm and is_enabled() and name.strip():
        extras = predict_identities_llm(name=name, email=email, username=username, seed=base)
        if extras:
            source = "mixed"

    identities = _dedupe(base + extras)
    return {
        "identities": identities,
        "source": source,
        "llmAvailable": ollama_available(),
    }
