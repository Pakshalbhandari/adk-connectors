"""Runner-wide policy for connector tool calls."""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict, deque
from typing import Any, Optional

from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

from .registry import ConnectorRegistry

audit = logging.getLogger("adk_connectors.audit")


class ConnectorPolicyPlugin(BasePlugin):
  """Applies to every connector tool call made through the Runner.

  - re-checks the user still has the connector (revocation mid-session)
  - per-user, per-connector rate limit
  - human confirmation for connectors marked `require_confirmation`
    (OpenAPI connectors; MCP/python handle it natively)
  - redacts the user's own secret from tool output and caps result size
  - turns connector errors into tool results so one bad connector can't kill a run
  - audit log line per call
  """

  def __init__(
      self,
      registry: ConnectorRegistry,
      *,
      calls_per_minute: int = 60,
      max_result_chars: int = 50_000,
  ) -> None:
    super().__init__(name="connector_policy")
    self.registry = registry
    self.calls_per_minute = calls_per_minute
    self.max_result_chars = max_result_chars
    self._calls: dict[tuple[str, str], deque[float]] = defaultdict(deque)

  async def before_tool_callback(
      self, *, tool: BaseTool, tool_args: dict[str, Any], tool_context: ToolContext
  ) -> Optional[dict]:
    connector_id = self.registry.connector_id_of(tool.name)
    if connector_id is None:
      return None
    user_id = tool_context.user_id

    if not await self.registry.is_active(user_id, connector_id):
      audit.info("deny user=%s tool=%s reason=not_connected", user_id, tool.name)
      return {"error": f"Connector '{connector_id}' is not connected for this user."}

    window = self._calls[(user_id, connector_id)]
    now = time.monotonic()
    while window and now - window[0] > 60:
      window.popleft()
    if len(window) >= self.calls_per_minute:
      audit.info("deny user=%s tool=%s reason=rate_limited", user_id, tool.name)
      return {"error": f"Rate limit reached for connector '{connector_id}'. Try again later."}
    window.append(now)

    manifest = await self.registry.store.get_connector(connector_id)
    if manifest and manifest.require_confirmation and manifest.kind == "openapi":
      confirmation = tool_context.tool_confirmation
      if confirmation is None:
        tool_context.request_confirmation(hint=f"Approve call to {tool.name}?")
        tool_context.actions.skip_summarization = True
        return {"error": "This tool call requires confirmation, please approve or reject."}
      if not confirmation.confirmed:
        return {"error": "This tool call was rejected by the user."}

    audit.info("call user=%s tool=%s", user_id, tool.name)
    return None

  async def after_tool_callback(
      self,
      *,
      tool: BaseTool,
      tool_args: dict[str, Any],
      tool_context: ToolContext,
      result: dict,
  ) -> Optional[dict]:
    connector_id = self.registry.connector_id_of(tool.name)
    if connector_id is None:
      return None
    secret = self.registry.credential(tool_context.user_id, connector_id)
    try:
      text = json.dumps(result, default=str)
    except (TypeError, ValueError):
      return None
    changed = False
    if secret and secret in text:
      text = text.replace(secret, "[REDACTED]")
      changed = True
    if len(text) > self.max_result_chars:
      return {"truncated": True, "content": text[: self.max_result_chars]}
    return json.loads(text) if changed else None

  async def on_tool_error_callback(
      self,
      *,
      tool: BaseTool,
      tool_args: dict[str, Any],
      tool_context: ToolContext,
      error: Exception,
  ) -> Optional[dict]:
    connector_id = self.registry.connector_id_of(tool.name)
    if connector_id is None:
      return None
    audit.warning("error user=%s tool=%s err=%r", tool_context.user_id, tool.name, error)
    return {"error": f"Connector '{connector_id}' failed: {type(error).__name__}"}
