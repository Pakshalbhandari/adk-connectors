"""Turns a ConnectorManifest into a native ADK toolset."""

from __future__ import annotations

import importlib
from typing import Callable, Optional

from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.base_toolset import BaseToolset
from google.adk.tools.function_tool import FunctionTool

from .manifest import TOOL_SEPARATOR, ConnectorManifest

HeaderProvider = Callable[[ReadonlyContext], dict[str, str]]


def tool_prefix(connector_id: str) -> str:
  # BaseToolset joins prefix and name with "_", so "github_" -> "github__create_issue".
  return connector_id + TOOL_SEPARATOR[:-1]


class StaticToolset(BaseToolset):
  """Wraps a fixed list of tools (used for python connectors)."""

  def __init__(self, tools: list[BaseTool], **kwargs) -> None:
    super().__init__(**kwargs)
    self._tools = tools

  async def get_tools(self, readonly_context: Optional[ReadonlyContext] = None) -> list[BaseTool]:
    return [t for t in self._tools if self._is_tool_selected(t, readonly_context)]


def build_toolset(
    manifest: ConnectorManifest,
    *,
    header_provider: Optional[HeaderProvider] = None,
    static_headers: Optional[dict[str, str]] = None,
    allow_private: bool = False,
) -> BaseToolset:
  common = dict(tool_filter=manifest.tool_allowlist, tool_name_prefix=tool_prefix(manifest.id))

  if manifest.kind == "mcp":
    from google.adk.tools.mcp_tool.mcp_session_manager import (
        SseConnectionParams,
        StreamableHTTPConnectionParams,
    )
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

    spec = manifest.mcp
    params_cls = (
        StreamableHTTPConnectionParams if spec.transport == "streamable_http" else SseConnectionParams
    )
    # Stdio is intentionally unsupported: it would run user-supplied commands on
    # the host, and ADK shares one stdio session across all users.
    return McpToolset(
        connection_params=params_cls(url=spec.url, headers=static_headers, timeout=spec.timeout),
        header_provider=header_provider,
        require_confirmation=manifest.require_confirmation,
        **common,
    )

  if manifest.kind == "openapi":
    from google.adk.tools.openapi_tool.openapi_spec_parser.openapi_toolset import OpenAPIToolset

    if static_headers and not header_provider:
      header_provider = lambda _ctx, h=dict(static_headers): h  # noqa: E731
    return OpenAPIToolset(
        spec_dict=manifest.openapi.spec, header_provider=header_provider,
        allow_private_network_access=allow_private, **common
    )

  if manifest.kind == "python":
    module_name, _, attr = manifest.python.entrypoint.partition(":")
    factory = getattr(importlib.import_module(module_name), attr)
    produced = factory(manifest)
    if isinstance(produced, BaseToolset):
      produced.tool_filter = produced.tool_filter or manifest.tool_allowlist
      produced.tool_name_prefix = tool_prefix(manifest.id)
      return produced
    tools = [
        t if isinstance(t, BaseTool)
        else FunctionTool(t, require_confirmation=manifest.require_confirmation)
        for t in produced
    ]
    return StaticToolset(tools, **common)

  raise ValueError(f"unknown connector kind {manifest.kind!r}")
