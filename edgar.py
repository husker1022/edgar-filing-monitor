"""EDGAR API client — search companies, fetch filings, extract text."""

import os
import re
import time
from datetime import date, timedelta

import anthropic
import requests
from bs4 import BeautifulSoup

_BASE_URL = "https://data.sec.gov"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def _headers() -> dict:
    user_agent = os.environ.get("EDGAR_USER_AGENT", "")
    if not user_agent:
        raise EnvironmentError("EDGAR_USER_AGENT environment variable is not set.")
    return {"User-Agent": user_agent}


def get_cik_by_ticker(ticker: str) -> str:
    """Look up a ticker symbol and return its CIK as a zero-padded 10-digit string."""
    ticker = ticker.upper().strip()
    response = requests.get(_TICKERS_URL, headers=_headers(), timeout=10)
    response.raise_for_status()

    # Response is a dict keyed by ordinal index; each value has cik_str, ticker, title.
    for entry in response.json().values():
        if entry["ticker"].upper() == ticker:
            return str(entry["cik_str"]).zfill(10)

    raise ValueError(f"Ticker '{ticker}' not found in EDGAR company list.")


def get_filings(cik: str, timeframe_days: int, filing_type: str) -> list[dict]:
    """Fetch recent filings for a CIK filtered by type and date range.

    Returns a list of dicts with keys:
        accession_number, form_type, filed_date, period_of_report, filing_url
    """
    cutoff = date.today() - timedelta(days=timeframe_days)
    filing_type = filing_type.strip().upper()

    url = f"{_BASE_URL}/submissions/CIK{cik}.json"
    response = requests.get(url, headers=_headers(), timeout=10)
    response.raise_for_status()
    data = response.json()

    # EDGAR may paginate older filings into separate files listed under data["files"].
    # Collect all recent filing batches; stop early once all dates fall before cutoff.
    recent = data.get("filings", {}).get("recent", {})
    batches = [recent]

    for extra in data.get("filings", {}).get("files", []):
        extra_url = f"{_BASE_URL}/submissions/{extra['name']}"
        time.sleep(0.15)  # stay well under 10 req/s
        r = requests.get(extra_url, headers=_headers(), timeout=10)
        r.raise_for_status()
        batches.append(r.json())

    results = []
    for batch in batches:
        accessions = batch.get("accessionNumber", [])
        forms      = batch.get("form", [])
        filed_dates = batch.get("filingDate", [])
        periods    = batch.get("reportDate", [])

        for acc, form, filed, period in zip(accessions, forms, filed_dates, periods):
            try:
                filed_date = date.fromisoformat(filed)
            except ValueError:
                continue

            if filed_date < cutoff:
                continue

            if filing_type != "ALL" and form.upper() != filing_type:
                continue

            # Build the EDGAR filing index URL from the accession number.
            acc_clean = acc.replace("-", "")
            filing_url = (
                f"https://www.sec.gov/Archives/edgar/data/"
                f"{int(cik)}/{acc_clean}/{acc}-index.htm"
            )

            results.append({
                "accession_number": acc,
                "form_type": form,
                "filed_date": filed,
                "period_of_report": period,
                "filing_url": filing_url,
            })

    return results


_MAX_CHARS = 680_000

# Pattern to identify 10-K section headers (Item N / Item NA style).
_SECTION_RE = re.compile(
    r"^[ \t]*((?:ITEM|Item)\s+\d+[A-Za-z]?\.?\s+[^\n]{3,80})[ \t]*$",
    re.MULTILINE,
)


def extract_filing_text(filing_url: str, form_type: str = "") -> str:
    """Fetch a filing's main document and return clean plain text.

    Parses the EDGAR index page to locate the primary document, then fetches
    and extracts text from HTML, XML, or plain-text content.

    For 10-K filings the extracted text is passed through an interactive
    section-triage step: Claude suggests which sections are boilerplate vs
    material, the user confirms or modifies, and only the kept sections are
    returned.  All other filing types are returned as-is (truncated to
    _MAX_CHARS characters).
    """
    r = requests.get(filing_url, headers=_headers(), timeout=15)
    r.raise_for_status()

    doc_url = _pick_primary_document(r.text, filing_url)
    if not doc_url:
        return ""

    time.sleep(0.15)
    doc_response = requests.get(doc_url, headers=_headers(), timeout=30)
    doc_response.raise_for_status()

    content_type = doc_response.headers.get("Content-Type", "")
    raw = doc_response.text

    if "html" in content_type or raw.lstrip().startswith("<"):
        text = _extract_text_from_html(raw)
    else:
        text = raw

    text = text.strip()

    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS]

    if form_type.upper() == "10-K":
        text = _triage_10k_sections(text)

    return text


def _triage_10k_sections(text: str) -> str:
    """Interactive section triage for 10-K filings.

    Scans the extracted text for Item-style section headers, asks Claude to
    classify each as material or boilerplate, prints the suggestions, prompts
    the user to confirm or toggle sections, then returns only the text from
    the kept sections.
    """
    spans = _find_section_spans(text)
    if not spans:
        print("[10-K triage] No section headers detected — returning full text.")
        return text

    headers = [name for name, _, _ in spans]
    classifications = _claude_classify_sections(headers)

    print("\n" + "=" * 70)
    print("10-K SECTION TRIAGE — Claude's suggestions")
    print("=" * 70)
    print(f"{'#':<4} {'Status':<12} Section")
    print("-" * 70)
    keep_flags = []
    for i, (name, _, _) in enumerate(spans):
        label = classifications.get(name, "material")
        keep = label.lower() != "boilerplate"
        keep_flags.append(keep)
        status = "[ KEEP  ]" if keep else "[  SKIP ]"
        print(f"{i+1:<4} {status:<12} {name}")
    print("=" * 70)
    print("\nPress Enter to accept, or type section numbers to toggle (e.g. '3 7 12'):")
    user_input = input("> ").strip()

    if user_input:
        for token in user_input.split():
            if token.isdigit():
                idx = int(token) - 1
                if 0 <= idx < len(keep_flags):
                    keep_flags[idx] = not keep_flags[idx]

    print("\nFinal selection:")
    for i, (name, _, _) in enumerate(spans):
        status = "KEEP" if keep_flags[i] else "SKIP"
        print(f"  [{status}] {name}")
    print()

    parts = []
    for (name, start, end), keep in zip(spans, keep_flags):
        if keep:
            parts.append(text[start:end].strip())

    return "\n\n".join(parts) if parts else text


def _find_section_spans(text: str) -> list[tuple[str, int, int]]:
    """Return (header_name, start, end) for each unique 10-K section.

    When a header appears more than once (table of contents + actual section),
    the last occurrence is used as the real section start.
    """
    # Normalise an item label to its canonical number for deduplication.
    def _item_key(header: str) -> str:
        m = re.match(r"(?:ITEM|Item)\s+(\d+[A-Za-z]?)", header, re.IGNORECASE)
        return m.group(1).upper() if m else header

    # Collect all matches with positions.
    all_matches: list[tuple[str, int]] = [
        (m.group(1).strip(), m.start()) for m in _SECTION_RE.finditer(text)
    ]

    # Deduplicate: keep the last occurrence of each item number.
    seen: dict[str, tuple[str, int]] = {}
    for name, pos in all_matches:
        seen[_item_key(name)] = (name, pos)

    # Sort by position and build spans.
    ordered = sorted(seen.values(), key=lambda x: x[1])
    spans = []
    for i, (name, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else len(text)
        spans.append((name, start, end))

    return spans


def _claude_classify_sections(headers: list[str]) -> dict[str, str]:
    """Ask Claude to classify each 10-K section header as material or boilerplate.

    Returns a dict mapping each header string to "material" or "boilerplate".
    """
    header_list = "\n".join(f"- {h}" for h in headers)
    prompt = (
        "You are reviewing the section headers from a 10-K annual report filing. "
        "Classify each section as either 'material' (contains substantive company-specific "
        "information an analyst would want to read) or 'boilerplate' (routine disclosures, "
        "legal formalities, or standard exhibits with little analytical value).\n\n"
        "Respond with exactly one line per section in this format:\n"
        "<section header> | material\n"
        "<section header> | boilerplate\n\n"
        "Sections to classify:\n"
        f"{header_list}"
    )

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response_text = stream.get_final_message().content[0].text

    result = {}
    for line in response_text.splitlines():
        if "|" in line:
            parts = line.split("|", 1)
            name = parts[0].strip().lstrip("-").strip()
            label = parts[1].strip().lower()
            if "boilerplate" in label:
                result[name] = "boilerplate"
            else:
                result[name] = "material"

    return result


def _pick_primary_document(index_html: str, filing_url: str) -> str | None:
    """Parse the EDGAR filing index page and return the URL of the primary document.

    The index page has a table with columns: Seq, Description, Document, Type, Size.
    The primary document is the first row whose Type matches the form type (e.g. 10-K),
    falling back to the first .htm/.txt file listed.
    """
    base_url = filing_url.rsplit("/", 1)[0]
    soup = BeautifulSoup(index_html, "html.parser")

    # The form type appears in a <type> tag or in a description cell.
    # More reliably, read it from the page header table.
    form_type = ""
    for row in soup.select("table tr"):
        cells = row.find_all("td")
        if len(cells) >= 2 and "form type" in cells[0].get_text().lower():
            form_type = cells[1].get_text().strip().upper()
            break

    # The document table has id="documentsTable" or class "tableFile".
    doc_table = soup.find("table", {"summary": re.compile(r"Document", re.I)})
    if not doc_table:
        return None

    first_htm = None
    for row in doc_table.find_all("tr")[1:]:  # skip header row
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        doc_type = cells[3].get_text().strip().upper()
        link = cells[2].find("a", href=True)
        if not link:
            continue
        href = link["href"]
        filename = href.split("/")[-1]
        if not filename.endswith((".htm", ".html", ".txt")):
            continue

        # Strip iXBRL viewer wrapper: /ix?doc=/Archives/...
        if href.startswith("/ix?doc="):
            href = href[len("/ix?doc="):]

        full_url = f"https://www.sec.gov{href}" if href.startswith("/") else f"{base_url}/{filename}"

        if form_type and doc_type == form_type:
            return full_url

        if first_htm is None:
            first_htm = full_url

    return first_htm


def _extract_text_from_html(raw: str) -> str:
    """Strip HTML/XML tags and collapse whitespace into readable plain text.

    Handles iXBRL documents by removing hidden XBRL metadata blocks while
    preserving the text content of inline XBRL value tags.
    """
    soup = BeautifulSoup(raw, "html.parser")

    # Remove script, style, and hidden elements.
    for tag in soup(["script", "style", "meta", "link"]):
        tag.decompose()

    # iXBRL: remove the hidden header/metadata block entirely.
    for tag in soup.find_all(re.compile(r"^ix:header$|^ix:hidden$", re.I)):
        tag.decompose()

    # iXBRL: unwrap inline value tags — keep their text, drop the tag wrapper.
    for tag in soup.find_all(re.compile(r"^ix:", re.I)):
        tag.unwrap()

    text = soup.get_text(separator="\n")
    # Collapse runs of blank lines to at most two.
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Collapse horizontal whitespace.
    text = re.sub(r"[ \t]+", " ", text)
    return text
