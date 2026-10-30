import os
import json
import uuid
import requests
import math
from http.server import BaseHTTPRequestHandler, HTTPServer

STATE_FILE = "/root/bot_state.json"
RH_ACCOUNT = os.getenv("ROBINHOOD_ACCOUNT_NUMBER", "RHS00005004")
RH_TOKEN = os.getenv("ROBINHOOD_BEARER_TOKEN", "LIVE_PRODUCTION_OAUTH_TOKEN_STRING_SECRET_HERE")

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "bot_allocations": {
                "crypto": {"allocated_capital": 110.0, "reserve_capital": 11.0, "spendable_ceiling": 99.0, "auto_trade": "OFF", "buy": "OFF", "sell": "OFF"},
                "equity": {"allocated_capital": 130.0, "reserve_capital": 13.0, "spendable_ceiling": 117.0, "auto_trade": "OFF", "buy": "OFF", "sell": "OFF"},
                "zerodha": {"allocated_capital": 50000.0, "reserve_capital": 10000.0, "spendable_ceiling": 40000.0, "auto_trade": "OFF", "buy": "OFF", "sell": "OFF"}
            }
        }
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state: dict):
    temp_file = STATE_FILE + ".tmp"
    with open(temp_file, "w") as f:
        json.dump(state, f, indent=4)
    os.replace(temp_file, STATE_FILE)

class MasterAPIHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):

        # SI_CRYPTO_WORKER_VIEW_V1
        if self.path.split("?", 1)[0] == "/api/v1/workers/crypto":
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                payload = {
                    "error": "LOCAL_ACCESS_REQUIRED",
                    "message": "Access through the SSH tunnel."
                }
                code = 403
            else:
                try:
                    from crypto_worker_view import snapshot
                    payload = snapshot()
                    code = 200
                except Exception as exc:
                    payload = {
                        "error": "WORKER_STATUS_UNAVAILABLE",
                        "error_type": type(exc).__name__,
                    }
                    code = 503
            raw = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store, private")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return


        # SI_LIVE_FEED_ROUTE_V1
        if self.path.split("?", 1)[0] == "/api/v1/live":
            try:
                with open(
                    "/home/botuser/si-bots/dashboard_live_feed.json",
                    "r", encoding="utf-8"
                ) as feed:
                    payload = json.load(feed)
                code = 200
            except (OSError, ValueError):
                payload = {
                    "bots": {},
                    "message": "Broker collector has no readable snapshot yet"
                }
                code = 503
            raw = json.dumps(payload, allow_nan=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store, private")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        # SI_EXECUTION_STATUS_FIX_V1
        if self.path == "/api/v1/status":
            saved = load_state().get("bot_allocations", {})
            payload = {
                "checked_at": __import__("datetime").datetime.now(
                    __import__("datetime").timezone.utc
                ).isoformat(),
                "api": "RESPONDING",
                "dashboard_execution_route": "BLOCKED",
                "bots": {
                    bot: {
                        "requested": saved.get(bot),
                        "configuration_present": bot in saved,
                        "worker_status": "UNKNOWN",
                        "screening_status": "NOT_VERIFIED",
                        "automatic_execution_verified": False,
                        "dashboard_execution_integration": "NOT_CONNECTED",
                        "broker_connection_status": "NOT_CHECKED",
                        "reason": "BOT_EXECUTION_NOT_CONFIGURED"
                    }
                    for bot in ("crypto", "equity", "zerodha")
                }
            }
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store, private")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        # SI_DASHBOARD_ROUTE_V1
        if self.path in ("/", "/index.html"):
            with open(
                "/opt/super-intelligence-dashboard/index.html", "rb"
            ) as page:
                content = page.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return
        if self.path == "/api/v1/config":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(load_state()).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self.send_response(400)
            self.end_headers()
            return
            
        raw_body = self.rfile.read(content_length).decode("utf-8").strip()
        try:
            body = json.loads(raw_body)
        except Exception:
            self.send_response(400)
            self.end_headers()
            return
        
        if self.path == "/api/v1/config/update":
            # SI_EXECUTION_STATUS_FIX_V1
            state = load_state()
            auto_trade_val = body.get("auto_trade")
            buy_val = body.get("buy_toggle") or body.get("buy")
            sell_val = body.get("sell_toggle") or body.get("sell")
            
            for bot in body.get("bots", []):
                if bot not in state["bot_allocations"]: continue
                target = state["bot_allocations"][bot]
                if auto_trade_val is not None: target["auto_trade"] = auto_trade_val
                if buy_val is not None: target["buy"] = buy_val
                if sell_val is not None: target["sell"] = sell_val
            save_state(state)
            
            response_data = json.dumps({"status": "SUCCESS", "updated_state": state}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response_data)
            
        elif self.path == "/api/v1/trade/execute":
            # SI_BLOCK_UNWIRED_EXECUTION_V1
            response_data = json.dumps({
                "status": "BLOCKED",
                "code": "BOT_EXECUTION_NOT_CONFIGURED",
                "message": (
                    "Live execution disabled: account-specific bot adapters "
                    "and durable order/fill tracking are not configured."
                ),
                "submitted": False
            }).encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_data)))
            self.end_headers()
            self.wfile.write(response_data)
            return

        else:
            self.send_response(404)
            self.end_headers()

if __name__ == "__main__":
    server_address = ("", 8000)
    httpd = HTTPServer(server_address, MasterAPIHandler)
    print("Super Intelligence Master Gateway Online on Port 8000")
    httpd.serve_forever()
