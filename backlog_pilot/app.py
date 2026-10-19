"""FastAPI web app: intake -> AI draft -> human review -> push to tracker, then curate the
intake's assumptions and questions with full version history."""

import json
import re
from datetime import date
from urllib.parse import urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from mission_control import STATIC_PATH, install, install_help, suite_apps
from pydantic import ValidationError
from sqlalchemy.engine import Engine
from sqlmodel import Session

from backlog_pilot import audit_search
from backlog_pilot.auth import (
    AuthSettings,
    User,
    load_auth_settings,
    user_from_claims,
    user_from_session,
    user_to_session,
)
from backlog_pilot.config import PACKAGE_DIR, Settings, load_settings
from backlog_pilot.db import audit, make_engine, utcnow
from backlog_pilot.help_reference import reference
from backlog_pilot.intakes import ANSWERED, ASSUMPTION, QUESTION, Intake, IntakeStore, NoteError
from backlog_pilot.llm import DraftContext, LLMError, LLMProvider, ScoredFeature, build_provider
from backlog_pilot.llm.prompts import redraft_input
from backlog_pilot.models import WSJF_PARTS, WSJF_SCALE, Decomposition
from backlog_pilot.personas import RETIRED, PersonaError, PersonaStore, normalize
from backlog_pilot.pipeline.decompose import (
    apply_edits,
    decompose,
    parse_reviewed_notes,
    unexplained_adjustments,
)
from backlog_pilot.pipeline.quality import check_story, is_ready, nfr_coverage
from backlog_pilot.pipeline.similarity import find_overlaps
from backlog_pilot.pipeline.trace import coverage, highlight, trace_story
from backlog_pilot.roadmap import (
    HORIZON,
    Product,
    RoadmapStore,
    plan_product,
    quarter_label,
    quarter_of,
    quarters_from,
    slot_label,
)
from backlog_pilot.suite import SuiteSettings, SuiteStore, authorized, load_suite_settings
from backlog_pilot.sync import Tracker, build_tracker
from backlog_pilot.trackers import MockTracker

USER_COOKIE = "bp_user"
# Each screen's help button opens the guide section about it (checked at start-up).
PAGE_HELP = {
    "intake.html": "drafting#drafting-a-request",
    "intakes.html": "intakes#assumptions-and-questions",
    "intake_detail.html": "intakes#when-a-change-may-affect-the-backlog",
    "backlog.html": "backlog#reading-an-item",
    "priorities.html": "priorities#how-the-score-works",
    "roadmap.html": "roadmap#how-the-plan-is-built",
    "portfolio.html": "roadmap#portfolio-view",
    "personas.html": "personas#the-library",
    "audit.html": "audit#searching",
}


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else many or one + 's'}"


_AUTO = object()  # create_app default: build the external tracker from the environment


def create_app(
    settings: Settings | None = None,
    provider: LLMProvider | None = None,
    engine: Engine | None = None,
    external: Tracker | None | object = _AUTO,
    auth: AuthSettings | None = None,
    suite: SuiteSettings | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    suite = suite or load_suite_settings()
    provider = provider or build_provider(settings)
    external = build_tracker() if external is _AUTO else external
    auth = auth or load_auth_settings()
    engine = engine or make_engine(settings.db_path)
    tracker = MockTracker(engine)
    inbox = SuiteStore(engine)
    intakes = IntakeStore(engine)
    personas = PersonaStore(engine)
    roadmap = RoadmapStore(engine)
    roadmap.seed_if_empty(settings.products_path)
    tracker.seed_if_empty(settings.sample_backlog_path,
                          {p.key: p.id for p in roadmap.products()})
    personas.seed_if_empty(settings.personas_path)
    # Draft scenarios have no recording yet, so offline demo mode hides them.
    scenarios = [sc for sc in json.loads(settings.scenarios_path.read_text())["scenarios"]
                 if not sc.get("draft")]
    users = [User(u["id"], u["name"], u["role"], u.get("approver", False))
             for u in json.loads(settings.users_path.read_text())["users"]]
    ai_label = ("AI draft (recorded demo)" if provider.name == "replay"
                else f"AI draft ({settings.model})")

    app = FastAPI(title="Backlog Pilot")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy",
                                    "camera=(), microphone=(), geolocation=()")
        return response

    if auth.mode == "oidc":
        _enable_oidc(app, auth)
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    templates.env.globals.update(
        provider_name=provider.name, users=users, auth_mode=auth.mode,
        tracker_name=external.label if external else tracker.name)
    install(app, templates, "Backlog Pilot", [("Leadership Pilot", suite.leadership_url)])
    templates.env.filters["plural"] = plural
    templates.env.globals.update(wsjf_scale=WSJF_SCALE, wsjf_parts=WSJF_PARTS)
    templates.env.filters["quarter"] = quarter_label
    templates.env.filters["slot"] = slot_label
    templates.env.filters.update(highlight_terms=_highlight, cite_links=_cite_links,
                                 dict_without=lambda params, key: {
                                     k: v for k, v in params.items() if k != key})

    def current_user(request: Request) -> User:
        if auth.mode == "oidc":
            user = user_from_session(request.session.get("user"))
            return user or User("anonymous", "Signed out", "", False)
        wanted = request.cookies.get(USER_COOKIE)
        return next((u for u in users if u.id == wanted), users[0])

    def actor(request: Request) -> str:
        return current_user(request).actor

    def deny(request: Request) -> HTMLResponse | None:
        """None if the user may make decisions, otherwise the error to show."""
        if current_user(request).approver:
            return None
        with Session(engine) as s:  # refused attempts are audited, like every decision
            audit(s, actor(request), "access_denied", f"{request.method} {request.url.path}")
            s.commit()
        return HTMLResponse(templates.get_template("partials/error.html").render(
            message="Only approvers can make this decision. Ask a product owner or "
                    "engineering lead."), status_code=403)

    def push_to_tracker(who: str) -> tuple[int, int]:
        if external is None:
            return 0, 0
        return tracker.push_pending(external, who, {p.id: p.name for p in roadmap.products()})

    def render(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(
            request, name, {"nav": name, "user": current_user(request), **context})

    install_help(app, templates, render, PACKAGE_DIR / "help", reference=reference,
                 page_help=PAGE_HELP)

    def error(request: Request, message: str) -> HTMLResponse:
        return render(request, "partials/error.html", message=message)

    def log(who: str, action: str, detail: str) -> None:
        with Session(engine) as s:
            audit(s, who, action, detail)
            s.commit()

    def horizon() -> list[str]:
        return quarters_from(quarter_of(utcnow().date()), HORIZON)

    def plan_for(product: Product):
        quarters = horizon()
        return plan_product(tracker.feature_inputs(product.id), quarters,
                            roadmap.capacity(product, quarters))

    def replan(cause: str, who: str, product_ids: set[int | None] | None = None) -> None:
        """Recompute affected roadmaps and log every feature that moved quarter."""
        for product in roadmap.products():
            if product_ids is None or product.id in product_ids:
                roadmap.record(product, plan_for(product), cause, who)

    replan("Initial plan", "Backlog Pilot")  # baseline snapshots; logs nothing the first time

    def draft_context() -> DraftContext:
        scored = [ScoredFeature(r["item"].title, *(r["wsjf"][p] for p in WSJF_PARTS))
                  for r in tracker.ranked_features() if r["wsjf"]]
        return DraftContext(personas=personas.briefs(), scored_features=scored[:25])

    def review(request: Request, draft: Decomposition, trace_source: str, source_text: str,
               intake: Intake | None, inbox_id: str = "") -> HTMLResponse:
        """The review screen: stories traced to the input, quality-checked, WSJF-scored."""
        traces = {path: trace_story(trace_source, story.source_quotes)
                  for path, story in draft.stories()}
        known = personas.approved_names()
        issues = {path: check_story(story, traces[path], known)
                  for path, story in draft.stories()}
        overlaps = find_overlaps(
            draft, tracker.candidates(exclude_intake=intake.id if intake else None))
        suggested = {}
        for ei, epic in enumerate(draft.epics):
            text = " ".join([epic.title, epic.description] + [f.title for f in epic.features])
            best = roadmap.suggest_product(text)
            suggested[ei] = best.key if best else ""
        return render(
            request, "partials/review.html", issues=issues, overlaps=overlaps, inbox_id=inbox_id,
            products=roadmap.products(), suggested_products=suggested,
            ready=sum(is_ready(i) for i in issues.values()),
            nfr=nfr_coverage(trace_source, draft),
            draft=draft, payload=draft.model_dump_json(),
            source_text=source_text, intake=intake, traces=traces, coverage=coverage(traces),
            segments=highlight(trace_source, {p: s.source_quotes for p, s in draft.stories()}),
            known_personas=known,
            proposed_personas={normalize(p.name) for p in draft.proposed_personas})

    def notes_partial(request: Request, intake_id: int, message: str = "") -> HTMLResponse:
        return render(request, "partials/intake_notes.html", view=intakes.view(intake_id),
                      message=message)

    # ---------- intake -> draft -> approve ----------

    @app.get("/", response_class=HTMLResponse)
    def new_intake(request: Request):
        return render(request, "intake.html", scenarios=scenarios, counts=tracker.counts(),
                      inbox=inbox.inbox())

    @app.post("/decompose", response_class=HTMLResponse)
    def run_decompose(request: Request, text: str = Form(""), inbox_id: str = Form("")):
        try:
            draft = decompose(provider, text, draft_context())
        except LLMError as e:
            return error(request, str(e))
        log(actor(request), "draft_generated",
            f"{provider.name}: {len(draft.epics)} epics, {draft.story_count()} stories"
            + (f" (from inbox #{inbox_id})" if inbox_id.isdigit() else ""))
        return review(request, draft, text.strip(), text.strip(), None,
                      inbox_id=inbox_id if inbox_id.isdigit() else "")

    @app.post("/approve", response_class=HTMLResponse)
    async def approve(request: Request):
        if (denied := deny(request)) is not None:
            return denied
        form = await request.form()
        items = [(k, v) for k, v in form.multi_items() if isinstance(v, str)]
        fields = dict(items)
        try:
            draft = Decomposition.model_validate_json(fields.get("payload", ""))
        except ValidationError:
            return error(request, "The draft could not be read. Generate it again.")
        approved = apply_edits(draft, fields)
        if not approved.epics:
            return error(request, "Nothing selected. Keep at least one story to push.")
        if missing := unexplained_adjustments(approved):
            return error(request, "You changed the AI's WSJF score for "
                         + ", ".join(f"'{t}'" for t in missing)
                         + ". Say why next to the score, so the change can be understood "
                           "later.")
        reviewed = [parse_reviewed_notes(ASSUMPTION, draft.assumptions, items),
                    parse_reviewed_notes(QUESTION, draft.open_questions, items)]
        dropped = approved._dropped + [
            f"{r.kind} '{text}'" for r in reviewed for i, text in enumerate(r.originals)
            if i not in r.kept] + [
            f"persona '{p.name}'" for i, p in enumerate(draft.proposed_personas)
            if f"persona:{i}" not in fields]
        if dropped and not approved._drop_reason:
            return error(request, f"You dropped {len(dropped)} item"
                         f"{'' if len(dropped) == 1 else 's'} the AI drafted ("
                         + "; ".join(dropped[:3]) + ("; ..." if len(dropped) > 3 else "")
                         + "). Say why under 'Why you dropped items', so the decision can be "
                           "understood later.")
        who = actor(request)
        superseded: list[str] = []
        with Session(engine) as s:
            intake_id = fields.get("intake_id", "")
            if intake_id:
                intake = s.get(Intake, int(intake_id)) if intake_id.isdigit() else None
                if intake is None:
                    return error(request, "That intake no longer exists.")
                superseded = tracker.supersede_intake_items(s, intake.id, who)
                intakes.mark_redrafted(s, intake)
            else:
                intake = intakes.create(s, fields.get("source_text", ""), draft.summary, who)
                if fields.get("inbox_id", "").isdigit():
                    inbox.mark_drafted(s, int(fields["inbox_id"]), intake.id, who)
            for r in reviewed:
                intakes.add_reviewed(s, intake, r, who, ai_label, approved._drop_reason)
            if dropped:
                audit(s, who, "draft_items_dropped", f"{intake.key}: " + "; ".join(dropped)
                      + f" | reason: {approved._drop_reason}")
            for i, proposal in enumerate(draft.proposed_personas):
                if f"persona:{i}" in fields:
                    personas.accept_proposal(s, proposal, who, ai_label)
            keys = tracker.create_from(s, approved, who, intake.id, ai_label,
                                       {p.key: p.id for p in roadmap.products()})
            s.commit()
            intake_key, intake_pk = intake.key, intake.id
        replan(f"{intake_key} approved", who)
        synced, failed = push_to_tracker(who)
        return render(request, "partials/pushed.html", keys=keys,
                      stories=approved.story_count(), intake_key=intake_key,
                      intake_id=intake_pk, superseded=superseded, external=external,
                      synced=synced, sync_failed=failed)

    # ---------- intakes: assumptions and questions ----------

    @app.get("/intakes", response_class=HTMLResponse)
    def intake_list(request: Request):
        return render(request, "intakes.html", rows=intakes.all_intakes())

    @app.get("/intakes/{intake_id}", response_class=HTMLResponse)
    def intake_detail(request: Request, intake_id: int):
        try:
            view = intakes.view(intake_id)
        except NoteError as e:
            return error(request, str(e))
        stories = tracker.stories_for_intake(intake_id)
        quotes = {st.key: st.details.get("source_quotes", []) for st in stories}
        traces = {k: trace_story(view.intake.source_text, q) for k, q in quotes.items()}
        return render(request, "intake_detail.html", view=view,
                      epics=tracker.epics_for_intake(intake_id), message="",
                      origin=inbox.for_intake(intake_id),
                      segments=highlight(view.intake.source_text, quotes),
                      coverage=coverage(traces), stories=stories, traces=traces)

    @app.get("/intakes/{intake_id}/notes", response_class=HTMLResponse)
    def intake_notes(request: Request, intake_id: int):
        return notes_partial(request, intake_id)

    @app.post("/intakes/{intake_id}/notes", response_class=HTMLResponse)
    def add_note(request: Request, intake_id: int, kind: str = Form(...),
                 text: str = Form("")):
        try:
            intakes.add_note(intake_id, kind, text, actor(request))
        except NoteError as e:
            return notes_partial(request, intake_id, str(e))
        return notes_partial(request, intake_id)

    @app.get("/notes/{note_id}/edit", response_class=HTMLResponse)
    def edit_form(request: Request, note_id: int):
        note, _ = intakes.history(note_id)
        return render(request, "partials/note_edit.html", note=note)

    @app.post("/notes/{note_id}/edit", response_class=HTMLResponse)
    def edit_note(request: Request, note_id: int, text: str = Form(""),
                  answer: str | None = Form(None), reason: str = Form("")):
        intake_id = intakes.intake_for_note(note_id)
        try:
            intakes.edit_note(note_id, actor(request), text=text, answer=answer, reason=reason)
        except NoteError as e:
            return notes_partial(request, intake_id, str(e))
        return notes_partial(request, intake_id)

    @app.post("/notes/{note_id}/remove", response_class=HTMLResponse)
    def remove_note(request: Request, note_id: int):
        intake_id = intakes.intake_for_note(note_id)
        intakes.remove_note(note_id, actor(request), request.headers.get("HX-Prompt", ""))
        return notes_partial(request, intake_id)

    @app.post("/notes/{note_id}/restore", response_class=HTMLResponse)
    def restore_note(request: Request, note_id: int):
        intake_id = intakes.intake_for_note(note_id)
        intakes.restore_note(note_id, actor(request), request.headers.get("HX-Prompt", ""))
        return notes_partial(request, intake_id)

    @app.get("/notes/{note_id}/history", response_class=HTMLResponse)
    def note_history(request: Request, note_id: int):
        note, versions = intakes.history(note_id)
        return render(request, "partials/note_history.html", note=note, versions=versions)

    @app.post("/intakes/{intake_id}/acknowledge", response_class=HTMLResponse)
    def acknowledge(request: Request, intake_id: int):
        if (denied := deny(request)) is not None:
            return denied
        intakes.acknowledge(intake_id, actor(request), request.headers.get("HX-Prompt", ""))
        return notes_partial(request, intake_id)

    @app.post("/intakes/{intake_id}/redraft", response_class=HTMLResponse)
    def redraft(request: Request, intake_id: int):
        view = intakes.view(intake_id)
        text = redraft_input(
            view.intake.source_text,
            [n.text for n in view.notes[ASSUMPTION]],
            [(n.text, n.answer) for n in view.notes[QUESTION] if n.status == ANSWERED],
            [n.text for n in view.notes[QUESTION] if n.status != ANSWERED],
        )
        try:
            draft = decompose(provider, text, draft_context())
        except LLMError as e:
            if provider.name == "replay":
                return error(request, "Re-drafting needs live AI mode, because the offline "
                             "demo only has recordings of the original scenarios. Your "
                             "changes are saved; you can also mark them as reviewed.")
            return error(request, str(e))
        log(actor(request), "redraft_generated",
            f"{view.intake.key}: {len(draft.epics)} epics, {draft.story_count()} stories")
        return review(request, draft, text, "", view.intake)

    # ---------- backlog, audit, user ----------

    @app.get("/backlog", response_class=HTMLResponse)
    def backlog(request: Request, superseded: bool = False, product: str = ""):
        products = roadmap.products()
        selected = next((p for p in products if p.key == product), None)
        tree = [e for e in tracker.tree(superseded)
                if selected is None or e["item"].product_id == selected.id]
        sources: dict[int, str] = {}
        traces = {}
        for epic in tree:
            for feature in epic["children"]:
                for node in feature["children"]:
                    story = node["item"]
                    if story.intake_id is None:
                        continue
                    if story.intake_id not in sources:
                        sources[story.intake_id] = intakes.view(story.intake_id).intake.source_text
                    traces[story.key] = trace_story(sources[story.intake_id],
                                                    story.details.get("source_quotes", []))
        return render(request, "backlog.html", tree=tree, counts=tracker.counts(),
                      show_superseded=superseded, traces=traces,
                      known_personas=personas.approved_names(), products=products,
                      selected=selected, product_names={p.id: p.name for p in products},
                      pending_sync=len(tracker.pending_sync()) if external else 0)

    # ---------- priorities (WSJF) ----------

    @app.get("/priorities", response_class=HTMLResponse)
    def priorities(request: Request, edit: str = "", message: str = "", product: str = ""):
        products = roadmap.products()
        selected = next((p for p in products if p.key == product), None)
        rows = [r for r in tracker.ranked_features()
                if selected is None or r["item"].product_id == selected.id]
        return render(request, "priorities.html", rows=rows, edit=edit, message=message,
                      products=products, selected=selected,
                      product_names={p.id: p.name for p in products})

    @app.post("/features/{key}/wsjf", response_class=HTMLResponse)
    async def rescore(request: Request, key: str):
        if (denied := deny(request)) is not None:
            return denied
        form = await request.form()
        values = {}
        for part in WSJF_PARTS:
            raw = str(form.get(part, ""))
            values[part] = int(raw) if raw.isdigit() else 0
        try:
            tracker.set_wsjf(key, values, actor(request), str(form.get("reason", "")))
        except ValueError as e:
            return priorities(request, edit=key, message=str(e))
        replan(f"WSJF re-score of {key}", actor(request), {tracker.product_of(key)})
        back = urlparse(request.headers.get("referer", "")).path
        return RedirectResponse(back if back.startswith("/roadmap") else "/priorities",
                                status_code=303)

    # ---------- roadmap ----------

    @app.get("/roadmap", response_class=HTMLResponse)
    def roadmap_page(request: Request, product: str = "", message: str = ""):
        products = roadmap.products()
        selected = next((p for p in products if p.key == product), products[0])
        return render(request, "roadmap.html", products=products, selected=selected,
                      plan=plan_for(selected), changes=roadmap.changes(selected, 12),
                      message=message)

    @app.get("/roadmap/portfolio", response_class=HTMLResponse)
    def portfolio(request: Request):
        rows = [{"product": p, "plan": plan_for(p)} for p in roadmap.products()]
        return render(request, "portfolio.html", rows=rows, quarters=horizon(),
                      changes=roadmap.changes(None, 12),
                      product_names={p.id: p.name for p in roadmap.products()})

    @app.post("/roadmap/{key}/capacity")
    async def set_capacity(request: Request, key: str):
        if (denied := deny(request)) is not None:
            return denied
        product = roadmap.product(key)
        form = await request.form()
        who = actor(request)
        for q in horizon():
            raw = str(form.get(f"cap:{q}", ""))
            if raw.isdigit():
                roadmap.set_capacity(product, q, int(raw), who)
        replan(f"Capacity change for {product.name}", who, {product.id})
        return RedirectResponse(f"/roadmap?product={key}", status_code=303)

    @app.post("/features/{key}/pin")
    def pin(request: Request, key: str, quarter: str = Form(""), reason: str = Form("")):
        if (denied := deny(request)) is not None:
            return denied
        who = actor(request)
        try:
            product_id = tracker.set_pin(key, quarter if quarter in horizon() else None, reason,
                                         who)
        except ValueError as e:
            pid = tracker.product_of(key)
            product = roadmap.product_by_id(pid) if pid else None
            return roadmap_page(request, product=product.key if product else "", message=str(e))
        replan(f"{key} {'pinned to ' + quarter_label(quarter) if quarter else 'unpinned'}",
               who, {product_id})
        product = roadmap.product_by_id(product_id) if product_id else None
        return RedirectResponse(f"/roadmap?product={product.key if product else ''}",
                                status_code=303)

    @app.post("/products/{key}")
    def edit_product(request: Request, key: str, name: str = Form(...),
                     description: str = Form(""), default_capacity: int = Form(13)):
        if (denied := deny(request)) is not None:
            return denied
        who = actor(request)
        roadmap.update_product(key, who, name, description, default_capacity)
        product = roadmap.product(key)
        replan(f"Default capacity for {product.name}", who, {product.id})
        return RedirectResponse(f"/roadmap?product={key}", status_code=303)

    # ---------- personas ----------

    def personas_page(request: Request, message: str = "", edit_id: int | None = None):
        return render(request, "personas.html", personas=personas.all_personas(),
                      usage=tracker.persona_usage(), message=message, edit_id=edit_id)

    @app.get("/personas", response_class=HTMLResponse)
    def persona_list(request: Request, edit: int | None = None):
        return personas_page(request, edit_id=edit)

    @app.post("/personas", response_class=HTMLResponse)
    def persona_add(request: Request, name: str = Form(""), description: str = Form(""),
                    goals: str = Form(""), pain_points: str = Form("")):
        if (denied := deny(request)) is not None:
            return denied
        try:
            personas.add(actor(request), name, description, goals, pain_points)
        except PersonaError as e:
            return personas_page(request, str(e))
        return RedirectResponse("/personas", status_code=303)

    @app.post("/personas/{persona_id}/edit", response_class=HTMLResponse)
    def persona_edit(request: Request, persona_id: int, name: str = Form(""),
                     description: str = Form(""), goals: str = Form(""),
                     pain_points: str = Form(""), reason: str = Form("")):
        if (denied := deny(request)) is not None:
            return denied
        try:
            personas.edit(persona_id, actor(request), name, description, goals, pain_points,
                          reason)
        except PersonaError as e:
            return personas_page(request, str(e), edit_id=persona_id)
        return RedirectResponse("/personas", status_code=303)

    @app.post("/personas/{persona_id}/status", response_class=HTMLResponse)
    def persona_status(request: Request, persona_id: int, status: str = Form(...),
                       reason: str = Form("")):
        if (denied := deny(request)) is not None:
            return denied
        try:
            personas.set_status(persona_id, actor(request),
                                RETIRED if status in (RETIRED, "rejected") else "approved",
                                reason)
        except PersonaError as e:
            return personas_page(request, str(e))
        return RedirectResponse("/personas", status_code=303)

    @app.get("/personas/{persona_id}/history", response_class=HTMLResponse)
    def persona_history(request: Request, persona_id: int):
        persona, versions = personas.history(persona_id)
        return render(request, "partials/persona_history.html", persona=persona,
                      versions=versions)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_log(request: Request, q: str = "", actor_filter: str = "", category: str = "",
                  item: str = "", since: str = "", until: str = "", page: int = 1,
                  ask: str = ""):
        page_size = 50
        message, plan, answer, cited, unverified = "", None, None, [], []
        if ask.strip():
            try:
                plan = provider.plan_audit(ask, utcnow().date(),
                                           audit_search.actors(engine))
            except LLMError as e:
                message = str(e)
        if plan is not None:
            query = plan.to_query()
            log(actor(request), "audit_asked", f"{ask.strip()} | plan: {plan.explanation}")
        else:
            query = audit_search.AuditQuery(
                text=q, actor=actor_filter, categories=[category] if category else [],
                item_keys=[k.upper() for k in re.findall(r"(?:BP|IN)-\d+", item, re.I)],
                since=_parse_date(since), until=_parse_date(until))
        results = audit_search.search(engine, query, limit=page_size,
                                      offset=(max(page, 1) - 1) * page_size)
        if plan is not None and results.events:
            try:
                raw = provider.answer_audit(ask, results.events[:200])
            except LLMError as e:
                raw, message = None, str(e)
            if raw is not None:
                answer, cited, unverified = audit_search.verify_answer(raw, results.events)
        questions = json.loads(settings.audit_questions_path.read_text())["questions"]
        return render(request, "audit.html", events=results.events, total=results.total,
                      page=max(page, 1), page_size=page_size, query=query, ask=ask,
                      plan=plan, answer=answer, cited=cited, unverified=unverified,
                      summary=audit_search.summarize(results.events) if plan else {},
                      message=message, questions=questions,
                      people=audit_search.actors(engine),
                      categories=audit_search.CATEGORY_LABELS,
                      terms=[t for t in re.findall(r"\w+", query.text) if len(t) > 1])

    # ---------- Mission Control suite API (server to server) ----------

    def source_app_of(body: dict) -> str:
        """The sending app: as stated, else matched from its link against the suite list."""
        if str(body.get("source_app", "")).strip():
            return str(body["source_app"]).strip()
        url = str(body.get("source_url", ""))
        return next((a.name for a in suite_apps("Backlog Pilot", [])
                     if a.url and a.url != "/" and url.startswith(a.url)), "Leadership Pilot")

    def api_denied(request: Request) -> JSONResponse | None:
        if authorized(suite, request.headers.get("authorization", "")):
            return None
        with Session(engine) as s:
            audit(s, "suite API", "access_denied",
                  f"{request.method} {request.url.path}: missing or wrong suite token")
            s.commit()
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    @app.post("/api/inbox")
    async def api_inbox(request: Request):
        if (denied := api_denied(request)) is not None:
            return denied
        body = await request.json()
        try:
            item = inbox.receive(str(body.get("title", "")), str(body.get("text", "")),
                                 str(body.get("source_ref", "")),
                                 str(body.get("source_url", "")),
                                 [str(x) for x in body.get("stakeholders", [])],
                                 str(body.get("submitted_by", "")),
                                 source_app_of(body))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=422)
        return JSONResponse({"id": item.id, "status": item.status}, status_code=201)

    @app.get("/api/inbox/{item_id}")
    def api_inbox_status(request: Request, item_id: int):
        if (denied := api_denied(request)) is not None:
            return denied
        item = inbox.get(item_id)
        if item is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        intake_key = intakes.view(item.intake_id).intake.key if item.intake_id else None
        return {"id": item.id, "status": item.status, "intake_key": intake_key,
                "intake_path": f"/intakes/{item.intake_id}" if item.intake_id else None}

    @app.post("/api/personas/proposals")
    async def api_propose_persona(request: Request):
        if (denied := api_denied(request)) is not None:
            return denied
        body = await request.json()
        try:
            persona = personas.propose(
                f"{body.get('submitted_by', 'Someone')} via Leadership Pilot",
                str(body.get("name", "")), str(body.get("description", "")),
                [str(g) for g in body.get("goals", [])],
                [str(p) for p in body.get("pain_points", [])])
        except PersonaError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse({"id": persona.id, "status": persona.status}, status_code=201)

    @app.get("/api/portfolio")
    def api_portfolio(request: Request):
        """Read-only roadmap view for Leadership Pilot's status narrator: per product, the
        computed plan (features by quarter, capacity, load) and the logged roadmap moves."""
        if (denied := api_denied(request)) is not None:
            return denied
        products = []
        for product in roadmap.products():
            plan = plan_for(product)
            features = [{"key": p.feature.key, "title": p.feature.title,
                         "epic": p.feature.epic_title, "status": p.feature.status,
                         "size": p.feature.size, "wsjf": p.feature.score, "quarter": q,
                         "reason": p.reason}
                        for q, items in plan.placed.items() for p in items]
            features += [{"key": p.feature.key, "title": p.feature.title,
                          "epic": p.feature.epic_title, "status": p.feature.status,
                          "size": p.feature.size, "wsjf": p.feature.score, "quarter": "",
                          "reason": p.reason} for p in plan.unplanned]
            products.append({
                "key": product.key, "name": product.name,
                "capacity": plan.capacity, "load": plan.used,
                "features": features,
                "unscored": [{"key": f.key, "title": f.title, "status": f.status}
                             for f in plan.unscored],
                "done": [{"key": f.key, "title": f.title} for f in plan.done],
                "changes": [{"feature_key": c.feature_key, "feature_title": c.feature_title,
                             "from": c.from_quarter, "to": c.to_quarter, "cause": c.cause,
                             "changed_by": c.changed_by, "changed_at": c.changed_at.isoformat()}
                            for c in roadmap.changes(product, limit=100)],
            })
        return {"as_of": utcnow().date().isoformat(), "quarters": horizon(),
                "source": "Backlog Pilot", "products": products}

    @app.post("/inbox/{item_id}/dismiss")
    def dismiss_inbox(request: Request, item_id: int):
        if (denied := deny(request)) is not None:
            return denied
        inbox.dismiss(item_id, actor(request), request.headers.get("HX-Prompt", ""))
        return RedirectResponse("/", status_code=303)

    @app.post("/sync/retry", response_class=HTMLResponse)
    def retry_sync(request: Request):
        if (denied := deny(request)) is not None:
            return denied
        push_to_tracker(actor(request))
        return RedirectResponse("/backlog", status_code=303)

    @app.post("/user")
    def switch_user(request: Request, user_id: str = Form(...)):
        back = urlparse(request.headers.get("referer", "")).path
        if not back.startswith("/") or back.startswith("//"):  # same-site paths only
            back = "/"
        response = RedirectResponse(back, status_code=303)
        if any(u.id == user_id for u in users):
            response.set_cookie(USER_COOKIE, user_id, httponly=True, samesite="lax")
        return response

    return app


def _highlight(value: str, terms: list[str]) -> Markup:
    """HTML-escape text, then mark case-insensitive matches of the search words."""
    escaped = str(escape(value))
    if not terms:
        return Markup(escaped)
    pattern = re.compile("|".join(re.escape(str(escape(t))) for t in terms), re.IGNORECASE)
    return Markup(pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped))


def _cite_links(answer: str) -> Markup:
    """Escape an AI answer, then turn [#42] citations into links to their events."""
    return Markup(re.sub(r"\[#(\d+)\]", r'<a class="cite" href="#ev-\1">[#\1]</a>',
                         str(escape(answer))))


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


# The suite API authenticates with its own bearer token, not a browser session.
PUBLIC_PATHS = ("/static", STATIC_PATH, "/login", "/auth/callback", "/logout", "/healthz", "/api/")


def _enable_oidc(app: FastAPI, auth: AuthSettings) -> None:
    """Sign-in with Microsoft Entra ID (or any OpenID Connect provider)."""
    from authlib.integrations.starlette_client import OAuth
    from starlette.middleware.sessions import SessionMiddleware

    oauth = OAuth()
    oauth.register("oidc", server_metadata_url=auth.metadata_url, client_id=auth.client_id,
                   client_secret=auth.client_secret,
                   client_kwargs={"scope": "openid profile email"})

    @app.middleware("http")
    async def require_sign_in(request: Request, call_next):
        if request.url.path.startswith(PUBLIC_PATHS) or request.session.get("user"):
            return await call_next(request)
        if request.method == "GET" and "hx-request" not in request.headers:
            request.session["next"] = request.url.path  # a sign-in prompt, not a refusal
            return RedirectResponse("/login", status_code=303)
        with Session(request.app.state.engine) as s:  # an action tried without signing in
            audit(s, "not signed in", "access_denied",
                  f"{request.method} {request.url.path} from "
                  f"{request.client.host if request.client else 'unknown'}")
            s.commit()
        return HTMLResponse("Sign in required.", status_code=401)

    # Added after require_sign_in so it runs first and provides request.session.
    app.add_middleware(SessionMiddleware, secret_key=auth.session_secret, same_site="lax",
                       https_only=auth.https_only, max_age=8 * 3600)

    @app.get("/login")
    async def login(request: Request):
        return await oauth.oidc.authorize_redirect(request, request.url_for("auth_callback"))

    @app.get("/auth/callback", name="auth_callback")
    async def auth_callback(request: Request):
        try:
            token = await oauth.oidc.authorize_access_token(request)  # verifies the ID token
            user = user_from_claims(token["userinfo"], auth)
        except Exception as e:  # any failure (state, token, claims) is refused and audited
            with Session(request.app.state.engine) as s:
                audit(s, "not signed in", "sign_in_failed",
                      f"{type(e).__name__} from "
                      f"{request.client.host if request.client else 'unknown'}")
                s.commit()
            return HTMLResponse('Sign-in failed. <a href="/login">Try again</a>.',
                                status_code=401)
        request.session["user"] = user_to_session(user)
        with Session(request.app.state.engine) as s:
            audit(s, user.actor, "signed_in", user.email or user.id)
            s.commit()
        return RedirectResponse(request.session.pop("next", "/"), status_code=303)

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)


def main() -> None:
    import uvicorn

    uvicorn.run("backlog_pilot.app:create_app", factory=True, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
