import time
import random
import logging
from curl_cffi import requests
import config
import database
import parser
from keep_alive import keep_alive

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

def send_telegram_alert(text: str, ad: dict = None):
    """Отправляет отформатированное сообщение всем администраторам."""
    for admin_id in config.ADMIN_IDS:
        payload = {
            "chat_id": admin_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        if ad:
            payload["title"] = ad.get("title")
            payload["price"] = ad.get("price")
            payload["description"] = ad.get("description", "")
            
        try:
            if config.RELAY_URL:
                url = f"{config.RELAY_URL}/send"
                response = requests.post(url, json=payload, impersonate="chrome124", timeout=15)
                if response.status_code == 200:
                    logger.info(f"Уведомление доставлено через Render (admin {admin_id})")
                else:
                    logger.error(f"Сбой реле ({admin_id}): код {response.status_code}, тело {response.text}")
            else:
                url = f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/sendMessage"
                proxies = {"https": config.TG_PROXY} if config.TG_PROXY else None
                response = requests.post(url, json=payload, impersonate="chrome124", timeout=10, proxies=proxies)
                if response.status_code != 200:
                    logger.error(f"Ошибка отправки админу {admin_id}. Код: {response.status_code}, Ответ: {response.text}")
        except Exception as e:
            logger.error(f"Сетевая ошибка при отправке админу {admin_id}: {e}")

def send_startup_notification(db_status: str):
    """Отправляет сервисное сообщение о запуске бота администраторам."""
    text = f"🟢 Бот мониторинга Авито успешно запущен на локальном сервере! Статус БД: {db_status}. Категорий: {len(config.TARGET_URLS)}."
    
    logger.info("Отправка уведомлений о запуске...")
    for admin_id in config.ADMIN_IDS:
        payload = {
            "chat_id": admin_id,
            "text": text
        }
        try:
            if config.RELAY_URL:
                url = f"{config.RELAY_URL}/send"
                response = requests.post(url, json=payload, impersonate="chrome124", timeout=15)
                if response.status_code == 200:
                    logger.info(f"Уведомление о старте доставлено через Render (admin {admin_id})")
                else:
                    logger.error(f"Сбой реле при старте ({admin_id}): код {response.status_code}, тело {response.text}")
            else:
                url = f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/sendMessage"
                proxies = {"https": config.TG_PROXY} if config.TG_PROXY else None
                response = requests.post(url, json=payload, impersonate="chrome124", timeout=10, proxies=proxies)
                if response.status_code != 200:
                    logger.error(f"Ошибка отправки в TG ({admin_id}): код {response.status_code}, тело {response.text}")
                else:
                    logger.info(f"Уведомление о старте отправлено админу {admin_id}.")
        except Exception as e:
            logger.error(f"Сетевая ошибка при отправке стартового уведомления админу {admin_id}: {e}")

def format_message(ad: dict) -> str:
    """Форматирует данные объявления в HTML-сообщение для Telegram (используется без реле)."""
    location = ad.get("location", "Не указано")
    return (
        f"📦 <b>{ad['title']}</b>\n"
        f"💰 <b>Цена продавца:</b> {ad['price']} ₽\n"
        f"📍 <b>Локация:</b> {location}\n"
        f"🔗 <a href='{ad['link']}'>Открыть объявление на Авито</a>\n\n"
        f"📈 <b>Рынок:</b> {config.ESTIMATED_MARKET} ₽\n"
        f"🤑 <b>Профит:</b> ~{ad['profit']} ₽"
    )

def main():
    logger.info("Запуск бота для мониторинга Авито...")
    
    # Запуск фонового HTTP-сервера для Render
    keep_alive()
    logger.info("HTTP-сервер (keep_alive) запущен на фоновом потоке.")

    # Инициализация базы данных
    database.init_db()

    # Проверка БД
    db_ok, db_status = database.check_connection()
    if not db_ok:
        logger.error(f"Критическая ошибка БД: {db_status}")
        db_status = f"Ошибка: {db_status}"
    else:
        logger.info(f"База данных успешно подключена: {db_status}")

    # Отправка приветственного сообщения
    send_startup_notification(db_status)

    while True:
        for url in config.TARGET_URLS:
            logger.info(f"Проверка Авито по ссылке: {url[:60]}...")
            
            # Загрузка и парсинг страницы
            html = parser.get_page_html(url)
            ads = parser.parse_ads(html)
            
            logger.info(f"Найдено {len(ads)} лотов, подходящих под фильтры (цена, стоп-слова).")

            new_ads_count = 0
            for ad in ads:
                # Проверка наличия объявления в базе данных
                if not database.is_ad_seen(ad['id']):
                    new_ads_count += 1
                    logger.info(f"Новый лот: {ad['title']} ({ad['price']} ₽) - ID: {ad['id']}")
                    
                    # Формирование и отправка сообщения в Telegram
                    message = format_message(ad)
                    send_telegram_alert(message, ad)
                    
                    # Сохранение ID объявления в базу, чтобы не отправлять повторно
                    database.mark_ad_seen(ad['id'])
                    
                    # Небольшая задержка, чтобы не спамить в Telegram (если лотов сразу много)
                    time.sleep(1)

            logger.info(f"Обработано {new_ads_count} новых лотов для этой ссылки.")
            
            # Пауза между проверками разных ссылок (5-10 секунд)
            if len(config.TARGET_URLS) > 1:
                time.sleep(random.randint(5, 10))

        # Рандомизированная задержка перед следующим полным циклом
        delay = random.randint(config.MIN_DELAY, config.MAX_DELAY)
        logger.info(f"Ожидание {delay} секунд до следующего полного цикла проверок...\n")
        time.sleep(delay)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Работа бота остановлена пользователем.")
    except Exception as e:
        logger.exception(f"Критическая ошибка в работе бота: {e}")
