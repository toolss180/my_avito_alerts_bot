import os
from dotenv import load_dotenv

# Загружаем переменные из .env файла ДО инициализации конфигов
load_dotenv()

# Подключение к базе данных Turso
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

# Токен Telegram бота (получить у @BotFather)
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")

# Прокси для Telegram (опционально)
TG_PROXY = os.getenv("TG_PROXY", None)

# URL реле на Render
RELAY_URL = os.getenv("RELAY_URL", "").strip()
if RELAY_URL and not RELAY_URL.endswith("/send"):
    RELAY_URL = f"{RELAY_URL.rstrip('/')}/send"

# Список ID администраторов, которым будут приходить уведомления (через запятую)
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Ссылка на поиск Авито. Теперь поддерживает несколько ссылок через запятую
TARGET_URLS = [u.strip() for u in os.getenv("TARGET_URL", "https://www.avito.ru/all/telefony/mobilnye_telefony/apple-ASgBAgICAkS0wA3OqzmwwQ2I_Dc?s=104&q=iphone+13+pro").split(",") if u.strip()]

# Минимальная цена, чтобы отсечь мусор (аксессуары или спам за 1 руб.)
MIN_PRICE = int(os.getenv("MIN_PRICE", 500))

# Список стоп-слов (регистронезависимо)
STOP_WORDS = [
    "коробка", "чехол", "запчасти", "донор", "битый", 
    "не включается", "icloud", "mdm", "заблокирован", 
    "скупка", "обмен", "копия"
]

# Интервалы задержки между проверками (в секундах)
MIN_DELAY = int(os.getenv("MIN_DELAY", 150))
MAX_DELAY = int(os.getenv("MAX_DELAY", 240))
