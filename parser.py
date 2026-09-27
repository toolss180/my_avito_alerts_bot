import os
import re
import logging
import time
import random
from curl_cffi import requests
from bs4 import BeautifulSoup
import config

logger = logging.getLogger(__name__)

# Инициализация постоянной сессии
session = requests.Session(impersonate="chrome120")
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"'
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
        time.sleep(random.uniform(1.5, 3.0))
        response = session.get(url, timeout=30)
        logger.info(f"Авито вернул статус {response.status_code}.")
        if response.status_code == 200:
            return response.text
        else:
            logger.error(f"Ошибка получения страницы. Статус-код: {response.status_code}")
            return ""
    except Exception as e:
        logger.error(f"Сетевая ошибка при запросе к Авито: {e}")
        return ""

def parse_ads(html: str) -> list:
    """Парсит HTML, извлекает карточки товаров (без локальной фильтрации)."""
    ads = []
    if not html:
        return ads

    soup = BeautifulSoup(html, 'html.parser')
    
    items = soup.find_all("div", attrs={"data-marker": "item"})
    if not items:
        items = soup.find_all("div", attrs={"data-item-id": True})
        
    logger.info(f"Найдено сырых карточек в HTML: {len(items)}")
    
    if not items:
        logger.warning(f"Title страницы: {soup.title.string if soup.title else 'Нет тега title'}")

    STOP_WORDS = [
        # Услуги, скупка, работа
        "ремонт", "скупка", "выкуп", "диагностика", "сервис", "мастер", 
        "чистка", "сборка пк", "апгрейд", "настройка", "установка windows",
        # Нерабочее, поломки, доноры
        "нерабоч", "не включается", "на запчасти", "под восстановление", 
        "дефект", "артефакт", "донор", "глючит", "заблокирован", "пароль", "icloud",
        # Приманки и опт
        "цена за", "за 1 шт", "за штуку", "оптом", "аукцион"
    ]

    for item in items:
        try:
            # Получаем ID объявления
            ad_id = item.get('data-item-id')
            if not ad_id:
                continue

            # Ищем ссылку с заголовком
            title_tag = item.find("h3") or item.find("a", attrs={"data-marker": "item-title"})
            if not title_tag or not title_tag.text.strip():
                continue
                
            title = title_tag.text.strip()
            
            link_tag = item.find("a", attrs={"itemprop": "url"}) or item.find("a", attrs={"data-marker": "item-title"})
            link = "https://www.avito.ru" + link_tag.get('href', '') if link_tag else ""

            # Получаем цену
            price_element = item.select_one('meta[itemprop="price"]')
            if price_element:
                price_str = price_element.get('content', '0')
                price = int(price_str)
            else:
                price_text = item.select_one('[data-marker="item-price"]')
                if price_text:
                    price_str = re.sub(r'[^\d]', '', price_text.text)
                    price = int(price_str) if price_str else 0
                else:
                    price = 0

            MIN_PRICE = int(os.getenv("MIN_PRICE", "300"))
            if price < MIN_PRICE:
                logger.info(f"Отсеян лот '{title}' - цена {price} ниже MIN_PRICE {MIN_PRICE}")
                continue

            lower_title = title.lower()
            if any(word in lower_title for word in STOP_WORDS):
                logger.info(f"Отсеян лот '{title}' (найдено стоп-слово)")
                continue

            # Попытка извлечь описание/характеристики для ИИ
            desc_element = item.select_one('[data-marker="item-specific-params"]')
            description = desc_element.text.strip() if desc_element else "Описание не найдено на карточке"

            # Попытка извлечь локацию
            loc_element = item.select_one('[class*="geo-root"]') or item.select_one('[class*="location"]')
            location = loc_element.text.strip() if loc_element else "Не указано"
            
            # Парсинг продавца (best-effort)
            seller_name_tag = item.select_one('[data-marker="seller-info/name"]') or item.select_one('[data-marker="seller-rating/name"]')
            seller_name = seller_name_tag.text.strip() if seller_name_tag else "Не указан"
            
            seller_rating_tag = item.select_one('[data-marker="seller-rating/score"]')
            seller_rating = seller_rating_tag.text.strip() if seller_rating_tag else "—"
            
            seller_reviews_tag = item.select_one('[data-marker="seller-rating/summary"]')
            seller_reviews_count = 0
            if seller_reviews_tag:
                rev_match = re.search(r'\d+', seller_reviews_tag.text)
                if rev_match:
                    seller_reviews_count = int(rev_match.group(0))

            seller_id = None
            seller_link = item.find("a", href=re.compile(r'/user/[^/]+/profile'))
            if seller_link:
                s_match = re.search(r'/user/([^/]+)/profile', seller_link.get('href'))
                if s_match:
                    seller_id = s_match.group(1)

            # Парсинг фото
            photo_url = None
            photo_img = item.select_one('[data-marker="item-photo"] img') or item.select_one('img[itemprop="image"]')
            if photo_img:
                photo_url = photo_img.get('src') or photo_img.get('data-src')

            ads.append({
                'id': ad_id,
                'title': title,
                'price': price,
                'link': link,
                'photo_url': photo_url,
                'description': description,
                'location': location,
                'seller_id': seller_id,
                'seller': {
                    'name': seller_name,
                    'rating': seller_rating,
                    'reviews': seller_reviews_count
                }
            })
        except Exception as e:
            logger.error(f"Ошибка при парсинге объявления {item.get('data-item-id', 'неизвестно')}: {e}")

    return ads
