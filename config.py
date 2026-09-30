import os
from dotenv import load_dotenv

# Загружаем переменные из .env файла ДО инициализации конфигов
load_dotenv()

# Токен Telegram бота (получить у @BotFather)
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")

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

# Список стоп-слов (регистронезависимо) — единый источник правды
STOP_WORDS = [
    # Услуги, скупка, работа
    "ремонт", "скупка", "выкуп", "диагностика", "сервис", "мастер",
    "чистка", "сборка пк", "апгрейд", "настройка", "установка windows",
    # Нерабочее, поломки, доноры
    "нерабоч", "не включается", "на запчасти", "под восстановление",
    "дефект", "артефакт", "донор", "глючит", "заблокирован", "пароль", "icloud",
    # Аксессуары и мусор
    "коробка", "чехол", "запчасти", "битый", "mdm", "обмен", "копия",
    # Приманки и опт
    "цена за", "за 1 шт", "за штуку", "оптом", "аукцион"
]

# Интервалы задержки между проверками (в секундах)
MIN_DELAY = int(os.getenv("MIN_DELAY", 180))
MAX_DELAY = int(os.getenv("MAX_DELAY", 300))

# Паузы между отдельными запросами внутри круга (в секундах)
REQ_DELAY_MIN = int(os.getenv("REQ_DELAY_MIN", 20))
REQ_DELAY_MAX = int(os.getenv("REQ_DELAY_MAX", 60))

# Ночной режим (часы по локальному времени и множитель)
NIGHT_START = int(os.getenv("NIGHT_START", 0))
NIGHT_END = int(os.getenv("NIGHT_END", 7))
NIGHT_MULTIPLIER = float(os.getenv("NIGHT_MULTIPLIER", 3.0))

# AI Провайдеры (Пулы ключей для ротации)
OPENROUTER_KEYS = [k.strip() for k in os.getenv("OPENROUTER_KEYS", os.getenv("OPENROUTER_API_KEY", "")).split(",") if k.strip()]
GROQ_KEYS = [k.strip() for k in os.getenv("GROQ_KEYS", "").split(",") if k.strip()]

# Категорийные правила маржинальности
CATEGORY_RULES = {
    "peripherals": {
        "keywords": ["клавиатура", "мышь", "наушники", "гарнитура", "кулер", "вентилятор", "корпус", "коврик", "микрофон"],
        "min_profit": 600,
        "min_discount_pct": 35
    },
    "components": {
        "keywords": ["процессор", "материнская плата", "память", "ddr", "ssd", "жесткий диск", "блок питания", "кулер башня"],
        "min_profit": 1200,
        "min_discount_pct": 25
    },
    "high_ticket": {
        "keywords": ["видеокарта", "rtx", "gtx", "radeon", "ноутбук", "системный блок", "пк", "монитор", "playstation", "xbox", "iphone", "ipad", "macbook", "смартфон", "телефон"],
        "min_profit": 3500,
        "min_discount_pct": 20
    }
}
