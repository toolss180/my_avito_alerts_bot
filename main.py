"""
Avito Alerts Termux Client (main.py).
Fetches Avito listings, filters via local database and blacklist rules,
and forwards promising deals to Render relay server.
"""

import json
import logging
import random
import subprocess
import threading
import time

import requests

import config
import database
from keep_alive import keep_alive
import parser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

last_battery_alert_time = 0


def check_battery_status():
    """Checks device battery status via termux-battery-status, alerts on low battery (<=15%), and returns dict."""
    global last_battery_alert_time

    battery_data = {"battery": "?", "charging_status": "UNKNOWN"}

    try:
        result = subprocess.run(
            ["termux-battery-status"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return battery_data

        data = json.loads(result.stdout)
        level = data.get("percentage", 100)
        status = data.get("status", "UNKNOWN")

        battery_data["battery"] = level
        battery_data["charging_status"] = status

        if level <= 15 and status == "DISCHARGING":
            now = time.time()
            if now - last_battery_alert_time >= 1800:  # 30-minute cooldown
                last_battery_alert_time = now
                logger.warning(f"🔋 Батарея критически низкая: {level}% ({status})")

                if config.RELAY_URL:
                    alert_url = config.RELAY_URL.replace("/send", "/battery-alert")
                    try:
                        requests.post(alert_url, json={"battery_level": level}, timeout=15)
                    except requests.exceptions.RequestException as e:
                        logger.warning(f"Не удалось отправить battery alert: {e}")

    except FileNotFoundError:
        logger.debug("termux-battery-status недоступен (не установлен termux-api)")
    except (json.JSONDecodeError, subprocess.TimeoutExpired) as e:
        logger.warning(f"Ошибка проверки батареи: {e}")
    except Exception as e:
        logger.warning(f"Неожиданная ошибка проверки батареи: {e}")

    return battery_data


def send_startup_notification(db_status: str):
    """Sends bot initialization status message to administrators via Relay."""
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
            response = requests.post(config.RELAY_URL, json=payload, timeout=30)
            if response.status_code == 200:
                logger.info(f"Уведомление о старте доставлено через Render (admin {admin_id})")
            else:
                logger.error(f"Сбой реле при старте ({admin_id}): код {response.status_code}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Сетевая ошибка при стартовом уведомлении админу {admin_id}: {e}")


def send_heartbeat(battery_data=None):
    """Sends periodic alive signal with local statistics and battery status to Render."""
    if not config.RELAY_URL:
        return
    relay_url = config.RELAY_URL
    ping_url = relay_url.replace("/send", "/ping") if relay_url.endswith("/send") else f"{relay_url.rstrip('/')}/ping"
    try:
        payload = {
            "status": "alive",
            "stats": database.get_stats_summary(),
            "db_lots_count": database.get_db_lots_count()
        }
        if battery_data:
            payload.update(battery_data)

        requests.post(ping_url, json=payload, timeout=15)
    except requests.exceptions.RequestException as e:
        logger.debug(f"Heartbeat failed: {e}")


def send_telegram_alert(ad: dict):
    """Forwards parsed lot data to Relay for AI valuation and Telegram notification."""
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
            response = requests.post(config.RELAY_URL, json=payload, timeout=30)
            if response.status_code == 200:
                logger.info(f"Запрос успешно передан на Render (admin {admin_id})")
            else:
                logger.error(f"Сбой реле ({admin_id}): код {response.status_code}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Сетевая ошибка при отправке админу {admin_id}: {e}")


def main():
    """Main daemon loop orchestrating scanning, deduplication, price drop tracking, and alert dispatch."""
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

    send_heartbeat(check_battery_status())

    def ping_loop():
        while True:
            time.sleep(60)
            batt_data = check_battery_status()
            send_heartbeat(batt_data)

    threading.Thread(target=ping_loop, daemon=True).start()

    while True:
        database.cleanup_old_lots(10)
        for url in config.TARGET_URLS:
            logger.info(f"Проверка Авито по ссылке: {url[:60]}...")

            html = parser.get_page_html(url)
            ads = parser.parse_ads(html, url)

            page_stats = {
                "scanned": len(ads),
                "price_filtered": 0,
                "word_filtered": 0,
                "duplicates": 0,
                "sent": 0
            }

            for ad in ads:
                try:
                    title = ad.get("title")
                    price = ad.get("price", 0)
                    ad_id = ad.get("id")

                    if not title or price == 0 or not ad_id:
                        continue

                    database.increment_stat("total_scanned")

                    # Check DB for duplicates or price drops
                    if database.is_lot_seen(ad_id):
                        is_drop, old_price = database.check_and_update_price(ad_id, price)
                        if is_drop:
                            logger.info(f"📉 Снижение цены на лот {ad_id}: было {old_price} ₽, стало {price} ₽")
                            ad["is_price_drop"] = True
                            ad["old_price"] = old_price
                            database.save_seen_lot(ad_id, title, price, ad.get("link", ""))
                        else:
                            page_stats["duplicates"] += 1
                            continue
                    else:
                        ad["is_price_drop"] = False
                        ad["old_price"] = None

                    if price < config.MIN_PRICE:
                        database.increment_stat("filtered_price")
                        page_stats["price_filtered"] += 1
                        database.save_seen_lot(ad_id, title, price, ad.get("link", ""))
                        continue

                    title_lower = title.lower()
                    if any(sw.lower() in title_lower for sw in config.STOP_WORDS):
                        database.increment_stat("filtered_stopwords")
                        page_stats["word_filtered"] += 1
                        database.save_seen_lot(ad_id, title, price, ad.get("link", ""))
                        continue

                    logger.info(f"Отправка нового лота {ad_id} на сервер: {title} ({price} ₽)")
                    page_stats["sent"] += 1
                    database.increment_stat("sent_to_server")

                    send_telegram_alert(ad)

                    database.save_seen_lot(ad_id, title, price, ad.get("link", ""))
                    time.sleep(1)
                except Exception as e:
                    logger.error(f"Ошибка обработки лота {ad.get('id')}: {e}", exc_info=True)

            logger.info(
                f"📊 Итог по странице {url[:60]}...: Из {page_stats['scanned']} лотов -> "
                f"{page_stats['duplicates']} дубли, {page_stats['price_filtered']} дешевые, "
                f"{page_stats['word_filtered']} стоп-слова. Отправлено на сервер: {page_stats['sent']}"
            )

            # Free memory in long-running process
            del ads

            if len(config.TARGET_URLS) > 1:
                delay_between = random.uniform(180.0, 300.0)
                logger.info(f"Пауза {delay_between:.1f} сек. перед следующей категорией...")
                time.sleep(delay_between)

        # Heartbeat before sleep interval
        send_heartbeat(check_battery_status())

        delay = random.uniform(config.MIN_DELAY, config.MAX_DELAY)
        logger.info(f"Ожидание {delay:.1f} секунд до следующего полного цикла проверок...\n")
        time.sleep(delay)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Работа бота остановлена пользователем.")
    except Exception as e:
        logger.exception(f"Критическая ошибка в работе бота: {e}")
