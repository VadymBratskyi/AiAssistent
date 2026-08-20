from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


class HostHelperError(RuntimeError):
    pass


class HostHelper:
    """Thin client for the Windows host-helper daemon.

    Configured via env: HOST_HELPER_URL (default http://host.docker.internal:47823)
    and HOST_HELPER_TOKEN. When either is missing, .available is False and
    calls fail gracefully so the bot can tell the user to set it up.
    """

    def __init__(self) -> None:
        self.url = os.environ.get("HOST_HELPER_URL", "").strip()
        self.token = os.environ.get("HOST_HELPER_TOKEN", "").strip()

    @property
    def available(self) -> bool:
        return bool(self.url and self.token)

    async def health(self) -> bool:
        if not self.url:
            return False
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                r = await c.get(f"{self.url}/health")
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def trigger_claude_login(self) -> None:
        if not self.available:
            raise HostHelperError("host-helper не налаштований (див. deploy/install-host-helper.ps1)")
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.post(
                    f"{self.url}/claude-login",
                    headers={"Authorization": f"Bearer {self.token}"},
                )
        except httpx.HTTPError as e:
            raise HostHelperError(f"host-helper недоступний: {e}") from e
        if r.status_code == 401:
            raise HostHelperError("host-helper відхилив токен (перевір HOST_HELPER_TOKEN у .env)")
        if r.status_code >= 400:
            raise HostHelperError(f"host-helper {r.status_code}: {r.text[:200]}")


__all__ = ["HostHelper", "HostHelperError"]
