"""A minimal OAuth2 provider (authorization code + refresh) for demos and tests.

Run:  python examples/demo_oauth_server.py --port 8766
Issued access tokens are accepted by examples/demo_mcp_server.py when both run
in the same process (tests) — for the manual demo use `python examples/demo_oauth_stack.py`.
"""

from __future__ import annotations

import argparse
import base64
import secrets
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

CLIENT_ID = "demo-client"
CLIENT_SECRET = "demo-secret"
TOKEN_TTL = 3600  # tests may lower this to force refreshes

ISSUED: dict[str, str] = {}  # access token -> user
_codes: dict[str, str] = {}  # auth code -> user
_refresh: dict[str, str] = {}  # refresh token -> user
STATS = {"code_grants": 0, "refresh_grants": 0}


async def authorize(request: Request):
  q = request.query_params
  if q.get("client_id") != CLIENT_ID:
    return JSONResponse({"error": "invalid_client"}, status_code=400)
  login = q.get("login")  # tests/automation pick the user directly
  if not login:
    buttons = "".join(
        f'<a href="?{urlencode({**q, "login": u})}"><button>Sign in as {u}</button></a> '
        for u in ("alice", "bob"))
    return HTMLResponse(f"<h3>Demo OAuth provider</h3><p>Authorize {CLIENT_ID}?</p>{buttons}")
  code = secrets.token_urlsafe(12)
  _codes[code] = login
  sep = "&" if "?" in q["redirect_uri"] else "?"
  return RedirectResponse(
      f"{q['redirect_uri']}{sep}{urlencode({'code': code, 'state': q.get('state', '')})}",
      status_code=302)


def _client_ok(request: Request, form) -> bool:
  cid, csec = form.get("client_id"), form.get("client_secret")
  auth = request.headers.get("authorization", "")
  if auth.lower().startswith("basic "):
    cid, _, csec = base64.b64decode(auth[6:]).decode().partition(":")
  return cid == CLIENT_ID and csec == CLIENT_SECRET


def _issue(user: str) -> dict:
  access, refresh = f"at-{user}-{secrets.token_hex(4)}", f"rt-{user}-{secrets.token_hex(4)}"
  ISSUED[access], _refresh[refresh] = user, user
  return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer",
          "expires_in": TOKEN_TTL}


async def token(request: Request):
  form = await request.form()
  if not _client_ok(request, form):
    return JSONResponse({"error": "invalid_client"}, status_code=401)
  grant = form.get("grant_type")
  if grant == "authorization_code":
    user = _codes.pop(form.get("code", ""), None)
    if user is None:
      return JSONResponse({"error": "invalid_grant"}, status_code=400)
    STATS["code_grants"] += 1
    return JSONResponse(_issue(user))
  if grant == "refresh_token":
    user = _refresh.pop(form.get("refresh_token", ""), None)
    if user is None:
      return JSONResponse({"error": "invalid_grant"}, status_code=400)
    STATS["refresh_grants"] += 1
    return JSONResponse(_issue(user))
  return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


app = Starlette(routes=[Route("/authorize", authorize), Route("/token", token, methods=["POST"])])

if __name__ == "__main__":
  import uvicorn

  p = argparse.ArgumentParser()
  p.add_argument("--port", type=int, default=8766)
  uvicorn.run(app, host="127.0.0.1", port=p.parse_args().port)
