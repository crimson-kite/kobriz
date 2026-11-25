"""Task-provider client for the kobriz ADLC plugin (REST mode).

The existing task management system is the source of truth. This client only *reads*
task data and *updates* status/comments through the user's own configured endpoints.
It never creates a parallel task store.

MCP mode is intentionally NOT implemented here: MCP tools are invoked by Claude (the
manager agent) via tool discovery, so duplicating them in a script would be wrong.
See ``skills/task-discovery/SKILL.md``.

Only the Python standard library is used (``urllib``) so there is no install step.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from config import Config, ConfigError

# Canonical field names the rest of the plugin relies on. The provider's own field
# names are translated into these via Config.field_map.
_CANONICAL_FIELDS = ("id", "title", "description", "status", "acceptance_criteria",
                     "dependencies", "kind", "labels")


@dataclass
class Task:
    """A task normalized to the plugin's canonical schema."""

    id: str
    title: str = ""
    description: str = ""
    status: str = ""
    acceptance_criteria: Any = None
    dependencies: List[str] = field(default_factory=list)
    kind: Optional[str] = None          # backend | frontend | shared | coordinated | None
    labels: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "acceptance_criteria": self.acceptance_criteria,
            "dependencies": self.dependencies,
            "kind": self.kind,
            "labels": self.labels,
        }


class TaskClientError(RuntimeError):
    """Raised when a task-provider request fails."""


class TransientError(TaskClientError):
    """A network-level failure that may succeed on retry (connection reset, DNS, timeout)."""


_IDEMPOTENT = {"GET", "HEAD", "PUT", "PATCH", "DELETE"}
_MAX_PAGES = 50          # hard stop for runaway/looping pagination
_MAX_RETRY_WAIT = 60.0   # never sleep longer than this for one retry (Retry-After is capped)


class RestTaskClient:
    """HTTP client for a REST task provider, driven entirely by Config.

    The ``opener`` argument allows tests to inject a fake transport without hitting
    the network.
    """

    def __init__(self, config: Config, opener: Optional[Any] = None,
                 sleep: Optional[Any] = None) -> None:
        if config.source_type != "rest":
            raise ConfigError("RestTaskClient requires ADLC_TASK_SOURCE_TYPE=rest")
        errors = config.validate()
        if errors:
            raise ConfigError("; ".join(errors))
        self.config = config
        # ``opener`` is any callable(request) -> (status:int, body:str); defaults to urllib.
        self._opener = opener or self._urllib_open
        self._sleep = sleep or time.sleep
        self._last_headers: Dict[str, str] = {}   # response headers of the latest urllib call

    # -- HTTP plumbing ----------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        cfg = self.config
        if cfg.auth_type == "bearer" and cfg.api_token:
            headers["Authorization"] = f"Bearer {cfg.api_token}"
        elif cfg.auth_type == "apikey" and cfg.api_token:
            headers[cfg.api_key_header] = cfg.api_token
        headers.update(cfg.extra_headers)
        return headers

    def _url(self, path: str) -> str:
        return self.config.api_base_url.rstrip("/") + "/" + path.lstrip("/")

    def _urllib_open(self, request: urllib.request.Request):
        """Return (status, body) for ANY HTTP status so _request can decide whether to retry."""
        self._last_headers = {}
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:  # noqa: S310 (configured URL)
                self._last_headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            self._last_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
            body = exc.read().decode("utf-8", "replace") if exc.fp else ""
            return exc.code, body
        except urllib.error.URLError as exc:
            raise TransientError(f"Could not reach task provider: {exc.reason}") from exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise TransientError(f"Task provider connection failed: {exc}") from exc

    def _retry_wait(self, attempt: int) -> float:
        """Seconds to wait before retry ``attempt`` (0-based): Retry-After if sent, else
        exponential backoff. Always capped so a hostile header cannot stall the run."""
        raw = self._last_headers.get("retry-after", "")
        try:
            wait = float(raw)
        except (TypeError, ValueError):
            wait = 0.5 * (2 ** attempt)
        return max(0.0, min(wait, _MAX_RETRY_WAIT))

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        """One logical request with bounded retries.

        429 is always safe to retry (the provider did not process it). 5xx and network errors
        are retried only for idempotent methods: re-sending a create could duplicate a task.
        """
        data = json.dumps(body).encode("utf-8") if body is not None else None
        retries = max(0, self.config.task_http_retries)
        for attempt in range(retries + 1):
            req = urllib.request.Request(self._url(path), data=data, method=method,
                                         headers=self._headers())
            self._last_headers = {}
            last = attempt == retries
            try:
                status, text = self._opener(req)
            except TransientError:
                if last or method not in _IDEMPOTENT:
                    raise
                self._sleep(self._retry_wait(attempt))
                continue
            retryable = status == 429 or (status >= 500 and method in _IDEMPOTENT)
            if status >= 400:
                if retryable and not last:
                    self._sleep(self._retry_wait(attempt))
                    continue
                raise TaskClientError(f"HTTP {status} from {path}: {text[:500]}")
            if not text:
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise TaskClientError(f"Provider returned non-JSON from {path}: {text[:200]}") from exc
        raise TaskClientError(f"Request to {path} failed after {retries + 1} attempts")  # pragma: no cover

    # -- Response mapping -------------------------------------------------------

    def _map_task(self, obj: Dict[str, Any]) -> Task:
        """Translate a provider task object into the canonical Task via field_map."""
        fm = self.config.field_map
        # Invert: canonical -> provider key (default to identity for canonical names).
        inverse = {canon: prov for prov, canon in fm.items()}

        def pick(canon: str, default: Any = None) -> Any:
            prov_key = inverse.get(canon, canon)
            return obj.get(prov_key, obj.get(canon, default))

        deps = pick("dependencies", []) or []
        if isinstance(deps, str):
            deps = [d.strip() for d in deps.split(",") if d.strip()]
        labels = pick("labels", []) or []
        if isinstance(labels, str):
            labels = [s.strip() for s in labels.split(",") if s.strip()]

        task_id = pick("id")
        if task_id is None:
            raise TaskClientError(f"Task object has no id field (checked {inverse.get('id', 'id')!r})")

        return Task(
            id=str(task_id),
            title=str(pick("title", "") or ""),
            description=str(pick("description", "") or ""),
            status=str(pick("status", "") or ""),
            acceptance_criteria=pick("acceptance_criteria"),
            dependencies=[str(d) for d in deps],
            kind=(str(pick("kind")) if pick("kind") is not None else None),
            labels=[str(x) for x in labels],
            raw=obj,
        )

    # -- Public API -------------------------------------------------------------

    @staticmethod
    def _items_of(payload: Any) -> Any:
        if isinstance(payload, dict):
            for key in ("tasks", "data", "items", "results"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        return payload

    @staticmethod
    def _next_link(payload: Any) -> Optional[str]:
        """A next-page URL/path from common envelope shapes, if the provider sent one."""
        if not isinstance(payload, dict):
            return None
        for container in (payload, payload.get("links"), payload.get("_links"), payload.get("meta")):
            if isinstance(container, dict):
                for key in ("next", "next_page", "next_url"):
                    val = container.get(key)
                    if isinstance(val, dict):
                        val = val.get("href")
                    if isinstance(val, str) and val:
                        return val
        return None

    def _same_origin_path(self, link: str) -> str:
        """Accept a relative path or an absolute URL on the configured host only, so the
        auth token is never sent to a server named inside a response body."""
        base = urllib.parse.urlsplit(self.config.api_base_url)
        parts = urllib.parse.urlsplit(link)
        if parts.scheme or parts.netloc:
            if (parts.scheme, parts.netloc) != (base.scheme, base.netloc):
                raise TaskClientError(f"Refusing to follow next-page link to another host: {link}")
            return urllib.parse.urlunsplit(("", "", parts.path, parts.query, ""))
        return link

    def list_tasks(self) -> List[Task]:
        """Fetch ALL tasks, following next-links or page numbers (bounded, loop-safe)."""
        path = self.config.list_path
        param = self.config.task_page_param
        page, seen_pages, seen_ids = 1, set(), set()
        tasks: List[Task] = []
        for _ in range(_MAX_PAGES):
            if param:
                sep = "&" if "?" in path else "?"
                req_path = f"{path}{sep}{urllib.parse.urlencode({param: page})}"
            else:
                req_path = path
            if req_path in seen_pages:
                break  # provider handed back a page we already read
            seen_pages.add(req_path)
            payload = self._request(self.config.list_method, req_path)
            items = self._items_of(payload)
            if not isinstance(items, list):
                raise TaskClientError("Task list response was not a JSON array or recognized envelope")
            fresh = [self._map_task(o) for o in items if isinstance(o, dict)]
            new = [t for t in fresh if t.id not in seen_ids]
            tasks.extend(new)
            seen_ids.update(t.id for t in new)
            link = self._next_link(payload)
            if link:
                path = self._same_origin_path(link)
                param = ""        # a next-link already carries its own paging state
                continue
            if param and fresh and new:
                page += 1
                continue
            break
        return tasks

    def get_task(self, task_id: str) -> Task:
        """Fetch full details for a single task."""
        path = self.config.details_path.format(task_id=urllib.parse.quote(task_id, safe=""))
        payload = self._request("GET", path)
        if isinstance(payload, dict) and "task" in payload and isinstance(payload["task"], dict):
            payload = payload["task"]
        if not isinstance(payload, dict):
            raise TaskClientError("Task details response was not a JSON object")
        return self._map_task(payload)

    def update_status(self, task_id: str, logical_status: str,
                      extra: Optional[Dict[str, Any]] = None) -> Any:
        """Update task status. ``logical_status`` is a lifecycle state; it is mapped to
        the provider's own value via Config.status_map before sending."""
        provider_status = self.config.status_map.get(logical_status, logical_status)
        path = self.config.status_path.format(task_id=urllib.parse.quote(task_id, safe=""))
        body: Dict[str, Any] = {"status": provider_status}
        if extra:
            body.update(extra)
        return self._request(self.config.status_method, path, body)

    def create_subtask(self, parent_id: str, title: str, description: str = "",
                       extra: Optional[Dict[str, Any]] = None) -> Task:
        """Create a subtask of ``parent_id`` in the provider and return it mapped.

        The body uses the provider's own field names (translated via field_map) and links
        the child to its parent via Config.parent_field. This writes to the user's existing
        task system — it does NOT create a parallel store. Callers must only invoke this
        after explicit user approval (see the task-splitting skill).
        """
        fm = self.config.field_map
        inverse = {canon: prov for prov, canon in fm.items()}  # canonical -> provider key

        def key(canon: str) -> str:
            return inverse.get(canon, canon)

        body: Dict[str, Any] = {
            key("title"): title,
            self.config.parent_field: parent_id,
        }
        if description:
            body[key("description")] = description
        if extra:
            body.update(extra)

        path = (self.config.subtask_path or self.config.list_path)
        payload = self._request(self.config.subtask_method, path, body)
        if isinstance(payload, dict):
            obj = payload.get("task", payload) if isinstance(payload.get("task"), dict) else payload
            try:
                return self._map_task(obj)
            except TaskClientError:
                # Provider returned something without a recognizable id; surface the raw body.
                return Task(id=f"(created under {parent_id})", title=title, raw=obj)
        return Task(id=f"(created under {parent_id})", title=title, raw={"response": payload})

    def add_comment(self, task_id: str, text: str) -> Any:
        """Post a comment to the task, if the provider supports it."""
        if not self.config.comment_path:
            raise TaskClientError(
                "Comments are not configured (set ADLC_TASK_COMMENT_PATH to enable)"
            )
        path = self.config.comment_path.format(task_id=urllib.parse.quote(task_id, safe=""))
        return self._request("POST", path, {"body": text})
