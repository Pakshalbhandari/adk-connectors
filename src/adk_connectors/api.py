"""HTTP API for registering connectors and connecting users to them.

Identity: `current_user` reads an `X-User-Id` header as a placeholder. Replace it
(via `app.dependency_overrides[current_user] = ...`) with your real auth — the
user id it returns must be the same `user_id` you pass to `Runner.run_async`.
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .manifest import ConnectorManifest
from .registry import ConnectorRegistry, RegistrationError


def current_user(x_user_id: str = Header(...)) -> str:
  return x_user_id


class RegisterRequest(BaseModel):
  manifest: ConnectorManifest
  probe: bool = True  # connect once and list tools before saving
  probe_secret: Optional[str] = None  # API key / bearer / an OAuth access token
  oauth_client_secret: Optional[str] = None  # oauth2 connectors: the OAuth app secret


class ConnectRequest(BaseModel):
  secret: Optional[str] = None


def create_app(registry: ConnectorRegistry) -> FastAPI:
  app = FastAPI(title="ADK Connector Registry")

  @app.post("/connectors")
  async def register(req: RegisterRequest, user: str = Depends(current_user)):
    manifest = req.manifest.model_copy(update={"owner": user})
    existing = await registry.store.get_connector(manifest.id)
    if existing and existing.owner != user:
      raise HTTPException(409, f"connector id {manifest.id!r} is taken")
    tools: list[str] = []
    try:
      registry.validate(manifest)  # URL policy runs before we connect anywhere
      if req.probe and (manifest.auth.type.value != "oauth2" or req.probe_secret):
        try:
          tools = await registry.probe(manifest, req.probe_secret)
        except Exception as e:
          raise RegistrationError(f"probe failed: {e!r}") from e
      await registry.register(manifest, oauth_client_secret=req.oauth_client_secret)
    except RegistrationError as e:
      raise HTTPException(400, str(e)) from e
    return {"id": manifest.id, "tools": tools}

  @app.get("/connectors")
  async def list_connectors():
    return [
        {"id": m.id, "name": m.name, "description": m.description, "kind": m.kind,
         "auth": m.auth.type.value, "owner": m.owner}
        for m in await registry.store.list_connectors()
        if m.enabled
    ]

  @app.delete("/connectors/{connector_id}")
  async def delete_connector(connector_id: str, user: str = Depends(current_user)):
    m = await registry.store.get_connector(connector_id)
    if m is None:
      raise HTTPException(404)
    if m.owner != user:
      raise HTTPException(403)
    await registry.unregister(connector_id)
    return {"deleted": connector_id}

  @app.get("/me/connections")
  async def my_connections(user: str = Depends(current_user)):
    stale = set(await registry.needs_reconnect(user))
    return [
        {"connector_id": c.connector_id, "enabled": c.enabled, "has_secret": bool(c.secret_ref),
         "needs_reconnect": c.connector_id in stale}
        for c in await registry.store.list_connections(user)
    ]

  @app.put("/me/connections/{connector_id}")
  async def connect(connector_id: str, req: ConnectRequest, user: str = Depends(current_user)):
    try:
      await registry.connect_user(user, connector_id, req.secret)
    except RegistrationError as e:
      raise HTTPException(400, str(e)) from e
    return {"connected": connector_id}

  @app.delete("/me/connections/{connector_id}")
  async def disconnect(connector_id: str, user: str = Depends(current_user)):
    await registry.disconnect_user(user, connector_id)
    return {"disconnected": connector_id}

  return app
