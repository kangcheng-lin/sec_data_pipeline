import argparse
import json
import shutil
import subprocess
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
    read_rolling_manifest,
    write_rolling_manifest,
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

def parse_year_month_to_date(year: int | None, month: int | None) -> str | None:
    """
    Convert --start-year/--start-month into YYYY-MM-01.
    If month is omitted, returns YYYY-01-01.
    """
    if year is None:
        return None
    if month is None:
        month = 1
    if not 1 <= month <= 12:
        raise ValueError("--start-month must be between 1 and 12.")
    return f"{year:04d}-{month:02d}-01"


def load_or_build_universe(
    rebuild_universe: bool = False,
    ticker_limit: int | None = None,
) -> pd.DataFrame:
    if FINAL_UNIVERSE_CSV.exists() and not rebuild_universe:
        print(f"Loading existing universe: {FINAL_UNIVERSE_CSV}")
        universe = pd.read_csv(
            FINAL_UNIVERSE_CSV,
            dtype=str,
            keep_default_na=False,
            na_filter=False,
        )
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
        old = pd.read_csv(
            BATCH_STATUS_CSV,
            dtype=str,
            keep_default_na=False,
            na_filter=False,
        )
        out = pd.concat([old, row], ignore_index=True)
    else:
        out = row

    out.to_csv(BATCH_STATUS_CSV, index=False)


def run_command(command: list[str], log_path: Path) -> tuple[bool, str]:
    """
    Run one extractor command and save stdout/stderr.
    """
    print("\nRunning command:")
    print(" ".join(command))

    completed = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        shell=False,
    )

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_text = (
        f"COMMAND:\n{' '.join(command)}\n\n"
        f"RETURN CODE:\n{completed.returncode}\n\n"
        f"STDOUT:\n{completed.stdout}\n\n"
        f"STDERR:\n{completed.stderr}\n"
    )
    log_path.write_text(log_text, encoding="utf-8")

    if completed.returncode == 0:
        return True, f"success | log={log_path}"

    return False, f"failed | returncode={completed.returncode} | log={log_path}"


def run_extractors_for_batch(batch_dir: Path, batch_id: int) -> tuple[bool, dict]:
    """
    Run all three extractors. Raw files are deleted only if all succeed.
    """
    batch_id_text = f"{batch_id:05d}"

    extractors = [
        (
            "balance_sheet",
            [
                "python",
                "src/extract_balance_sheet_from_filings_ticker_files.py",
                "--mode",
                "batch",
                "--input-dir",
                str(batch_dir),
                "--batch-id",
                batch_id_text,
            ],
        ),
        (
            "cash_flow",
            [
                "python",
                "src/extract_cash_flow_from_filings_ticker_files.py",
                "--mode",
                "batch",
                "--input-dir",
                str(batch_dir),
                "--batch-id",
                batch_id_text,
            ],
        ),
        (
            "income_statement",
            [
                "python",
                "src/extract_income_statement_from_filings_ticker_files.py",
                "--mode",
                "batch",
                "--input-dir",
                str(batch_dir),
                "--batch-id",
                batch_id_text,
            ],
        ),
    ]

    results = {}

    for name, command in extractors:
        log_path = BATCH_LOG_DIR / f"batch_{batch_id_text}_{name}_extraction_log.txt"
        ok, message = run_command(command, log_path)
        results[name] = message

        if not ok:
            return False, results

    return True, results


def mark_batch_manifest_processed(
    batch_id: int,
    manifest_path: Path = ROLLING_MANIFEST_CSV,
) -> int:
    """
    Mark all accessions from this batch as processed after successful extraction.
    """
    df = read_rolling_manifest(manifest_path)
    if df.empty or "batch_id" not in df.columns:
        return 0

    mask = df["batch_id"].astype(str) == str(batch_id)
    n = int(mask.sum())

    if n == 0:
        return 0

    now = datetime.now().isoformat(timespec="seconds")

    df.loc[mask, "pipeline_status"] = "processed"
    df.loc[mask, "raw_file_available"] = "False"
    df.loc[mask, "raw_file_deleted"] = "True"
    df.loc[mask, "processed_at"] = now

    write_rolling_manifest(df, manifest_path)
    return n


# ============================================================
# MAIN PIPELINE
# ============================================================

def run_rolling_batches(
    batch_size: int = BATCH_SIZE,
    start_year: int | None = START_YEAR,
    start_month: int | None = None,
    end_year: int | None = END_YEAR,
    end_month: int | None = None,
    ticker_limit: int | None = None,
    rebuild_universe: bool = False,
    download_txt_backup: bool = False,
    include_historical: bool = True,
    clear_batch_dir_before_run: bool = False,
    extract_after_download: bool = False,
    delete_after_extract: bool = False,
):
    start_date = parse_year_month_to_date(start_year, start_month)
    end_date = parse_year_month_to_date(end_year, end_month)

    universe = load_or_build_universe(
        rebuild_universe=rebuild_universe,
        ticker_limit=ticker_limit,
    )

    print("\nRolling SEC batch pipeline started.")
    print(f"Universe size: {len(universe)}")
    print(f"Batch size: {batch_size}")
    print(f"Start date inclusive: {start_date}")
    print(f"End date inclusive: {end_date}")
    print(f"Raw temporary folder: {ROLLING_RAW_DIR}")
    print(f"Permanent manifest: {ROLLING_MANIFEST_CSV}")
    print(f"Download TXT backup: {download_txt_backup}")
    print(f"Extract after download: {extract_after_download}")
    print(f"Delete after successful extraction: {delete_after_extract}")

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

            # Requires download_sec_filings_rolling_batch.py to support start_date/end_date.
            metadata = download_pending_reports_for_universe(
                universe=batch_df,
                forms=FORMS,
                output_dir=batch_dir,
                start_year=start_year,
                end_year=end_year,
                start_date=start_date,
                end_date=end_date,
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
            record["status"] = "download_completed"
            record["message"] = f"Download completed. Metadata: {metadata_path}"

            print(f"Batch {batch_id} download completed.")
            print(f"Raw size: {raw_size_gb:.4f} GB")
            print(f"Downloaded filing records: {num_downloaded}")
            print(f"Metadata saved to: {metadata_path}")

            if extract_after_download:
                if num_downloaded == 0:
                    print("No newly downloaded filings. Skipping extraction/deletion for this batch.")
                    record["status"] = "completed_no_new_downloads"
                    record["message"] += " | No new downloads; extraction skipped."
                else:
                    extract_ok, extract_results = run_extractors_for_batch(batch_dir, batch_id)
                    extraction_summary_path = BATCH_LOG_DIR / f"batch_{batch_id:05d}_extraction_summary.json"
                    extraction_summary_path.write_text(
                        json.dumps(extract_results, indent=2),
                        encoding="utf-8",
                    )

                    if extract_ok:
                        n_marked = mark_batch_manifest_processed(batch_id)
                        record["status"] = "extraction_completed"
                        record["message"] += (
                            f" | Extraction completed. "
                            f"Manifest rows marked processed: {n_marked}. "
                            f"Extraction summary: {extraction_summary_path}"
                        )

                        if delete_after_extract:
                            print(f"Deleting raw batch folder after successful extraction: {batch_dir}")
                            shutil.rmtree(batch_dir)
                            record["status"] = "processed_and_deleted"
                            record["message"] += " | Raw batch folder deleted."
                    else:
                        record["status"] = "extraction_failed"
                        record["message"] += (
                            f" | Extraction failed. Raw files kept. "
                            f"Extraction summary: {extraction_summary_path}"
                        )
                        print("Extraction failed. Raw files are kept for debugging.")

            elif delete_after_extract:
                print("WARNING: --delete-after-extract was requested but --extract-after-download was not set. No deletion performed.")
                record["message"] += " | Delete requested without extraction; no deletion performed."

        except TypeError as e:
            # Usually means download_sec_filings_rolling_batch.py has not yet been patched
            # to accept start_date/end_date.
            record["status"] = "failed"
            record["message"] = (
                "TypeError, likely because download_sec_filings_rolling_batch.py "
                "does not yet support start_date/end_date arguments. "
                f"Original error: {e}"
            )
            print(record["message"])
            print("Raw files are kept for inspection.")

        except Exception as e:
            record["status"] = "failed"
            record["message"] = str(e)
            print(f"Batch {batch_id} failed: {e}")
            print("Raw files are kept for inspection.")

        finally:
            record["end_time"] = datetime.now().isoformat(timespec="seconds")
            append_batch_status(record)

    print("\nRolling SEC batch pipeline finished.")
    print(f"Batch status saved to: {BATCH_STATUS_CSV}")
    print(f"Permanent accession manifest saved to: {ROLLING_MANIFEST_CSV}")


def parse_args():
    parser = argparse.ArgumentParser(description="Rolling SEC filing batch pipeline.")

    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--start-year", type=int, default=START_YEAR)
    parser.add_argument("--start-month", type=int, default=None)
    parser.add_argument("--end-year", type=int, default=None)
    parser.add_argument("--end-month", type=int, default=None)

    # Pilot testing only. Do not use for full run.
    parser.add_argument("--ticker-limit", type=int, default=None)

    parser.add_argument("--rebuild-universe", action="store_true")
    parser.add_argument("--download-txt-backup", action="store_true")
    parser.add_argument("--no-historical", action="store_true")
    parser.add_argument("--clear-batch-dir", action="store_true")

    parser.add_argument("--extract-after-download", action="store_true")
    parser.add_argument("--delete-after-extract", action="store_true")

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    run_rolling_batches(
        batch_size=args.batch_size,
        start_year=args.start_year,
        start_month=args.start_month,
        end_year=args.end_year,
        end_month=args.end_month,
        ticker_limit=args.ticker_limit,
        rebuild_universe=args.rebuild_universe,
        download_txt_backup=args.download_txt_backup,
        include_historical=not args.no_historical,
        clear_batch_dir_before_run=args.clear_batch_dir,
        extract_after_download=args.extract_after_download,
        delete_after_extract=args.delete_after_extract,
    )
