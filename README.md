# Phreesia briefing monitor

Daily SEC briefing cards delivered through an existing Codex task. Python handles
SEC access, extraction, checkpoints, and a durable delivery queue; the Codex task
reads the source documents and writes the summaries. No additional model API key
is required for this mode. Task execution still uses the user's Codex usage.

**Current status:** local preview built; recurring delivery is not activated.

## What is implemented

- Official Phreesia submissions inventory, including relevant historical pages.
- Baseline date plus last-successful-discovery checkpoint and seven-day overlap.
- SQLite queue keyed by accession; individual retrieval errors remain retryable.
- Primary documents and all exhibits in the filing's document table.
- HTML tables, labeled ownership XML, text, and text-bearing PDFs.
- Complete text chunking without the original prototype's truncation or 10-K prompts.
- A card schema with source quotations for each statement and an explicit record
  of every reviewed chunk/image. Quotes must occur in retrieved source text.
- A required, separately rendered revenue-by-business-line section for every
  earnings-related filing, regardless of SEC form.
- Saved, validated Markdown cards and explicit post-publication acknowledgments.
- One SEC request per second per process, finite retries, and local command locking.

The evidence validator verifies citation existence, not semantic truth. Codex must
still check that the evidence actually supports the claimed numbers and meaning.
Image-only PDFs need visual/OCR review. Unreadable documents stay queued rather
than producing a card falsely marked complete.

## Setup

Python 3.10+ on macOS/Linux. The existing `main.py` is the original interactive
Claude-based research tool; `briefings.py` is the new noninteractive task pipeline.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-monitor.txt
export EDGAR_USER_AGENT='PhreesiaBriefingMonitor contact@example.com'
```

Store the actual contact address locally, not in a public commit. The approved
address and machine-specific paths are in the private task setup, outside this repo.

## Commands

All commands require `--state-dir`. Use a separate directory for previews. Only
initialize the production baseline when the user approves activation.

```sh
# Historical examples only:
.venv/bin/python briefings.py --state-dir preview-state init --since 2026-09-01T00:00:00Z --preview
.venv/bin/python briefings.py --state-dir preview-state discover
.venv/bin/python briefings.py --state-dir preview-state queue
.venv/bin/python briefings.py --state-dir preview-state prepare 0001412408-26-000213

# Read ALL chunks and images from the returned packet, then write a card JSON:
.venv/bin/python briefings.py --state-dir preview-state submit-card 0001412408-26-000213 card.json
.venv/bin/python briefings.py --state-dir preview-state ready
```

See `CARD_SCHEMA.md` and `TASK_WORKFLOW.md` for the source-reading and publishing
procedure. `ready` lists cards but does not mark them delivered. Only acknowledge
after finding the published card in the task's actual message history:

```sh
.venv/bin/python briefings.py --state-dir monitor-state acknowledge ACCESSION --published-reference 'task-id:verified-message-id'
```

`status` reports counts, checkpoint, and last discovery attempt. A failed command
returns a nonzero exit code and a JSON error. A successful empty check is distinct
from an access failure. Only the task publishes notifications; the CLI never sends
email or writes a chat message itself.

## Daily recovery behavior

The proposed schedule is 9 a.m. America/New_York. On the next actual execution
after downtime, discovery covers the missing interval and the overlap. Older
inventory pages are fetched when the gap extends beyond the recent inventory.
Pending retrieval and delivery work survives independently of the checkpoint.

The schedule depends on the computer/app being available. We have not verified
whether missed runs execute immediately on app startup. A scheduled run or a
user-requested check performs the same catch-up procedure.

The direct downloader requires network permission in its execution environment.
A one-turn permission granted during development is not proof that unattended
runs have durable network access. Confirm that access before activation.

## Validation

```sh
python3 -m unittest -v test_briefings
```

Tests cover archive catch-up, deduplication, late discovery, amendments, malformed
inventory, access failures, XML footnotes, long-document preservation, source
evidence, image-review requirements, incomplete extraction, and delivery recovery.

The changes are prepared for review before activation. The original interactive
tool is preserved, and email/hosted execution remains outside the approved local
pilot.
