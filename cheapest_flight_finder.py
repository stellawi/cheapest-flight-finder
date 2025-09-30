"""
Cheapest Multi-City Flight Finder — Scheduled Service with Telegram Alerts & Airline Caching
-------------------------------------------------------------------------------------------
- Tracks cheapest multi-city flights
- Sends Telegram alerts with airlines, price, dates, and Google Flights multi-city link
- Caches airline names in SQLite
"""

import os
import sqlite3
import requests
from dataclasses import dataclass
from typing import Optional, List, Dict, Any
from dotenv import load_dotenv
from amadeus import Client

load_dotenv()

# --- Config ---
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_PATH = "flights.db"

# Multi-city itinerary
ITINERARY = [
    {"origin": "SIN", "destination": "VIE", "departure": "2026-08-22"},
    {"origin": "ZAG", "destination": "SIN", "departure": "2026-09-06"},
]

ADULTS = 1
MAX_OFFERS = 5
CURRENCY = "SGD"

# --- Data Classes ---
@dataclass
class FlightOffer:
    segments: List[Dict[str, Any]]
    price: float

# --- DB helpers ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cheapest_flights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            itinerary TEXT,
            price REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS airlines (
            code TEXT PRIMARY KEY,
            name TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_prev_best(itinerary_key: str) -> Optional[float]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT MIN(price) FROM cheapest_flights WHERE itinerary=?", (itinerary_key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] else None

def save_offer(itinerary_key: str, price: float):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO cheapest_flights (itinerary, price) VALUES (?,?)", (itinerary_key, price))
    conn.commit()
    conn.close()

# --- Airline helpers ---
def get_airline_name(amadeus_client, airline_code: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT name FROM airlines WHERE code=?", (airline_code,))
    row = cur.fetchone()
    if row:
        conn.close()
        return row[0]

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
    return airline_code

# --- Telegram ---
def send_telegram_alert(amadeus, flight_offer: FlightOffer, telegram_token: str, chat_id: str):
    message_lines = ["🛫 *New Cheapest Multi-City Flight Found!*"]
    itinerary_link_parts = []

    for idx, seg in enumerate(flight_offer.segments):
        airline_code = seg['validatingAirlineCodes'][0]
        airline_name = get_airline_name(amadeus, airline_code)
        origin = seg['itineraries'][0]['segments'][0]['departure']['iataCode']
        destination = seg['itineraries'][0]['segments'][-1]['arrival']['iataCode']
        departure_date = seg['itineraries'][0]['segments'][0]['departure']['at'][:10]
        return_date = seg['itineraries'][-1]['segments'][-1]['arrival']['at'][:10]

        message_lines.append(f"Segment {idx+1}: {origin} → {destination} | {airline_name} ({airline_code}) | {departure_date}")
        itinerary_link_parts.append(f"{origin}.{destination}.{departure_date}")

    message_lines.append(f"Total Price: USD {flight_offer.price}")
    booking_link = "https://www.google.com/flights?hl=en#flt=" + "*".join(itinerary_link_parts)
    message_lines.append(f"[Book here]({booking_link})")

    message = "\n".join(message_lines)
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    requests.post(url, data=payload)

# --- Runner ---
def run_check():
    init_db()
    amadeus_client = Client(client_id=AMADEUS_CLIENT_ID, client_secret=AMADEUS_CLIENT_SECRET)

    origins = [seg['origin'] for seg in ITINERARY]
    destinations = [seg['destination'] for seg in ITINERARY]
    departure_dates = [seg['departure'] for seg in ITINERARY]

    try:
        response = amadeus_client.shopping.flight_offers_search.get(
            originLocationCode=origins,
            destinationLocationCode=destinations,
            departureDate=departure_dates,
            adults=ADULTS,
            max=MAX_OFFERS,
            currencyCode=CURRENCY
        )
        offers = response.data
        if not offers:
            print("No offers found.")
            return

        # Choose the cheapest offer by total price
        cheapest_offer_data = min(offers, key=lambda x: float(x['price']['total']))
        cheapest_offer = FlightOffer(segments=[cheapest_offer_data], price=float(cheapest_offer_data['price']['total']))

        # Use a string key for the itinerary
        itinerary_key = "-".join([f"{seg['origin']}->{seg['destination']}:{seg['departure']}" for seg in ITINERARY])
        prev_best = get_prev_best(itinerary_key)

        if prev_best is None or cheapest_offer.price < prev_best:
            send_telegram_alert(amadeus_client, cheapest_offer, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

        save_offer(itinerary_key, cheapest_offer.price)

    except Exception as e:
        print(f"Error searching flights: {e}")

if __name__ == '__main__':
    run_check()
