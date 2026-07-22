#!/usr/bin/env python3
"""
Daily fuel price fetcher for GarageBookPro.
Scrapes goodreturns.in for Chennai & Coimbatore petrol/diesel rates,
sanity-checks against the previous saved value, and writes to Firebase
RTDB (appConfig/fuelPrices) using a scoped "fuelbot" service account.

If scraping fails or a price looks implausible (jumped by more than
₹5 from the last saved value), the script SKIPS the update rather than
writing bad data — the admin's manual "Save" button in the app remains
the fallback.
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

PAGES = {
    "chennaiPetrol": ("petrol", "Chennai", "https://www.goodreturns.in/petrol-price-in-chennai.html"),
    "chennaiDiesel": ("diesel", "Chennai", "https://www.goodreturns.in/diesel-price-in-chennai.html"),
    "cbePetrol": ("petrol", "Coimbatore", "https://www.goodreturns.in/petrol-price-in-coimbatore.html"),
    "cbeDiesel": ("diesel", "Coimbatore", "https://www.goodreturns.in/diesel-price-in-coimbatore.html"),
}

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
                           "Chrome/124.0 Safari/537.36"
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def extract_price(html, fuel, city):
    pattern = PRICE_PATTERN.format(fuel=re.escape(fuel), city=re.escape(city))
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
        return data["idToken"]


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


def main():
    print("=== GarageBookPro Fuel Price Bot ===")
    id_token = firebase_sign_in()
    print("Signed in to Firebase as fuelbot.")

    previous = firebase_get("appConfig/fuelPrices", id_token) or {}
    print("Previous saved prices:", previous)

    results = {}
    problems = []

    for key, (fuel, city, url) in PAGES.items():
        try:
            html = fetch_page(url)
        except Exception as e:
            problems.append(f"{key}: fetch failed ({e})")
            continue

        price = extract_price(html, fuel, city)
        if price is None:
            problems.append(f"{key}: could not find price pattern on page")
            continue

        prev_price = previous.get(key)
        if prev_price is not None and abs(price - prev_price) > MAX_JUMP:
            problems.append(
                f"{key}: scraped ₹{price} looks suspicious "
                f"(previous ₹{prev_price}, jump > ₹{MAX_JUMP}) — skipping"
            )
            continue

        results[key] = price
        print(f"{key}: ₹{price}  (prev: {prev_price})")
        time.sleep(1)  # be polite between requests

    if problems:
        print("\n--- Issues encountered ---")
        for p in problems:
            print(" -", p)

    if len(results) < 4:
        print(f"\nOnly {len(results)}/4 prices scraped successfully.")
        if len(results) == 0:
            print("Nothing usable — exiting without writing anything.")
            sys.exit(1)
        print("Writing only the successfully-scraped fields, leaving the rest untouched.")

    # Merge with previous so a partial scrape doesn't wipe good existing data
    merged = dict(previous)
    merged.update(results)
    merged["updatedAt"] = int(time.time() * 1000)
    merged["source"] = "auto"

    firebase_put("appConfig/fuelPrices", id_token, merged)
    print("\nFirebase updated:", merged)


if __name__ == "__main__":
    main()
