import os
import re
import html
import json
import urllib.parse
from collections import deque
from flask import Flask, request, jsonify
from duckduckgo_search import DDGS
import requests as std_requests

app = Flask(__name__)

# In-Memory Cache для защиты от дублей
seen_ads = deque(maxlen=1000)

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
   - profit_rub >= 1500 рублей.
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
3. Сделка выгодна (is_deal: true) ТОЛЬКО если цена ниже новой в ДНС на 30%, ниже рынка Б/У на 25%, и профит >= 1500 руб.

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
    ad_id = data.get("id") 
    
    # Фолбэк: если клиент не прислал ID, берем url
    if not ad_id and url_ad:
        ad_id = url_ad
        
    token = os.getenv("TG_BOT_TOKEN")
    if not token:
        return jsonify({"error": "TG_BOT_TOKEN is missing"}), 500

    # Если передан сервисный текст (нет цены или урла), просто транслируем
    if not price or not url_ad:
        final_text = html.escape(text, quote=False) if text else "Системное уведомление"
        std_requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": final_text, "parse_mode": "HTML"}
        )
        return jsonify({"status": "ok"}), 200

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
        std_requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id, 
                "text": final_text, 
                "parse_mode": "HTML",
                "reply_markup": reply_markup
            }
        )
        return jsonify({"status": "ok"}), 200

    # Фильтрация по is_deal
    if not ai_data.get("is_deal", False):
        dns_p = ai_data.get("dns_new_price")
        market_p = ai_data.get("market_used_price")
        prof = ai_data.get("profit_rub")
        verdict = ai_data.get("verdict", "No reason provided")
        print(f"[FILTERED] {title} ({price} ₽) | ДНС: {dns_p} ₽ | Б/У: {market_p} ₽ | Профит: {prof} ₽ | Причина: {verdict}")
        return jsonify({"status": "skipped", "reason": "not_profitable"}), 200

    # Если is_deal == True, отправляем красиво
    safe_title = html.escape(title, quote=False)
    dns_new_price = ai_data.get("dns_new_price")
    market_used_price = ai_data.get("market_used_price")
    profit_rub = ai_data.get("profit_rub", 0)
    profit_percent = ai_data.get("profit_percent", 0)
    verdict = html.escape(ai_data.get("verdict", ""), quote=False)
    risks = html.escape(ai_data.get("risks", ""), quote=False)

    dns_str = f"~{dns_new_price:,.0f} ₽" if isinstance(dns_new_price, (int, float)) else "Не найдено"
    market_str = f"~{market_used_price:,.0f} ₽" if isinstance(market_used_price, (int, float)) else "Не найдено"

    try:
        p = float(price)
        price_str = f"{p:,.0f} ₽".replace(',', ' ')
    except:
        price_str = f"{price} ₽"

    final_text = (
        f"🔥 <b>{safe_title}</b>\n\n"
        f"💰 <b>Цена продавца:</b> {price_str}\n"
        f"🏪 <b>Новый в ДНС / рознице:</b> {dns_str.replace(',', ' ')}\n"
        f"📊 <b>Рынок Б/У:</b> {market_str.replace(',', ' ')}\n"
        f"📈 <b>Потенциальный профит:</b> +{profit_rub:,.0f} ₽ ({profit_percent}%)\n\n"
        f"🧠 <b>Оценка:</b> {verdict}\n"
        f"⚠️ <b>Что проверить:</b> {risks}"
    )

    reply_markup = {
        "inline_keyboard": [[
            {"text": "🔗 Открыть на Авито", "url": url_ad},
            {"text": "🔍 Поиск в DNS", "url": f"https://www.dns-shop.ru/search/?q={urllib.parse.quote(title)}"}
        ]]
    }

    std_requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id, 
            "text": final_text, 
            "parse_mode": "HTML",
            "reply_markup": reply_markup
        }
    )

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
