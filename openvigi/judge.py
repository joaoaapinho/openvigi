"""
Ask Jev whether a described frame shows someone in danger.
image -> Qwen (local) -> text description -> Jev -> threat probability + situation
"""

import json
import os
import time
from pathlib import Path

import requests

from . import config
from .describe import describe

API_URL = os.getenv("JEV_API_URL", "https://api.typesafe.ai/v1/systemone")
MODEL = os.getenv("JEV_MODEL", "jev-latest")
THRESHOLD = float(os.getenv("JEV_THRESHOLD", "0.7"))  # a frame counts as a threat at or above this threshold

QUESTIONS = json.loads((Path(__file__).parent / "prompts" / "jev_questions.json").read_text())


def require_api_key():
    """Stop with a helpful message if no Jev key has been set anywhere."""
    if not config.get("JEV_API_KEY"):
        raise SystemExit("JEV_API_KEY is not set. Get a key at https://typesafe.ai, then add it in openvigi ui (Settings) or run: export JEV_API_KEY=...")


def ask(desc):
    """Send one description to Jev.

    Returns a dict with the threat probability (p), the most likely situation, how sure
    Jev is about it, the probability of every situation, and the input tokens used.
    """
    require_api_key()
    payload = {
        "model": MODEL,
        "state": {"camera": "store security camera", "description": desc},
        "questions": QUESTIONS,
    }
    r = requests.post(API_URL, json=payload, timeout=30, headers={"Authorization": f"Bearer {config.get('JEV_API_KEY')}"})
    if r.status_code in (401, 403):
        raise RuntimeError("Jev rejected the API key. Check JEV_API_KEY, or Settings in `openvigi ui`.")
    r.raise_for_status()
    data = r.json()

    situation = data["answers"]["situation"]
    return {
        "p": float(data["answers"]["threat"]["noul"]),  # noul is Jev's yes/no question type - its answer is P(yes)
        "situation": situation["choice"],
        "situation_confidence": situation.get("confidence"),
        "situation_probs": situation.get("probabilities", {}),
        "tokens": data.get("usage", {}).get("input_tokens"),
    }


def classify(image_bytes):
    """Describe a frame with Qwen, then have Jev judge the description.

    Returns everything ask() does, plus threat (True when p >= THRESHOLD), confidence
    (p rounded), description and latency_s.
    """
    t0 = time.time()
    desc = describe(image_bytes)
    answer = ask(desc)  # we ask even when nobody is in frame, since a wrecked empty shop still matters to most shop owners
    return {
        **answer,
        "threat": answer["p"] >= THRESHOLD,
        "confidence": round(answer["p"], 2),
        "latency_s": round(time.time() - t0, 2),
        "description": desc,
    }
