"""Meeting-intake ledger for the kobriz ADLC plugin.

Records which tracker tasks were created from which meeting action items, so re-running
``/kobriz:intake`` on the same meeting never creates a duplicate. Tracker creates are not
idempotent (a retried create makes a second task), so the manager checks here first.

Stored under ``<repo>/.kobriz/intake/``. Holds titles and ids only, never meeting text.
"""

from __future__ import annotations

import hashlib
import json
import os
import difflib
import re
import unicodedata
from typing import Any, Dict, Optional

from config import Config, atomic_write_json, kobriz_dir, now_iso


def draft_key(title: str) -> str:
    """Stable key for an action item: case, punctuation and spacing do not matter."""
    folded = unicodedata.normalize("NFKC", title).casefold()
    norm = re.sub(r"[\W_]+", " ", folded).strip() or title.strip()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def _norm(title: str) -> str:
    folded = unicodedata.normalize("NFKC", title).casefold()
    return re.sub(r"[\W_]+", " ", folded).strip()


def find_similar(title: str, existing: list, threshold: float) -> list:
    """Existing tracker tasks whose titles look like ``title``.

    ``existing`` is a list of {"id", "title"} the manager read from the tracker. Matches on a
    difflib ratio over normalized titles, or when one title contains the other (>= 3 words),
    so "Add rate limiting to login API" matches "Rate limiting for login API". A hint for the
    human to confirm, never an automatic skip.
    """
    want = _norm(title)
    hits = []
    for item in existing:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        have = _norm(str(item["title"]))
        ratio = difflib.SequenceMatcher(None, want, have).ratio()
        contained = len(want.split()) >= 3 and len(have.split()) >= 3 and (want in have or have in want)
        if ratio >= threshold or contained:
            hits.append({"id": str(item.get("id", "")), "title": str(item["title"]),
                         "similarity": round(max(ratio, 0.9 if contained else 0.0), 2)})
    return sorted(hits, key=lambda h: -h["similarity"])


class IntakeStore:
    def __init__(self, config: Config) -> None:
        self.dir = os.path.join(kobriz_dir(config), "intake")
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, meeting_id: str) -> str:
        if not str(meeting_id).strip():
            raise ValueError("meeting id is required")
        name = hashlib.sha256(str(meeting_id).encode("utf-8")).hexdigest()[:24]
        return os.path.join(self.dir, f"{name}.json")

    def show(self, meeting_id: str) -> Dict[str, Any]:
        path = self._path(meeting_id)
        if not os.path.isfile(path):
            return {"meeting_id": str(meeting_id), "items": {}}
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def check(self, meeting_id: str, title: str) -> Optional[Dict[str, Any]]:
        """The ledger entry if this action item already became a task, else None."""
        return self.show(meeting_id)["items"].get(draft_key(title))

    def record(self, meeting_id: str, title: str, task_id: str,
               meeting_title: str = "", source_quote: str = "",
               max_items: int = 0) -> Dict[str, Any]:
        data = self.show(meeting_id)
        key = draft_key(title)
        existing = data["items"].get(key)
        if existing and existing["task_id"] != str(task_id):
            raise ValueError(f"action item already recorded as task {existing['task_id']}")
        if not existing and max_items and len(data["items"]) >= max_items:
            raise ValueError(f"this meeting already produced {len(data['items'])} tasks "
                             f"(ADLC_INTAKE_MAX_ITEMS={max_items}); raise the cap deliberately "
                             f"if that is intended")
        if meeting_title:
            data["meeting_title"] = meeting_title
        entry = {"title": title, "task_id": str(task_id), "at": now_iso()}
        if source_quote.strip():   # short pointer for the reviewer; never a transcript dump
            entry["source_quote"] = " ".join(source_quote.split())[:200]
        data["items"][key] = entry
        atomic_write_json(self._path(meeting_id), data)
        return entry
