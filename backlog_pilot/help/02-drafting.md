# Drafting and review

The review screen is where the AI's draft becomes your backlog. Nothing is written until you
approve.

## Drafting a request

On **New intake**, paste the request (an email, notes, or a description of the problem), or
click a demo scenario, then **Draft backlog**. Requests sent from other Mission Control apps
wait in the **inbox** on the same page; open one to draft from it, and the intake will link
back to where it came from. Anyone signed in can draft; approving needs an approver.

## Reading the draft

- **Traced, AI inference, or Quote not found.** Each story cites the stakeholder's words, and
  code checks every quote against the input. Click a story's source to highlight the words.
  An *AI inference* story has no supporting words: confirm the stakeholder wants it.
- **Ready.** Each story is checked for 2 to 5 testable Given/When/Then criteria, an outcome,
  one capability, and a known persona. Warnings make it not ready; notes do not.
- **Non-functional gaps.** If the request mentions security, export control, data
  governance, auditability, or integrations and no story covers it, the gap is shown.
- **Possible duplicates.** Each feature and story is compared with the live backlog. Link a
  real overlap as related, or untick the drafted item.
- **Personas.** Stories use the persona library; a persona the AI needed but could not find
  is proposed, and joins the library only if you accept it.

## Editing the draft

- **Reword** any title freely. The AI's wording is kept with the item, shown as
  *reworded* in the backlog, and logged.
- **WSJF:** change any part of a feature's score. A changed score needs a reason next to it.
- **Untick or remove** stories, assumptions, questions, or proposed personas you do not want.
  Anything you drop that the AI drafted needs one reason, under **Why you dropped items**.
- Assign each epic to a **product** (suggested from its text), then **Approve & push**.

If a reason is missing, the approval is refused with a message and nothing is written.
