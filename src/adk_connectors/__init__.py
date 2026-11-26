"""Per-user connector registry for Google ADK.

    registry = ConnectorRegistry(SqliteConnectorStore("connectors.db"), secret_store)

    # Either give any agent the toolset ...
    agent = LlmAgent(..., tools=[ConnectorToolset(registry)])
    # ... or add a connector specialist to an existing orchestrator.
    orchestrator = LlmAgent(..., sub_agents=[*existing, make_connector_agent(registry, model=...)])

    app = install(App(name="my_app", root_agent=orchestrator), registry)
    runner = Runner(app=app, session_service=...)
"""

__version__ = "0.1.0"

from .agents import install, make_connector_agent
from .manifest import (AuthSpec, AuthType, ConnectorManifest, McpSpec, OAuthSpec, OpenApiSpec,
                       PythonSpec)
from .oauth import EncryptedSqliteCredentialService, PendingAuth, pending_auth_requests
from .plugin import ConnectorPolicyPlugin
from .registry import ConnectorRegistry, RegistrationError
from .secrets import EncryptedSqliteSecretStore, InMemorySecretStore, SecretStore
from .store import Connection, ConnectorStore, InMemoryConnectorStore, SqliteConnectorStore
from .toolset import ConnectorToolset

__all__ = [
    "install", "make_connector_agent", "EncryptedSqliteCredentialService", "OAuthSpec",
    "PendingAuth", "pending_auth_requests", "AuthSpec", "AuthType", "Connection", "ConnectorManifest", "ConnectorPolicyPlugin",
    "ConnectorRegistry", "ConnectorStore", "EncryptedSqliteSecretStore", "ConnectorToolset", "InMemoryConnectorStore",
    "InMemorySecretStore", "McpSpec", "OpenApiSpec", "PythonSpec", "RegistrationError",
    "SecretStore", "SqliteConnectorStore",
]
