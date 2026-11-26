# adk-connectors

[![CI](https://github.com/Pakshalbhandari/adk-connectors/actions/workflows/ci.yml/badge.svg)](https://github.com/Pakshalbhandari/adk-connectors/actions/workflows/ci.yml) [![PyPI](https://img.shields.io/pypi/v/adk-connectors)](https://pypi.org/project/adk-connectors/)

This is a connector registry you can plug into **Google ADK 2.15**. Any user can register a connector (an MCP server or an OpenAPI spec) or connect to one that already exists. On the user's next message, the ADK `Runner` picks it up with that user's own credentials. You don't rebuild the agent or restart the runner.

```
 user ──HTTP──▶ registry API ──▶ ConnectorStore (manifests, user↔connector)
                                 SecretStore    (per-user creds, encrypted)
                                        ▲
 Runner ─▶ LlmAgent(tools=[ConnectorToolset]) ─ get_tools(ctx.user_id) ─▶ McpToolset / OpenAPIToolset / python
   └─ App(plugins=[ConnectorPolicyPlugin])  revocation check · rate limit · confirmation · redaction · error isolation
```

## Install

```bash
pip install adk-connectors            # core
pip install "adk-connectors[api]"     # + FastAPI registration API
```

Requires Python 3.11+ and `google-adk` 2.15+. For development: `git clone https://github.com/Pakshalbhandari/adk-connectors && pip install -e ".[test]"`.

## Plug it into an existing multi-agent orchestration

Three steps: build a registry, add a connector specialist to your orchestrator, and install the plugin on your `App`.

```python
from adk_connectors import ConnectorRegistry, SqliteConnectorStore, EncryptedSqliteSecretStore
from adk_connectors import make_connector_agent, install

registry = ConnectorRegistry(SqliteConnectorStore("connectors.db"),
                             EncryptedSqliteSecretStore("connectors.db", FERNET_KEY))

connectors = make_connector_agent(registry, model="gemini-2.5-flash")

# Option A: LLM transfer. Add it next to your existing sub-agents.
orchestrator = LlmAgent(name="orchestrator", model=..., sub_agents=[billing, support, connectors])

# Option B: call it as a tool. Control comes back to the orchestrator afterwards.
orchestrator = LlmAgent(name="orchestrator", model=..., tools=[AgentTool(connectors)])

app = install(App(name="my_app", root_agent=orchestrator), registry)  # adds ConnectorPolicyPlugin
runner = Runner(app=app, session_service=...)
```

Notes:
- **Pass your real end-user id as `user_id` to `run_async`.** It has to match the user who connected the connectors. Both options carry it through to the specialist: a transfer stays in the same invocation, and `AgentTool`'s nested runner inherits the `user_id` and the app's plugins.
- **One specialist or several.** One specialist keeps a user's connector tools out of the orchestrator's own tool list. To split them up, use `make_connector_agent(registry, model=..., name="crm", only=["salesforce", "hubspot"])`.
- **Without a specialist.** You can also add `ConnectorToolset(registry)` directly to any existing agent's `tools=[...]`.

## Lower-level wiring

```python
from google.adk.agents.llm_agent import LlmAgent
from google.adk.apps.app import App
from google.adk.runners import Runner
from adk_connectors import (ConnectorRegistry, ConnectorToolset, ConnectorPolicyPlugin,
                            SqliteConnectorStore, EncryptedSqliteSecretStore)

registry = ConnectorRegistry(SqliteConnectorStore("connectors.db"),
                             EncryptedSqliteSecretStore("connectors.db", FERNET_KEY))

agent = LlmAgent(name="orchestrator", model="gemini-2.5-flash",
                 tools=[ConnectorToolset(registry), *your_own_tools])
app = App(name="my_app", root_agent=agent, plugins=[ConnectorPolicyPlugin(registry)])
runner = Runner(app=app, session_service=...)

# The user_id passed here decides which connectors (and credentials) are used.
runner.run_async(user_id="alice", session_id=..., new_message=...)
```

Expose the registration API with `adk_connectors.api.create_app(registry)`, which returns a FastAPI app.

## Connector manifest

```json
{
  "id": "notes",
  "name": "Notes",
  "kind": "mcp",
  "mcp": { "url": "https://notes.example.com/mcp", "transport": "streamable_http" },
  "auth": { "type": "api_key", "header_name": "X-API-Key" },
  "tool_allowlist": ["list_notes", "add_note"],
  "require_confirmation": false
}
```

| kind | who can register | built on |
|---|---|---|
| `mcp` (Streamable HTTP or SSE) | any user | `McpToolset` |
| `openapi` (inline spec) | any user | `OpenAPIToolset` |
| `python` (`module:factory`) | admin only (`registry.register(m, trusted=True)`) | `FunctionTool` / your `BaseToolset` |

Auth types are `none`, `api_key` (sent in a custom header), `bearer` (sent as `Authorization: Bearer`) and `oauth2` (each user signs in, see below). The model sees tools named `<connector_id>__<tool>`.

## OAuth2 connectors

These are for SaaS tools where each user signs in with their own account, such as Google, Slack, GitHub or Salesforce. The connector owner registers their OAuth app once. Each user then signs in from the chat. ADK's own auth machinery handles the code exchange, token storage and refresh.

```json
{"manifest": {"id": "crm", "name": "CRM", "kind": "mcp", "mcp": {"url": "https://mcp.crm.example/mcp"},
  "auth": {"type": "oauth2", "oauth": {
    "authorization_url": "https://crm.example/oauth/authorize",
    "token_url": "https://crm.example/oauth/token",
    "client_id": "abc123", "scopes": ["contacts.read"]}}},
 "oauth_client_secret": "..."}
```

How the flow works:

1. **Connect.** `PUT /me/connections/crm` takes no secret.
2. **Sign-in tool.** Until the user signs in, the agent sees one tool, `crm__connect`. When the model calls it, ADK emits its standard `adk_request_credential` event.
3. **Sign-in.** Your client sends the user to the provider's sign-in URL. When the provider redirects back, the client returns that redirect URL to the Runner. ADK then exchanges the code and saves the token in the Runner's `credential_service`, separately for each user. The real `crm__*` tools become available in the same turn.
4. **Later turns.** ADK loads the token, refreshes it when it expires, and sends it as `Authorization: Bearer`.

What your app needs:

```python
from adk_connectors import EncryptedSqliteCredentialService, pending_auth_requests

creds = EncryptedSqliteCredentialService("tokens.db", FERNET_KEY)   # or your own BaseCredentialService
registry = ConnectorRegistry(store, secrets,
                             oauth_redirect_uri="https://app.example.com/oauth/callback",
                             credential_service=creds)               # lets disconnect delete tokens
runner = Runner(app=app, session_service=..., credential_service=creds)  # required for OAuth

async for event in runner.run_async(user_id=uid, session_id=sid, new_message=msg):
    for req in pending_auth_requests(event):
        show_sign_in_link(req.auth_uri)        # popup or redirect in your frontend
# ... after the provider redirects to your callback with ?code=...&state=...
await consume(runner.run_async(user_id=uid, session_id=sid, new_message=req.response(callback_url)))
```

Notes:
- **Redirect URL.** Register `oauth_redirect_uri` with each provider. `adk web` overrides it with its own base URL, for example `http://localhost:8001`, and runs the popup for you.
- **Client secret.** The OAuth client secret lives in the `SecretStore`. The manifest only holds a reference to it.
- **When users must sign in again.** Changing the connector's URLs, client id or scopes requires every user to sign in again, and so does disconnecting.
- **No credential service.** If the Runner has no `credential_service`, OAuth connectors are hidden and a warning is logged.
- **Plain http.** Use plain `http` OAuth URLs only in development, with `CONNECTORS_ALLOW_PRIVATE=1` and `AUTHLIB_INSECURE_TRANSPORT=1`.

## API

| method | path | what it does |
|---|---|---|
| `POST` | `/connectors` | Registers a connector. Body: `{manifest, probe=true, probe_secret}`. Probing connects once and lists the tools; nothing is saved if the probe fails. |
| `GET` | `/connectors` | Lists the connectors available to connect to. |
| `DELETE` | `/connectors/{id}` | Deletes a connector. Only its owner can do this. |
| `PUT` | `/me/connections/{id}` | Connects the current user to a connector. Body: `{secret}`. |
| `GET` / `DELETE` | `/me/connections[/{id}]` | Lists or removes the current user's connections. |

The `X-User-Id` header is a **placeholder** for identity. Replace it with your real auth by overriding the `current_user` dependency.

## Run the demo

```bash
git clone https://github.com/Pakshalbhandari/adk-connectors && cd adk-connectors
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
export CONNECTORS_SECRET_KEY=$(.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
export CONNECTORS_ALLOW_PRIVATE=1   # demo MCP server is on localhost

.venv/bin/python examples/demo_mcp_server.py --port 8765 &      # a third-party connector
.venv/bin/uvicorn examples.serve_registry:app --port 8100 &     # registration API (adk web uses 8000)

curl -XPOST localhost:8100/connectors -H 'X-User-Id: user' -H 'content-type: application/json' \
  -d '{"manifest":{"id":"notes","name":"Notes","kind":"mcp","mcp":{"url":"http://127.0.0.1:8765/mcp"},
       "auth":{"type":"api_key","header_name":"X-API-Key"}},"probe_secret":"key-alice"}'
curl -XPUT localhost:8100/me/connections/notes -H 'X-User-Id: user' -H 'content-type: application/json' \
  -d '{"secret":"key-alice"}'

.venv/bin/adk web examples      # pick connector_agent and ask "who am I on notes?"
```

`adk web` uses the user id `user`, so the demo registers and connects as that user. Set `GOOGLE_API_KEY` (or Vertex env vars) for Gemini, or set `ADK_MODEL` to change the model.

### OAuth demo in `adk web`

```bash
export CONNECTORS_SECRET_KEY=<key> CONNECTORS_ALLOW_PRIVATE=1 AUTHLIB_INSECURE_TRANSPORT=1
.venv/bin/python examples/demo_oauth_stack.py &          # fake OAuth provider :8766 + MCP :8765
.venv/bin/uvicorn examples.serve_registry:app --port 8100 &
curl -XPOST localhost:8100/connectors -H 'X-User-Id: admin' -H 'content-type: application/json' -d '{
  "manifest":{"id":"crm","name":"CRM","kind":"mcp","mcp":{"url":"http://127.0.0.1:8765/mcp"},
    "auth":{"type":"oauth2","oauth":{"authorization_url":"http://127.0.0.1:8766/authorize",
      "token_url":"http://127.0.0.1:8766/token","client_id":"demo-client"}}},
  "oauth_client_secret":"demo-secret"}'
curl -XPUT localhost:8100/me/connections/crm -H 'X-User-Id: user' -H 'content-type: application/json' -d '{}'
.venv/bin/adk web examples --port 8001     # ask: "connect my CRM", sign in as alice, then "who am I in the CRM?"
```

## Tests

`.venv/bin/python -m pytest -q` runs 19 end-to-end tests, including multi-agent orchestration, an OpenAPI connector, a connector owner trying to redirect users' secrets, and the full OAuth2 flow (sign-in from chat, per-user tokens, refresh, disconnect). They start a real MCP server and run the real ADK 2.15 `Runner` with a scripted LLM. The tests cover:

- each user seeing only their own tools
- a newly connected connector being picked up on the next run
- each user calling a connector with their own credential
- disconnecting a connector
- a broken or unauthorized connector being isolated from the rest
- the registration policy (SSRF protection, admin-only python connectors)
- the HTTP API's probe and ownership checks
- the API and the agent sharing state across processes, with secrets encrypted at rest

## Design notes

- **Per-user resolution.** ADK calls `BaseToolset.get_tools(readonly_context)` on every invocation, and `readonly_context.user_id` identifies the user. Each connector gets one toolset object, shared across users. Each user's credentials are injected through `header_provider` when a tool is called. ADK keys MCP sessions by a hash of the headers, so every user gets an isolated MCP session.
- **Stdio MCP is deliberately unsupported.** It would run user-supplied commands on the host, and ADK shares a single stdio session across all users.
- **Re-consent on changes.** A connection records the connector's endpoint, spec, code and auth as they were when the user connected (`trust_fingerprint`). If the owner changes any of these, the connection pauses until the user reconnects, so an update can't redirect users' credentials. `GET /me/connections` shows `needs_reconnect`.
- **Fault isolation.** Listing a connector's tools has a timeout, and a failure only drops that one connector. Errors during tool calls are turned into tool results by the plugin, so they don't end the run.
- **Prompt injection.** Connector output is untrusted. Use `require_confirmation` for connectors that write data, and keep the instruction that tool output is data.

## Not done yet (production checklist)

- **OAuth2 extras.** Still to add: PKCE, OIDC discovery (`issuer_url`), token revocation at the provider on disconnect, and ADK `ServiceAccount` or `client_credentials` connectors.
- **Real identity and authorization on the API.** Add admin roles and per-tenant connector visibility.
- **Shared state across replicas.** The rate limiter and credential cache are per process. Move them to Redis for multiple replicas.
- **Production secret storage.** Use Secret Manager or Vault instead of `EncryptedSqliteSecretStore`.
- **Many connectors per user.** When a user has a lot of tools, give each connector its own sub-agent with `ConnectorToolset(registry, only=["github"])` and let the orchestrator delegate.
- **Comparison with ADK's `AgentRegistry`.** ADK 2.15 ships `google.adk.integrations.agent_registry.AgentRegistry` (Google Cloud managed). This project is the self-hosted, user-driven alternative. You could add a backend that reads from `AgentRegistry` too.
