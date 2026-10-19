# Backlog Pilot

An AI backlog copilot for IT product owners in Product Engineering & R&D (PLM, CAD data,
engineering change, engineering BOM and SAP integration, engineering data governance).

Paste a stakeholder request, email, or meeting notes. Backlog Pilot drafts epics, features,
and user stories with Given/When/Then acceptance criteria, and lists its assumptions and the
questions to take back to the stakeholder. The product owner edits and approves the draft
before anything is written to the tracker, and every draft and approval goes into an audit log.

Every story is **traced to the stakeholder's own words**: the model cites verbatim quotes and
the server checks them against the input, so each story is labeled *traced*, *AI inference*
(no stakeholder words support it), or *quote not found*. Clicking a story's source highlights
the quoted text. Drafts use a maintained **persona library**; personas the model needs but
cannot find are proposed, and join the library only when a reviewer accepts them. Persona
edits are versioned like everything else.

**Quality checks** run as rules, not model opinions: each story gets a *ready* check
(2–5 acceptance criteria, Given/When/Then form, no untestable words like "fast" or
"seamless", one capability per story, persona in the library, quotes found), and the draft is
checked for **non-functional coverage**: if the input mentions export control, security,
auditability, data governance, or integrations and no story covers it, the gap is flagged.

**Duplicate detection:** every drafted feature and story is compared with the live backlog
using plain text similarity (TF-IDF, no model, no extra dependency). Possible overlaps show the
existing item, its status, and a similarity score; the reviewer links real ones as related or
unticks them. It matches shared vocabulary, not synonyms, so it suggests rather than decides.

**WSJF prioritization:** the model drafts value, time criticality, risk reduction, and job
size for each feature, calibrated against features already scored in the backlog. Reviewers
adjust scores before approval, with a reason (the AI's numbers are kept beside theirs), and the
**Priorities** page ranks every live feature by WSJF with re-scoring and full history.

**Roadmap:** every epic belongs to a product (suggested from its text, confirmed by the
reviewer). Each product has quarterly capacity in WSJF job-size points, and the roadmap is
*computed*: in-progress and pinned work stays put, then features fill quarters in WSJF order
until capacity runs out, and the rest is shown as *Unplanned*. Re-scoring, changing capacity,
pinning, or approving new work recalculates the plan, and every feature that moved quarter is
logged with its cause and owner. A portfolio view shows all products' epics and capacity use.

**Audit search.** The audit log has exact full-text search (SQLite FTS5) with filters for
person, activity, item key, and dates, so it stays fast at any size. *Ask the audit log*
takes a plain-English question: the model turns it into a search plan, code runs the plan, and
in live mode the model answers from the matching events only, citing each one (`[#42]`).
Citations to events that were not retrieved are marked *[unverified]*. Offline, suggested
questions replay recorded plans and the answer is a summary computed from the events.

After approval, each request lives on as an **intake** whose assumptions and questions can be
edited, answered, removed, restored, or added one at a time. Every change is a new version
recording what changed, who changed it, when, and why; the AI's original wording stays as
version 1. Changes that could invalidate the drafted stories (any assumption change, or a
question being answered) are flagged, and a person decides: **re-draft with updates** (old
items are kept as *Superseded*) or **no backlog change needed** (the decision is logged).

## Quick start

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
.venv/bin/python -m backlog_pilot.app      # http://127.0.0.1:8000
```

The app starts in **offline demo mode** (`BP_LLM_PROVIDER=replay`). It needs no API key or
network for the AI step: the three demo scenarios on the intake page are served from recorded
responses in `data/replays/`, so a live demo still works on bad conference-room wifi.

### Live AI mode

```bash
BP_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
BP_MODEL=claude-opus-5-5
```

Live mode calls Claude through the Anthropic SDK with structured outputs, so every response is
validated against the backlog schema in `backlog_pilot/models.py`. Set `BP_RECORD=true` to save
live responses into `data/replays/`, then replay them offline in a demo.

## Built-in Help

**Help** in the side menu explains every screen, and the **?** in the app bar opens the
section about the screen you are on. The **rules and settings reference** is generated from
the same constants the rules run on, so it always matches what the app does; tests fail if a
screen has no help section or a setting is undocumented. Every app also serves the shared
*How Mission Control works* guide (from `mission-control-common`).

Across the suite, overriding a proposed score, rating, placement, or fix, rejecting a draft,
or dropping anything the AI drafted needs a reason; rewording a draft keeps the AI's original
beside it. Every refused attempt is audited, including actions tried without signing in and
failed sign-ins.

## Mission Control suite

Backlog Pilot is the hub of the **Mission Control** suite, with Leadership Pilot (listening
tour and weekly status), PLM Insight (engineering data quality and ECOs), Support Pilot
(answers and recurring problems), and App Portfolio (rationalization and licenses). Each is
a separate deployment, and all of them hand work to Backlog Pilot's inbox:

- **App menu** in the top bar (`MISSION_CONTROL_APPS`, the same in every app).
- **Intake inbox:** the other apps send work (Leadership Pilot sends ranked problems, with the
  stakeholders who raised them); a product owner drafts from one, and the approved intake links back to the problem.
- **Persona proposals:** personas drafted from interviews arrive as *Proposed* and join the
  library only when an approver accepts them.
- **Suite API:** server to server, authenticated with a shared bearer token
  (`BP_SUITE_TOKEN`; off when unset). In production, use Microsoft Entra ID app-to-app tokens.

## Enterprise deployment

Everything below is configured in `.env` (see `.env.example`). The adapters follow the
vendors' documented APIs and are covered by tests with simulated responses; they have not yet
been run against a live Jira, Azure DevOps, Foundry, or Entra ID tenant.

**Claude on Microsoft Foundry.** `BP_LLM_PROVIDER=foundry`, `BP_MODEL=<deployment name>`,
`ANTHROPIC_FOUNDRY_RESOURCE=<resource>`. With a *Hosted on Azure* deployment, prompts and
completions stay inside Azure (a US Data Zone option keeps inference in the US). Authenticate
with the Azure key, or leave it unset and install `.[azure]` to use Entra ID (managed identity
in Azure, `az login` locally), so no key is stored. Refusal fallback runs client-side there.

**Jira or Azure DevOps.** `BP_TRACKER=jira` or `ado`. Approved items are pushed parents
first (Epic, Feature, Story; ADO: Epic, Feature, User Story) with acceptance criteria, WSJF
(ADO: Business Value, Time Criticality, Effort fields), product, and a link back to the
Backlog Pilot key. Backlog Pilot remains the record of intakes and decisions. If a push fails,
the approval stands, the item shows *Sync pending* with the reason, and an approver can retry.

**Single sign-on.** `BP_AUTH=oidc`. In Entra ID: register an app (web redirect URI
`https://<host>/auth/callback`), create a client secret, add an app role named
`BacklogPilot.Approver`, and assign it to product owners and engineering leads. Everyone
signed in can draft and work on assumptions and questions; approving drafts, acknowledging
changes, scoring WSJF, and changing the roadmap need the approver role. Demo mode keeps the
"acting as" picker, with the same approver rule (Riley Adams and Sam Patel cannot approve).

## Demo script (5 minutes)

1. **Intake.** Click *ECO approvals are too slow*, then **Draft backlog**.
2. **Review.** Point out the "traced to input" count, click a story's source to highlight the
   stakeholder's words, and show an *AI inference* story the model added on its own. Point out
   persona chips and any proposed persona. Rename a story, untick one, and say why under *Why you dropped items*.
3. **Approve.** Edit an assumption, remove one, add one, then click **Approve & push**.
4. **Intake.** Open the intake link. Switch the *acting as* user to the Director of Engineering,
   answer a question, and show the "may affect the backlog" banner and the version history.
5. **Roadmap.** Open PLM-SAP Integration, re-score a feature on Priorities, and come back:
   the feature moves quarter, something else is pushed out, and both moves are logged with the
   reason. Finish on the portfolio view.
6. **Backlog.** The new epic sits next to the existing work, marked *AI-drafted*.
7. **Audit log.** Every draft and approval is recorded, which is the governance story.
8. Toggle light/dark in the app bar.

## Demo video

`scripts/record_demo_video.py` records the demo as a captioned MP4 (about 2.5 minutes). It
starts a private copy of the app in offline demo mode with a fresh database, drives Chrome
through the flow with Playwright, and overlays captions and a visible pointer, so every take
is identical. Re-record whenever the UI changes:

```bash
.venv/bin/pip install -e ".[demo]" && .venv/bin/python -m playwright install ffmpeg
.venv/bin/python scripts/record_demo_video.py --out ~/mission-control-suite/demo/backlog-pilot-demo.mp4
```

`--pace 1.3` slows it down for narration; `--dark` records the dark theme.

## Project layout

```
backlog_pilot/
  app.py              FastAPI routes (intake, review, approve, intakes, notes, backlog, audit)
  intakes.py          intakes, assumptions/questions, version history, materiality rules
  personas.py         persona library with version history
  roadmap.py          products, quarterly capacity, WSJF-driven planning, change log
  sync.py             push to Jira / Azure DevOps
  suite.py            Mission Control suite: intake inbox, suite API settings
  audit_search.py     full-text audit search, question plans, citation checks
  auth.py             demo users or OIDC sign-in, approver role
  llm/foundry.py      Claude on Microsoft Foundry
  db.py               engine, schema version, audit log
  models.py           Pydantic backlog schema (also the LLM output schema)
  pipeline/           decomposition, reviewer edits, quote tracing (trace.py),
                      quality checks and non-functional coverage (quality.py),
                      duplicate detection (similarity.py)
  llm/                providers: anthropic (live), replay (offline), prompts
  trackers/mock.py    SQLite stand-in for Jira, with audit log
  templates/ static/  server-rendered UI (Jinja2 + HTMX), Brightlayer UI styling
data/
  scenarios.json      fictional demo inputs
  sample_backlog.json fictional existing backlog (seeded on first run)
  users.json          fictional demo users for the "acting as" switcher
  personas.json       starter persona library (seeded on first run)
  products.json       five fictional products with default capacity
  replays/            recorded responses for offline mode
scripts/
  record_demo_video.py  captioned demo recording (Playwright)
tests/
```

## Roadmap

- **M1 (this release):** intake → draft → review → approve against the mock tracker; offline
  demo mode; Brightlayer UI styling; audit log.
- **M2 (done):** quality checks and non-functional coverage, WSJF scoring and the Priorities
  page, duplicate detection against the existing backlog.
- **M3 (done):** product roadmap planned from WSJF and quarterly capacity, portfolio view,
  pins, and a roadmap change log; product filters on Backlog and Priorities.
- **M4 (done, not yet verified against live systems):** Jira and Azure DevOps push with
  retry, Claude on Microsoft Foundry (Entra ID or key), single sign-on with approver roles.

## Data and branding

- All sample data and scenarios are fictional. Do not commit real company data.
- The UI uses the open-source Brightlayer UI color and theme values (BSD-3-Clause); see
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). This is an independent project, not affiliated with or endorsed by Eaton, and it uses no Eaton logos or trademarks.
- Secrets come only from environment variables; `.env` is git-ignored.

## Development

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

## License

Released under the [MIT License](LICENSE). Third-party components keep their own licenses; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
