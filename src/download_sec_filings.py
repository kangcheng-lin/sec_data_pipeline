import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd
import requests
from dotenv import load_dotenv


# -----------------------------
# CONFIG
# -----------------------------
load_dotenv()

USER_AGENT = os.getenv("SEC_USER_AGENT")

if not USER_AGENT:
    raise ValueError(
        "Missing SEC_USER_AGENT. Please set it in your local .env file."
    )
BASE_SEC = "https://www.sec.gov"
BASE_DATA = "https://data.sec.gov"

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

# The script is intended to live inside src/.
# Project root is therefore one level above this file.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "sec_filings"

NASDAQ_SYMBOLS_CSV = DATA_DIR / "nasdaq-listed-symbols.csv"
OTHER_LISTED_CSV = DATA_DIR / "other-listed.csv"

# Short/reference outputs from the ticker-list scripts. These are used only
# to produce a human-readable final universe file.
NASDAQ_LISTED_CSV = DATA_DIR / "nasdaq-listed.csv"
NYSE_LISTED_CSV = DATA_DIR / "nyse-listed.csv"
FINAL_UNIVERSE_CSV = DATA_DIR / "sec_download_universe_final.csv"
RAW_UNIVERSE_CSV = DATA_DIR / "sec_download_universe_raw.csv"
UNIVERSE_SNAPSHOT_DIR = DATA_DIR / "universe_snapshots"
CACHE_MANIFEST_CSV = OUTPUT_DIR / "download_cache_manifest.csv"

# Conservative first-pass universe filters.
EXCLUDE_SECURITY_NAME_PATTERNS = [
    r"\bright(s)?\b",
    r"\bwarrant(s)?\b",
    r"\bunit(s)?\b",
    r"\bpreferred\b",
    r"\bpreference\b",
    r"\bdepositary\b",
    r"\bdepository\b",
    r"\bnote(s)?\b",
    r"\bbond(s)?\b",
    r"\betf\b",
    r"\bfund\b",
    r"\btrust\b",
    r"\bnextshares\b",
    r"\badr\b",
    r"\bads\b",
    r"\bacquisition\s+corp.*\bunit(s)?\b",
]


# -----------------------------
# BASIC HELPERS
# -----------------------------
def sanitize_filename_part(value: str | None, default: str = "unknown") -> str:
    """Make SEC document names safe for Windows/macOS/Linux paths."""
    if not value:
        value = default
    value = str(value)
    value = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or default


def normalize_ticker_for_sec(ticker: str) -> str:
    """Normalize exchange symbols to the SEC company_tickers.json convention."""
    ticker = str(ticker).strip().upper()
    # SEC commonly uses '-' for share classes, while market data files may use '.'.
    ticker = ticker.replace(".", "-")
    return ticker


def parse_year(date_text: str | None) -> int | None:
    """Return YYYY from SEC date string like '2024-09-28', otherwise None."""
    if not date_text or len(str(date_text)) < 4:
        return None
    try:
        return int(str(date_text)[:4])
    except ValueError:
        return None


def date_in_year_range(
    filing: dict,
    start_year: int | None = None,
    end_year: int | None = None,
    date_basis: str = "report_date",
) -> bool:
    """Filter filing by inclusive year range.

    date_basis options:
      - 'report_date': fiscal/report period date; best for financial statements
      - 'filing_date': SEC filing date
      - 'either': keep if either report_date or filing_date falls in range
    """
    if date_basis not in {"report_date", "filing_date", "either"}:
        raise ValueError("date_basis must be 'report_date', 'filing_date', or 'either'")

    def ok(date_value: str | None) -> bool:
        year = parse_year(date_value)
        if year is None:
            return False
        if start_year is not None and year < start_year:
            return False
        if end_year is not None and year > end_year:
            return False
        return True

    if date_basis == "either":
        return ok(filing.get("report_date")) or ok(filing.get("filing_date"))
    return ok(filing.get(date_basis))


def get_ticker_to_cik_map() -> dict:
    url = f"{BASE_SEC}/files/company_tickers.json"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    time.sleep(0.15)

    raw = resp.json()
    out = {}
    for _, row in raw.items():
        ticker = str(row["ticker"]).upper()
        cik = str(row["cik_str"]).zfill(10)
        out[ticker] = {
            "cik": cik,
            "title": row["title"],
        }
    return out


def ticker_to_cik(ticker: str, ticker_map: dict | None = None) -> tuple[str, str]:
    ticker = normalize_ticker_for_sec(ticker)
    mapping = ticker_map if ticker_map is not None else get_ticker_to_cik_map()

    if ticker not in mapping:
        raise ValueError(f"Ticker not found in SEC ticker-CIK map: {ticker}")

    return mapping[ticker]["cik"], mapping[ticker]["title"]


def get_json(url: str) -> dict:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    time.sleep(0.15)
    return resp.json()


def get_company_submissions(cik: str) -> dict:
    cik = cik.zfill(10)
    url = f"{BASE_DATA}/submissions/CIK{cik}.json"
    return get_json(url)


def columnar_filings_to_records(filings_dict: dict, forms=("10-K", "10-Q")) -> list[dict]:
    records = []
    accession_numbers = filings_dict.get("accessionNumber", [])
    n = len(accession_numbers)

    for i in range(n):
        form = filings_dict.get("form", [None] * n)[i]
        if form not in forms:
            continue

        accession_number = filings_dict["accessionNumber"][i]
        records.append({
            "accession_number": accession_number,
            "accession_no_dash": accession_number.replace("-", ""),
            "filing_date": filings_dict.get("filingDate", [None] * n)[i],
            "report_date": filings_dict.get("reportDate", [None] * n)[i],
            "form": form,
            "primary_document": filings_dict.get("primaryDocument", [None] * n)[i],
            "primary_doc_description": filings_dict.get("primaryDocDescription", [None] * n)[i],
        })

    return records


def get_all_company_filings(cik: str, forms=("10-K", "10-Q"), include_historical: bool = True) -> list[dict]:
    submissions = get_company_submissions(cik)
    all_records = []

    recent = submissions.get("filings", {}).get("recent", {})
    recent_records = columnar_filings_to_records(recent, forms=forms)
    all_records.extend(recent_records)

    historical_files = submissions.get("filings", {}).get("files", []) if include_historical else []
    for file_info in historical_files:
        file_name = file_info["name"]
        file_url = f"{BASE_DATA}/submissions/{file_name}"
        hist_json = get_json(file_url)

        if "accessionNumber" in hist_json:
            hist_filings = hist_json
        elif "filings" in hist_json and "recent" in hist_json["filings"]:
            hist_filings = hist_json["filings"]["recent"]
        else:
            print(f"Unexpected structure in {file_name}: {hist_json.keys()}")
            continue

        all_records.extend(columnar_filings_to_records(hist_filings, forms=forms))

    unique = {record["accession_number"]: record for record in all_records}
    records = sorted(unique.values(), key=lambda x: (x["filing_date"] or ""), reverse=True)
    return records


def filing_primary_doc_url(cik: str, filing: dict) -> str:
    cik_no_leading_zeros = str(int(cik))
    accession_no_dash = filing["accession_no_dash"]
    primary_doc = filing["primary_document"]
    return f"{BASE_SEC}/Archives/edgar/data/{cik_no_leading_zeros}/{accession_no_dash}/{primary_doc}"


def filing_complete_submission_url(cik: str, filing: dict) -> str:
    cik_no_leading_zeros = str(int(cik))
    accession_with_dash = filing["accession_number"]
    accession_no_dash = filing["accession_no_dash"]
    return f"{BASE_SEC}/Archives/edgar/data/{cik_no_leading_zeros}/{accession_no_dash}/{accession_with_dash}.txt"


def download_file(url: str, output_path: Path, sleep_sec: float = 0.15, skip_existing: bool = True) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if skip_existing and output_path.exists() and output_path.stat().st_size > 0:
        return "skipped_existing"

    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    output_path.write_bytes(resp.content)
    time.sleep(sleep_sec)
    return "success"


# -----------------------------
# DOWNLOAD CACHE HELPERS
# -----------------------------
def accession_no_dash_to_accession(accession_no_dash: str) -> str:
    """Convert 18-digit accession without dashes to SEC dashed accession format."""
    s = str(accession_no_dash or "")
    if not re.fullmatch(r"\d{18}", s):
        return s
    return f"{s[:10]}-{s[10:12]}-{s[12:]}"


def local_file_exists(path_text: str | None) -> bool:
    if not path_text:
        return False
    try:
        p = Path(path_text)
        return p.exists() and p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def cache_row_from_metadata_record(record: dict, metadata_path: Path) -> dict:
    local_path = record.get("local_path", "")
    txt_local_path = record.get("txt_local_path", "")
    return {
        "ticker": record.get("ticker", metadata_path.parent.name),
        "company_name": record.get("company_name", ""),
        "cik": record.get("cik", ""),
        "form": record.get("form", ""),
        "report_date": record.get("report_date", ""),
        "filing_date": record.get("filing_date", ""),
        "accession_number": record.get("accession_number", ""),
        "accession_no_dash": record.get("accession_no_dash", ""),
        "primary_document": record.get("primary_document", ""),
        "local_path": local_path,
        "txt_local_path": txt_local_path,
        "primary_download_status": record.get("primary_download_status", ""),
        "txt_download_status": record.get("txt_download_status", ""),
        "primary_file_exists": local_file_exists(local_path),
        "txt_file_exists": local_file_exists(txt_local_path),
        "metadata_path": str(metadata_path),
        "cache_source": "download_metadata",
        "last_cache_scan": datetime.now().isoformat(timespec="seconds"),
    }


def cache_rows_from_existing_files(output_dir: str | Path = OUTPUT_DIR) -> list[dict]:
    """Best-effort cache rows from files already present even if metadata is missing.

    Filenames created by this project include the accession number without dashes.
    This lets the cache recognize old downloads that predate the manifest.
    """
    output_dir = Path(output_dir)
    rows_by_accession: dict[str, dict] = {}
    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        if path.name in {"download_metadata.json", "batch_download_metadata.json", CACHE_MANIFEST_CSV.name}:
            continue
        if path.suffix.lower() not in {".htm", ".html", ".txt"}:
            continue

        m = re.search(r"_(\d{18})(?:_|\.)", path.name)
        if not m:
            continue
        accession_no_dash = m.group(1)
        accession_number = accession_no_dash_to_accession(accession_no_dash)
        parts = path.name.split("_")
        report_date = parts[0] if len(parts) > 0 else ""
        filing_date = parts[1] if len(parts) > 1 else ""
        form = parts[2] if len(parts) > 2 else ""
        ticker = path.parent.name

        row = rows_by_accession.setdefault(accession_number, {
            "ticker": ticker,
            "company_name": "",
            "cik": "",
            "form": form,
            "report_date": report_date,
            "filing_date": filing_date,
            "accession_number": accession_number,
            "accession_no_dash": accession_no_dash,
            "primary_document": "",
            "local_path": "",
            "txt_local_path": "",
            "primary_download_status": "found_existing",
            "txt_download_status": "",
            "primary_file_exists": False,
            "txt_file_exists": False,
            "metadata_path": "",
            "cache_source": "file_scan",
            "last_cache_scan": datetime.now().isoformat(timespec="seconds"),
        })
        if path.suffix.lower() == ".txt":
            row["txt_local_path"] = str(path)
            row["txt_file_exists"] = True
            row["txt_download_status"] = "found_existing"
        else:
            row["local_path"] = str(path)
            row["primary_file_exists"] = True
            row["primary_download_status"] = "found_existing"

    return list(rows_by_accession.values())


def rebuild_download_cache_manifest(
    output_dir: str | Path = OUTPUT_DIR,
    manifest_path: str | Path = CACHE_MANIFEST_CSV,
) -> pd.DataFrame:
    """Rebuild accession-level cache from per-ticker metadata and existing files."""
    output_dir = Path(output_dir)
    rows: list[dict] = []

    for metadata_path in output_dir.glob("*/download_metadata.json"):
        try:
            records = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Warning: could not read {metadata_path}: {e}")
            continue
        if not isinstance(records, list):
            continue
        rows.extend(cache_row_from_metadata_record(record, metadata_path) for record in records)

    # Include files even if metadata is absent or stale. Metadata rows win later.
    rows.extend(cache_rows_from_existing_files(output_dir))

    columns = [
        "ticker", "company_name", "cik", "form", "report_date", "filing_date",
        "accession_number", "accession_no_dash", "primary_document",
        "local_path", "txt_local_path", "primary_download_status", "txt_download_status",
        "primary_file_exists", "txt_file_exists", "metadata_path", "cache_source", "last_cache_scan",
    ]
    if not rows:
        df = pd.DataFrame(columns=columns)
    else:
        df = pd.DataFrame(rows)
        for col in columns:
            if col not in df.columns:
                df[col] = ""
        df["primary_file_exists"] = df["primary_file_exists"].fillna(False).astype(bool)
        df["txt_file_exists"] = df["txt_file_exists"].fillna(False).astype(bool)
        # Prefer rows with existing files and metadata over pure file-scan rows.
        df["_score"] = (
            df["primary_file_exists"].astype(int) * 10
            + df["txt_file_exists"].astype(int) * 10
            + (df["cache_source"] == "download_metadata").astype(int)
        )
        df = df.sort_values(["accession_number", "_score"]).drop_duplicates("accession_number", keep="last")
        df = df.drop(columns=["_score"])
        df = df[columns].sort_values(["ticker", "filing_date", "accession_number"], ascending=[True, False, True])

    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(manifest_path, index=False)
    print(f"Download cache manifest saved to: {manifest_path} ({len(df)} accessions)")
    return df


def build_cache_index(cache_df: pd.DataFrame | None) -> dict[str, dict]:
    if cache_df is None or cache_df.empty or "accession_number" not in cache_df.columns:
        return {}
    return {str(row["accession_number"]): row.to_dict() for _, row in cache_df.iterrows()}


def filing_is_cached(
    filing: dict,
    cache_index: dict[str, dict] | None,
    expected_primary_path: Path,
    expected_txt_path: Path | None = None,
    require_txt: bool = True,
) -> bool:
    if not cache_index:
        return False
    accession = filing.get("accession_number", "")
    cached = cache_index.get(accession)
    if not cached:
        return False

    primary_ok = (
        local_file_exists(str(expected_primary_path))
        or bool(cached.get("primary_file_exists"))
        or local_file_exists(cached.get("local_path"))
    )
    if not primary_ok:
        return False

    if not require_txt:
        return True

    return (
        expected_txt_path is not None and local_file_exists(str(expected_txt_path))
    ) or bool(cached.get("txt_file_exists")) or local_file_exists(cached.get("txt_local_path"))


# -----------------------------
# TICKER UNIVERSE HELPERS
# -----------------------------
def read_csv_if_exists(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")
    return pd.read_csv(path, dtype=str).fillna("")


def security_name_is_excluded(security_name: str) -> bool:
    name = str(security_name).lower()
    return any(re.search(pattern, name, flags=re.IGNORECASE) for pattern in EXCLUDE_SECURITY_NAME_PATTERNS)


def clean_company_name(value: str) -> str:
    """Light cleanup for display-only company names."""
    value = str(value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def load_reference_company_names(
    nasdaq_listed_csv: Path = NASDAQ_LISTED_CSV,
    nyse_listed_csv: Path = NYSE_LISTED_CSV,
) -> pd.DataFrame:
    """Load short NASDAQ/NYSE files for cross-referencing the final universe.

    This is intentionally separate from filtering. The full files
    `nasdaq-listed-symbols.csv` and `other-listed.csv` are still the source of
    truth for universe construction because they contain useful flags such as
    ETF/Test Issue/Financial Status/Exchange.
    """
    frames: list[pd.DataFrame] = []

    if nasdaq_listed_csv.exists():
        nasdaq = pd.read_csv(nasdaq_listed_csv, dtype=str).fillna("")
        if {"Symbol", "Security Name"}.issubset(nasdaq.columns):
            nasdaq_ref = pd.DataFrame({
                "ticker": nasdaq["Symbol"].map(normalize_ticker_for_sec),
                "nasdaq_reference_name": nasdaq["Security Name"].map(clean_company_name),
                "in_nasdaq_listed_file": True,
            })
            frames.append(nasdaq_ref.drop_duplicates("ticker"))
        else:
            print(f"Warning: {nasdaq_listed_csv} missing Symbol/Security Name columns; skipping NASDAQ reference names.")
    else:
        print(f"Warning: NASDAQ reference file not found: {nasdaq_listed_csv}")

    if nyse_listed_csv.exists():
        nyse = pd.read_csv(nyse_listed_csv, dtype=str).fillna("")
        # The generated nyse-listed.csv usually has ACT Symbol + Company Name.
        if {"ACT Symbol", "Company Name"}.issubset(nyse.columns):
            nyse_ref = pd.DataFrame({
                "ticker": nyse["ACT Symbol"].map(normalize_ticker_for_sec),
                "nyse_reference_name": nyse["Company Name"].map(clean_company_name),
                "in_nyse_listed_file": True,
            })
        elif {"Symbol", "Company Name"}.issubset(nyse.columns):
            nyse_ref = pd.DataFrame({
                "ticker": nyse["Symbol"].map(normalize_ticker_for_sec),
                "nyse_reference_name": nyse["Company Name"].map(clean_company_name),
                "in_nyse_listed_file": True,
            })
        else:
            print(f"Warning: {nyse_listed_csv} missing expected ticker/name columns; skipping NYSE reference names.")
            nyse_ref = pd.DataFrame()
        if not nyse_ref.empty:
            frames.append(nyse_ref.drop_duplicates("ticker"))
    else:
        print(f"Warning: NYSE reference file not found: {nyse_listed_csv}")

    if not frames:
        return pd.DataFrame(columns=[
            "ticker",
            "nasdaq_reference_name",
            "nyse_reference_name",
            "in_nasdaq_listed_file",
            "in_nyse_listed_file",
        ])

    ref = frames[0]
    for frame in frames[1:]:
        ref = ref.merge(frame, on="ticker", how="outer")

    for col in ["nasdaq_reference_name", "nyse_reference_name"]:
        if col not in ref.columns:
            ref[col] = ""
    for col in ["in_nasdaq_listed_file", "in_nyse_listed_file"]:
        if col not in ref.columns:
            ref[col] = False
        ref[col] = ref[col].fillna(False).astype(bool)

    return ref


def add_final_universe_company_names(
    universe: pd.DataFrame,
    nasdaq_listed_csv: Path = NASDAQ_LISTED_CSV,
    nyse_listed_csv: Path = NYSE_LISTED_CSV,
) -> pd.DataFrame:
    """Cross-reference the selected universe with short NASDAQ/NYSE name files."""
    final = universe.copy()
    ref = load_reference_company_names(nasdaq_listed_csv, nyse_listed_csv)
    if not ref.empty:
        final = final.merge(ref, on="ticker", how="left")
    else:
        final["nasdaq_reference_name"] = ""
        final["nyse_reference_name"] = ""
        final["in_nasdaq_listed_file"] = False
        final["in_nyse_listed_file"] = False

    for col in ["company_name_source", "Security Name", "nasdaq_reference_name", "nyse_reference_name"]:
        if col not in final.columns:
            final[col] = ""
        final[col] = final[col].fillna("").map(clean_company_name)

    for col in ["in_nasdaq_listed_file", "in_nyse_listed_file"]:
        if col not in final.columns:
            final[col] = False
        final[col] = final[col].fillna(False).astype(bool)

    # Prefer the clean official short-list name for display, but keep all
    # columns so we can audit mismatches later.
    def choose_name(row) -> str:
        if row.get("exchange_source") == "NASDAQ" and row.get("nasdaq_reference_name"):
            return row["nasdaq_reference_name"]
        if row.get("exchange_source") == "NYSE" and row.get("nyse_reference_name"):
            return row["nyse_reference_name"]
        return row.get("company_name_source") or row.get("Security Name") or ""

    final["company_name_final"] = final.apply(choose_name, axis=1)
    final["listed_reference_sources"] = final.apply(
        lambda row: ",".join(
            source for source, flag in [
                ("nasdaq-listed", row.get("in_nasdaq_listed_file", False)),
                ("nyse-listed", row.get("in_nyse_listed_file", False)),
            ]
            if flag
        ),
        axis=1,
    )

    preferred_cols = [
        "ticker",
        "raw_ticker",
        "company_name_final",
        "exchange_source",
        "listed_reference_sources",
        "nasdaq_reference_name",
        "nyse_reference_name",
        "company_name_source",
        "Security Name",
        "in_nasdaq_listed_file",
        "in_nyse_listed_file",
    ]
    remaining_cols = [c for c in final.columns if c not in preferred_cols]
    return final[[c for c in preferred_cols if c in final.columns] + remaining_cols]


def load_nasdaq_tickers(path: Path = NASDAQ_SYMBOLS_CSV) -> pd.DataFrame:
    df = read_csv_if_exists(path)

    required = {"Symbol", "Security Name", "Test Issue", "Financial Status", "ETF", "NextShares"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"NASDAQ ticker file missing columns: {sorted(missing)}")

    out = df.copy()
    out = out[
        (out["Test Issue"].str.upper() == "N")
        & (out["Financial Status"].str.upper() == "N")
        & (out["ETF"].str.upper() == "N")
        & (out["NextShares"].str.upper() == "N")
    ].copy()

    out = out[~out["Security Name"].apply(security_name_is_excluded)].copy()
    out["raw_ticker"] = out["Symbol"].str.upper().str.strip()
    out["ticker"] = out["raw_ticker"].apply(normalize_ticker_for_sec)
    out["exchange_source"] = "NASDAQ"
    out["company_name_source"] = out.get("Company Name", out["Security Name"])

    return out[["ticker", "raw_ticker", "exchange_source", "company_name_source", "Security Name"]].drop_duplicates("ticker")


def load_nyse_tickers(path: Path = OTHER_LISTED_CSV) -> pd.DataFrame:
    df = read_csv_if_exists(path)

    required = {"ACT Symbol", "Security Name", "Exchange", "Test Issue", "ETF"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Other-listed ticker file missing columns: {sorted(missing)}")

    out = df.copy()
    out = out[
        (out["Exchange"].str.upper() == "N")
        & (out["Test Issue"].str.upper() == "N")
        & (out["ETF"].str.upper() == "N")
    ].copy()

    out = out[~out["Security Name"].apply(security_name_is_excluded)].copy()
    out["raw_ticker"] = out["ACT Symbol"].str.upper().str.strip()
    out["ticker"] = out["raw_ticker"].apply(normalize_ticker_for_sec)
    out["exchange_source"] = "NYSE"
    out["company_name_source"] = out.get("Company Name", out["Security Name"])

    return out[["ticker", "raw_ticker", "exchange_source", "company_name_source", "Security Name"]].drop_duplicates("ticker")


def build_download_universe(
    nasdaq_csv: Path = NASDAQ_SYMBOLS_CSV,
    other_listed_csv: Path = OTHER_LISTED_CSV,
    include_nasdaq: bool = True,
    include_nyse: bool = True,
    ticker_limit: int | None = None,
    output_path: Path | None = None,
    final_universe_output_path: Path | None = FINAL_UNIVERSE_CSV,
    nasdaq_reference_csv: Path = NASDAQ_LISTED_CSV,
    nyse_reference_csv: Path = NYSE_LISTED_CSV,
) -> pd.DataFrame:
    frames = []
    if include_nasdaq:
        frames.append(load_nasdaq_tickers(nasdaq_csv))
    if include_nyse:
        frames.append(load_nyse_tickers(other_listed_csv))

    if not frames:
        raise ValueError("At least one of include_nasdaq/include_nyse must be True.")

    universe = pd.concat(frames, ignore_index=True)
    universe = universe.sort_values(["ticker", "exchange_source"]).drop_duplicates("ticker", keep="first")
    universe = universe.reset_index(drop=True)

    if ticker_limit is not None:
        universe = universe.head(ticker_limit).copy()

    final_universe = add_final_universe_company_names(
        universe,
        nasdaq_listed_csv=nasdaq_reference_csv,
        nyse_listed_csv=nyse_reference_csv,
    )

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        universe.to_csv(output_path, index=False)

    if final_universe_output_path is not None:
        final_universe_output_path.parent.mkdir(parents=True, exist_ok=True)
        final_universe.to_csv(final_universe_output_path, index=False)
        print(f"Final cross-referenced universe saved to: {final_universe_output_path}")

        # Keep dated monthly snapshots so we can audit ticker additions/delistings over time.
        run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_path = UNIVERSE_SNAPSHOT_DIR / f"sec_download_universe_final_{run_stamp}.csv"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        final_universe.to_csv(snapshot_path, index=False)
        print(f"Universe snapshot saved to: {snapshot_path}")

    return final_universe


# -----------------------------
# SINGLE-TICKER DOWNLOAD
# -----------------------------
def download_company_reports(
    ticker: str,
    forms=("10-K", "10-Q"),
    output_dir: str | Path = OUTPUT_DIR,
    start_year: int | None = None,
    end_year: int | None = None,
    date_basis: str = "report_date",
    max_reports: int | None = None,
    download_txt_backup: bool = True,
    txt_subdir: str | None = None,
    ticker_map: dict | None = None,
    skip_existing: bool = True,
    cache_index: dict[str, dict] | None = None,
    include_historical: bool = True,
) -> list[dict]:
    ticker = normalize_ticker_for_sec(ticker)
    cik, company_name = ticker_to_cik(ticker, ticker_map=ticker_map)

    filings = get_all_company_filings(cik, forms=forms, include_historical=include_historical)

    if start_year is not None or end_year is not None:
        filings = [
            f for f in filings
            if date_in_year_range(f, start_year=start_year, end_year=end_year, date_basis=date_basis)
        ]

    if max_reports is not None:
        filings = filings[:max_reports]

    print(f"Found {len(filings)} filings for {ticker} after filtering by {date_basis}.")

    company_dir = Path(output_dir) / ticker
    downloaded = []

    for filing in filings:
        url = filing_primary_doc_url(cik, filing)
        primary_doc = filing["primary_document"] or "primary_document.htm"
        safe_primary_doc = sanitize_filename_part(primary_doc, default="primary_document.htm")
        safe_form = sanitize_filename_part(filing["form"], default="form")

        filename = (
            f"{filing.get('report_date') or 'no_report_date'}_"
            f"{filing.get('filing_date') or 'no_filing_date'}_"
            f"{safe_form}_"
            f"{filing['accession_no_dash']}_"
            f"{safe_primary_doc}"
        )
        output_path = company_dir / filename

        print(f"Downloading {ticker} {filing['form']} | report {filing['report_date']} | filed {filing['filing_date']}")

        filing_out = {
            "ticker": ticker,
            "company_name": company_name,
            "cik": cik,
            "primary_url": url,
            "local_path": str(output_path),
            **filing,
        }

        txt_url = filing_complete_submission_url(cik, filing)
        txt_filename = (
            f"{filing.get('report_date') or 'no_report_date'}_"
            f"{filing.get('filing_date') or 'no_filing_date'}_"
            f"{safe_form}_"
            f"{filing['accession_no_dash']}.txt"
        )
        txt_output_path = (company_dir / txt_filename) if txt_subdir is None else (company_dir / txt_subdir / txt_filename)

        if filing_is_cached(
            filing,
            cache_index=cache_index,
            expected_primary_path=output_path,
            expected_txt_path=txt_output_path,
            require_txt=download_txt_backup,
        ):
            filing_out["primary_download_status"] = "skipped_cached"
            filing_out["txt_url"] = txt_url
            filing_out["txt_local_path"] = str(txt_output_path)
            filing_out["txt_download_status"] = "skipped_cached" if download_txt_backup else "not_requested"
            downloaded.append(filing_out)
            continue

        try:
            status = download_file(url, output_path, skip_existing=skip_existing)
            filing_out["primary_download_status"] = status
        except Exception as e:
            print(f"Primary document failed: {url}")
            print(f"Reason: {e}")
            filing_out["primary_download_status"] = "failed"
            filing_out["primary_download_error"] = str(e)

        if download_txt_backup or filing_out.get("primary_download_status") == "failed":
            txt_url = filing_complete_submission_url(cik, filing)
            txt_filename = (
                f"{filing.get('report_date') or 'no_report_date'}_"
                f"{filing.get('filing_date') or 'no_filing_date'}_"
                f"{safe_form}_"
                f"{filing['accession_no_dash']}.txt"
            )
            txt_output_path = (company_dir / txt_filename) if txt_subdir is None else (company_dir / txt_subdir / txt_filename)

            try:
                status = download_file(txt_url, txt_output_path, skip_existing=skip_existing)
                filing_out["txt_url"] = txt_url
                filing_out["txt_local_path"] = str(txt_output_path)
                filing_out["txt_download_status"] = status
            except Exception as e:
                print(f"TXT download failed: {txt_url}")
                print(f"Reason: {e}")
                filing_out["txt_download_status"] = "failed"
                filing_out["txt_download_error"] = str(e)

        downloaded.append(filing_out)

    metadata_path = company_dir / "download_metadata.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(downloaded, indent=2), encoding="utf-8")

    return downloaded


# -----------------------------
# BATCH DOWNLOAD
# -----------------------------
def download_reports_for_universe(
    universe: pd.DataFrame,
    forms=("10-K", "10-Q"),
    output_dir: str | Path = OUTPUT_DIR,
    start_year: int | None = None,
    end_year: int | None = None,
    date_basis: str = "report_date",
    max_reports_per_ticker: int | None = None,
    download_txt_backup: bool = True,
    txt_subdir: str | None = None,
    skip_existing: bool = True,
    continue_on_error: bool = True,
    rebuild_cache_at_start: bool = True,
    cache_manifest_path: str | Path = CACHE_MANIFEST_CSV,
    include_historical: bool = True,
) -> list[dict]:
    ticker_map = get_ticker_to_cik_map()
    cache_df = rebuild_download_cache_manifest(output_dir, cache_manifest_path) if rebuild_cache_at_start else pd.DataFrame()
    cache_index = build_cache_index(cache_df)
    batch_metadata = []

    total = len(universe)
    for idx, row in universe.iterrows():
        ticker = normalize_ticker_for_sec(row["ticker"])
        print("\n" + "=" * 80)
        print(f"[{idx + 1}/{total}] Processing {ticker} ({row.get('exchange_source', '')})")

        record = {
            "ticker": ticker,
            "raw_ticker": row.get("raw_ticker", ticker),
            "company_name_final": row.get("company_name_final", row.get("company_name_source", "")),
            "exchange_source": row.get("exchange_source", ""),
            "listed_reference_sources": row.get("listed_reference_sources", ""),
            "security_name": row.get("Security Name", ""),
        }

        try:
            filings = download_company_reports(
                ticker=ticker,
                forms=forms,
                output_dir=output_dir,
                start_year=start_year,
                end_year=end_year,
                date_basis=date_basis,
                max_reports=max_reports_per_ticker,
                download_txt_backup=download_txt_backup,
                txt_subdir=txt_subdir,
                ticker_map=ticker_map,
                skip_existing=skip_existing,
                cache_index=cache_index,
                include_historical=include_historical,
            )
            record["status"] = "success"
            record["num_filing_records"] = len(filings)
        except Exception as e:
            record["status"] = "failed"
            record["error"] = str(e)
            print(f"Ticker failed: {ticker} | {e}")
            if not continue_on_error:
                batch_metadata.append(record)
                raise

        batch_metadata.append(record)

        # Write progress after every ticker so long jobs are recoverable.
        progress_path = Path(output_dir) / "batch_download_metadata.json"
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        progress_path.write_text(json.dumps(batch_metadata, indent=2), encoding="utf-8")

        # Refresh cache after each ticker so an interrupted monthly run can resume cleanly.
        cache_df = rebuild_download_cache_manifest(output_dir, cache_manifest_path)
        cache_index = build_cache_index(cache_df)

    return batch_metadata


# -----------------------------
# EXAMPLE USAGE
# -----------------------------
if __name__ == "__main__":
    # Start with a small ticker_limit for pilot testing. Set ticker_limit=None for full run.
    universe = build_download_universe(
        nasdaq_csv=NASDAQ_SYMBOLS_CSV,
        other_listed_csv=OTHER_LISTED_CSV,
        include_nasdaq=True,
        include_nyse=True,
        ticker_limit=10,  # change to None after the pilot works
        output_path=RAW_UNIVERSE_CSV,
        final_universe_output_path=FINAL_UNIVERSE_CSV,
        nasdaq_reference_csv=NASDAQ_LISTED_CSV,
        nyse_reference_csv=NYSE_LISTED_CSV,
    )

    print(f"Universe size for this run: {len(universe)}")
    print(universe[["ticker", "company_name_final", "exchange_source", "listed_reference_sources"]].head(20).to_string(index=False))

    metadata = download_reports_for_universe(
        universe=universe,
        forms=("10-K", "10-Q"),
        output_dir=OUTPUT_DIR,
        start_year=2000,
        end_year=None,
        date_basis="report_date",
        max_reports_per_ticker=None,
        download_txt_backup=True,
        txt_subdir=None,  # saves .txt next to .htm so extractors can find them
        skip_existing=True,
        continue_on_error=True,
        rebuild_cache_at_start=True,
        cache_manifest_path=CACHE_MANIFEST_CSV,
        include_historical=True,
    )

    print(f"\nDone. Attempted {len(metadata)} tickers.")
    print(f"Batch metadata saved to: {OUTPUT_DIR / 'batch_download_metadata.json'}")
