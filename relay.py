import os
import re
import html
import json
import urllib.parse
import threading
from collections import deque
from flask import Flask, request, jsonify
from duckduckgo_search import DDGS
import requests as std_requests

app = Flask(__name__)

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

seen_ads = deque(maxlen=1000)
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

def set_webhook():
    token = os.getenv("TG_BOT_TOKEN")
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if token and render_url:
        webhook_url = f"{render_url.rstrip('/')}/telegram-webhook"
        url = f"https://api.telegram.org/bot{token}/setWebhook?url={webhook_url}"
        try:
            res = std_requests.get(url, timeout=10)
            print(f"Webhook set result: {res.text}")
        except Exception as e:
            print(f"Failed to set webhook: {e}")

# Запускаем привязку вебхука в фоне (при старте сервера)
threading.Thread(target=set_webhook, daemon=True).start()

def ask_gemini(title, price):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    
    prompt = f"""Ты — жесткий аналитик перепродажи электроники в РФ.
Товар с Авито: '{title}'
Заявленная цена продавца: {price} руб.

ТВОИ ЗАДАЧИ:
1. С помощью Google Search найди точную стоимость этого товара (или прямого современного аналога) в рознице РФ, в ПЕРВУЮ ОЧЕРЕДЬ в ДНС (dns-shop.ru), а также в Ситилинк / Яндекс Маркет.
2. Определи реальную медианную цену такого товара на вторичном рынке (Б/У в рабочем состоянии).
3. Рассчитай чистый профит: profit_rub = market_used_price - price.
4. Сделай вывод: сделка выгодна (is_deal: true) ТОЛЬКО при одновременном выполнении условий:
   - Цена продавца СТРОГО ниже цены нового товара в ДНС (минимум на 30%).
   - Цена продавца минимум на 25% ниже рынка Б/У (market_used_price).
   - profit_rub >= {CONFIG['min_profit_rub']} рублей.
   Если это оверпрайс, сломанный хлам или неликвид — is_deal: false.

Ответ верни СТРОГО в формате JSON без markdown-тегов и пояснений:
{{
  "dns_new_price": <число или null>,
  "market_used_price": <число>,
  "profit_rub": <число>,
  "profit_percent": <число>,
  "is_deal": <boolean>,
  "verdict": "<краткий вердикт, 1 предложение>",
  "risks": "<главные риски при проверке, 1 предложение>"
}}"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"googleSearch": {}}]
    }
    
    try:
        resp = std_requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=25)
        if resp.status_code == 200:
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"Gemini API error: {e}")
        
    return None

def ask_openrouter_ddg(title, price):
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None
        
    try:
        results = DDGS().text(f"{title} цена новый днс бу авито", max_results=3)
        snippets = "\n".join([r['body'] for r in results])
    except Exception as e:
        print(f"DDG error: {e}")
        snippets = "Поиск недоступен."
        
    prompt = f"""Ты — жесткий аналитик перепродажи электроники в РФ.
Товар с Авито: '{title}'
Заявленная цена продавца: {price} руб.

Найденная информация в интернете:
{snippets}

ТВОИ ЗАДАЧИ:
1. Оцени цену нового товара в ДНС/рознице и медианную Б/У цену.
2. Рассчитай чистый профит: profit_rub = market_used_price - price.
3. Сделка выгодна (is_deal: true) ТОЛЬКО если цена ниже новой в ДНС на 30%, ниже рынка Б/У на 25%, и профит >= {CONFIG['min_profit_rub']} руб.

Ответ верни СТРОГО в формате JSON без markdown-тегов и пояснений:
{{
  "dns_new_price": <число или null>,
  "market_used_price": <число>,
  "profit_rub": <число>,
  "profit_percent": <число>,
  "is_deal": <boolean>,
  "verdict": "<краткий вердикт>",
  "risks": "<риски>"
}}"""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": 300
    }
    
    url = "https://openrouter.ai/api/v1/chat/completions"
        
    try:
        resp = std_requests.post(url, json=payload, headers=headers, timeout=25)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"OpenRouter API error: {e}")
        
    return None

def parse_ai_json(raw_text):
    if not raw_text:
        return None
    match = re.search(r'\{.*\}', raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None

def evaluate_lot(title, price):
    raw_response = ask_gemini(title, price)
    ai_data = parse_ai_json(raw_response)
    
    if not ai_data:
        raw_response = ask_openrouter_ddg(title, price)
        ai_data = parse_ai_json(raw_response)
        
    return ai_data

def send_tg_msg(chat_id, text, reply_markup=None, disable_notification=False):
    token = os.getenv("TG_BOT_TOKEN")
    if not token:
        return
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": disable_notification
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
        
    std_requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)

@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True, silent=True)
    if not data or "message" not in data:
        return "OK", 200
        
    msg = data["message"]
    chat_id = msg.get("chat", {}).get("id")
    text = msg.get("text", "").strip()
    
    if chat_id not in ADMIN_IDS:
        return "OK", 200
        
    if text == "/stats":
        status_text = "⏸ На паузе" if CONFIG['is_paused'] else "▶️ Активен"
        resp_text = (
            f"📊 <b>Статистика мониторинга:</b>\n"
            f"• Всего лотов получено: {STATS['scanned']}\n"
            f"• Отсеяно ИИ (не выгодно): {STATS['filtered']}\n"
            f"• Одобрено к покупке: {STATS['approved']} (из них срочных: {STATS['urgent']})\n"
            f"• Текущий порог профита: от {CONFIG['min_profit_rub']} ₽\n"
            f"• Статус: {status_text}"
        )
        send_tg_msg(chat_id, resp_text)
        
    elif text.startswith("/profit "):
        try:
            val = int(text.split()[1])
            CONFIG["min_profit_rub"] = val
            send_tg_msg(chat_id, f"✅ Порог профита успешно изменен на {val} ₽")
        except ValueError:
            send_tg_msg(chat_id, "❌ Неверный формат. Используйте: /profit 2500")
            
    elif text == "/pause":
        CONFIG["is_paused"] = True
        send_tg_msg(chat_id, "⏸ Мониторинг поставлен на паузу. Уведомления отключены.")
        
    elif text == "/resume":
        CONFIG["is_paused"] = False
        send_tg_msg(chat_id, "▶️ Мониторинг возобновлен!")
        
    return "OK", 200


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "GET":
        return jsonify({"status": "running", "service": "avito-relay"}), 200
    return send_alert()

@app.route("/send", methods=["POST"])
def send_alert():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "No JSON payload provided"}), 400

    chat_id = data.get("chat_id")
    title = data.get("title")
    price = data.get("price")
    url_ad = data.get("url")
    text = data.get("text")
    seller = data.get("seller", {"name": "Не указан", "rating": "—", "reviews": 0})
    ad_id = data.get("id") or url_ad
        
    token = os.getenv("TG_BOT_TOKEN")
    if not token:
        return jsonify({"error": "TG_BOT_TOKEN is missing"}), 500

    # Если передан сервисный текст (нет цены или урла), просто транслируем (только если не на паузе)
    if not price or not url_ad:
        if not CONFIG["is_paused"]:
            final_text = html.escape(text, quote=False) if text else "Системное уведомление"
            send_tg_msg(chat_id, final_text)
        return jsonify({"status": "ok"}), 200

    if CONFIG["is_paused"]:
        return jsonify({"status": "paused"}), 200

    STATS["scanned"] += 1

    # Защита от дублей (In-Memory Cache)
    if ad_id and ad_id in seen_ads:
        return jsonify({"status": "duplicate"}), 200
    if ad_id:
        seen_ads.append(ad_id)

    # ИИ Аудит
    ai_data = evaluate_lot(title, price)
    
    if not ai_data:
        # Резервный контур: ИИ недоступен
        safe_title = html.escape(title, quote=False)
        final_text = (
            f"⚠️ <b>[ИИ недоступен: проверьте вручную]</b>\n\n"
            f"🔥 <b>{safe_title}</b>\n"
            f"💰 <b>Цена продавца:</b> {price} ₽\n"
        )
        reply_markup = {
            "inline_keyboard": [[
                {"text": "🔗 Открыть на Авито", "url": url_ad},
                {"text": "🔍 Поиск в DNS", "url": f"https://www.dns-shop.ru/search/?q={urllib.parse.quote(title)}"}
            ]]
        }
        send_tg_msg(chat_id, final_text, reply_markup=reply_markup)
        return jsonify({"status": "ok"}), 200

    # Фильтрация по is_deal
    if not ai_data.get("is_deal", False):
        STATS["filtered"] += 1
        dns_p = ai_data.get("dns_new_price")
        market_p = ai_data.get("market_used_price")
        prof = ai_data.get("profit_rub")
        verdict = ai_data.get("verdict", "No reason provided")
        print(f"[FILTERED] {title} ({price} ₽) | ДНС: {dns_p} ₽ | Б/У: {market_p} ₽ | Профит: {prof} ₽ | Причина: {verdict}")
        return jsonify({"status": "skipped", "reason": "not_profitable"}), 200

    STATS["approved"] += 1

    safe_title = html.escape(title, quote=False)
    dns_new_price = ai_data.get("dns_new_price")
    market_used_price = ai_data.get("market_used_price")
    profit_rub = ai_data.get("profit_rub", 0)
    profit_percent = ai_data.get("profit_percent", 0)
    verdict = html.escape(ai_data.get("verdict", ""), quote=False)
    risks = html.escape(ai_data.get("risks", ""), quote=False)

    # Определение срочности
    is_urgent = False
    if profit_rub >= 5000 or profit_percent >= 40:
        is_urgent = True
        STATS["urgent"] += 1
        
    title_block = f"🚨🔥 <b>СРОЧНЫЙ ВЫКУП! ОГРОМНЫЙ ПРОФИТ</b>\n🔥 <b>{safe_title}</b>" if is_urgent else f"🔥 <b>{safe_title}</b>"

    dns_str = f"~{dns_new_price:,.0f} ₽" if isinstance(dns_new_price, (int, float)) else "Не найдено"
    market_str = f"~{market_used_price:,.0f} ₽" if isinstance(market_used_price, (int, float)) else "Не найдено"

    try:
        p = float(price)
        price_str = f"{p:,.0f} ₽".replace(',', ' ')
    except:
        price_str = f"{price} ₽"
        
    # Блок продавца
    seller_name = html.escape(str(seller.get('name', 'Не указан')), quote=False)
    seller_rating = html.escape(str(seller.get('rating', '—')), quote=False)
    seller_reviews = seller.get('reviews', 0)
    
    seller_block = f"👤 <b>Продавец:</b> {seller_name} | ⭐ {seller_rating} ({seller_reviews} отз.)"
    if str(seller_reviews) == "0":
        seller_block += "\n🚨 <b>Внимание:</b> Профиль без отзывов! Оформляйте сделку строго через Авито Доставку с проверкой при получении."

    final_text = (
        f"{title_block}\n\n"
        f"💰 <b>Цена продавца:</b> {price_str}\n"
        f"🏪 <b>Новый в ДНС / рознице:</b> {dns_str.replace(',', ' ')}\n"
        f"📊 <b>Рынок Б/У:</b> {market_str.replace(',', ' ')}\n"
        f"📈 <b>Потенциальный профит:</b> +{profit_rub:,.0f} ₽ ({profit_percent}%)\n\n"
        f"{seller_block}\n\n"
        f"🧠 <b>Оценка:</b> {verdict}\n"
        f"⚠️ <b>Что проверить:</b> {risks}"
    )

    reply_markup = {
        "inline_keyboard": [[
            {"text": "🔗 Открыть на Авито", "url": url_ad},
            {"text": "🔍 Поиск в DNS", "url": f"https://www.dns-shop.ru/search/?q={urllib.parse.quote(title)}"}
        ]]
    }

    send_tg_msg(chat_id, final_text, reply_markup=reply_markup, disable_notification=(not is_urgent))

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
