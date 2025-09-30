"""
Optimized Multi-City Cheapest Flight Finder
------------------------------------------
- Suggests cheapest dates for each segment
- Segment-wise optimization reduces API calls
- SGD pricing
- Airline name caching with SQLite
- Telegram alerts with multi-city booking link
"""

import os
import sqlite3
import requests
from dataclasses import dataclass
from typing import List, Dict, Any
from datetime import datetime, timedelta
from dotenv import load_dotenv
from amadeus import Client

load_dotenv()

# --- Config ---
AMADEUS_CLIENT_ID = os.getenv("AMADEUS_CLIENT_ID")
AMADEUS_CLIENT_SECRET = os.getenv("AMADEUS_CLIENT_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_PATH = "flights.db"
ADULTS = 1
MAX_OFFERS = 5
CURRENCY = "SGD"
STEP_DAYS = 2  # Step size for date range (every N days)

# Multi-city itinerary with date ranges
ITINERARY = [
    {"origin": "SIN", "destination": "VIE", "start_date": "2026-08-22", "days_range": 8},
    {"origin": "ZAG", "destination": "SIN", "start_date": "2026-09-06", "days_range": 8},
]

# --- Data Classes ---
@dataclass
class FlightOffer:
    segments: List[Dict[str, Any]]
    price: float
    dates: List[str]

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

def get_prev_best(itinerary_key: str) -> float:
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
    message_lines = ["🛫 *Suggested Cheapest Multi-City Flight!*"]
    itinerary_link_parts = []

    for idx, seg_data in enumerate(flight_offer.segments):
        seg = seg_data
        airline_code = seg['validatingAirlineCodes'][0]
        airline_name = get_airline_name(amadeus, airline_code)
        origin = seg['itineraries'][0]['segments'][0]['departure']['iataCode']
        destination = seg['itineraries'][0]['segments'][-1]['arrival']['iataCode']
        departure_date = seg['itineraries'][0]['segments'][0]['departure']['at'][:10]

        message_lines.append(f"Segment {idx+1}: {origin} → {destination} | {airline_name} ({airline_code}) | {departure_date}")
        itinerary_link_parts.append(f"{origin}.{destination}.{departure_date}")

    message_lines.append(f"Total Price: {CURRENCY} {flight_offer.price}")
    booking_link = "https://www.google.com/flights?hl=en#flt=" + "*".join(itinerary_link_parts)
    message_lines.append(f"[Book here]({booking_link})")

    message = "\n".join(message_lines)
    url = f"https://api.telegram.org/bot{telegram_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    requests.post(url, data=payload)

# --- Date helpers ---
def generate_date_options(segment, step=1):
    start = datetime.strptime(segment["start_date"], "%Y-%m-%d")
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(0, segment["days_range"], step)]

# --- Optimized Segment-wise Cheapest Search ---
def find_cheapest_per_segment(amadeus_client, step=STEP_DAYS):
    segment_cheapest = []

    for seg in ITINERARY:
        best_price = float("inf")
        best_date = None
        best_offer_data = None

        for date in generate_date_options(seg, step=step):
            try:
                response = amadeus_client.shopping.flight_offers_search.get(
                    originLocationCode=seg["origin"],
                    destinationLocationCode=seg["destination"],
                    departureDate=date,
                    adults=ADULTS,
                    max=MAX_OFFERS,
                    currencyCode=CURRENCY
                )
                offers = response.data
                if not offers:
                    continue

                cheapest_offer = min(offers, key=lambda x: float(x['price']['total']))
                price = float(cheapest_offer['price']['total'])
                if price < best_price:
                    best_price = price
                    best_date = date
                    best_offer_data = cheapest_offer
            except Exception as e:
                print(f"Error searching segment {seg['origin']}→{seg['destination']} on {date}: {e}")

        if best_offer_data:
            segment_cheapest.append({"offer": best_offer_data, "date": best_date})
        else:
            segment_cheapest.append(None)

    return segment_cheapest

# --- Runner ---
def run_check():
    init_db()
    amadeus_client = Client(client_id=AMADEUS_CLIENT_ID, client_secret=AMADEUS_CLIENT_SECRET)

    segment_results = find_cheapest_per_segment(amadeus_client, step=STEP_DAYS)
    if all(segment_results):
        best_offer = FlightOffer(
            segments=[seg["offer"] for seg in segment_results],
            price=sum(float(seg["offer"]["price"]["total"]) for seg in segment_results),
            dates=[seg["date"] for seg in segment_results]
        )

        itinerary_key = "-".join([f"{seg['origin']}->{seg['destination']}:{date}" for seg, date in zip(ITINERARY, best_offer.dates)])
        prev_best = get_prev_best(itinerary_key)

        if prev_best is None or best_offer.price < prev_best:
            send_telegram_alert(amadeus_client, best_offer, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

        save_offer(itinerary_key, best_offer.price)
    else:
        print("No valid flight offers found for the given date ranges.")

if __name__ == '__main__':
    run_check()
