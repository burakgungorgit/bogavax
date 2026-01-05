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
# BAKİYE OKUMA
# =========================================================

def get_balance(asset):
    try:
        balance = client.get_asset_balance(asset=asset)
        return float(balance['free']) if balance else 0.0
    except:
        return 0.0

# --- Cüzdanı yazdır ---
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

def round_quantity(symbol, qty):
    """
    Binance LOT_SIZE kuralına %100 uygun miktar üretir.
    round() ASLA kullanılmaz.
    Her zaman aşağı (floor) yuvarlanır.
    """
    info = client.get_symbol_info(symbol)
    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            step = float(f["stepSize"])
            return math.floor(qty / step) * step
    return qty

# =========================================================
# SEMBOL FILTRELERINI TEK SEFERDE
# =========================================================

def get_symbol_filters(symbol):
    info = client.get_symbol_info(symbol)

    filters = {
        "step_size": None,
        "min_qty": None,
        "min_notional": None
    }

    for f in info["filters"]:
        if f["filterType"] == "LOT_SIZE":
            filters["step_size"] = float(f["stepSize"])
            filters["min_qty"] = float(f["minQty"])

        elif f["filterType"] == "MIN_NOTIONAL":
            filters["min_notional"] = float(f["minNotional"])

    return filters

# =========================================================
# stepSize garantili yuvarlama
# =========================================================

def round_step_size(qty, step_size):
    return math.floor(qty / step_size) * step_size

# =========================================================
# %100 GUVENLI ALIM HESAPLAMA
# =========================================================

def get_safe_buy_qty(symbol, usdt_balance, price):
    filters = get_symbol_filters(symbol)

    step = filters["step_size"]
    min_qty = filters["min_qty"]
    min_notional = filters["min_notional"]

    # %99 kullan → komisyon + fiyat oynaklığı tamponu
    raw_qty = (usdt_balance * 0.99) / price
    qty = round_step_size(raw_qty, step)

    if qty < min_qty:
        return 0

    # minNotional + %10 güvenlik
    if qty * price < min_notional * 1.1:
        return 0

    return qty

# =========================================================
# %100 GUVENLI SATIS HESAPLAMA
# =========================================================

def get_safe_sell_qty(symbol, asset_balance):
    filters = get_symbol_filters(symbol)

    step = filters["step_size"]
    min_qty = filters["min_qty"]

    # %99.9 sat → küsurat & fee güvenliği
    raw_qty = asset_balance * 0.999
    qty = round_step_size(raw_qty, step)

    if qty < min_qty:
        return 0

    return qty

# =========================================================
# EMİR
# =========================================================

def check_min_notional(symbol, qty, price):
    info = client.get_symbol_info(symbol)
    min_notional = 10.0

    for f in info['filters']:
        if f['filterType'] == 'MIN_NOTIONAL':
            min_notional = float(f['minNotional'])
            break

    # güvenlik tamponu
    return qty * price >= min_notional * 1.1


# =========================================================
# EMIR GONDER
# =========================================================

def place_order(symbol, side, qty, price):
    try:
        if not check_min_notional(symbol, qty, price):
            write_log("Emir reddedildi: minimum notional değerin altında.")
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
# ORTALAMA GERCEKLESEN FIYAT
# =========================================================

def get_avg_fill_price(order):
    fills = order.get("fills", [])
    if fills:
        total = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        qty = sum(float(f["qty"]) for f in fills)
        return total / qty
    return None

# =========================================================
# FİYAT & PNL
# =========================================================

def buy_price(p): return p * (1 + COMMISSION)
def sell_price(p): return p * (1 - COMMISSION)
def calc_pnl(entry, current): return sell_price(current) - buy_price(entry)

# =========================================================
# ANA DÖNGÜ
# =========================================================

def main():
    write_log("Bot başlatıldı.")
    state = load_state()
    in_position = state["in_position"]
    entry_price = state["entry_price"]
    write_log(f"Başlangıç durumu: in_position={in_position}, entry_price={entry_price}")
    
    awaiting_confirmation = False
    signal_time = None

    while True:
        try:
            df = get_klines(SYMBOL, INTERVAL)
            if len(df) < 201:
                time.sleep(60)
                continue

            df["ema100"] = calculate_ema(df, 100)
            df["ema200"] = calculate_ema(df, 200)
            prev, last = df.iloc[-2], df.iloc[-1]
            
            # ---------------- ALIŞ ----------------

            if not in_position and not awaiting_confirmation:
                if prev["ema100"] < prev["ema200"] and last["ema100"] > last["ema200"]:
                    write_log("Sinyal oluştu. Mum kapanışı bekleniyor.")
                    signal_time = str(last["timestamp"])
                    awaiting_confirmation = True

            elif awaiting_confirmation:
                if str(last["timestamp"]) != signal_time:
                    if last["ema100"] > last["ema200"]:
                        
                        usdt = get_balance("USDT")
                        price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
                        
                        qty = get_safe_buy_qty(SYMBOL, usdt, price)
                        write_log(f"ALIM DEBUG | usdt={usdt} qty={qty} notional={qty*price}")
                        
                        if usdt >= MIN_USDT and qty > 0:
                            order = place_order(SYMBOL, SIDE_BUY, qty, price)
                            if order:
                                entry_price = get_avg_fill_price(order)
                                in_position = True
                                save_state({"in_position": True, "entry_price": entry_price})
                                write_log(f"Alım yapıldı: {qty} AVAX @ {entry_price}")
                        else:
                            write_log("Yetersiz bakiye.")
                            
                    else:
                        write_log("Sinyal geçersizleşti.")
                    awaiting_confirmation = False
            
             # ---------------- SATIS ----------------

            elif in_position:
                current = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
                target = entry_price * 1.065
                stop = entry_price * 0.975

                if current >= target or current <= stop:
                    asset = SYMBOL.replace("USDT", "")
                    balance = get_balance(asset)
                    
                     # %100 Binance uyumlu satış miktarı
                    sell_qty = get_safe_sell_qty(SYMBOL, balance)
                    write_log(
            f"SATIŞ DEBUG | fiyat={current} hedef={target} stop={stop} "
            f"bakiye={balance} sell_qty={sell_qty}")
                    
                    

                    if sell_qty > 0:
                        write_log(f"Satış sinyali: fiyat {current}, hedef {target}, stop {stop}")
                        
                        order = place_order(SYMBOL, SIDE_SELL, sell_qty, current)
                        
                        if order:
                            sell = get_avg_fill_price(order) or current
                            pnl = calc_pnl(entry_price, sell)
                            result = "Kâr" if sell >= entry_price else "Zarar"
                            
                            write_log(f"{result}: {sell_qty} AVAX satıldı @ {sell} | PnL: {round(pnl, 3)}")
                            
                            in_position = False
                            entry_price = 0.0
                            save_state({"in_position": False, "entry_price": 0.0})
                            
                        else:
                            write_log("Satış emri başarısız oldu.")
                            
                    else:
                        write_log("Satış için yeterli AVAX yok.")
                        

        except Exception as e:
            write_log_limited(f"Hata - döngü: {e}", key="loop_error")
            write_log_limited("İnternet kopmuş olabilir. 60 saniye bekleniyor...", key="internet_wait")
            time.sleep(60)
            continue 

        time.sleep(60)

# --- Başlat ---
if __name__ == "__main__":
    print_balances()
    main()