"""OAuth2 connectors on top of ADK's own auth machinery.

Server side (handled for you by ConnectorRegistry/ConnectorToolset):
  - a user without a token sees one tool, `<connector>__connect`. Calling it
    triggers ADK's standard `adk_request_credential` event;
  - once the client returns the OAuth redirect, ADK exchanges the code, and the
    token is saved in the Runner's `credential_service`, per app + user;
  - on later turns the token is loaded (and refreshed when expired) by ADK's
    CredentialManager and sent as `Authorization: Bearer ...`.

Client side (your app / frontend) — use `pending_auth_requests` and
`PendingAuth.response(...)`. `adk web` already does this with a popup; it uses
its own base URL (e.g. http://localhost:8001) as the redirect URI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from fastapi.openapi.models import OAuth2, OAuthFlowAuthorizationCode, OAuthFlows
from google.adk.agents.context import Context
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.auth.auth_credential import AuthCredential, AuthCredentialTypes, OAuth2Auth
from google.adk.auth.auth_tool import AuthConfig, AuthToolArguments
from google.adk.auth.credential_manager import CredentialManager
from google.adk.auth.credential_service.base_credential_service import BaseCredentialService
from google.adk.events.event import Event
from google.adk.tools.base_authenticated_tool import BaseAuthenticatedTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from .builders import tool_prefix
from .manifest import ConnectorManifest

logger = logging.getLogger(__name__)

REQUEST_CREDENTIAL = "adk_request_credential"


def credential_key(manifest: ConnectorManifest, connection_nonce: str) -> str:
  # Tied to the consented endpoint/auth and to this specific connection, so a
  # connector change or a disconnect+reconnect never reuses an old token.
  return f"adkc_{manifest.id}_{manifest.trust_fingerprint()[:8]}_{connection_nonce}"


def build_auth_config(
    manifest: ConnectorManifest,
    *,
    client_secret: Optional[str],
    redirect_uri: Optional[str],
    connection_nonce: str,
) -> AuthConfig:
  spec = manifest.auth.oauth
  scheme = OAuth2(flows=OAuthFlows(authorizationCode=OAuthFlowAuthorizationCode(
      authorizationUrl=spec.authorization_url,
      tokenUrl=spec.token_url,
      scopes={s: s for s in spec.scopes},
  )))
  raw = AuthCredential(
      auth_type=AuthCredentialTypes.OAUTH2,
      oauth2=OAuth2Auth(
          client_id=spec.client_id, client_secret=client_secret, redirect_uri=redirect_uri
      ),
  )
  return AuthConfig(
      auth_scheme=scheme,
      raw_auth_credential=raw,
      credential_key=credential_key(manifest, connection_nonce),
  )


async def load_access_token(
    auth_config: AuthConfig, readonly_context: ReadonlyContext
) -> tuple[Optional[str], bool]:
  """Loads (and refreshes if needed) the user's token via ADK's CredentialManager.

  Returns (token, just_authorized). `just_authorized` is True when the token came
  from a sign-in completed in this very invocation; the caller must then keep
  `<id>__connect` listed so ADK can resume that pending tool call.
  """
  inv = readonly_context._invocation_context  # pylint: disable=protected-access
  if inv.credential_service is None:
    logger.warning(
        "OAuth connectors need a credential_service on the Runner "
        "(e.g. Runner(..., credential_service=InMemoryCredentialService()))."
    )
    return None, False
  ctx = Context(inv)
  try:
    stored = await inv.credential_service.load_credential(auth_config, ctx)
    credential = await CredentialManager(auth_config.model_copy(deep=True)).get_auth_credential(ctx)
  except Exception as e:  # a broken token endpoint must not break the turn
    logger.warning("loading OAuth token for %s failed: %r", auth_config.credential_key, e)
    return None, False
  if credential and credential.oauth2 and credential.oauth2.access_token:
    return credential.oauth2.access_token, stored is None
  return None, False


class ConnectOAuthTool(BaseAuthenticatedTool):
  """Shown instead of a connector's tools until the user has authorized it."""

  def __init__(self, manifest: ConnectorManifest, auth_config: AuthConfig) -> None:
    super().__init__(
        name=f"{tool_prefix(manifest.id)}_connect",
        description=(
            f"Connect the user's {manifest.name} account so its tools become "
            f"available. Call this when the user wants to use {manifest.name}. "
            "The user will be asked to sign in."
        ),
        auth_config=auth_config,
        response_for_auth_required={
            "status": "pending_user_authorization",
            "message": f"Ask the user to complete sign-in to {manifest.name}.",
        },
    )
    self._connector_name = manifest.name

  def _get_declaration(self) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(name=self.name, description=self.description)

  async def _run_async_impl(
      self, *, args: dict[str, Any], tool_context: ToolContext, credential: AuthCredential
  ) -> Any:
    # Reaching here means ADK exchanged the code and saved the token.
    return {
        "status": "connected",
        "message": f"{self._connector_name} is connected; its tools are now available.",
    }


# --------------------------------------------------------------------------
# Client-side helpers
# --------------------------------------------------------------------------


@dataclass
class PendingAuth:
  """An OAuth sign-in the client must complete. Send the user to `auth_uri`."""

  request_id: str
  auth_uri: str
  args: AuthToolArguments

  def response(self, callback_url: str) -> types.Content:
    """Message to send back to the Runner after the provider redirected to
    `callback_url` (the full URL, including `code` and `state`)."""
    config = self.args.auth_config.model_copy(deep=True)
    config.exchanged_auth_credential.oauth2.auth_response_uri = callback_url
    return types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        id=self.request_id,
        name=REQUEST_CREDENTIAL,
        response=config.model_dump(mode="json", exclude_none=True, by_alias=True),
    ))])


def pending_auth_requests(event: Event) -> list[PendingAuth]:
  """Extracts OAuth sign-in requests from a Runner event."""
  out = []
  for call in event.get_function_calls() if event.content else []:
    if call.name != REQUEST_CREDENTIAL:
      continue
    args = AuthToolArguments.model_validate(call.args)
    cred = args.auth_config.exchanged_auth_credential
    if cred and cred.oauth2 and cred.oauth2.auth_uri:
      out.append(PendingAuth(request_id=call.id, auth_uri=cred.oauth2.auth_uri, args=args))
  return out


# --------------------------------------------------------------------------
# Persistent credential service
# --------------------------------------------------------------------------


class EncryptedSqliteCredentialService(BaseCredentialService):
  """ADK credential_service storing OAuth tokens Fernet-encrypted in SQLite.

  Pass it to `Runner(credential_service=...)` and to
  `ConnectorRegistry(credential_service=...)` so disconnects delete tokens.
  For production, implement BaseCredentialService on your KMS-backed store.
  """

  def __init__(self, path: str, key: str | bytes) -> None:
    import sqlite3

    from cryptography.fernet import Fernet

    self._fernet = Fernet(key)
    self._db = sqlite3.connect(path, check_same_thread=False)
    with self._db:
      self._db.execute(
          "CREATE TABLE IF NOT EXISTS oauth_tokens (app_name TEXT, user_id TEXT, "
          "credential_key TEXT, blob BLOB, PRIMARY KEY (app_name, user_id, credential_key))"
      )

  @staticmethod
  def _ids(ctx) -> tuple[str, str]:
    inv = ctx._invocation_context  # pylint: disable=protected-access
    return inv.app_name, inv.user_id

  async def load_credential(self, auth_config: AuthConfig, callback_context) -> Optional[AuthCredential]:
    app, user = self._ids(callback_context)
    row = self._db.execute(
        "SELECT blob FROM oauth_tokens WHERE app_name=? AND user_id=? AND credential_key=?",
        (app, user, auth_config.credential_key)).fetchone()
    if not row:
      return None
    return AuthCredential.model_validate_json(self._fernet.decrypt(row[0]))

  async def save_credential(self, auth_config: AuthConfig, callback_context) -> None:
    app, user = self._ids(callback_context)
    cred = auth_config.exchanged_auth_credential
    with self._db:
      if cred is None:
        self._db.execute(
            "DELETE FROM oauth_tokens WHERE app_name=? AND user_id=? AND credential_key=?",
            (app, user, auth_config.credential_key))
      else:
        self._db.execute(
            "INSERT OR REPLACE INTO oauth_tokens VALUES (?, ?, ?, ?)",
            (app, user, auth_config.credential_key,
             self._fernet.encrypt(cred.model_dump_json().encode())))

  async def delete_for_user(self, user_id: str, credential_key: str) -> None:
    with self._db:
      self._db.execute(
          "DELETE FROM oauth_tokens WHERE user_id=? AND credential_key=?", (user_id, credential_key))
