"""Durable SEC filing queue for Codex briefing cards; no paid model API required.

Discovery/downloads are Python jobs. The Codex task reads the prepared source
chunks, writes evidence-backed card JSON, and publishes validated Markdown.
Nothing in this module creates a schedule, sends email, or calls a model.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, parse_qs
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

CIK = "0001412408"
SUBMISSIONS = f"https://data.sec.gov/submissions/CIK{CIK}.json"
ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")


def now():
    return datetime.now(timezone.utc)


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("A timestamp must include its timezone")
    return result.astimezone(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def normalize(value):
    return " ".join(value.split())


class SecClient:
    """One request/second, identified requests, finite backoff, no block bypass."""
    def __init__(self, user_agent):
        if not user_agent or "@" not in user_agent:
            raise ValueError("Set EDGAR_USER_AGENT to an application name and contact email")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.last_request = 0.0

    def get(self, url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"www.sec.gov", "data.sec.gov"}:
            raise ValueError(f"Unapproved SEC document URL: {url}")
        for attempt in range(3):
            time.sleep(max(0, 1.0 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.session.get(url, timeout=(10, 60), allow_redirects=False)
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise
                time.sleep(2 ** (attempt + 1))
                continue
            if response.status_code == 403:
                raise RuntimeError(f"SEC access denied (403): {url}; stop and retry in a later run")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 2:
                    response.raise_for_status()
                retry = response.headers.get("Retry-After", "")
                delay = int(retry) if retry.isdigit() else 2 ** (attempt + 1)
                if delay > 30:
                    raise RuntimeError(f"SEC requested a later retry ({delay}s): {url}")
                time.sleep(max(1, delay))
                continue
            if 300 <= response.status_code < 400:
                raise RuntimeError(f"Unexpected SEC redirect; review destination: {url}")
            response.raise_for_status()
            return response
        raise RuntimeError("SEC request exhausted retries")

    def json(self, url):
        return self.get(url).json()


class State:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "state.sqlite", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS filings (
                accession TEXT PRIMARY KEY, metadata TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'discovered', packet TEXT,
                card TEXT, markdown TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                published_reference TEXT, delivered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, started TEXT NOT NULL,
                completed TEXT, status TEXT NOT NULL, error TEXT
            );
        """)

    def setting(self, key):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    def initialize(self, since, preview=False):
        if self.setting("start_at"):
            raise ValueError("Already initialized; refusing to reset the monitoring baseline")
        with self.db:
            self.set("start_at", stamp(instant(since)))
            self.set("mode", "preview" if preview else "production")

    def row(self, accession):
        row = self.db.execute("SELECT * FROM filings WHERE accession=?", (accession,)).fetchone()
        if not row:
            raise ValueError(f"Unknown accession: {accession}")
        return row


def batch_filings(batch):
    keys = ("accessionNumber", "form", "filingDate", "primaryDocument")
    if any(key not in batch for key in keys):
        raise ValueError("Missing inventory columns; checkpoint not advanced")
    size = len(batch.get("accessionNumber", []))
    if any(len(batch.get(key, [])) != size for key in keys):
        raise ValueError("Incomplete submissions inventory; checkpoint not advanced")
    optional = ("acceptanceDateTime", "reportDate")
    for key in optional:
        if key in batch and len(batch[key]) != size:
            raise ValueError(f"Malformed inventory column: {key}")
    for i in range(size):
        accession = batch["accessionNumber"][i]
        if not ACCESSION.fullmatch(accession):
            raise ValueError(f"Invalid accession in inventory: {accession}")
        accepted = batch.get("acceptanceDateTime", [""] * size)[i]
        # Historical archive batches may omit acceptance timestamps. Keep the
        # whole filing day eligible instead of silently dropping those records.
        if accepted:
            accepted = stamp(instant(accepted))
        yield {
            "accession": accession, "form": batch["form"][i],
            "filed_date": batch["filingDate"][i], "accepted_at": accepted,
            "report_date": batch.get("reportDate", [""] * size)[i],
            "primary_document": batch["primaryDocument"][i],
            "filing_url": f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{accession.replace('-', '')}/{accession}-index.html",
        }


def discover(state, client, cutoff=None):
    start = state.setting("start_at")
    if not start:
        raise ValueError("Initialize a baseline before discovery")
    cutoff = cutoff or now()
    checkpoint = state.setting("discovery_checkpoint") or start
    if instant(checkpoint) > cutoff:
        raise ValueError("Scan cutoff precedes the saved checkpoint")
    lower = max(instant(start), instant(checkpoint) - timedelta(days=7))
    with state.db:
        run_id = state.db.execute("INSERT INTO runs(started,status) VALUES (?,?)", (stamp(cutoff), "running")).lastrowid
    try:
        submissions = client.json(SUBMISSIONS)
        if int(submissions.get("cik", -1)) != int(CIK):
            raise ValueError("Submissions response is not Phreesia")
        filing_data = submissions["filings"]
        batches = [filing_data["recent"]]
        for archive in filing_data.get("files", []):
            if archive["filingTo"] < lower.date().isoformat() or archive["filingFrom"] > cutoff.date().isoformat():
                continue
            name = archive["name"]
            if not re.fullmatch(r"CIK\d{10}-submissions-\d+\.json", name):
                raise ValueError("Unexpected archive filename")
            batches.append(client.json(f"https://data.sec.gov/submissions/{name}"))
        found = []
        for batch in batches:
            for filing in batch_filings(batch):
                if filing["accepted_at"]:
                    in_range = lower <= instant(filing["accepted_at"]) <= cutoff
                else:
                    in_range = lower.date().isoformat() <= filing["filed_date"] <= cutoff.date().isoformat()
                if in_range:
                    found.append(filing)
        # Inventory and checkpoint commit together. Network failures above
        # cannot falsely move the last successful discovery time forward.
        inserted = 0
        with state.db:
            for filing in found:
                inserted += state.db.execute(
                    "INSERT OR IGNORE INTO filings(accession,metadata) VALUES (?,?)",
                    (filing["accession"], json.dumps(filing)),
                ).rowcount
            state.set("discovery_checkpoint", stamp(cutoff))
            state.db.execute("UPDATE runs SET completed=?,status='success' WHERE id=?", (stamp(now()), run_id))
        return {"new_filings": inserted, "from": stamp(lower), "through": stamp(cutoff)}
    except Exception as exc:
        with state.db:
            state.db.execute("UPDATE runs SET completed=?,status='failed',error=? WHERE id=?", (stamp(now()), str(exc), run_id))
        raise


def clean_sec_url(href, base):
    if href.startswith("/ix?doc=") or href.startswith("/ixviewer/doc/action?doc="):
        href = parse_qs(urlparse(href).query)["doc"][0]
    url = urljoin(base, href)
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "www.sec.gov" or not parsed.path.startswith("/Archives/"):
        raise ValueError("Document link is outside the SEC archive")
    return url


def pick_documents(index_html, filing):
    soup = BeautifulSoup(index_html, "html.parser")
    table = soup.find("table", summary=re.compile("Document", re.I))
    if table is None:
        raise ValueError("SEC document table not found")
    records = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        link = cells[2].find("a", href=True)
        if not link:
            continue
        kind = cells[3].get_text(" ", strip=True)
        # Keep all exhibits, including certification exhibits. The model may
        # summarize them briefly but the downloader must not prejudge materiality.
        if kind == filing["form"] or kind.startswith("EX-"):
            url = clean_sec_url(link["href"], filing["filing_url"])
            records.append({"url": url, "type": kind, "description": cells[1].get_text(" ", strip=True)})
    if not any(doc["type"] == filing["form"] for doc in records):
        raise ValueError("Primary document not present in SEC document table")
    # Ownership pages can list both a rendered and raw XML version. One copy
    # of the same raw document suffices; XML retains field labels and footnotes.
    unique = {}
    for doc in records:
        url = re.sub(r"/xsl[^/]+/", "/", doc["url"])
        doc["url"] = url
        unique[url] = doc
    return list(unique.values())


def extract_text(content, url):
    if content.startswith(b"%PDF") or urlparse(url).path.lower().endswith(".pdf"):
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("PDF requires pypdf; install requirements-monitor.txt") from exc
        pages = []
        for i, page in enumerate(PdfReader(io.BytesIO(content)).pages, 1):
            text = page.extract_text() or ""
            if not text.strip():
                raise ValueError(f"PDF page {i} has no extractable text; requires visual/OCR review")
            pages.append(f"[Page {i}]\n{text}")
        return "\n\n".join(pages), []
    raw = content.decode("utf-8-sig", errors="replace")
    if "\ufffd" in raw:
        raise ValueError("Document has invalid UTF-8; requires encoding review")
    if urlparse(url).path.lower().endswith(".xml"):
        root = ET.fromstring(raw)
        lines = []
        def walk(element, prefix=""):
            tag = element.tag.split("}")[-1]
            path = f"{prefix}/{tag}" if prefix else tag
            # Include footnote IDs and codes so values remain interpretable.
            attributes = " ".join(f"{k}={v}" for k, v in element.attrib.items())
            if element.text and element.text.strip():
                lines.append(f"{path}{' [' + attributes + ']' if attributes else ''}: {element.text.strip()}")
            elif attributes:
                lines.append(f"{path}: {attributes}")
            for child in element:
                walk(child, path)
        walk(root)
        return "\n".join(lines), []
    if not raw.lstrip().startswith("<"):
        return raw.strip(), []
    soup = BeautifulSoup(raw, "html.parser")
    images = [urljoin(url, image.get("src", "")) for image in soup.find_all("img") if image.get("src")]
    for tag in list(soup.find_all(["script", "style", "meta", "link", "ix:header", "ix:hidden"])):
        if tag.parent is not None:
            tag.decompose()
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if cells:
            row.replace_with("\n" + " | ".join(cell.get_text(" ", strip=True) for cell in cells) + "\n")
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text), sorted(set(images))


def chunks(text, size=24000):
    # Every character is preserved, including long tables and later sections.
    return [text[start:start + size] for start in range(0, len(text), size)]


def prepare(state, client, accession):
    row = state.row(accession)
    if row["status"] in {"ready", "delivered"}:
        raise ValueError("Refusing to overwrite a prepared or delivered card")
    filing = json.loads(row["metadata"])
    folder = state.root / "filings" / accession
    folder.mkdir(parents=True, exist_ok=True)
    packet = {"filing": filing, "documents": [], "issues": [], "prepared_at": stamp(now())}
    retry_urls = set()
    if row["packet"] and Path(row["packet"]).exists():
        previous = json.loads(Path(row["packet"]).read_text())
        retry_urls = {issue.get("url") for issue in previous["issues"] if issue["kind"] == "unreadable"}
    with state.db:
        state.db.execute("UPDATE filings SET attempts=attempts+1 WHERE accession=?", (accession,))
    try:
        index = client.get(filing["filing_url"]).text
        docs = pick_documents(index, filing)
        for doc in docs:
            doc_id = hashlib.sha256(doc["url"].encode()).hexdigest()[:16]
            try:
                cached = folder / (doc_id + ".raw")
                reuse = cached.exists() and row["status"] != "failed" and doc["url"] not in retry_urls
                content = cached.read_bytes() if reuse else client.get(doc["url"]).content
                if not reuse:
                    temporary = cached.with_suffix(".raw.tmp")
                    temporary.write_bytes(content)
                    temporary.replace(cached)
                text, images = extract_text(content, doc["url"])
                if not text.strip():
                    raise ValueError("No readable document text")
                parts = []
                for i, body in enumerate(chunks(text), 1):
                    chunk_id = f"{doc_id}:{i}"
                    parts.append({"id": chunk_id, "text": body})
                packet["documents"].append({**doc, "id": doc_id, "chunks": parts, "images": images})
                if images:
                    packet["issues"].append({"document_id": doc_id, "kind": "images", "detail": f"{len(images)} embedded images require visual review or an explicit coverage limitation"})
            except Exception as exc:
                packet["issues"].append({"document_id": doc_id, "kind": "unreadable", "url": doc["url"], "detail": str(exc)})
        if not any(d["type"] == filing["form"] for d in packet["documents"]):
            raise ValueError("Could not read the primary filing document")
        packet_path = folder / "packet.json"
        atomic_json(packet_path, packet)
        with state.db:
            state.db.execute("UPDATE filings SET status='retrieved',packet=?,error=? WHERE accession=?", (str(packet_path), json.dumps(packet["issues"]) if packet["issues"] else None, accession))
        return {"accession": accession, "packet": str(packet_path), "documents": len(packet["documents"]), "chunks": sum(len(d["chunks"]) for d in packet["documents"]), "issues": packet["issues"]}
    except Exception as exc:
        with state.db:
            state.db.execute("UPDATE filings SET status='failed',error=? WHERE accession=?", (str(exc), accession))
        raise


def validate_card(packet, card):
    documents = {doc["id"]: doc for doc in packet["documents"]}
    expected = {chunk["id"] for doc in documents.values() for chunk in doc["chunks"]}
    reviewed = card.get("reviewed_chunks", [])
    if not isinstance(reviewed, list) or set(reviewed) != expected or len(reviewed) != len(expected):
        raise ValueError("Card must record review of every source chunk exactly once")
    if not isinstance(card.get("title"), str) or not card["title"].strip():
        raise ValueError("Missing card title")
    if not 1 <= len(card.get("summary", [])) <= 3 or not 1 <= len(card.get("facts", [])) <= 5:
        raise ValueError("Require 1–3 summary statements and 1–5 key facts")
    claims = [*card["summary"], *card["facts"], card.get("why_it_matters", {})]
    if card.get("watch_next"):
        claims.append(card["watch_next"])
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str) or not claim["text"].strip() or not claim.get("evidence"):
            raise ValueError("Every statement needs text and supporting evidence")
        for evidence in claim["evidence"]:
            doc_id = evidence.get("document_id")
            quote = evidence.get("quote", "")
            if doc_id not in documents or not isinstance(quote, str) or len(normalize(quote)) < 12:
                raise ValueError("Invalid evidence reference")
            text = "".join(c["text"] for c in documents[doc_id]["chunks"])
            if normalize(quote) not in normalize(text):
                raise ValueError("Evidence quote does not occur in source document")
    if card["why_it_matters"].get("kind") != "interpretation":
        raise ValueError("Why-it-matters must be explicitly labeled interpretation")
    # Fail closed while retaining the filing for retry. A failure notice may be
    # posted by the task, but incomplete cards cannot be marked delivered.
    if any(issue["kind"] == "unreadable" for issue in packet["issues"]):
        raise ValueError("Unreadable documents remain pending; retry before publishing a complete card")
    expected_images = {url for doc in documents.values() for url in doc.get("images", [])}
    reviews = card.get("image_reviews", [])
    if {review.get("url") for review in reviews} != expected_images or len(reviews) != len(expected_images):
        raise ValueError("Review every embedded image, including any equivalent text layer")
    for review in reviews:
        if review.get("result") not in {"decorative", "read", "text_equivalent"} or not review.get("note"):
            raise ValueError("Image review needs a supported result and a review note")


def render_card(packet, card, preview=False):
    filing = packet["filing"]
    docs = {doc["id"]: doc for doc in packet["documents"]}
    def linked(claim):
        ids = list(dict.fromkeys(e["document_id"] for e in claim["evidence"]))
        links = " · ".join(f"[{docs[key]['type']}]({docs[key]['url']})" for key in ids)
        return f"{claim['text']} {links}"
    accepted = instant(filing["accepted_at"]).astimezone(ZoneInfo("America/New_York")).strftime("%b %d, %Y at %I:%M %p %Z") if filing["accepted_at"] else filing["filed_date"] + " (acceptance time unavailable)"
    lines = [f"### PHR · {filing['form']} · {card['title']}", "", f"{'**PREVIEW — historical example** · ' if preview else ''}Filed: {accepted}", ""]
    if filing.get("report_date"):
        lines.extend([f"Reporting period/event date: {filing['report_date']}", ""])
    lines.extend(["**What happened**", "", " ".join(linked(claim) for claim in card["summary"]), "", "**Key facts**", ""])
    lines.extend("- " + linked(claim) for claim in card["facts"])
    lines.extend(["", "**Why it matters — interpretation**", "", linked(card["why_it_matters"])])
    if card.get("watch_next"):
        lines.extend(["", "**Watch next:** " + linked(card["watch_next"])])
    if card.get("coverage_note"):
        lines.extend(["", "**Coverage:** " + card["coverage_note"]])
    lines.extend(["", f"[Read original filing and exhibits]({filing['filing_url']})", "", f"Accession: `{filing['accession']}`", ""])
    return "\n".join(lines)


def save_card(state, accession, card):
    row = state.row(accession)
    if row["status"] != "retrieved":
        raise ValueError("Prepare the filing before submitting a new card")
    packet = json.loads(Path(row["packet"]).read_text())
    validate_card(packet, card)
    markdown = render_card(packet, card, preview=state.setting("mode") == "preview")
    folder = Path(row["packet"]).parent
    atomic_json(folder / "card.json", card)
    (folder / "card.md").write_text(markdown)
    with state.db:
        state.db.execute("UPDATE filings SET status='ready',card=?,markdown=? WHERE accession=?", (json.dumps(card), markdown, accession))
    return {"accession": accession, "status": "ready", "markdown": str(folder / "card.md")}


def mark_delivered(state, accession, reference):
    if not reference.strip():
        raise ValueError("A verified task-message reference is required")
    row = state.row(accession)
    if row["status"] == "delivered":
        return
    if row["status"] != "ready":
        raise ValueError("Only a validated ready card may be acknowledged")
    with state.db:
        state.db.execute("UPDATE filings SET status='delivered',published_reference=?,delivered_at=? WHERE accession=?", (reference, stamp(now()), accession))


def queue(state):
    result = []
    for row in state.db.execute("SELECT * FROM filings WHERE status != 'delivered'"):
        filing = json.loads(row["metadata"])
        result.append({**filing, "status": row["status"], "packet": row["packet"], "error": row["error"]})
    return sorted(result, key=lambda f: (f["accepted_at"] or f["filed_date"], f["accession"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, help="Persistent private working directory (use a separate directory for previews)")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--since", required=True, help="ISO timestamp with timezone; never inferred silently")
    init.add_argument("--preview", action="store_true")
    sub.add_parser("discover")
    sub.add_parser("queue")
    sub.add_parser("status")
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("accession")
    submit = sub.add_parser("submit-card")
    submit.add_argument("accession")
    submit.add_argument("json_file")
    acknowledge = sub.add_parser("acknowledge")
    acknowledge.add_argument("accession")
    acknowledge.add_argument("--published-reference", required=True)
    sub.add_parser("ready")
    args = parser.parse_args()
    state = State(args.state_dir)
    lock = (state.root / "run.lock").open("a")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another command is using this state; retry later") from exc
        if args.command == "init":
            state.initialize(args.since, args.preview)
            result = {"mode": state.setting("mode"), "start_at": state.setting("start_at")}
        elif args.command == "discover":
            result = discover(state, SecClient(os.environ.get("EDGAR_USER_AGENT")))
        elif args.command == "prepare":
            result = prepare(state, SecClient(os.environ.get("EDGAR_USER_AGENT")), args.accession)
        elif args.command == "queue":
            result = queue(state)
        elif args.command == "submit-card":
            result = save_card(state, args.accession, json.loads(Path(args.json_file).read_text()))
        elif args.command == "acknowledge":
            mark_delivered(state, args.accession, args.published_reference)
            result = {"accession": args.accession, "status": "delivered"}
        elif args.command == "ready":
            result = [{"accession": item["accession"], "markdown": state.row(item["accession"])["markdown"]} for item in queue(state) if item["status"] == "ready"]
        else:
            result = {"mode": state.setting("mode"), "start_at": state.setting("start_at"), "discovery_checkpoint": state.setting("discovery_checkpoint"), "pending": len(queue(state)), "counts": dict(state.db.execute("SELECT status,COUNT(*) FROM filings GROUP BY status")), "last_run": dict(row) if (row := state.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()) else None}
        print(json.dumps(result, indent=2))
    except Exception as exc:
        print(json.dumps({"error": str(exc), "command": args.command}), file=sys.stderr)
        return 1
    finally:
        state.db.close()
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
