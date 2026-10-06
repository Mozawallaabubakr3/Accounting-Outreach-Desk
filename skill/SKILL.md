---
name: accounting-outreach
description: Operate User’s accounting-firm outreach workflow to research Miami and Fort Lauderdale firms, verify decision makers, prepare pro bono emails, review drafts, send authorized messages, and process replies using the local Outreach Desk.
---

# Accounting Outreach

Use the workflow at <workspace>/outreach-agent/. Read PROMPT.md once for User’s voice, target market, evidence, authority and retry rules. Provider availability is shown by status.

From that directory, run python3 -m outreach.cli status. Inspect relevant records in the local dashboard or through Agent.store; do not read the full vault or dump all records into context. The run command performs discovery, cached website research, Hunter enrichment, conservative qualification and drafting. Resolve ambiguous records using their exact source evidence. Read README.md only for setup or diagnosis.

Launch Start Outreach Agent.command for the dashboard. The private launch URL is in data/launch-url.txt; never share it publicly. Keys and Gmail refresh tokens remain local and excluded from exports.

Sending defaults off. User’s approval records the hash of each exact draft. The send command checks Gmail identity, footer, public portfolio, verification, replies, suppression, hours and daily cap. An uncertain result must be reconciled rather than resent through another tool. Initial outreach attaches the current PDF. One reviewed follow-up is available after six business days. Replies stop automatic follow-ups; opt-outs and bounces suppress the address.

The worker command can run continuously; --send processes already-approved drafts only. It is not scheduled until User starts or explicitly schedules it. If credentials are missing, continue source-backed research with available connected tools, but do not claim unattended discovery, verification or delivery is live-tested.
