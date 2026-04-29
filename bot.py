from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

WEBHOOK = os.getenv("WEBHOOK", "").strip()
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "-6"))  # cámbialo si no estás en UTC-6
STATE_PATH = Path(".cache/state.json")

URLS = {
    "Jueves 7": "https://www.ticketmaster.com.mx/bts-world-tour-arirang-in-mexico-ciudad-de-mexico-07-05-2026/event/1400642AA1B78268",
    "Sábado 9": "https://www.ticketmaster.com.mx/bts-world-tour-arirang-in-mexico-ciudad-de-mexico-09-05-2026/event/1400642AA32C84D5",
    "Domingo 10": "https://www.ticketmaster.com.mx/bts-world-tour-arirang-in-mexico-ciudad-de-mexico-10-05-2026/event/1400642AA32D84D7",
}

# Ajustes de alerta
MOVE_REPEAT = 2
TICKET_REPEAT = 3
ALERT_SLEEP_SECONDS = 2

# Phrases que suelen indicar que no hay boletos
NO_TICKET_PHRASES = [
    "no tickets available",
    "currently no tickets available",
    "sold out",
    "tickets are unavailable",
    "there are currently no tickets available",
    "no availability",
]

# CTA más confiables para Ticketmaster / eventos similares
TICKET_CTA_SELECTORS = [
    "button:has-text('Buy Tickets')",
    "button:has-text('Get Tickets')",
    "button:has-text('Find Tickets')",
    "button:has-text('Select Tickets')",
    "button:has-text('Add to Cart')",
    "a:has-text('Buy Tickets')",
    "a:has-text('Get Tickets')",
    "a:has-text('Find Tickets')",
    "a:has-text('Select Tickets')",
    "a:has-text('Add to Cart')",
]


def local_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=TZ_OFFSET_HOURS)


def normalize_text(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text.lower()


def load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def send_discord(message: str, file_path: Optional[str] = None, repeat: int = 1) -> None:
    if not WEBHOOK:
        print("WEBHOOK vacío. Revisa el secret WEBHOOK.")
        return

    for _ in range(repeat):
        try:
            if file_path:
                with open(file_path, "rb") as f:
                    r = requests.post(
                        WEBHOOK,
                        data={"content": message},
                        files={"file": f},
                        timeout=30,
                    )
            else:
                r = requests.post(
                    WEBHOOK,
                    json={"content": message},
                    timeout=30,
                )

            if r.status_code == 429:
                try:
                    retry_after = float(r.json().get("retry_after", 5))
                except Exception:
                    retry_after = 5
                time.sleep(retry_after)
            else:
                # 204 suele ser OK en webhooks de Discord
                print(f"Discord status: {r.status_code}")

            time.sleep(ALERT_SLEEP_SECONDS)

        except Exception as e:
            print("Error Discord:", e)


def visible_count(page, selectors) -> int:
    total = 0
    for selector in selectors:
        loc = page.locator(selector)
        try:
            count = min(loc.count(), 5)
            for i in range(count):
                try:
                    if loc.nth(i).is_visible():
                        total += 1
                except Exception:
                    pass
        except Exception:
            pass
    return total


def collect_signals(page) -> Tuple[str, str, Dict[str, Any]]:
    """
    Devuelve:
      status: BOLETOS | SIN | OTRO
      signature: hash estable del estado relevante
      meta: datos útiles para debug/alertas
    """
    page.wait_for_selector("body", timeout=15000)
    page.wait_for_timeout(2500)

    title = normalize_text(page.title())
    body_text = page.locator("body").inner_text(timeout=15000)
    body_text = normalize_text(body_text)[:6000]

    no_tickets = any(phrase in body_text for phrase in NO_TICKET_PHRASES)

    cta_count = visible_count(page, TICKET_CTA_SELECTORS)

    # Circulitos / seats visibles del mapa
    seat_count = 0
    try:
        seat_count = page.locator("svg circle[fill]:visible").count()
    except Exception:
        seat_count = 0

    # Score conservador para reducir falsos positivos
    ticket_score = 0
    if cta_count > 0:
        ticket_score += 2
    if seat_count >= 8:
        ticket_score += 2
    elif seat_count >= 4:
        ticket_score += 1
    if any(k in body_text for k in ["buy tickets", "get tickets", "find tickets", "select tickets", "add to cart"]):
        ticket_score += 1

    has_tickets = (not no_tickets) and ticket_score >= 2

    if has_tickets:
        status = "BOLETOS"
    elif no_tickets:
        status = "SIN"
    else:
        status = "OTRO"

    # Firma enfocada en elementos útiles para movimiento real
    signal_blob = json.dumps(
        {
            "title": title,
            "body": body_text[:3500],
            "cta_count": cta_count,
            "seat_count": seat_count,
            "ticket_score": ticket_score,
            "status": status,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    signature = hashlib.sha256(signal_blob.encode("utf-8")).hexdigest()

    meta = {
        "title": title,
        "cta_count": cta_count,
        "seat_count": seat_count,
        "ticket_score": ticket_score,
        "status": status,
    }
    return status, signature, meta


def screenshot_name(nombre: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", nombre).strip("_")
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"{safe}_{stamp}.png"


def main() -> None:
    state = load_state()
    updated_state: Dict[str, Any] = dict(state)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="es-ES",
        )

        for nombre, url in URLS.items():
            page = context.new_page()
            prev = state.get(nombre, {})
            prev_signature = prev.get("signature")
            prev_status = prev.get("status")

            try:
                print(f"Revisando {nombre}...")
                page.goto(url, wait_until="domcontentloaded", timeout=60000)

                status, signature, meta = collect_signals(page)
                now = local_now().strftime("%H:%M:%S")

                changed = prev_signature is not None and prev_signature != signature

                # screenshot solo si importa
                sc_path = None
                if changed or status == "BOLETOS":
                    sc_path = screenshot_name(nombre)
                    page.screenshot(path=sc_path, full_page=False)

                # MOVIMIENTO: cambio de firma respecto a la ejecución anterior
                if changed:
                    send_discord(
                        f"🟡 MOVIMIENTO DETECTADO\n"
                        f"📅 {nombre}\n"
                        f"🕒 {now}\n"
                        f"🎯 CTA: {meta['cta_count']} | Seats: {meta['seat_count']} | Score: {meta['ticket_score']}",
                        file_path=sc_path,
                        repeat=MOVE_REPEAT,
                    )

                # BOLETOS: alerta fuerte
                if status == "BOLETOS":
                    send_discord(
                        f"🚨🚨 BOLETOS DISPONIBLES 🚨🚨\n"
                        f"📅 {nombre}\n"
                        f"🕒 {now}\n\n"
                        f"{url}\n"
                        f"🎯 CTA: {meta['cta_count']} | Seats: {meta['seat_count']} | Score: {meta['ticket_score']}",
                        file_path=sc_path,
                        repeat=TICKET_REPEAT,
                    )

                # SIN: solo cuando cambia de estado
                elif prev_status is not None and prev_status != status:
                    send_discord(
                        f"🔴 SIN BOLETOS\n"
                        f"📅 {nombre}\n"
                        f"🕒 {now}\n"
                        f"🎯 CTA: {meta['cta_count']} | Seats: {meta['seat_count']} | Score: {meta['ticket_score']}"
                    )

                updated_state[nombre] = {
                    "signature": signature,
                    "status": status,
                    "updated_at": now,
                    "meta": meta,
                }

            except PlaywrightTimeoutError as e:
                now = local_now().strftime("%H:%M:%S")
                send_discord(
                    f"⚠️ TIMEOUT\n📅 {nombre}\n🕒 {now}\n{e}"
                )
            except Exception as e:
                now = local_now().strftime("%H:%M:%S")
                send_discord(
                    f"⚠️ ERROR\n📅 {nombre}\n🕒 {now}\n{e}"
                )
            finally:
                try:
                    page.close()
                except Exception:
                    pass

        save_state(updated_state)
        browser.close()


if __name__ == "__main__":
    main()