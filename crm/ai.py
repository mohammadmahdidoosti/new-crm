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
    "«کارشناس:» یا «طرف مقابل:» بنویس؛ برچسب و حرف همان نوبت در یک خط باشند. پیام‌های ضبط‌شده‌ی خودکار تلفن (مثل «مکالمات شما ضبط می‌گردد» یا «در صف انتظار هستید») "
    "را با برچسب «سیستم:» بنویس، نه کارشناس. زمان (مثل ۰۱:۲۳) ننویس. "
    "چیزی اضافه یا خلاصه نکن و جواب نده؛ اگر بخشی نامفهوم است، [نامفهوم] بنویس."
)


class AIError(RuntimeError):
    pass


def _key() -> str:
    key = settings.OPENROUTER_API_KEY
    if not key:
        raise AIError("OPENROUTER_API_KEY تنظیم نشده است")
    return key


def to_mp3(data: bytes, speed: float = 1.0, trim: bool = True) -> bytes:
    """Mono 16 kHz mp3. trim drops pauses longer than 0.6 s (keeping 0.25 s so turns stay apart); speed > 1 plays the
    voice faster. The model bills audio per second, so both make the call cheaper."""
    filters = []
    if trim:
        filters.append("silenceremove=stop_periods=-1:stop_duration=0.6:stop_threshold=-40dB:stop_silence=0.25")
    if speed and abs(speed - 1.0) > 0.01:
        filters.append(f"atempo={speed:g}")
    with tempfile.TemporaryDirectory() as t:
        src, dst = Path(t) / "in", Path(t) / "out.mp3"
        src.write_bytes(data)
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
        if filters:
            cmd += ["-af", ",".join(filters)]
        subprocess.run(cmd + ["-ac", "1", "-ar", "16000", "-b:a", "48k", str(dst)], check=True, timeout=300)
        return dst.read_bytes()


def seconds(data: bytes) -> float:
    """Length of an audio file in seconds (0 if ffprobe cannot tell)."""
    with tempfile.NamedTemporaryFile() as f:
        f.write(data)
        f.flush()
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", f.name],
                           capture_output=True, text=True, timeout=60)
    try:
        return round(float(r.stdout.strip()), 1)
    except ValueError:
        return 0.0


def _usage(j: dict, model: str) -> dict:
    u = j.get("usage") or {}
    pd, cd = u.get("prompt_tokens_details") or {}, u.get("completion_tokens_details") or {}
    return {"model": j.get("model") or model, "prompt": u.get("prompt_tokens") or 0,
            "audio": pd.get("audio_tokens") or 0, "cached": pd.get("cached_tokens") or 0,
            "completion": u.get("completion_tokens") or 0, "reasoning": cd.get("reasoning_tokens") or 0,
            "cost": float(u.get("cost") or 0)}


def _chat(body: dict, title: str, post=None, log: list | None = None, step: str = "") -> tuple[str, float]:
    """One OpenRouter request. If log is given, a token/cost breakdown of the request is appended to it."""
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
    usage = _usage(j, body.get("model", ""))
    if log is not None:
        log.append({"step": step or title, **usage})
    return txt.strip(), usage["cost"]


def transcribe(data: bytes, post=None, log: list | None = None, model: str | None = None,
               speed: float | None = None, trim: bool | None = None) -> tuple[str, float]:
    speed = settings.TRANSCRIBE_SPEED if speed is None else speed
    trim = settings.TRANSCRIBE_TRIM if trim is None else trim
    mp3 = to_mp3(data, speed=speed, trim=trim)
    body = {"model": model or settings.TRANSCRIBE_MODEL, "temperature": 0, "max_tokens": 8000, "usage": {"include": True},
            # writing down speech needs no "thinking"; thinking tokens are billed as output
            "reasoning": {"enabled": False},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": ASK_TRANSCRIBE},
                {"type": "input_audio", "input_audio": {"data": base64.b64encode(mp3).decode(), "format": "mp3"}}]}]}
    if log is not None:
        before = len(log)
        text, cost = _chat(body, "Serviq CRM voice", post=post, log=log, step="transcribe")
        if len(log) > before:
            log[-1].update({"seconds": seconds(mp3), "speed": speed, "trim": trim})
        return normalize_transcript(text), cost
    text, cost = _chat(body, "Serviq CRM voice", post=post)
    return normalize_transcript(text), cost


LABELS = ("کارشناس", "طرف مقابل", "سیستم")
_LABEL = re.compile(r"^\s*\**\s*(کارشناس|طرف مقابل|سیستم)\s*\**\s*[:：]\s*\**\s*(.*)$")


def normalize_transcript(text: str) -> str:
    """One turn per line as «label: words». Models sometimes put the label alone on a line with the words below it,
    or continue a turn on unlabelled lines; both would hide the agent's words from the banned-word check."""
    turns: list[list[str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _LABEL.match(line)
        if m:
            turns.append([m.group(1), m.group(2).strip()])
        elif turns:
            turns[-1][1] = (turns[-1][1] + " " + line).strip()
        else:
            turns.append(["", line])
    return "\n".join(f"{who}: {words}" if who else words for who, words in turns if words or who)


def _parse_json(text: str) -> dict:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < 0:
        raise AIError("تحلیل‌گر خروجی JSON نداد")
    return json.loads(t[start:end + 1])


def ask_json(system: str, user: str, post=None, log: list | None = None, step: str = "analyze") -> tuple[dict, float]:
    body = {"model": settings.ANALYZE_MODEL, "temperature": 0.1, "max_tokens": 4000, "usage": {"include": True},
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    text, cost = _chat(body, "Serviq CRM analysis", post=post, log=log, step=step)
    return _parse_json(text), cost


def ask_text(system: str, user: str, post=None) -> tuple[str, float]:
    body = {"model": settings.ANALYZE_MODEL, "temperature": 0.3, "max_tokens": 1500, "usage": {"include": True},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    return _chat(body, "Serviq CRM draft", post=post)
