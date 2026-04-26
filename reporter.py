"""Renders the run JSON into a self-contained HTML report."""

import html
import json
import os

import markdown as md


def generate_html(data: dict, output_path: str) -> None:
    """Write a JSON file and a self-contained HTML report for a completed run.

    Args:
        data:        The full run dict (matches the schema in CLAUDE.md).
        output_path: Path prefix without extension, e.g. 'output/PHR_20260412_103000'.
                     This function writes <output_path>.json and <output_path>.html.
    """
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    json_path = output_path + ".json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    html_path = output_path + ".html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(_render_html(data))


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_STYLE = """
* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #f4f6f9;
  color: #1a1a2e;
  padding: 32px 16px;
  line-height: 1.6;
}

header {
  max-width: 860px;
  margin: 0 auto 32px;
}

header h1 {
  font-size: 1.6rem;
  font-weight: 700;
  margin-bottom: 6px;
}

header .meta {
  font-size: 0.875rem;
  color: #555;
}

.card {
  background: #fff;
  border-radius: 10px;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08);
  max-width: 860px;
  margin: 0 auto 24px;
  padding: 24px 28px;
}

.card-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.company-name {
  font-size: 1.1rem;
  font-weight: 700;
  flex: 1 1 auto;
}

.badge {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  white-space: nowrap;
}

.badge-ticker {
  background: #e8f0fe;
  color: #1a56db;
}

.badge-form {
  background: #fef3c7;
  color: #92400e;
}

.filing-meta {
  font-size: 0.82rem;
  color: #666;
  margin-bottom: 14px;
}

.summary {
  font-size: 0.95rem;
  margin-bottom: 16px;
  color: #222;
}

.bullets {
  margin-bottom: 18px;
}

.bullets ul {
  list-style: none;
  padding: 0;
  margin: 0;
}

.bullets ul li {
  font-size: 0.9rem;
  padding: 5px 0 5px 20px;
  position: relative;
  color: #333;
  border-bottom: 1px solid #f0f0f0;
}

.bullets ul li:last-child { border-bottom: none; }

.bullets ul li::before {
  content: "▸";
  position: absolute;
  left: 0;
  color: #1a56db;
  font-size: 0.75rem;
  top: 7px;
}

.filing-link {
  display: inline-block;
  font-size: 0.82rem;
  color: #1a56db;
  text-decoration: none;
  border: 1px solid #c7d7f9;
  border-radius: 6px;
  padding: 4px 12px;
}

.filing-link:hover { background: #e8f0fe; }

.no-filings {
  max-width: 860px;
  margin: 0 auto;
  text-align: center;
  color: #888;
  padding: 48px;
  background: #fff;
  border-radius: 10px;
}
"""


def _render_html(data: dict) -> str:
    company  = html.escape(data.get("company", "Unknown Company"))
    ticker   = html.escape(data.get("ticker", "").upper())
    timeframe = data.get("timeframe_days", "")
    filter_  = html.escape(data.get("filing_type_filter", "all"))
    run_ts   = html.escape(data.get("run_timestamp", ""))
    filings  = data.get("filings", [])

    cards_html = ""
    if not filings:
        cards_html = '<div class="no-filings">No filings found for this run.</div>'
    else:
        for filing in filings:
            cards_html += _render_card(company, ticker, filing)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{company} ({ticker}) — EDGAR Filing Report</title>
  <style>{_STYLE}</style>
</head>
<body>
  <header>
    <h1>{company} <span class="badge badge-ticker">{ticker}</span></h1>
    <div class="meta">
      Last {timeframe} days &nbsp;·&nbsp; Filing type: {filter_} &nbsp;·&nbsp;
      {len(filings)} filing{"s" if len(filings) != 1 else ""} found &nbsp;·&nbsp;
      Run: {run_ts}
    </div>
  </header>
  {cards_html}
</body>
</html>"""


def _render_card(company: str, ticker: str, filing: dict) -> str:
    form_type   = html.escape(filing.get("form_type", ""))
    filed_date  = html.escape(filing.get("filed_date", ""))
    period      = html.escape(filing.get("period_of_report", ""))
    filing_url  = html.escape(filing.get("filing_url", "#"))
    bullets     = filing.get("key_bullets", [])

    period_str = f" &nbsp;·&nbsp; Period: {period}" if period else ""

    # Convert summary markdown to HTML (produces a <p> block).
    summary_md = filing.get("summary", "No summary available.")
    summary_html = md.markdown(summary_md)

    # Render bullets as a markdown list then convert, so bold/italic in bullets works.
    bullets_md = "\n".join(f"- {b}" for b in bullets)
    bullets_html = md.markdown(bullets_md)

    return f"""
  <div class="card">
    <div class="card-header">
      <span class="company-name">{company}</span>
      <span class="badge badge-ticker">{ticker}</span>
      <span class="badge badge-form">{form_type}</span>
    </div>
    <div class="filing-meta">Filed: {filed_date}{period_str}</div>
    <div class="summary">{summary_html}</div>
    <div class="bullets">{bullets_html}</div>
    <a class="filing-link" href="{filing_url}" target="_blank" rel="noopener">
      View original filing ↗
    </a>
  </div>"""
