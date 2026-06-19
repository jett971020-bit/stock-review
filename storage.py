from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests


DATA_FILE = Path(__file__).with_name("stock_review_data.json")
DEFAULT_DATA = {"watchlist": [], "history": [], "reminders": []}
GIST_API = "https://api.github.com/gists"


def _secret(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value:
        return value
    try:
        import streamlit as st

        return str(st.secrets.get(name, default))
    except Exception:
        return default


def _normalized(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "watchlist": data.get("watchlist", []),
        "history": data.get("history", []),
        "reminders": data.get("reminders", []),
    }


def _gist_config() -> tuple[str, str, str] | None:
    token = _secret("GITHUB_TOKEN")
    gist_id = _secret("GIST_ID")
    filename = _secret("GIST_FILENAME", "stock_review_data.json")
    if token and gist_id:
        return token, gist_id, filename
    return None


def load_app_data() -> dict[str, Any]:
    gist = _gist_config()
    if gist:
        token, gist_id, filename = gist
        response = requests.get(
            f"{GIST_API}/{gist_id}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=20,
        )
        response.raise_for_status()
        files = response.json().get("files", {})
        content = files.get(filename, {}).get("content")
        if content:
            return _normalized(json.loads(content))
        return DEFAULT_DATA.copy()

    if not DATA_FILE.exists():
        return DEFAULT_DATA.copy()
    try:
        return _normalized(json.loads(DATA_FILE.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return DEFAULT_DATA.copy()


def save_app_data(data: dict[str, Any]) -> None:
    normalized = _normalized(data)
    gist = _gist_config()
    content = json.dumps(normalized, ensure_ascii=False, indent=2)

    if gist:
        token, gist_id, filename = gist
        response = requests.patch(
            f"{GIST_API}/{gist_id}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={"files": {filename: {"content": content}}},
            timeout=20,
        )
        response.raise_for_status()
        return

    DATA_FILE.write_text(content, encoding="utf-8")
