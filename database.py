import os
import sqlite3
import logging

logger = logging.getLogger(__name__)

# BUG-2 FIX: абсолютный путь, чтобы БД не создавалась в случайной CWD
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "avito_local.db")


def _connect():
    """Создаёт соединение с WAL-режимом и таймаутом на блокировку."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def check_connection() -> tuple[bool, str]:
    try:
        with _connect() as conn:
            conn.execute("SELECT 1")
        return True, "Локальная БД SQLite OK"
    except Exception as e:
        return False, f"Ошибка БД: {e}"


def init_db():
    try:
        with _connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_items (
                    lot_id TEXT PRIMARY KEY,
                    title TEXT,
                    price INTEGER,
                    url TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    key TEXT PRIMARY KEY,
                    value INTEGER DEFAULT 0
                )
            """)
            default_stats = [
                'total_scanned', 'filtered_price',
                'filtered_stopwords', 'sent_to_server', 'deals_found'
            ]
            for key in default_stats:
                conn.execute(
                    "INSERT OR IGNORE INTO stats (key, value) VALUES (?, 0)",
                    (key,)
                )
    except Exception as e:
        logger.error(f"Ошибка init_db: {e}")


def get_ad_price(lot_id: str):
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT price FROM seen_items WHERE lot_id = ?",
                (str(lot_id),)
            ).fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"Ошибка get_ad_price: {e}")
        return None


def is_lot_seen(lot_id: str) -> bool:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_items WHERE lot_id = ?",
                (str(lot_id),)
            ).fetchone()
        return row is not None
    except Exception as e:
        logger.error(f"Ошибка is_lot_seen: {e}")
        return False


def save_seen_lot(lot_id: str, title: str, price: int, url: str):
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO seen_items (lot_id, title, price, url) VALUES (?, ?, ?, ?)",
                (str(lot_id), str(title), price, str(url))
            )
    except Exception as e:
        logger.error(f"Ошибка save_seen_lot: {e}")


def increment_stat(stat_key: str):
    try:
        with _connect() as conn:
            conn.execute(
                "UPDATE stats SET value = value + 1 WHERE key = ?",
                (stat_key,)
            )
    except Exception as e:
        logger.error(f"Ошибка increment_stat: {e}")


def get_stats_summary() -> dict:
    stats = {}
    try:
        with _connect() as conn:
            rows = conn.execute("SELECT key, value FROM stats").fetchall()
        for row in rows:
            stats[row[0]] = row[1]
    except Exception as e:
        logger.error(f"Ошибка get_stats_summary: {e}")
    return stats


def get_db_lots_count() -> int:
    try:
        with _connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM seen_items").fetchone()
        return row[0] if row else 0
    except Exception as e:
        logger.error(f"Ошибка get_db_lots_count: {e}")
        return 0


def cleanup_old_lots(days: int = 10):
    try:
        with _connect() as conn:
            cur = conn.execute(
                "DELETE FROM seen_items WHERE created_at < datetime('now', ?)",
                (f'-{days} days',)
            )
            if cur.rowcount > 0:
                logger.info(f"Очистка БД: удалено {cur.rowcount} старых лотов (>{days} дней)")
    except Exception as e:
        logger.error(f"Ошибка cleanup_old_lots: {e}")
