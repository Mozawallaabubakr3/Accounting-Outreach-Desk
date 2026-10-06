# Accounting-Outreach-Desk

A local accounting-firm prospecting and outreach desk with evidence, email verification, exact draft approvals and reply handling.

Created as a source snapshot on October 6, 2026 from Abubakr Mozawalla’s working project and approved Obsidian documentation. The repository describes a personal prototype; it does not claim client results or measured application throughput.

## Architecture

`outreach/agent.py` coordinates discovery, website evidence, enrichment, qualification, verification and drafts. `providers.py` connects optional search, verification and Gmail providers. `store.py` persists records and suppression state. `auth.py` protects the local dashboard; `net.py` bounds outbound fetching. `dashboard.py` and `static/` provide the review interface.

Workflow: public firm evidence → named decision maker → verified business email → qualified draft → exact message/attachment approval → permitted sending window → provider receipt → replies/opt-outs stop follow-ups. Routine processing makes no language-model calls. Sending defaults off.

## Run

Python 3.11+, standard library. Run `python3 -m outreach.cli serve` and use its private local launch link. Set up your own Brave Search, Hunter and Gmail connections as needed; keep secrets in local environment files. Supply your own postal address and attachment before authorizing outreach. `python3 -m unittest discover -s tests` checks mocked workflows without contacting real firms.

## Export boundaries

The snapshot includes actual implementation code and configuration examples. Databases, applicant information, resumes, account sessions, machine-specific deployment files, emails, prospect records, raw footage and generated media are excluded. Private working copies remain separate.
