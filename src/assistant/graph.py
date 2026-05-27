from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx
import msal

log = logging.getLogger(__name__)

# Microsoft Graph Command Line Tools — a Microsoft first-party, multi-tenant
# PUBLIC client that supports the device-code flow and is pre-consented for
# many delegated Graph scopes. This is what `Connect-MgGraph` uses, so we can
# authenticate without registering our own Azure AD app.
GRAPH_CLI_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"

# Work/school accounts only (not personal MSA).
AUTHORITY = "https://login.microsoftonline.com/organizations"

# Read-only delegated scopes. offline_access/openid/profile are added by msal.
SCOPES = ["Mail.Read", "Calendars.Read", "User.Read"]

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class GraphAuthError(RuntimeError):
    pass


@dataclass
class DeviceFlow:
    user_code: str
    verification_uri: str
    message: str
    _flow: dict


class GraphAuth:
    """Device-code auth with an on-disk MSAL token cache."""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache = msal.SerializableTokenCache()
        if self.cache_path.exists():
            self._cache.deserialize(self.cache_path.read_text(encoding="utf-8"))
        self._app = msal.PublicClientApplication(
            GRAPH_CLI_CLIENT_ID,
            authority=AUTHORITY,
            token_cache=self._cache,
        )

    def _persist(self) -> None:
        if self._cache.has_state_changed:
            self.cache_path.write_text(self._cache.serialize(), encoding="utf-8")

    def has_account(self) -> bool:
        return bool(self._app.get_accounts())

    def start_device_flow(self) -> DeviceFlow:
        flow = self._app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise GraphAuthError(
                f"Device flow could not start: {flow.get('error_description', flow)}"
            )
        return DeviceFlow(
            user_code=flow["user_code"],
            verification_uri=flow["verification_uri"],
            message=flow["message"],
            _flow=flow,
        )

    async def complete_device_flow(self, flow: DeviceFlow) -> None:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, self._app.acquire_token_by_device_flow, flow._flow
        )
        if "access_token" not in result:
            raise GraphAuthError(
                f"{result.get('error')}: {result.get('error_description')}"
            )
        self._persist()

    async def get_token(self) -> str:
        accounts = self._app.get_accounts()
        if not accounts:
            raise GraphAuthError("Not signed in. Run /login_ms first.")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self._app.acquire_token_silent(SCOPES, account=accounts[0]),
        )
        self._persist()
        if not result or "access_token" not in result:
            raise GraphAuthError("Token expired and silent refresh failed. Run /login_ms again.")
        return result["access_token"]


class GraphClient:
    def __init__(self, auth: GraphAuth):
        self.auth = auth

    async def _get(self, path: str, params: dict | None = None) -> dict:
        token = await self.auth.get_token()
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{GRAPH_BASE}{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
        if resp.status_code >= 400:
            raise GraphAuthError(f"Graph {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def me(self) -> dict:
        return await self._get("/me", {"$select": "displayName,mail,userPrincipalName"})

    async def inbox(self, top: int = 10) -> list[dict]:
        data = await self._get(
            "/me/messages",
            {
                "$select": "subject,from,receivedDateTime,isRead,bodyPreview,webLink",
                "$top": str(top),
                "$orderby": "receivedDateTime desc",
            },
        )
        return data.get("value", [])

    async def calendar_view(self, start_iso: str, end_iso: str) -> list[dict]:
        data = await self._get(
            "/me/calendarView",
            {
                "startDateTime": start_iso,
                "endDateTime": end_iso,
                "$select": "subject,start,end,organizer,isOnlineMeeting,onlineMeeting,location,webLink",
                "$orderby": "start/dateTime",
                "$top": "50",
            },
        )
        return data.get("value", [])

    async def event(self, event_id: str) -> dict:
        return await self._get(f"/me/events/{event_id}")


__all__ = ["GraphAuth", "GraphClient", "GraphAuthError", "DeviceFlow", "SCOPES"]
