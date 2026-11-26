"""Registry config shared by the API process and the agent process."""

import os

from adk_connectors import ConnectorRegistry, EncryptedSqliteSecretStore, SqliteConnectorStore


def build_registry():
  key = os.environ.get("CONNECTORS_SECRET_KEY")
  if not key:
    raise SystemExit(
        "Set CONNECTORS_SECRET_KEY (python -c 'from cryptography.fernet import Fernet;"
        " print(Fernet.generate_key().decode())')")
  db = os.environ.get("CONNECTORS_DB", "connectors.db")
  return ConnectorRegistry(
      SqliteConnectorStore(db),
      EncryptedSqliteSecretStore(db, key),
      allow_private_urls=os.environ.get("CONNECTORS_ALLOW_PRIVATE") == "1",
      # Where OAuth providers send the user back. `adk web` uses its own base URL.
      oauth_redirect_uri=os.environ.get("CONNECTORS_OAUTH_REDIRECT", "http://localhost:8001"),
  )
