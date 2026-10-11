"""
Describe a camera frame, using Qwen-VL running in Ollama.

The description is Qwen's answer to prompts/qwen_prompt.txt, followed by its answers to
the one-line questions in prompts/qwen_checks.txt. We ask those separately because Qwen
is much better at spotting a weapon when that is the only thing it is asked about.
"""

import base64
import os
import re
from pathlib import Path

import requests

MODEL = os.getenv("QWEN_MODEL", "qwen3-vl:8b-instruct")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
PROMPTS = Path(__file__).parent / "prompts"
PROMPT = (PROMPTS / "qwen_prompt.txt").read_text()

# Each line of qwen_checks.txt is "Label: question".
CHECKS = []
for line in (PROMPTS / "qwen_checks.txt").read_text().splitlines():
    if line.strip() and not line.startswith("#"):
        label, question = line.split(":", 1)
        CHECKS.append((label.strip(), question.strip()))


def ask_qwen(image_b64, prompt, max_tokens=400):
    """Ask Qwen one question about a base64-encoded image and return its answer."""
    r = requests.post(f"{OLLAMA_URL}/api/generate", json={
        "model": MODEL,
        "prompt": prompt,
        "images": [image_b64],
        "think": False,  # otherwise Qwen3 can use up the whole token budget thinking
        # temperature 0 keeps answers repeatable; the token cap stops it rambling on busy frames
        "options": {"temperature": 0, "num_predict": max_tokens},
        "stream": False,
    }, timeout=600)
    r.raise_for_status()
    return r.json()["response"].strip()


def describe(image_bytes):
    """Describe one frame, with the answers to the focused checks added at the end."""
    image_b64 = base64.b64encode(image_bytes).decode()
    desc = ask_qwen(image_b64, PROMPT)
    if people_count(desc) != 0:  # no point asking about weapons in an empty room
        for label, question in CHECKS:
            answer = ask_qwen(image_b64, question, max_tokens=60)
            desc += f"\n{label}: {' '.join(answer.split())}"
    return desc


def people_count(desc):
    """Read the "People: N" line from a description, or None if Qwen left it out."""
    m = re.search(r"People:\s*(\d+)", desc)
    return int(m.group(1)) if m else None
