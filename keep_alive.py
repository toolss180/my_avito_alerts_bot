import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running")
    
    # Отключаем логирование каждого HTTP-запроса, чтобы не спамить в консоль
    def log_message(self, format, *args):
        pass

def run():
    # Render передает порт через переменную окружения PORT
    port = int(os.environ.get("PORT", 8080))
    server_address = ('0.0.0.0', port)
    httpd = HTTPServer(server_address, RequestHandler)
    httpd.serve_forever()

def keep_alive():
    """Запускает фейковый HTTP-сервер в фоновом потоке."""
    server_thread = threading.Thread(target=run, daemon=True)
    server_thread.start()
