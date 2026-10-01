"""
Avito Alerts Relay Service (Render Backend).
Handles AI lot valuation, alert routing to Telegram, webhook commands, and status monitoring.
"""

import base64
from collections import deque
import html
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.parse

from flask import Flask, jsonify, request
import requests as std_requests

import config

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Global monitoring state
CONFIG = {
    "min_profit_rub": 1500,
    "min_profit_percent": 25,
    "is_paused": False
}

STATS = {
    "scanned": 0,
    "filtered": 0,
    "approved": 0,
    "urgent": 0
}

LAST_HEARTBEAT_TIME = 0
PARSER_OFFLINE_ALERT_SENT = False
PHONE_STATS = {}
TOTAL_SEEN_COUNT = 0
SYSTEM_STATUS = {"last_ping": 0, "battery": "?", "charging_status": "UNKNOWN", "blocks_today": 0, "mode": "normal"}

seen_ads = deque(maxlen=2000)
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# AI result cache: avoids calling AI twice for the same lot sent to multiple admins
AI_CACHE: dict = {}
AI_CACHE_TTL = 600  # seconds


def evaluate_lot_cached(ad_id, title, price, photo_url=None):
    """Returns AI evaluation result from cache if fresh, otherwise calls evaluate_lot and caches it."""
    cache_key = f"{ad_id}:{price}"
    now = time.time()

    cached = AI_CACHE.get(cache_key)
    if cached and (now - cached["ts"]) < AI_CACHE_TTL:
        logging.info(f"[AI Cache HIT] {cache_key}")
        return cached["data"]

    result = evaluate_lot(title, price, photo_url)

    if result and result.get("market_used_price"):
        # Evict stale entries when cache grows large
        if len(AI_CACHE) > 500:
            stale_keys = [k for k, v in AI_CACHE.items() if (now - v["ts"]) >= AI_CACHE_TTL]
            for k in stale_keys:
                del AI_CACHE[k]
        AI_CACHE[cache_key] = {"data": result, "ts": now}

    return result

BLACKLIST_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_data.db")


def _bl_connect():
    """Returns SQLite connection to the blacklist database with WAL mode."""
    conn = sqlite3.connect(BLACKLIST_DB, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_blacklist_db():
    """Initializes blacklist table in SQLite if it does not exist."""
    with _bl_connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS blacklist "
            "(seller_id TEXT PRIMARY KEY, banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )


init_blacklist_db()


def is_seller_banned(seller_id):
    """Checks whether the seller is in the blacklist."""
    if not seller_id:
        return False
    with _bl_connect() as conn:
        row = conn.execute("SELECT 1 FROM blacklist WHERE seller_id = ?", (str(seller_id),)).fetchone()
    return bool(row)


def ban_seller(seller_id):
    """Adds a seller ID to the blacklist."""
    if not seller_id:
        return
    with _bl_connect() as conn:
        conn.execute("INSERT OR IGNORE INTO blacklist (seller_id) VALUES (?)", (str(seller_id),))


def get_blacklist_count():
    """Returns the total number of banned sellers."""
    with _bl_connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM blacklist").fetchone()
    return row[0] if row else 0


def detect_category(title):
    """Determines profit and discount threshold rules based on item title."""
    title_low = (title or "").lower()
    for cat, data in config.CATEGORY_RULES.items():
        if any(kw in title_low for kw in data["keywords"]):
            return data
    return config.CATEGORY_RULES.get("components", {"min_profit": 1200, "min_discount_pct": 25})


def download_and_encode_image(url):
    """Downloads an image from URL and converts it to base64 for Gemini vision analysis."""
    if not url:
        return None
    try:
        r = std_requests.get(url, timeout=5)
        if r.status_code == 200:
            return base64.b64encode(r.content).decode("utf-8")
    except Exception as e:
        logging.warning(f"Failed to download image: {e}")
    return None


def watchdog_loop():
    """Background monitor checking if Termux client has ceased sending heartbeats (>20 min)."""
    global PARSER_OFFLINE_ALERT_SENT
    while True:
        time.sleep(60)
        if LAST_HEARTBEAT_TIME > 0:
            time_since_last = time.time() - LAST_HEARTBEAT_TIME
            if time_since_last > 1200 and not PARSER_OFFLINE_ALERT_SENT:
                for admin_id in ADMIN_IDS:
                    send_tg_msg(
                        admin_id,
                        "⚠️ <b>Внимание: Парсер на телефоне перестал отвечать!</b>\n\n"
                        "Последний сигнал был более 20 минут назад. Проверьте Termux на устройстве "
                        "(возможно, система выгрузила процесс из памяти или пропал интернет)."
                    )
                PARSER_OFFLINE_ALERT_SENT = True


threading.Thread(target=watchdog_loop, daemon=True).start()

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📱 Статус Termux"}, {"text": "📊 Статистика"}],
        [{"text": "🧪 Тестовый алерт"}]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}


def set_webhook():
    """Registers Telegram webhook URL and configures bot command menu."""
    token = os.getenv("TG_BOT_TOKEN")
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if token and render_url:
        webhook_url = f"{render_url.rstrip('/')}/telegram-webhook"
        url = f"https://api.telegram.org/bot{token}/setWebhook?url={webhook_url}"
        try:
            res = std_requests.get(url, timeout=10)
            logging.info(f"Webhook set result: {res.text}")
        except Exception as e:
            logging.error(f"Failed to set webhook: {e}")

        commands = [
            {"command": "status", "description": "Проверить статус Termux"},
            {"command": "stats", "description": "Статистика мониторинга"},
            {"command": "test", "description": "Тестовая карточка лота"},
            {"command": "pause", "description": "Приостановить алерты"},
            {"command": "resume", "description": "Возобновить алерты"}
        ]
        try:
            std_requests.post(f"https://api.telegram.org/bot{token}/setMyCommands", json={"commands": commands}, timeout=10)
        except Exception as e:
            logging.warning(f"Failed to set bot commands: {e}")


threading.Thread(target=set_webhook, daemon=True).start()



# Gemini model name — verify current name in Google AI Studio before changing
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")


def ask_gemini(title, price, photo_url=None):
    """Last-resort AI fallback: Evaluates item market value via Google Gemini."""
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"

    cat_rules = detect_category(title)
    min_profit = cat_rules["min_profit"]
    min_discount_pct = cat_rules["min_discount_pct"]

    prompt = f"""Ты профессиональный оценщик компьютерного железа на вторичном рынке РФ (Авито). Если у тебя нет доступа к внешнему веб-поиску, используй свои знания о среднерыночных ценах б/у комплектующих в РФ за 2025-2026 год. Ты ОБЯЗАН вернуть валидный JSON с реалистичной ценой market_used_price. Никогда не возвращай пустые поля или null.
Товар с Авито: '{title}'
Цена продавца: {price} руб.

ЗАДАЧИ:
1. Оцени фото (если приложено): реальное ли это "домашнее" фото товара (видны ли дефекты) или скачано из интернета (стоковое/каталожное).
2. Оцени ликвидность товара для перепродажи: "Высокая (1-3 дня)", "Средняя (до 2 недель)", "Низкая (висяк)".
3. С помощью Google Search найди цену нового товара (или аналога) в рознице РФ (ДНС dns-shop.ru в приоритете, Ситилинк, Яндекс Маркет). Если поиск не работает, назови примерную цену нового по памяти.
4. Определи реальную среднюю цену на вторичном рынке (Б/У).
5. Рассчитай profit_rub = market_used_price - price.
6. Определи is_deal (true/false) по правилам:
   - Если цена товара <= 1500 руб: profit_rub >= 600 руб И цена минимум на 40% ниже розницы ДНС / рынка Б/У.
   - Если цена товара > 1500 руб: profit_rub >= {min_profit} руб И цена минимум на {min_discount_pct}% ниже рынка Б/У и строго дешевле нового в рознице.
   - Если это оверпрайс, мусор, сомнительный лот или откровенно стоковое фото: is_deal: false.

Ответ верни СТРОГО в формате JSON без markdown-оберток:
{{
  "dns_new_price": <число или 0>,
  "market_used_price": <число>,
  "profit_rub": <число>,
  "profit_percent": <число>,
  "liquidity": "<Высокая/Средняя/Низкая>",
  "photo_verdict": "<краткий вердикт по фото>",
  "is_deal": <boolean>,
  "verdict": "<краткое пояснение, 1 предложение>",
  "risks": "<риски при проверке, 1 предложение>"
}}"""

    parts = [{"text": prompt}]

    if photo_url:
        b64_img = download_and_encode_image(photo_url)
        if b64_img:
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": b64_img
                }
            })

    payload = {
        "contents": [{"parts": parts}],
        "tools": [{"googleSearch": {}}]
    }

    try:
        resp = std_requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=25)
        if resp.status_code == 200:
            data = resp.json()
            raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            logging.info(f"Gemini Raw Response: {raw_text[:300]}")
            return raw_text
        else:
            logging.error(f"❌ Ошибка вызова Gemini, status: {resp.status_code}, response: {resp.text[:300]}")
    except Exception as e:
        logging.error(f"❌ Ошибка вызова Gemini: {e}", exc_info=True)

    return None


class KeyManager:
    """Manages pool of API keys with rate-limit cooldown tracking."""

    def __init__(self, keys, provider_name=""):
        self.keys = keys
        self.cooldowns = {k: 0 for k in keys}
        self.provider = provider_name

    def get_key(self):
        now = time.time()
        available = [k for k in self.keys if self.cooldowns[k] < now]
        if not available:
            return None
        return available[0]

    def mark_429(self, key, cooldown_min=5):
        masked = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "***"
        logging.warning(f"[Key Rotation] {self.provider} ключ {masked} словил 429. Уходит в кулдаун на {cooldown_min} минут...")
        self.cooldowns[key] = time.time() + cooldown_min * 60


groq_manager = KeyManager(config.GROQ_KEYS, "Groq")
or_manager = KeyManager(config.OPENROUTER_KEYS, "OpenRouter")


# Groq model — override via GROQ_MODEL env var if the default is deprecated.
# Current production models: openai/gpt-oss-20b, openai/gpt-oss-120b, qwen/qwen3.6-27b
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")


def ask_groq(prompt, json_mode=True):
    """Primary evaluation provider: Ultra-fast inference via Groq."""
    if not groq_manager.keys:
        return None

    url = "https://api.groq.com/openai/v1/chat/completions"

    for _ in range(len(groq_manager.keys)):
        key = groq_manager.get_key()
        if not key:
            logging.warning("Все ключи Groq в кулдауне.")
            break

        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            resp = std_requests.post(url, json=payload, headers=headers, timeout=25)
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"].strip()
                logging.info(f"Groq success ({GROQ_MODEL}): {content[:150]}")
                return content
            elif resp.status_code == 429:
                groq_manager.mark_429(key, cooldown_min=5)
            else:
                logging.error(
                    f"❌ Groq failed: model={GROQ_MODEL}, status={resp.status_code}, "
                    f"response={resp.text[:300]}"
                )
                break
        except Exception as e:
            logging.error(f"❌ Groq request exception: {e}", exc_info=True)
            break

    return None




_or_free_models_cache: list = []
_or_free_models_fetched_at: float = 0.0
_OR_CACHE_TTL = 3600        # 1 hour for successful catalog fetch
_OR_FALLBACK_CACHE_TTL = 300  # 5 minutes for failed/empty catalog


def get_free_models() -> list:
    """Fetches the current list of truly-free text-only OpenRouter models with 1h caching.
    Falls back to ['openrouter/free'] on error, cached for 5 minutes to avoid 15s timeout per lot."""
    global _or_free_models_cache, _or_free_models_fetched_at

    if _or_free_models_cache and (time.time() - _or_free_models_fetched_at) < _OR_CACHE_TTL:
        return _or_free_models_cache

    try:
        key = or_manager.get_key()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        resp = std_requests.get("https://openrouter.ai/api/v1/models", headers=headers, timeout=15)
        if resp.status_code == 200:
            models = resp.json().get("data", [])
            free_ids = [
                m["id"] for m in models
                if m.get("pricing", {}).get("prompt") == "0"
                and m.get("pricing", {}).get("completion") == "0"
                and m.get("architecture", {}).get("output_modalities", ["text"]) == ["text"]
            ][:8]
            if free_ids:
                _or_free_models_cache = free_ids
                _or_free_models_fetched_at = time.time()
                logging.info(f"[OpenRouter] Загружено {len(free_ids)} бесплатных моделей: {free_ids}")
                return _or_free_models_cache
            else:
                logging.warning("[OpenRouter] Каталог вернул 0 бесплатных моделей, используем fallback.")
        else:
            logging.error(f"[OpenRouter] Не удалось получить список моделей: status={resp.status_code}, response={resp.text[:200]}")
    except Exception as e:
        logging.error(f"[OpenRouter] Ошибка получения списка моделей: {e}")

    # Cache the fallback list for 5 minutes to avoid 15s timeout hit on every lot
    _or_free_models_cache = ["openrouter/free"]
    _or_free_models_fetched_at = time.time() - (_OR_CACHE_TTL - _OR_FALLBACK_CACHE_TTL)
    return _or_free_models_cache


_or_free_vision_models_cache: list = []
_or_free_vision_models_fetched_at: float = 0.0


def get_free_vision_models() -> list:
    """Fetches the current list of free multimodal (vision) OpenRouter models with 1h caching."""
    global _or_free_vision_models_cache, _or_free_vision_models_fetched_at

    if _or_free_vision_models_cache and (time.time() - _or_free_vision_models_fetched_at) < _OR_CACHE_TTL:
        return _or_free_vision_models_cache

    try:
        key = or_manager.get_key()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        resp = std_requests.get("https://openrouter.ai/api/v1/models", headers=headers, timeout=15)
        if resp.status_code == 200:
            models = resp.json().get("data", [])
            free_ids = [
                m["id"] for m in models
                if m.get("pricing", {}).get("prompt") == "0"
                and m.get("pricing", {}).get("completion") == "0"
                and "image" in m.get("architecture", {}).get("input_modalities", [])
                and m.get("architecture", {}).get("output_modalities", ["text"]) == ["text"]
            ][:4]
            if free_ids:
                _or_free_vision_models_cache = free_ids
                _or_free_vision_models_fetched_at = time.time()
                logging.info(f"[OpenRouter Vision] Загружено {len(free_ids)} бесплатных vision моделей: {free_ids}")
                return _or_free_vision_models_cache
            else:
                logging.warning("[OpenRouter Vision] Каталог вернул 0 бесплатных vision моделей.")
        else:
            logging.error(f"[OpenRouter Vision] Не удалось получить список моделей: status={resp.status_code}, response={resp.text[:200]}")
    except Exception as e:
        logging.error(f"[OpenRouter Vision] Ошибка получения списка моделей: {e}")

    _or_free_vision_models_cache = []
    _or_free_vision_models_fetched_at = time.time() - (_OR_CACHE_TTL - _OR_FALLBACK_CACHE_TTL)
    return _or_free_vision_models_cache


def ask_openrouter(prompt, max_tokens=1000):
    """Secondary provider: Auto-routing across live free OpenRouter models."""
    if not or_manager.keys:
        return None

    url = "https://openrouter.ai/api/v1/chat/completions"

    for model in get_free_models():
        key = or_manager.get_key()
        if not key:
            logging.warning("Все ключи OpenRouter в кулдауне.")
            return None

        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": max_tokens
        }

        try:
            resp = std_requests.post(url, json=payload, headers=headers, timeout=25)
            if resp.status_code == 200:
                raw_content = resp.json()["choices"][0]["message"]["content"]
                if not raw_content or not raw_content.strip():
                    logging.error(
                        f"[Auto-Free] {model}: 200 OK но контент пустой, "
                        f"response={resp.text[:200]}"
                    )
                    continue
                content = raw_content.strip()
                logging.info(f"OpenRouter ({model}) success: {content[:150]}")
                return content
            elif resp.status_code == 429:
                or_manager.mark_429(key, cooldown_min=5)
                logging.error(f"[Auto-Free] {model}: 429 rate limit, response={resp.text[:200]}")
                continue
            else:
                logging.error(f"[Auto-Free] {model}: status={resp.status_code}, response={resp.text[:200]}")
                continue
        except Exception as e:
            logging.warning(f"[Auto-Free] Модель {model} упала с ошибкой сети: {e}, пробуем следующую...")
            continue

    return None


def parse_ai_json(raw_text):
    """Safely extracts and parses JSON payload from LLM markdown/text output."""
    if not raw_text:
        return {"is_deal": False, "verdict": "ИИ вернул пустой ответ"}

    clean_text = raw_text.strip()
    if clean_text.startswith("```json"):
        clean_text = clean_text[7:]
    elif clean_text.startswith("```"):
        clean_text = clean_text[3:]
    if clean_text.endswith("```"):
        clean_text = clean_text[:-3]
    clean_text = clean_text.strip()

    data = None
    try:
        data = json.loads(clean_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", clean_text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError as e:
                logging.error(f"JSON Parse error (greedy): {e}. Raw text: {raw_text}")

    if data:
        used_price = data.get("market_used_price")
        if not used_price or used_price == 0:
            alt_price = data.get("market_price") or data.get("estimated_price") or 0
            data["market_used_price"] = alt_price
        return data

    logging.warning(f"Не удалось распарсить ответ ИИ: {raw_text}")
    return {"is_deal": False, "verdict": "Ошибка парсинга ответа ИИ"}


def evaluate_lot(title, price, photo_url=None):
    """Orchestrates 3-tier AI evaluation: Groq -> OpenRouter Auto-Free -> Gemini."""
    cat_rules = detect_category(title)
    min_profit = cat_rules["min_profit"]
    min_discount_pct = cat_rules["min_discount_pct"]

    prompt = f"""Ты профессиональный оценщик компьютерного железа на вторичном рынке РФ (Авито). 
Твоя база знаний охватывает цены за 2024-2026 год.
Оцени товар ИСКЛЮЧИТЕЛЬНО по своим знаниям рынка (без внешнего поиска).

Товар с Авито: '{title}'
Цена продавца: {price} руб.

ЗАДАЧИ:
1. Оцени ликвидность: "Высокая", "Средняя", "Низкая".
2. Оцени цену нового товара в ДНС/рознице и среднюю Б/У цену (market_used_price). Ты обязан дать реалистичные числа, никаких null.
3. Рассчитай profit_rub = market_used_price - price.
4. Определи is_deal (true/false) по правилам (порог профита {min_profit} руб, мин. скидка {min_discount_pct}%).

Respond ONLY with a valid JSON object. Do not include any explanations, markdown formatting, or introductory text.
{{
  "dns_new_price": <число или 0>,
  "market_used_price": <число>,
  "profit_rub": <число>,
  "profit_percent": <число>,
  "liquidity": "<Высокая/Средняя/Низкая>",
  "photo_verdict": "Не оценивалось (OpenRouter)",
  "is_deal": <boolean>,
  "verdict": "<краткое пояснение, 1 предложение>",
  "risks": "<риски при проверке, 1 предложение>"
}}"""

    # 1. Primary: Groq
    raw_response = ask_groq(prompt, json_mode=True)
    ai_data = parse_ai_json(raw_response) if raw_response else {}

    # 2. Secondary fallback: OpenRouter Auto-Free
    if not ai_data.get("market_used_price"):
        logging.info("Groq не дал цену или в кулдауне — фоллбэк на OpenRouter...")
        raw_response = ask_openrouter(prompt)
        ai_data = parse_ai_json(raw_response) if raw_response else {}

    # 3. Tertiary fallback: Gemini
    if not ai_data.get("market_used_price"):
        logging.warning("OpenRouter не дал цену или упал — пробуем Gemini fallback")
        raw_response_gemini = ask_gemini(title, price, photo_url)
        if raw_response_gemini:
            ai_data = parse_ai_json(raw_response_gemini)

    # Final diagnostic: log if all tiers exhausted without a price
    if not ai_data.get("market_used_price"):
        logging.warning(
            f"⚠️ Все три уровня ИИ не вернули market_used_price. "
            f"Провайдеры: Groq (model={GROQ_MODEL}), OpenRouter (models={get_free_models()}), "
            f"Gemini (model={GEMINI_MODEL}). Лот будет отправлен на ручную проверку."
        )

    return ai_data


def parse_vision_json(raw_text):
    """Safely extracts and parses JSON payload from vision model output."""
    if not raw_text:
        return None

    clean_text = raw_text.strip()
    if clean_text.startswith("```json"):
        clean_text = clean_text[7:]
    elif clean_text.startswith("```"):
        clean_text = clean_text[3:]
    if clean_text.endswith("```"):
        clean_text = clean_text[:-3]
    clean_text = clean_text.strip()

    data = None
    try:
        data = json.loads(clean_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", clean_text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError as e:
                logging.error(f"Vision JSON parse error: {e}. Raw: {raw_text[:200]}")

    if isinstance(data, dict):
        return data
    return None


def ask_vision(title, price, photo_b64):
    """Evaluates item photo via cascade: Groq Vision -> OpenRouter Free Vision -> Gemini Vision."""
    if not photo_b64:
        return None

    prompt = f"""Ты эксперт по оценке б/у товаров на Авито.
Проанализируй приложенную фотографию к объявлению:
Товар: '{title}'
Цена продавца: {price} руб.

ПРАВИЛА ОЦЕНКИ:
1. Оценивай ТОЛЬКО то, что реально видно на фото. Не выдумывай детали.
2. Если фото слишком мелкое, смазанное или неразборчивое, пиши photo_type "unclear", а не фантазируй.
3. photo_type: строго одно из "real" (живое фото), "stock" (стоковое/каталожное из интернета), "screenshot" (скриншот экрана), "render" (3D-рендер), "unclear" (непонятно/смазано).
4. matches_title: соответствует ли изображение заявленному товару (true, false, или null если неясно).
5. visible_defects: массив строк с реально видимыми дефектами (царапины, трещины, сколы, вмятины, следы разборки). Если дефектов нет, верни [].
6. red_flags: массив строк с подозрительными деталями (водяные знаки других сайтов, фото экрана, несоответствие ревизии). Если нет, верни [].
7. condition_score: оценка визуального состояния от 1 до 5 (целое число).
8. summary: одно предложение на русском языке с кратким вердиктом по фото.

Ответ верни СТРОГО в формате JSON без markdown-оберток:
{{
  "photo_type": "real|stock|screenshot|render|unclear",
  "matches_title": true,
  "visible_defects": [],
  "red_flags": [],
  "condition_score": 5,
  "summary": "Краткое резюме по фото"
}}"""

    # 1. Primary: Groq Vision
    if groq_manager.keys:
        url = "https://api.groq.com/openai/v1/chat/completions"
        for _ in range(len(groq_manager.keys)):
            key = groq_manager.get_key()
            if not key:
                logging.warning("Все ключи Groq в кулдауне (Vision).")
                break

            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            payload = {
                "model": GROQ_VISION_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{photo_b64}"
                                }
                            }
                        ]
                    }
                ],
                "response_format": {"type": "json_object"},
                "max_completion_tokens": 1024,
                "temperature": 0.2
            }

            try:
                resp = std_requests.post(url, json=payload, headers=headers, timeout=25)
                if resp.status_code == 200:
                    raw_content = resp.json()["choices"][0]["message"]["content"]
                    parsed = parse_vision_json(raw_content)
                    if parsed:
                        logging.info(f"Groq Vision ({GROQ_VISION_MODEL}) success")
                        return parsed
                elif resp.status_code == 429:
                    groq_manager.mark_429(key, cooldown_min=5)
                else:
                    logging.error(
                        f"❌ Groq Vision failed: model={GROQ_VISION_MODEL}, status={resp.status_code}, "
                        f"response={resp.text[:300]}"
                    )
                    break
            except Exception as e:
                logging.error(f"❌ Groq Vision request exception: {e}", exc_info=True)
                break

    # 2. Secondary fallback: OpenRouter Free Vision
    if or_manager.keys:
        url = "https://openrouter.ai/api/v1/chat/completions"
        for model in get_free_vision_models():
            key = or_manager.get_key()
            if not key:
                logging.warning("Все ключи OpenRouter в кулдауне (Vision).")
                break

            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{photo_b64}"
                                }
                            }
                        ]
                    }
                ],
                "temperature": 0.2,
                "max_tokens": 1024
            }

            try:
                resp = std_requests.post(url, json=payload, headers=headers, timeout=25)
                if resp.status_code == 200:
                    raw_content = resp.json()["choices"][0]["message"]["content"]
                    if not raw_content or not raw_content.strip():
                        logging.error(
                            f"[Vision Auto-Free] {model}: 200 OK но контент пустой, "
                            f"response={resp.text[:200]}"
                        )
                        continue
                    parsed = parse_vision_json(raw_content)
                    if parsed:
                        logging.info(f"OpenRouter Vision ({model}) success")
                        return parsed
                    else:
                        logging.error(f"[Vision Auto-Free] {model}: не удалось распарсить JSON, response={resp.text[:200]}")
                        continue
                elif resp.status_code == 429:
                    or_manager.mark_429(key, cooldown_min=5)
                    logging.error(f"[Vision Auto-Free] {model}: 429 rate limit, response={resp.text[:200]}")
                    continue
                else:
                    logging.error(f"[Vision Auto-Free] {model}: status={resp.status_code}, response={resp.text[:200]}")
                    continue
            except Exception as e:
                logging.warning(f"[Vision Auto-Free] Модель {model} упала с ошибкой сети: {e}, пробуем следующую...")
                continue

    # 3. Tertiary fallback: Gemini Vision
    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}"
        parts = [
            {"text": prompt},
            {
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": photo_b64
                }
            }
        ]
        payload = {
            "contents": [{"parts": parts}]
        }

        try:
            resp = std_requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=25)
            if resp.status_code == 200:
                data = resp.json()
                raw_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                parsed = parse_vision_json(raw_text)
                if parsed:
                    logging.info(f"Gemini Vision ({GEMINI_MODEL}) success")
                    return parsed
            else:
                logging.error(f"❌ Ошибка вызова Gemini Vision, status: {resp.status_code}, response: {resp.text[:300]}")
        except Exception as e:
            logging.error(f"❌ Ошибка вызова Gemini Vision: {e}", exc_info=True)

    return None


def send_tg_msg(chat_id, text, reply_markup=None, disable_notification=False):
    """Sends an HTML formatted message to Telegram."""
    token = os.getenv("TG_BOT_TOKEN")
    if not token:
        return None
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": disable_notification
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = std_requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10)
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
        else:
            logging.error(
                f"Telegram sendMessage error: chat_id={chat_id}, "
                f"status={r.status_code}, response={r.text[:200]}"
            )
    except Exception as e:
        logging.error(f"Telegram sendMessage error: {e}")
    return None


def pin_tg_msg(chat_id, message_id):
    """Pins a Telegram message in the specified chat."""
    token = os.getenv("TG_BOT_TOKEN")
    if not token or not message_id:
        return
    try:
        std_requests.post(
            f"https://api.telegram.org/bot{token}/pinChatMessage",
            json={"chat_id": chat_id, "message_id": message_id, "disable_notification": True},
            timeout=10
        )
    except Exception as e:
        logging.error(f"Telegram pinChatMessage error: {e}")


def send_tg_alert(cb_id, text):
    """Responds to a Telegram callback query with a pop-up alert."""
    token = os.getenv("TG_BOT_TOKEN")
    if not token:
        return
    try:
        std_requests.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": text, "show_alert": True},
            timeout=10
        )
    except Exception as e:
        logging.error(f"Telegram answerCallbackQuery error: {e}")


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    """Telegram Webhook handler for button callbacks and admin commands."""
    data = request.get_json(force=True, silent=True)
    if not data:
        return "OK", 200

    if "callback_query" in data:
        try:
            cb = data["callback_query"]
            cb_id = cb["id"]
            message = cb.get("message")
            if not message:
                send_tg_alert(cb_id, "Сообщение устарело, повторите команду.")
                return "OK", 200

            chat_id = message.get("chat", {}).get("id")
            cb_data = cb.get("data", "")

            if chat_id not in ADMIN_IDS:
                return "OK", 200

            if cb_data.startswith("ban:"):
                parts = cb_data.split(":", 1)
                s_id = parts[1] if len(parts) > 1 else None
                if s_id and s_id != "None":
                    ban_seller(s_id)
                    send_tg_alert(cb_id, "Продавец заблокирован и больше не появится в ленте")

                    markup = message.get("reply_markup", {})
                    new_keyboard = []
                    for row in markup.get("inline_keyboard", []):
                        new_row = []
                        for btn in row:
                            if btn.get("callback_data") == cb_data:
                                new_row.append({"text": "✅ Продавец в ЧС", "callback_data": "dummy"})
                            else:
                                new_row.append(btn)
                        new_keyboard.append(new_row)

                    try:
                        std_requests.post(
                            f"https://api.telegram.org/bot{os.getenv('TG_BOT_TOKEN')}/editMessageReplyMarkup",
                            json={
                                "chat_id": chat_id,
                                "message_id": message.get("message_id"),
                                "reply_markup": {"inline_keyboard": new_keyboard}
                            },
                            timeout=10
                        )
                    except Exception as e:
                        logging.error(f"Telegram editMessageReplyMarkup error: {e}")
                else:
                    send_tg_alert(cb_id, "ID продавца неизвестен!")

            elif cb_data.startswith("checklist:"):
                msg_text = message.get("text", "")
                raw_title = msg_text.splitlines()[0] if msg_text else "Товар"
                safe_title = html.escape(raw_title, quote=False)

                send_tg_alert(cb_id, "Генерирую чек-лист, подождите...")

                def generate_checklist(t, t_safe, cid):
                    prompt = (
                        f"Назови краткий чек-лист (4-5 конкретных шагов) для проверки перед покупкой товара: {t}. "
                        "Укажи нужные утилиты (FurMark, AIDA64, CrystalDiskInfo, MemTest и т.д.), допустимые температуры "
                        "и на какие дефекты смотреть на месте."
                    )
                    try:
                        raw_text = ask_groq(prompt, json_mode=False)
                        if not raw_text:
                            raw_text = ask_openrouter(prompt, max_tokens=1000)

                        if raw_text:
                            safe_text = html.escape(raw_text, quote=False)
                            send_tg_msg(cid, f"📋 <b>Чек-лист: {t_safe}</b>\n\n{safe_text}")
                        else:
                            send_tg_msg(cid, "❌ Ошибка генерации чек-листа (Все ИИ недоступны)")
                    except Exception as e:
                        logging.error(f"Checklist generation error: {e}")
                        send_tg_msg(cid, "❌ Сетевая ошибка при генерации чек-листа")

                threading.Thread(target=generate_checklist, args=(raw_title, safe_title, chat_id), daemon=True).start()

            elif cb_data == "menu:stats":
                st = "⏸ На паузе" if CONFIG["is_paused"] else "▶️ Активен"
                if LAST_HEARTBEAT_TIME == 0:
                    p_st = "🔴 Не в сети"
                else:
                    p_st = "🟢 Онлайн" if (time.time() - LAST_HEARTBEAT_TIME) < 180 else "🔴 Не в сети"
                send_tg_alert(
                    cb_id,
                    f"Скан: {PHONE_STATS.get('total_scanned', 0)}\n"
                    f"Отсеяно: {PHONE_STATS.get('filtered_price', 0)} по цене\n"
                    f"На реле: {PHONE_STATS.get('sent_to_server', 0)}\n"
                    f"В базе (моб): {TOTAL_SEEN_COUNT}\n"
                    f"Реле: {st}\n"
                    f"Парсер: {p_st}"
                )

            elif cb_data == "menu:toggle_pause":
                CONFIG["is_paused"] = not CONFIG["is_paused"]
                st = "Пауза" if CONFIG["is_paused"] else "Активен"
                send_tg_alert(cb_id, f"Статус изменен: {st}")

            elif cb_data == "menu:blacklist":
                cnt = get_blacklist_count()
                send_tg_alert(cb_id, f"В черном списке: {cnt} продавцов.")

            elif cb_data == "check_status":
                if LAST_HEARTBEAT_TIME == 0:
                    parser_status = "🔴 <b>Внимание: Termux оффлайн!</b>\nНи одного сигнала еще не получено."
                else:
                    secs_ago = int(time.time() - LAST_HEARTBEAT_TIME)
                    if secs_ago < 180:
                        parser_status = f"🟢 <b>Termux активен и на связи!</b>\nПоследний сигнал: {secs_ago} сек. назад"
                    else:
                        parser_status = f"🔴 <b>Внимание: Termux оффлайн!</b>\nСигнала нет уже более 3 минут (прошло {secs_ago} сек)."

                status_msg = (
                    f"{parser_status}\n\n"
                    f"📊 <b>Собрано парсером:</b> {PHONE_STATS.get('total_scanned', 0)}"
                )
                try:
                    std_requests.post(
                        f"https://api.telegram.org/bot{os.getenv('TG_BOT_TOKEN')}/editMessageText",
                        json={
                            "chat_id": chat_id,
                            "message_id": message.get("message_id"),
                            "text": status_msg,
                            "parse_mode": "HTML",
                            "reply_markup": {"inline_keyboard": [[{"text": "🔄 Проверить статус", "callback_data": "check_status"}]]}
                        },
                        timeout=10
                    )
                except Exception as e:
                    logging.error(f"Telegram editMessageText error: {e}")
                send_tg_alert(cb_id, "Статус обновлен")

            elif cb_data == "parser_status":
                last_ping = SYSTEM_STATUS.get("last_ping", 0)
                if last_ping == 0:
                    status_text = "🔴 <b>Ни одного сигнала еще не получено.</b>"
                else:
                    mins_ago = int((time.time() - last_ping) / 60)
                    status_text = f"⏱ Последний пинг: {mins_ago} минут назад"

                battery = SYSTEM_STATUS.get("battery", "?")
                charging = SYSTEM_STATUS.get("charging_status", "UNKNOWN")

                msg_text = (
                    "📱 <b>Статус устройства Termux:</b>\n"
                    f"{status_text}\n"
                    f"🔋 Заряд батареи: {battery}% ({charging})"
                )

                try:
                    std_requests.post(
                        f"https://api.telegram.org/bot{os.getenv('TG_BOT_TOKEN')}/editMessageText",
                        json={
                            "chat_id": chat_id,
                            "message_id": message.get("message_id"),
                            "text": msg_text,
                            "parse_mode": "HTML",
                            "reply_markup": {"inline_keyboard": [[{"text": "🔄 Обновить статус", "callback_data": "parser_status"}]]}
                        },
                        timeout=10
                    )
                except Exception as e:
                    logging.error(f"Telegram editMessageText error: {e}")
                send_tg_alert(cb_id, "Статус парсера обновлен")

        except Exception as e:
            logging.error(f"Ошибка обработки callback_query: {e}", exc_info=True)

        return "OK", 200

    if "message" in data:
        msg = data["message"]
        chat_id = msg.get("chat", {}).get("id")
        text = msg.get("text", "").strip()

        if chat_id not in ADMIN_IDS:
            return "OK", 200

        if text in ["/start", "/menu"]:
            inline_kb = {"inline_keyboard": [[{"text": "🔋 Статус парсера", "callback_data": "parser_status"}]]}
            send_tg_msg(chat_id, "Привет! Вот панель управления мониторингом Авито:", reply_markup=MAIN_KEYBOARD)
            send_tg_msg(chat_id, "Дополнительные действия:", reply_markup=inline_kb)

        elif text in ["/status", "📱 Статус Termux"]:
            if LAST_HEARTBEAT_TIME == 0:
                parser_status = "🔴 <b>Внимание: Termux оффлайн!</b>\nНи одного сигнала еще не получено."
            else:
                secs_ago = int(time.time() - LAST_HEARTBEAT_TIME)
                if secs_ago < 180:
                    parser_status = f"🟢 <b>Termux активен и на связи!</b>\nПоследний сигнал: {secs_ago} сек. назад"
                else:
                    parser_status = f"🔴 <b>Внимание: Termux оффлайн!</b>\nСигнала нет уже более 3 минут (прошло {secs_ago} сек). Проверьте запуск main.py на телефоне."

            status_msg = (
                f"{parser_status}\n\n"
                f"📊 <b>Собрано парсером:</b> {PHONE_STATS.get('total_scanned', 0)}"
            )
            markup = {"inline_keyboard": [[{"text": "🔄 Проверить статус", "callback_data": "check_status"}]]}
            send_tg_msg(chat_id, status_msg, reply_markup=markup)

        elif text in ["/stats", "📊 Статистика"]:
            status_text = "⏸ На паузе" if CONFIG["is_paused"] else "▶️ Активен"

            if LAST_HEARTBEAT_TIME == 0:
                parser_status = "🔴 Не в сети (еще не подключался)"
            else:
                mins_ago = int((time.time() - LAST_HEARTBEAT_TIME) / 60)
                if mins_ago < 3:
                    parser_status = f"🟢 Онлайн (был {mins_ago} мин назад)"
                else:
                    parser_status = "🔴 Не в сети"

            resp_text = (
                f"📊 <b>Статистика мониторинга (Агрегировано):</b>\n"
                f"• Всего просканировано: {PHONE_STATS.get('total_scanned', 0)}\n"
                f"• Отсеяно по цене: {PHONE_STATS.get('filtered_price', 0)}\n"
                f"• Отсеяно стоп-словами: {PHONE_STATS.get('filtered_stopwords', 0)}\n"
                f"• Проверено через ИИ: {PHONE_STATS.get('sent_to_server', 0)}\n"
                f"• В локальной базе телефона: {TOTAL_SEEN_COUNT} лотов.\n"
                f"• Текущий порог профита: от {CONFIG['min_profit_rub']} ₽\n"
                f"• В черном списке: {get_blacklist_count()}\n"
                f"• Блокировок за сутки: {SYSTEM_STATUS.get('blocks_today', 0)}\n"
                f"• Режим парсера: {SYSTEM_STATUS.get('mode', 'normal')}\n"
                f"📡 <b>Парсер:</b> {parser_status}\n"
                f"• Статус реле: {status_text}"
            )
            send_tg_msg(chat_id, resp_text)

        elif text in ["/test", "🧪 Тестовый алерт"]:
            test_title = "iPhone 13 Pro Max 256GB"
            test_msg = (
                f"🔥 <b>{test_title}</b>\n\n"
                f"💰 <b>Цена продавца:</b> 55 000 ₽\n"
                f"🏪 <b>Новый в ДНС / рознице:</b> ~90 000 ₽\n"
                f"📊 <b>Рынок Б/У:</b> ~65 000 ₽\n"
                f"📈 <b>Потенциальный профит:</b> +10 000 ₽ (18%)\n\n"
                f"⚡ <b>Ликвидность:</b> Высокая (1-3 дня)\n"
                f"📸 <b>Фото:</b> Реальное домашнее фото, мелкие царапины на корпусе\n\n"
                f"👤 <b>Продавец:</b> Иван | ⭐ 4.8 (12 отз.)\n\n"
                f"🧠 <b>Оценка:</b> Выгодная сделка, хорошая маржа.\n"
                f"⚠️ <b>Что проверить:</b> Проверить экран на выгорание, FaceID."
            )
            markup = {"inline_keyboard": [
                [
                    {"text": "🔗 Открыть на Авито", "url": "https://www.avito.ru/"},
                    {"text": "🔍 Проверить в DNS", "url": f"https://www.dns-shop.ru/search/?q={urllib.parse.quote(test_title)}"}
                ]
            ]}
            send_tg_msg(chat_id, test_msg, reply_markup=markup)

        elif text.startswith("/profit "):
            try:
                val = int(text.split()[1])
                CONFIG["min_profit_rub"] = val
                send_tg_msg(chat_id, f"✅ Порог профита успешно изменен на {val} ₽")
            except (IndexError, ValueError):
                send_tg_msg(chat_id, "❌ Неверный формат. Используйте: /profit 2500")

        elif text == "/pause":
            CONFIG["is_paused"] = True
            send_tg_msg(chat_id, "⏸ Мониторинг поставлен на паузу.")

        elif text == "/resume":
            CONFIG["is_paused"] = False
            send_tg_msg(chat_id, "▶️ Мониторинг возобновлен!")

        elif text == "/setwebhook":
            threading.Thread(target=set_webhook).start()
            send_tg_msg(chat_id, "✅ Запущена фоновая установка вебхука.")

    return "OK", 200


@app.route("/", methods=["GET", "POST", "HEAD"])
def index():
    """Healthcheck endpoint for Render and fallback handler."""
    if request.method in ["GET", "HEAD"]:
        return "OK", 200
    return send_alert()


@app.route("/setwebhook", methods=["GET"])
def manual_set_webhook():
    """Manual trigger to register Telegram webhook."""
    threading.Thread(target=set_webhook).start()
    return jsonify({"status": "webhook_setup_initiated"}), 200


@app.route("/ping", methods=["POST"])
def ping():
    """Heartbeat endpoint from Termux client."""
    global LAST_HEARTBEAT_TIME, PARSER_OFFLINE_ALERT_SENT, PHONE_STATS, TOTAL_SEEN_COUNT, SYSTEM_STATUS
    LAST_HEARTBEAT_TIME = time.time()

    data = request.get_json(force=True, silent=True)
    if data:
        PHONE_STATS = data.get("stats", PHONE_STATS)
        TOTAL_SEEN_COUNT = data.get("db_lots_count", TOTAL_SEEN_COUNT)
        if "battery" in data:
            SYSTEM_STATUS["battery"] = data["battery"]
        if "charging_status" in data:
            SYSTEM_STATUS["charging_status"] = data["charging_status"]
        if "blocks_today" in data:
            SYSTEM_STATUS["blocks_today"] = data["blocks_today"]
        if "mode" in data:
            SYSTEM_STATUS["mode"] = data["mode"]

    SYSTEM_STATUS["last_ping"] = LAST_HEARTBEAT_TIME

    if PARSER_OFFLINE_ALERT_SENT:
        for admin_id in ADMIN_IDS:
            send_tg_msg(admin_id, "✅ <b>Связь с Termux восстановлена!</b> Парсер снова в сети и сканирует лоты.")
        PARSER_OFFLINE_ALERT_SENT = False

    return jsonify({"status": "pong"}), 200


@app.route("/battery-alert", methods=["POST"])
def battery_alert():
    """Emergency endpoint when Termux battery is low."""
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "No JSON payload"}), 400

    level = data.get("battery_level", "?")
    msg = f"🔋⚠️ <b>Внимание! Батарея телефона садится: {level}%</b>\n\nПарсер может скоро отключиться, подключите зарядку!"

    for admin_id in ADMIN_IDS:
        send_tg_msg(admin_id, msg)

    logging.warning(f"Battery alert sent: {level}%")
    return jsonify({"status": "ok"}), 200


@app.route("/send", methods=["POST"])
def send_alert():
    """Primary webhook: receives parsed lots from Termux, applies AI audit, and alerts Telegram."""
    global LAST_HEARTBEAT_TIME, PARSER_OFFLINE_ALERT_SENT
    LAST_HEARTBEAT_TIME = time.time()

    if PARSER_OFFLINE_ALERT_SENT:
        for admin_id in ADMIN_IDS:
            send_tg_msg(admin_id, "✅ <b>Связь с Termux восстановлена!</b> Парсер снова в сети и сканирует лоты.")
        PARSER_OFFLINE_ALERT_SENT = False

    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "No JSON payload provided"}), 400

    chat_id = data.get("chat_id")
    title = data.get("title") or "Без названия"
    price = data.get("price")
    url_ad = data.get("url")
    photo_url = data.get("photo_url")
    text = data.get("text")
    seller_id = data.get("seller_id")
    seller = data.get("seller") if isinstance(data.get("seller"), dict) else {"name": "Не указан", "rating": "—", "reviews": 0}
    is_price_drop = bool(data.get("is_price_drop", False))
    old_price = data.get("old_price")
    ad_id = data.get("id") or url_ad

    token = os.getenv("TG_BOT_TOKEN")
    if not token:
        return jsonify({"error": "TG_BOT_TOKEN is missing"}), 500

    # Handle system notifications
    if not price or not url_ad:
        if not CONFIG["is_paused"]:
            final_text = html.escape(text, quote=False) if text else "Системное уведомление"
            send_tg_msg(chat_id, final_text)
        return jsonify({"status": "ok"}), 200

    if CONFIG["is_paused"]:
        return jsonify({"status": "paused"}), 200

    STATS["scanned"] += 1

    # In-memory deduplication keyed by (lot, chat) so each admin gets every lot
    dedup_key = (ad_id, chat_id) if ad_id else None
    if not is_price_drop:
        if dedup_key and dedup_key in seen_ads:
            return jsonify({"status": "duplicate"}), 200

    if dedup_key:
        seen_ads.append(dedup_key)

    # Blacklist check
    if is_seller_banned(seller_id):
        return jsonify({"status": "skipped", "reason": "blacklisted_seller"}), 200

    # AI Evaluation (result cached per lot to avoid duplicate API calls for multiple admins)
    ai_data = evaluate_lot_cached(ad_id, title, price, photo_url) or {}
    ai_failed = not ai_data.get("market_used_price")

    # Profitability filtering: If AI failed, do NOT discard (forward to manual check)
    if not ai_failed and not ai_data.get("is_deal", False):
        STATS["filtered"] += 1
        logging.info(f"[ОТСЕВ] {title} ({price} ₽) | Б/У: {ai_data.get('market_used_price')} ₽ | Причина: {ai_data.get('verdict')}")
        return jsonify({"status": "skipped", "reason": "not_profitable"}), 200

    STATS["approved"] += 1

    # Vision Analysis (evaluated only for approved lots or manual check, if photo_url exists)
    vision_data = None
    if photo_url:
        try:
            now = time.time()
            vision_cache_key = f"vision:{ad_id}"
            cached_vision = AI_CACHE.get(vision_cache_key) or (AI_CACHE.get(ad_id, {}).get("vision_data") if isinstance(AI_CACHE.get(ad_id), dict) else None)
            if cached_vision and (now - cached_vision.get("ts", 0)) < AI_CACHE_TTL:
                vision_data = cached_vision.get("data")
                logging.info(f"[Vision Cache HIT] {ad_id}")
            else:
                photo_b64 = download_and_encode_image(photo_url)
                if photo_b64:
                    vision_data = ask_vision(title, price, photo_b64)
                    if vision_data:
                        if len(AI_CACHE) > 500:
                            stale_keys = [k for k, v in AI_CACHE.items() if isinstance(v, dict) and (now - v.get("ts", 0)) >= AI_CACHE_TTL]
                            for k in stale_keys:
                                del AI_CACHE[k]
                        AI_CACHE[vision_cache_key] = {"data": vision_data, "ts": now}
                        if ad_id:
                            AI_CACHE[ad_id] = {"vision_data": {"data": vision_data, "ts": now}, "ts": now}
        except Exception as e:
            logging.error(f"Ошибка при анализе фото лота (vision): {e}", exc_info=True)
            vision_data = None

    stock_warning = ""
    if vision_data:
        photo_type = str(vision_data.get("photo_type", "")).strip().lower()
        if photo_type in ["stock", "screenshot"]:
            stock_warning = "🚩 Фото не похоже на реальное (стоковое или скриншот)\n"

        summary = html.escape(str(vision_data.get("summary", "")).strip(), quote=False)
        raw_defects = vision_data.get("visible_defects", [])
        defects_list = []
        if isinstance(raw_defects, list):
            defects_list = [html.escape(str(d).strip(), quote=False) for d in raw_defects if str(d).strip()]

        if defects_list:
            defects_text = f"Дефекты: {', '.join(defects_list)}"
            photo_verdict = f"{summary} ({defects_text})" if summary else defects_text
        else:
            photo_verdict = summary or "Реальное фото, без видимых дефектов"
    elif photo_url:
        photo_verdict = "не удалось оценить"
    else:
        photo_verdict = "Нет фото"

    safe_title = html.escape(title, quote=False)

    try:
        p = float(price)
        price_str = f"{p:,.0f} ₽".replace(",", " ")
    except (ValueError, TypeError):
        price_str = f"{price} ₽"

    # Anti-scam seller badges
    raw_reviews = seller.get("reviews", 0)
    try:
        seller_reviews_count = int(raw_reviews)
    except (ValueError, TypeError):
        seller_reviews_count = 0

    raw_rating = seller.get("rating")
    try:
        seller_rating = (
            float(str(raw_rating).strip().replace(",", "."))
            if raw_rating is not None and str(raw_rating).strip() not in ["—", "-", ""]
            else 0.0
        )
    except (ValueError, TypeError, AttributeError):
        seller_rating = 0.0

    if seller_reviews_count == 0:
        seller_block = "🚩 <b>Внимание:</b> Профиль без отзывов (высокий риск скама/предоплаты)"
    elif seller_reviews_count >= 15 and seller_rating >= 4.7:
        seller_block = f"✅ <b>Проверенный продавец:</b> {seller_rating}★ ({seller_reviews_count} отз.)"
    else:
        seller_block = f"👤 <b>Продавец:</b> {seller_reviews_count} отз."

    has_delivery = seller.get("has_delivery", False)
    if has_delivery:
        seller_block += " | 📦 Авито Доставка"

    is_super_deal = bool(is_price_drop)

    if ai_failed:
        final_text = (
            f"⚠️ <b>Требуется ручная проверка (ИИ не смог оценить цену)</b>\n"
            f"🔥 <b>{safe_title}</b>\n\n"
            f"💰 <b>Цена продавца:</b> {price_str}\n\n"
            f"{stock_warning}"
            f"📸 <b>Фото:</b> {photo_verdict}\n\n"
            f"{seller_block}\n\n"
            f"🧠 <b>Ошибка ИИ:</b> {html.escape(str(ai_data.get('verdict', 'Нет ответа от ИИ API')), quote=False)}"
        )
    else:
        dns_new_price = ai_data.get("dns_new_price")
        market_used_price = ai_data.get("market_used_price")
        profit_rub = ai_data.get("profit_rub", 0)
        profit_percent = ai_data.get("profit_percent", 0)
        liquidity = html.escape(str(ai_data.get("liquidity", "Неизвестно")), quote=False)
        verdict = html.escape(str(ai_data.get("verdict", "")), quote=False)
        risks = html.escape(str(ai_data.get("risks", "")), quote=False)

        is_super_deal = is_super_deal or (profit_rub >= 4000 or profit_percent >= 50)
        if is_super_deal:
            STATS["urgent"] += 1

        if is_price_drop:
            drop_pct = 0
            try:
                op = float(old_price) if old_price else 0.0
                np = float(price) if price else 0.0
                if op > 0:
                    drop_pct = int(((op - np) / op) * 100)
                old_str = f"{op:,.0f}".replace(",", " ")
            except (ValueError, TypeError):
                old_str = str(old_price)

            title_block = (
                f"📉 <b>СНИЖЕНИЕ ЦЕНЫ!</b> Было: <s>{old_str} ₽</s> ➔ Стало: <b>{price_str}</b> (-{drop_pct}%)\n"
                f"🚨🚨🚨 <b>МЕГА-СДЕЛКА / СРОЧНЫЙ ВЫКУП</b>\n🔥 <b>{safe_title}</b>"
            )
        elif is_super_deal:
            title_block = f"🚨🚨🚨 <b>МЕГА-СДЕЛКА / СРОЧНЫЙ ВЫКУП</b>\n🔥 <b>{safe_title}</b>"
        else:
            title_block = f"💡 <b>Выгодный лот</b>\n🔥 <b>{safe_title}</b>"

        dns_str = f"~{dns_new_price:,.0f} ₽" if isinstance(dns_new_price, (int, float)) else "Не найдено"
        market_str = f"~{market_used_price:,.0f} ₽" if isinstance(market_used_price, (int, float)) else "Не найдено"

        final_text = (
            f"{title_block}\n\n"
            f"💰 <b>Цена продавца:</b> {price_str}\n"
            f"🏪 <b>Новый в ДНС / рознице:</b> {dns_str.replace(',', ' ')}\n"
            f"📊 <b>Рынок Б/У:</b> {market_str.replace(',', ' ')}\n"
            f"📈 <b>Потенциальный профит:</b> +{profit_rub:,.0f} ₽ ({profit_percent}%)\n\n"
            f"⚡ <b>Ликвидность:</b> {liquidity}\n"
            f"{stock_warning}"
            f"📸 <b>Фото:</b> {photo_verdict}\n\n"
            f"{seller_block}\n\n"
            f"🧠 <b>Оценка:</b> {verdict}\n"
            f"⚠️ <b>Что проверить:</b> {risks}"
        )

    kb = [
        [
            {"text": "🔗 Открыть на Авито", "url": url_ad},
            {"text": "🔍 Найти в DNS / Цены", "url": f"https://www.dns-shop.ru/search/?q={urllib.parse.quote(title)}"}
        ],
        [
            {"text": "📋 Чек-лист проверки", "callback_data": f"checklist:{(ad_id or 'x')[:40]}"}
        ]
    ]
    if seller_id:
        kb.append([{"text": "🚫 В ЧС продавца", "callback_data": f"ban:{str(seller_id)[:40]}"}])

    reply_markup = {"inline_keyboard": kb}

    disable_notification = not is_super_deal
    msg_id = send_tg_msg(chat_id, final_text, reply_markup=reply_markup, disable_notification=disable_notification)

    if is_super_deal and msg_id:
        pin_tg_msg(chat_id, msg_id)

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
