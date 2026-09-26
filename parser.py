import re
import logging
from curl_cffi import requests
from bs4 import BeautifulSoup
import config

logger = logging.getLogger(__name__)

def get_page_html(url: str) -> str:
    """Получает HTML-код страницы с помощью curl_cffi с эмуляцией браузера."""
    try:
        # Используем impersonate="chrome124" для обхода базовой защиты
        response = requests.get(url, impersonate="chrome124", timeout=30)
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
    # Находим все карточки товаров на странице
    items = soup.select('div[data-marker="item"]')

    for item in items:
        try:
            # Извлекаем ID объявления
            ad_id = item.get('data-item-id')
            if not ad_id:
                continue

            # Находим элемент с заголовком и ссылкой
            title_element = item.select_one('a[itemprop="url"]')
            if not title_element:
                continue
            
            title = title_element.get('title', '').strip() or title_element.text.strip()
            link = "https://www.avito.ru" + title_element.get('href', '')

            # Извлекаем цену
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

            # 1. Фильтрация по цене
            if price < config.MIN_PRICE or price > config.MAX_BUY_PRICE:
                continue

            # 2. Фильтрация по стоп-словам в заголовке (регистронезависимо)
            title_lower = title.lower()
            if any(stop_word.lower() in title_lower for stop_word in config.STOP_WORDS):
                continue

            # Расчет профита
            profit = config.ESTIMATED_MARKET - price

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
                'profit': profit,
                'description': description,
                'location': location
            })
        except Exception as e:
            logger.error(f"Ошибка при парсинге объявления {item.get('data-item-id', 'неизвестно')}: {e}")

    return ads
