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

def send_telegram_alert(text: str):
    """Отправляет отформатированное сообщение всем администраторам."""
    url = f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/sendMessage"
    proxies = {"https": config.TG_PROXY} if config.TG_PROXY else None
    for admin_id in config.ADMIN_IDS:
        payload = {
            "chat_id": admin_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            response = requests.post(url, json=payload, impersonate="chrome124", timeout=10, proxies=proxies)
            if response.status_code != 200:
                logger.error(f"Ошибка отправки админу {admin_id}. Код: {response.status_code}, Ответ: {response.text}")
        except Exception as e:
            logger.error(f"Сетевая ошибка при отправке админу {admin_id}: {e}")

def send_startup_notification(db_status: str):
    """Отправляет сервисное сообщение о запуске бота администраторам."""
    text = f"🟢 Бот мониторинга Авито успешно запущен на локальном сервере! Статус БД: {db_status}."
    url = f"https://api.telegram.org/bot{config.TG_BOT_TOKEN}/sendMessage"
    proxies = {"https": config.TG_PROXY} if config.TG_PROXY else None
    
    logger.info("Отправка уведомлений о запуске...")
    for admin_id in config.ADMIN_IDS:
        payload = {
            "chat_id": admin_id,
            "text": text
        }
        try:
            response = requests.post(url, json=payload, impersonate="chrome124", timeout=10, proxies=proxies)
            if response.status_code != 200:
                logger.error(f"Ошибка отправки в TG ({admin_id}): код {response.status_code}, тело {response.text}")
            else:
                logger.info(f"Уведомление о старте отправлено админу {admin_id}.")
        except Exception as e:
            logger.error(f"Сетевая ошибка при отправке стартового уведомления админу {admin_id}: {e}")

def format_message(ad: dict) -> str:
    """Форматирует данные объявления в HTML-сообщение для Telegram."""
    return (
        f"<b>{ad['title']}</b>\n\n"
        f"💰 Цена: <b>{ad['price']} ₽</b>\n"
        f"📈 Рынок: {config.ESTIMATED_MARKET} ₽\n"
        f"🤑 Профит: <b>~{ad['profit']} ₽</b>\n\n"
        f"<a href='{ad['link']}'>Перейти к объявлению</a>"
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
        logger.info(f"Проверка Авито по ссылке...")
        
        # Загрузка и парсинг страницы
        html = parser.get_page_html(config.TARGET_URL)
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
                send_telegram_alert(message)
                
                # Сохранение ID объявления в базу, чтобы не отправлять повторно
                database.mark_ad_seen(ad['id'])
                
                # Небольшая задержка, чтобы не спамить в Telegram (если лотов сразу много)
                time.sleep(1)

        logger.info(f"Обработано {new_ads_count} новых лотов в этом цикле.")
        
        # Рандомизированная задержка перед следующей проверкой
        delay = random.randint(config.MIN_DELAY, config.MAX_DELAY)
        logger.info(f"Ожидание {delay} секунд до следующей проверки...\n")
        time.sleep(delay)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Работа бота остановлена пользователем.")
    except Exception as e:
        logger.exception(f"Критическая ошибка в работе бота: {e}")
