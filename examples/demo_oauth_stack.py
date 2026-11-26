"""Demo OAuth provider + an MCP server that accepts its tokens, in one process.

    python examples/demo_oauth_stack.py      # provider :8766, MCP server :8765/mcp
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import uvicorn  # noqa: E402

from examples import demo_mcp_server, demo_oauth_server  # noqa: E402


async def main() -> None:
  _, mcp_app = demo_mcp_server.build_app()
  servers = [
      uvicorn.Server(uvicorn.Config(demo_oauth_server.app, host="127.0.0.1", port=8766)),
      uvicorn.Server(uvicorn.Config(mcp_app, host="127.0.0.1", port=8765)),
  ]
  print("OAuth provider: http://127.0.0.1:8766   MCP server: http://127.0.0.1:8765/mcp")
  await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
  asyncio.run(main())
