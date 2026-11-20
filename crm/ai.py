"""Voice -> Persian text -> structured analysis, via OpenRouter.

The transcription logic is copied from the Dastsaz project (ffmpeg to mono 16 kHz mp3, then an audio-capable model on
OpenRouter writes it down) and is deliberately independent of that code base. The OpenRouter key is this project's own.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
from pathlib import Path

import requests
from django.conf import settings

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
ASK_TRANSCRIBE = (
    "این یک مکالمه‌ی تلفنیِ فارسی بین کارشناس پشتیبانی یک شرکت خدمات نظافت و یک مشتری یا یک نیروی نظافت است. "
    "فقط متنِ دقیقِ همان چیزی را که گفته می‌شود بنویس، با نقطه‌گذاریِ درست. هر نوبت صحبت را در یک خط جدا با برچسب "
    "«کارشناس:» یا «طرف مقابل:» بنویس. چیزی اضافه یا خلاصه نکن و جواب نده؛ اگر بخشی نامفهوم است، [نامفهوم] بنویس."
)


class AIError(RuntimeError):
    pass


def _key() -> str:
    key = settings.OPENROUTER_API_KEY
    if not key:
        raise AIError("OPENROUTER_API_KEY تنظیم نشده است")
    return key


def to_mp3(data: bytes) -> bytes:
    with tempfile.TemporaryDirectory() as t:
        src, dst = Path(t) / "in", Path(t) / "out.mp3"
        src.write_bytes(data)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", "16000", "-b:a", "48k", str(dst)],
                       check=True, timeout=300)
        return dst.read_bytes()


def _chat(body: dict, title: str, post=None) -> tuple[str, float]:
    post = post or requests.post
    r = post(OPENROUTER, json=body, timeout=300,
             headers={"Authorization": f"Bearer {_key()}", "X-Title": title})
    try:
        j = r.json()
    except ValueError as e:
        raise AIError(f"پاسخ نامعتبر از OpenRouter (HTTP {r.status_code})") from e
    if "error" in j:
        raise AIError(str(j["error"])[:300])
    txt = ((j.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return txt.strip(), float((j.get("usage") or {}).get("cost") or 0)


def transcribe(data: bytes, post=None) -> tuple[str, float]:
    mp3 = to_mp3(data)
    body = {"model": settings.TRANSCRIBE_MODEL, "temperature": 0, "max_tokens": 8000, "usage": {"include": True},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": ASK_TRANSCRIBE},
                {"type": "input_audio", "input_audio": {"data": base64.b64encode(mp3).decode(), "format": "mp3"}}]}]}
    return _chat(body, "Serviq CRM voice", post=post)


def _parse_json(text: str) -> dict:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < 0:
        raise AIError("تحلیل‌گر خروجی JSON نداد")
    return json.loads(t[start:end + 1])


def ask_json(system: str, user: str, post=None) -> tuple[dict, float]:
    body = {"model": settings.ANALYZE_MODEL, "temperature": 0.1, "max_tokens": 4000, "usage": {"include": True},
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    text, cost = _chat(body, "Serviq CRM analysis", post=post)
    return _parse_json(text), cost


def ask_text(system: str, user: str, post=None) -> tuple[str, float]:
    body = {"model": settings.ANALYZE_MODEL, "temperature": 0.3, "max_tokens": 1500, "usage": {"include": True},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    return _chat(body, "Serviq CRM draft", post=post)
