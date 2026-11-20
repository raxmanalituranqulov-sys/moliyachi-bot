"""Moliyachi bot uchun ma'lumotlar bazasi.

DATABASE_URL berilsa — PostgreSQL (masalan, tekin Neon), aks holda lokal SQLite fayl.
"""
import os
import sqlite3
from contextlib import contextmanager

DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_PATH = os.getenv("DB_PATH", "moliya.db")
PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

if PG:
    import psycopg
    from psycopg.rows import dict_row


@contextmanager
def connect():
    if PG:
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _q(sql: str) -> str:
    """SQLite '?' belgilarini Postgres '%s' ga almashtiradi."""
    return sql.replace("?", "%s") if PG else sql


def _exec(sql: str, params=()):
    with connect() as c:
        cur = c.execute(_q(sql), params)
        return cur.rowcount


def _one(sql: str, params=()):
    with connect() as c:
        return c.execute(_q(sql), params).fetchone()


def _all(sql: str, params=()):
    with connect() as c:
        return c.execute(_q(sql), params).fetchall()


def init_db():
    id_col = "BIGSERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    money = "DOUBLE PRECISION" if PG else "REAL"
    statements = [
        """CREATE TABLE IF NOT EXISTS users (
            user_id      BIGINT PRIMARY KEY,
            name         TEXT,
            joined_at    TEXT NOT NULL,
            last_report  TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS transactions (
            id          {id_col},
            user_id     BIGINT NOT NULL,
            type        TEXT NOT NULL CHECK (type IN ('kirim', 'chiqim')),
            amount      {money} NOT NULL CHECK (amount > 0),
            category    TEXT NOT NULL,
            note        TEXT,
            created_at  TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_tx_user_date ON transactions (user_id, created_at)",
    ]
    with connect() as c:
        for s in statements:
            c.execute(s)


# ---------- Foydalanuvchilar ----------

def add_user(user_id: int, name: str, now: str):
    _exec(
        "INSERT INTO users (user_id, name, joined_at) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id) DO NOTHING",
        (user_id, name, now),
    )


def get_user(user_id: int):
    return _one("SELECT * FROM users WHERE user_id = ?", (user_id,))


def all_users():
    return _all("SELECT user_id, last_report FROM users")


def set_last_report(user_id: int, month: str):
    _exec("UPDATE users SET last_report = ? WHERE user_id = ?", (month, user_id))


# ---------- Amallar ----------

def add_transaction(user_id, tx_type, amount, category, note, created_at) -> int:
    row = _one(
        "INSERT INTO transactions (user_id, type, amount, category, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) RETURNING id",
        (user_id, tx_type, amount, category, note, created_at),
    )
    return row["id"]


def delete_transaction(user_id: int, tx_id: int) -> bool:
    return _exec("DELETE FROM transactions WHERE id = ? AND user_id = ?", (tx_id, user_id)) > 0


def recent(user_id: int, limit: int = 10):
    return _all(
        "SELECT * FROM transactions WHERE user_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (user_id, limit),
    )


def balance(user_id: int):
    """Umumiy (barcha vaqt) kirim va chiqim."""
    row = _one(
        "SELECT "
        " COALESCE(SUM(CASE WHEN type='kirim' THEN amount END), 0) AS kirim,"
        " COALESCE(SUM(CASE WHEN type='chiqim' THEN amount END), 0) AS chiqim "
        "FROM transactions WHERE user_id = ?",
        (user_id,),
    )
    return float(row["kirim"]), float(row["chiqim"])


def month_by_category(user_id: int, month: str):
    """month: 'YYYY-MM'. Turi va kategoriyasi bo'yicha jamlanma."""
    return _all(
        "SELECT type, category, SUM(amount) AS total, COUNT(*) AS cnt "
        "FROM transactions WHERE user_id = ? AND substr(created_at, 1, 7) = ? "
        "GROUP BY type, category ORDER BY total DESC",
        (user_id, month),
    )


def month_transactions(user_id: int, month: str):
    return _all(
        "SELECT * FROM transactions WHERE user_id = ? AND substr(created_at, 1, 7) = ? "
        "ORDER BY created_at",
        (user_id, month),
    )


def monthly_archive(user_id: int):
    """Barcha oylar bo'yicha jamlanma: oy, kirim, chiqim."""
    return _all(
        "SELECT substr(created_at, 1, 7) AS month,"
        " COALESCE(SUM(CASE WHEN type='kirim' THEN amount END), 0) AS kirim,"
        " COALESCE(SUM(CASE WHEN type='chiqim' THEN amount END), 0) AS chiqim "
        "FROM transactions WHERE user_id = ? "
        "GROUP BY substr(created_at, 1, 7) ORDER BY month DESC",
        (user_id,),
    )
