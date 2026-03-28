from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import requests
from dotenv import load_dotenv


# =========================================================
# PATHS
# =========================================================
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = PROJECT_ROOT / ".env"
DATA_INPUT_DIR = PROJECT_ROOT / "data" / "input"
DATA_OUTPUT_DIR = PROJECT_ROOT / "data" / "output"

load_dotenv(ENV_PATH)

DATA_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# =========================================================
# CONFIG
# =========================================================
TOKEN = os.getenv("WB_TOKEN")
SUBJECT_ID = int(os.getenv("WB_SUBJECT_ID", "197"))

CARDS_EXCEL_FILE = os.getenv("CARDS_EXCEL_FILE", "new_cards.xlsx")
DRY_RUN = os.getenv("CARDS_DRY_RUN", "true").strip().lower() == "true"
PROCESS_ALL_ROWS = os.getenv("CARDS_PROCESS_ALL_ROWS", "true").strip().lower() == "true"
BATCH_SIZE = int(os.getenv("CARDS_BATCH_SIZE", "100"))
SLEEP_BETWEEN_BATCHES = float(os.getenv("CARDS_SLEEP_BETWEEN_BATCHES", "1.0"))

if not TOKEN:
    raise ValueError("WB_TOKEN не найден в .env")


# =========================================================
# API
# =========================================================
UPLOAD_URL = "https://content-api.wildberries.ru/content/v2/cards/upload"


# =========================================================
# МАППИНГ ХАРАКТЕРИСТИК ДЛЯ subjectID=197
# =========================================================
CHARCS: Dict[str, int] = {
    "Рисунок": 12,
    "Любимые герои": 51,
    "Размер постельного белья": 973,
    "Вид ткани постельного белья": 980,
    "Размер наволочки": 993,
    "Уход за вещами": 11892,
    "Размер пододеяльника": 17387,
    "Размер простыни": 17394,
    "Назначение подарка": 59611,
    "Повод": 59615,
    "Назначение текстиля": 60898,
    "Тип простыни": 72790,
    "Упаковка": 85571,
    "Плотность ткани": 89016,
    "Количество предметов в упаковке": 179792,
    "Комплектация": 378533,
    "Цвет": 14177449,
    "Состав": 14177450,
    "Страна производства": 14177451,
    "ТНВЭД": 15000001,
    "Номер декларации соответствия": 15001135,
    "Номер сертификата соответствия": 15001136,
    "Дата регистрации сертификата/декларации": 15001137,
    "Дата окончания действия сертификата/декларации": 15001138,
    "Ставка НДС": 15001405,
    "ИКПУ": 15001650,
    "Код упаковки": 15001706,
    "Комплектность постельного белья": 15001828,
    "Застежка наволочки": 15001831,
    "Застежка пододеяльника": 15001832,
    "Артикул OZON": 15003293,
}

COLUMN_RENAME = {
    "Затежка наволочки": "Застежка наволочки",
    "Номер сертификата": "Номер сертификата соответствия",
    "Действует от": "Дата регистрации сертификата/декларации",
    "Действует до": "Дата окончания действия сертификата/декларации",
}

MULTI_VALUE_FIELDS = {
    "Цвет",
    "Состав",
    "Комплектация",
    "Уход за вещами",
    "Назначение подарка",
    "Повод",
    "Размер простыни",
    "Размер пододеяльника",
    "Размер наволочки",
    "Застежка наволочки",
    "Застежка пододеяльника",
}

NUMERIC_FIELDS = {
    "Плотность ткани",
}

IGNORED_EXCEL_COLUMNS = {
    "Категория продавца",
    "Цена",
    "Нужна маркировка КИЗ",
    "Подтверждаю, что есть маркировка",
    "Сертификат соответствия",
    "Баркод",
    "Штрихкод",
    "SKU",
    "skus",
    "Skus",
    "Размер SKU",
    "WB SKU",
}


# =========================================================
# HELPERS
# =========================================================
def is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def clean_str(value: Any) -> str:
    if is_empty(value):
        return ""
    return str(value).strip()


def split_multi_value(value: Any, separators: Optional[List[str]] = None) -> List[str]:
    if is_empty(value):
        return []

    text = str(value).strip()
    parts = [text]

    for sep in (separators or [",", ";", "|"]):
        tmp = []
        for part in parts:
            tmp.extend(part.split(sep))
        parts = tmp

    result = []
    seen = set()

    for part in parts:
        item = part.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)

    return result


def normalize_number(value: Any) -> Optional[int | float]:
    if is_empty(value):
        return None

    try:
        text = str(value).replace(",", ".").strip()
        num = float(text)
        if num.is_integer():
            return int(num)
        return num
    except Exception:
        return None


def excel_date_to_str(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, pd.Timestamp):
        return value.strftime("%d.%m.%Y")

    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            base = datetime(1899, 12, 30)
            dt = base + timedelta(days=float(value))
            return dt.strftime("%d.%m.%Y")
        except Exception:
            pass

    text = str(value).strip()
    if not text:
        return ""

    for dayfirst in (True, False):
        try:
            dt = pd.to_datetime(text, errors="raise", dayfirst=dayfirst)
            return dt.strftime("%d.%m.%Y")
        except Exception:
            pass

    return text


def chunked(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


# =========================================================
# CHARACTERISTICS
# =========================================================
def build_characteristic(field_name: str, value: Any) -> Optional[Dict[str, Any]]:
    if field_name not in CHARCS:
        return None

    if is_empty(value):
        return None

    charc_id = CHARCS[field_name]

    if field_name in {
        "Дата регистрации сертификата/декларации",
        "Дата окончания действия сертификата/декларации",
    }:
        prepared = excel_date_to_str(value)
        if not prepared:
            return None
        return {"id": charc_id, "value": [prepared]}

    if field_name == "ТНВЭД":
        prepared = clean_str(value)
        prepared = "".join(ch for ch in prepared if ch.isdigit())
        if len(prepared) != 10:
            return None
        return {"id": charc_id, "value": [prepared]}

    if field_name == "Количество предметов в упаковке":
        prepared_num = normalize_number(value)
        if prepared_num is None:
            return None
        if isinstance(prepared_num, float) and not prepared_num.is_integer():
            return None
        return {"id": charc_id, "value": [str(int(prepared_num))]}

    if field_name in NUMERIC_FIELDS:
        prepared_num = normalize_number(value)
        if prepared_num is None:
            return None
        return {"id": charc_id, "value": prepared_num}

    if field_name in MULTI_VALUE_FIELDS:
        prepared_list = split_multi_value(value)
        if not prepared_list:
            return None
        return {"id": charc_id, "value": prepared_list}

    prepared = clean_str(value)
    if not prepared:
        return None

    return {"id": charc_id, "value": [prepared]}


def build_dimensions(row: pd.Series) -> Optional[Dict[str, Any]]:
    length = normalize_number(row.get("Длина"))
    width = normalize_number(row.get("Ширина"))
    height = normalize_number(row.get("Высота"))
    weight = normalize_number(row.get("Вес"))

    if all(v is None for v in [length, width, height, weight]):
        return None

    dims: Dict[str, Any] = {}

    if length is not None:
        dims["length"] = length
    if width is not None:
        dims["width"] = width
    if height is not None:
        dims["height"] = height
    if weight is not None:
        dims["weightBrutto"] = weight

    return dims if dims else None


def build_variant(row: pd.Series) -> Dict[str, Any]:
    vendor_code = clean_str(row.get("Артикул продавца"))
    title = clean_str(row.get("Наименование"))
    description = clean_str(row.get("Описание"))
    brand = clean_str(row.get("Бренд"))

    if not vendor_code:
        raise ValueError("Пустой 'Артикул продавца'")
    if not title:
        raise ValueError("Пустое 'Наименование'")

    normalized_row: Dict[str, Any] = {}
    for col in row.index:
        new_col = COLUMN_RENAME.get(col, col)
        normalized_row[new_col] = row[col]

    characteristics: List[Dict[str, Any]] = []
    for field_name in CHARCS.keys():
        if field_name in normalized_row:
            char_obj = build_characteristic(field_name, normalized_row[field_name])
            if char_obj:
                characteristics.append(char_obj)

    variant: Dict[str, Any] = {
        "vendorCode": vendor_code,
        "title": title[:60],
        "description": description,
        "brand": brand,
        "characteristics": characteristics,
    }

    dims = build_dimensions(row)
    if dims:
        variant["dimensions"] = dims

    variant = {
        k: v for k, v in variant.items()
        if not (isinstance(v, str) and v == "")
    }

    return variant


def build_card(row: pd.Series) -> Dict[str, Any]:
    return {
        "subjectID": SUBJECT_ID,
        "variants": [build_variant(row)],
    }


# =========================================================
# API
# =========================================================
def upload_batch(batch: List[Dict[str, Any]]) -> Dict[str, Any]:
    headers = {
        "Authorization": TOKEN,
        "Content-Type": "application/json",
    }

    response = requests.post(UPLOAD_URL, headers=headers, json=batch, timeout=120)

    result: Dict[str, Any] = {
        "status_code": response.status_code,
        "text": response.text,
    }

    try:
        result["json"] = response.json()
    except Exception:
        result["json"] = None

    return result


# =========================================================
# MAIN
# =========================================================
def main() -> None:
    excel_path = DATA_INPUT_DIR / CARDS_EXCEL_FILE
    payload_path = DATA_OUTPUT_DIR / "payload_cards.json"
    preview_path = DATA_OUTPUT_DIR / "payload_preview.xlsx"
    upload_log_path = DATA_OUTPUT_DIR / "upload_results.json"
    build_errors_path = DATA_OUTPUT_DIR / "build_errors.xlsx"

    if not excel_path.exists():
        raise FileNotFoundError(f"Не найден файл: {excel_path}")

    df = pd.read_excel(excel_path, dtype=object)
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all").reset_index(drop=True)

    if df.empty:
        raise ValueError("В Excel нет данных")

    if not PROCESS_ALL_ROWS:
        df = df.head(1)

    payload: List[Dict[str, Any]] = []
    preview_rows: List[Dict[str, Any]] = []
    build_errors: List[Dict[str, Any]] = []

    for idx, row in df.iterrows():
        try:
            card = build_card(row)
            payload.append(card)

            variant = card["variants"][0]
            preview_rows.append(
                {
                    "excel_row": idx + 2,
                    "vendorCode": variant.get("vendorCode"),
                    "title": variant.get("title"),
                    "brand": variant.get("brand"),
                    "subjectID": card.get("subjectID"),
                    "characteristics_count": len(variant.get("characteristics", [])),
                    "has_dimensions": "dimensions" in variant,
                }
            )
        except Exception as e:
            build_errors.append(
                {
                    "excel_row": idx + 2,
                    "vendorCode": clean_str(row.get("Артикул продавца")),
                    "error": str(e),
                }
            )

    with open(payload_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    pd.DataFrame(preview_rows).to_excel(preview_path, index=False)

    if build_errors:
        pd.DataFrame(build_errors).to_excel(build_errors_path, index=False)

    print(f"Excel: {excel_path}")
    print(f"Карточек собрано: {len(payload)}")
    print(f"Ошибок сборки: {len(build_errors)}")
    print(f"Payload: {payload_path}")
    print(f"Preview: {preview_path}")
    if build_errors:
        print(f"Ошибки: {build_errors_path}")

    if not payload:
        print("Нет карточек для отправки")
        return

    print("\nПервая собранная карточка:\n")
    print(json.dumps(payload[0], ensure_ascii=False, indent=2))

    if DRY_RUN:
        print("\nCARDS_DRY_RUN=true -> в WB ничего не отправлялось")
        return

    batches = chunked(payload, BATCH_SIZE)
    upload_results: List[Dict[str, Any]] = []

    print(f"\nНачинаю отправку. Batch count: {len(batches)}")

    for i, batch in enumerate(batches, start=1):
        print(f"\nОтправка batch {i}/{len(batches)} | карточек: {len(batch)}")

        result = upload_batch(batch)
        upload_results.append(
            {
                "batch_number": i,
                "cards_count": len(batch),
                **result,
            }
        )

        with open(upload_log_path, "w", encoding="utf-8") as f:
            json.dump(upload_results, f, ensure_ascii=False, indent=2)

        print(f"HTTP: {result['status_code']}")
        if result["json"] is not None:
            print(json.dumps(result["json"], ensure_ascii=False, indent=2))
        else:
            print(result["text"])

        time.sleep(SLEEP_BETWEEN_BATCHES)

    print(f"\nГотово. Лог отправки: {upload_log_path}")


if __name__ == "__main__":
    main()