"""Personal time assistant — factual answers from DB; LLM only when needed."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from local_llm import OLLAMA_MODEL, chat, ollama_available

TZ = ZoneInfo("America/New_York")

FALLBACK_NO_LLM = (
    "Local AI is offline. Install Ollama and run: ollama pull qwen2.5:0.5b\n"
    "Or set OLLAMA_BASE_URL if Ollama runs elsewhere."
)


def _today_et() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _row_date(r: dict) -> str:
    return str(r.get("Date") or r.get("_date") or "")


def _row_hours(r: dict) -> float:
    try:
        return float(r.get("Hours") or 0)
    except (TypeError, ValueError):
        return 0.0


def _all_rows() -> list[dict]:
    from db import fetch_all_entries, get_session, init_db

    init_db()
    session = get_session()
    try:
        return fetch_all_entries(session)
    finally:
        session.close()


def _name_tokens(name: str) -> set[str]:
    """First/last name parts for matching \"nabeel's hours\" etc."""
    parts = re.split(r"[\s._-]+", name.lower())
    skip = {"mr", "mrs", "ms", "dr", "muhammad", "mohammad", "mohammed"}
    return {p for p in parts if len(p) >= 3 and p not in skip}


def _user_directory() -> list[dict]:
    from settings_store import load_users_raw

    out: list[dict] = []
    for email, cfg in load_users_raw().items():
        idents = {str(i).lower().strip() for i in cfg.get("identities") or []}
        idents.add(email.lower())
        idents.add(str(cfg.get("username") or "").lower())
        idents.add(str(cfg.get("name") or "").lower())
        idents.update(_name_tokens(str(cfg.get("name") or "")))
        idents.update(_name_tokens(str(cfg.get("username") or "")))
        idents.discard("")
        out.append(
            {
                "email": email.lower(),
                "name": cfg.get("name") or email.split("@")[0],
                "username": (cfg.get("username") or email.split("@")[0]).lower(),
                "role": (cfg.get("role") or "employee").lower(),
                "teams": [str(t) for t in cfg.get("teams") or []],
                "idents": idents,
            }
        )
    return out


def _can_view_target(viewer: dict, target: dict) -> bool:
    if viewer.get("email", "").lower() == target.get("email", "").lower():
        return True
    role = (viewer.get("role") or "employee").lower()
    if role == "admin":
        return True
    if role == "lead":
        vteams = {t.lower() for t in viewer.get("teams") or []}
        tteams = {t.lower() for t in target.get("teams") or []}
        return bool(vteams & tteams)
    return False


def _normalize_query(msg: str) -> str:
    """Fix common typos in time-assistant queries."""
    msg = msg.lower()
    msg = re.sub(r"\bfyesterday\b", "yesterday", msg)
    msg = re.sub(r"\byday\b", "yesterday", msg)
    msg = re.sub(r"\byester\b", "yesterday", msg)
    return msg


def _token_matches_msg(token: str, msg: str) -> bool:
    if len(token) < 3:
        return False
    pat = rf"\b{re.escape(token)}(?:'s|s)?\b"
    return bool(re.search(pat, msg))


def _find_mentioned_user(msg: str, directory: list[dict], *, exclude_email: str = "") -> dict | None:
    best: dict | None = None
    best_len = 0
    for u in directory:
        if u["email"] == exclude_email:
            continue
        for token in u["idents"]:
            if _token_matches_msg(token, msg):
                if len(token) > best_len:
                    best = u
                    best_len = len(token)
    return best


def _is_self_query(msg: str) -> bool:
    """True when the user is clearly asking about their own time."""
    if re.search(r"\bmy\b", msg) or re.search(r"\bmyself\b", msg) or re.search(r"\bmine\b", msg):
        return True
    if re.search(r"\bfor me\b", msg) or re.search(r"\babout me\b", msg):
        return True
    if re.match(r"^my\b", msg.strip()):
        return True
    return False


def _detect_subject(message: str, viewer: dict, *, context: str = "") -> tuple[str, dict | None]:
    """Return ('self', viewer) or ('other', target_user_dict)."""
    msg = _normalize_query(message)
    combined = _normalize_query(f"{context} {message}")
    directory = _user_directory()
    viewer_email = viewer.get("email", "").lower()

    other = _find_mentioned_user(combined, directory, exclude_email=viewer_email)
    if other and not _is_self_query(msg):
        return "other", other

    if _is_self_query(msg):
        viewer_entry = next((u for u in directory if u["email"] == viewer_email), None)
        return "self", viewer_entry or viewer

    if other:
        return "other", other

    viewer_entry = next((u for u in directory if u["email"] == viewer_email), None)
    return "self", viewer_entry or viewer


def _rows_for_user(all_rows: list[dict], user: dict) -> list[dict]:
    idents = user.get("idents") or set()
    if isinstance(idents, list):
        idents = set(idents)
    return [r for r in all_rows if str(r.get("User") or "").lower() in idents]


def _parse_period(message: str, *, context: str = "") -> tuple[str, str, str, str]:
    """label, start_date, end_date, display_title. Current message wins over chat history."""
    msg = _normalize_query(message)
    ctx = _normalize_query(context)
    today = datetime.now(TZ).date()
    today_s = today.strftime("%Y-%m-%d")

    def _resolve(source: str) -> tuple[str, str, str, str] | None:
        if "yesterday" in source:
            d = today - timedelta(days=1)
            ds = d.strftime("%Y-%m-%d")
            return "yesterday", ds, ds, f"Yesterday ({ds} ET)"

        if re.search(r"\btoday\b", source):
            return "today", today_s, today_s, f"Today ({today_s} ET)"

        if "last month" in source:
            first = today.replace(day=1)
            last = first - timedelta(days=1)
            start = last.replace(day=1)
            return (
                "last_month",
                start.strftime("%Y-%m-%d"),
                last.strftime("%Y-%m-%d"),
                f"Last month ({start.strftime('%Y-%m-%d')} to {last.strftime('%Y-%m-%d')} ET)",
            )

        if "this month" in source:
            start = today.replace(day=1).strftime("%Y-%m-%d")
            return "this_month", start, today_s, f"This month ({start} to {today_s} ET)"

        if "this week" in source or (re.search(r"\bweek\b", source) and "last week" not in source):
            start = (today - timedelta(days=7)).strftime("%Y-%m-%d")
            return "this_week", start, today_s, f"This week ({start} to {today_s} ET)"

        if "last week" in source:
            end = today - timedelta(days=today.weekday() + 1)
            start = end - timedelta(days=6)
            return (
                "last_week",
                start.strftime("%Y-%m-%d"),
                end.strftime("%Y-%m-%d"),
                f"Last week ({start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')} ET)",
            )
        return None

    current = _resolve(msg)
    if current:
        return current
    historical = _resolve(ctx)
    if historical:
        return historical

    return (
        "this_week",
        (today - timedelta(days=7)).strftime("%Y-%m-%d"),
        today_s,
        f"This week ({(today - timedelta(days=7)).strftime('%Y-%m-%d')} to {today_s} ET)",
    )


def _filter_period(rows: list[dict], start: str, end: str) -> list[dict]:
    return [r for r in rows if start <= _row_date(r) <= end]


def _format_period_report(*, person_name: str, title: str, rows: list[dict]) -> str:
    if not rows:
        return f"{person_name} — {title}\n\nNo logged time in the database for this period."

    total = round(sum(_row_hours(r) for r in rows), 2)
    tasks = round(sum(_row_hours(r) for r in rows if r.get("Source") == "clickup"), 2)
    meets = round(sum(_row_hours(r) for r in rows if r.get("Source") == "meeting"), 2)

    by_client: dict[str, float] = defaultdict(float)
    for r in rows:
        by_client[str(r.get("Client") or "Unknown")] += _row_hours(r)
    client_lines = [
        f"  • {c}: {h:.2f}h" for c, h in sorted(by_client.items(), key=lambda x: -x[1])[:8]
    ]

    entry_lines = []
    for r in sorted(rows, key=lambda x: (_row_date(x), -_row_hours(x))):
        entry_lines.append(
            f"  • {_row_date(r)} | {r.get('Client') or '-'} | "
            f"{(r.get('Task') or '-')[:60]} | {_row_hours(r):.2f}h | {r.get('Source') or '-'}"
        )
    shown = entry_lines[:15]
    more = len(entry_lines) - len(shown)

    parts = [
        f"{person_name} — {title}",
        "",
        f"Total: {total:.2f}h ({tasks:.2f}h tasks, {meets:.2f}h meetings)",
        f"Entries: {len(rows)}",
        "",
        "By client:",
        *client_lines,
        "",
        "Entries:",
        *shown,
    ]
    if more > 0:
        parts.append(f"  … and {more} more entries")
    return "\n".join(parts)


def _history_context(history: list[dict] | None) -> str:
    """Recent user turns for follow-ups like \"yesterday only\" after asking about Nabeel."""
    if not history:
        return ""
    parts: list[str] = []
    for turn in history[-4:]:
        if turn.get("role") == "user":
            content = (turn.get("content") or "").strip()
            if content:
                parts.append(content)
    return " ".join(parts)


def try_data_answer(*, profile: dict, message: str, history: list[dict] | None = None) -> str | None:
    """Deterministic answer for hours/time questions — no LLM hallucination."""
    msg = _normalize_query(message.strip())
    if not msg:
        return None

    context = _history_context(history)
    combined = _normalize_query(f"{context} {message}")

    # Only handle time/hours/activity style questions
    time_words = (
        "hour",
        "time",
        "spent",
        "log",
        "work",
        "task",
        "meet",
        "client",
        "yesterday",
        "today",
        "week",
        "month",
        "activity",
        "only",
    )
    if not any(w in combined for w in time_words):
        return None

    subject_kind, subject = _detect_subject(message, profile, context=context)
    if not subject:
        return None

    viewer = next((u for u in _user_directory() if u["email"] == profile.get("email", "").lower()), profile)

    if subject_kind == "other":
        if not _can_view_target(viewer, subject):
            return (
                f"I can only show your own time data. "
                f"You're signed in as {profile.get('name')}. "
                f"Ask: \"my hours yesterday\" or contact an admin."
            )

    _, start, end, title = _parse_period(message, context=context)
    all_rows = _all_rows()
    user_rows = _rows_for_user(all_rows, subject)
    period_rows = _filter_period(user_rows, start, end)

    person = subject.get("name") or profile.get("name") or "You"
    if subject_kind == "self":
        person = profile.get("name") or "You"

    return _format_period_report(person_name=person, title=title, rows=period_rows)


def build_user_context(profile: dict) -> str:
    """Compact facts for LLM fallback only."""
    directory = _user_directory()
    viewer = next((u for u in directory if u["email"] == profile.get("email", "").lower()), None)
    if not viewer:
        viewer = {"idents": {i.lower() for i in profile.get("identities") or []}, "name": profile.get("name")}

    rows = _rows_for_user(_all_rows(), viewer)
    today = _today_et()
    yesterday = (datetime.now(TZ).date() - timedelta(days=1)).strftime("%Y-%m-%d")

    today_rows = _filter_period(rows, today, today)
    yday_rows = _filter_period(rows, yesterday, yesterday)

    def sum_h(rs):
        return round(sum(_row_hours(r) for r in rs), 2)

    lines = [
        f"Signed-in user: {profile.get('name')} ({profile.get('email')})",
        f"Role: {profile.get('role')} — chat shows ONLY this user's data unless admin asks about team.",
        f"Yesterday ({yesterday}): {sum_h(yday_rows)}h, {len(yday_rows)} entries",
        f"Today ({today}): {sum_h(today_rows)}h, {len(today_rows)} entries",
    ]
    for label, rs in [("Yesterday entries", yday_rows[:10]), ("Today entries", today_rows[:10])]:
        if rs:
            lines.append(label + ":")
            for r in rs:
                lines.append(
                    f"  - {_row_date(r)} | {r.get('User')} | {r.get('Client')} | "
                    f"{_row_hours(r):.2f}h | {r.get('Source')}"
                )
    return "\n".join(lines)


def answer_chat(*, profile: dict, message: str, history: list[dict] | None = None) -> dict:
    # 1) Factual DB answer first (fixes wrong dates / hallucinated hours)
    data_reply = try_data_answer(profile=profile, message=message, history=history)
    if data_reply:
        return {"reply": data_reply, "source": "data", "model": OLLAMA_MODEL}

    if not ollama_available():
        return {
            "reply": FALLBACK_NO_LLM,
            "source": "offline",
            "model": OLLAMA_MODEL,
        }

    context = build_user_context(profile)
    system = f"""You are the Digilatics Time Assistant.
The signed-in user is {profile.get('name')}. You ONLY have their personal time data below.
You CANNOT see other employees' hours unless the user is admin (role: {profile.get('role')}).
Never invent numbers. If asked about someone else and role is not admin, say you only have the signed-in user's data.
For hours questions, prefer exact figures from USER DATA only.

USER DATA:
{context}"""

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for turn in (history or [])[-4:]:
        role = turn.get("role")
        content = (turn.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message.strip()})

    reply = chat(messages, max_tokens=300, temperature=0.1)
    if not reply:
        return {
            "reply": "Sorry, I couldn't generate a reply. Try asking: \"my hours yesterday\"",
            "source": "error",
            "model": OLLAMA_MODEL,
        }
    return {"reply": reply, "source": "ollama", "model": OLLAMA_MODEL}
