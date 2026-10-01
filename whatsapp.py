#!/usr/bin/env python3
"""
Send a personalised job-enquiry message + your resume to HR contacts on WhatsApp.

Uses WhatsApp Web through a real Chromium window (Playwright). The first run shows
a QR code — scan it with your phone (WhatsApp > Linked devices). The login is saved
in ./wa_session so later runs start straight away.

Usage:
    python backend/whatsapp.py                                  # contacts.csv + the PDF in this folder
    python backend/whatsapp.py --numbers +919876543210 +91981234...
    python backend/whatsapp.py --dry-run                        # preview only

contacts.csv columns: phone (required), name, company (optional).
message.txt may use {name} and {company} placeholders.
"""

import argparse
import csv
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent.parent  # project root (contacts.csv, message.txt, wa_session/)
SESSION_DIR = HERE / "wa_session"
LOG_FILE = HERE / "sent_log.csv"

# WhatsApp Web changes its markup often, so every element has a few fallbacks.
COMPOSE_BOX = 'footer div[contenteditable="true"]'
ATTACH_BUTTON = (
    'button[title="Attach"], div[title="Attach"], [aria-label="Attach"], '
    'span[data-icon="plus-rounded"], span[data-icon="plus"], span[data-icon="clip"]'
)
DOCUMENT_OPTION = 'li:has-text("Document"), div[role="button"]:has-text("Document"), span:text-is("Document")'
SEND_BUTTON = '[aria-label="Send"], span[data-icon="send"], span[data-icon="wds-ic-send-filled"]'
INVALID_NUMBER = "text=/isn.t on WhatsApp|phone number shared via url is invalid/i"


class NotOnWhatsApp(Exception):
    pass


def normalise_phone(raw: str, default_cc: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+") or len(digits) > 10:
        return digits
    return default_cc + digits  # local 10-digit number -> add country code


def load_contacts(args) -> list[dict]:
    if args.numbers:
        rows = [{"phone": n} for n in args.numbers]
    else:
        with open(args.contacts, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    contacts, seen = [], set()
    for row in rows:
        phone = normalise_phone(row.get("phone", ""), args.country_code)
        if len(phone) < 10 or phone in seen:
            continue
        seen.add(phone)
        contacts.append({
            "phone": phone,
            "name": (row.get("name") or "").strip(),
            "company": (row.get("company") or "").strip(),
        })
    return contacts


def build_message(template: str, contact: dict) -> str:
    msg = template.format(
        name=contact["name"] or "there",
        company=contact["company"] or "your company",
    )
    return msg.strip()


def already_sent() -> set[str]:
    if not LOG_FILE.exists():
        return set()
    with open(LOG_FILE, newline="") as f:
        return {r["phone"] for r in csv.DictReader(f) if r["status"] in ("sent", "not_on_whatsapp")}


def log_result(phone: str, status: str, detail: str = "") -> None:
    new = not LOG_FILE.exists()
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "phone", "status", "detail"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), phone, status, detail])


def sync_contacts_status(contacts_file: Path, default_cc: str) -> None:
    """Write each contact's latest result from sent_log.csv into contacts.csv."""
    if not contacts_file.exists():
        return
    latest = {}  # phone -> (status, timestamp); "sent" is never overwritten
    if LOG_FILE.exists():
        with open(LOG_FILE, newline="") as f:
            for r in csv.DictReader(f):
                if latest.get(r["phone"], ("",))[0] != "sent":
                    latest[r["phone"]] = (r["status"], r["timestamp"])
    with open(contacts_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = [c for c in reader.fieldnames if c not in ("status", "last_attempt")]
        rows = list(reader)
    with open(contacts_file, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields + ["status", "last_attempt"], extrasaction="ignore")
        w.writeheader()
        for row in rows:
            status, ts = latest.get(normalise_phone(row["phone"], default_cc), ("pending", ""))
            w.writerow({**row, "status": status, "last_attempt": ts})


def send_one(page, phone: str, message: str, files: list[Path]) -> None:
    # Pre-fill the text via the URL; WhatsApp keeps line breaks this way.
    page.goto(f"https://web.whatsapp.com/send?phone={phone}&text={quote(message)}")

    box = page.locator(COMPOSE_BOX)
    invalid = page.locator(INVALID_NUMBER)
    deadline = time.time() + 60
    while time.time() < deadline:
        if invalid.count():
            page.keyboard.press("Escape")  # close the "isn't on WhatsApp" popup
            raise NotOnWhatsApp()
        if box.count() and box.first.inner_text().strip():
            break
        page.wait_for_timeout(500)
    else:
        raise RuntimeError("chat did not open in time")

    page.wait_for_timeout(1000)
    box.first.click()
    page.keyboard.press("Enter")
    page.wait_for_timeout(2000)

    if not files:
        return
    # Attach the documents (resume etc.).
    page.locator(ATTACH_BUTTON).first.click()
    with page.expect_file_chooser(timeout=15000) as fc:
        page.locator(DOCUMENT_OPTION).first.click()
    fc.value.set_files([str(f) for f in files])
    page.locator(SEND_BUTTON).last.click(timeout=20000)
    page.wait_for_timeout(4000)  # let the upload finish before navigating away


def main() -> None:
    p = argparse.ArgumentParser(description="WhatsApp HR outreach with resume attached")
    p.add_argument("--resume", type=Path, help="path to your resume (default: the PDF in this folder)")
    p.add_argument("--contacts", default=HERE / "contacts.csv", type=Path)
    p.add_argument("--numbers", nargs="+", help="phone numbers instead of a CSV")
    p.add_argument("--message", default=HERE / "message.txt", type=Path)
    p.add_argument("--country-code", default="91", help="added to 10-digit numbers (default 91)")
    p.add_argument("--min-delay", type=int, default=45, help="seconds between contacts (min)")
    p.add_argument("--max-delay", type=int, default=120, help="seconds between contacts (max)")
    p.add_argument("--limit", type=int, default=20, help="max contacts per run (default 20)")
    p.add_argument("--resend", action="store_true", help="also message numbers already sent to")
    p.add_argument("--dry-run", action="store_true", help="print messages, send nothing")
    args = p.parse_args()

    if args.resume is None:
        pdfs = sorted(HERE.glob("*.pdf"))
        if len(pdfs) != 1:
            sys.exit("Put exactly one PDF in this folder, or pass --resume.")
        args.resume = pdfs[0]
    resume = args.resume.expanduser().resolve()
    if not resume.is_file():
        sys.exit(f"Resume not found: {resume}")
    template = args.message.read_text(encoding="utf-8")
    if "<EDIT ME" in template:
        sys.exit(f"Edit your intro in {args.message} first.")

    sync_contacts_status(args.contacts, args.country_code)
    contacts = load_contacts(args)
    if not args.resend:
        done = already_sent()
        skipped = [c for c in contacts if c["phone"] in done]
        contacts = [c for c in contacts if c["phone"] not in done]
        if skipped:
            print(f"Skipping {len(skipped)} number(s) already messaged (use --resend to override).")
    contacts = contacts[: args.limit]
    if not contacts:
        sys.exit("Nothing to send.")

    if args.dry_run:
        for c in contacts:
            print(f"\n=== +{c['phone']} ===\n{build_message(template, c)}\n[attach: {resume.name}]")
        return

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(SESSION_DIR), headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://web.whatsapp.com")
        print("Waiting for WhatsApp Web login (scan the QR code if shown)...")
        page.wait_for_selector("#pane-side, [aria-label='Chat list']", timeout=180_000)

        for i, c in enumerate(contacts, 1):
            print(f"[{i}/{len(contacts)}] +{c['phone']} {c['name']} {c['company']}".rstrip(), end=" ... ", flush=True)
            try:
                send_one(page, c["phone"], build_message(template, c), [resume])
                log_result(c["phone"], "sent")
                print("sent")
            except NotOnWhatsApp:
                log_result(c["phone"], "not_on_whatsapp")
                print("not on WhatsApp, skipped")
                continue  # nothing was sent, so no need to wait
            except Exception as e:
                log_result(c["phone"], "failed", str(e).splitlines()[0])
                print(f"FAILED ({str(e).splitlines()[0]})")
            finally:
                sync_contacts_status(args.contacts, args.country_code)
            if i < len(contacts):
                wait = random.randint(args.min_delay, args.max_delay)
                print(f"    waiting {wait}s")
                time.sleep(wait)

        ctx.close()
    print(f"\nDone. Results logged to {LOG_FILE}")


if __name__ == "__main__":
    main()
