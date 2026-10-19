# Audit log

Every draft, approval, edit, score, move, push, and refused attempt is recorded with who did
it and when.

## Searching

Search by words (exact full-text search), and filter by person, activity, item key (such as
`BP-103` or `IN-2`), and dates. Categories include drafts and approvals, assumptions and
questions, prioritization, roadmap, personas, tracker sync, and sign-in and access.

## Ask the audit log

Type a question in plain English. The AI turns it into a search plan, code runs the plan,
and the answer cites the events it used (for example `[#42]`). A citation to an event that
was not retrieved is marked *[unverified]*. In the offline demo, suggested questions replay
recorded plans and the answer is a summary computed from the events.

## Refused attempts

Recorded as **access denied**: an action someone without approver rights tried, an action
tried without signing in, and an inbox request from another app without the right suite
token. A sign-in that fails is recorded as **sign-in failed**.
