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
        return True, "Локальная БД SQLite ОК"
    except Exception as e:
        return False, f"Ошибка БД: {e}"

def init_db():
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seen_ads (
                id TEXT PRIMARY KEY,
                price INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            cursor.execute("ALTER TABLE seen_ads ADD COLUMN price INTEGER")
        except:
            pass
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Ошибка init_db: {e}")

def get_ad_price(ad_id: str):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT price FROM seen_ads WHERE id = ?", (str(ad_id),))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        print(f"Ошибка get_ad_price: {e}")
        return None

def is_ad_seen(ad_id: str) -> bool:
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM seen_ads WHERE id = ?", (str(ad_id),))
        row = cursor.fetchone()
        conn.close()
        return row is not None
    except Exception as e:
        print(f"Ошибка is_ad_seen: {e}")
        return False

def mark_ad_seen(ad_id: str, price: int):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO seen_ads (id, price) VALUES (?, ?)", (str(ad_id), price))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Ошибка mark_ad_seen: {e}")
