"""Read/write users.json and clients.json for admin settings."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import bcrypt

from sheets_refs import CLIENTS_FILE, load_employees

HERE = os.path.dirname(os.path.abspath(__file__))
USERS_PATH = os.path.join(HERE, "users.json")
ALLOWED_DOMAIN = os.getenv("ALLOWED_DOMAIN", "digilatics.com").lower().strip()


def _atomic_write_json(path: str, data: Any) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def load_users_raw() -> dict:
    if not os.path.exists(USERS_PATH):
        return {}
    with open(USERS_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_users_raw(raw: dict) -> None:
    _atomic_write_json(USERS_PATH, raw)


def load_clients_raw() -> list[dict]:
    path = CLIENTS_FILE if os.path.isabs(CLIENTS_FILE) else os.path.join(HERE, CLIENTS_FILE)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def save_clients_raw(clients: list[dict]) -> None:
    path = CLIENTS_FILE if os.path.isabs(CLIENTS_FILE) else os.path.join(HERE, CLIENTS_FILE)
    _atomic_write_json(path, clients)


def list_users_public() -> list[dict]:
    raw = load_users_raw()
    out: list[dict] = []
    for email, cfg in sorted(raw.items(), key=lambda x: x[0].lower()):
        out.append(
            {
                "email": email.lower(),
                "name": cfg.get("name") or email.split("@")[0],
                "username": cfg.get("username") or email.split("@")[0],
                "role": (cfg.get("role") or "employee").lower(),
                "teams": cfg.get("teams") or [],
                "identities": cfg.get("identities") or [],
            }
        )
    return out


def list_clients_public() -> list[dict]:
    return [
        {
            "name": str(c.get("name") or "").strip(),
            "aliases": c.get("aliases") or [],
            "domains": c.get("domains") or [],
        }
        for c in load_clients_raw()
        if str(c.get("name") or "").strip()
    ]


def _find_client_index(clients: list[dict], name: str) -> int:
    key = name.strip().lower()
    for i, c in enumerate(clients):
        if str(c.get("name") or "").strip().lower() == key:
            return i
    return -1


def update_client(
    *,
    original_name: str,
    name: str,
    aliases: list[str],
    domains: list[str],
) -> dict:
    original_name = original_name.strip()
    name = name.strip()
    if len(name) < 2:
        raise ValueError("Client name is too short")

    clients = load_clients_raw()
    idx = _find_client_index(clients, original_name)
    if idx < 0:
        raise ValueError("Client not found")

    if name.lower() != original_name.lower():
        if _find_client_index(clients, name) >= 0:
            raise ValueError("Another client already uses that name")

    clients[idx] = {
        "name": name,
        "aliases": [a.strip() for a in aliases if a.strip()],
        "domains": [d.strip().lower() for d in domains if d.strip()],
    }
    clients.sort(key=lambda c: str(c.get("name") or "").lower())
    save_clients_raw(clients)
    return clients[idx]


def delete_client(*, name: str) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Client name is required")

    clients = load_clients_raw()
    idx = _find_client_index(clients, name)
    if idx < 0:
        raise ValueError("Client not found")

    removed = clients.pop(idx)
    save_clients_raw(clients)
    return {
        "name": str(removed.get("name") or "").strip(),
        "aliases": removed.get("aliases") or [],
        "domains": removed.get("domains") or [],
    }


def list_team_options() -> list[str]:
    teams: set[str] = set()
    for cfg in load_users_raw().values():
        for t in cfg.get("teams") or []:
            if str(t).strip():
                teams.add(str(t).strip())
    try:
        _, _, email_team, _ = load_employees()
        for t in email_team.values():
            if str(t).strip():
                teams.add(str(t).strip())
    except Exception:
        pass
    return sorted(teams, key=str.lower)


def _norm_email(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email:
        email = f"{email}@{ALLOWED_DOMAIN}"
    return email


def add_client(*, name: str, aliases: list[str], domains: list[str]) -> dict:
    name = name.strip()
    if len(name) < 2:
        raise ValueError("Client name is too short")

    clients = load_clients_raw()
    key = name.lower()
    if any(str(c.get("name") or "").strip().lower() == key for c in clients):
        raise ValueError("Client already exists")

    entry = {
        "name": name,
        "aliases": [a.strip() for a in aliases if a.strip()],
        "domains": [d.strip().lower() for d in domains if d.strip()],
    }
    clients.append(entry)
    clients.sort(key=lambda c: str(c.get("name") or "").lower())
    save_clients_raw(clients)
    return entry


def add_user(
    *,
    email: str,
    name: str,
    username: str,
    password: str,
    role: str,
    teams: list[str],
    extra_identities: list[str],
) -> dict:
    email = _norm_email(email)
    if not email.endswith(f"@{ALLOWED_DOMAIN}"):
        raise ValueError(f"Email must be @{ALLOWED_DOMAIN}")

    role = (role or "employee").lower()
    if role not in {"employee", "lead", "admin"}:
        raise ValueError("Invalid role")

    name = name.strip()
    username = (username or email.split("@")[0]).strip().lower()
    if not re.match(r"^[a-z0-9._-]+$", username):
        raise ValueError("Username may only contain letters, numbers, dots, dashes, underscores")

    raw = load_users_raw()
    if email in raw:
        raise ValueError("User already exists")
    for other_email, cfg in raw.items():
        if (cfg.get("username") or "").lower() == username:
            raise ValueError("Username already taken")

    from identity_predict import predict_identities

    base = predict_identities(name=name, email=email, username=username)["identities"]
    idents: list[str] = []
    for item in base + (extra_identities or []):
        v = str(item).strip()
        if v and v.lower() not in {i.lower() for i in idents}:
            idents.append(v)

    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    entry: dict = {
        "role": role,
        "name": name,
        "username": username,
        "password_hash": pw_hash,
        "identities": idents,
    }
    if role == "lead" and teams:
        entry["teams"] = [t.strip() for t in teams if t.strip()]

    raw[email] = entry
    save_users_raw(raw)
    return {"email": email, **{k: v for k, v in entry.items() if k != "password_hash"}}


def update_profile(*, email: str, name: str | None = None, new_password: str | None = None) -> dict:
    email = email.lower().strip()
    raw = load_users_raw()
    if email not in raw:
        raise ValueError("User not found")

    cfg = raw[email]
    if name is not None:
        name = name.strip()
        if len(name) < 2:
            raise ValueError("Name is too short")
        cfg["name"] = name
        idents = list(cfg.get("identities") or [])
        if name not in idents:
            idents.append(name)
        first = name.split()[0] if name.split() else ""
        if first and first not in idents:
            idents.append(first)
        cfg["identities"] = idents

    if new_password:
        if len(new_password) < 6:
            raise ValueError("Password must be at least 6 characters")
        cfg["password_hash"] = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    raw[email] = cfg
    save_users_raw(raw)
    return {"email": email, "name": cfg.get("name"), "username": cfg.get("username")}
