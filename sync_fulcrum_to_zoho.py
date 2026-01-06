#!/usr/bin/env python3
"""Sync item information from Fulcrum Pro to Zoho CRM."""

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_FULCRUM_ITEMS_ENDPOINT = "/items"
DEFAULT_ZOHO_MODULE = "Products"
ZOHO_BATCH_LIMIT = 100


@dataclass
class SyncConfig:
    fulcrum_base_url: str
    fulcrum_token: str
    fulcrum_items_endpoint: str
    zoho_base_url: str
    zoho_token: str
    zoho_module: str
    zoho_upsert_key: Optional[str]
    field_map: Dict[str, str]
    request_timeout: float
    dry_run: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync item information from Fulcrum Pro to Zoho CRM."
    )
    parser.add_argument(
        "--field-map",
        help="Path to JSON file mapping Fulcrum fields to Zoho fields.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print payloads without sending to Zoho.",
    )
    return parser.parse_args()


def load_field_map(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def get_env(name: str, required: bool = False, default: Optional[str] = None) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value or ""


def build_config(args: argparse.Namespace) -> SyncConfig:
    fulcrum_base_url = get_env("FULCRUM_BASE_URL", required=True)
    fulcrum_token = get_env("FULCRUM_API_TOKEN", required=True)
    fulcrum_items_endpoint = get_env(
        "FULCRUM_ITEMS_ENDPOINT", default=DEFAULT_FULCRUM_ITEMS_ENDPOINT
    )
    zoho_base_url = get_env("ZOHO_BASE_URL", required=True)
    zoho_token = get_env("ZOHO_ACCESS_TOKEN", required=True)
    zoho_module = get_env("ZOHO_MODULE", default=DEFAULT_ZOHO_MODULE)
    zoho_upsert_key = os.getenv("ZOHO_UPSERT_KEY")
    field_map = load_field_map(args.field_map)
    request_timeout = float(get_env("REQUEST_TIMEOUT", default="30"))
    return SyncConfig(
        fulcrum_base_url=fulcrum_base_url.rstrip("/"),
        fulcrum_token=fulcrum_token,
        fulcrum_items_endpoint=fulcrum_items_endpoint,
        zoho_base_url=zoho_base_url.rstrip("/"),
        zoho_token=zoho_token,
        zoho_module=zoho_module,
        zoho_upsert_key=zoho_upsert_key,
        field_map=field_map,
        request_timeout=request_timeout,
        dry_run=args.dry_run,
    )


def request_json(
    method: str,
    url: str,
    headers: Dict[str, str],
    payload: Any = None,
    timeout: float = 30,
) -> Any:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers = {**headers, "Content-Type": "application/json"}
    request = Request(url, method=method, headers=headers, data=data)
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else None


def fetch_fulcrum_items(config: SyncConfig) -> List[Dict[str, Any]]:
    url = f"{config.fulcrum_base_url}{config.fulcrum_items_endpoint}"
    headers = {
        "Authorization": f"Bearer {config.fulcrum_token}",
        "Accept": "application/json",
    }
    items: List[Dict[str, Any]] = []
    while url:
        response = request_json("GET", url, headers=headers, timeout=config.request_timeout)
        if isinstance(response, dict) and "items" in response:
            items.extend(response["items"])
            next_page = response.get("next_page")
            if next_page:
                if next_page.startswith("http"):
                    url = next_page
                else:
                    url = f"{config.fulcrum_base_url}{next_page}"
            else:
                url = ""
        elif isinstance(response, list):
            items.extend(response)
            url = ""
        else:
            raise ValueError(
                "Unexpected Fulcrum response format; expected list or {items: []}."
            )
    return items


def map_item_to_zoho(item: Dict[str, Any], field_map: Dict[str, str]) -> Dict[str, Any]:
    if not field_map:
        return item
    mapped: Dict[str, Any] = {}
    for fulcrum_field, zoho_field in field_map.items():
        mapped[zoho_field] = item.get(fulcrum_field)
    return mapped


def chunk_records(records: List[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    for index in range(0, len(records), size):
        yield records[index : index + size]


def build_zoho_payload(
    records: List[Dict[str, Any]],
    upsert_key: Optional[str],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"data": records}
    if upsert_key:
        payload["duplicate_check_fields"] = [upsert_key]
    return payload


def send_to_zoho(config: SyncConfig, records: List[Dict[str, Any]]) -> None:
    url = f"{config.zoho_base_url}/crm/v2/{config.zoho_module}/upsert"
    headers = {
        "Authorization": f"Zoho-oauthtoken {config.zoho_token}",
        "Accept": "application/json",
    }
    for batch in chunk_records(records, ZOHO_BATCH_LIMIT):
        payload = build_zoho_payload(batch, config.zoho_upsert_key)
        if config.dry_run:
            print(json.dumps(payload, indent=2))
            continue
        request_json(
            "POST",
            url,
            headers=headers,
            payload=payload,
            timeout=config.request_timeout,
        )
        time.sleep(0.2)


def format_http_error(exc: HTTPError) -> str:
    body = ""
    if exc.fp is not None:
        try:
            body = exc.read().decode("utf-8")
        except UnicodeDecodeError:
            body = exc.read().decode("latin-1")
    details = f": {body}" if body else ""
    return f"HTTP {exc.code} {exc.reason}{details}"


def main() -> int:
    args = parse_args()
    try:
        config = build_config(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        fulcrum_items = fetch_fulcrum_items(config)
        zoho_records = [map_item_to_zoho(item, config.field_map) for item in fulcrum_items]
        if not zoho_records:
            print("No Fulcrum items to sync.")
            return 0
        send_to_zoho(config, zoho_records)
    except HTTPError as exc:
        print(f"Sync failed: {format_http_error(exc)}", file=sys.stderr)
        return 1
    except (URLError, ValueError) as exc:
        print(f"Sync failed: {exc}", file=sys.stderr)
        return 1

    print(f"Synced {len(zoho_records)} records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
