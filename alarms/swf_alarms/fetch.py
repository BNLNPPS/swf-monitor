"""REST client for monitor /api/panda/* endpoints.

The alarm engine runs on the monitor host and uses the same public/local
monitor base URL configured in config.toml.
"""
from __future__ import annotations

import httpx


class FetchError(RuntimeError):
    pass


class Client:
    def __init__(self, base_url: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        try:
            r = httpx.get(url, params=params, timeout=self.timeout, verify=True)
        except httpx.HTTPError as e:
            raise FetchError(f"request failed: {url}: {e}") from e
        if r.status_code >= 400:
            raise FetchError(f"{r.status_code} {r.reason_phrase} from {url}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise FetchError(f"non-json response from {url}: {r.text[:200]}") from e

    def list_tasks(self, *, days: int = 1, status: str | None = None,
                   username: str | None = None, taskname: str | None = None,
                   processingtype: str | None = None,
                   limit: int = 50, before_id: int | None = None) -> dict:
        params = {"days": days, "limit": limit}
        for k, v in (("status", status), ("username", username),
                     ("taskname", taskname),
                     ("processingtype", processingtype),
                     ("before_id", before_id)):
            if v is not None:
                params[k] = v
        return self._get("/api/panda/tasks/", params)

    def iter_all_tasks(self, **filters):
        """Paginate through all matching tasks. Yields task dicts."""
        before_id = None
        while True:
            batch = self.list_tasks(before_id=before_id, **filters)
            for item in batch.get("items", []):
                yield item
            if not batch.get("has_more"):
                return
            before_id = batch.get("next_before_id")
            if before_id is None:
                return

    def get_task(self, jeditaskid: int) -> dict:
        return self._get(f"/api/panda/tasks/{jeditaskid}/")

    def activity(self, *, days: int = 1) -> dict:
        return self._get("/api/panda/activity/", {"days": days})
