import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

USER_AGENT = os.getenv("SEC_USER_AGENT")
if not USER_AGENT:
    raise ValueError("Missing SEC_USER_AGENT. Please set it in your local .env file.")

BASE_SEC = "https://www.sec.gov"
BASE_DATA = "https://data.sec.gov"

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "gzip, deflate",
}

# This script is intended to live inside src/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"

NASDAQ_SYMBOLS_CSV = DATA_DIR / "nasdaq-listed-symbols.csv"
OTHER_LISTED_CSV = DATA_DIR / "other-listed.csv"

NASDAQ_LISTED_CSV = DATA_DIR / "nasdaq-listed.csv"
NYSE_LISTED_CSV = DATA_DIR / "nyse-listed.csv"

RAW_UNIVERSE_CSV = DATA_DIR / "sec_download_universe_raw.csv"
FINAL_UNIVERSE_CSV = DATA_DIR / "sec_download_universe_final.csv"
UNIVERSE_SNAPSHOT_DIR = DATA_DIR / "universe_snapshots"

ROLLING_DIR = DATA_DIR / "sec_rolling"
ROLLING_MANIFEST_CSV = ROLLING_DIR / "sec_filing_manifest.csv"

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

ROLLING_MANIFEST_COLUMNS = [
    "ticker",
    "company_name",
    "cik",
    "form",
    "report_date",
    "filing_date",
    "accession_number",
    "accession_no_dash",
    "primary_document",
    "primary_url",
    "txt_url",
    "local_path",
    "txt_local_path",
    "download_status",
    "txt_download_status",
    "pipeline_status",
    "raw_file_available",
    "raw_file_deleted",
    "batch_id",
    "last_attempt_at",
    "downloaded_at",
    "processed_at",
    "error",
]


# ============================================================
# BASIC HELPERS
# ============================================================

def sanitize_filename_part(value: str | None, default: str = "unknown") -> str:
    if not value:
        value = default
    value = str(value)
    value = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or default


def normalize_ticker_for_sec(ticker: str) -> str:
    ticker = str(ticker).strip().upper()
    ticker = ticker.replace(".", "-")
    return ticker


def parse_year(date_text: str | None) -> int | None:
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


def date_in_date_range(
    filing: dict,
    start_date: str | None = None,
    end_date: str | None = None,
    date_basis: str = "report_date",
) -> bool:
    """
    Filter filing by inclusive date range.

    Example:
      start_date="2026-01-01" keeps filings whose selected date is
      on or after 2026-01-01.

    date_basis options:
      - "report_date": fiscal/report period date; best for financial statements
      - "filing_date": SEC filing date
      - "either": keep if either report_date or filing_date is in range
    """
    if date_basis not in {"report_date", "filing_date", "either"}:
        raise ValueError("date_basis must be 'report_date', 'filing_date', or 'either'")

    start_ts = pd.to_datetime(start_date, errors="coerce") if start_date else None
    end_ts = pd.to_datetime(end_date, errors="coerce") if end_date else None

    if start_ts is not None and pd.isna(start_ts):
        raise ValueError(f"Invalid start_date: {start_date}")
    if end_ts is not None and pd.isna(end_ts):
        raise ValueError(f"Invalid end_date: {end_date}")

    def ok(date_value: str | None) -> bool:
        if not date_value:
            return False

        d = pd.to_datetime(date_value, errors="coerce")
        if pd.isna(d):
            return False

        if start_ts is not None and d < start_ts:
            return False
        if end_ts is not None and d > end_ts:
            return False

        return True

    if date_basis == "either":
        return ok(filing.get("report_date")) or ok(filing.get("filing_date"))

    return ok(filing.get(date_basis))


def get_json(url: str) -> dict:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    time.sleep(0.15)
    return resp.json()


def download_file(url: str, output_path: Path, sleep_sec: float = 0.15, skip_existing: bool = True) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if skip_existing and output_path.exists() and output_path.stat().st_size > 0:
        return "skipped_existing"

    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    output_path.write_bytes(resp.content)
    time.sleep(sleep_sec)
    return "success"


# ============================================================
# SEC METADATA HELPERS
# ============================================================

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


def get_company_submissions(cik: str) -> dict:
    cik = str(cik).zfill(10)
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


def get_all_company_filings(
    cik: str,
    forms=("10-K", "10-Q"),
    include_historical: bool = True,
) -> list[dict]:
    submissions = get_company_submissions(cik)
    all_records = []

    recent = submissions.get("filings", {}).get("recent", {})
    all_records.extend(columnar_filings_to_records(recent, forms=forms))

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


# ============================================================
# TICKER UNIVERSE HELPERS
# ============================================================

def read_csv_if_exists(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Required file not found: {path}")
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)


def security_name_is_excluded(security_name: str) -> bool:
    name = str(security_name).lower()
    return any(re.search(pattern, name, flags=re.IGNORECASE) for pattern in EXCLUDE_SECURITY_NAME_PATTERNS)


def clean_company_name(value: str) -> str:
    value = str(value or "").strip()
    value = re.sub(r"\s+", " ", value)
    return value


def load_reference_company_names(
    nasdaq_listed_csv: Path = NASDAQ_LISTED_CSV,
    nyse_listed_csv: Path = NYSE_LISTED_CSV,
) -> pd.DataFrame:
    frames = []

    if nasdaq_listed_csv.exists():
        nasdaq = pd.read_csv(nasdaq_listed_csv, dtype=str, keep_default_na=False, na_filter=False)
        if {"Symbol", "Security Name"}.issubset(nasdaq.columns):
            nasdaq_ref = pd.DataFrame({
                "ticker": nasdaq["Symbol"].map(normalize_ticker_for_sec),
                "nasdaq_reference_name": nasdaq["Security Name"].map(clean_company_name),
                "in_nasdaq_listed_file": True,
            })
            frames.append(nasdaq_ref.drop_duplicates("ticker"))

    if nyse_listed_csv.exists():
        nyse = pd.read_csv(nyse_listed_csv, dtype=str, keep_default_na=False, na_filter=False)

        if {"ACT Symbol", "Company Name"}.issubset(nyse.columns):
            nyse_ref = pd.DataFrame({
                "ticker": nyse["ACT Symbol"].map(normalize_ticker_for_sec),
                "nyse_reference_name": nyse["Company Name"].map(clean_company_name),
                "in_nyse_listed_file": True,
            })
            frames.append(nyse_ref.drop_duplicates("ticker"))

        elif {"Symbol", "Company Name"}.issubset(nyse.columns):
            nyse_ref = pd.DataFrame({
                "ticker": nyse["Symbol"].map(normalize_ticker_for_sec),
                "nyse_reference_name": nyse["Company Name"].map(clean_company_name),
                "in_nyse_listed_file": True,
            })
            frames.append(nyse_ref.drop_duplicates("ticker"))

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

    return out[
        ["ticker", "raw_ticker", "exchange_source", "company_name_source", "Security Name"]
    ].drop_duplicates("ticker")


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

    return out[
        ["ticker", "raw_ticker", "exchange_source", "company_name_source", "Security Name"]
    ].drop_duplicates("ticker")


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

        run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_path = UNIVERSE_SNAPSHOT_DIR / f"sec_download_universe_final_{run_stamp}.csv"
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        final_universe.to_csv(snapshot_path, index=False)
        print(f"Universe snapshot saved to: {snapshot_path}")

    return final_universe


# ============================================================
# ROLLING MANIFEST HELPERS
# ============================================================

def read_rolling_manifest(manifest_path: str | Path = ROLLING_MANIFEST_CSV) -> pd.DataFrame:
    manifest_path = Path(manifest_path)

    if not manifest_path.exists():
        return pd.DataFrame(columns=ROLLING_MANIFEST_COLUMNS)

    df = pd.read_csv(manifest_path, dtype=str, keep_default_na=False, na_filter=False)

    for col in ROLLING_MANIFEST_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    return df[ROLLING_MANIFEST_COLUMNS]


def write_rolling_manifest(
    df: pd.DataFrame,
    manifest_path: str | Path = ROLLING_MANIFEST_CSV,
) -> None:
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    for col in ROLLING_MANIFEST_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    df = df[ROLLING_MANIFEST_COLUMNS].copy()
    df = df.drop_duplicates("accession_number", keep="last")
    df = df.sort_values(["ticker", "filing_date", "accession_number"], ascending=[True, False, True])
    df.to_csv(manifest_path, index=False)


def upsert_rolling_manifest_rows(
    new_rows: list[dict],
    manifest_path: str | Path = ROLLING_MANIFEST_CSV,
) -> pd.DataFrame:
    existing = read_rolling_manifest(manifest_path)

    if not new_rows:
        return existing

    new_df = pd.DataFrame(new_rows)

    for col in ROLLING_MANIFEST_COLUMNS:
        if col not in new_df.columns:
            new_df[col] = ""

    combined = pd.concat([existing, new_df[ROLLING_MANIFEST_COLUMNS]], ignore_index=True)
    combined = combined.drop_duplicates("accession_number", keep="last")

    write_rolling_manifest(combined, manifest_path)
    return combined


def build_manifest_index(manifest_df: pd.DataFrame) -> dict[str, dict]:
    if manifest_df.empty or "accession_number" not in manifest_df.columns:
        return {}

    return {
        str(row["accession_number"]): row.to_dict()
        for _, row in manifest_df.iterrows()
    }


def filing_should_skip(
    filing: dict,
    manifest_index: dict[str, dict],
    skip_pipeline_statuses: set[str] | None = None,
) -> bool:
    if skip_pipeline_statuses is None:
        skip_pipeline_statuses = {"downloaded", "processed"}

    accession = str(filing.get("accession_number", ""))

    if accession not in manifest_index:
        return False

    old = manifest_index[accession]
    return old.get("pipeline_status", "") in skip_pipeline_statuses


# ============================================================
# ROLLING DOWNLOAD FUNCTIONS
# ============================================================

def build_filing_output_paths(
    ticker: str,
    cik: str,
    filing: dict,
    output_dir: str | Path,
    txt_subdir: str | None = None,
) -> tuple[str, Path, str, Path]:
    output_dir = Path(output_dir)
    company_dir = output_dir / ticker

    primary_url = filing_primary_doc_url(cik, filing)
    txt_url = filing_complete_submission_url(cik, filing)

    primary_doc = filing["primary_document"] or "primary_document.htm"
    safe_primary_doc = sanitize_filename_part(primary_doc, default="primary_document.htm")
    safe_form = sanitize_filename_part(filing["form"], default="form")

    primary_filename = (
        f"{filing.get('report_date') or 'no_report_date'}_"
        f"{filing.get('filing_date') or 'no_filing_date'}_"
        f"{safe_form}_"
        f"{filing['accession_no_dash']}_"
        f"{safe_primary_doc}"
    )

    txt_filename = (
        f"{filing.get('report_date') or 'no_report_date'}_"
        f"{filing.get('filing_date') or 'no_filing_date'}_"
        f"{safe_form}_"
        f"{filing['accession_no_dash']}.txt"
    )

    primary_path = company_dir / primary_filename
    txt_path = company_dir / txt_filename if txt_subdir is None else company_dir / txt_subdir / txt_filename

    return primary_url, primary_path, txt_url, txt_path


def discover_company_filings_for_rolling(
    ticker: str,
    forms=("10-K", "10-Q"),
    start_year: int | None = 2001,
    end_year: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    date_basis: str = "report_date",
    max_reports: int | None = None,
    ticker_map: dict | None = None,
    include_historical: bool = True,
) -> tuple[str, str, list[dict]]:
    ticker = normalize_ticker_for_sec(ticker)
    cik, company_name = ticker_to_cik(ticker, ticker_map=ticker_map)

    filings = get_all_company_filings(
        cik,
        forms=forms,
        include_historical=include_historical,
    )

    if start_year is not None or end_year is not None:
        filings = [
            f for f in filings
            if date_in_year_range(
                f,
                start_year=start_year,
                end_year=end_year,
                date_basis=date_basis,
            )
        ]

    if start_date is not None or end_date is not None:
        filings = [
            f for f in filings
            if date_in_date_range(
                f,
                start_date=start_date,
                end_date=end_date,
                date_basis=date_basis,
            )
        ]

    if max_reports is not None:
        filings = filings[:max_reports]

    return cik, company_name, filings


def download_pending_company_reports(
    ticker: str,
    forms=("10-K", "10-Q"),
    output_dir: str | Path = PROJECT_ROOT / "sec_filings_rolling_tmp",
    start_year: int | None = 2001,
    end_year: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    date_basis: str = "report_date",
    max_reports: int | None = None,
    download_txt_backup: bool = False,
    txt_subdir: str | None = None,
    ticker_map: dict | None = None,
    include_historical: bool = True,
    manifest_path: str | Path = ROLLING_MANIFEST_CSV,
    batch_id: str | int = "",
    skip_pipeline_statuses: set[str] | None = None,
) -> list[dict]:
    ticker = normalize_ticker_for_sec(ticker)
    now = datetime.now().isoformat(timespec="seconds")

    manifest_df = read_rolling_manifest(manifest_path)
    manifest_index = build_manifest_index(manifest_df)

    cik, company_name, filings = discover_company_filings_for_rolling(
        ticker=ticker,
        forms=forms,
        start_year=start_year,
        end_year=end_year,
        start_date=start_date,
        end_date=end_date,
        date_basis=date_basis,
        max_reports=max_reports,
        ticker_map=ticker_map,
        include_historical=include_historical,
    )

    print(f"Found {len(filings)} candidate filings for {ticker} after filtering by {date_basis}.")

    pending_filings = [
        f for f in filings
        if not filing_should_skip(
            f,
            manifest_index=manifest_index,
            skip_pipeline_statuses=skip_pipeline_statuses,
        )
    ]

    print(f"{ticker}: {len(pending_filings)} pending filings to download.")

    downloaded_records = []

    for filing in pending_filings:
        primary_url, primary_path, txt_url, txt_path = build_filing_output_paths(
            ticker=ticker,
            cik=cik,
            filing=filing,
            output_dir=output_dir,
            txt_subdir=txt_subdir,
        )

        print(
            f"Downloading {ticker} {filing['form']} | "
            f"report {filing.get('report_date')} | "
            f"filed {filing.get('filing_date')}"
        )

        row = {
            "ticker": ticker,
            "company_name": company_name,
            "cik": cik,
            "form": filing.get("form", ""),
            "report_date": filing.get("report_date", ""),
            "filing_date": filing.get("filing_date", ""),
            "accession_number": filing.get("accession_number", ""),
            "accession_no_dash": filing.get("accession_no_dash", ""),
            "primary_document": filing.get("primary_document", ""),
            "primary_url": primary_url,
            "txt_url": txt_url,
            "local_path": str(primary_path),
            "txt_local_path": str(txt_path) if download_txt_backup else "",
            "download_status": "",
            "txt_download_status": "not_requested" if not download_txt_backup else "",
            "pipeline_status": "",
            "raw_file_available": "False",
            "raw_file_deleted": "False",
            "batch_id": str(batch_id),
            "last_attempt_at": now,
            "downloaded_at": "",
            "processed_at": "",
            "error": "",
        }

        try:
            status = download_file(primary_url, primary_path, skip_existing=True)

            row["download_status"] = status
            row["pipeline_status"] = "downloaded"
            row["raw_file_available"] = "True"
            row["downloaded_at"] = datetime.now().isoformat(timespec="seconds")

        except Exception as e:
            print(f"Primary document failed: {primary_url}")
            print(f"Reason: {e}")

            row["download_status"] = "failed"
            row["pipeline_status"] = "download_failed"
            row["raw_file_available"] = "False"
            row["error"] = str(e)

        if download_txt_backup or row["download_status"] == "failed":
            row["txt_local_path"] = str(txt_path)

            try:
                txt_status = download_file(txt_url, txt_path, skip_existing=True)
                row["txt_download_status"] = txt_status

                if row["download_status"] == "failed" and txt_status in {"success", "skipped_existing"}:
                    row["pipeline_status"] = "downloaded"
                    row["raw_file_available"] = "True"
                    row["downloaded_at"] = datetime.now().isoformat(timespec="seconds")

            except Exception as e:
                print(f"TXT download failed: {txt_url}")
                print(f"Reason: {e}")

                row["txt_download_status"] = "failed"
                row["error"] = (row["error"] + f" | TXT error: {e}").strip(" |")

        downloaded_records.append(row)

        # Persist after every filing, so interrupted runs can resume.
        upsert_rolling_manifest_rows([row], manifest_path=manifest_path)

        # Refresh in-memory index so duplicates within the same run are skipped.
        manifest_df = read_rolling_manifest(manifest_path)
        manifest_index = build_manifest_index(manifest_df)

    return downloaded_records


def download_pending_reports_for_universe(
    universe: pd.DataFrame,
    forms=("10-K", "10-Q"),
    output_dir: str | Path = PROJECT_ROOT / "sec_filings_rolling_tmp",
    start_year: int | None = 2001,
    end_year: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    date_basis: str = "report_date",
    max_reports_per_ticker: int | None = None,
    download_txt_backup: bool = False,
    txt_subdir: str | None = None,
    continue_on_error: bool = True,
    include_historical: bool = True,
    manifest_path: str | Path = ROLLING_MANIFEST_CSV,
    batch_id: str | int = "",
    skip_pipeline_statuses: set[str] | None = None,
) -> list[dict]:
    ticker_map = get_ticker_to_cik_map()
    batch_metadata = []

    total = len(universe)

    for local_idx, (_, row) in enumerate(universe.iterrows(), start=1):
        ticker = normalize_ticker_for_sec(row["ticker"])

        print("\n" + "=" * 80)
        print(f"[{local_idx}/{total}] Processing {ticker} ({row.get('exchange_source', '')})")

        record = {
            "ticker": ticker,
            "raw_ticker": row.get("raw_ticker", ticker),
            "company_name_final": row.get("company_name_final", row.get("company_name_source", "")),
            "exchange_source": row.get("exchange_source", ""),
            "listed_reference_sources": row.get("listed_reference_sources", ""),
            "security_name": row.get("Security Name", ""),
            "status": "",
            "num_downloaded_records": 0,
            "error": "",
        }

        try:
            downloaded = download_pending_company_reports(
                ticker=ticker,
                forms=forms,
                output_dir=output_dir,
                start_year=start_year,
                end_year=end_year,
                start_date=start_date,
                end_date=end_date,
                date_basis=date_basis,
                max_reports=max_reports_per_ticker,
                download_txt_backup=download_txt_backup,
                txt_subdir=txt_subdir,
                ticker_map=ticker_map,
                include_historical=include_historical,
                manifest_path=manifest_path,
                batch_id=batch_id,
                skip_pipeline_statuses=skip_pipeline_statuses,
            )

            record["status"] = "success"
            record["num_downloaded_records"] = len(downloaded)

        except Exception as e:
            record["status"] = "failed"
            record["error"] = str(e)
            print(f"Ticker failed: {ticker} | {e}")

            if not continue_on_error:
                batch_metadata.append(record)
                raise

        batch_metadata.append(record)

    return batch_metadata


if __name__ == "__main__":
    print("This module is intended to be called by run_sec_rolling_batches.py.")
    print("Example:")
    print("python src/run_sec_rolling_batches.py --ticker-limit 20 --batch-size 10 --start-year 2001")