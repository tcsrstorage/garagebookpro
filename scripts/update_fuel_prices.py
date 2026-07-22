#!/usr/bin/env python3
"""
Daily fuel price fetcher for GarageBookPro.

Reads the current city list from Firebase (appConfig/fuelPrices/cities —
each city has a display name, a "slug" used to build the goodreturns.in
URL, and last-known petrol/diesel prices), re-scrapes petrol & diesel for
every city in that list, sanity-checks each new price against the
previous saved value, and writes the merged result back.

Admin can add/remove cities entirely from the app (Fuel Prices Update
screen) — this script picks up whatever list is currently in Firebase,
no code changes needed for a new city.

If scraping fails or a price looks implausible (jumped by more than ₹5
from the last saved value), that one city is SKIPPED rather than writing
bad data — the admin's manual "Save" button in the app remains the
fallback for any city that can't be auto-updated.
"""
import os
import re
import sys
import time
import json
import urllib.request
import urllib.error

FIREBASE_API_KEY = "AIzaSyBDwIc6PLljPA4p6nwBAvoAYBVR6o1ryoA"
DATABASE_URL = "https://garagebookpro-default-rtdb.asia-southeast1.firebasedatabase.app"
FUELBOT_EMAIL = os.environ["FUELBOT_EMAIL"]
FUELBOT_PASSWORD = os.environ["FUELBOT_PASSWORD"]

# Matches: "the price of 1 litre of petrol in Chennai is ₹107.76 per litre"
PRICE_PATTERN = r"price of 1 litre of {fuel} in {city} is ₹\s*([\d]+\.[\d]+)\s*per litre"

MAX_JUMP = 5.0  # ₹ — if the scraped price differs from the last saved value
                # by more than this, treat it as a bad scrape and skip.


def fetch_page(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0 Safari/537.36 "
                           "GarageBookProFuelBot/1.0 (+https://garagebookpro.in; "
                           "reads today's petrol/diesel rate once per city per day; "
                           "contact: srrameshin@gmail.com)"
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def extract_price(html, fuel, city_name):
    pattern = PRICE_PATTERN.format(fuel=re.escape(fuel), city=re.escape(city_name))
    m = re.search(pattern, html, re.IGNORECASE)
    if not m:
        return None
    return float(m.group(1))


def firebase_sign_in():
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_API_KEY}"
    payload = json.dumps({
        "email": FUELBOT_EMAIL,
        "password": FUELBOT_PASSWORD,
        "returnSecureToken": True,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data["idToken"], data["localId"]


def firebase_get(path, id_token):
    url = f"{DATABASE_URL}/{path}.json?auth={id_token}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body.strip() != "null" else None
    except urllib.error.HTTPError as e:
        print(f"Firebase GET failed ({path}): {e.code} {e.reason}")
        return None


def firebase_put(path, id_token, data):
    url = f"{DATABASE_URL}/{path}.json?auth={id_token}"
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="PUT",
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def firebase_push(path, id_token, data):
    url = f"{DATABASE_URL}/{path}.json?auth={id_token}"
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST",
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_cities(fuel_prices):
    """Return the cities dict, migrating the old flat schema if needed."""
    if not fuel_prices:
        return {}
    if "cities" in fuel_prices and fuel_prices["cities"]:
        return fuel_prices["cities"]
    # Legacy flat format fallback (chennaiPetrol/chennaiDiesel/cbePetrol/cbeDiesel)
    legacy = {}
    if fuel_prices.get("chennaiPetrol") is not None:
        legacy["chennai"] = {
            "name": "Chennai", "slug": "chennai",
            "petrol": fuel_prices["chennaiPetrol"], "diesel": fuel_prices.get("chennaiDiesel"),
        }
    if fuel_prices.get("cbePetrol") is not None:
        legacy["coimbatore"] = {
            "name": "Coimbatore", "slug": "coimbatore",
            "petrol": fuel_prices["cbePetrol"], "diesel": fuel_prices.get("cbeDiesel"),
        }
    return legacy


def main():
    print("=== GarageBookPro Fuel Price Bot ===")
    id_token, bot_uid = firebase_sign_in()
    print("Signed in to Firebase as fuelbot.")

    fuel_prices = firebase_get("appConfig/fuelPrices", id_token) or {}
    cities = get_cities(fuel_prices)

    if not cities:
        print("No cities configured in appConfig/fuelPrices/cities — nothing to do.")
        sys.exit(0)

    print(f"Cities configured: {[c.get('name') for c in cities.values()]}")

    updated_cities = dict(cities)  # start from existing data, overwrite per-city on success
    problems = []
    changed_summary = []

    for key, city in cities.items():
        name = city.get("name", key)
        slug = city.get("slug") or name.lower().replace(" ", "-")
        prev_petrol = city.get("petrol")
        prev_diesel = city.get("diesel")

        city_result = dict(city)  # copy; only overwrite fields that succeed

        for fuel, prev_value, field in (("petrol", prev_petrol, "petrol"), ("diesel", prev_diesel, "diesel")):
            url = f"https://www.goodreturns.in/{fuel}-price-in-{slug}.html"
            try:
                html = fetch_page(url)
            except Exception as e:
                problems.append(f"{name} {fuel}: fetch failed ({e})")
                continue

            price = extract_price(html, fuel, name)
            if price is None:
                problems.append(f"{name} {fuel}: could not find price pattern on page ({url})")
                continue

            if prev_value is not None and abs(price - prev_value) > MAX_JUMP:
                problems.append(
                    f"{name} {fuel}: scraped ₹{price} looks suspicious "
                    f"(previous ₹{prev_value}, jump > ₹{MAX_JUMP}) — skipping"
                )
                continue

            city_result[field] = price
            print(f"{name} {fuel}: ₹{price}  (prev: {prev_value})")
            time.sleep(1)  # be polite between requests

        if city_result.get("petrol") != city.get("petrol") or city_result.get("diesel") != city.get("diesel"):
            changed_summary.append(f"{name} ₹{city_result.get('petrol'):.2f}/₹{city_result.get('diesel'):.2f}")

        updated_cities[key] = city_result

    if problems:
        print("\n--- Issues encountered ---")
        for p in problems:
            print(" -", p)

    if not changed_summary:
        print("\nNo prices changed (or all scrapes failed) — nothing new to write.")
        sys.exit(0 if not problems else 1)

    merged = dict(fuel_prices)
    merged["cities"] = updated_cities
    merged["updatedAt"] = int(time.time() * 1000)
    merged["source"] = "auto"
    # Drop legacy flat fields once migrated to the cities structure
    for legacy_field in ("chennaiPetrol", "chennaiDiesel", "cbePetrol", "cbeDiesel"):
        merged.pop(legacy_field, None)

    firebase_put("appConfig/fuelPrices", id_token, merged)
    print("\nFirebase updated:", merged)

    firebase_push("activityLog", id_token, {
        "type": "fuel_update",
        "uid": bot_uid,
        "email": "fuelbot (auto)",
        "detail": f"Auto: {', '.join(changed_summary)}",
        "at": {".sv": "timestamp"},
    })
    print("Activity logged.")


if __name__ == "__main__":
    main()
