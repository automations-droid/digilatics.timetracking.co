"""
Digilatics Time Intelligence — secure backend
=============================================

FastAPI app with Google login + role/team access wall. Time entries live in
Postgres (synced from ClickUp + Google Calendar); the browser never talks to
Sheets or source APIs directly.
"""

import os
import json
import logging
from contextlib import asynccontextmanager
from typing import Optional

import bcrypt
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth
from dotenv import load_dotenv

from security import (
    SecurityHeadersMiddleware,
    client_ip,
    establish_session,
    is_local_dev_request,
    login_guard,
    require_strong_session_secret,
    validate_password_strength,
    verify_session_binding,
)

load_dotenv()

logging.basicConfig(level=logging.INFO)

# ─────────────────────────── config ───────────────────────────
SESSION_SECRET   = require_strong_session_secret(os.getenv("SESSION_SECRET"))
ALLOWED_DOMAIN   = os.getenv("ALLOWED_DOMAIN", "digilatics.com").lower().strip()
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_SECRET    = os.getenv("GOOGLE_CLIENT_SECRET", "")
CACHE_TTL        = int(os.getenv("CACHE_TTL_SECONDS", "60"))
COOKIE_SECURE    = os.getenv("COOKIE_SECURE", "true").lower() == "true"
# Local-only impersonation. Never enable in production.
DEV_LOGIN        = os.getenv("DEV_LOGIN", "false").lower() == "true"
BASE_URL         = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")

HERE = os.path.dirname(os.path.abspath(__file__))


# ─────────────────────────── user directory ───────────────────────────
def load_users() -> dict:
    path = os.path.join(HERE, "users.json")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = json.load(f)
    out = {}
    for email, cfg in raw.items():
        e = email.lower().strip()
        out[e] = {
            "role": (cfg.get("role") or "employee").lower(),
            "teams": [t.strip() for t in cfg.get("teams", [])],
            "identities": [str(i).lower().strip() for i in cfg.get("identities", [])] + [e],
            "name": cfg.get("name") or e.split("@")[0],
            "password_hash": cfg.get("password_hash") or "",
            "username": (cfg.get("username") or e.split("@")[0]).lower().strip(),
        }
    return out


def resolve_login_identity(login: str) -> Optional[str]:
    """Accept email or username → return email key in users.json."""
    login = (login or "").strip().lower()
    if not login:
        return None
    users = load_users()
    if login in users:
        return login
    for email, cfg in users.items():
        if cfg.get("username") == login:
            return email
    # allow bare local-part match against email
    for email in users:
        if email.split("@")[0] == login:
            return email
    return None


def verify_password(plain: str, password_hash: str) -> bool:
    if not plain or not password_hash:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def user_profile(email: str) -> Optional[dict]:
    """Directory profile only — unknown emails are not auto-provisioned."""
    email = (email or "").lower().strip()
    users = load_users()
    if email not in users:
        return None
    p = dict(users[email])
    p.pop("password_hash", None)
    p["email"] = email
    return p


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=128)


# ─────────────────────────── Postgres read ───────────────────────────
import time as _time

_cache = {"data": None, "timers": None, "ts": 0.0}


def fetch_all_rows(force: bool = False):
    """Read time_entries from Postgres; cache for CACHE_TTL seconds."""
    now = _time.time()
    if not force and _cache["data"] is not None and now - _cache["ts"] < CACHE_TTL:
        return _cache["data"], _cache["timers"]

    from db import fetch_all_entries, get_session, init_db

    init_db()
    session = get_session()
    try:
        data = fetch_all_entries(session)
    finally:
        session.close()

    # Active timers previously lived on a Sheet tab; live ClickUp timers can be
    # wired later. Empty list keeps the dashboard happy.
    timers: list = []
    _cache.update(data=data, timers=timers, ts=now)
    return data, timers


# ─────────────────────────── row scoping (the access wall) ───────────────────────────
def scope_rows_own(rows, profile: dict):
    """Only rows belonging to this user (by identity), regardless of lead/admin role."""
    idents = {str(i).lower().strip() for i in profile.get("identities") or []}
    return [r for r in rows if str(r.get("User", "")).lower() in idents]


def scope_rows(rows, profile: dict):
    role = profile["role"]
    if role == "admin":
        return rows
    if role == "lead":
        teams = {t.lower() for t in profile["teams"]}
        idents = set(profile["identities"])
        return [
            r for r in rows
            if str(r.get("Team", "")).lower() in teams
            or str(r.get("User", "")).lower() in idents
        ]
    idents = set(profile["identities"])
    return [r for r in rows if str(r.get("User", "")).lower() in idents]


# ─────────────────────────── app + auth ───────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    from db import init_db
    from ingest.scheduler import start_scheduler, stop_scheduler

    init_db()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Digilatics Time Intelligence", lifespan=lifespan)
# Inner middleware first; SessionMiddleware last = outermost so session exists for CSRF.
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="dti_session",
    same_site="lax",
    https_only=COOKIE_SECURE,
    max_age=60 * 60 * 12,
)

oauth = OAuth()
if GOOGLE_CLIENT_ID and GOOGLE_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile", "hd": ALLOWED_DOMAIN},
    )


def current_user(request: Request) -> Optional[dict]:
    u = request.session.get("user")
    return u if u and u.get("email") else None


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="Not signed in")
    verify_session_binding(request)
    # Session email must still exist in the directory (revoked users kicked out)
    if not user_profile(u["email"]):
        request.session.clear()
        raise HTTPException(status_code=401, detail="Not signed in")
    return u


def require_profile(request: Request) -> dict:
    u = require_user(request)
    p = user_profile(u["email"])
    if not p:
        request.session.clear()
        raise HTTPException(401, "Not signed in")
    return p


@app.post("/api/login")
async def api_login(request: Request, body: LoginBody):
    ip = client_ip(request)
    login_key = (body.username or "").strip().lower()
    login_guard.check(f"ip:{ip}", f"user:{login_key}")

    email = resolve_login_identity(body.username)
    cfg = load_users().get(email) if email else None
    ok = bool(email and cfg and verify_password(body.password, cfg.get("password_hash") or ""))
    if not ok:
        login_guard.record_failure(f"ip:{ip}", f"user:{login_key}")
        raise HTTPException(401, "Invalid username or password")

    login_guard.record_success(f"ip:{ip}", f"user:{login_key}", f"user:{email}")
    p = user_profile(email)
    assert p is not None
    establish_session(request, email=email, name=p.get("name") or email.split("@")[0])
    return {
        "ok": True,
        "email": p["email"],
        "name": p["name"],
        "role": p["role"],
        "teams": p["teams"],
    }


@app.get("/login")
async def login(request: Request):
    # Password form is on "/". Optional Google OAuth redirect when configured.
    # DEV_LOGIN ?as= only on localhost — never a shareable login link.
    if DEV_LOGIN and request.query_params.get("as"):
        if not is_local_dev_request(request):
            raise HTTPException(403, "DEV_LOGIN is local-only")
        who = request.query_params.get("as", "").lower().strip()
        p = user_profile(who)
        if not p:
            raise HTTPException(403, "Unknown user")
        establish_session(request, email=who, name=p.get("name") or who.split("@")[0])
        return RedirectResponse("/")
    if "google" in oauth._clients:
        redirect_uri = f"{BASE_URL}/auth"
        return await oauth.google.authorize_redirect(request, redirect_uri, hd=ALLOWED_DOMAIN)
    return RedirectResponse("/")


@app.get("/auth")
async def auth(request: Request):
    token = await oauth.google.authorize_access_token(request)
    info = token.get("userinfo") or {}
    email = (info.get("email") or "").lower()
    if not info.get("email_verified", False):
        raise HTTPException(403, "Email not verified.")
    if ALLOWED_DOMAIN and not email.endswith("@" + ALLOWED_DOMAIN):
        raise HTTPException(403, f"Only @{ALLOWED_DOMAIN} accounts may sign in.")
    # Directory allowlist — Google alone is not enough
    p = user_profile(email)
    if not p:
        raise HTTPException(403, "Your account is not provisioned. Ask an admin.")
    establish_session(
        request,
        email=email,
        name=info.get("name") or p.get("name") or email.split("@")[0],
    )
    return RedirectResponse("/")


@app.post("/api/logout")
async def api_logout(request: Request):
    request.session.clear()
    return JSONResponse({"ok": True})


@app.get("/logout")
async def logout(request: Request):
    # Prefer POST /api/logout from the UI (CSRF-protected). GET kept for bookmark safety
    # but only clears when same-site navigations send the cookie.
    request.session.clear()
    return RedirectResponse("/")


@app.get("/api/me")
async def api_me(request: Request):
    u = require_user(request)
    p = user_profile(u["email"])
    assert p is not None
    return {
        "email": p["email"],
        "name": u.get("name") or p["name"],
        "role": p["role"],
        "teams": p["teams"],
        "csrf": request.session.get("csrf"),
    }


@app.get("/api/my-data")
async def api_my_data(request: Request):
    """Time entries for the signed-in user only (My Time page)."""
    p = require_profile(request)
    try:
        rows, _ = fetch_all_rows()
    except Exception:
        logging.exception("api_my_data db read failed")
        raise HTTPException(502, "Could not read database")
    return JSONResponse(scope_rows_own(rows, p))


@app.get("/api/data")
async def api_data(request: Request):
    p = require_profile(request)
    try:
        rows, _ = fetch_all_rows()
    except Exception:
        logging.exception("api_data db read failed")
        raise HTTPException(502, "Could not read database")
    return JSONResponse(scope_rows(rows, p))


@app.get("/api/live/today")
async def api_live_today(request: Request, force: bool = False):
    """Today's hours — live ClickUp time + Meet rows from DB (30m cache). ?force=1 bypasses cache."""
    p = require_profile(request)
    try:
        from ingest.live_today import live_today_payload

        return JSONResponse(live_today_payload(p, force=force))
    except Exception:
        logging.exception("api_live_today failed")
        raise HTTPException(502, "Could not load live today stats")


@app.get("/api/active-timers")
async def api_timers(request: Request):
    p = require_profile(request)
    try:
        _, timers = fetch_all_rows()
    except Exception:
        timers = []
    if p["role"] == "admin":
        return JSONResponse(timers)
    if p["role"] == "lead":
        teams = {t.lower() for t in p["teams"]}
        idents = set(p["identities"])
        return JSONResponse([
            t for t in timers
            if str(t.get("Team", "")).lower() in teams
            or str(t.get("User", "")).lower() in idents
        ])
    idents = set(p["identities"])
    return JSONResponse([t for t in timers if str(t.get("User", "")).lower() in idents])


@app.get("/api/sync/status")
async def api_sync_status(request: Request):
    """Admin-only recent sync runs."""
    p = require_profile(request)
    if p["role"] != "admin":
        raise HTTPException(403, "Admin only")
    from sqlalchemy import select
    from db import SyncRun, get_session

    session = get_session()
    try:
        runs = session.execute(select(SyncRun).order_by(SyncRun.id.desc()).limit(20)).scalars().all()
        return [
            {
                "id": r.id,
                "job": r.job,
                "status": r.status,
                "rows_inserted": r.rows_inserted,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "debug": r.debug,
            }
            for r in runs
        ]
    finally:
        session.close()


@app.post("/api/sync/{job}")
async def api_sync_trigger(job: str, request: Request):
    """Admin-only manual sync trigger: clickup | meet | sheet_import."""
    p = require_profile(request)
    if p["role"] != "admin":
        raise HTTPException(403, "Admin only")
    if job not in {"clickup", "meet", "sheet_import"}:
        raise HTTPException(404, "Unknown job")
    if job == "clickup":
        from ingest.clickup import run_sync
    elif job == "meet":
        from ingest.meet import run_sync
    else:
        from ingest.import_sheet import run_import as run_sync
    try:
        debug = run_sync()
        _cache["data"] = None  # bust cache
        return {"ok": True, "debug": debug}
    except Exception:
        logging.exception("sync job %s failed", job)
        raise HTTPException(502, "Sync failed")


# ─────────────────────────── settings / admin directory ───────────────────────────
class AddClientBody(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    aliases: str = ""
    domains: str = ""


class UpdateClientBody(BaseModel):
    original_name: str = Field(min_length=2, max_length=200)
    name: str = Field(min_length=2, max_length=200)
    aliases: str = ""
    domains: str = ""


class AddUserBody(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    name: str = Field(min_length=2, max_length=120)
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=10, max_length=128)
    role: str = "employee"
    teams: str = ""
    identities: str = ""


class ProfileUpdateBody(BaseModel):
    name: str | None = None
    current_password: str | None = None
    new_password: str | None = None


class PredictIdentitiesBody(BaseModel):
    name: str = Field(min_length=2)
    email: str = Field(min_length=3)
    username: str = ""


class ChatMessageBody(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[dict] = Field(default_factory=list)


def _require_admin(profile: dict) -> None:
    if profile.get("role") != "admin":
        raise HTTPException(403, "Admin only")


def _split_csv(s: str) -> list[str]:
    return [p.strip() for p in (s or "").split(",") if p.strip()]


@app.get("/api/settings/meta")
async def api_settings_meta(request: Request):
    p = require_profile(request)
    from settings_store import list_team_options, list_clients_public, list_users_public

    meta = {
        "teams": list_team_options(),
        "clientCount": len(list_clients_public()),
        "userCount": len(list_users_public()),
        "isAdmin": p.get("role") == "admin",
    }
    return JSONResponse(meta)


@app.get("/api/settings/profile")
async def api_settings_profile(request: Request):
    p = require_profile(request)
    return JSONResponse(
        {
            "email": p["email"],
            "name": p.get("name"),
            "username": p.get("username"),
            "role": p.get("role"),
            "teams": p.get("teams") or [],
        }
    )


@app.patch("/api/settings/profile")
async def api_settings_profile_update(request: Request, body: ProfileUpdateBody):
    u = require_user(request)
    email = u["email"].lower()
    users = load_users()
    cfg = users.get(email)
    if not cfg:
        raise HTTPException(404, "User not found")

    if body.new_password:
        if not body.current_password or not verify_password(body.current_password, cfg.get("password_hash") or ""):
            raise HTTPException(400, "Current password is incorrect")
        try:
            validate_password_strength(body.new_password)
        except ValueError as e:
            raise HTTPException(400, str(e))

    from settings_store import update_profile

    try:
        updated = update_profile(
            email=email,
            name=body.name.strip() if body.name is not None else None,
            new_password=body.new_password,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Keep display name in session in sync (does not change data-access identities)
    if body.name is not None:
        name = body.name.strip()
        sess = request.session.get("user") or {}
        sess["name"] = name
        request.session["user"] = sess

    return JSONResponse({"ok": True, "profile": updated})


@app.get("/api/admin/clients")
async def api_admin_clients(request: Request):
    p = require_profile(request)
    _require_admin(p)
    from settings_store import list_clients_public

    return JSONResponse(list_clients_public())


@app.post("/api/admin/clients")
async def api_admin_clients_add(request: Request, body: AddClientBody):
    p = require_profile(request)
    _require_admin(p)
    from settings_store import add_client

    try:
        client = add_client(
            name=body.name.strip(),
            aliases=_split_csv(body.aliases),
            domains=_split_csv(body.domains),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True, "client": client})


@app.patch("/api/admin/clients")
async def api_admin_clients_update(request: Request, body: UpdateClientBody):
    p = require_profile(request)
    _require_admin(p)
    from settings_store import update_client

    try:
        client = update_client(
            original_name=body.original_name.strip(),
            name=body.name.strip(),
            aliases=_split_csv(body.aliases),
            domains=_split_csv(body.domains),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True, "client": client})


@app.delete("/api/admin/clients")
async def api_admin_clients_delete(request: Request, name: str):
    p = require_profile(request)
    _require_admin(p)
    from settings_store import delete_client

    try:
        removed = delete_client(name=name.strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True, "client": removed})


@app.get("/api/admin/users")
async def api_admin_users(request: Request):
    p = require_profile(request)
    _require_admin(p)
    from settings_store import list_users_public

    return JSONResponse(list_users_public())


@app.post("/api/admin/users")
async def api_admin_users_add(request: Request, body: AddUserBody):
    p = require_profile(request)
    _require_admin(p)
    try:
        validate_password_strength(body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    from settings_store import add_user

    try:
        user = add_user(
            email=body.email.strip(),
            name=body.name.strip(),
            username=body.username.strip().lower(),
            password=body.password,
            role=(body.role or "employee").lower(),
            teams=_split_csv(body.teams),
            extra_identities=_split_csv(body.identities),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"ok": True, "user": user})


@app.post("/api/admin/predict-identities")
async def api_predict_identities(request: Request, body: PredictIdentitiesBody):
    p = require_profile(request)
    _require_admin(p)
    from identity_predict import predict_identities

    return JSONResponse(
        predict_identities(
            name=body.name.strip(),
            email=body.email.strip(),
            username=body.username.strip(),
        )
    )


@app.get("/api/chat/status")
async def api_chat_status(request: Request):
    require_user(request)
    from local_llm import OLLAMA_MODEL, list_models, ollama_available

    return JSONResponse(
        {
            "available": ollama_available(),
            "model": OLLAMA_MODEL,
            "models": list_models(),
        }
    )


@app.post("/api/chat")
async def api_chat(request: Request, body: ChatMessageBody):
    p = require_profile(request)
    from chat_assistant import answer_chat

    try:
        result = answer_chat(
            profile=p,
            message=body.message.strip(),
            history=body.history[-8:],
        )
    except Exception:
        logging.exception("chat failed")
        raise HTTPException(502, "Chat failed")
    return JSONResponse(result)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return FileResponse(
        os.path.join(HERE, "static", "app.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


if os.path.isdir(os.path.join(HERE, "static")):
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.get("/logo.svg")
async def logo_svg():
    p = os.path.join(HERE, "static", "logo.svg")
    if os.path.exists(p):
        return FileResponse(p, media_type="image/svg+xml")
    raise HTTPException(404)


@app.get("/logo.png")
async def logo():
    p = os.path.join(HERE, "static", "logo.svg")
    if os.path.exists(p):
        return FileResponse(p, media_type="image/svg+xml")
    p = os.path.join(HERE, "static", "logo.png")
    if os.path.exists(p):
        return FileResponse(p)
    raise HTTPException(404)


@app.get("/healthz")
async def healthz():
    return {"ok": True}
