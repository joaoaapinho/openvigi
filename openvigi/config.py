"""
Keys and Telegram settings, stored in ~/.config/openvigi/config.json.

The UI writes this file. Environment variables with the same names take priority,
so anyone who prefers export JEV_API_KEY=... never needs the file at all.
"""

import json
import os
from pathlib import Path

PATH = Path(os.getenv("OPENVIGI_CONFIG", Path.home() / ".config" / "openvigi" / "config.json"))
KEYS = ("JEV_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def load():
    """Read the saved settings. A missing or broken file counts as empty."""
    try:
        return json.loads(PATH.read_text())
    except (OSError, ValueError):
        return {}


def get(name):
    """Look a setting up in the environment first, then in the config file."""
    return os.getenv(name) or load().get(name, "")


def where(name):
    """Say where a setting comes from ("env", "file" or None), so the page can show it."""
    if os.getenv(name):
        return "env"
    return "file" if load().get(name) else None


def save(updates):
    """Merge `updates` into the config file. The file is only readable by its owner."""
    data = load()
    data.update({k: v.strip() for k, v in updates.items() if k in KEYS})
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(PATH, 0o600)  # os.open only applies the mode to new files
