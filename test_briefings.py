import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import briefings as b


def batch(rows):
    fields = ("accessionNumber", "form", "filingDate", "primaryDocument", "acceptanceDateTime", "reportDate")
    return {key: [row[i] for row in rows] for i, key in enumerate(fields)}


def row(number, date, form="8-K"):
    return (f"0001412408-26-{number:06}", form, date, "filing.htm", date + "T18:00:00Z", date)


class FakeClient:
    def __init__(self, response, archives=None):
        self.response = response
        self.archives = archives or {}
        self.requested = []

    def json(self, url):
        self.requested.append(url)
        if url == b.SUBMISSIONS:
            return self.response
        result = self.archives[url.rsplit("/", 1)[-1]]
        if isinstance(result, Exception):
            raise result
        return result


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = b.State(self.temp.name)
        self.state.initialize("2026-09-01T00:00:00Z", preview=True)

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def client(self, rows, files=None, archives=None):
        return FakeClient({"cik": 1412408, "filings": {"recent": batch(rows), "files": files or []}}, archives)

    def seed(self):
        b.discover(self.state, self.client([row(1, "2026-09-02")]), b.instant("2026-09-03T00:00:00Z"))
        return "0001412408-26-000001"

    def test_daily_catchup_reads_archives_and_deduplicates(self):
        files = [{"name": "CIK0001412408-submissions-001.json", "filingFrom": "2026-09-01", "filingTo": "2026-09-04"}, {"name": "CIK0001412408-submissions-002.json", "filingFrom": "2020-01-01", "filingTo": "2020-12-31"}]
        client = self.client([row(3, "2026-09-08")], files, {files[0]["name"]: batch([row(1, "2026-09-02"), row(2, "2026-09-04", "8-K/A")])})
        cutoff = b.instant("2026-09-09T00:00:00Z")
        self.assertEqual(b.discover(self.state, client, cutoff)["new_filings"], 3)
        self.assertEqual(b.discover(self.state, client, cutoff)["new_filings"], 0)
        self.assertEqual([f["accession"][-6:] for f in b.queue(self.state)], ["000001", "000002", "000003"])
        self.assertFalse(any("002.json" in url for url in client.requested))

    def test_late_inventory_addition_is_found_in_overlap(self):
        self.seed()
        b.discover(self.state, self.client([row(1, "2026-09-02"), row(2, "2026-09-02")]), b.instant("2026-09-04T00:00:00Z"))
        self.assertEqual(len(b.queue(self.state)), 2)

    def test_archive_failure_does_not_advance_checkpoint_or_commit_partial_scan(self):
        self.seed()
        saved = self.state.setting("discovery_checkpoint")
        files = [{"name": "CIK0001412408-submissions-001.json", "filingFrom": "2026-09-01", "filingTo": "2026-09-05"}]
        client = self.client([row(3, "2026-09-08")], files, {files[0]["name"]: RuntimeError("source unavailable")})
        with self.assertRaisesRegex(RuntimeError, "source unavailable"):
            b.discover(self.state, client, b.instant("2026-09-09T00:00:00Z"))
        self.assertEqual(self.state.setting("discovery_checkpoint"), saved)
        self.assertEqual(len(b.queue(self.state)), 1)
        self.assertEqual(self.state.db.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()[0], "failed")

    def test_no_checkpoint_on_malformed_inventory(self):
        client = self.client([row(1, "2026-09-02")])
        client.response["filings"]["recent"]["form"] = []
        with self.assertRaises(ValueError):
            b.discover(self.state, client, b.instant("2026-09-03T00:00:00Z"))
        self.assertIsNone(self.state.setting("discovery_checkpoint"))

    def test_historical_inventory_without_acceptance_or_period_is_retained(self):
        client = self.client([row(1, "2026-09-02")])
        del client.response["filings"]["recent"]["acceptanceDateTime"]
        del client.response["filings"]["recent"]["reportDate"]
        self.assertEqual(b.discover(self.state, client, b.instant("2026-09-03T00:00:00Z"))["new_filings"], 1)

    def test_amendment_is_separate_and_prebaseline_and_future_are_excluded(self):
        client = self.client([row(1, "2026-08-31"), row(2, "2026-09-02"), row(3, "2026-09-02", "8-K/A"), row(4, "2026-09-04")])
        b.discover(self.state, client, b.instant("2026-09-03T00:00:00Z"))
        self.assertEqual([f["form"] for f in b.queue(self.state)], ["8-K", "8-K/A"])

    def test_baseline_cannot_be_reset_and_timezone_required(self):
        with self.assertRaises(ValueError):
            self.state.initialize("2026-09-10T00:00:00Z")
        with self.assertRaises(ValueError):
            b.instant("2026-09-10T00:00:00")

    def test_document_picker_keeps_exhibits_and_xml(self):
        html = '<table summary="Document Format Files"><tr><th>Seq</th></tr>'
        for kind, name in [("4", "xslF345X06/form4.xml"), ("4", "form4.xml"), ("EX-99.1", "earnings.htm"), ("EX-10.1", "agreement.pdf"), ("GRAPHIC", "logo.jpg")]:
            html += f'<tr><td>1</td><td>Document</td><td><a href="{name}">{name}</a></td><td>{kind}</td></tr>'
        html += '</table>'
        docs = b.pick_documents(html, {"form": "4", "filing_url": "https://www.sec.gov/Archives/edgar/data/1412408/example/index.html"})
        self.assertEqual([d["type"] for d in docs], ["4", "EX-99.1", "EX-10.1"])
        self.assertTrue(docs[0]["url"].endswith("/form4.xml"))

    def test_xml_preserves_transaction_labels_and_footnotes(self):
        xml = b'<ownershipDocument><transaction><transactionCode>F</transactionCode><shares>250</shares></transaction><footnotes><footnote id="F1">Tax withholding only</footnote></footnotes></ownershipDocument>'
        text, images = b.extract_text(xml, "https://www.sec.gov/Archives/form4.xml")
        self.assertIn("transaction/transactionCode: F", text)
        self.assertIn("footnote [id=F1]: Tax withholding only", text)
        self.assertEqual(images, [])

    def test_tables_preserve_row_relationships_and_hidden_xbrl_is_removed(self):
        html = b'<html><ix:header>Hidden junk</ix:header><table><tr><td>Revenue</td><td>$129.5m</td></tr></table><img src="chart.jpg"></html>'
        text, images = b.extract_text(html, "https://www.sec.gov/Archives/earnings.htm")
        self.assertIn("Revenue | $129.5m", text)
        self.assertNotIn("Hidden junk", text)
        self.assertEqual(images, ["https://www.sec.gov/Archives/chart.jpg"])

    def test_long_documents_are_not_truncated(self):
        source = "a" * 700000 + "Material information at the end"
        self.assertEqual("".join(b.chunks(source)), source)

    def packet_and_card(self):
        accession = self.seed()
        filing = json.loads(self.state.row(accession)["metadata"])
        packet = {"filing": filing, "documents": [{"id": "doc", "type": "8-K", "url": "https://www.sec.gov/Archives/filing.htm", "chunks": [{"id": "doc:1", "text": "Revenue increased to $129.5 million."}]}], "issues": []}
        claim = {"text": "Revenue reached $129.5 million.", "evidence": [{"document_id": "doc", "quote": "Revenue increased to $129.5 million."}]}
        card = {"filing_category": "other", "title": "Revenue update", "summary": [claim], "facts": [claim], "why_it_matters": {**claim, "kind": "interpretation"}, "reviewed_chunks": ["doc:1"]}
        path = Path(self.temp.name) / "packet.json"
        b.atomic_json(path, packet)
        with self.state.db:
            self.state.db.execute("UPDATE filings SET status='retrieved',packet=? WHERE accession=?", (str(path), accession))
        return accession, packet, card

    def test_card_validation_and_delivery_survive_restart(self):
        accession, packet, card = self.packet_and_card()
        b.save_card(self.state, accession, card)
        self.assertEqual(self.state.row(accession)["status"], "ready")
        self.state.db.close()
        self.state = b.State(self.temp.name)
        self.assertEqual(self.state.row(accession)["status"], "ready")
        self.assertIn("PREVIEW", self.state.row(accession)["markdown"])
        b.mark_delivered(self.state, accession, "task:verified-message-1")
        b.mark_delivered(self.state, accession, "task:verified-message-1")
        self.assertEqual(b.queue(self.state), [])

    def test_earnings_cards_require_and_render_business_line_revenue(self):
        accession, packet, card = self.packet_and_card()
        card["filing_category"] = "earnings"
        with self.assertRaisesRegex(ValueError, "revenue_breakout"):
            b.validate_card(packet, card)
        card["revenue_breakout"] = [card["facts"][0]]
        b.save_card(self.state, accession, card)
        markdown = self.state.row(accession)["markdown"]
        self.assertIn("**Revenue by business line**", markdown)
        self.assertIn("Revenue reached $129.5 million.", markdown)

    def test_ready_card_can_be_revised_but_delivered_card_cannot(self):
        accession, _, card = self.packet_and_card()
        b.save_card(self.state, accession, card)
        card["title"] = "Revised revenue update"
        b.save_card(self.state, accession, card)
        self.assertIn("Revised revenue update", self.state.row(accession)["markdown"])
        b.mark_delivered(self.state, accession, "task:verified-message-2")
        with self.assertRaisesRegex(ValueError, "Prepare the filing"):
            b.save_card(self.state, accession, card)

    def test_missing_evidence_and_unread_chunks_rejected(self):
        accession, packet, card = self.packet_and_card()
        card["summary"][0]["evidence"][0]["quote"] = "This quote is fabricated and absent."
        with self.assertRaisesRegex(ValueError, "does not occur"):
            b.save_card(self.state, accession, card)
        self.assertEqual(self.state.row(accession)["status"], "retrieved")
        card["reviewed_chunks"] = []
        with self.assertRaisesRegex(ValueError, "every source chunk"):
            b.validate_card(packet, card)

    def test_partial_extraction_stays_pending_even_with_coverage_note(self):
        _, packet, card = self.packet_and_card()
        packet["issues"] = [{"kind": "unreadable", "detail": "Missing exhibit"}]
        with self.assertRaisesRegex(ValueError, "remain pending"):
            b.validate_card(packet, card)
        card["coverage_note"] = "An exhibit could not be retrieved."
        with self.assertRaisesRegex(ValueError, "remain pending"):
            b.validate_card(packet, card)

    def test_images_require_explicit_review(self):
        _, packet, card = self.packet_and_card()
        url = "https://www.sec.gov/Archives/chart.jpg"
        packet["documents"][0]["images"] = [url]
        with self.assertRaisesRegex(ValueError, "every embedded image"):
            b.validate_card(packet, card)
        card["image_reviews"] = [{"url": url, "result": "text_equivalent", "note": "Reviewed image; the same figures appear in the extracted text."}]
        b.validate_card(packet, card)

    def test_source_identity_and_empty_invalid_inventory_are_rejected(self):
        client = self.client([])
        client.response["cik"] = 1
        with self.assertRaisesRegex(ValueError, "not Phreesia"):
            b.discover(self.state, client)
        client.response["cik"] = 1412408
        client.response["filings"]["recent"] = {}
        with self.assertRaisesRegex(ValueError, "Missing inventory columns"):
            b.discover(self.state, client)

    def test_prepare_failure_preserves_filing_for_retry(self):
        accession = self.seed()
        with self.assertRaisesRegex(RuntimeError, "network down"):
            with patch.object(b.SecClient, "get", side_effect=RuntimeError("network down")):
                b.prepare(self.state, b.SecClient("Monitor contact@example.com"), accession)
        self.assertEqual(self.state.row(accession)["status"], "failed")
        self.assertEqual(len(b.queue(self.state)), 1)

    def test_delivery_cannot_be_acknowledged_before_validation(self):
        accession = self.seed()
        with self.assertRaises(ValueError):
            b.mark_delivered(self.state, accession, "task:message")

    def test_client_stops_on_access_denial_and_retries_rate_limit(self):
        client = b.SecClient("Monitor contact@example.com")
        response = unittest.mock.Mock(status_code=403)
        with patch.object(client.session, "get", return_value=response) as get, patch.object(b.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "403"):
                client.get(b.SUBMISSIONS)
            self.assertEqual(get.call_count, 1)
        rate = unittest.mock.Mock(status_code=429, headers={"Retry-After": "1"})
        ok = unittest.mock.Mock(status_code=200)
        with patch.object(client.session, "get", side_effect=[rate, ok]) as get, patch.object(b.time, "sleep"):
            self.assertIs(client.get(b.SUBMISSIONS), ok)
            self.assertEqual(get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
