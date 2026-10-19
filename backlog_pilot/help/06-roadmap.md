# Roadmap

The roadmap is computed from WSJF and capacity, not drawn, so it always matches the backlog.

## How the plan is built

For each product, over four quarters:

1. Pinned features go where a person put them.
2. In-progress features stay in this quarter.
3. Everything else fills quarters in WSJF order, highest first, where it fits the quarter's
   capacity (in WSJF job-size points).
4. Scored features that fit nowhere are **Unplanned**; unscored ones wait to be scored.

## Changing the plan

Approvers can change a quarter's capacity, re-score a feature, or **pin** a feature to a
quarter. A pin overrides the rules, so it needs a reason. Every change recalculates the plan,
and every feature that moved is logged with the cause and who caused it.

## Portfolio view

**Portfolio view** shows every product's epics across the quarters, with capacity used, and
the latest moves across the portfolio.
