"""Lightweight local LLM via Ollama (CPU-friendly, no GPU required)."""

from __future__ import annotations

import json
import logging
import os
import re

import httpx

log = logging.getLogger("local_llm")

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b")
ENABLE_LOCAL_LLM = os.getenv("ENABLE_LOCAL_LLM", "true").lower() == "true"
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "90"))


def is_enabled() -> bool:
    return ENABLE_LOCAL_LLM


def ollama_available() -> bool:
    if not ENABLE_LOCAL_LLM:
        return False
    try:
        with httpx.Client(timeout=3.0) as client:
            r = client.get(f"{OLLAMA_BASE_URL}/api/tags")
            return r.status_code == 200
    except Exception:
        return False


def list_models() -> list[str]:
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(f"{OLLAMA_BASE_URL}/api/tags")
            r.raise_for_status()
            models = r.json().get("models") or []
            return [str(m.get("name") or "") for m in models if m.get("name")]
    except Exception:
        return []


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    max_tokens: int = 512,
    temperature: float = 0.2,
) -> str | None:
    """Call Ollama /api/chat. Returns assistant text or None on failure."""
    if not ENABLE_LOCAL_LLM:
        return None
    payload = {
        "model": model or OLLAMA_MODEL,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": temperature},
    }
    try:
        with httpx.Client(timeout=OLLAMA_TIMEOUT) as client:
            r = client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
            msg = data.get("message") or {}
            text = (msg.get("content") or "").strip()
            return text or None
    except Exception:
        log.exception("ollama chat failed model=%s", model or OLLAMA_MODEL)
        return None


def extract_json_array(text: str) -> list[str]:
    """Parse a JSON string array from model output."""
    if not text:
        return []
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[[\s\S]*?\]", text)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except json.JSONDecodeError:
            pass
    return []
