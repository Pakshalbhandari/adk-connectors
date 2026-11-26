"""OAuth2 connectors: sign-in from the chat, per-user tokens, refresh, disconnect."""

from __future__ import annotations

import os
import threading
import time

import httpx
import pytest
import uvicorn
from cryptography.fernet import Fernet
from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps.app import App
from google.adk.auth.credential_service.in_memory_credential_service import (
    InMemoryCredentialService,
)
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from adk_connectors import (
    AuthSpec, AuthType, ConnectorManifest, ConnectorPolicyPlugin, ConnectorRegistry,
    ConnectorToolset, EncryptedSqliteCredentialService, McpSpec, OAuthSpec,
    RegistrationError, pending_auth_requests,
)
from examples import demo_oauth_server as oauth
from tests.test_end_to_end import ScriptedLlm, _free_port, mcp_url  # noqa: F401

os.environ["AUTHLIB_INSECURE_TRANSPORT"] = "1"  # the demo provider is plain http
REDIRECT = "https://app.example.com/oauth/callback"


@pytest.fixture(scope="module")
def oauth_url():
  port = _free_port()
  server = uvicorn.Server(uvicorn.Config(oauth.app, host="127.0.0.1", port=port,
                                         log_level="warning"))
  threading.Thread(target=server.run, daemon=True).start()
  while not server.started:
    time.sleep(0.05)
  yield f"http://127.0.0.1:{port}"
  server.should_exit = True


def oauth_manifest(mcp: str, provider: str) -> ConnectorManifest:
  return ConnectorManifest(
      id="crm", name="CRM", kind="mcp", mcp=McpSpec(url=mcp),
      auth=AuthSpec(type=AuthType.OAUTH2, oauth=OAuthSpec(
          authorization_url=f"{provider}/authorize", token_url=f"{provider}/token",
          client_id=oauth.CLIENT_ID, scopes=["notes.read"])),
  )


class Chat:
  """Drives one user's session the way a frontend would."""

  def __init__(self, runner: Runner, user_id: str):
    self.runner, self.user_id, self.session_id = runner, user_id, f"s-{user_id}"
    self.pending = []

  async def send(self, message: types.Content) -> str:
    out = []
    async for ev in self.runner.run_async(
        user_id=self.user_id, session_id=self.session_id, new_message=message):
      self.pending += pending_auth_requests(ev)
      if ev.content and ev.content.parts:
        out += [p.text for p in ev.content.parts if p.text]
    return " ".join(out)

  async def say(self, text: str) -> str:
    return await self.send(types.Content(role="user", parts=[types.Part(text=text)]))

  async def complete_sign_in(self, as_user: str) -> str:
    """Plays the browser: open auth_uri, consent, land on the redirect URL."""
    req = self.pending.pop()
    async with httpx.AsyncClient() as c:
      r = await c.get(req.auth_uri + f"&login={as_user}")
    assert r.status_code == 302
    callback = r.headers["location"]
    assert callback.startswith(REDIRECT)
    return await self.send(req.response(callback))


@pytest.fixture
async def stack(mcp_url, oauth_url, tmp_path):  # noqa: F811
  creds = EncryptedSqliteCredentialService(str(tmp_path / "tokens.db"), Fernet.generate_key())
  registry = ConnectorRegistry(allow_private_urls=True, oauth_redirect_uri=REDIRECT,
                               credential_service=creds)
  await registry.register(oauth_manifest(mcp_url, oauth_url),
                          oauth_client_secret=oauth.CLIENT_SECRET)
  llm = ScriptedLlm(model="s", seen_tools=[])
  agent = LlmAgent(name="a", model=llm, tools=[ConnectorToolset(registry)])
  runner = Runner(
      app=App(name="oauth_app", root_agent=agent, plugins=[ConnectorPolicyPlugin(registry)]),
      session_service=InMemorySessionService(), credential_service=creds,
      auto_create_session=True)
  yield registry, runner, llm, creds
  await registry.close()


async def test_sign_in_from_chat_then_use_tools(stack):
  registry, runner, llm, _ = stack
  await registry.connect_user("alice", "crm")  # no secret: OAuth happens in the chat
  chat = Chat(runner, "alice")

  # 1. Not authorized: only the connect tool is offered. The model calls it.
  llm.call_tool = "crm__connect"
  await chat.say("connect my CRM")
  assert llm.seen_tools[-1] == ["crm__connect"]
  assert len(chat.pending) == 1 and "/authorize?" in chat.pending[0].auth_uri

  # 2. User signs in; ADK exchanges the code; same turn now sees the real tools.
  before = oauth.STATS["code_grants"]
  reply = await chat.complete_sign_in("alice")
  assert oauth.STATS["code_grants"] == before + 1
  assert "connected" in reply
  assert "crm__whoami" in llm.seen_tools[-1]

  # 3. Later turn: the stored token is used; the MCP server sees alice.
  llm.call_tool = "crm__whoami"
  assert "alice" in await chat.say("who am I in the CRM?")


async def test_tokens_are_per_user(stack):
  registry, runner, llm, _ = stack
  for user in ("alice", "bob"):
    await registry.connect_user(user, "crm")
    chat = Chat(runner, user)
    llm.call_tool = "crm__connect"
    await chat.say("connect")
    await chat.complete_sign_in(user)
  llm.call_tool = "crm__whoami"
  assert "bob" in await Chat(runner, "bob").say("who am I?")
  assert "alice" in await Chat(runner, "alice").say("who am I?")
  # carol connected nothing and must not ride on anyone's token
  await registry.connect_user("carol", "crm")
  await Chat(runner, "carol").say("who am I?")
  assert llm.seen_tools[-1] == ["crm__connect"]


async def test_expired_token_is_refreshed(stack, monkeypatch):
  registry, runner, llm, _ = stack
  monkeypatch.setattr(oauth, "TOKEN_TTL", 1)
  await registry.connect_user("alice", "crm")
  chat = Chat(runner, "alice")
  llm.call_tool = "crm__connect"
  await chat.say("connect")
  await chat.complete_sign_in("alice")

  time.sleep(2)
  before = oauth.STATS["refresh_grants"]
  llm.call_tool = "crm__whoami"
  assert "alice" in await chat.say("who am I?")
  assert oauth.STATS["refresh_grants"] > before


async def test_disconnect_deletes_token_and_reconnect_needs_sign_in(stack):
  registry, runner, llm, creds = stack
  await registry.connect_user("alice", "crm")
  chat = Chat(runner, "alice")
  llm.call_tool = "crm__connect"
  await chat.say("connect")
  await chat.complete_sign_in("alice")
  assert creds._db.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0] == 1
  raw = creds._db.execute("SELECT blob FROM oauth_tokens").fetchone()[0]
  assert b"at-alice" not in raw  # encrypted at rest

  await registry.disconnect_user("alice", "crm")
  assert creds._db.execute("SELECT COUNT(*) FROM oauth_tokens").fetchone()[0] == 0
  await registry.connect_user("alice", "crm")
  llm.call_tool = None
  await Chat(runner, "alice").say("hi")
  assert llm.seen_tools[-1] == ["crm__connect"]


async def test_client_secret_kept_out_of_manifest(stack, mcp_url, oauth_url):  # noqa: F811
  registry, *_ = stack
  stored = await registry.store.get_connector("crm")
  assert oauth.CLIENT_SECRET not in stored.model_dump_json()
  assert await registry.secrets.get(stored.auth.oauth.client_secret_ref) == oauth.CLIENT_SECRET

  # Re-registering the same OAuth app keeps its secret without resending it.
  again = await registry.register(oauth_manifest(mcp_url, oauth_url))
  assert again.auth.oauth.client_secret_ref == stored.auth.oauth.client_secret_ref

  fresh = ConnectorRegistry(allow_private_urls=True, oauth_redirect_uri=REDIRECT)
  with pytest.raises(RegistrationError, match="oauth_client_secret"):
    await fresh.register(oauth_manifest(mcp_url, oauth_url))
  with pytest.raises(RegistrationError, match="oauth_redirect_uri"):
    await ConnectorRegistry(allow_private_urls=True).register(
        oauth_manifest(mcp_url, oauth_url), oauth_client_secret="x")


async def test_without_credential_service_oauth_connectors_are_hidden(mcp_url, oauth_url):  # noqa: F811
  registry = ConnectorRegistry(allow_private_urls=True, oauth_redirect_uri=REDIRECT)
  await registry.register(oauth_manifest(mcp_url, oauth_url), oauth_client_secret="s")
  await registry.connect_user("alice", "crm")
  llm = ScriptedLlm(model="s", seen_tools=[])
  runner = Runner(app=App(name="x", root_agent=LlmAgent(
      name="a", model=llm, tools=[ConnectorToolset(registry)])),
      session_service=InMemorySessionService(), auto_create_session=True)
  await Chat(runner, "alice").say("hi")
  assert llm.seen_tools[-1] == []
  await registry.close()
