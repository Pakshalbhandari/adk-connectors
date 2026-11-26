"""ConnectorToolset: drop into any LlmAgent's `tools=[...]`."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset

from .registry import ConnectorRegistry

logger = logging.getLogger(__name__)


class ConnectorToolset(BaseToolset):
  """Exposes exactly the connectors the current user has connected.

  ADK calls `get_tools` with the invocation's context on every run, so a user who
  connects a new connector sees its tools on their next message — no agent
  rebuild, no runner restart.

  Args:
    registry: The shared ConnectorRegistry.
    only: Restrict to these connector ids (e.g. one sub-agent per connector).
    list_timeout: Seconds to wait for one connector to list its tools. A slow or
      broken connector is skipped instead of failing the whole turn.
  """

  def __init__(
      self,
      registry: ConnectorRegistry,
      *,
      only: Optional[list[str]] = None,
      list_timeout: float = 10.0,
  ) -> None:
    # Re-list on every model step (not once per invocation) so a connector the
    # user just authorized via `<id>__connect` is usable in the same turn.
    super().__init__(disable_invocation_cache=True)
    self.registry = registry
    self.only = set(only) if only else None
    self.list_timeout = list_timeout

  async def get_tools(self, readonly_context: Optional[ReadonlyContext] = None) -> list[BaseTool]:
    if readonly_context is None:  # no user -> no user connectors
      return []
    resolved = await self.registry.resolve_for_user(readonly_context.user_id, readonly_context)
    if self.only is not None:
      resolved = [(m, ts) for m, ts in resolved if m.id in self.only]

    async def list_one(manifest, toolset) -> list[BaseTool]:
      try:
        return await asyncio.wait_for(
            toolset.get_tools_with_prefix(readonly_context), self.list_timeout
        )
      except Exception as e:  # isolate failures per connector
        logger.warning("connector %s unavailable: %r", manifest.id, e)
        return []

    results = await asyncio.gather(*(list_one(m, ts) for m, ts in resolved))
    return [tool for tools in results for tool in tools]
