import libsql_experimental as libsql
import config
import logging

logger = logging.getLogger(__name__)

LOCAL_DB_NAME = "avito_ads.db"

def _get_connection():
    """Возвращает подключение к Turso, если заданы ключи, иначе к локальной SQLite."""
    if config.TURSO_DATABASE_URL:
        return libsql.connect(
            config.TURSO_DATABASE_URL,
            auth_token=config.TURSO_AUTH_TOKEN
        )
    else:
        return libsql.connect(LOCAL_DB_NAME)

def init_db():
    """Инициализация базы данных и создание таблицы, если её нет."""
    try:
        conn = _get_connection()
        conn.execute('''
            CREATE TABLE IF NOT EXISTS seen_ads (
                id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
    except Exception as e:
        logger.error(f"Ошибка БД (init_db): {e}")

def check_connection() -> tuple[bool, str]:
    """Проверяет подключение к БД и выполняет тестовый запрос."""
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT count(*) FROM seen_ads")
        cursor.fetchone()
        
        if config.TURSO_DATABASE_URL:
            return True, "Turso (libSQL)"
        else:
            return True, "Локальный SQLite"
    except Exception as e:
        return False, str(e)

def is_ad_seen(ad_id: str) -> bool:
    """Проверяет, было ли уже просмотрено объявление с данным ID."""
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM seen_ads WHERE id = ?', (ad_id,))
        result = cursor.fetchone()
        return result is not None
    except Exception as e:
        logger.error(f"Ошибка БД (is_ad_seen): {e}")
        return False

def mark_ad_seen(ad_id: str):
    """Добавляет ID объявления в базу данных просмотренных."""
    try:
        conn = _get_connection()
        conn.execute('INSERT OR IGNORE INTO seen_ads (id) VALUES (?)', (ad_id,))
        conn.commit()
    except Exception as e:
        logger.error(f"Ошибка БД (mark_ad_seen): {e}")
