import time
import logging
import os
from http.client import RemoteDisconnected
from cryptography.fernet import Fernet
from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

MONITORED_SYMBOLS = [
    "RELIANCE", "INFY", "TCS", "HDFCBANK", "ICICIBANK", 
    "SBIN", "ITC", "TATAMOTORS", "BHARTIARTL", "AXISBANK"
]

def load_credentials():
    with open('/opt/zerodha-bot/.secret.key', 'rb') as kf:
        key = kf.read()
    with open('/opt/zerodha-bot/.env.enc', 'rb') as ef:
        decrypted = Fernet(key).decrypt(ef.read()).decode('utf-8')
    env = {}
    for line in decrypted.splitlines():
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env

def get_kite_client():
    env = load_credentials()
    api_key = env.get('ZERODHA_API_KEY')
    token_path = '/opt/zerodha-bot/.access_token'
    if not os.path.exists(token_path):
        raise FileNotFoundError("Access token file missing. Run get_token.py.")
    with open(token_path, 'r') as f:
        access_token = f.read().strip()
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite

def run_supervisor():
    logger.info("Starting Zerodha Live Exit Supervisor service...")
    try:
        kite = get_kite_client()
        logger.info("Successfully connected to Kite Connect API.")
    except Exception as e:
        logger.error(f"Failed to initialize Kite client: {e}")
        return

    logger.info(f"Entering continuous exit supervision loop for Zerodha symbols: {MONITORED_SYMBOLS}")

    while True:
        try:
            positions = kite.positions().get('net', [])
            open_positions = {p['tradingsymbol']: p for p in positions if p['quantity'] != 0}

            for symbol in MONITORED_SYMBOLS:
                if symbol in open_positions:
                    pos = open_positions[symbol]
                    qty = pos['quantity']
                    pnl = pos['pnl']
                    logger.info(f"SUPERVISING {symbol}: Qty={qty}, PnL={pnl}")
                else:
                    logger.info(f"{symbol} EXIT BLOCKED: position not found")

        except (TokenException, RemoteDisconnected) as te:
            logger.warning(f"API connection error/expired token: {te}. Retrying next cycle...")
        except Exception as e:
            logger.error(f"Error fetching position data ({e})")

        time.sleep(60)

if __name__ == '__main__':
    run_supervisor()
