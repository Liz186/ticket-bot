from playwright.sync_api import sync_playwright
import requests
import time
import random
from datetime import datetime
import os

WEBHOOK = os.getenv("WEBHOOK")

URLS = {
    "Jueves 7": "https://www.ticketmaster.com.mx/bts-world-tour-arirang-in-mexico-ciudad-de-mexico-07-05-2026/event/1400642AA1B78268",
    "Sabado 9": "https://www.ticketmaster.com.mx/bts-world-tour-arirang-in-mexico-ciudad-de-mexico-09-05-2026/event/1400642AA32C84D5",
    "Domingo 10": "https://www.ticketmaster.com.mx/bts-world-tour-arirang-in-mexico-ciudad-de-mexico-10-05-2026/event/1400642AA32D84D7"
}

def enviar_alerta(mensaje):
    try:
        requests.post(WEBHOOK, json={"content": mensaje})
    except Exception as e:
        print("Error Discord:", e)

def detectar_drop(page):
    try:
        page.wait_for_selector("svg", timeout=15000)
        page.wait_for_timeout(3000)

        if page.locator("text=No tickets available").count() > 0:
            return False

        seats = page.locator("svg circle[fill]:not([fill='none'])")

        total = seats.count()
        print("Seats:", total)

        if total < 3:
            return False

        for i in range(min(5, total)):
            if seats.nth(i).is_visible():
                return True

        return False

    except Exception as e:
        print("Error:", e)
        return False

with sync_playwright() as p:

    browser = p.chromium.launch(headless=True)
    context = browser.new_context()

    for nombre, url in URLS.items():

        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")

        hay_drop = detectar_drop(page)

        hora = datetime.now().strftime("%H:%M:%S")
        estado = "🟢 BOLETOS" if hay_drop else "🔴 SIN BOLETOS"

        print(f"{nombre}: {estado}")

        enviar_alerta(
            f"{estado}\n📅 {nombre}\n🕒 {hora}\n\n{url}"
        )

        if hay_drop:
            enviar_alerta(
                f"🚨🚨 DROP DETECTADO 🚨🚨\n📅 {nombre}\n🕒 {hora}\n\n{url}"
            )

        page.close()

    browser.close()
