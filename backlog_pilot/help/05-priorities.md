# Priorities (WSJF)

Every live feature, ranked by Weighted Shortest Job First.

## How the score works

WSJF = (Business value + Time criticality + Risk reduction) / Job size. Each part uses the
scale 1, 2, 3, 5, 8, 13, 20. The AI drafts scores calibrated against features already scored,
and its numbers are kept beside any change.

## Re-scoring

Approvers click a feature to re-score it. Changing an existing score needs a reason, which is
kept in the feature's WSJF history and the audit log. Re-scoring recalculates the roadmap,
and any feature that moves quarter is logged with the re-score as the cause.
