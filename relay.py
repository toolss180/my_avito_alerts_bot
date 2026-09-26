import os
import requests
from flask import Flask, request, jsonify
from duckduckgo_search import DDGS

app = Flask(__name__)

def ask_gemini(title, price, description, location):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    
    prompt = f"""Ты профессиональный аналитик перепродажи техники на Авито. Оцени предложение:
Товар: {title}
Цена продавца: {price} руб.
Описание: {description}
Город: {location}

Сделай поиск в Google и найди актуальную среднюю цену на Б/У рынке РФ на эту точную модель.
Ответь СТРОГО по шаблону (без markdown, только текст):
🌐 Реальный рынок (поиск Google): [диапазон цен]
📊 Выгода: [разница в рублях и %]
⚠️ Риски: [анализ описания]
🎯 Вердикт: [БРАТЬ / СПОРНО / НЕ БРАТЬ]"""

    payload = {
        "contents": [{
            "parts": [{"text": prompt}]
        }],
        "tools": [{"googleSearch": {}}]
    }
    
    try:
        resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=20)
        if resp.status_code == 200:
            data = resp.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        print(f"Gemini API error: {e}")
        
    return None

def ask_openrouter(title, price, description, location):
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None
        
    # DuckDuckGo fallback search
    try:
        results = DDGS().text(f"{title} цена бу", max_results=3)
        snippets = "\n".join([r['body'] for r in results])
    except Exception as e:
        print(f"DDG error: {e}")
        snippets = "Поиск недоступен."
        
    prompt = f"""Ты эксперт по перепродаже техники. Оцени предложение:
Товар: {title}
Цена продавца: {price} руб.
Описание: {description}
Город: {location}

Результаты веб-поиска (DDG):
{snippets}

Ответь СТРОГО по шаблону:
🌐 Реальный рынок (поиск DDG): [диапазон цен]
📊 Выгода: [разница в рублях и %]
⚠️ Риски: [анализ описания]
🎯 Вердикт: [БРАТЬ / СПОРНО / НЕ БРАТЬ]"""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    model = os.getenv("AI_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": 200
    }
    
    url = "https://openrouter.ai/api/v1/chat/completions"
        
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"OpenRouter API error: {e}")
        
    return None

def get_ai_analysis(title, price, description, location):
    provider = os.getenv("AI_PROVIDER", "gemini").lower()
    
    if provider == "both":
        res = ask_gemini(title, price, description, location)
        if not res:
            res = ask_openrouter(title, price, description, location)
        return res
    elif provider == "openrouter":
        return ask_openrouter(title, price, description, location)
    else: # default to gemini
        res = ask_gemini(title, price, description, location)
        if not res and os.getenv("OPENROUTER_API_KEY"): # fallback
            res = ask_openrouter(title, price, description, location)
        return res

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
    text = data.get("text") # Fallback or raw text

    if title and price and url_ad:
        # It's an ad alert, process via AI
        ai_analysis_text = get_ai_analysis(title, price, description, location)
        if not ai_analysis_text:
            ai_analysis_text = "(ИИ временно недоступен)"
            
        final_text = (
            f"📦 <b>{title}</b>\n"
            f"💰 <b>Цена продавца:</b> {price} ₽\n"
            f"📍 <b>Локация:</b> {location}\n"
            f"🔗 <a href='{url_ad}'>Открыть объявление на Авито</a>\n\n"
            f"🧠 <b>Анализ рынка и рисков:</b>\n"
            f"{ai_analysis_text}"
        )
    else:
        # It's a raw message (e.g. startup notification)
        final_text = text

    if not final_text:
        return jsonify({"error": "No text or ad data provided"}), 400

    payload = {
        "chat_id": chat_id,
        "text": final_text,
        "parse_mode": data.get("parse_mode", "HTML"),
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
