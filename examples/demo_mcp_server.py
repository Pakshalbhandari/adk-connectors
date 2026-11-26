"""A tiny third-party MCP connector that requires an API key.

Run:  python examples/demo_mcp_server.py --port 8765
"""

from __future__ import annotations

import argparse
import contextvars

from mcp.server.mcpserver import MCPServer

try:
  from examples import demo_oauth_server
except ImportError:  # run as a script from examples/
  import demo_oauth_server  # type: ignore

API_KEYS = {"key-alice": "alice", "key-bob": "bob"}
_caller: contextvars.ContextVar[str] = contextvars.ContextVar("caller", default="?")

server = MCPServer(name="notes")
_notes: dict[str, list[str]] = {}


@server.tool()
def whoami() -> str:
  """Return which account the connector is authenticated as."""
  return _caller.get()


@server.tool()
def add_note(text: str) -> str:
  """Save a note for the authenticated account."""
  _notes.setdefault(_caller.get(), []).append(text)
  return f"saved ({len(_notes[_caller.get()])} notes)"


@server.tool()
def list_notes() -> list[str]:
  """List the authenticated account's notes."""
  return _notes.get(_caller.get(), [])


class ApiKeyAuth:
  """ASGI middleware: 401 unless X-API-Key or an OAuth bearer token is valid."""

  def __init__(self, app):
    self.app = app

  async def __call__(self, scope, receive, send):
    if scope["type"] != "http":
      return await self.app(scope, receive, send)
    headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
    who = API_KEYS.get(headers.get("x-api-key", ""))
    auth = headers.get("authorization", "")
    if who is None and auth.lower().startswith("bearer "):
      who = demo_oauth_server.ISSUED.get(auth[7:])  # OAuth tokens from the demo provider
    if who is None:
      await send({"type": "http.response.start", "status": 401,
                  "headers": [(b"content-type", b"text/plain")]})
      await send({"type": "http.response.body", "body": b"unauthorized"})
      return
    token = _caller.set(who)
    try:
      await self.app(scope, receive, send)
    finally:
      _caller.reset(token)


def build_app():
  starlette_app = server.streamable_http_app(stateless_http=True)
  return starlette_app, ApiKeyAuth(starlette_app)


if __name__ == "__main__":
  import uvicorn

  p = argparse.ArgumentParser()
  p.add_argument("--port", type=int, default=8765)
  args = p.parse_args()
  base, wrapped = build_app()
  uvicorn.run(wrapped, host="127.0.0.1", port=args.port)
