import time
import random
import logging
import requests
import threading

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
            response = requests.post(config.RELAY_URL, json=payload, timeout=15)
            if response.status_code == 200:
                logger.info(f"Уведомление о старте доставлено через Render (admin {admin_id})")
            else:
                logger.error(f"Сбой реле при старте ({admin_id}): код {response.status_code}, тело {response.text}")
        except Exception as e:
            logger.error(f"Сетевая ошибка при отправке стартового уведомления админу {admin_id}: {e}")

def send_heartbeat():
    if not config.RELAY_URL: return
    relay_url = config.RELAY_URL
    ping_url = relay_url.replace("/send", "/ping") if relay_url.endswith("/send") else f"{relay_url.rstrip('/')}/ping"
    try:
        payload = {
            "status": "alive",
            "stats": database.get_stats_summary(),
            "db_lots_count": database.get_db_lots_count()
        }
        requests.post(ping_url, json=payload, timeout=5)
    except Exception as e:
        logger.debug(f"Heartbeat failed: {e}")

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
            "photo_url": ad.get("photo_url"),
            "description": ad.get("description", ""),
            "location": ad.get("location", "Не указано"),
            "seller_id": ad.get("seller_id"),
            "seller": ad.get("seller", {}),
            "is_price_drop": ad.get("is_price_drop", False),
            "old_price": ad.get("old_price")
        }
            
        try:
            response = requests.post(config.RELAY_URL, json=payload, timeout=15)
            if response.status_code == 200:
                logger.info(f"Запрос успешно передан на Render (admin {admin_id})")
            else:
                logger.error(f"Сбой реле ({admin_id}): код {response.status_code}, тело {response.text}")
        except Exception as e:
            logger.error(f"Сетевая ошибка при отправке админу {admin_id}: {e}")

def main():
    logger.info("Запуск бота для мониторинга Авито...")
    
    keep_alive()
    logger.info("HTTP-сервер (keep_alive) запущен на фоновом потоке.")

    database.init_db()

    db_ok, db_status = database.check_connection()
    if not db_ok:
        logger.error(f"Критическая ошибка БД: {db_status}")
        db_status = f"Ошибка: {db_status}"
    else:
        logger.info(f"База данных успешно подключена: {db_status}")

    send_startup_notification(db_status)

    parser.warmup_session()
    
    send_heartbeat()
    
    def ping_loop():
        while True:
            time.sleep(60)
            send_heartbeat()
            
    threading.Thread(target=ping_loop, daemon=True).start()

    while True:
        database.cleanup_old_lots(10)
        for url in config.TARGET_URLS:
            logger.info(f"Проверка Авито по ссылке: {url[:60]}...")
            
            html = parser.get_page_html(url)
            ads = parser.parse_ads(html)
            
            new_ads_count = 0
            for ad in ads:
                if not ad['title'] or ad['price'] == 0:
                    continue
                
                database.increment_stat("total_scanned")
                
                # Check DB first
                if database.is_lot_seen(ad['id']):
                    # Check for price drop
                    old_price = database.get_ad_price(ad['id'])
                    if old_price is not None and ad['price'] < old_price:
                        logger.info(f"📉 Снижение цены на лот {ad['id']}: было {old_price} ₽, стало {ad['price']} ₽")
                        ad['is_price_drop'] = True
                        ad['old_price'] = old_price
                        database.save_seen_lot(ad['id'], ad['title'], ad['price'], ad['link'])
                    else:
                        continue
                else:
                    new_ads_count += 1
                    ad['is_price_drop'] = False
                    ad['old_price'] = None
                    
                if ad['price'] < config.MIN_PRICE:
                    logger.info(f"Отсеян лот '{ad['title']}' - цена {ad['price']} ниже MIN_PRICE {config.MIN_PRICE}")
                    database.increment_stat("filtered_price")
                    database.save_seen_lot(ad['id'], ad['title'], ad['price'], ad['link'])
                    continue
                    
                title_lower = ad['title'].lower()
                if any(sw.lower() in title_lower for sw in config.STOP_WORDS):
                    logger.info(f"Отсеян лот '{ad['title']}' - найдено стоп-слово")
                    database.increment_stat("filtered_stopwords")
                    database.save_seen_lot(ad['id'], ad['title'], ad['price'], ad['link'])
                    continue
                
                logger.info(f"Новый лот отправляется на Render: {ad['title']} ({ad['price']} ₽) - ID: {ad['id']}")
                database.increment_stat("sent_to_server")
                
                send_telegram_alert(ad)
                
                database.save_seen_lot(ad['id'], ad['title'], ad['price'], ad['link'])
                time.sleep(1)

            logger.info(f"Обработано {new_ads_count} новых лотов для этой ссылки.")
            
            if len(config.TARGET_URLS) > 1:
                time.sleep(random.randint(5, 10))

        delay = random.randint(config.MIN_DELAY, config.MAX_DELAY)
        logger.info(f"Ожидание {delay} секунд до следующего полного цикла проверок...\n")
        send_heartbeat()
        time.sleep(delay)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Работа бота остановлена пользователем.")
    except Exception as e:
        logger.exception(f"Критическая ошибка в работе бота: {e}")
