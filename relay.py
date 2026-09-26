import os
import requests
from flask import Flask, request, jsonify
from duckduckgo_search import DDGS

app = Flask(__name__)

def ask_gemini(title, price, description):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    
    prompt = f"Ты эксперт по вторичному рынку техники. Найди в Google актуальные цены на Б/У рынке РФ на модель '{title}'. Цена продавца: {price} руб. Описание: {description}. Ответь кратко: 1) Реальная вилка цен на рынке 2) Выгода/наценка продавца 3) Риски по описанию 4) Вердикт (Брать / Не брать)."

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
        
    prompt = f"Оцени объявление с учетом найденных цен в интернете: {snippets}\n\nТовар: {title}\nЦена: {price} руб.\nОписание: {description}\nОтветь кратко: 1) Рынок 2) Выгода 3) Риски 4) Вердикт."

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
    text = data.get("text") 

    if title and price and url_ad:
        analysis = get_market_analysis(title, price, description)
            
        final_text = (
            f"📦 <b>{title}</b>\n"
            f"💰 <b>Цена:</b> {price} ₽\n"
            f"🔗 <a href='{url_ad}'>Открыть на Авито</a>\n\n"
            f"🌐 <b>Анализ рынка из интернета:</b>\n"
            f"{analysis}"
        )
    else:
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
