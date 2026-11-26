"""End-to-end: real MCP server + real ADK 2.15 Runner, scripted fake LLM."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import AsyncGenerator

import httpx
import pytest
import uvicorn
from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps.app import App
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types

from adk_connectors import (
    AuthSpec, AuthType, ConnectorManifest, ConnectorPolicyPlugin, ConnectorRegistry,
    ConnectorToolset, McpSpec, PythonSpec, RegistrationError, SqliteConnectorStore,
)
from adk_connectors.api import create_app
from examples import demo_mcp_server


# --------------------------------------------------------------------- fixtures

def _free_port() -> int:
  with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    return s.getsockname()[1]


@pytest.fixture(scope="session")
def mcp_url():
  port = _free_port()
  _, app = demo_mcp_server.build_app()
  server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
  t = threading.Thread(target=server.run, daemon=True)
  t.start()
  for _ in range(100):
    if server.started:
      break
    time.sleep(0.05)
  yield f"http://127.0.0.1:{port}/mcp"
  server.should_exit = True
  t.join(timeout=5)


def notes_manifest(url: str) -> ConnectorManifest:
  return ConnectorManifest(
      id="notes", name="Notes", kind="mcp", mcp=McpSpec(url=url),
      auth=AuthSpec(type=AuthType.API_KEY, header_name="X-API-Key"),
  )


def clock_tools(manifest):
  def current_time() -> str:
    """Return the current server time."""
    return "12:00"
  return [current_time]


class ScriptedLlm(BaseLlm):
  """Calls `call_tool` once (if offered), then answers with the tool result.

  Records which tool names the runner offered on each request.
  """

  call_tool: str | None = None
  call_args: dict = {}
  seen_tools: list[list[str]] = []

  async def generate_content_async(
      self, llm_request: LlmRequest, stream: bool = False
  ) -> AsyncGenerator[LlmResponse, None]:
    self.seen_tools.append(sorted(llm_request.tools_dict))
    last = llm_request.contents[-1]
    responses = [p.function_response for p in last.parts or [] if p.function_response]
    if responses:
      text = f"result: {responses[0].response}"
      yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))
      return
    if self.call_tool and self.call_tool in llm_request.tools_dict:
      fc = types.FunctionCall(name=self.call_tool, args=self.call_args)
      yield LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=fc)]))
      return
    yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="no tool")]))


@pytest.fixture
async def setup(mcp_url, tmp_path):
  registry = ConnectorRegistry(
      SqliteConnectorStore(str(tmp_path / "c.db")), allow_private_urls=True
  )
  await registry.register(notes_manifest(mcp_url))
  await registry.register(
      ConnectorManifest(id="clock", name="Clock", kind="python",
                        python=PythonSpec(entrypoint="tests.test_end_to_end:clock_tools")),
      trusted=True,
  )
  llm = ScriptedLlm(model="scripted", seen_tools=[])
  agent = LlmAgent(name="assistant", model=llm, instruction="help",
                   tools=[ConnectorToolset(registry)])
  app = App(name="connectors_demo", root_agent=agent, plugins=[ConnectorPolicyPlugin(registry)])
  runner = Runner(app=app, session_service=InMemorySessionService(), auto_create_session=True)
  yield registry, runner, llm
  await registry.close()


async def ask(runner: Runner, user_id: str, text: str = "hi") -> str:
  out = []
  async for ev in runner.run_async(
      user_id=user_id, session_id=f"s-{user_id}-{time.monotonic_ns()}",
      new_message=types.Content(role="user", parts=[types.Part(text=text)]),
  ):
    if ev.content and ev.content.parts:
      out += [p.text for p in ev.content.parts if p.text]
  return " ".join(out)


# ------------------------------------------------------------------------ tests

async def test_tools_are_per_user_and_picked_up_live(setup):
  registry, runner, llm = setup
  await registry.connect_user("alice", "notes", secret="key-alice")

  await ask(runner, "alice")
  await ask(runner, "bob")
  assert llm.seen_tools[0] == ["notes__add_note", "notes__list_notes", "notes__whoami"]
  assert llm.seen_tools[1] == []

  # Bob connects two connectors; his very next run sees them, no restart.
  await registry.connect_user("bob", "notes", secret="key-bob")
  await registry.connect_user("bob", "clock")
  llm.seen_tools.clear()
  await ask(runner, "bob")
  assert llm.seen_tools[0] == [
      "clock__current_time", "notes__add_note", "notes__list_notes", "notes__whoami"]


async def test_each_user_calls_with_their_own_credential(setup):
  registry, runner, llm = setup
  await registry.connect_user("alice", "notes", secret="key-alice")
  await registry.connect_user("bob", "notes", secret="key-bob")
  llm.call_tool = "notes__whoami"
  assert "alice" in await ask(runner, "alice")
  assert "bob" in await ask(runner, "bob")


async def test_disconnect_removes_tools(setup):
  registry, runner, llm = setup
  await registry.connect_user("alice", "notes", secret="key-alice")
  await registry.disconnect_user("alice", "notes")
  await ask(runner, "alice")
  assert llm.seen_tools[-1] == []


async def test_broken_connector_is_isolated(setup):
  registry, runner, llm = setup
  await registry.register(ConnectorManifest(
      id="dead", name="Dead", kind="mcp", mcp=McpSpec(url="http://127.0.0.1:1/mcp", timeout=1)))
  await registry.connect_user("alice", "dead")
  await registry.connect_user("alice", "clock")
  await ask(runner, "alice")
  assert llm.seen_tools[-1] == ["clock__current_time"]


async def test_wrong_credential_tools_hidden(setup):
  registry, runner, llm = setup
  await registry.connect_user("alice", "notes", secret="wrong")
  await ask(runner, "alice")
  assert llm.seen_tools[-1] == []


async def test_registration_policy():
  registry = ConnectorRegistry()
  with pytest.raises(RegistrationError):  # python requires admin
    await registry.register(ConnectorManifest(
        id="evil", name="x", kind="python", python=PythonSpec(entrypoint="os:system")))
  with pytest.raises(RegistrationError):  # private network blocked by default
    await registry.register(ConnectorManifest(
        id="internal", name="x", kind="mcp", mcp=McpSpec(url="http://169.254.169.254/mcp")))
  with pytest.raises(RegistrationError):
    await registry.register(ConnectorManifest(
        id="local", name="x", kind="mcp", mcp=McpSpec(url="https://localhost/mcp")))
  with pytest.raises(ValueError):  # "__" reserved as tool separator
    ConnectorManifest(id="a__b", name="x", kind="mcp", mcp=McpSpec(url="https://x.io"))


async def test_http_api_register_probe_and_connect(mcp_url):
  registry = ConnectorRegistry(allow_private_urls=True)
  app = create_app(registry)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
    body = {"manifest": notes_manifest(mcp_url).model_dump(mode="json"),
            "probe_secret": "key-alice"}
    r = await c.post("/connectors", json=body, headers={"X-User-Id": "alice"})
    assert r.status_code == 200, r.text
    assert set(r.json()["tools"]) == {"notes__add_note", "notes__list_notes", "notes__whoami"}

    # someone else can't hijack the id
    r = await c.post("/connectors", json=body, headers={"X-User-Id": "mallory"})
    assert r.status_code == 409

    # probe with a bad key fails and nothing is saved
    bad = {**body, "manifest": {**body["manifest"], "id": "notes_two"}, "probe_secret": "nope"}
    r = await c.post("/connectors", json=bad, headers={"X-User-Id": "alice"})
    assert r.status_code == 400
    assert await registry.store.get_connector("notes_two") is None

    r = await c.put("/me/connections/notes", json={"secret": "key-bob"},
                    headers={"X-User-Id": "bob"})
    assert r.status_code == 200
    r = await c.get("/me/connections", headers={"X-User-Id": "bob"})
    assert r.json() == [{"connector_id": "notes", "enabled": True, "has_secret": True,
                        "needs_reconnect": False}]
  await registry.close()


async def test_api_and_runner_in_separate_processes_share_state(mcp_url, tmp_path):
  """API and agent use separate registry instances that share only files on disk."""
  from cryptography.fernet import Fernet
  from adk_connectors import EncryptedSqliteSecretStore

  db, key = str(tmp_path / "shared.db"), Fernet.generate_key()
  make = lambda: ConnectorRegistry(  # noqa: E731
      SqliteConnectorStore(db), EncryptedSqliteSecretStore(db, key), allow_private_urls=True)
  api_registry, agent_registry = make(), make()

  api = create_app(api_registry)
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://t") as c:
    await c.post("/connectors", headers={"X-User-Id": "alice"},
                 json={"manifest": notes_manifest(mcp_url).model_dump(mode="json"), "probe": False})
    await c.put("/me/connections/notes", json={"secret": "key-alice"},
                headers={"X-User-Id": "alice"})

  import sqlite3
  raw = sqlite3.connect(db).execute("SELECT blob FROM secrets").fetchone()[0]
  assert b"key-alice" not in raw  # encrypted at rest

  llm = ScriptedLlm(model="scripted", seen_tools=[], call_tool="notes__whoami")
  agent = LlmAgent(name="a", model=llm, tools=[ConnectorToolset(agent_registry)])
  runner = Runner(app=App(name="x", root_agent=agent,
                          plugins=[ConnectorPolicyPlugin(agent_registry)]),
                  session_service=InMemorySessionService(), auto_create_session=True)
  assert "alice" in await ask(runner, "alice")
  await api_registry.close()
  await agent_registry.close()
