import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd

from download_sec_filings_rolling_batch import (
    PROJECT_ROOT,
    DATA_DIR,
    NASDAQ_SYMBOLS_CSV,
    OTHER_LISTED_CSV,
    NASDAQ_LISTED_CSV,
    NYSE_LISTED_CSV,
    RAW_UNIVERSE_CSV,
    FINAL_UNIVERSE_CSV,
    ROLLING_DIR,
    ROLLING_MANIFEST_CSV,
    build_download_universe,
    download_pending_reports_for_universe,
)


# ============================================================
# CONFIG
# ============================================================

BATCH_SIZE = 10
START_YEAR = 2001
END_YEAR = None
FORMS = ("10-K", "10-Q")

ROLLING_RAW_DIR = PROJECT_ROOT / "sec_filings_rolling_tmp"

BATCH_LOG_DIR = ROLLING_DIR / "batch_logs"
BATCH_STATUS_CSV = BATCH_LOG_DIR / "rolling_batch_status.csv"


# ============================================================
# HELPERS
# ============================================================

def load_or_build_universe(
    rebuild_universe: bool = False,
    ticker_limit: int | None = None,
) -> pd.DataFrame:
    if FINAL_UNIVERSE_CSV.exists() and not rebuild_universe:
        print(f"Loading existing universe: {FINAL_UNIVERSE_CSV}")
        universe = pd.read_csv(FINAL_UNIVERSE_CSV, dtype=str, keep_default_na=False, na_filter=False)
    else:
        print("Building universe from NASDAQ/NYSE ticker files...")

        universe = build_download_universe(
            nasdaq_csv=NASDAQ_SYMBOLS_CSV,
            other_listed_csv=OTHER_LISTED_CSV,
            include_nasdaq=True,
            include_nyse=True,
            ticker_limit=None,
            output_path=RAW_UNIVERSE_CSV,
            final_universe_output_path=FINAL_UNIVERSE_CSV,
            nasdaq_reference_csv=NASDAQ_LISTED_CSV,
            nyse_reference_csv=NYSE_LISTED_CSV,
        )

    if ticker_limit is not None:
        universe = universe.head(ticker_limit).copy()

    return universe.reset_index(drop=True)


def iter_batches(df: pd.DataFrame, batch_size: int):
    n = len(df)

    for start in range(0, n, batch_size):
        end = min(start + batch_size, n)
        batch_id = start // batch_size + 1
        yield batch_id, start, end, df.iloc[start:end].copy()


def get_dir_size_gb(path: Path) -> float:
    if not path.exists():
        return 0.0

    total = 0

    for p in path.rglob("*"):
        if p.is_file():
            total += p.stat().st_size

    return total / (1024 ** 3)


def append_batch_status(record: dict) -> None:
    BATCH_LOG_DIR.mkdir(parents=True, exist_ok=True)

    row = pd.DataFrame([record])

    if BATCH_STATUS_CSV.exists():
        old = pd.read_csv(BATCH_STATUS_CSV, dtype=str, keep_default_na=False, na_filter=False)
        out = pd.concat([old, row], ignore_index=True)
    else:
        out = row

    out.to_csv(BATCH_STATUS_CSV, index=False)


# ============================================================
# MAIN PIPELINE
# ============================================================

def run_rolling_batches(
    batch_size: int = BATCH_SIZE,
    start_year: int | None = START_YEAR,
    end_year: int | None = END_YEAR,
    ticker_limit: int | None = None,
    rebuild_universe: bool = False,
    download_txt_backup: bool = False,
    include_historical: bool = True,
    clear_batch_dir_before_run: bool = False,
):
    universe = load_or_build_universe(
        rebuild_universe=rebuild_universe,
        ticker_limit=ticker_limit,
    )

    print("\nRolling SEC batch download started.")
    print(f"Universe size: {len(universe)}")
    print(f"Batch size: {batch_size}")
    print(f"Start year: {start_year}")
    print(f"End year: {end_year}")
    print(f"Raw temporary folder: {ROLLING_RAW_DIR}")
    print(f"Permanent manifest: {ROLLING_MANIFEST_CSV}")
    print(f"Download TXT backup: {download_txt_backup}")

    BATCH_LOG_DIR.mkdir(parents=True, exist_ok=True)
    ROLLING_RAW_DIR.mkdir(parents=True, exist_ok=True)

    for batch_id, start, end, batch_df in iter_batches(universe, batch_size):
        batch_dir = ROLLING_RAW_DIR / f"batch_{batch_id:05d}"
        tickers = batch_df["ticker"].tolist()

        print("\n" + "=" * 100)
        print(f"Batch {batch_id} | rows {start}:{end} | tickers: {tickers}")
        print(f"Batch folder: {batch_dir}")
        print("=" * 100)

        record = {
            "batch_id": batch_id,
            "start_row": start,
            "end_row": end,
            "num_tickers": len(batch_df),
            "tickers": ",".join(tickers),
            "batch_dir": str(batch_dir),
            "start_time": datetime.now().isoformat(timespec="seconds"),
            "end_time": "",
            "raw_size_gb": "",
            "num_downloaded_records": "",
            "status": "started",
            "message": "",
        }

        try:
            if clear_batch_dir_before_run and batch_dir.exists():
                print(f"Clearing old batch folder: {batch_dir}")
                shutil.rmtree(batch_dir)

            batch_dir.mkdir(parents=True, exist_ok=True)

            metadata = download_pending_reports_for_universe(
                universe=batch_df,
                forms=FORMS,
                output_dir=batch_dir,
                start_year=start_year,
                end_year=end_year,
                date_basis="report_date",
                max_reports_per_ticker=None,
                download_txt_backup=download_txt_backup,
                txt_subdir=None,
                continue_on_error=True,
                include_historical=include_historical,
                manifest_path=ROLLING_MANIFEST_CSV,
                batch_id=batch_id,
                skip_pipeline_statuses={"downloaded", "processed"},
            )

            metadata_path = BATCH_LOG_DIR / f"batch_{batch_id:05d}_download_metadata.json"
            metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

            raw_size_gb = get_dir_size_gb(batch_dir)
            num_downloaded = sum(int(x.get("num_downloaded_records", 0)) for x in metadata)

            record["raw_size_gb"] = round(raw_size_gb, 4)
            record["num_downloaded_records"] = num_downloaded
            record["status"] = "completed"
            record["message"] = f"Download completed. Metadata: {metadata_path}"

            print(f"Batch {batch_id} completed.")
            print(f"Raw size: {raw_size_gb:.4f} GB")
            print(f"Downloaded filing records: {num_downloaded}")
            print(f"Metadata saved to: {metadata_path}")

        except Exception as e:
            record["status"] = "failed"
            record["message"] = str(e)

            print(f"Batch {batch_id} failed: {e}")
            print("Raw files are kept for inspection.")

        finally:
            record["end_time"] = datetime.now().isoformat(timespec="seconds")
            append_batch_status(record)

    print("\nRolling SEC batch download finished.")
    print(f"Batch status saved to: {BATCH_STATUS_CSV}")
    print(f"Permanent accession manifest saved to: {ROLLING_MANIFEST_CSV}")


def parse_args():
    parser = argparse.ArgumentParser(description="Rolling SEC filing batch downloader.")

    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--start-year", type=int, default=START_YEAR)
    parser.add_argument("--end-year", type=int, default=None)

    # Pilot testing only. Do not use for full run.
    parser.add_argument("--ticker-limit", type=int, default=None)

    parser.add_argument("--rebuild-universe", action="store_true")
    parser.add_argument("--download-txt-backup", action="store_true")
    parser.add_argument("--no-historical", action="store_true")
    parser.add_argument("--clear-batch-dir", action="store_true")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    run_rolling_batches(
        batch_size=args.batch_size,
        start_year=args.start_year,
        end_year=args.end_year,
        ticker_limit=args.ticker_limit,
        rebuild_universe=args.rebuild_universe,
        download_txt_backup=args.download_txt_backup,
        include_historical=not args.no_historical,
        clear_batch_dir_before_run=args.clear_batch_dir,
    )