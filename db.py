"""
Postgres layer + token encryption สำหรับ quest bot (multi-user)

Backend 2 แบบ (เลือกอัตโนมัติจาก DATABASE_URL):
  - postgresql://...  → asyncpg (Railway / Docker compose / Postgres จริง)
  - ว่าง / sqlite:... → SQLite ไฟล์เดียว (local zero-setup / โฮสต์ไม่มี Postgres)
    ค่า default: ไฟล์ questbot.db (หรือ SQLITE_PATH)

ตาราง:
  accounts     หนึ่งแถว = หนึ่ง token ที่ผู้ใช้ลงทะเบียน (token เก็บแบบเข้ารหัส)
  completions  log เควสที่ทำเสร็จ (ใช้ทำ leaderboard + กันนับซ้ำ)
"""
from __future__ import annotations

import os
import re
from typing import Optional

import asyncpg
from cryptography.fernet import Fernet

# อ่าน env แบบ lazy (ตอนใช้จริง) ไม่ใช่ตอน import — กันปัญหา load_dotenv() มาทีหลัง
_fernet_cached: Optional[Fernet] = None


def _fernet() -> Fernet:
    global _fernet_cached
    if _fernet_cached is None:
        key = os.getenv("ENCRYPTION_KEY", "")
        if not key:
            raise RuntimeError("ENCRYPTION_KEY ไม่ได้ตั้ง")
        _fernet_cached = Fernet(key.encode())
    return _fernet_cached


def encrypt(token: str) -> str:
    return _fernet().encrypt(token.encode()).decode()


def decrypt(blob: str) -> str:
    return _fernet().decrypt(blob.encode()).decode()


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              BIGSERIAL   PRIMARY KEY,
    discord_user_id BIGINT      NOT NULL,                 -- คนที่ลงทะเบียน (เจ้าของ token)
    discord_name    TEXT        NOT NULL DEFAULT '?',
    token_enc       TEXT        NOT NULL,                 -- token เข้ารหัส Fernet
    account_user_id TEXT        NOT NULL UNIQUE,          -- id ของบัญชี Discord ที่ token นี้เป็น
    username        TEXT        NOT NULL DEFAULT '?',
    active          BOOLEAN     NOT NULL DEFAULT TRUE,
    last_error      TEXT,
    added_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS completions (
    id           BIGSERIAL   PRIMARY KEY,
    account_id   BIGINT      NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    quest_id     TEXT        NOT NULL,
    quest_name   TEXT        NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, quest_id)
);
CREATE TABLE IF NOT EXISTS panels (
    message_id BIGINT      PRIMARY KEY,    -- ข้อความแผงที่ปักไว้ (ไว้ auto-refresh สถิติ)
    channel_id BIGINT      NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS user_prefs (
    discord_user_id  BIGINT  PRIMARY KEY,
    presence_enabled BOOLEAN NOT NULL DEFAULT TRUE   -- โชว์เควสบนโปรไฟล์ตัวเองไหม (รายคน)
);
CREATE TABLE IF NOT EXISTS notify_msgs (
    discord_user_id BIGINT PRIMARY KEY,              -- embed สรุปเควสเสร็จ (1 อันต่อคน, ไว้แก้ซ้ำ)
    channel_id      BIGINT NOT NULL,
    message_id      BIGINT NOT NULL
);
"""

# SQLite mirror ของ SCHEMA ข้างบน (query ทั้งหมดเขียนแบบ $1 แล้วแปลเป็น ? อัตโนมัติ)
SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER   PRIMARY KEY AUTOINCREMENT,
    discord_user_id INTEGER   NOT NULL,
    discord_name    TEXT      NOT NULL DEFAULT '?',
    token_enc       TEXT      NOT NULL,
    account_user_id TEXT      NOT NULL UNIQUE,
    username        TEXT      NOT NULL DEFAULT '?',
    active          INTEGER   NOT NULL DEFAULT 1,
    last_error      TEXT,
    added_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS completions (
    id           INTEGER   PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER   NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    quest_id     TEXT      NOT NULL,
    quest_name   TEXT      NOT NULL,
    completed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (account_id, quest_id)
);
CREATE TABLE IF NOT EXISTS panels (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS user_prefs (
    discord_user_id  INTEGER PRIMARY KEY,
    presence_enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS notify_msgs (
    discord_user_id INTEGER PRIMARY KEY,
    channel_id      INTEGER NOT NULL,
    message_id      INTEGER NOT NULL
);
"""

_placeholder_re = re.compile(r"\$\d+")


def _q(query: str) -> str:
    """แปล placeholder $1,$2,... (Postgres) เป็น ? (SQLite)"""
    return _placeholder_re.sub("?", query)


class DB:
    def __init__(self) -> None:
        self.mode: str = "pg"          # 'pg' | 'sqlite'
        self.pool: Optional[asyncpg.Pool] = None
        self._sqlite = None            # aiosqlite.Connection (เฉพาะโหมด sqlite)

    async def connect(self) -> None:
        url = os.getenv("DATABASE_URL", "").strip()
        if not url or url.startswith("sqlite:"):
            import aiosqlite

            self.mode = "sqlite"
            if url.startswith("sqlite:"):
                rest = url[len("sqlite:"):]
                path = rest[3:] if rest.startswith("///") else rest
                path = path or "questbot.db"
            else:
                path = os.getenv("SQLITE_PATH", "").strip() or "questbot.db"
            self._sqlite = await aiosqlite.connect(path)
            self._sqlite.row_factory = aiosqlite.Row
            await self._sqlite.execute("PRAGMA journal_mode=WAL")
            await self._sqlite.execute("PRAGMA foreign_keys=ON")
            await self._sqlite.executescript(SCHEMA_SQLITE)
            await self._sqlite.commit()
            return
        self.mode = "pg"
        self.pool = await asyncpg.create_pool(url, min_size=1, max_size=5)
        async with self.pool.acquire() as c:
            await c.execute(SCHEMA)

    async def close(self) -> None:
        if self._sqlite is not None:
            await self._sqlite.close()
            self._sqlite = None
        if self.pool:
            await self.pool.close()
            self.pool = None

    # ── low-level (query เขียนแบบ $n ได้ทั้ง 2 backend) ──────────
    async def _fetch(self, query: str, *args):
        if self.mode == "pg":
            async with self.pool.acquire() as c:
                return await c.fetch(query, *args)
        async with self._sqlite.execute(_q(query), args) as cur:
            return await cur.fetchall()

    async def _fetchrow(self, query: str, *args):
        if self.mode == "pg":
            async with self.pool.acquire() as c:
                return await c.fetchrow(query, *args)
        async with self._sqlite.execute(_q(query), args) as cur:
            return await cur.fetchone()

    async def _fetchval(self, query: str, *args):
        if self.mode == "pg":
            async with self.pool.acquire() as c:
                return await c.fetchval(query, *args)
        async with self._sqlite.execute(_q(query), args) as cur:
            row = await cur.fetchone()
            return row[0] if row is not None else None

    async def _execute(self, query: str, *args) -> str:
        """คืน tag แบบ asyncpg ('DELETE 1' / 'INSERT 0 1') ให้โค้ดเดิมใช้ .endswith('1') ได้"""
        if self.mode == "pg":
            async with self.pool.acquire() as c:
                return await c.execute(query, *args)
        async with self._sqlite.execute(_q(query), args) as cur:
            await self._sqlite.commit()
            return f"EXECUTE {cur.rowcount}"

    # ── accounts ────────────────────────────────────────────────
    async def add_account(self, discord_user_id: int, discord_name: str,
                          token: str, account_user_id: str, username: str) -> str:
        """เพิ่ม/อัปเดต token  → คืน 'added' หรือ 'updated'"""
        enc = encrypt(token)
        exists = await self._fetchval(
            "SELECT id FROM accounts WHERE account_user_id=$1", account_user_id)
        if exists:
            await self._execute(
                "UPDATE accounts SET token_enc=$1, discord_user_id=$2, discord_name=$3, "
                "username=$4, active=TRUE, last_error=NULL WHERE account_user_id=$5",
                enc, discord_user_id, discord_name, username, account_user_id)
            return "updated"
        await self._execute(
            "INSERT INTO accounts (discord_user_id, discord_name, token_enc, "
            "account_user_id, username) VALUES ($1,$2,$3,$4,$5)",
            discord_user_id, discord_name, enc, account_user_id, username)
        return "added"

    async def list_user_accounts(self, discord_user_id: int) -> list[asyncpg.Record]:
        return await self._fetch(
            "SELECT id, username, account_user_id, active, last_error "
            "FROM accounts WHERE discord_user_id=$1 ORDER BY added_at", discord_user_id)

    async def all_active_accounts(self) -> list[asyncpg.Record]:
        return await self._fetch(
            "SELECT id, discord_user_id, token_enc, username, account_user_id "
            "FROM accounts WHERE active=TRUE")

    async def remove_account(self, account_id: int, discord_user_id: int) -> bool:
        res = await self._execute(
            "DELETE FROM accounts WHERE id=$1 AND discord_user_id=$2",
            account_id, discord_user_id)
        return res.endswith("1")  # "DELETE 1" = ลบสำเร็จ

    async def mark_error(self, account_id: int, err: str) -> None:
        await self._execute(
            "UPDATE accounts SET active=FALSE, last_error=$1 WHERE id=$2", err, account_id)

    # ── completions / leaderboard ───────────────────────────────
    async def record_completion(self, account_id: int, quest_id: str, quest_name: str) -> bool:
        """คืน True ถ้าเป็นการบันทึกใหม่ (ยังไม่เคยนับ)"""
        res = await self._execute(
            "INSERT INTO completions (account_id, quest_id, quest_name) "
            "VALUES ($1,$2,$3) ON CONFLICT DO NOTHING", account_id, quest_id, quest_name)
        return res.endswith("1")  # "INSERT 0 1" = แถวใหม่

    async def count_for_account(self, account_id: int) -> int:
        return await self._fetchval(
            "SELECT COUNT(*) FROM completions WHERE account_id=$1", account_id)

    async def leaderboard(self, limit: int = 10) -> list[asyncpg.Record]:
        """อันดับรายคน (รวมทุก token ของคนนั้น)"""
        return await self._fetch(
            "SELECT a.discord_user_id, MAX(a.discord_name) AS name, "
            "       COUNT(c.id) AS total, COUNT(DISTINCT a.id) AS accounts "
            "FROM accounts a LEFT JOIN completions c ON c.account_id=a.id "
            "GROUP BY a.discord_user_id ORDER BY total DESC, accounts DESC LIMIT $1", limit)

    async def user_rank(self, discord_user_id: int) -> tuple[int | None, int, int]:
        """คืน (อันดับ, จำนวนเควส, จำนวนคนทั้งหมด) ของผู้ใช้คนนี้"""
        rows = await self._fetch(
            "SELECT a.discord_user_id, COUNT(c.id) AS total "
            "FROM accounts a LEFT JOIN completions c ON c.account_id=a.id "
            "GROUP BY a.discord_user_id ORDER BY total DESC")
        for i, r in enumerate(rows):
            if r["discord_user_id"] == discord_user_id:
                return i + 1, int(r["total"]), len(rows)
        return None, 0, len(rows)

    async def global_stats(self) -> asyncpg.Record:
        """สถิติรวมทั้งระบบ"""
        return await self._fetchrow(
            "SELECT (SELECT COUNT(*) FROM accounts WHERE active) AS accounts, "
            "       (SELECT COUNT(DISTINCT discord_user_id) FROM accounts) AS users, "
            "       (SELECT COUNT(*) FROM completions) AS quests")

    # ── panels (ไว้ auto-refresh) ───────────────────────────────
    async def add_panel(self, channel_id: int, message_id: int) -> None:
        await self._execute(
            "INSERT INTO panels (message_id, channel_id) VALUES ($1,$2) "
            "ON CONFLICT (message_id) DO NOTHING", message_id, channel_id)

    async def all_panels(self) -> list[asyncpg.Record]:
        return await self._fetch("SELECT message_id, channel_id FROM panels")

    async def remove_panel(self, message_id: int) -> None:
        await self._execute("DELETE FROM panels WHERE message_id=$1", message_id)

    # ── user preferences ────────────────────────────────────────
    async def get_presence_pref(self, discord_user_id: int) -> bool:
        """โชว์ presence บนโปรไฟล์ไหม (default เปิด)"""
        v = await self._fetchval(
            "SELECT presence_enabled FROM user_prefs WHERE discord_user_id=$1", discord_user_id)
        return True if v is None else bool(v)

    async def set_presence_pref(self, discord_user_id: int, enabled: bool) -> None:
        await self._execute(
            "INSERT INTO user_prefs (discord_user_id, presence_enabled) VALUES ($1,$2) "
            "ON CONFLICT (discord_user_id) DO UPDATE SET presence_enabled=excluded.presence_enabled",
            discord_user_id, enabled)

    # ── notify embed (สรุปเควสเสร็จ ต่อคน) ───────────────────────
    async def get_notify_msg(self, uid: int) -> asyncpg.Record | None:
        return await self._fetchrow(
            "SELECT channel_id, message_id FROM notify_msgs WHERE discord_user_id=$1", uid)

    async def set_notify_msg(self, uid: int, channel_id: int, message_id: int) -> None:
        await self._execute(
            "INSERT INTO notify_msgs (discord_user_id, channel_id, message_id) VALUES ($1,$2,$3) "
            "ON CONFLICT (discord_user_id) DO UPDATE SET channel_id=excluded.channel_id, "
            "message_id=excluded.message_id",
            uid, channel_id, message_id)

    async def recent_completions(self, uid: int, limit: int = 12) -> list[asyncpg.Record]:
        return await self._fetch(
            "SELECT a.username, c.quest_name, c.completed_at "
            "FROM completions c JOIN accounts a ON a.id=c.account_id "
            "WHERE a.discord_user_id=$1 ORDER BY c.completed_at DESC LIMIT $2", uid, limit)

    async def user_completion_total(self, uid: int) -> int:
        return await self._fetchval(
            "SELECT COUNT(*) FROM completions c JOIN accounts a ON a.id=c.account_id "
            "WHERE a.discord_user_id=$1", uid) or 0
