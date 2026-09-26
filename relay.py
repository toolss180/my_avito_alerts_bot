import os
import re
import html
import requests
from flask import Flask, request, jsonify
from duckduckgo_search import DDGS

app = Flask(__name__)

def ask_gemini(title, price, description):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    
    prompt = f"""Ты эксперт по вторичному рынку техники. Найди в Google актуальные цены на Б/У рынке РФ на модель '{title}'.
Цена продавца: {price} руб.
Описание: {description}.

Ответь СТРОГО по формату без воды:
- Рыночная вилка: [диапазон]
- Выгода: [руб и %]
- Риски: [1 короткое предложение]
- Вердикт: [🟢 БРАТЬ / 🟡 СОМНИТЕЛЬНО / 🔴 ПРОПУСТИТЬ]"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"googleSearch": {}}]
    }
    
    try:
        resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"Gemini API error: {e}")
        
    return None

def ask_openrouter_ddg(title, price, description):
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None
        
    try:
        results = DDGS().text(f"{title} цена авито бу", max_results=3)
        snippets = "\n".join([r['body'] for r in results])
    except Exception as e:
        print(f"DDG error: {e}")
        snippets = "Поиск недоступен."
        
    prompt = f"""Оцени объявление с учетом найденных цен в интернете: {snippets}

Товар: {title}
Цена: {price} руб.
Описание: {description}

Ответь СТРОГО по формату без воды:
- Рыночная вилка: [диапазон]
- Выгода: [руб и %]
- Риски: [1 короткое предложение]
- Вердикт: [🟢 БРАТЬ / 🟡 СОМНИТЕЛЬНО / 🔴 ПРОПУСТИТЬ]"""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "meta-llama/llama-3.3-70b-instruct:free",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": 200
    }
    
    url = "https://openrouter.ai/api/v1/chat/completions"
        
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"OpenRouter API error: {e}")
        
    return None

def get_market_analysis(title, price, description):
    res = ask_gemini(title, price, description)
    if not res:
        res = ask_openrouter_ddg(title, price, description)
    return res if res else "(Анализ рынка временно недоступен)"

@app.route('/', methods=['GET'])
def health_check():
    return "OK", 200

@app.route('/send', methods=['POST'])
def send_message():
    token = os.getenv("TG_BOT_TOKEN")
    if not token:
        return jsonify({"error": "TG_BOT_TOKEN is not configured"}), 500

    data = request.get_json()
    if not data or 'chat_id' not in data:
        return jsonify({"error": "Missing 'chat_id' in JSON body"}), 400

    chat_id = data["chat_id"]
    title = data.get("title")
    price = data.get("price")
    url_ad = data.get("url")
    description = data.get("description", "Не указано")
    location = data.get("location", "Не указано")
    text = data.get("text") 

    if title and price and url_ad:
        analysis = get_market_analysis(title, price, description)
        
        # Фильтрация по вердикту "БРАТЬ" или выгоде > 15%
        should_send = False
        if "БРАТЬ" in analysis:
            should_send = True
        elif "(Анализ рынка временно недоступен)" in analysis:
            should_send = True # Пропускаем, если ИИ лежит, чтобы не терять лоты
        else:
            # Ищем проценты в тексте
            percentages = re.findall(r'(\d+)\s*%', analysis)
            for p in percentages:
                if int(p) > 15:
                    should_send = True
                    break
                    
        if not should_send:
            print(f"Skipping AD: {title} | AI Verdict did not match criteria.")
            return jsonify({"status": "skipped", "reason": "Not profitable enough or risky"}), 200

        # Экранирование для Telegram HTML
        safe_title = html.escape(title, quote=False)
        safe_location = html.escape(location, quote=False)
        safe_analysis = html.escape(analysis, quote=False)
        
        try:
            price_str = f"{int(price):,} ₽".replace(',', ' ')
        except ValueError:
            price_str = f"{price} ₽"

        final_text = (
            f"🔥 <b>{safe_title}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Цена продавца:</b> <code>{price_str}</code>\n"
            f"📍 <b>Город:</b> {safe_location}\n"
            f"🔗 <a href='{url_ad}'><b>👉 Открыть объявление на Авито</b></a>\n\n"
            f"🌐 <b>Оценка рынка (Google Search + AI):</b>\n"
            f"{safe_analysis}\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
    else:
        # Fallback for plain messages (e.g. startup alert)
        final_text = html.escape(text, quote=False) if text else None

    if not final_text:
        return jsonify({"error": "No text or ad data provided"}), 400

    payload = {
        "chat_id": chat_id,
        "text": final_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": data.get("disable_web_page_preview", True)
    }

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    try:
        response = requests.post(url, json=payload, timeout=15)
        try:
            resp_json = response.json()
        except ValueError:
            resp_json = {"error": "Invalid JSON response from Telegram", "text": response.text}
        
        return jsonify(resp_json), response.status_code
    except Exception as e:
        return jsonify({"error": f"Relay request failed: {str(e)}"}), 502

if __name__ == '__main__':
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
