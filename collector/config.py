"""Configuration and SKU identity layer."""
import csv
import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKU_MAP_PATH = REPO_ROOT / "sku_map.csv"
PUBLIC_DIR = REPO_ROOT / "public"

US_MARKETPLACE_ID = "ATVPDKIKX0DER"
SPAPI_HOST = "https://sellingpartnerapi-na.amazon.com"
LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
WALMART_HOST = "https://marketplace.walmartapis.com"

# Availability policy. Only these two pools are sellable through MCF.
# Everything else Amazon reports (inbound, reserved, unfulfillable, researching)
# is recorded for forecasting but NEVER published to the website.
DEFAULT_SAFETY_BUFFER = int(os.getenv("DEFAULT_SAFETY_BUFFER", "2"))
BUFFER_PERCENT = float(os.getenv("BUFFER_PERCENT", "0.05"))

SHEET_NAME = os.getenv("SHEET_NAME", "Silverpot Inventory")
TAB_SNAPSHOTS = "snapshots"
TAB_CURRENT = "current"


@dataclass
class SkuRow:
    """One physical product, addressed by its marketplace SKU.

    Amazon and Walmart currently use the same SKU string, so `sku` serves both.
    `walmart_sku_override` exists for the day that stops being true - most
    likely when the FNSKU-to-manufacturer-barcode conversion issues new Amazon
    SKUs while the Walmart listings keep the old ones.
    """
    internal_code: str
    product_name: str
    sku: str
    asin: str
    walmart_sku_override: str
    website_product_id: str
    safety_buffer: int | None
    active: bool = True

    @property
    def amazon_sku(self) -> str:
        return self.sku

    @property
    def walmart_sku(self) -> str:
        return self.walmart_sku_override or self.sku


@dataclass
class SkuMap:
    rows: list[SkuRow] = field(default_factory=list)

    @property
    def by_amazon_sku(self) -> dict[str, SkuRow]:
        return {r.amazon_sku.strip().upper(): r for r in self.rows if r.amazon_sku}

    @property
    def by_walmart_sku(self) -> dict[str, SkuRow]:
        return {r.walmart_sku.strip().upper(): r for r in self.rows if r.walmart_sku}


def _to_bool(v: str) -> bool:
    return str(v).strip().upper() in {"TRUE", "1", "YES", "Y"}


def load_sku_map(path: Path = SKU_MAP_PATH) -> SkuMap:
    rows: list[SkuRow] = []
    seen_internal: set[str] = set()
    with open(path, newline="", encoding="utf-8") as fh:
        for i, rec in enumerate(csv.DictReader(fh), start=2):
            code = (rec.get("internal_code") or "").strip()
            if not code:
                raise ValueError(f"sku_map.csv line {i}: internal_code is blank. It is the join key.")
            if code in seen_internal:
                raise ValueError(f"sku_map.csv line {i}: duplicate internal_code '{code}'.")
            seen_internal.add(code)
            sku = (rec.get("sku") or "").strip()
            if not sku:
                raise ValueError(f"sku_map.csv line {i}: sku is blank for '{code}'.")
            buf = (rec.get("safety_buffer") or "").strip()
            rows.append(
                SkuRow(
                    internal_code=code,
                    product_name=(rec.get("product_name") or "").strip(),
                    sku=sku,
                    asin=(rec.get("asin") or "").strip(),
                    walmart_sku_override=(rec.get("walmart_sku_override") or "").strip(),
                    website_product_id=(rec.get("website_product_id") or "").strip(),
                    safety_buffer=int(buf) if buf else None,
                    active=_to_bool(rec.get("active", "TRUE")),
                )
            )
    return SkuMap(rows=rows)


def require_env(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
    return {n: os.environ[n] for n in names}
