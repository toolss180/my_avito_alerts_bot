import re
import logging
import time
import random
from curl_cffi import requests
from bs4 import BeautifulSoup
import config

logger = logging.getLogger(__name__)

# Инициализация постоянной сессии
session = requests.Session(impersonate="chrome124")
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "Sec-Ch-Ua-Mobile": "?1",
    "Sec-Ch-Ua-Platform": '"Android"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1"
})

def warmup_session():
    """Сделать один тихий GET-запрос на главную с задержкой 2-3 секунды, чтобы получить стартовые куки."""
    try:
        logger.info("Прогрев сессии (Avito)...")
        session.get("https://www.avito.ru", timeout=30)
        time.sleep(random.uniform(2.0, 3.0))
    except Exception as e:
        logger.error(f"Ошибка при прогреве сессии: {e}")

def get_page_html(url: str) -> str:
    """Загружает HTML-код страницы с помощью curl_cffi с сессией."""
    try:
        time.sleep(random.uniform(1.5, 3.5))
        response = session.get(url, timeout=30)
        if response.status_code == 200:
            return response.text
        else:
            logger.error(f"Ошибка получения страницы. Статус-код: {response.status_code}")
            return ""
    except Exception as e:
        logger.error(f"Сетевая ошибка при запросе к Авито: {e}")
        return ""

def parse_ads(html: str) -> list:
    """Парсит HTML, извлекает карточки товаров и фильтрует их."""
    ads = []
    if not html:
        return ads

    soup = BeautifulSoup(html, 'html.parser')
    # Ищем все карточки товаров на странице
    items = soup.select('div[data-marker="item"]')

    for item in items:
        try:
            # Получаем ID объявления
            ad_id = item.get('data-item-id')
            if not ad_id:
                continue

            # Ищем ссылку с заголовком и ссылкой
            title_element = item.select_one('a[itemprop="url"]')
            if not title_element:
                continue
            
            title = title_element.get('title', '').strip() or title_element.text.strip()
            link = "https://www.avito.ru" + title_element.get('href', '')

            # Получаем цену
            price_element = item.select_one('meta[itemprop="price"]')
            if price_element:
                price_str = price_element.get('content', '0')
                price = int(price_str)
            else:
                # Альтернативный способ парсинга цены, если meta тег отсутствует
                price_text = item.select_one('[data-marker="item-price"]')
                if price_text:
                    price_str = re.sub(r'[^\d]', '', price_text.text)
                    price = int(price_str) if price_str else 0
                else:
                    price = 0

            # 1. Фильтрация по минимальной цене (отсекаем мусор)
            if price < config.MIN_PRICE:
                continue

            # 2. Фильтрация по стоп-словам в заголовке (регистронезависимо)
            title_lower = title.lower()
            if any(stop_word.lower() in title_lower for stop_word in config.STOP_WORDS):
                continue

            # Попытка извлечь описание/характеристики для ИИ
            desc_element = item.select_one('[data-marker="item-specific-params"]')
            description = desc_element.text.strip() if desc_element else "Описание не найдено на карточке"

            # Попытка извлечь локацию
            loc_element = item.select_one('[class*="geo-root"]') or item.select_one('[class*="location"]')
            location = loc_element.text.strip() if loc_element else "Не указано"

            ads.append({
                'id': ad_id,
                'title': title,
                'price': price,
                'link': link,
                'description': description,
                'location': location
            })
        except Exception as e:
            logger.error(f"Ошибка при парсинге объявления {item.get('data-item-id', 'неизвестно')}: {e}")

    return ads
