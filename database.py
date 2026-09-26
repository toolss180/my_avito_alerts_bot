import sqlite3

DB_PATH = "avito_ads.db"

def get_connection():
    return sqlite3.connect(DB_PATH)

def check_connection() -> tuple[bool, str]:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        conn.close()
        return True, "Локальный SQLite 🟢"
    except Exception as e:
        return False, f"Ошибка подключения к БД: {e}"

def init_db():
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seen_ads (
                id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Ошибка init_db: {e}")

def is_ad_seen(ad_id: str) -> bool:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM seen_ads WHERE id = ?", (str(ad_id),))
        row = cursor.fetchone()
        conn.close()
        return row is not None
    except Exception as e:
        print(f"Ошибка чтения БД: {e}")
        return False

def mark_ad_seen(ad_id: str):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO seen_ads (id) VALUES (?)", (str(ad_id),))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Ошибка записи в БД: {e}")
