"""Credential storage. Secrets never go into ADK session state or the connector DB."""

from __future__ import annotations

import secrets as _secrets
from typing import Optional, Protocol


class SecretStore(Protocol):
  async def put(self, user_id: str, connector_id: str, value: str) -> str:
    """Stores a secret and returns an opaque reference."""
    ...

  async def get(self, ref: str) -> Optional[str]: ...

  async def delete(self, ref: str) -> None: ...


class InMemorySecretStore:
  """Dev only. In production back this with Secret Manager / Vault / KMS-encrypted rows."""

  def __init__(self) -> None:
    self._values: dict[str, str] = {}

  async def put(self, user_id: str, connector_id: str, value: str) -> str:
    ref = f"mem://{user_id}/{connector_id}/{_secrets.token_hex(8)}"
    self._values[ref] = value
    return ref

  async def get(self, ref: str) -> Optional[str]:
    return self._values.get(ref)

  async def delete(self, ref: str) -> None:
    self._values.pop(ref, None)


class EncryptedSqliteSecretStore:
  """Fernet-encrypted secrets in SQLite, shareable across processes.

  `key` is a Fernet key (`Fernet.generate_key()`); keep it in your KMS/env,
  never next to the database. For production prefer Secret Manager / Vault.
  """

  def __init__(self, path: str, key: str | bytes) -> None:
    import sqlite3

    from cryptography.fernet import Fernet

    self._fernet = Fernet(key)
    self._db = sqlite3.connect(path, check_same_thread=False)
    with self._db:
      self._db.execute("CREATE TABLE IF NOT EXISTS secrets (ref TEXT PRIMARY KEY, blob BLOB)")

  async def put(self, user_id: str, connector_id: str, value: str) -> str:
    ref = f"sqlite://{user_id}/{connector_id}/{_secrets.token_hex(8)}"
    with self._db:
      self._db.execute("INSERT INTO secrets VALUES (?, ?)", (ref, self._fernet.encrypt(value.encode())))
    return ref

  async def get(self, ref: str) -> Optional[str]:
    row = self._db.execute("SELECT blob FROM secrets WHERE ref=?", (ref,)).fetchone()
    return self._fernet.decrypt(row[0]).decode() if row else None

  async def delete(self, ref: str) -> None:
    with self._db:
      self._db.execute("DELETE FROM secrets WHERE ref=?", (ref,))
