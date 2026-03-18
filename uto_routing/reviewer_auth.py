from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request, Response

from uto_routing.config import RuntimeSettings


@dataclass(frozen=True)
class ReviewerIdentity:
    username: str
    display_name: str
    expires_at: str

    def to_dict(self) -> dict[str, str]:
        return {
            "username": self.username,
            "display_name": self.display_name,
            "expires_at": self.expires_at,
        }


class ReviewerAuthManager:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.enabled = settings.auth_mode == "reviewer"
        self.cookie_name = settings.session_cookie_name
        self.secret = (settings.session_secret or "").encode("utf-8")

    def validate_configuration(self) -> None:
        if not self.enabled:
            return
        missing = []
        if not self.settings.reviewer_username:
            missing.append("UTO_REVIEWER_USERNAME")
        if not self.settings.reviewer_password:
            missing.append("UTO_REVIEWER_PASSWORD")
        if not self.settings.session_secret:
            missing.append("UTO_SESSION_SECRET")
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"Reviewer auth is enabled but missing required settings: {joined}")

    def authenticate_credentials(self, username: str, password: str) -> bool:
        if not self.enabled:
            return True
        assert self.settings.reviewer_username is not None
        assert self.settings.reviewer_password is not None
        username_ok = hmac.compare_digest(username, self.settings.reviewer_username)
        password_ok = hmac.compare_digest(password, self.settings.reviewer_password)
        return username_ok and password_ok

    def issue_session(self, username: str | None = None) -> ReviewerIdentity:
        issued_at = datetime.now(UTC)
        expires_at = issued_at + timedelta(hours=self.settings.session_ttl_hours)
        return ReviewerIdentity(
            username=username or self.settings.reviewer_username or "reviewer",
            display_name=self.settings.reviewer_display_name,
            expires_at=expires_at.isoformat(),
        )

    def set_session_cookie(self, response: Response, identity: ReviewerIdentity, request: Request) -> None:
        token = self._encode(identity)
        response.set_cookie(
            key=self.cookie_name,
            value=token,
            httponly=True,
            secure=self._secure_cookie(request),
            samesite="lax",
            max_age=self.settings.session_ttl_hours * 3600,
            path="/",
        )

    def clear_session_cookie(self, response: Response) -> None:
        response.delete_cookie(key=self.cookie_name, path="/")

    def resolve_identity(self, request: Request) -> ReviewerIdentity | None:
        if not self.enabled:
            return ReviewerIdentity(
                username="anonymous",
                display_name="Anonymous",
                expires_at=(datetime.now(UTC) + timedelta(days=3650)).isoformat(),
            )
        token = request.cookies.get(self.cookie_name)
        return self.resolve_identity_from_token(token)

    def resolve_identity_from_token(self, token: str | None) -> ReviewerIdentity | None:
        if not self.enabled:
            return ReviewerIdentity(
                username="anonymous",
                display_name="Anonymous",
                expires_at=(datetime.now(UTC) + timedelta(days=3650)).isoformat(),
            )
        if not token:
            return None
        return self._decode(token)

    def session_cookie_header(self, identity: ReviewerIdentity) -> str:
        return f"{self.cookie_name}={self._encode(identity)}"

    def decode_cookie_value(self, token: str | None) -> ReviewerIdentity | None:
        if not token:
            return None
        return self._decode(token)

    def _secure_cookie(self, request: Request) -> bool:
        if self.settings.force_secure_cookies:
            return True
        return request.url.scheme == "https"

    def _encode(self, identity: ReviewerIdentity) -> str:
        payload = identity.to_dict()
        payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        payload_b64 = base64.urlsafe_b64encode(payload_json).decode("ascii").rstrip("=")
        signature = hmac.new(self.secret, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{payload_b64}.{signature}"

    def _decode(self, token: str) -> ReviewerIdentity | None:
        try:
            payload_b64, signature = token.split(".", 1)
        except ValueError:
            return None
        expected_signature = hmac.new(self.secret, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_signature):
            return None
        padding = "=" * (-len(payload_b64) % 4)
        try:
            payload_json = base64.urlsafe_b64decode(payload_b64 + padding)
            payload = json.loads(payload_json)
            expires_at = datetime.fromisoformat(payload["expires_at"])
        except Exception:
            return None
        if expires_at < datetime.now(UTC):
            return None
        return ReviewerIdentity(
            username=str(payload["username"]),
            display_name=str(payload.get("display_name", payload["username"])),
            expires_at=payload["expires_at"],
        )

