"""Configuration and SKU identity layer."""
import csv
import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKU_MAP_PATH = REPO_ROOT / "sku_map.csv"
IGNORED_SKUS_PATH = REPO_ROOT / "ignored_amazon_skus.csv"
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

    `amazon_sku_aliases` holds the *other* Amazon SKUs that are the same
    physical product. Converting a listing to stickerless (commingled)
    inventory issues a second seller SKU - Silverpot's carry a `-stickerless`
    suffix - and Amazon reports each one's stock separately. Both pools ship
    the same tea, so both count.
    """
    internal_code: str
    product_name: str
    format: str
    sku: str
    asin: str
    walmart_sku_override: str
    website_product_id: str
    safety_buffer: int | None
    amazon_sku_aliases: list[str] = field(default_factory=list)
    active: bool = True

    @property
    def amazon_sku(self) -> str:
        return self.sku

    @property
    def amazon_skus(self) -> list[str]:
        """Every Amazon SKU whose FBA stock belongs to this product.

        Order is primary first, then aliases. Case-insensitively deduplicated,
        because counting one pool twice would overstate what is sellable.
        """
        out: list[str] = []
        seen: set[str] = set()
        for s in [self.sku, *self.amazon_sku_aliases]:
            key = (s or "").strip().upper()
            if key and key not in seen:
                seen.add(key)
                out.append(s.strip())
        return out

    @property
    def walmart_sku(self) -> str:
        return self.walmart_sku_override or self.sku


@dataclass
class SkuMap:
    rows: list[SkuRow] = field(default_factory=list)

    @property
    def by_amazon_sku(self) -> dict[str, SkuRow]:
        """Every Amazon SKU and alias, pointing at the product it belongs to."""
        out: dict[str, SkuRow] = {}
        for r in self.rows:
            for s in r.amazon_skus:
                out[s.upper()] = r
        return out

    @property
    def by_walmart_sku(self) -> dict[str, SkuRow]:
        return {r.walmart_sku.strip().upper(): r for r in self.rows if r.walmart_sku}

    @property
    def by_asin(self) -> dict[str, list[SkuRow]]:
        """ASIN to the products listing under it.

        A list, not a single row: the same ASIN can legitimately carry more
        than one row, and silently keeping the first would make an ambiguous
        answer look definite. This is how an unrecognised Amazon SKU gets
        identified - the SKU string says nothing, the ASIN names the product.
        """
        out: dict[str, list[SkuRow]] = {}
        for r in self.rows:
            if r.asin:
                out.setdefault(r.asin.strip().upper(), []).append(r)
        return out


def _to_bool(v: str) -> bool:
    return str(v).strip().upper() in {"TRUE", "1", "YES", "Y"}


def _split_aliases(raw: str) -> list[str]:
    """Aliases are separated by ; or | so a SKU containing a comma stays safe."""
    parts = [p.strip() for chunk in (raw or "").split(";") for p in chunk.split("|")]
    return [p for p in parts if p]


def load_sku_map(path: Path = SKU_MAP_PATH) -> SkuMap:
    rows: list[SkuRow] = []
    seen_internal: set[str] = set()
    # Every Amazon SKU string may belong to exactly one product. Two rows
    # claiming the same SKU would quietly attribute one tea's stock to another,
    # so it is a hard error rather than a warning.
    claimed_amazon: dict[str, str] = {}
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
            row = SkuRow(
                internal_code=code,
                product_name=(rec.get("product_name") or "").strip(),
                format=(rec.get("format") or "").strip(),
                sku=sku,
                asin=(rec.get("asin") or "").strip(),
                walmart_sku_override=(rec.get("walmart_sku_override") or "").strip(),
                website_product_id=(rec.get("website_product_id") or "").strip(),
                safety_buffer=int(buf) if buf else None,
                amazon_sku_aliases=_split_aliases(rec.get("amazon_sku_aliases", "")),
                active=_to_bool(rec.get("active", "TRUE")),
            )
            for s in row.amazon_skus:
                owner = claimed_amazon.get(s.upper())
                if owner:
                    raise ValueError(
                        f"sku_map.csv line {i}: Amazon SKU '{s}' is already "
                        f"claimed by '{owner}'. One SKU cannot belong to two products."
                    )
                claimed_amazon[s.upper()] = code
            rows.append(row)
    return SkuMap(rows=rows)


def load_ignored_amazon_skus(path: Path = IGNORED_SKUS_PATH) -> dict[str, str]:
    """Amazon SKUs deliberately left unmapped, keyed by SKU to the reason why.

    These are stock the pipeline knowingly does not publish - retired parent
    SKUs and the like. Recording the decision in a file, with a reason and a
    date, is the point: it is reversible, reviewable, and it stops the run
    from re-asking a question that has already been answered.

    An ignored SKU is still reported, quietly, with its quantity. Ignoring is
    not the same as not looking, and a retired SKU that suddenly gains stock
    is worth seeing.
    """
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            sku = (rec.get("amazon_sku") or "").strip()
            if sku:
                out[sku.upper()] = (rec.get("reason") or "").strip()
    return out


def require_env(*names: str) -> dict[str, str]:
    missing = [n for n in names if not os.getenv(n)]
    if missing:
        raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")
    return {n: os.environ[n] for n in names}
