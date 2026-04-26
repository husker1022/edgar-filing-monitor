# edgar-filing-monitor

## Project Overview

A Python CLI tool that fetches SEC EDGAR filings for any public company, summarizes them using the Claude API, and produces a JSON data file plus a human-readable HTML report.

## Architecture

```
edgar-filing-monitor/
├── CLAUDE.md
├── README.md
├── requirements.txt
├── main.py              # CLI entry point — prompts user, orchestrates the run
├── edgar.py             # EDGAR API client (search, fetch filings, extract text)
├── summarizer.py        # Claude API integration — summarizes filing text
├── reporter.py          # Renders JSON → HTML report
└── output/              # All run artifacts land here (gitignored)
    ├── <ticker>_<date>.json
    └── <ticker>_<date>.html
```

No database. No async framework. Flat files and JSON for storage.

## Key Design Decisions

- **No code changes required to add companies.** Company, timeframe, and filing type are all runtime inputs.
- **JSON is the data layer.** The HTML report is generated from JSON, so future tools can consume the same data.
- **One JSON file per run.** Named `<TICKER>_<YYYYMMDD_HHMMSS>.json` so multiple runs don't overwrite each other.
- **One HTML report per run.** Same naming convention as the JSON, generated from it immediately after fetch.
- **Filing text extraction is best-effort.** EDGAR filings vary in format (HTML, XML, plain text). Parse what we can; skip/truncate if a document is too large to summarize in one call.

## JSON Output Schema

```json
{
  "company": "Phreesia Inc.",
  "ticker": "PHR",
  "cik": "0001412408",
  "timeframe_days": 365,
  "filing_type_filter": "all",
  "run_timestamp": "2024-01-15T10:30:00Z",
  "filings": [
    {
      "accession_number": "0001428185-24-000012",
      "form_type": "10-K",
      "filed_date": "2024-01-10",
      "period_of_report": "2023-10-31",
      "filing_url": "https://www.sec.gov/...",
      "summary": "Annual report summary...",
      "key_bullets": [
        "Revenue increased 18% YoY to $XXXm",
        "..."
      ]
    }
  ]
}
```

## HTML Report Layout

One card per filing. Each card shows:
- Company name + ticker badge
- Filing type + date filed
- Summary paragraph
- Key bullet points list
- Link to the original EDGAR filing

Simple, clean styling — no external CSS frameworks, inline `<style>` block only so the file is fully self-contained.

## EDGAR API Notes

- Base URL: `https://data.sec.gov`
- Company search by ticker: `GET /submissions/CIK{cik}.json`
- Full-text search: `https://efts.sec.gov/LATEST/search-index?q=...`
- Filing document index: `https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/`
- User-Agent header **required**: `User-Agent: YourName yourname@email.com` — EDGAR blocks requests without it.
- Rate limit: stay under 10 requests/second.

## Claude API Usage

- Default model: `claude-sonnet-4-6`
- After the initial run completes, the user is prompted: "Re-summarize with Opus for higher quality? (y/n)". If yes, re-run summarization using `claude-opus-4-6` and overwrite the JSON and HTML with the upgraded results.
- Prompt instructs Claude to return a 2–3 sentence summary and 3–5 key bullet points.
- Response parsed as structured text (bullets prefixed with `-`).
- If filing text exceeds ~100k characters, truncate to first 80k before sending.

## Test Case

**Phreesia, Inc.** — ticker `PHR`, CIK `0001412408`

Use this company when developing and manually testing. It has a mix of filing types (10-K, 10-Q, 8-K) and a manageable filing volume.

## Environment Variables

```
ANTHROPIC_API_KEY=sk-...   # Required for Claude summarization
EDGAR_USER_AGENT=...       # "Name email@example.com" — sent in all EDGAR requests
```

## Running the Tool

```bash
python main.py
```

The CLI will prompt for:
1. Company ticker or name
2. Timeframe (30 / 60 / 90 / 365 / 730 days)
3. Filing type (all / 10-K / 10-Q / 8-K / etc.)

Output files are written to `output/`.

## Dependencies

- `anthropic` — Claude API client
- `requests` — HTTP calls to EDGAR
- `python-dotenv` — load `.env` for API keys
- `beautifulsoup4` — HTML filing text extraction
- Standard library only beyond the above (`json`, `datetime`, `os`, `re`)
