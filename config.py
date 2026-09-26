import os

# Подключение к базе данных Turso
TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

# Токен вашего Telegram бота (получить у @BotFather)
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")

# Список ID администраторов, которым будут приходить уведомления (через запятую)
ADMIN_IDS = [x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# Ссылка на поиск Авито. 
TARGET_URL = os.getenv("TARGET_URL", "https://www.avito.ru/all/telefony/mobilnye_telefony/apple-ASgBAgICAkS0wA3OqzmwwQ2I_Dc?s=104&q=iphone+13+pro")

# Минимальная цена, чтобы отсечь мусор
MIN_PRICE = int(os.getenv("MIN_PRICE", 15000))
# Максимальная цена выкупа для перепродажи
MAX_BUY_PRICE = int(os.getenv("MAX_BUY_PRICE", 28000))
# Оценочная рыночная цена для расчета профита
ESTIMATED_MARKET = int(os.getenv("ESTIMATED_MARKET", 35000))

# Список стоп-слов (регистронезависимо)
STOP_WORDS = [
    "коробка", "чехол", "запчасти", "донор", "битый", 
    "не включается", "icloud", "mdm", "заблокирован", 
    "скупка", "обмен", "копия"
]

# Интервалы задержки между проверками (в секундах)
MIN_DELAY = int(os.getenv("MIN_DELAY", 150))
MAX_DELAY = int(os.getenv("MAX_DELAY", 240))
