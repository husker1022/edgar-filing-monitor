"""CLI entry point — prompts user and orchestrates the run."""

import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from edgar import get_cik_by_ticker, get_filings, extract_filing_text
from reporter import generate_html
from summarizer import summarize_filing

# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

_TIMEFRAME_OPTIONS = {
    "1": 30,
    "2": 60,
    "3": 90,
    "4": 365,
    "5": 730,
}

_FILING_TYPE_OPTIONS = {
    "1": "all",
    "2": "10-K",
    "3": "10-Q",
    "4": "8-K",
    "5": "DEF 14A",
    "6": "S-1",
}


def _prompt(prompt_text: str, *, default: str = "") -> str:
    try:
        value = input(prompt_text).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(0)
    return value if value else default


def _prompt_ticker() -> str:
    while True:
        ticker = _prompt("Enter company ticker (e.g. PHR): ").upper()
        if ticker:
            return ticker
        print("  Ticker cannot be empty.")


def _prompt_timeframe() -> int:
    print("\nSelect timeframe:")
    for k, v in _TIMEFRAME_OPTIONS.items():
        label = f"{v} days" if v < 365 else (f"{v // 365} year" + ("s" if v > 365 else ""))
        print(f"  {k}. {label}")
    while True:
        choice = _prompt("Choice [4]: ", default="4")
        if choice in _TIMEFRAME_OPTIONS:
            return _TIMEFRAME_OPTIONS[choice]
        print("  Please enter a number between 1 and 5.")


def _prompt_filing_type() -> str:
    print("\nSelect filing type:")
    for k, v in _FILING_TYPE_OPTIONS.items():
        print(f"  {k}. {v}")
    while True:
        choice = _prompt("Choice [1]: ", default="1")
        if choice in _FILING_TYPE_OPTIONS:
            return _FILING_TYPE_OPTIONS[choice]
        print("  Please enter a number between 1 and 6.")


# ---------------------------------------------------------------------------
# Core run
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = "claude-sonnet-4-6"
_OPUS_MODEL    = "claude-opus-4-6"


def _run(ticker: str, timeframe_days: int, filing_type: str, model: str) -> dict:
    """Fetch, extract, summarize, and return the full run dict."""

    # 1. Resolve CIK
    print(f"\n[1/4] Looking up CIK for {ticker}...")
    cik = get_cik_by_ticker(ticker)
    print(f"      CIK: {cik}")

    # 2. Fetch filing list
    type_label = filing_type if filing_type != "all" else "all types"
    print(f"\n[2/4] Fetching filings ({type_label}, last {timeframe_days} days)...")
    filings = get_filings(cik, timeframe_days, filing_type)
    if not filings:
        print("      No filings found for these criteria.")
        return {}
    print(f"      Found {len(filings)} filing(s).")

    # 3. Extract text + summarize each filing
    print(f"\n[3/4] Extracting and summarizing with {model}...")
    enriched = []
    for i, filing in enumerate(filings, 1):
        form   = filing["form_type"]
        dated  = filing["filed_date"]
        print(f"      [{i}/{len(filings)}] {form} filed {dated} — extracting text...")
        text = extract_filing_text(filing["filing_url"], form_type=form)
        if not text:
            print(f"             Could not extract text, skipping.")
            continue
        print(f"             {len(text):,} chars — summarizing...")
        summary = summarize_filing(text, model=model)
        enriched.append({**filing, **summary})
        print(f"             Done.")

    # Build the run dict
    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "company": _resolve_company_name(cik, ticker),
        "ticker": ticker,
        "cik": cik,
        "timeframe_days": timeframe_days,
        "filing_type_filter": filing_type,
        "run_timestamp": run_ts,
        "filings": enriched,
    }


def _resolve_company_name(cik: str, ticker: str) -> str:
    """Best-effort company name from EDGAR; fall back to ticker."""
    try:
        import requests, os as _os
        r = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            headers={"User-Agent": _os.environ.get("EDGAR_USER_AGENT", "")},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("name", ticker)
    except Exception:
        return ticker


def _output_prefix(ticker: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    os.makedirs("output", exist_ok=True)
    return os.path.join("output", f"{ticker}_{ts}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("  EDGAR Filing Monitor")
    print("=" * 60)

    ticker       = _prompt_ticker()
    timeframe    = _prompt_timeframe()
    filing_type  = _prompt_filing_type()

    data = _run(ticker, timeframe, filing_type, model=_DEFAULT_MODEL)
    if not data or not data.get("filings"):
        print("\nNo data to write. Exiting.")
        return

    prefix = _output_prefix(ticker)
    print(f"\n[4/4] Writing output files...")
    generate_html(data, prefix)
    print(f"      JSON : {prefix}.json")
    print(f"      HTML : {prefix}.html")

    # Opus upgrade prompt
    print("\n" + "-" * 60)
    upgrade = _prompt(
        "Re-summarize with claude-opus-4-6 for higher quality? (y/N): ",
        default="n",
    ).lower()

    if upgrade == "y":
        print(f"\nRe-summarizing {len(data['filings'])} filing(s) with {_OPUS_MODEL}...")
        upgraded_filings = []
        for i, filing in enumerate(data["filings"], 1):
            form  = filing["form_type"]
            dated = filing["filed_date"]
            print(f"  [{i}/{len(data['filings'])}] {form} filed {dated} — extracting text...")
            text = extract_filing_text(filing["filing_url"], form_type=form)
            if not text:
                upgraded_filings.append(filing)
                continue
            print(f"      {len(text):,} chars — summarizing with Opus...")
            summary = summarize_filing(text, model=_OPUS_MODEL)
            upgraded_filings.append({**filing, **summary})
            print(f"      Done.")

        data["filings"] = upgraded_filings
        data["run_timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        print(f"\nOverwriting output files with Opus summaries...")
        generate_html(data, prefix)
        print(f"  JSON : {prefix}.json")
        print(f"  HTML : {prefix}.html")

    print("\nDone. Open the HTML file in your browser to review the report.")
    print(f"  {prefix}.html")


if __name__ == "__main__":
    main()
