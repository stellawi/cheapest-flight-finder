"""
Cheapest Flight Finder — Scheduled Service with Telegram Alerts & Airline Caching
---------------------------------------------------------------------------------
- Tracks cheapest flights to Austria
- Sends Telegram alerts with airline name, price, dates, and Google Flights booking link
- Caches airline names in SQLite to reduce API calls
- Ready for GitHub Actions scheduled workflow
"""

import os
import time
import sqlite3
import requests
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from datetime import datetime
from dotenv import load_dotenv
from amadeus import Client

load_dotenv()

# --- Config ---
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_PATH = "flights.db"
AUSTRIA_AIRPORTS = ["VIE", "SZG", "GRZ", "INN", "LNZ"]
ORIGIN = os.getenv("ORIGIN", "SIN")
DEPART_DATE = os.getenv("DEPART_DATE", "2025-06-15")
RETURN_DATE = os.getenv("RETURN_DATE", "2025-07-01") or None

# --- Data Classes ---
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

# --- DB helpers ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS cheapest_flights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        origin TEXT,
        destination TEXT,
        departure_date TEXT,
        return_date TEXT,
        price REAL,
        airline_code TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS airlines (
        code TEXT PRIMARY KEY,
        name TEXT
    )''')
    conn.commit()
    conn.close()

def get_prev_best(origin: str, dest: str, depart: str, ret: Optional[str]) -> Optional[float]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''SELECT MIN(price) FROM cheapest_flights WHERE origin=? AND destination=? AND departure_date=? AND (return_date=? OR (? IS NULL AND return_date IS NULL))''',
                (origin, dest, depart, ret, ret))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else None

def save_offer(flight_offer: FlightOffer, airline_code: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('''INSERT INTO cheapest_flights (origin,destination,departure_date,return_date,price,airline_code)
                   VALUES (?,?,?,?,?,?)''',
                (flight_offer.origin, flight_offer.destination, flight_offer.departure_date, flight_offer.return_date, flight_offer.price, airline_code))
    conn.commit()
    conn.close()

# --- Airline helpers ---
def get_airline_name(amadeus_client, airline_code: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT name FROM airlines WHERE code = ?", (airline_code,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row[0]

    # Fetch from Amadeus API
    try:
        response = amadeus_client.reference_data.airlines.get(airlineCodes=airline_code)
        if response.data:
            airline_name = response.data[0].get('businessName') or response.data[0].get('commonName') or airline_code
            cur.execute("INSERT OR REPLACE INTO airlines (code, name) VALUES (?, ?)", (airline_code, airline_name))
            conn.commit()
            conn.close()
            return airline_name
    except Exception as e:
        print(f"Error fetching airline name for {airline_code}: {e}")

    conn.close()
    return airline_code  # fallback

# --- Telegram ---
def send_telegram_alert(amadeus, flight_data, telegram_token, chat_id):
    airline_code = flight_data['validatingAirlineCodes'][0]
    airline_name = get_airline_name(amadeus, airline_code)

    origin = flight_data['itineraries'][0]['segments'][0]['departure']['iataCode']
    destination = flight_data['itineraries'][0]['segments'][-1]['arrival']['iataCode']
    departure_date = flight_data['itineraries'][0]['segments'][0]['departure']['at'][:10]
    return_date = flight_data['itineraries'][-1]['segments'][-1]['arrival']['at'][:10]
    price = flight_data['price']['total']

    booking_link = (
        f"https://www.google.com/flights?hl=en#flt="
        f"{origin}.{destination}.{departure_date}*"
        f"{destination}.{origin}.{return_date}"
    )

    message = (
        f"🛫 *New Cheapest Flight Found!*\n"
        f"Airline: {airline_name} ({airline_code})\n"
        f"Route: {origin} → {destination}\n"
        f"Dates: {departure_date} to {return_date}\n"
        f"Price: USD {price}\n"
        f"[Book here]({booking_link})"
    )

    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    requests.post(url, data=payload)

# --- Runner ---
def run_check():
    init_db()
    amadeus_client = Client(client_id=AMADEUS_CLIENT_ID, client_secret=AMADEUS_CLIENT_SECRET)

    for dest in AUSTRIA_AIRPORTS:
        try:
            response = amadeus_client.shopping.flight_offers_search.get(
                originLocationCode=ORIGIN,
                destinationLocationCode=dest,
                departureDate=DEPART_DATE,
                returnDate=RETURN_DATE,
                adults=1,
                currencyCode="USD",
                max=5
            )
            offers = response.data
            if not offers:
                continue

            cheapest = min(offers, key=lambda x: float(x['price']['total']))
            prev_best = get_prev_best(ORIGIN, dest, DEPART_DATE, RETURN_DATE)

            if prev_best is None or float(cheapest['price']['total']) < prev_best:
                send_telegram_alert(amadeus_client, cheapest, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

            save_offer(
                FlightOffer(
                    origin=ORIGIN,
                    destination=dest,
                    departure_date=DEPART_DATE,
                    return_date=RETURN_DATE,
                    price=float(cheapest['price']['total']),
                    currency=cheapest['price']['currency'],
                    provider='amadeus',
                    offer_data=cheapest
                ),
                airline_code=cheapest['validatingAirlineCodes'][0]
            )

        except Exception as e:
            print(f"Error searching flights to {dest}: {e}")

if __name__ == '__main__':
    run_check()