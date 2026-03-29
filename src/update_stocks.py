import gzip
import json
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from dotenv import load_dotenv


# =========================
# PATHS
# =========================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"

load_dotenv(ENV_PATH)


# =========================
# CONFIG
# =========================
WB_TOKEN = os.getenv("WB_TOKEN")
XML_URL = os.getenv("XML_URL", "https://ctradei.com/x/shop2_1410641-yml.xml")
TARGET_WAREHOUSE_NAME = os.getenv("TARGET_WAREHOUSE_NAME", "Ситрейд")
STOCKS_DRY_RUN = os.getenv("STOCKS_DRY_RUN", "true").strip().lower() == "true"
ZERO_MISSING_IN_XML = os.getenv("ZERO_MISSING_IN_XML", "false").strip().lower() == "true"
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "1000"))
MIN_SAFE_STOCK = int(os.getenv("MIN_SAFE_STOCK", "2"))

# Universal notifications
NOTIFY_ENABLED = os.getenv("NOTIFY_ENABLED", "false").strip().lower() == "true"
NOTIFY_PROVIDER = os.getenv("NOTIFY_PROVIDER", "").strip().lower()
NOTIFY_TO = os.getenv("NOTIFY_TO", "").strip()
NOTIFY_FROM_EMAIL = os.getenv("NOTIFY_FROM_EMAIL", "").strip()
NOTIFY_FROM_NAME = os.getenv("NOTIFY_FROM_NAME", "Automation").strip()
NOTIFY_SUCCESS_POLICY = os.getenv("NOTIFY_SUCCESS_POLICY", "hourly").strip().lower()
NOTIFY_SUBJECT_PREFIX = os.getenv("NOTIFY_SUBJECT_PREFIX", "[AUTOMATION]").strip()
NOTIFY_STATE_FILE_RAW = os.getenv("NOTIFY_STATE_FILE", ".runtime/notify_state.json").strip()
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()

if not WB_TOKEN:
    raise ValueError("WB_TOKEN не найден в .env")

NOTIFY_STATE_FILE = Path(NOTIFY_STATE_FILE_RAW)
if not NOTIFY_STATE_FILE.is_absolute():
    NOTIFY_STATE_FILE = PROJECT_ROOT / NOTIFY_STATE_FILE


# =========================
# URL API
# =========================
CARDS_LIST_URL = "https://content-api.wildberries.ru/content/v2/get/cards/list"
WAREHOUSES_URL = "https://marketplace-api.wildberries.ru/api/v3/warehouses"
STOCKS_UPDATE_URL = "https://marketplace-api.wildberries.ru/api/v3/stocks/{warehouse_id}"

HEADERS = {
    "Authorization": WB_TOKEN,
    "Content-Type": "application/json",
}


# =========================
# HTTP
# =========================
RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = int(os.getenv("HTTP_MAX_RETRIES", "3"))
RETRY_SLEEP_SECONDS = float(os.getenv("HTTP_RETRY_SLEEP_SECONDS", "2"))


def _request_with_retry(method, url, **kwargs):
    last_exception = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, **kwargs)

            if resp.status_code in RETRY_STATUS_CODES:
                print(
                    f"[HTTP RETRY] {method} {url} -> {resp.status_code}, "
                    f"attempt {attempt}/{MAX_RETRIES}"
                )
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_SLEEP_SECONDS * attempt)
                    continue

            resp.raise_for_status()
            return resp

        except requests.exceptions.RequestException as e:
            last_exception = e
            print(
                f"[HTTP ERROR] {method} {url}, "
                f"attempt {attempt}/{MAX_RETRIES}: {e}"
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_SLEEP_SECONDS * attempt)
                continue
            raise

    if last_exception:
        raise last_exception

    raise RuntimeError(f"HTTP request failed: {method} {url}")


def http_get(url, headers=None, timeout=120):
    return _request_with_retry("GET", url, headers=headers, timeout=timeout)


def http_post(url, json_payload=None, headers=None, timeout=120):
    return _request_with_retry(
        "POST",
        url,
        json=json_payload,
        headers=headers,
        timeout=timeout,
    )


def http_put(url, json_payload=None, headers=None, timeout=120):
    return _request_with_retry(
        "PUT",
        url,
        json=json_payload,
        headers=headers,
        timeout=timeout,
    )


# =========================
# XML
# =========================
def download_xml_bytes(url: str) -> bytes:
    resp = http_get(url, timeout=180)
    content = resp.content

    content_type = resp.headers.get("Content-Type", "").lower()
    if url.endswith(".gz") or "gzip" in content_type:
        try:
            content = gzip.decompress(content)
        except Exception:
            pass

    return content


def parse_supplier_stocks_from_xml(xml_bytes: bytes) -> Dict[str, int]:
    root = ET.parse(BytesIO(xml_bytes)).getroot()
    offers = root.findall(".//offer")

    result: Dict[str, int] = {}
    bad_count = 0

    if not offers:
        print("В XML не найдено ни одного <offer>.")
        return result

    for offer in offers:
        certificate = (offer.findtext("certificate") or "").strip()
        count_raw = (offer.findtext("count") or "").strip()

        if not certificate:
            continue

        try:
            normalized = count_raw.replace(",", ".")
            count = int(float(normalized)) if normalized else 0
        except Exception:
            count = 0
            bad_count += 1
            if bad_count <= 20:
                print(f"[WARN] Не удалось распарсить count для {certificate}: {count_raw!r}")

        result[certificate] = result.get(certificate, 0) + count

    print(f"Остатков parsed: {len(result)}")
    print(f"Проблемных count: {bad_count}")
    return result


# =========================
# WB CARDS
# =========================
def get_all_wb_cards():
    cards = []
    cursor_nm_id = None
    cursor_updated_at = None

    while True:
        payload = {
            "settings": {
                "cursor": {"limit": 100},
                "filter": {"withPhoto": -1},
            }
        }

        if cursor_nm_id and cursor_updated_at:
            payload["settings"]["cursor"]["nmID"] = cursor_nm_id
            payload["settings"]["cursor"]["updatedAt"] = cursor_updated_at

        resp = http_post(CARDS_LIST_URL, json_payload=payload, headers=HEADERS)
        data = resp.json()

        batch = data.get("cards", [])
        if not batch:
            break

        cards.extend(batch)

        if len(batch) < 100:
            break

        cursor = data.get("cursor", {})
        cursor_nm_id = cursor.get("nmID")
        cursor_updated_at = cursor.get("updatedAt")

        if not cursor_nm_id or not cursor_updated_at:
            break

        time.sleep(0.25)

    return cards


def build_vendorcode_to_chrtid_map(cards):
    result = {}

    for card in cards:
        vendor_code = (card.get("vendorCode") or "").strip()
        sizes = card.get("sizes", []) or []

        if not vendor_code:
            continue

        for size in sizes:
            chrt_id = size.get("chrtID") or size.get("chrtId")
            if chrt_id:
                result[vendor_code] = int(chrt_id)
                break

    return result


# =========================
# WAREHOUSES
# =========================
def get_wb_warehouses():
    resp = http_get(WAREHOUSES_URL, headers=HEADERS)
    data = resp.json()
    return data if isinstance(data, list) else []


def find_warehouse_id_by_name(warehouses, target_name):
    target_name_lower = target_name.strip().lower()

    for wh in warehouses:
        name = (wh.get("name") or "").strip()
        if name.lower() == target_name_lower:
            return int(wh["id"])

    for wh in warehouses:
        name = (wh.get("name") or "").strip()
        if target_name_lower in name.lower():
            return int(wh["id"])

    names = [w.get("name", "") for w in warehouses]
    raise RuntimeError(f'Склад с именем "{target_name}" не найден. Доступные склады: {names}')


# =========================
# STOCK RULES
# =========================
def normalize_stock_for_wb(raw_stock: int) -> int:
    stock = max(0, int(raw_stock))
    if stock <= MIN_SAFE_STOCK:
        return 0
    return stock


# =========================
# STOCK UPDATE + REPORT
# =========================
def build_stock_updates(
    supplier_stocks: Dict[str, int],
    wb_map: Dict[str, int],
    zero_missing_in_xml: bool = False,
) -> Tuple[List[dict], List[str], List[str], List[dict]]:
    updates: List[dict] = []
    missing_in_wb: List[str] = []
    missing_in_xml: List[str] = []
    changes: List[dict] = []

    for vendor_code, raw_count in supplier_stocks.items():
        chrt_id = wb_map.get(vendor_code)

        if not chrt_id:
            missing_in_wb.append(vendor_code)
            continue

        wb_amount = normalize_stock_for_wb(raw_count)

        updates.append(
            {
                "vendorCode": vendor_code,
                "chrtId": chrt_id,
                "amount": wb_amount,
            }
        )

        if wb_amount == 0 and raw_count > 0:
            reason = f"скрыли: остаток поставщика {raw_count} <= порога {MIN_SAFE_STOCK}"
            change_type = "hidden"
        elif wb_amount < raw_count:
            reason = "уменьшили после бизнес-логики"
            change_type = "decreased"
        elif wb_amount > raw_count:
            reason = "увеличили"
            change_type = "increased"
        else:
            reason = "без изменений"
            change_type = "unchanged"

        changes.append(
            {
                "vendorCode": vendor_code,
                "raw_stock": raw_count,
                "wb_amount": wb_amount,
                "delta": wb_amount - raw_count,
                "change_type": change_type,
                "reason": reason,
            }
        )

    if zero_missing_in_xml:
        supplier_keys = set(supplier_stocks.keys())

        for vendor_code, chrt_id in wb_map.items():
            if vendor_code not in supplier_keys:
                missing_in_xml.append(vendor_code)
                updates.append(
                    {
                        "vendorCode": vendor_code,
                        "chrtId": chrt_id,
                        "amount": 0,
                    }
                )
                changes.append(
                    {
                        "vendorCode": vendor_code,
                        "raw_stock": None,
                        "wb_amount": 0,
                        "delta": None,
                        "change_type": "zeroed_missing_in_xml",
                        "reason": "обнулили: карточка есть в WB, но товара нет в XML поставщика",
                    }
                )

    return updates, missing_in_wb, missing_in_xml, changes


def print_changes_report(changes: List[dict]) -> None:
    hidden = [x for x in changes if x["change_type"] == "hidden"]
    decreased = [x for x in changes if x["change_type"] == "decreased"]
    increased = [x for x in changes if x["change_type"] == "increased"]
    unchanged = [x for x in changes if x["change_type"] == "unchanged"]
    zeroed_missing = [x for x in changes if x["change_type"] == "zeroed_missing_in_xml"]

    print("\n=== СВОДКА ИЗМЕНЕНИЙ ===")
    print(f"Скрыли из-за малого остатка: {len(hidden)}")
    print(f"Уменьшили: {len(decreased)}")
    print(f"Увеличили: {len(increased)}")
    print(f"Без изменений: {len(unchanged)}")
    print(f"Обнулили из-за отсутствия в XML: {len(zeroed_missing)}")

    if hidden:
        print("\n--- Скрыли из-за малого остатка ---")
        for x in hidden[:100]:
            print(f'{x["vendorCode"]}: {x["raw_stock"]} -> {x["wb_amount"]} | {x["reason"]}')

    if decreased:
        print("\n--- Уменьшили ---")
        for x in decreased[:100]:
            print(f'{x["vendorCode"]}: {x["raw_stock"]} -> {x["wb_amount"]} | {x["reason"]}')

    if increased:
        print("\n--- Увеличили ---")
        for x in increased[:100]:
            print(f'{x["vendorCode"]}: {x["raw_stock"]} -> {x["wb_amount"]} | {x["reason"]}')

    if zeroed_missing:
        print("\n--- Обнулили из-за отсутствия в XML ---")
        for x in zeroed_missing[:100]:
            print(f'{x["vendorCode"]}: нет в XML -> 0 | {x["reason"]}')


def update_wb_stocks(warehouse_id, updates):
    url = STOCKS_UPDATE_URL.format(warehouse_id=warehouse_id)

    for i in range(0, len(updates), BATCH_SIZE):
        batch = updates[i:i + BATCH_SIZE]

        payload = {
            "stocks": [
                {
                    "chrtId": item["chrtId"],
                    "amount": item["amount"],
                }
                for item in batch
            ]
        }

        print(f"\nОтправка батча: {len(batch)}")

        resp = http_put(url, json_payload=payload, headers=HEADERS, timeout=180)

        try:
            print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
        except Exception:
            print(resp.text if resp.text else "<empty>")

        time.sleep(0.3)


# =========================
# NOTIFICATIONS
# =========================
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_notify_state() -> dict:
    if not NOTIFY_STATE_FILE.exists():
        return {}
    try:
        return json.loads(NOTIFY_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_notify_state(state: dict) -> None:
    NOTIFY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTIFY_STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def should_send_success_notification() -> bool:
    policy = NOTIFY_SUCCESS_POLICY

    if policy == "always":
        return True
    if policy == "never":
        return False

    state = load_notify_state()
    last_sent_raw = state.get("last_success_sent_at")
    if not last_sent_raw:
        return True

    try:
        last_sent = datetime.fromisoformat(last_sent_raw)
    except Exception:
        return True

    now = utc_now()

    if policy == "hourly":
        return now - last_sent >= timedelta(hours=1)
    if policy == "daily":
        return now - last_sent >= timedelta(days=1)

    return True


def mark_success_notification_sent() -> None:
    state = load_notify_state()
    state["last_success_sent_at"] = utc_now().isoformat()
    save_notify_state(state)


def send_brevo_email(subject: str, text_body: str) -> None:
    if not BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY не найден в .env")
    if not NOTIFY_TO:
        raise RuntimeError("NOTIFY_TO не найден в .env")
    if not NOTIFY_FROM_EMAIL:
        raise RuntimeError("NOTIFY_FROM_EMAIL не найден в .env")

    url = "https://api.brevo.com/v3/smtp/email"

    headers = {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json",
    }

    payload = {
        "sender": {
            "email": NOTIFY_FROM_EMAIL,
            "name": NOTIFY_FROM_NAME or "Automation",
        },
        "to": [{"email": NOTIFY_TO}],
        "subject": subject,
        "textContent": text_body[:10000],
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)

    if resp.status_code >= 400:
        raise RuntimeError(f"Brevo API error {resp.status_code}: {resp.text}")


def send_notification(subject: str, body: str) -> None:
    if not NOTIFY_ENABLED:
        print("Уведомления отключены: NOTIFY_ENABLED=false")
        return

    provider = NOTIFY_PROVIDER
    if provider != "brevo":
        print(f"Провайдер уведомлений не поддерживается: {provider!r}")
        return

    send_brevo_email(subject=subject, text_body=body)
    print("Уведомление отправлено")


def build_summary_message(
    changes: List[dict],
    updates_count: int,
    missing_in_wb_count: int,
    missing_in_xml_count: int,
    warehouse_name: str,
    dry_run: bool,
) -> str:
    hidden = [x for x in changes if x["change_type"] == "hidden"]
    decreased = [x for x in changes if x["change_type"] == "decreased"]
    increased = [x for x in changes if x["change_type"] == "increased"]
    unchanged = [x for x in changes if x["change_type"] == "unchanged"]
    zeroed_missing = [x for x in changes if x["change_type"] == "zeroed_missing_in_xml"]

    lines = [
        "Обновление остатков: успешно",
        "",
        f"Склад: {warehouse_name}",
        f"Dry run: {dry_run}",
        f"К обновлению: {updates_count}",
        f"Скрыли из-за малого остатка: {len(hidden)}",
        f"Уменьшили: {len(decreased)}",
        f"Увеличили: {len(increased)}",
        f"Без изменений: {len(unchanged)}",
        f"Обнулили из-за отсутствия в XML: {len(zeroed_missing)}",
        f"Есть в XML, но нет в WB: {missing_in_wb_count}",
        f"Есть в WB, но нет в XML: {missing_in_xml_count}",
        "",
        f"MIN_SAFE_STOCK={MIN_SAFE_STOCK}",
        f"ZERO_MISSING_IN_XML={ZERO_MISSING_IN_XML}",
        f"SUCCESS_POLICY={NOTIFY_SUCCESS_POLICY}",
    ]

    examples = hidden[:5] + zeroed_missing[:5]
    if examples:
        lines.append("")
        lines.append("Примеры:")
        for x in examples:
            if x["change_type"] == "zeroed_missing_in_xml":
                lines.append(f'- {x["vendorCode"]}: нет в XML -> 0')
            else:
                lines.append(f'- {x["vendorCode"]}: {x["raw_stock"]} -> {x["wb_amount"]}')

    return "\n".join(lines)


def build_error_message(error_text: str) -> str:
    return "\n".join(
        [
            "Обновление остатков: ОШИБКА",
            "",
            error_text,
        ]
    )


def success_subject() -> str:
    return f"{NOTIFY_SUBJECT_PREFIX} Успех: update_stocks.py"


def error_subject() -> str:
    return f"{NOTIFY_SUBJECT_PREFIX} Ошибка: update_stocks.py"


# =========================
# MAIN
# =========================
def main():
    print("1. XML...")
    xml_bytes = download_xml_bytes(XML_URL)

    print("2. parse...")
    supplier_stocks = parse_supplier_stocks_from_xml(xml_bytes)

    if not supplier_stocks:
        raise RuntimeError("Из XML не удалось получить пары certificate -> count")

    print("3. cards...")
    cards = get_all_wb_cards()
    print(f"Карточек WB найдено: {len(cards)}")

    wb_map = build_vendorcode_to_chrtid_map(cards)
    print(f"Артикулов WB с chrtId найдено: {len(wb_map)}")

    if not wb_map:
        raise RuntimeError("Не удалось построить мапу vendorCode -> chrtId")

    print("4. warehouses...")
    warehouses = get_wb_warehouses()
    warehouse_id = find_warehouse_id_by_name(warehouses, TARGET_WAREHOUSE_NAME)
    print(f'Найден склад "{TARGET_WAREHOUSE_NAME}" -> warehouseId={warehouse_id}')

    print("5. updates...")
    updates, missing_in_wb, missing_in_xml, changes = build_stock_updates(
        supplier_stocks=supplier_stocks,
        wb_map=wb_map,
        zero_missing_in_xml=ZERO_MISSING_IN_XML,
    )

    print(f"К обновлению подготовлено: {len(updates)}")
    print(f"Есть в XML, но нет в WB: {len(missing_in_wb)}")
    print(f"Есть в WB, но нет в XML: {len(missing_in_xml)}")
    print(f"MIN_SAFE_STOCK={MIN_SAFE_STOCK}")
    print(f"ZERO_MISSING_IN_XML={ZERO_MISSING_IN_XML}")

    if missing_in_wb[:20]:
        print("\nПримеры артикулов, которых нет в WB:")
        for item in missing_in_wb[:20]:
            print(" -", item)

    print_changes_report(changes)

    summary_text = build_summary_message(
        changes=changes,
        updates_count=len(updates),
        missing_in_wb_count=len(missing_in_wb),
        missing_in_xml_count=len(missing_in_xml),
        warehouse_name=TARGET_WAREHOUSE_NAME,
        dry_run=STOCKS_DRY_RUN,
    )

    if not updates:
        print("\nНечего обновлять.")
        if should_send_success_notification():
            send_notification(success_subject(), summary_text + "\n\nНечего обновлять.")
            mark_success_notification_sent()
        return

    if STOCKS_DRY_RUN:
        print("\nSTOCKS_DRY_RUN=True, запись в WB не выполняется.")
        if should_send_success_notification():
            send_notification(success_subject(), summary_text + "\n\nЗапись в WB не выполнялась.")
            mark_success_notification_sent()
        return

    print("\n6. update...")
    update_wb_stocks(warehouse_id, updates)

    print("\nГОТОВО")

    if should_send_success_notification():
        send_notification(success_subject(), summary_text)
        mark_success_notification_sent()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        error_text = str(e)
        print(f"\nERROR: {error_text}")
        try:
            send_notification(error_subject(), build_error_message(error_text))
        except Exception as notify_error:
            print(f"Не удалось отправить уведомление об ошибке: {notify_error}")
        raise