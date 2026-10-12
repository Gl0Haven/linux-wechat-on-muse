#!/usr/bin/env python3
"""
屏幕直播服务：每 2 秒截 :99 一次，HTTP 页面自动刷新。
跑在 127.0.0.1:8099，给 cloudflared 做源站。
"""
import http.server
import socketserver
import threading
import time
import subprocess
import os
import sys

PORT = 8099
SHOT = "/tmp/live_screen.png"
os.environ["DISPLAY"] = ":99"

HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>WeChat Live</title>
<style>body{background:#111;color:#ccc;font-family:sans-serif;text-align:center;margin:0;padding:10px}
img{max-width:96vw;border:1px solid #333} .t{color:#888;font-size:12px}</style>
</head><body>
<h3>WeChat :99 实时屏幕</h3>
<div class="t" id="ts"></div>
<img id="s" src="/shot.png">
<script>
setInterval(()=>{
  document.getElementById('s').src='/shot.png?t='+Date.now();
  document.getElementById('ts').textContent=new Date().toLocaleString();
},2000);
</script></body></html>"""

def capture_loop():
    xshot = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xshot.py")
    while True:
        try:
            subprocess.run([sys.executable, xshot, SHOT],
                           timeout=15, capture_output=True,
                           env={**os.environ, "DISPLAY": ":99"})
        except Exception:
            pass
        time.sleep(2)

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        if self.path.startswith("/shot.png"):
            try:
                with open(SHOT, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_error(404)
        else:
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

threading.Thread(target=capture_loop, daemon=True).start()
# 先截一张
_xshot = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xshot.py")
subprocess.run([sys.executable, _xshot, SHOT], capture_output=True,
               env={**os.environ, "DISPLAY": ":99"})

with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
    print(f"live screen on 127.0.0.1:{PORT}", flush=True)
    httpd.serve_forever()
