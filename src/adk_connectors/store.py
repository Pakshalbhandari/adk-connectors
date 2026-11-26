"""Persistence for connector manifests and per-user connections."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from typing import Optional, Protocol

from .manifest import ConnectorManifest


@dataclass(frozen=True)
class Connection:
  """A user's link to a connector."""

  user_id: str
  connector_id: str
  secret_ref: Optional[str] = None
  enabled: bool = True
  # ConnectorManifest.trust_fingerprint() the user agreed to when connecting.
  consented: Optional[str] = None


class ConnectorStore(Protocol):
  async def upsert_connector(self, manifest: ConnectorManifest) -> None: ...
  async def get_connector(self, connector_id: str) -> Optional[ConnectorManifest]: ...
  async def list_connectors(self) -> list[ConnectorManifest]: ...
  async def delete_connector(self, connector_id: str) -> None: ...
  async def connect(self, connection: Connection) -> None: ...
  async def disconnect(self, user_id: str, connector_id: str) -> Optional[Connection]: ...
  async def get_connection(self, user_id: str, connector_id: str) -> Optional[Connection]: ...
  async def list_connections(self, user_id: str) -> list[Connection]: ...


class InMemoryConnectorStore:
  def __init__(self) -> None:
    self._connectors: dict[str, ConnectorManifest] = {}
    self._connections: dict[tuple[str, str], Connection] = {}

  async def upsert_connector(self, manifest: ConnectorManifest) -> None:
    self._connectors[manifest.id] = manifest

  async def get_connector(self, connector_id: str) -> Optional[ConnectorManifest]:
    return self._connectors.get(connector_id)

  async def list_connectors(self) -> list[ConnectorManifest]:
    return list(self._connectors.values())

  async def delete_connector(self, connector_id: str) -> None:
    self._connectors.pop(connector_id, None)
    for key in [k for k in self._connections if k[1] == connector_id]:
      del self._connections[key]

  async def connect(self, connection: Connection) -> None:
    self._connections[(connection.user_id, connection.connector_id)] = connection

  async def disconnect(self, user_id: str, connector_id: str) -> Optional[Connection]:
    return self._connections.pop((user_id, connector_id), None)

  async def get_connection(self, user_id: str, connector_id: str) -> Optional[Connection]:
    return self._connections.get((user_id, connector_id))

  async def list_connections(self, user_id: str) -> list[Connection]:
    return [c for (u, _), c in self._connections.items() if u == user_id]


class SqliteConnectorStore:
  """Single-file store, fine for dev and small deployments. Swap for Postgres/Firestore."""

  def __init__(self, path: str = "connectors.db") -> None:
    self._db = sqlite3.connect(path, check_same_thread=False)
    self._lock = asyncio.Lock()
    self._db.executescript(
        """
        CREATE TABLE IF NOT EXISTS connectors (
          id TEXT PRIMARY KEY, manifest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS connections (
          user_id TEXT NOT NULL, connector_id TEXT NOT NULL,
          secret_ref TEXT, enabled INTEGER NOT NULL DEFAULT 1, consented TEXT,
          PRIMARY KEY (user_id, connector_id));
        """
    )
    cols = {r[1] for r in self._db.execute("PRAGMA table_info(connections)")}
    if "consented" not in cols:  # migrate databases created by 0.1.0 dev builds
      with self._db:
        self._db.execute("ALTER TABLE connections ADD COLUMN consented TEXT")

  async def _run(self, sql: str, args: tuple = ()) -> list[tuple]:
    async with self._lock:
      def go() -> list[tuple]:
        with self._db:
          return self._db.execute(sql, args).fetchall()
      return await asyncio.to_thread(go)

  async def upsert_connector(self, manifest: ConnectorManifest) -> None:
    await self._run(
        "INSERT INTO connectors(id, manifest) VALUES(?, ?) "
        "ON CONFLICT(id) DO UPDATE SET manifest=excluded.manifest",
        (manifest.id, manifest.model_dump_json()),
    )

  async def get_connector(self, connector_id: str) -> Optional[ConnectorManifest]:
    rows = await self._run("SELECT manifest FROM connectors WHERE id=?", (connector_id,))
    return ConnectorManifest.model_validate_json(rows[0][0]) if rows else None

  async def list_connectors(self) -> list[ConnectorManifest]:
    rows = await self._run("SELECT manifest FROM connectors ORDER BY id")
    return [ConnectorManifest.model_validate_json(r[0]) for r in rows]

  async def delete_connector(self, connector_id: str) -> None:
    await self._run("DELETE FROM connections WHERE connector_id=?", (connector_id,))
    await self._run("DELETE FROM connectors WHERE id=?", (connector_id,))

  async def connect(self, connection: Connection) -> None:
    await self._run(
        "INSERT INTO connections(user_id, connector_id, secret_ref, enabled, consented) "
        "VALUES(?, ?, ?, ?, ?) ON CONFLICT(user_id, connector_id) DO UPDATE SET "
        "secret_ref=excluded.secret_ref, enabled=excluded.enabled, "
        "consented=excluded.consented",
        (connection.user_id, connection.connector_id, connection.secret_ref,
         int(connection.enabled), connection.consented),
    )

  async def get_connection(self, user_id: str, connector_id: str) -> Optional[Connection]:
    rows = await self._run(
        "SELECT user_id, connector_id, secret_ref, enabled, consented FROM connections "
        "WHERE user_id=? AND connector_id=?", (user_id, connector_id))
    return Connection(rows[0][0], rows[0][1], rows[0][2], bool(rows[0][3]), rows[0][4]) if rows else None

  async def disconnect(self, user_id: str, connector_id: str) -> Optional[Connection]:
    existing = await self.get_connection(user_id, connector_id)
    await self._run(
        "DELETE FROM connections WHERE user_id=? AND connector_id=?", (user_id, connector_id))
    return existing

  async def list_connections(self, user_id: str) -> list[Connection]:
    rows = await self._run(
        "SELECT user_id, connector_id, secret_ref, enabled, consented FROM connections "
        "WHERE user_id=?", (user_id,))
    return [Connection(r[0], r[1], r[2], bool(r[3]), r[4]) for r in rows]
