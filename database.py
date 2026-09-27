import sqlite3
import datetime

DB_PATH = "avito_local.db"

def get_connection():
    return sqlite3.connect(DB_PATH)

def check_connection() -> tuple[bool, str]:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        conn.close()
        return True, "Локальная БД SQLite OK"
    except Exception as e:
        return False, f"Ошибка БД: {e}"

def init_db():
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seen_items (
                lot_id TEXT PRIMARY KEY,
                title TEXT,
                price INTEGER,
                url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        """)
        # Initialize default stats if not exists
        default_stats = ['total_scanned', 'filtered_price', 'filtered_stopwords', 'sent_to_server', 'deals_found']
        for key in default_stats:
            cursor.execute("INSERT OR IGNORE INTO stats (key, value) VALUES (?, 0)", (key,))
            
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Ошибка init_db: {e}")

def get_ad_price(lot_id: str):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT price FROM seen_items WHERE lot_id = ?", (str(lot_id),))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"Ошибка get_ad_price: {e}")
        return None

def is_lot_seen(lot_id: str) -> bool:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM seen_items WHERE lot_id = ?", (str(lot_id),))
        row = cursor.fetchone()
        conn.close()
        return row is not None
    except Exception as e:
        print(f"Ошибка is_lot_seen: {e}")
        return False

def save_seen_lot(lot_id: str, title: str, price: int, url: str):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO seen_items (lot_id, title, price, url) VALUES (?, ?, ?, ?)",
            (str(lot_id), str(title), price, str(url))
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Ошибка save_seen_lot: {e}")

def increment_stat(stat_key: str):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE stats SET value = value + 1 WHERE key = ?", (stat_key,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Ошибка increment_stat: {e}")

def get_stats_summary() -> dict:
    stats = {}
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM stats")
        rows = cursor.fetchall()
        for row in rows:
            stats[row[0]] = row[1]
        conn.close()
    except Exception as e:
        print(f"Ошибка get_stats_summary: {e}")
    return stats

def get_db_lots_count() -> int:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM seen_items")
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        print(f"Ошибка get_db_lots_count: {e}")
        return 0

def cleanup_old_lots(days=10):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM seen_items WHERE created_at < datetime('now', ?)", (f'-{days} days',))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Ошибка cleanup_old_lots: {e}")
