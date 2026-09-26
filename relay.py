import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/', methods=['GET'])
def health_check():
    return "OK", 200

@app.route('/send', methods=['POST'])
def send_message():
    token = os.getenv("TG_BOT_TOKEN")
    if not token:
        return jsonify({"error": "TG_BOT_TOKEN is not configured"}), 500

    data = request.get_json()
    if not data or 'chat_id' not in data or 'text' not in data:
        return jsonify({"error": "Missing 'chat_id' or 'text' in JSON body"}), 400

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    # Перенаправляем весь JSON-payload в Telegram
    try:
        response = requests.post(url, json=data, timeout=15)
        # Возвращаем оригинальный ответ Telegram
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
