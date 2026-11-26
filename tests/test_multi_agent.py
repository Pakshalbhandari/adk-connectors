"""Plugging the package into an existing multi-agent orchestration."""

from __future__ import annotations

import time
from typing import AsyncGenerator

import pytest
from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps.app import App
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.tools.agent_tool import AgentTool
from google.genai import types

from adk_connectors import ConnectorRegistry, install, make_connector_agent
from tests.test_end_to_end import mcp_url, notes_manifest  # noqa: F401  (fixture)


class StepLlm(BaseLlm):
  """Calls `steps` tools in order (if offered), then echoes the last tool result."""

  steps: list[tuple[str, dict]] = []
  seen_tools: list[list[str]] = []

  async def generate_content_async(
      self, llm_request: LlmRequest, stream: bool = False
  ) -> AsyncGenerator[LlmResponse, None]:
    self.seen_tools.append(sorted(llm_request.tools_dict))
    done = sum(
        1 for c in llm_request.contents for p in c.parts or []
        if p.function_call and p.function_call.name in dict(self.steps)
    )
    if done < len(self.steps) and self.steps[done][0] in llm_request.tools_dict:
      name, args = self.steps[done]
      fc = types.FunctionCall(name=name, args=args)
      yield LlmResponse(content=types.Content(role="model", parts=[types.Part(function_call=fc)]))
      return
    results = [p.function_response.response for c in llm_request.contents
               for p in c.parts or [] if p.function_response]
    text = f"final: {results[-1] if results else 'nothing'}"
    yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


def existing_orchestration(extra_sub_agents=(), extra_tools=()):
  """Stands in for the user's pre-existing ADK app."""
  billing = LlmAgent(name="billing", model=StepLlm(model="x"), description="Billing questions.")
  orch_llm = StepLlm(model="orch")
  orchestrator = LlmAgent(
      name="orchestrator", model=orch_llm, instruction="Route the request.",
      sub_agents=[billing, *extra_sub_agents], tools=list(extra_tools),
  )
  return orchestrator, orch_llm


async def run(runner, user_id):
  out = []
  async for ev in runner.run_async(
      user_id=user_id, session_id=f"{user_id}-{time.monotonic_ns()}",
      new_message=types.Content(role="user", parts=[types.Part(text="who am I on notes?")])):
    if ev.content and ev.content.parts:
      out += [f"{ev.author}: {p.text}" for p in ev.content.parts if p.text]
  return "\n".join(out)


@pytest.fixture
async def registry(mcp_url):  # noqa: F811
  r = ConnectorRegistry(allow_private_urls=True)
  await r.register(notes_manifest(mcp_url))
  await r.connect_user("alice", "notes", secret="key-alice")
  await r.connect_user("bob", "notes", secret="key-bob")
  yield r
  await r.close()


async def test_connector_specialist_as_sub_agent(registry):
  spec_llm = StepLlm(model="spec", steps=[("notes__whoami", {})])
  specialist = make_connector_agent(registry, model=spec_llm)
  orchestrator, orch_llm = existing_orchestration(extra_sub_agents=[specialist])
  orch_llm.steps = [("transfer_to_agent", {"agent_name": "connectors"})]

  app = install(App(name="existing_app", root_agent=orchestrator), registry)
  install(app, registry)  # idempotent
  assert len(app.plugins) == 1
  runner = Runner(app=app, session_service=InMemorySessionService(), auto_create_session=True)

  out_a, out_b = await run(runner, "alice"), await run(runner, "bob")
  assert "connectors:" in out_a and "alice" in out_a
  assert "bob" in out_b and "alice" not in out_b
  # Connector tools live on the specialist, not the orchestrator.
  assert not any("notes__" in t for seen in orch_llm.seen_tools for t in seen)


async def test_connector_specialist_as_agent_tool(registry):
  spec_llm = StepLlm(model="spec", steps=[("notes__whoami", {})])
  specialist = make_connector_agent(registry, model=spec_llm)
  orchestrator, orch_llm = existing_orchestration(extra_tools=[AgentTool(specialist)])
  orch_llm.steps = [("connectors", {"request": "who am I on notes?"})]

  app = install(App(name="existing_app", root_agent=orchestrator), registry)
  runner = Runner(app=app, session_service=InMemorySessionService(), auto_create_session=True)
  # AgentTool runs a nested Runner; user_id and plugins carry through.
  assert "bob" in await run(runner, "bob")
  assert "alice" in await run(runner, "alice")


async def test_scoped_specialists(registry):
  agent = make_connector_agent(registry, model=StepLlm(model="s"), name="notes_only",
                               only=["notes"])
  assert agent.tools[0].only == {"notes"}
