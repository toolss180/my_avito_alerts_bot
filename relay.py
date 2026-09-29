import os
import re
import html
import json
import urllib.parse
import threading
import sqlite3
import base64
from collections import deque
from flask import Flask, request, jsonify
import requests as std_requests
import logging
import config

import time

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

# Глобальное состояние
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

seen_ads = deque(maxlen=1000)
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

def init_blacklist_db():
    conn = sqlite3.connect("bot_data.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS blacklist (seller_id TEXT PRIMARY KEY, banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()

init_blacklist_db()

def is_seller_banned(seller_id):
    if not seller_id: return False
    conn = sqlite3.connect("bot_data.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT 1 FROM blacklist WHERE seller_id = ?", (str(seller_id),))
    row = c.fetchone()
    conn.close()
    return bool(row)

def ban_seller(seller_id):
    if not seller_id: return
    conn = sqlite3.connect("bot_data.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO blacklist (seller_id) VALUES (?)", (str(seller_id),))
    conn.commit()
    conn.close()

def get_blacklist_count():
    conn = sqlite3.connect("bot_data.db", check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM blacklist")
    count = c.fetchone()[0]
    conn.close()
    return count

def detect_category(title):
    title_low = title.lower()
    for cat, data in config.CATEGORY_RULES.items():
        if any(kw in title_low for kw in data["keywords"]):
            return data
    return config.CATEGORY_RULES["components"]

def download_and_encode_image(url):
    if not url: return None
    try:
        r = std_requests.get(url, timeout=5)
        if r.status_code == 200:
            return base64.b64encode(r.content).decode('utf-8')
    except:
        pass
    return None

def watchdog_loop():
    global PARSER_OFFLINE_ALERT_SENT
    while True:
        time.sleep(60)
        if LAST_HEARTBEAT_TIME > 0:
            time_since_last = time.time() - LAST_HEARTBEAT_TIME
            if time_since_last > 1200 and not PARSER_OFFLINE_ALERT_SENT:
                for admin_id in ADMIN_IDS:
                    send_tg_msg(admin_id, "⚠️ <b>Внимание: Парсер на телефоне перестал отвечать!</b>\n\nПоследний сигнал был более 20 минут назад. Проверьте Termux на устройстве (возможно, система выгрузила процесс из памяти или пропал интернет).")
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
            
        # Установка команд меню
        commands = [
            {"command": "status", "description": "Проверить статус Termux"},
            {"command": "stats", "description": "Статистика мониторинга"},
            {"command": "test", "description": "Тестовая карточка лота"},
            {"command": "pause", "description": "Приостановить алерты"},
            {"command": "resume", "description": "Возобновить алерты"}
        ]
        try:
            std_requests.post(f"https://api.telegram.org/bot{token}/setMyCommands", json={"commands": commands}, timeout=10)
        except:
            pass

threading.Thread(target=set_webhook, daemon=True).start()

def ask_gemini(title, price, photo_url=None):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key: return None
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={api_key}"
    
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

def ask_openrouter(prompt, max_tokens=300):
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key: return None
        
    models = [
        "meta-llama/llama-3.1-8b-instruct:free",
        "google/gemma-2-9b-it:free",
        "qwen/qwen-2.5-72b-instruct:free"
    ]
    
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = "https://openrouter.ai/api/v1/chat/completions"
    
    for model in models:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": max_tokens
        }
        
        try:
            resp = std_requests.post(url, json=payload, headers=headers, timeout=25)
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"]["content"].strip()
                logging.info(f"OpenRouter ({model}) success: {content[:150]}")
                return content
            else:
                logging.warning(f"OpenRouter ({model}) failed, status: {resp.status_code}, response: {resp.text[:150]}")
        except Exception as e:
            logging.warning(f"OpenRouter ({model}) error: {e}", exc_info=True)
            
    return None

def parse_ai_json(raw_text):
    if not raw_text: return {"is_deal": False, "verdict": "ИИ вернул пустой ответ"}
    
    # Очистка markdown-оберток
    clean_text = raw_text.strip()
    if clean_text.startswith("```json"):
        clean_text = clean_text[7:]
    elif clean_text.startswith("```"):
        clean_text = clean_text[3:]
    if clean_text.endswith("```"):
        clean_text = clean_text[:-3]
    clean_text = clean_text.strip()
        
    data = None
    
    # 1. Сначала пробуем распарсить очищенный текст напрямую
    try:
        data = json.loads(clean_text)
    except json.JSONDecodeError:
        # 2. Жадный поиск от первой { до последней }
        match = re.search(r'\{.*\}', clean_text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError as e:
                logging.error(f"JSON Parse error (greedy): {e}. Raw text: {raw_text}")
                
    if data:
        # Маппинг альтернативных ключей цены, если market_used_price отсутствует/0
        used_price = data.get("market_used_price")
        if not used_price or used_price == 0:
            alt_price = data.get("market_price") or data.get("estimated_price") or 0
            data["market_used_price"] = alt_price
        return data
            
    # Если не удалось найти валидный JSON, отдаем безопасный дефолт
    logging.warning(f"Не удалось распарсить ответ ИИ: {raw_text}")
    return {"is_deal": False, "verdict": "Ошибка парсинга ответа ИИ"}

def evaluate_lot(title, price, photo_url=None):
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

    # Основная попытка через OpenRouter
    raw_response = ask_openrouter(prompt)
    ai_data = parse_ai_json(raw_response) if raw_response else {}
    
    # Если OpenRouter не вернул данные о ценах, фоллбэк на Gemini
    if not ai_data.get("market_used_price"):
        logging.warning("OpenRouter не дал цену или упал — пробуем Gemini fallback")
        raw_response_gemini = ask_gemini(title, price, photo_url)
        if raw_response_gemini:
            ai_data = parse_ai_json(raw_response_gemini)
            
    return ai_data

def send_tg_msg(chat_id, text, reply_markup=None, disable_notification=False):
    token = os.getenv("TG_BOT_TOKEN")
    if not token: return None
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": disable_notification
    }
    if reply_markup: payload["reply_markup"] = reply_markup
    try:
        r = std_requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10)
        if r.status_code == 200:
            return r.json().get("result", {}).get("message_id")
    except Exception as e:
        logging.error(f"Telegram sendMessage error: {e}")
    return None

def pin_tg_msg(chat_id, message_id):
    token = os.getenv("TG_BOT_TOKEN")
    if not token: return
    try:
        std_requests.post(f"https://api.telegram.org/bot{token}/pinChatMessage", json={
            "chat_id": chat_id,
            "message_id": message_id,
            "disable_notification": True
        }, timeout=10)
    except Exception as e:
        logging.error(f"Telegram pinChatMessage error: {e}")

def send_tg_alert(cb_id, text):
    token = os.getenv("TG_BOT_TOKEN")
    if not token: return
    try:
        std_requests.post(f"https://api.telegram.org/bot{token}/answerCallbackQuery", json={"callback_query_id": cb_id, "text": text, "show_alert": True}, timeout=10)
    except Exception as e:
        logging.error(f"Telegram answerCallbackQuery error: {e}")

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True, silent=True)
    if not data: return "OK", 200

    if "callback_query" in data:
        try:
            cb = data["callback_query"]
            cb_id = cb["id"]
            # Telegram может прислать callback без message (для очень старых сообщений)
            if "message" not in cb:
                send_tg_alert(cb_id, "Сообщение устарело, повторите команду.")
                return "OK", 200
            chat_id = cb["message"]["chat"]["id"]
            cb_data = cb.get("data", "")
            
            if chat_id not in ADMIN_IDS: return "OK", 200
        
            if cb_data.startswith("ban:"):
                s_id = cb_data.split(":")[1]
                if s_id and s_id != "None":
                    ban_seller(s_id)
                    send_tg_alert(cb_id, "Продавец заблокирован и больше не появится в ленте")
                    
                    # Обновляем кнопку
                    markup = cb["message"].get("reply_markup", {})
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
                        std_requests.post(f"https://api.telegram.org/bot{os.getenv('TG_BOT_TOKEN')}/editMessageReplyMarkup", json={
                            "chat_id": chat_id,
                            "message_id": cb["message"]["message_id"],
                            "reply_markup": {"inline_keyboard": new_keyboard}
                        }, timeout=10)
                    except Exception as e:
                        logging.error(f"Telegram editMessageReplyMarkup error: {e}")
                else:
                    send_tg_alert(cb_id, "ID продавца неизвестен!")

            elif cb_data.startswith("checklist:"):
                msg_text = cb["message"].get("text", "")
                # Извлекаем заголовок товара из первой строки сообщения
                raw_title = msg_text.splitlines()[0] if msg_text else "Товар"
                safe_title = html.escape(raw_title, quote=False)
                
                # BUG-4/BUG-6: answerCallbackQuery СРАЗУ, до любых долгих операций
                send_tg_alert(cb_id, "Генерирую чек-лист, подождите...")
                
                def generate_checklist(t, t_safe, cid):
                    prompt = f"Назови краткий чек-лист (4-5 конкретных шагов) для проверки перед покупкой товара: {t}. Укажи нужные утилиты (FurMark, AIDA64, CrystalDiskInfo, MemTest и т.д.), допустимые температуры и на какие дефекты смотреть на месте."
                    
                    try:
                        raw_text = ask_openrouter(prompt, max_tokens=500)
                        if raw_text:
                            safe_text = html.escape(raw_text, quote=False)
                            send_tg_msg(cid, f"📋 <b>Чек-лист: {t_safe}</b>\n\n{safe_text}")
                        else:
                            send_tg_msg(cid, "❌ Ошибка генерации чек-листа (OpenRouter недоступен)")
                    except Exception as e:
                        logging.error(f"Checklist generation error: {e}")
                        send_tg_msg(cid, "❌ Сетевая ошибка при генерации чек-листа")
                        
                threading.Thread(target=generate_checklist, args=(raw_title, safe_title, chat_id), daemon=True).start()

            elif cb_data == "menu:stats":
                st = "⏸ На паузе" if CONFIG['is_paused'] else "▶️ Активен"
                if LAST_HEARTBEAT_TIME == 0:
                    p_st = "🔴 Не в сети"
                else:
                    p_st = "🟢 Онлайн" if (time.time() - LAST_HEARTBEAT_TIME) < 180 else "🔴 Не в сети"
                send_tg_alert(cb_id, f"Скан: {PHONE_STATS.get('total_scanned', 0)}\nОтсеяно: {PHONE_STATS.get('filtered_price', 0)} по цене\nНа реле: {PHONE_STATS.get('sent_to_server', 0)}\nВ базе (моб): {TOTAL_SEEN_COUNT}\nРеле: {st}\nПарсер: {p_st}")
                
            elif cb_data == "menu:toggle_pause":
                CONFIG["is_paused"] = not CONFIG["is_paused"]
                st = "Пауза" if CONFIG['is_paused'] else "Активен"
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
                    std_requests.post(f"https://api.telegram.org/bot{os.getenv('TG_BOT_TOKEN')}/editMessageText", json={
                        "chat_id": chat_id,
                        "message_id": cb["message"]["message_id"],
                        "text": status_msg,
                        "parse_mode": "HTML",
                        "reply_markup": {"inline_keyboard": [[{"text": "🔄 Проверить статус", "callback_data": "check_status"}]]}
                    }, timeout=10)
                except Exception as e:
                    logging.error(f"Telegram editMessageText error: {e}")
                send_tg_alert(cb_id, "Статус обновлен")

        except Exception as e:
            logging.error(f"Ошибка обработки callback_query: {e}")
        
        return "OK", 200

    if "message" in data:
        msg = data["message"]
        chat_id = msg.get("chat", {}).get("id")
        text = msg.get("text", "").strip()
        
        if chat_id not in ADMIN_IDS: return "OK", 200
        
        if text in ["/start", "/menu"]:
            send_tg_msg(chat_id, "Привет! Вот панель управления мониторингом Авито:", reply_markup=MAIN_KEYBOARD)
            
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
            status_text = "⏸ На паузе" if CONFIG['is_paused'] else "▶️ Активен"
            
            # Статус парсера
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
                f"📡 <b>Парсер:</b> {parser_status}\n"
                f"• Статус реле: {status_text}"
            )
            send_tg_msg(chat_id, resp_text)
            
        elif text in ["/test", "🧪 Тестовый алерт"]:
            test_title = "iPhone 13 Pro Max 256GB"
            import urllib.parse
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
            except ValueError:
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
    if request.method in ["GET", "HEAD"]:
        return "OK", 200
    return send_alert()

@app.route("/setwebhook", methods=["GET"])
def manual_set_webhook():
    threading.Thread(target=set_webhook).start()
    return jsonify({"status": "webhook_setup_initiated"}), 200

@app.route("/ping", methods=["POST"])
def ping():
    global LAST_HEARTBEAT_TIME, PARSER_OFFLINE_ALERT_SENT, PHONE_STATS, TOTAL_SEEN_COUNT
    LAST_HEARTBEAT_TIME = time.time()
    
    data = request.get_json(force=True, silent=True)
    if data:
        PHONE_STATS = data.get("stats", PHONE_STATS)
        TOTAL_SEEN_COUNT = data.get("db_lots_count", TOTAL_SEEN_COUNT)
    
    if PARSER_OFFLINE_ALERT_SENT:
        for admin_id in ADMIN_IDS:
            send_tg_msg(admin_id, "✅ <b>Связь с Termux восстановлена!</b> Парсер снова в сети и сканирует лоты.")
        PARSER_OFFLINE_ALERT_SENT = False
        
    return jsonify({"status": "pong"}), 200

@app.route("/send", methods=["POST"])
def send_alert():
    global LAST_HEARTBEAT_TIME, PARSER_OFFLINE_ALERT_SENT
    LAST_HEARTBEAT_TIME = time.time()
    
    if PARSER_OFFLINE_ALERT_SENT:
        for admin_id in ADMIN_IDS:
            send_tg_msg(admin_id, "✅ <b>Связь с Termux восстановлена!</b> Парсер снова в сети и сканирует лоты.")
        PARSER_OFFLINE_ALERT_SENT = False

    data = request.get_json(force=True, silent=True)
    if not data: return jsonify({"error": "No JSON payload provided"}), 400

    chat_id = data.get("chat_id")
    title = data.get("title") or "Без названия"
    price = data.get("price")
    url_ad = data.get("url")
    photo_url = data.get("photo_url")
    text = data.get("text")
    seller_id = data.get("seller_id")
    seller = data.get("seller") or {"name": "Не указан", "rating": "—", "reviews": 0}
    is_price_drop = data.get("is_price_drop", False)
    old_price = data.get("old_price")
    ad_id = data.get("id") or url_ad
        
    token = os.getenv("TG_BOT_TOKEN")
    if not token: return jsonify({"error": "TG_BOT_TOKEN is missing"}), 500

    if not price or not url_ad:
        if not CONFIG["is_paused"]:
            final_text = html.escape(text, quote=False) if text else "Системное уведомление"
            send_tg_msg(chat_id, final_text)
        return jsonify({"status": "ok"}), 200

    if CONFIG["is_paused"]: return jsonify({"status": "paused"}), 200

    STATS["scanned"] += 1

    # Защита от дублей (In-Memory Cache), но пропускаем если это снижение цены
    if not is_price_drop:
        if ad_id and ad_id in seen_ads: return jsonify({"status": "duplicate"}), 200
    
    if ad_id: seen_ads.append(ad_id)

    # Проверка черного списка
    if is_seller_banned(seller_id):
        return jsonify({"status": "skipped", "reason": "blacklisted_seller"}), 200

    # ИИ Аудит
    ai_data = evaluate_lot(title, price, photo_url)
    
    if not ai_data:
        # Резервный контур: ИИ недоступен
        safe_title = html.escape(title, quote=False)
        final_text = (
            f"⚠️ <b>[ИИ недоступен: проверьте вручную]</b>\n\n"
            f"🔥 <b>{safe_title}</b>\n"
            f"💰 <b>Цена продавца:</b> {price} ₽\n"
        )
        reply_markup = {"inline_keyboard": [[{"text": "🔗 Открыть на Авито", "url": url_ad}]]}
        send_tg_msg(chat_id, final_text, reply_markup=reply_markup)
        return jsonify({"status": "ok"}), 200

    # Фильтрация по is_deal
    if not ai_data.get("is_deal", False):
        STATS["filtered"] += 1
        logging.info(f"[ОТСЕВ] {title} ({price} ₽) | Б/У: {ai_data.get('market_used_price')} ₽ | Причина: {ai_data.get('verdict')}")
        return jsonify({"status": "skipped", "reason": "not_profitable"}), 200

    STATS["approved"] += 1

    safe_title = html.escape(title, quote=False)
    dns_new_price = ai_data.get("dns_new_price")
    market_used_price = ai_data.get("market_used_price")
    profit_rub = ai_data.get("profit_rub", 0)
    profit_percent = ai_data.get("profit_percent", 0)
    liquidity = html.escape(str(ai_data.get("liquidity", "Неизвестно")), quote=False)
    photo_verdict = html.escape(str(ai_data.get("photo_verdict", "Нет фото")), quote=False)
    verdict = html.escape(str(ai_data.get("verdict", "")), quote=False)
    risks = html.escape(str(ai_data.get("risks", "")), quote=False)

    is_super_deal = profit_rub >= 4000 or profit_percent >= 50 or is_price_drop
    if is_super_deal: STATS["urgent"] += 1
        
    # Формирование шапки (price drop / urgent)
    if is_price_drop:
        drop_pct = 0
        if old_price and old_price > 0:
            drop_pct = int(((old_price - price) / old_price) * 100)
        
        try:
            op = float(old_price)
            old_str = f"{op:,.0f}".replace(',', ' ')
        except: old_str = old_price
        
        try:
            p = float(price)
            new_str = f"{p:,.0f}".replace(',', ' ')
        except: new_str = price
        
        title_block = f"📉 <b>СНИЖЕНИЕ ЦЕНЫ!</b> Было: <s>{old_str} ₽</s> ➔ Стало: <b>{new_str} ₽</b> (-{drop_pct}%)\n"
        title_block += f"🚨🚨🚨 <b>МЕГА-СДЕЛКА / СРОЧНЫЙ ВЫКУП</b>\n🔥 <b>{safe_title}</b>"
    elif is_super_deal:
        title_block = f"🚨🚨🚨 <b>МЕГА-СДЕЛКА / СРОЧНЫЙ ВЫКУП</b>\n🔥 <b>{safe_title}</b>"
    else:
        title_block = f"💡 <b>Выгодный лот</b>\n🔥 <b>{safe_title}</b>"

    dns_str = f"~{dns_new_price:,.0f} ₽" if isinstance(dns_new_price, (int, float)) else "Не найдено"
    market_str = f"~{market_used_price:,.0f} ₽" if isinstance(market_used_price, (int, float)) else "Не найдено"

    try:
        p = float(price)
        price_str = f"{p:,.0f} ₽".replace(',', ' ')
    except:
        price_str = f"{price} ₽"
        
    # Анти-скам бейджи продавца
    raw_reviews = seller.get('reviews', 0)
    try:
        seller_reviews_count = int(raw_reviews)
    except (ValueError, TypeError):
        seller_reviews_count = 0
        
    raw_rating = seller.get('rating')
    try:
        seller_rating = float(str(raw_rating).strip().replace(',', '.')) if raw_rating is not None and str(raw_rating).strip() not in ["—", "-", ""] else 0.0
    except (ValueError, TypeError, AttributeError):
        seller_rating = 0.0

    if seller_reviews_count == 0:
        seller_block = "🚩 <b>Внимание:</b> Профиль без отзывов (высокий риск скама/предоплаты)"
    elif seller_reviews_count >= 15 and seller_rating >= 4.7:
        seller_block = f"✅ <b>Проверенный продавец:</b> {seller_rating}★ ({seller_reviews_count} отз.)"
    else:
        seller_block = f"👤 <b>Продавец:</b> {seller_reviews_count} отз."
        
    has_delivery = seller.get('has_delivery', False)
    if has_delivery:
        seller_block += " | 📦 Авито Доставка"

    final_text = (
        f"{title_block}\n\n"
        f"💰 <b>Цена продавца:</b> {price_str}\n"
        f"🏪 <b>Новый в ДНС / рознице:</b> {dns_str.replace(',', ' ')}\n"
        f"📊 <b>Рынок Б/У:</b> {market_str.replace(',', ' ')}\n"
        f"📈 <b>Потенциальный профит:</b> +{profit_rub:,.0f} ₽ ({profit_percent}%)\n\n"
        f"⚡ <b>Ликвидность:</b> {liquidity}\n"
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
