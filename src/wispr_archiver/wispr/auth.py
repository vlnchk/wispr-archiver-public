"""Wispr OAuth: file token storage and proactive refresh.

- FileTokenStorage implements the SDK `TokenStorage` protocol. The JSON file lives outside
  the repository, mode 0600 (directory 0700); every write is temp -> fsync -> rename ->
  fsync(dir) under an exclusive flock on a sidecar lock file.
- TokenManager refreshes the token itself before a run (the SDK 2.3.0 does not restore the
  token expiry after a restart and turns a 401 into a full interactive login). The new token
  pair is persisted before it is used. invalid_grant -> ReauthRequired (exit 2).

Tokens are never logged or printed; only sha256 prefixes of refresh tokens are.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import ValidationError

from wispr_archiver import WISPR_MCP_URL
from wispr_archiver.errors import ConfigError, FormatError, ReauthRequired, ToolError
from wispr_archiver.log import event

FILE_VERSION = 1
SCOPE = "openid offline_access"
PRM_URL = "https://api.wisprflow.ai/.well-known/oauth-protected-resource"


def sha8(secret: str | None) -> str | None:
    return None if secret is None else hashlib.sha256(secret.encode()).hexdigest()[:8]


def client_metadata(port: int) -> OAuthClientMetadata:
    return OAuthClientMetadata.model_validate(
        {
            "client_name": "wispr-archiver",
            "redirect_uris": [f"http://127.0.0.1:{port}/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": SCOPE,
        }
    )


def _inside_git_worktree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in path.resolve().parents)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class FileTokenStorage:
    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], datetime] = _utcnow,
        ignore_stored_tokens: bool = False,
        allow_in_repo: bool = False,
        allow_registration: bool = True,
    ) -> None:
        if not allow_in_repo and _inside_git_worktree(path):
            raise ConfigError("token file must live outside any git repository")
        self.path = path
        self.lock_path = path.with_name(path.name + ".lock")
        self._now = now
        # `login` starts from scratch but keeps the client registration.
        self._ignore_tokens = ignore_stored_tokens
        # Unattended runs must never register a new OAuth client (a write on Wispr's side).
        self._allow_registration = allow_registration

    # --- file primitives ----------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self, exclusive: bool) -> Iterator[None]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return {"version": FILE_VERSION}
        if st.st_mode & 0o077:
            raise ConfigError("token file permissions are too open (need 0600)")
        if self.path.parent.stat().st_mode & 0o022:
            raise ConfigError("token directory is writable by group/others (need 0700)")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError("token file is unreadable") from exc
        if not isinstance(data, dict) or data.get("version") != FILE_VERSION:
            raise ConfigError("token file has an unknown format")
        return data

    def _write_unlocked(self, data: dict[str, Any]) -> None:
        raw = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
        tmp = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        tmp.replace(self.path)
        dfd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def read(self) -> dict[str, Any]:
        with self._locked(exclusive=False):
            return self._read_unlocked()

    def update(self, **changes: Any) -> dict[str, Any]:
        with self._locked(exclusive=True):
            data = self._read_unlocked()
            data.update(changes)
            data["version"] = FILE_VERSION
            self._write_unlocked(data)
            return data

    # --- SDK TokenStorage protocol ------------------------------------------------------

    async def get_tokens(self) -> OAuthToken | None:
        if self._ignore_tokens:
            return None
        raw = self.read().get("tokens")
        return None if raw is None else OAuthToken.model_validate(raw)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.store_tokens(tokens)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self.read().get("client_info")
        return None if raw is None else OAuthClientInformationFull.model_validate(raw)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        if not self._allow_registration:
            raise ReauthRequired("client registration is only allowed during login")
        self.update(client_info=client_info.model_dump(mode="json", exclude_none=True))

    # --- extras -------------------------------------------------------------------------

    def store_tokens(self, tokens: OAuthToken) -> None:
        """Persist a token pair (atomically, before first use). Keeps the old refresh token
        if the server did not rotate it."""
        with self._locked(exclusive=True):
            data = self._read_unlocked()
            if tokens.refresh_token is None and data.get("tokens"):
                tokens = tokens.model_copy(
                    update={"refresh_token": data["tokens"].get("refresh_token")}
                )
            now = self._now()
            data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
            data["obtained_at"] = now.isoformat()
            data["expires_at"] = (
                (now + timedelta(seconds=tokens.expires_in)).isoformat()
                if tokens.expires_in is not None
                else None
            )
            data["version"] = FILE_VERSION
            self._write_unlocked(data)
        self._ignore_tokens = False

    def expires_at(self) -> datetime | None:
        value = self.read().get("expires_at")
        return None if value is None else datetime.fromisoformat(value)

    def drop_client_info_unless(self, metadata: OAuthClientMetadata) -> None:
        """Forget a client registration whose redirect URIs differ (e.g. another port)."""
        data = self.read()
        info = data.get("client_info")
        if info is None:
            return
        wanted = [str(u) for u in metadata.redirect_uris or []]
        if [str(u) for u in info.get("redirect_uris") or []] != wanted:
            self.update(client_info=None)


#: Refresh when less than this share of the token lifetime is left (live: 24 h tokens).
LIFETIME_SHARE = 0.25


def effective_margin(data: dict[str, Any], margin: timedelta) -> timedelta:
    """min(configured margin, 25 % of the token lifetime).

    Wispr issues 24 h access tokens (live check 2026-11-18): a fixed 24 h margin would
    rotate the refresh token on every 30-minute run. With 25 % we refresh about every
    18 h and still have ~12 runs to retry a failed refresh before expiry.
    """
    obtained, expires = data.get("obtained_at"), data.get("expires_at")
    if not (obtained and expires):
        return margin
    lifetime = datetime.fromisoformat(expires) - datetime.fromisoformat(obtained)
    if lifetime <= timedelta(0):
        return margin
    return min(margin, lifetime * LIFETIME_SHARE)


@dataclass(frozen=True)
class RefreshOutcome:
    refreshed: bool
    rotated: bool
    expires_in: int | None
    scope: str | None
    old_sha8: str | None
    new_sha8: str | None


class TokenManager:
    def __init__(
        self,
        storage: FileTokenStorage,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.storage = storage
        self.transport = transport
        self._now = now

    def _client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=self.transport, timeout=httpx2.Timeout(30.0))

    async def discover(self) -> dict[str, str]:
        """Official OAuth metadata (RFC 9728 + RFC 8414). Cached in the token file."""
        try:
            async with self._client() as http:
                prm = (await http.get(PRM_URL)).raise_for_status().json()
                resource = str(prm["resource"])
                issuer = str(prm["authorization_servers"][0]).rstrip("/")
                asm_url = f"{issuer}/.well-known/oauth-authorization-server"
                asm = (await http.get(asm_url)).raise_for_status().json()
        except httpx2.HTTPError as exc:
            raise ToolError("OAuth metadata unavailable", kind="transient") from exc
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise FormatError("OAuth metadata does not match the observed contract") from exc
        if resource != WISPR_MCP_URL:
            raise FormatError("protected resource metadata names an unexpected resource")
        endpoints = {
            "token_endpoint": str(asm["token_endpoint"]),
            "issuer": str(asm.get("issuer", issuer)),
            "resource": resource,
        }
        self.storage.update(endpoints=endpoints)
        return endpoints

    async def refresh(
        self, *, force: bool = False, margin: timedelta = timedelta(hours=24)
    ) -> RefreshOutcome:
        data = self.storage.read()
        tokens = data.get("tokens") or {}
        refresh_token = tokens.get("refresh_token")
        info = data.get("client_info") or {}
        if not refresh_token or not info.get("client_id"):
            raise ReauthRequired("no stored refresh token or client registration: run login")
        expires_at = data.get("expires_at")
        if not force and expires_at:
            left = datetime.fromisoformat(expires_at) - self._now()
            if left > effective_margin(data, margin):
                return RefreshOutcome(
                    False,
                    False,
                    None,
                    tokens.get("scope"),
                    sha8(refresh_token),
                    sha8(refresh_token),
                )
        endpoints = data.get("endpoints") or await self.discover()

        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": info["client_id"],
            "resource": endpoints["resource"],
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        method = info.get("token_endpoint_auth_method") or "none"
        secret = info.get("client_secret")
        if method == "client_secret_post" and secret:
            form["client_secret"] = secret
        elif method == "client_secret_basic" and secret:
            raw = f"{info['client_id']}:{secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
            form.pop("client_id")

        try:
            async with self._client() as http:
                resp = await http.post(endpoints["token_endpoint"], data=form, headers=headers)
        except httpx2.HTTPError as exc:
            raise ToolError("token endpoint unreachable", kind="transient") from exc
        if resp.status_code >= 500 or resp.status_code == 429:
            raise ToolError(f"token endpoint HTTP {resp.status_code}", kind="transient")
        if resp.status_code != 200:
            code = "unknown"
            with contextlib.suppress(ValueError, AttributeError):
                code = str(resp.json().get("error", "unknown"))[:40]
            event("token_refresh_rejected", status=f"http_{resp.status_code}", reason=code)
            raise ReauthRequired(f"refresh rejected: HTTP {resp.status_code} {code}")
        try:
            new = OAuthToken.model_validate_json(resp.content)
        except ValidationError as exc:
            raise FormatError("token endpoint response does not match RFC 6749") from exc
        self.storage.store_tokens(new)  # persisted before anything uses it
        stored = self.storage.read()["tokens"]
        outcome = RefreshOutcome(
            refreshed=True,
            rotated=stored.get("refresh_token") != refresh_token,
            expires_in=new.expires_in,
            scope=new.scope,
            old_sha8=sha8(refresh_token),
            new_sha8=sha8(stored.get("refresh_token")),
        )
        event(
            "token_refreshed",
            rotated=outcome.rotated,
            expires_in=outcome.expires_in,
            token_sha8=outcome.new_sha8,
        )
        return outcome
