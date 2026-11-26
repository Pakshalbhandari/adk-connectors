"""Connector manifest: the contract anyone fills in to register a connector."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
from enum import Enum
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, model_validator

# Tool names exposed to the model are "<connector_id>__<tool_name>".
# Gemini function names are limited to 64 chars, so keep ids short.
TOOL_SEPARATOR = "__"
CONNECTOR_ID_PATTERN = r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"  # no "__", no trailing "_"


class AuthType(str, Enum):
  NONE = "none"
  API_KEY = "api_key"  # secret sent as-is in `header_name`
  BEARER = "bearer"  # secret sent as "Authorization: Bearer <secret>"
  OAUTH2 = "oauth2"  # per-user OAuth2 authorization-code flow, run by ADK


class OAuthSpec(BaseModel):
  """OAuth2 authorization-code settings for the connector's OAuth app.

  The client secret is never stored here: pass it to `ConnectorRegistry.register`
  (or the API's `oauth_client_secret`) and only a secret-store reference is kept.
  """

  authorization_url: str
  token_url: str
  client_id: str
  scopes: list[str] = Field(default_factory=list)
  client_secret_ref: Optional[str] = None  # set by the registry


class AuthSpec(BaseModel):
  type: AuthType = AuthType.NONE
  header_name: str = "X-API-Key"
  oauth: Optional[OAuthSpec] = None

  @model_validator(mode="after")
  def _check_oauth(self) -> "AuthSpec":
    if self.type == AuthType.OAUTH2 and self.oauth is None:
      raise ValueError("auth.type='oauth2' requires an `oauth` section")
    return self

  def headers_for(self, secret: Optional[str]) -> dict[str, str]:
    if self.type == AuthType.NONE or not secret:
      return {}
    if self.type in (AuthType.BEARER, AuthType.OAUTH2):
      return {"Authorization": f"Bearer {secret}"}
    return {self.header_name: secret}


class McpSpec(BaseModel):
  url: str
  transport: Literal["streamable_http", "sse"] = "streamable_http"
  timeout: float = 10.0


class OpenApiSpec(BaseModel):
  spec: dict[str, Any]


class PythonSpec(BaseModel):
  """In-process connector. Trusted / admin-registered only (never via the public API).

  `entrypoint` is "package.module:factory". The factory is called with the
  manifest and must return a BaseToolset or a list of BaseTool / callables.
  """

  entrypoint: str


class ConnectorManifest(BaseModel):
  id: str = Field(pattern=CONNECTOR_ID_PATTERN, min_length=2, max_length=24)
  name: str
  description: str = ""
  version: str = "1"
  owner: Optional[str] = None
  kind: Literal["mcp", "openapi", "python"]
  mcp: Optional[McpSpec] = None
  openapi: Optional[OpenApiSpec] = None
  python: Optional[PythonSpec] = None
  auth: AuthSpec = Field(default_factory=AuthSpec)
  tool_allowlist: Optional[list[str]] = None
  require_confirmation: bool = False
  enabled: bool = True

  @model_validator(mode="after")
  def _check_kind(self) -> "ConnectorManifest":
    if getattr(self, self.kind) is None:
      raise ValueError(f"kind={self.kind!r} requires a `{self.kind}` section")
    return self

  def fingerprint(self) -> str:
    """Stable hash; a change means cached toolsets must be rebuilt."""
    raw = json.dumps(self.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

  def trust_fingerprint(self) -> str:
    """Hash of everything that decides where a user's credential is sent.

    A user consents to this when they connect. If the owner later changes the
    endpoint, spec, code or auth, existing connections stop working until each
    user reconnects, so a connector update can't redirect their secrets.
    """
    raw = json.dumps(
        self.model_dump(mode="json", include={"kind", "mcp", "openapi", "python", "auth"}),
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# URL policy for user-supplied endpoints (SSRF protection).
# ---------------------------------------------------------------------------


class UrlPolicyError(ValueError):
  pass


def check_public_url(url: str, *, allow_private: bool = False) -> None:
  """Reject non-http(s) URLs and, unless allowed, hosts on private networks."""
  parsed = urlparse(url)
  if parsed.scheme not in ("http", "https") or not parsed.hostname:
    raise UrlPolicyError(f"unsupported url: {url!r}")
  if allow_private:
    return
  if parsed.scheme != "https":
    raise UrlPolicyError("connector urls must use https")
  host = parsed.hostname
  if host in ("localhost", "metadata", "metadata.google.internal") or host.endswith(
      (".local", ".internal", ".localhost")
  ):
    raise UrlPolicyError(f"host not allowed: {host}")
  try:
    infos = socket.getaddrinfo(host, parsed.port or 443)
  except socket.gaierror as e:
    raise UrlPolicyError(f"cannot resolve {host}") from e
  for info in infos:
    ip = ipaddress.ip_address(info[4][0])
    if not ip.is_global:
      raise UrlPolicyError(f"{host} resolves to non-public address {ip}")
