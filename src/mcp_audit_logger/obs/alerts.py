"""AlertManager client (Prometheus AlertManager v2 API).

Used by `obs_investigate` to surface active and recently-resolved alerts
that touch a given service. Alert filtering is best-effort by service
label — we accept either `service` or `service_name` as the label name
(both are common in operator deployments).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx


class AlertManagerClient:
    backend_name = "alertmanager"

    def __init__(
        self,
        url: str,
        *,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._timeout = timeout
        self._client = client

    def _make_client(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(base_url=self._url, timeout=self._timeout)

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        owns = self._client is None
        client = self._make_client()
        try:
            url = path if self._client is None else f"{self._url}{path}"
            return await client.get(url, params=params)
        finally:
            if owns:
                await client.aclose()

    async def active_alerts(self, service: str) -> list[dict[str, Any]]:
        """Return active alerts whose labels include the given service name.

        Filters in-process rather than via AlertManager's filter syntax —
        AM doesn't have a clean OR across multiple label names, and we
        want to match either `service` or `service_name`.
        """
        resp = await self._get("/api/v2/alerts", {"active": "true", "silenced": "false"})
        resp.raise_for_status()
        body = resp.json()
        return [a for a in (body or []) if _alert_matches_service(a, service)]

    async def recently_resolved(
        self, service: str, *, since: datetime, until: datetime
    ) -> list[dict[str, Any]]:
        """Return alerts that resolved within the given window (best-effort).

        AlertManager keeps recently-resolved alerts in memory for a short
        window; we list them and filter to those whose endsAt falls in
        [since, until].
        """
        resp = await self._get("/api/v2/alerts", {"active": "false", "silenced": "false"})
        resp.raise_for_status()
        body = resp.json()
        out: list[dict[str, Any]] = []
        for a in body or []:
            if not _alert_matches_service(a, service):
                continue
            ends_at = a.get("endsAt")
            if not ends_at:
                continue
            try:
                ends = datetime.fromisoformat(str(ends_at).replace("Z", "+00:00"))
            except ValueError:
                continue
            if since <= ends <= until:
                out.append(a)
        return out


def _alert_matches_service(alert: dict[str, Any], service: str) -> bool:
    labels = alert.get("labels") or {}
    for key in ("service", "service_name", "service.name", "job"):
        if labels.get(key) == service:
            return True
    return False
