"""Cost lab: transcribe the same voices with cheaper settings and keep the results next to the real transcript,
so a supervisor can compare quality and price before switching. Nothing here changes a call."""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from django.conf import settings

from . import ai

VARIANTS = {
    "lite": {"label": "Flash-Lite، سرعت عادی", "model": "google/gemini-2.5-flash-lite", "speed": 1.0},
    "flash125": {"label": "Flash، سرعت ۱.۲۵", "model": "google/gemini-2.5-flash", "speed": 1.25},
    "lite125": {"label": "Flash-Lite، سرعت ۱.۲۵", "model": "google/gemini-2.5-flash-lite", "speed": 1.25},
}


def lab_dir() -> Path:
    d = Path(settings.MEDIA_ROOT) / "lab"
    d.mkdir(parents=True, exist_ok=True)
    return d


def results_path() -> Path:
    return lab_dir() / "audio_lab.json"


def load() -> dict:
    p = results_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def save(data: dict) -> None:
    tmp = results_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(results_path())


def run_one(call, variant: str) -> dict:
    v = VARIANTS[variant]
    log: list = []
    with call.audio.open("rb") as f:
        text, cost = ai.transcribe(f.read(), log=log, model=v["model"], speed=v["speed"], trim=True)
    return {"text": text, "cost": cost, "usage": log[-1] if log else {}}


_WORD = re.compile(r"[\w؀-ۿ]+")


def words(text: str) -> list[str]:
    body = [ln.split(":", 1)[1] if ":" in ln else ln for ln in (text or "").splitlines()
            if not ln.strip().startswith("سیستم")]
    return _WORD.findall(" ".join(body).replace("‌", ""))


def similarity(a: str, b: str) -> int:
    """Percent of words two transcripts share in order (speaker labels and phone-system lines ignored)."""
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0
    return round(100 * difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio())


def numbers(text: str) -> set[str]:
    """Digits mentioned (prices, phone numbers, plate numbers), normalised to Latin digits."""
    t = (text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    return set(re.findall(r"\d{2,}", t))
