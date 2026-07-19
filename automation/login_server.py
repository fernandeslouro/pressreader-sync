#!/usr/bin/env python3
"""Temporary localhost-only remote control for PressReader Sync login."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

PAGE = b"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>PressReader Sync login</title><style>
body{margin:0;background:#222;color:#eee;font:16px sans-serif}header{position:sticky;top:0;padding:9px;
background:#111;display:flex;gap:12px;align-items:center;z-index:2}button{font:inherit;padding:7px 12px}
#screen{display:block;width:min(100%,1440px);height:auto;margin:auto;background:white;cursor:default}
#status{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.hint{color:#bbb;font-size:13px}
</style></head><body><header><b>PressReader Sync login</b><span id="status">Connecting...</span>
<button id="finish">Save login &amp; close</button></header>
<div class="hint">Click and type normally. Clipboard paste and mouse-wheel scrolling are supported.</div>
<img id="screen" draggable="false" alt="Remote PressReader browser"></body><script>
const screen=document.getElementById('screen'), status=document.getElementById('status');
async function send(value){await fetch('/event',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value)});refresh()}
async function refresh(){screen.src='/screen.png?t='+Date.now();try{let r=await fetch('/meta');let j=await r.json();status.textContent=j.url}catch(e){}}
screen.addEventListener('click',e=>{let r=screen.getBoundingClientRect();send({kind:'click',x:(e.clientX-r.left)*screen.naturalWidth/r.width,y:(e.clientY-r.top)*screen.naturalHeight/r.height})});
screen.addEventListener('contextmenu',e=>e.preventDefault());
screen.addEventListener('wheel',e=>{e.preventDefault();send({kind:'wheel',dx:e.deltaX,dy:e.deltaY})},{passive:false});
window.addEventListener('keydown',e=>{if(e.ctrlKey||e.metaKey){return} e.preventDefault();
 const named={Enter:'Enter',Backspace:'Backspace',Tab:'Tab',Escape:'Escape',ArrowUp:'ArrowUp',ArrowDown:'ArrowDown',ArrowLeft:'ArrowLeft',ArrowRight:'ArrowRight',Delete:'Delete'};
 if(named[e.key])send({kind:'key',key:named[e.key]});else if(e.key.length===1)send({kind:'type',text:e.key})});
window.addEventListener('paste',e=>{e.preventDefault();send({kind:'type',text:e.clipboardData.getData('text')})});
document.getElementById('finish').onclick=async()=>{if(confirm('Save this browser session and stop the login server?')){await send({kind:'finish'});document.body.innerHTML='<h2>Login saved. You may close this tab.</h2>'}};
screen.onload=()=>setTimeout(refresh,700);refresh();
</script></html>"""


class LoginServer(HTTPServer):
    should_stop = False

    def __init__(self, address: tuple[str, int], context: Any):
        super().__init__(address, Handler)
        self.context = context
        self.timeout = 1

    @property
    def page(self):
        pages = self.context.pages
        return pages[-1] if pages else self.context.new_page()


class Handler(BaseHTTPRequestHandler):
    server: LoginServer

    def log_message(self, _format: str, *_args: Any) -> None:
        pass

    def reply(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/?"):
            self.reply(HTTPStatus.OK, "text/html; charset=utf-8", PAGE)
        elif self.path.startswith("/screen.png"):
            try:
                body = self.server.page.screenshot(type="png", animations="disabled")
                self.reply(HTTPStatus.OK, "image/png", body)
            except Exception as err:
                self.reply(HTTPStatus.INTERNAL_SERVER_ERROR, "text/plain", str(err).encode())
        elif self.path == "/meta":
            body = json.dumps({"url": self.server.page.url}).encode()
            self.reply(HTTPStatus.OK, "application/json", body)
        else:
            self.reply(HTTPStatus.NOT_FOUND, "text/plain", b"Not found")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/event":
            self.reply(HTTPStatus.NOT_FOUND, "text/plain", b"Not found")
            return
        try:
            length = min(int(self.headers.get("Content-Length", "0")), 65536)
            event = json.loads(self.rfile.read(length))
            kind = event.get("kind")
            page = self.server.page
            if kind == "click":
                page.mouse.click(float(event["x"]), float(event["y"]))
            elif kind == "wheel":
                page.mouse.wheel(float(event.get("dx", 0)), float(event.get("dy", 0)))
            elif kind == "key":
                page.keyboard.press(str(event["key"]))
            elif kind == "type":
                page.keyboard.insert_text(str(event.get("text", ""))[:8192])
            elif kind == "finish":
                self.server.should_stop = True
            else:
                raise ValueError("unknown event")
            self.reply(HTTPStatus.OK, "application/json", b"{}")
        except Exception as err:
            self.reply(HTTPStatus.BAD_REQUEST, "text/plain", str(err).encode())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6080)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.profile.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(args.profile), headless=True, accept_downloads=True,
            viewport={"width": 1440, "height": 1000}, locale="en-US",
            args=["--disable-dev-shm-usage"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.pressreader.com/catalog", wait_until="domcontentloaded", timeout=60_000)
        server = LoginServer((args.host, args.port), context)
        print(f"PressReader Sync login server listening on {args.host}:{args.port}", flush=True)
        while not server.should_stop:
            server.handle_request()
        context.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
