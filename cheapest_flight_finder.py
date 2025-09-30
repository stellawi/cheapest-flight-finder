"""
Cheapest Flight Finder — Scheduled Service with Telegram Alerts
---------------------------------------------------------------
This version adds:
- Daily scheduled run (Heroku Scheduler or cron)
- Telegram alerts when a *new* cheapest fare is found

Setup:
1. Get Amadeus API keys (see previous instructions)
2. Create a Telegram Bot via @BotFather, get bot token & chat id
3. In `.env` file add:
   AMADEUS_CLIENT_ID=...
   AMADEUS_CLIENT_SECRET=...
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
4. Deploy to Heroku/Render and schedule `python cheapest_flight_finder.py` daily
"""

import os
import time
import sqlite3
import requests
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# --- Config ---
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

AMADEUS_TOKEN_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADEUS_FLIGHT_OFFERS_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"
AUSTRIA_AIRPORTS = ["VIE", "SZG", "GRZ", "INN", "LNZ"]
DB_PATH = "flights.db"

@dataclass
class FlightOffer:
    origin: str
    destination: str
    departure_date: str
    return_date: Optional[str]
    price: float
    currency: str
    provider: str
    offer_data: Dict[str, Any]

class AmadeusClient:
    def __init__(self, client_id: str, client_secret: str):
        if not client_id or not client_secret:
            raise ValueError("Missing Amadeus credentials")
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.token_expiry = 0

    def get_token(self) -> str:
        now = time.time()
        if self.token and now < self.token_expiry - 10:
            return self.token
        resp = requests.post(AMADEUS_TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        resp.raise_for_status()
        data = resp.json()
        self.token = data["access_token"]
        self.token_expiry = now + int(data.get("expires_in", 1800))
        return self.token

    def search_flights(self, origin: str, dest: str, depart_date: str, return_date: Optional[str] = None,
                       adults: int = 1, currency: str = "USD", max_results: int = 5) -> List[FlightOffer]:
        token = self.get_token()
        headers = {"Authorization": f"Bearer {token}"}
        params = {
            "originLocationCode": origin,
            "destinationLocationCode": dest,
            "departureDate": depart_date,
            "adults": adults,
            "currencyCode": currency,
            "max": max_results,
        }
        if return_date:
            params["returnDate"] = return_date

        resp = requests.get(AMADEUS_FLIGHT_OFFERS_URL, headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json()
        offers: List[FlightOffer] = []
        for item in data.get("data", []):
            price_info = item.get("price", {})
            total = float(price_info.get("grandTotal", price_info.get("total", 0)))
            currency_code = price_info.get("currency", currency)
            offers.append(FlightOffer(
                origin=origin,
                destination=dest,
                departure_date=depart_date,
                return_date=return_date,
                price=total,
                currency=currency_code,
                provider="amadeus",
                offer_data=item,
            ))
        return offers

# --- DB helpers ---
def init_db(path: str = DB_PATH):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        origin TEXT,
        destination TEXT,
        departure_date TEXT,
        return_date TEXT,
        price REAL,
        currency TEXT,
        provider TEXT,
        checked_at TEXT
    )''')
    conn.commit()
    conn.close()


def get_prev_best(origin: str, dest: str, depart: str, ret: Optional[str]) -> Optional[float]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''SELECT MIN(price) FROM offers WHERE origin=? AND destination=? AND departure_date=? AND (return_date=? OR (? IS NULL AND return_date IS NULL))''',
                (origin, dest, depart, ret, ret))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def save_offer(o: FlightOffer):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute('''INSERT INTO offers (origin,destination,departure_date,return_date,price,currency,provider,checked_at)
                   VALUES (?,?,?,?,?,?,?,?)''',
                (o.origin, o.destination, o.departure_date, o.return_date, o.price, o.currency, o.provider, now))
    conn.commit()
    conn.close()

# --- Telegram ---
def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text})

# --- Runner ---
def run_check():
    origin = os.getenv("ORIGIN", "SIN")  # default: Singapore
    depart = os.getenv("DEPART_DATE", "2026-08-22")
    ret = os.getenv("RETURN_DATE", "2026-09-06") or None

    init_db()
    client = AmadeusClient(AMADEUS_CLIENT_ID, AMADEUS_CLIENT_SECRET)

    cheapest_overall: Optional[FlightOffer] = None
    for dest in AUSTRIA_AIRPORTS:
        try:
            offers = client.search_flights(origin, dest, depart, ret, adults=1, currency="USD")
            if offers:
                cheapest = min(offers, key=lambda x: x.price)
                prev_best = get_prev_best(origin, dest, depart, ret)
                if prev_best is None or cheapest.price < prev_best:
                    msg = f"New cheapest {origin}->{dest}! {depart}{' - '+ret if ret else ''} → {cheapest.price} {cheapest.currency}"
                    print(msg)
                    send_telegram_message(msg)
                save_offer(cheapest)
                if not cheapest_overall or cheapest.price < cheapest_overall.price:
                    cheapest_overall = cheapest
        except Exception as e:
            print("Error searching", dest, e)

    if cheapest_overall:
        print("Run complete. Overall cheapest:", cheapest_overall.price, cheapest_overall.currency)
    else:
        print("No offers found.")

if __name__ == "__main__":
    run_check()
