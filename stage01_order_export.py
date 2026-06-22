"""
Stage 01 – Order Data Export
=============================
Fetches today's unshipped orders from the CA / Rithum REST API and saves them
to downloads/rithum_orders.csv.

This replaces the manual "Scheduled Export for early shipping" email workflow
described in the process guide. Instead of waiting for a Rithum-generated CSV
attachment, we call the ChannelAdvisor Orders API directly so the data is always
fresh and the schedule can be controlled independently.

Steps in this stage
-------------------
Step 1.1  Authenticate with the CA REST API
Step 1.2  Obtain access token
Step 1.3  Fetch all matching orders for today (paginated)
Step 1.4  Save orders to downloads/rithum_orders.csv

Required .env keys
------------------
CA_APPLICATION_ID   – ChannelAdvisor OAuth application ID
CA_SHARED_SECRET    – ChannelAdvisor OAuth shared secret
CA_REFRESH_TOKEN    – ChannelAdvisor OAuth refresh token
CA_PROFILE_ID       – ChannelAdvisor profile (account) ID

Optional .env keys
------------------
CA_ORDER_FILTER     – Override the default OData $filter expression
DEBUG               – Set to 1 to enable verbose logging

Exit codes
----------
0   – Orders fetched and saved successfully
10  – No orders matched today's filter (nothing to process downstream)
1   – Any other error
"""

import csv
import datetime
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import dotenv_values, load_dotenv

DOTENV_PATH = Path(__file__).resolve().with_name(".env")
OUTPUT_PATH = Path("downloads/rithum_orders.csv").resolve()

NO_ORDERS_EXIT_CODE = 10

_CA_TOKEN_URL = "https://api.channeladvisor.com/oauth2/token"
_CA_ORDERS_URL = "https://api.channeladvisor.com/v1/Orders"

_DEFAULT_ORDER_FILTER = (
    "ShippingStatus eq 'Unshipped'"
    " and (SiteName eq 'Amazon UK'"
    " or SiteName eq 'eBay Fixed Price UK'"
    " or SiteName eq 'Temu UK')"
    " and (RequestedShippingClass eq null"
    " or (RequestedShippingClass ne 'NextDay'"
    " and RequestedShippingClass ne 'SecondDay'"
    " and RequestedShippingClass ne 'Prime NextDay'"
    " and RequestedShippingClass ne 'Prime SecondDay'"
    " and RequestedShippingClass ne 'Prime Standard'"
    " and RequestedShippingClass ne 'Premium NextDay'"
    " and RequestedShippingClass ne 'Premium SecondDay'))"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y"}


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _dotenv_or_env(dotenv_map: dict[str, Any], name: str) -> str | None:
    value = dotenv_map.get(name) or os.getenv(name)
    return str(value) if value is not None else None


def _log(message: str) -> None:
    print(f"[DONE] {message}")


def _log_info(debug: bool, message: str) -> None:
    if debug:
        print(f"[INFO] {message}")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    ca_application_id: str
    ca_shared_secret: str
    ca_refresh_token: str
    ca_profile_id: str
    ca_order_filter: str
    output_path: Path
    debug: bool

    @staticmethod
    def load(dotenv_path: Path = DOTENV_PATH) -> "Config":
        load_dotenv(dotenv_path=dotenv_path, override=True, encoding="utf-8-sig")
        dotenv_map = dotenv_values(dotenv_path, encoding="utf-8-sig")

        return Config(
            ca_application_id=_require_env("CA_APPLICATION_ID").strip(),
            ca_shared_secret=_require_env("CA_SHARED_SECRET").strip(),
            ca_refresh_token=_require_env("CA_REFRESH_TOKEN").strip(),
            ca_profile_id=_require_env("CA_PROFILE_ID").strip(),
            ca_order_filter=(
                _dotenv_or_env(dotenv_map, "CA_ORDER_FILTER") or _DEFAULT_ORDER_FILTER
            ).strip(),
            output_path=OUTPUT_PATH,
            debug=_env_flag("DEBUG", default=False),
        )


# ---------------------------------------------------------------------------
# CA / Rithum API
# ---------------------------------------------------------------------------


def _get_access_token(config: Config) -> str:
    response = requests.post(
        _CA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": config.ca_application_id,
            "client_secret": config.ca_shared_secret,
            "refresh_token": config.ca_refresh_token,
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(
            f"CA API authentication failed ({response.status_code}): {response.text[:500]}"
        )
    token = response.json().get("access_token")
    if not token:
        raise RuntimeError(
            "CA API returned a token response without an access_token field."
        )
    return token


def _fetch_orders_page(
    access_token: str,
    profile_id: str,
    filter_expr: str,
    skip: int,
    top: int = 100,
) -> list[dict[str, Any]]:
    response = requests.get(
        _CA_ORDERS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "$filter": filter_expr,
            "$top": top,
            "$skip": skip,
            "profileid": profile_id,
        },
        timeout=60,
    )
    if not response.ok:
        raise RuntimeError(
            f"CA API orders request failed ({response.status_code}): {response.text[:500]}"
        )
    return response.json().get("value", [])


def _utc_to_display_datetime(value: Any) -> str:
    """Convert a CA API UTC timestamp to 'DD/MM/YYYY HH:MM' (Basic Layout format)."""
    if not value:
        return ""
    text = str(value).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(text, fmt)
            return dt.strftime("%d/%m/%Y %H:%M")
        except ValueError:
            continue
    return text


def _order_to_row(order: dict[str, Any]) -> dict[str, Any]:
    """Map a CA API order object to a flat CSV row using Basic Layout column names."""
    order_id = str(order.get("SiteOrderID") or "").strip()
    row: dict[str, Any] = {
        "Site Order ID": order_id,
        "Site Name": order.get("SiteName"),
        "Buyer": order.get("BuyerEmailAddress"),
        "Order Date": _utc_to_display_datetime(order.get("CheckoutDateUtc")),
        "Estimated Ship Date": _utc_to_display_datetime(
            order.get("EstimatedShipDateUtc")
        ),
        "Shipping Status": order.get("ShippingStatus"),
        "Order Total": order.get("TotalPrice"),
        "Payment Type": order.get("PaymentStatus"),
        "Shipping First Name": order.get("ShippingFirstName"),
        "Shipping Last Name": order.get("ShippingLastName"),
        "Shipping Company Name": order.get("ShippingTitle"),
        "Shipping Address Line 1": order.get("ShippingAddressLine1"),
        "Shipping Address Line 2": order.get("ShippingAddressLine2"),
        "Shipping City": order.get("ShippingCity"),
        "Shipping State or Province": order.get("ShippingStateOrProvince"),
        "Shipping Postal Code": order.get("ShippingPostalCode"),
        "ShippingCountry": order.get("ShippingCountry"),
        "Shipping Day Phone": order.get("ShippingPhoneNumber"),
        "RequestedShippingCarrier": order.get("RequestedShippingCarrier"),
        "RequestedShippingClass": order.get("RequestedShippingClass"),
        "SecondarySiteOrderID": order.get("SecondarySiteOrderID"),
    }
    return {k: v for k, v in row.items() if v is not None and v != ""}


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(dict.fromkeys(key for row in rows for key in row.keys()))
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run(config: Config) -> int:
    _log_info(config.debug, f"Loaded .env from: {DOTENV_PATH}")
    _log_info(config.debug, f"Output path: {config.output_path}")

    _log("Stage 1 / Step 1.1: Authenticating with CA REST API")
    access_token = _get_access_token(config)
    _log("Stage 1 / Step 1.2: CA API access token obtained")

    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    filter_expr = (
        config.ca_order_filter
        + f" and EstimatedShipDateUtc ge {today.isoformat()}T00:00:00Z"
        + f" and EstimatedShipDateUtc lt {tomorrow.isoformat()}T00:00:00Z"
    )
    _log_info(config.debug, f"CA API order filter: {filter_expr}")

    all_rows: list[dict[str, Any]] = []
    page_size = 100
    skip = 0

    while True:
        page = _fetch_orders_page(
            access_token,
            config.ca_profile_id,
            filter_expr,
            skip,
            page_size,
        )
        if not page:
            break
        all_rows.extend(_order_to_row(order) for order in page)
        _log_info(
            config.debug,
            f"CA API: fetched {len(all_rows)} orders so far (page size {len(page)})",
        )
        skip += len(page)
        if len(page) < page_size:
            break

    _log(f"Stage 1 / Step 1.3: Fetched {len(all_rows)} orders from CA REST API for {today}")

    if not all_rows:
        print(
            "[WARN] No CA/Rithum orders matched today's filter. "
            "Nothing to process downstream."
        )
        return NO_ORDERS_EXIT_CODE

    _write_csv(config.output_path, all_rows)
    _log(f"Stage 1 / Step 1.4: Saved {len(all_rows)} orders to {config.output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(run(Config.load()))
