"""SQLite 持久化层：连接管理、事务与建表。

每个操作使用独立连接，写操作走 BEGIN IMMEDIATE 使并发写串行化，
因此服务实例无状态，进程重启后可直接在同一数据库文件上继续执行。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    package_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    version TEXT NOT NULL,
    compatibility_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policies (
    policy_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    rules_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (policy_id, version)
);
CREATE TABLE IF NOT EXISTS snapshots (
    vehicle_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    hardware_batch TEXT NOT NULL,
    software_version TEXT NOT NULL,
    battery_percent INTEGER NOT NULL,
    online INTEGER NOT NULL,
    health_score REAL NOT NULL,
    reported_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vehicle_flags (
    vehicle_id TEXT PRIMARY KEY,
    quarantined INTEGER NOT NULL DEFAULT 0,
    quarantine_reason TEXT NOT NULL DEFAULT '',
    blocked_packages_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS waves (
    wave_id TEXT PRIMARY KEY,
    package_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    target_percent REAL NOT NULL,
    state TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    manual_override INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE (package_id, seq)
);
CREATE TABLE IF NOT EXISTS wave_members (
    wave_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    included INTEGER NOT NULL,
    reasons_json TEXT NOT NULL,
    PRIMARY KEY (wave_id, vehicle_id)
);
CREATE TABLE IF NOT EXISTS commands (
    command_id TEXT PRIMARY KEY,
    wave_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    package_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (wave_id, vehicle_id, kind)
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL,
    wave_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    late INTEGER NOT NULL DEFAULT 0,
    duplicate INTEGER NOT NULL DEFAULT 0,
    reported_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    reported_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS health_reports (
    vehicle_id TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    score REAL NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (vehicle_id, reported_at)
);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wave_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wave_id TEXT NOT NULL,
    action TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);
"""


class Store:
    """SQLite 文件存储；db_path 必须是文件路径以保证重启后可恢复。"""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        conn = self._new()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _new(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self._new()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE 保证多线程 / 多进程下串行提交。"""
        conn = self._new()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
