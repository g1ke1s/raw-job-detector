"""
Unified async LLM client.

Fallback order: gemini_a -> gemini_b -> groq_a -> groq_b -> mistral -> openrouter

complete() walks the chain until one provider succeeds.
Raises RuntimeError only when ALL providers are exhausted.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)

# Per-provider sliding-window rate limits (requests per minute)
_RPM = {
    "gemini_a": 14,
    "gemini_b": 14,
    "groq_a": 29,
    "groq_b": 29,
    "mistral": 9,
    "openrouter": 19,
}

# Simple in-process rate limit tracker: {provider: [timestamps]}
_windows: dict[str, list[float]] = {k: [] for k in _RPM}
_benched: dict[str, float] = {}   # provider -> unbenched_at unix ts


def _is_available(provider: str) -> bool:
    if provider in _benched:
        if time.time() < _benched[provider]:
            return False
        del _benched[provider]
    # sliding window check
    now = time.time()
    window = _windows[provider]
    _windows[provider] = [t for t in window if now - t < 60]
    return len(_windows[provider]) < _RPM[provider]


def _record_call(provider: str) -> None:
    _windows[provider].append(time.time())


def _bench(provider: str, seconds: int = 60) -> None:
    _benched[provider] = time.time() + seconds
    log.info("LLM provider %s benched for %ds", provider, seconds)


# ── per-provider call implementations ────────────────────────────────────

async def _call_gemini(messages: list[dict], key: str, max_tokens: int) -> str:
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    user = next((m["content"] for m in messages if m["role"] == "user"), "")
    contents = []
    if system:
        contents.append({"role": "user", "parts": [{"text": f"[SYSTEM]\n{system}\n[/SYSTEM]"}]})
        contents.append({"role": "model", "parts": [{"text": "Understood."}]})
    contents.append({"role": "user", "parts": [{"text": user}]})

    payload = {
        "contents": contents,
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.0},
    }
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash-lite:generateContent?key={key}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(url, json=payload)
    if r.status_code == 429:
        raise _RateError("gemini 429")
    r.raise_for_status()
    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


async def _call_groq(messages: list[dict], key: str, max_tokens: int) -> str:
    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
        )
    if r.status_code == 429:
        raise _RateError("groq 429")
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


async def _call_mistral(messages: list[dict], max_tokens: int) -> str:
    payload = {
        "model": "mistral-small-latest",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
            json=payload,
        )
    if r.status_code == 429:
        raise _RateError("mistral 429")
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


async def _call_openrouter(messages: list[dict], max_tokens: int) -> str:
    payload = {
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
            json=payload,
        )
    if r.status_code == 429:
        raise _RateError("openrouter 429")
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


class _RateError(Exception):
    pass


# ── Public entry point ────────────────────────────────────────────────────

_CHAIN = [
    ("gemini_a", lambda msgs, mt: _call_gemini(msgs, settings.gemini_api_key, mt)),
    ("gemini_b", lambda msgs, mt: _call_gemini(msgs, settings.gemini_api_key_2, mt)),
    ("groq_a",   lambda msgs, mt: _call_groq(msgs, settings.groq_api_key, mt)),
    ("groq_b",   lambda msgs, mt: _call_groq(msgs, settings.groq_api_key_2, mt)),
    ("mistral",  lambda msgs, mt: _call_mistral(msgs, mt)),
    ("openrouter", lambda msgs, mt: _call_openrouter(msgs, mt)),
]


async def complete(
    messages: list[dict],
    max_tokens: int = 200,
    purpose: str = "",
) -> Optional[str]:
    """
    Walk the fallback chain. Return text on first success.
    Return None if all providers are exhausted (caller decides what to do).
    """
    for provider, fn in _CHAIN:
        if not _is_available(provider):
            continue
        try:
            _record_call(provider)
            result = await fn(messages, max_tokens)
            log.debug("LLM success provider=%s purpose=%s", provider, purpose)
            return result
        except _RateError:
            _bench(provider, 60)
        except Exception as e:
            log.warning("LLM provider %s error: %s", provider, e)
            _bench(provider, 30)

    log.warning("All LLM providers exhausted (purpose=%s)", purpose)
    return None
