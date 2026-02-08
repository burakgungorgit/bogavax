# bot.py

import os
import time
import math
import json
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from binance.client import Client
from binance.enums import *
import requests

# =========================================================
# ORTAM DEĞİŞKENLERİ
# =========================================================

load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =========================================================
# TELEGRAM SPAM KORUMA
# =========================================================

log_cooldowns = {}
def send_telegram(msg, key=None, cooldown=180):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        if key:
            now = time.time()
            if key in log_cooldowns and now - log_cooldowns[key] < cooldown:
                return
            log_cooldowns[key] = now

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        print(f"Hata - Telegram gönderilemedi: {e}")

LOG_FILE = "log.txt"
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB

# =========================================================
# LOG SİSTEMİ
# =========================================================

def write_log(msg):
    # Dosya boyutu 5MB'ı geçtiyse eski logu yeniden adlandır
    if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) >= MAX_LOG_SIZE:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        new_name = f"log_{timestamp}.txt"
        os.rename(LOG_FILE, new_name)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"[{now}] {msg}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(full_msg + "\n")
    print(full_msg)
    send_telegram(full_msg, key=msg)  # spam kontrolü aktif

# --- Log tekrarını sınırlı yaz ---
def write_log_limited(msg, key, cooldown=300):
    now = time.time()
    if key not in log_cooldowns or now - log_cooldowns[key] > cooldown:
        write_log(msg)
        log_cooldowns[key] = now


# =========================================================
# STATE (POZİSYON) DOSYASI
# =========================================================

STATE_FILE = "state.json"

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {"in_position": False, "entry_price": 0.0}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        write_log(f"Durum kaydedilemedi: {e}")

# =========================================================
# BINANCE BAĞLANTI
# =========================================================

def get_time_offset_ms():
    try:
        server_time = requests.get("https://api.binance.com/api/v3/time", timeout=5).json()["serverTime"]
        local_time = int(time.time() * 1000)
        offset = local_time - server_time
        write_log(f"Zaman farkı: {offset} ms")
        return offset
    except Exception as e:
        write_log_limited(f"Hata - zaman farkı alınamadı: {e}", key="time_offset")
        return

# --- Binance istemcisi ---
client = Client(API_KEY, API_SECRET)
client.time_offset = -get_time_offset_ms()

# =========================================================
# BOT AYARLARI
# =========================================================

SYMBOL = "AVAXUSDT"
INTERVAL = Client.KLINE_INTERVAL_15MINUTE
COMMISSION = 0.001
MIN_USDT = 10

# =========================================================
# STRATEJİ PARAMETRELERİ
# =========================================================
EMA_SHORT = 100
EMA_LONG = 200
STOP_LOSS_MULT = 0.975      # %2,5 zarar
TAKE_PROFIT_MULT = 1.065   # %6,5 kâr


# =========================================================
# BAKİYE OKUMA
# =========================================================

def get_balance(asset):
    try:
        balance = client.get_asset_balance(asset=asset)
        return float(balance['free']) if balance else 0.0
    except:
        return 0.0

# =========================================================
# CÜZDANI YAZDIR
# =========================================================
def print_balances():
    try:
        account = client.get_account()
        balances = account["balances"]
        print("Cüzdan:")
        for b in balances:
            if float(b["free"]) > 0:
                print(f"{b['asset']}: {b['free']}")
    except:
        pass
# =========================================================
# 🚨 BAŞLANGIÇ BAKİYE DOĞRULAMA (AWS / SERVICE KANITI)
# =========================================================

def startup_balance_check():
    write_log("Başlangıç bakiye kontrolü yapılıyor...")

    try:
        account = client.get_account()
        balances = account["balances"]

        found = False
        for b in balances:
            free = float(b["free"])
            if free > 0 and b["asset"] in ["USDT", "AVAX"]:
                write_log(f"BAKİYE | {b['asset']} = {free}")
                found = True

        if not found:
            write_log("UYARI: USDT veya AVAX bakiyesi bulunamadı!")

        write_log("Başlangıç bakiye kontrolü tamamlandı.")

    except Exception as e:
        write_log(f"KRİTİK HATA: Binance bakiyesine erişilemiyor → {e}")

# =========================================================
# MARKET VERİLERİ
# =========================================================

def get_klines(symbol, interval, limit=999):
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'qav', 'trades', 'tbbav', 'tbqav', 'ignore'
    ])
    df['close'] = df['close'].astype(float)
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df[['timestamp', 'close']]

# =========================================================
# EMA HESAPLAMA
# =========================================================

def calculate_ema(df, period):
    return df['close'].ewm(span=period, adjust=False).mean()

# =========================================================
# 🔒 LOT SIZE & PRECISION GÜVENLİK KATMANI
# =========================================================

# =========================================================
# EMİR YUVARLA
# =========================================================

def round_quantity(symbol, qty):
    info = client.get_symbol_info(symbol)
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            precision = int(round(-math.log(step, 10), 0))
            return round(qty, precision)
    return qty

# =========================================================
# MİN NOTİONAL
# =========================================================

def check_min_notional(symbol, qty, price):
    info = client.get_symbol_info(symbol)
    for f in info["filters"]:
        if f["filterType"] == "MIN_NOTIONAL":
            return qty * price >= float(f["minNotional"])
    return qty * price >= 10

# =========================================================
# EMİR GÖNDER
# =========================================================

def place_order(symbol, side, qty, price):
    try:
        if not check_min_notional(symbol, qty, price):
            write_log("Emir reddedildi: minimum notional altı.")
            return None
        return client.create_order(
            symbol=symbol,
            side=side,
            type=ORDER_TYPE_MARKET,
            quantity=qty
        )
    except Exception as e:
        write_log_limited(f"Hata - emir gönderilemedi: {e}", key="order_error")
        return None

# =========================================================
# ORTALAMA FİYAT
# =========================================================

def get_avg_fill_price(order):
    fills = order.get("fills", [])
    if fills:
        total = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        qty = sum(float(f["qty"]) for f in fills)
        return total / qty
    return None

# =========================================================
# ANA DÖNGÜ
# =========================================================

def main():
    write_log("AVAXUSDT Bot başlatıldı (%6,5 TP / %2,5 SL).")

    # 🔍 BAŞLANGIÇ BAKİYE LOGU
    startup_balance_check()

    state = load_state()
    in_position = state["in_position"]
    entry_price = state["entry_price"]
    awaiting_confirmation = False
    signal_time = None


    while True:
        try:
            df = get_klines(SYMBOL, INTERVAL)
            if len(df) < EMA_LONG + 2:
                time.sleep(60)
                continue

            df["ema_short"] = calculate_ema(df, EMA_SHORT)
            df["ema_long"] = calculate_ema(df, EMA_LONG)
            prev, last = df.iloc[-2], df.iloc[-1]

            # --- Alım sinyali ---
            if not in_position and not awaiting_confirmation:
                if prev["ema_short"] < prev["ema_long"] and last["ema_short"] > last["ema_long"]:
                    write_log("Sinyal oluştu. Mum kapanışı bekleniyor.")
                    signal_time = str(last["timestamp"])
                    awaiting_confirmation = True

            elif awaiting_confirmation:
                if str(last["timestamp"]) != signal_time:
                    if last["ema_short"] > last["ema_long"]:
                        usdt = get_balance("USDT")
                        price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
                        qty = round_quantity(SYMBOL, usdt * 0.99 / price)

                        if usdt >= MIN_USDT and qty > 0:
                            order = place_order(SYMBOL, SIDE_BUY, qty, price)
                            if order:
                                entry_price = get_avg_fill_price(order)
                                in_position = True
                                save_state({"in_position": True, "entry_price": entry_price})
                                write_log(f"✅ Alım yapıldı: {qty} AVAX @ {entry_price}")
                        else:
                            write_log("Yetersiz bakiye.")
                    else:
                        write_log("Sinyal geçersizleşti.")
                    awaiting_confirmation = False

            # --- Satış ---
            elif in_position:
                current = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
                avax_balance = get_balance("AVAX")
                sell_qty = round_quantity(SYMBOL, avax_balance * 0.99)

                if current <= entry_price * STOP_LOSS_MULT:
                    write_log(f"🛑 %2,5 zarar stop-loss @ {current}")
                    place_order(SYMBOL, SIDE_SELL, sell_qty, current)
                    in_position = False
                    entry_price = 0.0
                    save_state({"in_position": False, "entry_price": 0.0})

                elif current >= entry_price * TAKE_PROFIT_MULT:
                    write_log(f"✅ %6,5 kâr take-profit @ {current}")
                    place_order(SYMBOL, SIDE_SELL, sell_qty, current)
                    in_position = False
                    entry_price = 0.0
                    save_state({"in_position": False, "entry_price": 0.0})

        except Exception as e:
            write_log(f"Hata: {e}")
            time.sleep(60)

        time.sleep(60)

# =========================================================
# BAŞLAT
# =========================================================

if __name__ == "__main__":
    main()