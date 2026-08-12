"""Shared client-matching helpers ported from n8n matcher v5."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# Fuzzy (levenshtein) caused many wrong client assignments — off by default.
ENABLE_FUZZY_MATCH = os.getenv("ENABLE_FUZZY_MATCH", "false").lower() == "true"

SUFFIXES = [
    "inspections and engineering",
    "& engineering",
    "and engineering",
    "engineering",
    "inspections",
    "inspection",
    "services",
    "service",
    "group",
    "inc",
    "llc",
    "ltd",
    "co",
    "cleaning",
]


@dataclass
class Client:
    canonical: str
    aliases: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)


@dataclass
class MatchIndexEntry:
    text: str
    canonical: str


def levenshtein(a: str, b: str) -> int:
    m, n = len(a), len(b)
    if m == 0:
        return n
    if n == 0:
        return m
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[n]


def norm(s: Optional[str]) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def generate_stripped_aliases(canonical: str) -> list[str]:
    words = canonical.strip().split()
    aliases: list[str] = []
    if len(words) >= 3:
        stripped = " ".join(words[:-1]).strip()
        if len(stripped) >= 3:
            aliases.append(stripped)
    lower = canonical.lower().strip()
    for suffix in SUFFIXES:
        if lower.endswith(" " + suffix):
            stripped = canonical[: len(canonical) - len(suffix)].strip()
            if (
                len(stripped) >= 3
                and len(stripped) < len(canonical)
                and not any(a.lower() == stripped.lower() for a in aliases)
            ):
                aliases.append(stripped)
    return aliases


def enrich_client(canonical: str, aliases_raw: str = "", domains_raw: str = "") -> Client:
    aliases = [s.strip() for s in aliases_raw.split(",") if s.strip()]
    for a in generate_stripped_aliases(canonical):
        if not any(x.lower() == a.lower() for x in aliases):
            aliases.append(a)
    if canonical.lower() == "digilatics" and not any(a.lower() == "digilatcis" for a in aliases):
        aliases.append("Digilatcis")
    domains = [
        s.strip().lower().lstrip("@")
        for s in domains_raw.split(",")
        if s.strip()
    ]
    return Client(canonical=canonical, aliases=aliases, domains=domains)


def build_match_index(clients: list[Client]) -> list[MatchIndexEntry]:
    index: list[MatchIndexEntry] = []
    for c in clients:
        index.append(MatchIndexEntry(text=norm(c.canonical), canonical=c.canonical))
        for a in c.aliases:
            t = norm(a)
            if t:
                index.append(MatchIndexEntry(text=t, canonical=c.canonical))
    index.sort(key=lambda e: len(e.text), reverse=True)
    return index


def exact_match(haystack: Optional[str], index: list[MatchIndexEntry], min_length: int = 1) -> Optional[str]:
    if not haystack:
        return None
    hay = f" {norm(haystack)} "
    digilatics_match = None
    for e in index:
        if not e.text or len(e.text) < min_length:
            continue
        if f" {e.text} " in hay:
            if e.canonical.lower() == "digilatics":
                if not digilatics_match:
                    digilatics_match = e.canonical
                continue
            return e.canonical
    return digilatics_match


def starts_with_match(haystack: Optional[str], index: list[MatchIndexEntry]) -> Optional[str]:
    if not haystack:
        return None
    normed = norm(haystack)
    if not normed:
        return None
    digilatics_match = None
    for e in index:
        if not e.text:
            continue
        matches = (normed == e.text) or normed.startswith(e.text + " ")
        if matches:
            if e.canonical.lower() == "digilatics":
                if not digilatics_match:
                    digilatics_match = e.canonical
                continue
            return e.canonical
    return digilatics_match


def fuzzy_match(haystack: Optional[str], index: list[MatchIndexEntry]) -> Optional[str]:
    if not haystack:
        return None
    tokens = [t for t in norm(haystack).split(" ") if len(t) >= 5]
    if not tokens:
        return None
    best = None
    best_dist = 3
    for e in index:
        if not e.text or len(e.text) < 5 or " " in e.text:
            continue
        for tok in tokens:
            if tok[0] != e.text[0]:
                continue
            d = levenshtein(tok, e.text)
            if d < best_dist:
                best_dist = d
                best = e.canonical
    return best


def legacy_split(title: Optional[str], index: list[MatchIndexEntry]) -> Optional[str]:
    if not title:
        return None
    parts = re.split(r"\s[-|–—]\s", title)
    if len(parts) < 2:
        return None
    first = parts[0].strip()
    if not first:
        return None
    return exact_match(first, index)


def is_digilatics(client_name: Optional[str]) -> bool:
    if not client_name:
        return False
    return str(client_name).lower().strip() == "digilatics"


def custom_field_match(custom_fields: Any, index: list[MatchIndexEntry]) -> Optional[str]:
    if not isinstance(custom_fields, list):
        return None
    for cf in custom_fields:
        if not cf or not cf.get("name"):
            continue
        if str(cf["name"]).strip().lower() != "client":
            continue
        field_value = None
        cf_type = cf.get("type")
        value = cf.get("value")
        type_config = cf.get("type_config") or {}
        opts = type_config.get("options") or []
        if cf_type == "drop_down" and value is not None:
            opt = None
            if isinstance(value, (int, float)):
                opt = next((o for o in opts if o.get("orderindex") == value), None)
            elif isinstance(value, str):
                opt = next((o for o in opts if o.get("id") == value), None)
            if opt and opt.get("name"):
                field_value = opt["name"]
        elif cf_type == "labels" and isinstance(value, list) and value:
            opt = next((o for o in opts if o.get("id") == value[0]), None)
            if opt and opt.get("label"):
                field_value = opt["label"]
        elif isinstance(value, str):
            field_value = value
        if not field_value:
            continue
        hay = f" {norm(field_value)} "
        for e in index:
            if e.text and f" {e.text} " in hay:
                return e.canonical
        return field_value.strip()
    return None


def domain_match_text(text: Optional[str], clients: list[Client]) -> Optional[str]:
    if not text:
        return None
    lower = text.lower()
    for c in clients:
        if not c.domains:
            continue
        for domain in sorted(c.domains, key=len, reverse=True):
            if domain and domain in lower:
                return c.canonical
    return None


def domain_match_attendees(
    attendee_emails: Optional[list[str]],
    clients: list[Client],
    own_domain: str = "digilatics.com",
) -> Optional[str]:
    if not isinstance(attendee_emails, list):
        return None
    own = own_domain.lower()
    for email in attendee_emails:
        if not email or "@" not in email:
            continue
        dom = email.split("@", 1)[1].lower().strip()
        if dom == own:
            continue
        for c in clients:
            if c.domains and dom in c.domains:
                return c.canonical
    return None


def is_all_digilatics(attendee_emails: Optional[list[str]], own_domain: str = "digilatics.com") -> bool:
    if not isinstance(attendee_emails, list) or len(attendee_emails) == 0:
        return True
    own = own_domain.lower()
    for email in attendee_emails:
        if not email or "@" not in email:
            continue
        dom = email.split("@", 1)[1].lower().strip()
        if dom != own and dom != "gmail.com":
            return False
    return True


def match_client_clickup(
    *,
    title: str,
    description: str,
    custom_fields: Any,
    user_fallback: str,
    clients: list[Client],
    index: list[MatchIndexEntry],
    user_team: str = "",
    user_email: str = "",
    allowed_clients: Optional[list[str]] = None,
    use_claude: bool = True,
) -> dict:
    from claude_match import priority_override, refine_match

    ov = priority_override(title, user_fallback or "Digilatics")
    if ov:
        return ov

    cf = custom_field_match(custom_fields, index)
    if cf and not is_digilatics(cf):
        return {"client": cf, "via": "custom_field"}

    dm = domain_match_text(title, clients) or domain_match_text(description, clients)
    if dm and not is_digilatics(dm):
        return {"client": dm, "via": "domain_match"}

    t = exact_match(title, index)
    sw = starts_with_match(title, index)
    d = exact_match(description, index, min_length=4)
    fz = fuzzy_match(title, index) if ENABLE_FUZZY_MATCH else None
    lg = legacy_split(title, index)

    if t and not is_digilatics(t):
        result = {"client": t, "via": "title_exact"}
    elif sw and not is_digilatics(sw):
        result = {"client": sw, "via": "title_starts_with"}
    elif d and not is_digilatics(d):
        result = {"client": d, "via": "description_exact"}
    elif fz and not is_digilatics(fz):
        result = {"client": fz, "via": "title_fuzzy"}
    elif lg and not is_digilatics(lg):
        result = {"client": lg, "via": "legacy_split"}
    elif cf or t or sw or d or fz or lg:
        result = {"client": user_fallback or "Digilatics", "via": "digilatics_override"}
    else:
        result = {"client": user_fallback or "Digilatics", "via": "fallback"}

    if use_claude:
        allowed = allowed_clients or [c.canonical for c in clients]
        result = refine_match(
            result,
            title=title,
            allowed_clients=allowed,
            user_fallback=user_fallback or "Digilatics",
            user_team=user_team,
            user_email=user_email,
            description=description,
            source="clickup",
        )
    return result


def match_launchpad_subclient(task_title: Optional[str], subclients: list[str]) -> Optional[str]:
    if not task_title or not subclients:
        return None
    hay = norm(task_title)
    for sc in sorted(subclients, key=len, reverse=True):
        n = norm(sc)
        if n and n in hay:
            return sc
    return None


def detect_launchpad_region(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    t = title.lower()
    if "southeast" in t:
        return "Southeast"
    if "southwest" in t:
        return "Southwest"
    return None


def get_subclients_for_region(region: Optional[str], subclients: list[str]) -> list[str]:
    if not region:
        return []
    suffix = " - " + region.lower()
    return [sc for sc in subclients if sc.lower().endswith(suffix)]


def match_client_with_launchpad(
    *,
    title: str,
    description: str,
    custom_fields: Any,
    user_fallback: str,
    clients: list[Client],
    index: list[MatchIndexEntry],
    subclients: list[str],
    user_team: str = "",
    user_email: str = "",
    allowed_clients: Optional[list[str]] = None,
    use_claude: bool = True,
) -> dict:
    result = match_client_clickup(
        title=title,
        description=description,
        custom_fields=custom_fields,
        user_fallback=user_fallback,
        clients=clients,
        index=index,
        user_team=user_team,
        user_email=user_email,
        allowed_clients=allowed_clients,
        use_claude=use_claude,
    )
    if result.get("client") and str(result["client"]).lower() == "launchpad":
        sub = match_launchpad_subclient(title, subclients)
        if sub:
            return {"client": sub, "via": "launchpad_subclient"}
        region = detect_launchpad_region(title)
        if region:
            region_clients = get_subclients_for_region(region, subclients)
            if region_clients:
                return {
                    "client": "Launchpad",
                    "via": result["via"],
                    "region": region,
                    "regionClients": region_clients,
                }
        return {"client": "Launchpad", "via": result["via"]}
    return result


def match_client_meet(
    *,
    title: str,
    attendee_emails: list[str],
    user_fallback: str,
    clients: list[Client],
    index: list[MatchIndexEntry],
    user_team: str = "",
    user_email: str = "",
    allowed_clients: Optional[list[str]] = None,
    use_claude: bool = True,
) -> dict:
    from claude_match import priority_override, refine_match

    ov = priority_override(title, user_fallback or "Digilatics")
    if ov:
        return ov

    t = exact_match(title, index)
    sw = starts_with_match(title, index)
    dm = domain_match_attendees(attendee_emails, clients, "digilatics.com")
    fz = fuzzy_match(title, index) if ENABLE_FUZZY_MATCH else None
    lg = legacy_split(title, index)

    if t and not is_digilatics(t):
        result = {"client": t, "via": "title_exact"}
    elif sw and not is_digilatics(sw):
        result = {"client": sw, "via": "title_starts_with"}
    elif dm and not is_digilatics(dm):
        result = {"client": dm, "via": "domain_match"}
    elif fz and not is_digilatics(fz):
        result = {"client": fz, "via": "title_fuzzy"}
    elif lg and not is_digilatics(lg):
        result = {"client": lg, "via": "legacy_split"}
    elif t or sw or dm or fz or lg:
        result = {"client": user_fallback or "Digilatics", "via": "digilatics_override"}
    elif is_all_digilatics(attendee_emails, "digilatics.com"):
        result = {"client": user_fallback or "Digilatics", "via": "internal_fallback"}
    else:
        result = {"client": user_fallback or "Digilatics", "via": "fallback"}

    if use_claude:
        allowed = allowed_clients or [c.canonical for c in clients]
        result = refine_match(
            result,
            title=title,
            allowed_clients=allowed,
            user_fallback=user_fallback or "Digilatics",
            user_team=user_team,
            user_email=user_email,
            attendee_emails=attendee_emails,
            source="meeting",
        )
    return result
