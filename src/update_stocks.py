import gzip
import json
import os
import time
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Dict, List, Tuple

import requests
from dotenv import load_dotenv


# =========================
# PATHS
# =========================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(PROJECT_ROOT, ".env")

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

if not WB_TOKEN:
    raise ValueError("WB_TOKEN не найден в .env")


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
def http_get(url, headers=None, timeout=120):
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp


def http_post(url, json_payload=None, headers=None, timeout=120):
    resp = requests.post(url, json=json_payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp


def http_put(url, json_payload=None, headers=None, timeout=120):
    resp = requests.put(url, json=json_payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp


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

    if not updates:
        print("\nНечего обновлять.")
        return

    if STOCKS_DRY_RUN:
        print("\nSTOCKS_DRY_RUN=True, запись в WB не выполняется.")
        return

    print("\n6. update...")
    update_wb_stocks(warehouse_id, updates)

    print("\nГОТОВО")


if __name__ == "__main__":
    main()