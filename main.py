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
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def send_startup_notification(db_status: str):
    """Отправляет сервисное сообщение о запуске бота администраторам через Relay."""
    if not config.RELAY_URL:
        logger.error("RELAY_URL не задан или сервер Render недоступен.")
        return
        
    text = f"🟢 Бот мониторинга Авито успешно запущен на локальном сервере! Статус БД: {db_status}. Категорий: {len(config.TARGET_URLS)}."
    
    logger.info("Отправка уведомлений о запуске...")
    for admin_id in config.ADMIN_IDS:
        payload = {
            "chat_id": admin_id,
            "text": text
        }
        try:
            response = requests.post(config.RELAY_URL, json=payload, impersonate="chrome124", timeout=15)
            if response.status_code == 200:
                logger.info(f"Уведомление о старте доставлено через Render (admin {admin_id})")
            else:
                logger.error(f"Сбой реле при старте ({admin_id}): код {response.status_code}, тело {response.text}")
        except Exception as e:
            logger.error(f"Сетевая ошибка при отправке стартового уведомления админу {admin_id}: {e}")

def send_telegram_alert(ad: dict):
    """Отправляет сырые данные лота на Relay для ИИ-анализа и пересылки."""
    if not config.RELAY_URL:
        logger.error("RELAY_URL не задан или сервер Render недоступен.")
        return
        
    for admin_id in config.ADMIN_IDS:
        payload = {
            "chat_id": admin_id,
            "id": ad.get("id"),
            "title": ad.get("title"),
            "price": ad.get("price"),
            "url": ad.get("link"),
            "description": ad.get("description", ""),
            "location": ad.get("location", "Не указано")
        }
            
        try:
            response = requests.post(config.RELAY_URL, json=payload, impersonate="chrome124", timeout=15)
            if response.status_code == 200:
                logger.info(f"Запрос успешно передан на Render (admin {admin_id})")
            else:
                logger.error(f"Сбой реле ({admin_id}): код {response.status_code}, тело {response.text}")
        except Exception as e:
            logger.error(f"Сетевая ошибка при отправке админу {admin_id}: {e}")

def main():
    logger.info("Запуск бота для мониторинга Авито...")
    
    # Запуск фонового HTTP-сервера для локального хостинга
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

    # Прогрев сессии парсера
    parser.warmup_session()

    while True:
        for url in config.TARGET_URLS:
            logger.info(f"Проверка Авито по ссылке: {url[:60]}...")
            
            # Загрузка и парсинг страницы
            html = parser.get_page_html(url)
            ads = parser.parse_ads(html)
            
            new_ads_count = 0
            for ad in ads:
                # Фильтрация невалидных лотов
                if not ad['title'] or ad['price'] == 0:
                    continue
                    
                # Фильтрация по минимальной цене
                if ad['price'] < config.MIN_PRICE:
                    logger.info(f"Отсеян лот '{ad['title']}' - цена {ad['price']} ниже MIN_PRICE {config.MIN_PRICE}")
                    continue
                    
                # Фильтрация по стоп-словам
                title_lower = ad['title'].lower()
                if any(sw.lower() in title_lower for sw in config.STOP_WORDS):
                    logger.info(f"Отсеян лот '{ad['title']}' - найдено стоп-слово")
                    continue
                
                # Проверка наличия объявления в базе данных
                if not database.is_ad_seen(ad['id']):
                    new_ads_count += 1
                    logger.info(f"Новый лот: {ad['title']} ({ad['price']} ₽) - ID: {ad['id']}")
                    
                    # Отправка сырых данных на Relay
                    send_telegram_alert(ad)
                    
                    # Сохранение ID объявления в базу
                    database.mark_ad_seen(ad['id'])
                    
                    # Небольшая задержка, чтобы не спамить Relay
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
