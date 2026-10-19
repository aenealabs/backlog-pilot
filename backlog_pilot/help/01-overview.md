# Backlog Pilot

Backlog Pilot turns a stakeholder's request into a reviewed, prioritized, planned backlog,
with every story traced to the stakeholder's own words.

## What it is for

Requests arrive as emails, meeting notes, and hallway asks. Backlog Pilot drafts epics,
features, and user stories from them, with acceptance criteria, WSJF scores, the assumptions
it made, and the questions to take back. A product owner reviews and approves the draft
before anything reaches the backlog, and the approved request lives on as an **intake** whose
assumptions and answers can change, with change control.

## Who does what

| Who | Does what |
| --- | --- |
| **The AI** | Drafts epics, features, stories, WSJF scores, assumptions, questions, and personas it needs, quoting the stakeholder's words for each story. |
| **Rules (code)** | Check every quote against the input, check each story is ready, flag non-functional gaps and likely duplicates, compute WSJF and the roadmap, and flag changes that may affect the backlog. |
| **A product owner (approver)** | Edits and approves drafts, scores WSJF, sets capacity and pins, and decides what changes mean. Changing a proposed score or placement, or dropping drafted items, needs a reason. |
| **The audit log** | Records every draft, approval, change, move, and refused attempt. |

## A typical request

1. **New intake:** paste the request (or open one from the inbox) and click **Draft backlog**.
2. **Review:** check traces, quality, duplicates, and WSJF; reword, untick, and adjust.
3. **Approve & push:** the stories join the backlog and the roadmap recalculates.
4. **Intakes:** as assumptions change or questions are answered, decide whether to re-draft.
5. **Priorities and Roadmap:** re-score, set capacity, and pin; every move is logged.

The full list of checks and thresholds is in the [rules and settings reference](reference).
