"""Local dev/preview server: serves the SPA and proxies /api to the backend.

Only used outside docker compose (docker uses nginx.conf).  Keeps the browser
same-origin so localStorage tokens and fetch behave exactly as in production.
"""
import http.server
import os
import socketserver
import urllib.error
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'frontend')
BACKEND = os.getenv('BACKEND_ORIGIN', 'http://127.0.0.1:8000')


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, *args):
        pass

    def _proxy(self, method):
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ('host', 'content-length')}
        req = urllib.request.Request(BACKEND + self.path[len('/api'):], data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req) as resp:
                data = resp.read()
                self.send_response(resp.status)
                for key, value in resp.headers.items():
                    if key.lower() not in ('transfer-encoding', 'connection', 'content-length'):
                        self.send_header(key, value)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as err:
            data = err.read()
            self.send_response(err.code)
            self.send_header('Content-Type', err.headers.get('Content-Type', 'application/json'))
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith('/api/'):
            return self._proxy('GET')
        return super().do_GET()

    def do_POST(self):
        if self.path.startswith('/api/'):
            return self._proxy('POST')
        self.send_error(405)

    def do_PUT(self):
        if self.path.startswith('/api/'):
            return self._proxy('PUT')
        self.send_error(405)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


if __name__ == '__main__':
    port = int(os.getenv('PREVIEW_PORT', '8080'))
    with Server(('127.0.0.1', port), Handler) as httpd:
        print(f'preview on http://127.0.0.1:{port} -> {BACKEND}')
        httpd.serve_forever()
