"""ConnectorRegistry: registration, per-user connections, and toolset resolution."""

from __future__ import annotations

import asyncio
import logging
import secrets as _secrets
from typing import Any, Optional

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_toolset import BaseToolset

from .builders import StaticToolset, build_toolset
from .manifest import TOOL_SEPARATOR, AuthType, ConnectorManifest, check_public_url
from .secrets import InMemorySecretStore, SecretStore
from .store import Connection, ConnectorStore, InMemoryConnectorStore

logger = logging.getLogger(__name__)


class RegistrationError(ValueError):
  pass


class ConnectorRegistry:
  """The single object both the registration API and the ADK runtime talk to.

  Args:
    store: Where manifests and user connections live.
    secrets: Where per-user credentials live.
    allow_private_urls: Permit http:// and private-network hosts. Dev only.
    oauth_redirect_uri: Your app's OAuth callback URL, registered with each
      OAuth provider. Required for `auth.type="oauth2"` connectors.
    credential_service: The same ADK credential_service the Runner uses. Optional;
      when it has `delete_for_user`, disconnecting also deletes the OAuth token.
  """

  def __init__(
      self,
      store: Optional[ConnectorStore] = None,
      secrets: Optional[SecretStore] = None,
      *,
      allow_private_urls: bool = False,
      oauth_redirect_uri: Optional[str] = None,
      credential_service: Optional[Any] = None,
  ) -> None:
    self.store = store or InMemoryConnectorStore()
    self.secrets = secrets or InMemorySecretStore()
    self.allow_private_urls = allow_private_urls
    self.oauth_redirect_uri = oauth_redirect_uri
    self.credential_service = credential_service
    # (connector_id) -> (fingerprint, toolset). Toolsets are shared across users;
    # per-user identity flows through header_provider at call time.
    self._toolsets: dict[str, tuple[str, BaseToolset]] = {}
    # (user_id, connector_id) -> secret, refreshed on every resolve.
    self._creds: dict[tuple[str, str], str] = {}
    self._lock = asyncio.Lock()
    self._warned_no_credential_service = False

  # ------------------------------------------------------------------ admin

  async def register(
      self,
      manifest: ConnectorManifest,
      *,
      trusted: bool = False,
      oauth_client_secret: Optional[str] = None,
  ) -> ConnectorManifest:
    """Registers or updates a connector. Returns the manifest as stored.

    `trusted=False` is what the public API uses: python connectors (arbitrary
    in-process code) are refused, and endpoint URLs must be public https.
    For OAuth2 connectors pass the OAuth app's client secret here; it goes into
    the secret store and only a reference is kept on the manifest.
    """
    self.validate(manifest, trusted=trusted)
    manifest = await self._attach_client_secret(manifest, oauth_client_secret)
    await self.store.upsert_connector(manifest)
    await self._evict(manifest.id)
    return manifest

  async def _attach_client_secret(
      self, manifest: ConnectorManifest, client_secret: Optional[str]
  ) -> ConnectorManifest:
    if manifest.auth.type != AuthType.OAUTH2:
      return manifest
    oauth = manifest.auth.oauth
    existing = await self.store.get_connector(manifest.id)
    old = existing.auth.oauth if existing and existing.auth.oauth else None
    if client_secret:
      ref = await self.secrets.put("__connector__", manifest.id, client_secret)
    elif old and old.client_id == oauth.client_id and old.client_secret_ref:
      ref = old.client_secret_ref  # unchanged OAuth app: keep its secret
    elif oauth.client_secret_ref and await self.secrets.get(oauth.client_secret_ref):
      ref = oauth.client_secret_ref
    else:
      raise RegistrationError("oauth2 connectors require oauth_client_secret")
    if old and old.client_secret_ref and old.client_secret_ref != ref:
      await self.secrets.delete(old.client_secret_ref)
    new_oauth = oauth.model_copy(update={"client_secret_ref": ref})
    return manifest.model_copy(
        update={"auth": manifest.auth.model_copy(update={"oauth": new_oauth})})

  def validate(self, manifest: ConnectorManifest, *, trusted: bool = False) -> None:
    if manifest.kind == "python" and not trusted:
      raise RegistrationError("python connectors can only be registered by an admin")
    urls = [manifest.mcp.url] if manifest.kind == "mcp" else []
    if manifest.auth.type == AuthType.OAUTH2:
      if not self.oauth_redirect_uri:
        raise RegistrationError("registry has no oauth_redirect_uri configured")
      urls += [manifest.auth.oauth.token_url, manifest.auth.oauth.authorization_url]
    for url in urls:
      try:
        check_public_url(url, allow_private=self.allow_private_urls)
      except ValueError as e:
        raise RegistrationError(str(e)) from e

  async def unregister(self, connector_id: str) -> None:
    await self.store.delete_connector(connector_id)
    await self._evict(connector_id)
    for key in [k for k in self._creds if k[1] == connector_id]:
      del self._creds[key]

  async def probe(self, manifest: ConnectorManifest, secret: Optional[str] = None) -> list[str]:
    """Connects once and lists tool names, to validate a connector before saving it."""
    toolset = build_toolset(
        manifest, static_headers=manifest.auth.headers_for(secret) or None,
        allow_private=self.allow_private_urls)
    try:
      tools = await toolset.get_tools_with_prefix(None)
      return [t.name for t in tools]
    finally:
      await toolset.close()

  # ------------------------------------------------------------------ users

  async def connect_user(
      self, user_id: str, connector_id: str, secret: Optional[str] = None
  ) -> Connection:
    manifest = await self.store.get_connector(connector_id)
    if manifest is None:
      raise RegistrationError(f"unknown connector {connector_id!r}")
    old = await self.store.get_connection(user_id, connector_id)
    if manifest.auth.type == AuthType.OAUTH2:
      # No secret up front: the user signs in from the chat via `<id>__connect`.
      # A fresh nonce per connection means a reconnect never revives an old token.
      ref = f"oauth:{_secrets.token_hex(8)}"
      if old and old.secret_ref:
        await self._forget(user_id, manifest, old)
      conn = Connection(user_id=user_id, connector_id=connector_id, secret_ref=ref,
                        consented=manifest.trust_fingerprint())
      await self.store.connect(conn)
      return conn
    if manifest.auth.type != AuthType.NONE and not secret:
      raise RegistrationError(f"connector {connector_id!r} requires a credential")
    ref = await self.secrets.put(user_id, connector_id, secret) if secret else None
    conn = Connection(user_id=user_id, connector_id=connector_id, secret_ref=ref,
                      consented=manifest.trust_fingerprint())
    await self.store.connect(conn)
    if old and old.secret_ref and old.secret_ref != ref:
      await self.secrets.delete(old.secret_ref)
    return conn

  async def disconnect_user(self, user_id: str, connector_id: str) -> None:
    old = await self.store.disconnect(user_id, connector_id)
    if old and old.secret_ref:
      manifest = await self.store.get_connector(connector_id)
      await self._forget(user_id, manifest, old)
    self._creds.pop((user_id, connector_id), None)

  async def _forget(self, user_id: str, manifest: Optional[ConnectorManifest],
                    conn: Connection) -> None:
    if conn.secret_ref.startswith("oauth:"):
      if manifest and hasattr(self.credential_service, "delete_for_user"):
        from .oauth import credential_key
        await self.credential_service.delete_for_user(
            user_id, credential_key(manifest, conn.secret_ref.removeprefix("oauth:")))
    else:
      await self.secrets.delete(conn.secret_ref)

  # ---------------------------------------------------------------- runtime

  async def resolve_for_user(
      self, user_id: str, readonly_context: Optional[ReadonlyContext] = None
  ) -> list[tuple[ConnectorManifest, BaseToolset]]:
    """Returns the user's active connectors with ready ADK toolsets.

    OAuth2 connectors need `readonly_context` (to reach the Runner's
    credential_service). Without a token yet, they resolve to a toolset holding
    only `<id>__connect`.
    """
    resolved: list[tuple[ConnectorManifest, BaseToolset]] = []
    active: set[str] = set()
    for conn in await self.store.list_connections(user_id):
      if not conn.enabled:
        continue
      manifest = await self.store.get_connector(conn.connector_id)
      if manifest is None or not manifest.enabled:
        continue
      if conn.consented != manifest.trust_fingerprint():
        logger.info("connector %s changed since %s connected; reconnect required",
                    manifest.id, user_id)
        continue
      if manifest.auth.type == AuthType.OAUTH2:
        if readonly_context is None or not (conn.secret_ref or "").startswith("oauth:"):
          continue
        if readonly_context._invocation_context.credential_service is None:  # noqa: SLF001
          if not self._warned_no_credential_service:
            logger.warning("OAuth connectors are hidden: the Runner has no credential_service "
                           "(e.g. Runner(..., credential_service=InMemoryCredentialService())).")
            self._warned_no_credential_service = True
          continue
        auth_config = await self._oauth_config(manifest, conn)
        if auth_config is None:
          continue
        from .oauth import ConnectOAuthTool, load_access_token
        token, just_authorized = await load_access_token(auth_config, readonly_context)
        active.add(manifest.id)
        if token is None or just_authorized:
          # Still listed right after sign-in so ADK can resume the pending call.
          resolved.append((manifest, StaticToolset([ConnectOAuthTool(manifest, auth_config)])))
        if token is not None:
          self._creds[(user_id, manifest.id)] = token
          resolved.append((manifest, await self._toolset_for(manifest)))
        continue
      if conn.secret_ref:
        secret = await self.secrets.get(conn.secret_ref)
        if secret is None:
          logger.warning("missing secret for %s/%s", user_id, conn.connector_id)
          continue
        self._creds[(user_id, manifest.id)] = secret
      active.add(manifest.id)
      resolved.append((manifest, await self._toolset_for(manifest)))
    # Drop cached creds for connections that went away.
    for key in [k for k in self._creds if k[0] == user_id and k[1] not in active]:
      del self._creds[key]
    return resolved

  async def is_active(self, user_id: str, connector_id: str) -> bool:
    conn = await self.store.get_connection(user_id, connector_id)
    if conn is None or not conn.enabled:
      return False
    manifest = await self.store.get_connector(connector_id)
    return (manifest is not None and manifest.enabled
            and conn.consented == manifest.trust_fingerprint())

  async def needs_reconnect(self, user_id: str) -> list[str]:
    """Connector ids whose endpoint/auth changed since the user connected."""
    out = []
    for conn in await self.store.list_connections(user_id):
      m = await self.store.get_connector(conn.connector_id)
      if m is not None and conn.consented != m.trust_fingerprint():
        out.append(m.id)
    return out

  def credential(self, user_id: str, connector_id: str) -> Optional[str]:
    return self._creds.get((user_id, connector_id))

  @staticmethod
  def connector_id_of(tool_name: str) -> Optional[str]:
    head, sep, _ = tool_name.partition(TOOL_SEPARATOR)
    return head if sep else None

  async def close(self) -> None:
    async with self._lock:
      toolsets = [ts for _, ts in self._toolsets.values()]
      self._toolsets.clear()
    for ts in toolsets:
      await ts.close()

  # -------------------------------------------------------------- internals

  async def _toolset_for(self, manifest: ConnectorManifest) -> BaseToolset:
    fp = manifest.fingerprint()
    async with self._lock:
      cached = self._toolsets.get(manifest.id)
      if cached and cached[0] == fp:
        return cached[1]
      toolset = build_toolset(
          manifest, header_provider=self._header_provider(manifest),
          allow_private=self.allow_private_urls)
      self._toolsets[manifest.id] = (fp, toolset)
    if cached:
      await cached[1].close()
    return toolset

  async def _oauth_config(self, manifest: ConnectorManifest, conn: Connection):
    from .oauth import build_auth_config
    ref = manifest.auth.oauth.client_secret_ref
    client_secret = await self.secrets.get(ref) if ref else None
    if client_secret is None:
      logger.warning("connector %s has no OAuth client secret", manifest.id)
      return None
    return build_auth_config(
        manifest, client_secret=client_secret, redirect_uri=self.oauth_redirect_uri,
        connection_nonce=conn.secret_ref.removeprefix("oauth:"))

  async def _evict(self, connector_id: str) -> None:
    async with self._lock:
      cached = self._toolsets.pop(connector_id, None)
    if cached:
      await cached[1].close()

  def _header_provider(self, manifest: ConnectorManifest):
    connector_id, auth = manifest.id, manifest.auth

    def provide(ctx: ReadonlyContext) -> dict[str, str]:
      return auth.headers_for(self._creds.get((ctx.user_id, connector_id)))

    return provide
