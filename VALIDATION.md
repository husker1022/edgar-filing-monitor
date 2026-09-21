# Preview validation

Validated locally on September 20, 2026 (Eastern Time).

## Automated checks

`python3 -m unittest -v test_briefings` — 21 tests passed.

The tests exercise archive recovery, overlap discovery, deduplication, amendments,
malformed inventory, source identity, checkpoint preservation on failure, restart
recovery, evidence matching, explicit image review, ownership XML footnotes,
long-document preservation, retryable preparation failures, and delivery gates.
They also enforce and render the revenue-by-business-line section for earnings
cards and permit a reviewed card to be revised before publication.

`git diff --check` — passed for tracked modifications. New Python modules also
imported and executed during tests and live retrieval.

## Live SEC retrieval

The official issuer inventory yielded 138 filings since January 1, 2026 in a
separate preview database. Repeated discovery inserted zero duplicates. The
inventory included insider forms, Form 144, and Schedules 13D/13G.

| Accession | Form | Retrieved documents | Extracted characters preserved |
|---|---|---:|---:|
| 0001412408-26-000213 | 8-K | 3 | 93,230 |
| 0001412408-26-000238 | 4 | 1 | 3,132 |
| 0001412408-26-000223 | 10-Q | 6 | 478,576 |
| 0001412408-26-000042 | 8-K/A | 5 | 157,156 |

Each document was re-extracted from the saved raw response and compared with its
concatenated chunks; no extracted text was lost. This checks chunk completeness,
not perfect semantic interpretation of every document format.

Two historical cards were fully read and validated: the earnings 8-K and insider
Form 4. The earnings review included the press release, stakeholder letter, and
20 embedded page images. The quarterly report and amendment were retrieval and
chunk-preservation tests; full briefing cards for those two were not generated.

## Not activated or proven yet

- No production baseline or recurring automation exists.
- No email was sent and no external model API was invoked by the new pipeline.
- Live unattended scheduling, startup behavior after missed runs, and durable
  network permission must be checked during activation.
- Post-publication acknowledgment was tested against simulated messages; real
  task-history reconciliation must be verified after the first live card.
- Text-bearing PDF extraction is implemented, but the four live filing tests
  above used HTML/XML. Image-only PDFs remain a review requirement.
- Evidence matching verifies the existence of source excerpts; it does not
  mathematically prove the interpretation or financial accuracy of a summary.
