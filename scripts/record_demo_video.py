"""Record the Backlog Pilot demo as a captioned screen video.

Starts a private copy of the app (offline demo mode, fresh database), drives it through the
demo flow in Chrome with Playwright, overlays captions and a visible pointer, and writes an
MP4. Every run is identical, so re-record any time the UI changes.

    pip install -e ".[demo]" && python -m playwright install ffmpeg
    python scripts/record_demo_video.py --out ~/mission-control-suite/demo/backlog-pilot-demo.mp4
"""

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Locator, Page, sync_playwright

REPO = Path(__file__).resolve().parent.parent
WIDTH, HEIGHT = 1440, 900

OVERLAY_JS = """
(() => {
  const install = () => {
    if (document.getElementById('demo-caption')) return;
    const style = document.createElement('style');
    style.textContent = `
      #demo-caption { position: fixed; left: 50%; bottom: 28px; transform: translateX(-50%);
        max-width: 1100px; padding: 14px 24px; border-radius: 10px; z-index: 99999;
        background: rgba(17, 23, 29, .88); color: #fff;
        font: 600 21px/1.4 "Open Sans", sans-serif;
        text-align: center; box-shadow: 0 8px 24px rgba(0,0,0,.35); transition: opacity .3s; }
      #demo-caption:empty { opacity: 0; }
      #demo-cursor { position: fixed; width: 22px; height: 22px; margin: -11px 0 0 -11px;
        border-radius: 50%; background: rgba(240, 170, 31, .55); border: 2px solid #f0aa1f;
        z-index: 100000; pointer-events: none; left: 50%; top: 50%;
        transition: left .6s ease-in-out, top .6s ease-in-out, transform .15s; }
      #demo-cursor.press { transform: scale(.7); }
      #demo-card { position: fixed; inset: 0; z-index: 100001; display: flex;
        flex-direction: column;
        align-items: center; justify-content: center; background: #005eb8; color: #fff;
        font-family: "Open Sans", sans-serif; text-align: center; padding: 40px; }
      #demo-card h1 { font-size: 54px; font-weight: 300; margin: 0 0 16px; }
      #demo-card p { font-size: 24px; margin: 6px 0; opacity: .92; max-width: 1000px; }`;
    document.head.appendChild(style);
    const caption = document.createElement('div');
    caption.id = 'demo-caption';
    caption.textContent = sessionStorage.getItem('demo-caption') || '';
    const cursor = document.createElement('div');
    cursor.id = 'demo-cursor';
    const pos = (sessionStorage.getItem('demo-cursor') || '').split(',');
    if (pos.length === 2) { cursor.style.left = pos[0]; cursor.style.top = pos[1]; }
    document.body.append(caption, cursor);
  };
  window.__demoCaption = (text) => {
    install();
    sessionStorage.setItem('demo-caption', text);
    document.getElementById('demo-caption').textContent = text;
  };
  window.__demoCursor = (x, y, press) => {
    install();
    const c = document.getElementById('demo-cursor');
    c.style.left = x + 'px'; c.style.top = y + 'px';
    sessionStorage.setItem('demo-cursor', x + 'px,' + y + 'px');
    c.classList.toggle('press', !!press);
  };
  window.__demoCard = (title, lines) => {
    install();
    let card = document.getElementById('demo-card');
    if (!card) {
      card = document.createElement('div');
      card.id = 'demo-card';
      document.body.appendChild(card);
    }
    card.innerHTML = '<h1></h1>' + lines.map(() => '<p></p>').join('');
    card.querySelector('h1').textContent = title;
    card.querySelectorAll('p').forEach((p, i) => p.textContent = lines[i]);
  };
  window.__demoCardOff = () => document.getElementById('demo-card')?.remove();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
  else install();
})();
"""


READ_BASE, READ_CHARS_PER_SEC = 1.2, 14.0  # time to read a caption: 1.2 s + 14 chars/s


def read_time(text: str) -> float:
    return READ_BASE + len(text) / READ_CHARS_PER_SEC


class Demo:
    def __init__(self, page: Page, base: str, pace: float):
        self.page, self.base, self.pace = page, base, pace
        self.started = time.monotonic()  # the video starts when the page opens
        self.cues: list[tuple[float, str]] = []  # (seconds into the video, caption)
        self._on_screen: tuple[str, float] | None = None  # caption or card, and when it appeared

    def readable(self) -> None:
        """Wait until the caption or card on screen has been up long enough to read."""
        if self._on_screen:
            text, at = self._on_screen
            left = read_time(text) - (time.monotonic() - at)
            if left > 0:
                self.page.wait_for_timeout(left * 1000)

    def mark(self, text: str) -> None:
        self.cues.append((time.monotonic() - self.started, text))

    def wait(self, seconds: float) -> None:
        self.page.wait_for_timeout(seconds * 1000 * self.pace)

    def caption(self, text: str, hold: float = 3.5) -> None:
        self.readable()
        self.page.evaluate("t => window.__demoCaption(t)", text)
        self.mark(text)
        self._on_screen = (text, time.monotonic()) if text else None
        self.wait(hold)

    def card(self, title: str, lines: list[str], hold: float = 4.5) -> None:
        self.readable()
        text = " · ".join([title, *lines])
        self.page.evaluate("([t, l]) => window.__demoCard(t, l)", [title, lines])
        self.mark(text)
        self._on_screen = (text, time.monotonic())
        self.wait(hold)
        self.readable()
        self.page.evaluate("() => window.__demoCardOff()")
        self._on_screen = None

    def goto(self, path: str) -> None:
        self.readable()
        self.page.goto(self.base + path)
        self.page.wait_for_load_state("networkidle")

    def show(self, target: Locator, hold: float = 0.6) -> None:
        target.scroll_into_view_if_needed()
        self.page.evaluate(
            "el => window.scrollBy({top: el.getBoundingClientRect().top - innerHeight * 0.3,"
            " behavior: 'smooth'})", target.element_handle())
        self.wait(1.0)
        box = target.bounding_box()
        if box:
            self.page.evaluate("([x, y]) => window.__demoCursor(x, y)",
                               [box["x"] + min(box["width"] / 2, 60), box["y"] + box["height"] / 2])
        self.wait(hold)

    def click(self, target: Locator, hold: float = 0.8) -> None:
        self.show(target, 0.7)
        box = target.bounding_box()
        if box:
            self.page.evaluate("([x, y]) => window.__demoCursor(x, y, true)",
                               [box["x"] + min(box["width"] / 2, 60), box["y"] + box["height"] / 2])
        target.click()
        self.wait(hold)

    def type(self, target: Locator, text: str) -> None:
        self.click(target, 0.3)
        target.press_sequentially(text, delay=35 * self.pace)
        self.wait(0.5)

    def select(self, target: Locator, value: str, hold: float = 1.2) -> None:
        self.show(target, 0.5)
        target.select_option(value)
        self.wait(hold)


def run_flow(d: Demo) -> None:
    page = d.page
    page.on("dialog", lambda dialog: dialog.accept(
        "Steering committee confirmed the stories still hold"))

    d.goto("/")
    d.card("Backlog Pilot", ["An AI backlog copilot for Product Engineering & R&D IT",
                             "AI drafts. People decide. Every decision is on record."], 5)

    d.caption("A stakeholder sends a request: suppliers keep building to old drawing revisions.")
    d.click(page.get_by_text("Suppliers build to old revisions"))
    d.caption("One click turns it into epics, features, and stories.", 1.5)
    d.click(page.get_by_role("button", name="Draft backlog"), 0.5)
    page.wait_for_selector("text=Draft for review")
    d.wait(1)

    d.caption("Every story is traced to the stakeholder's own words, checked by code, "
              "not trusted from the model.", 1)
    d.show(page.locator(".source-panel"), 2)
    d.click(page.locator(".trace-jump").first, 2.5)

    d.caption("Quality checks run as rules: ready stories, and coverage of security, "
              "export control, and audit needs.", 1)
    d.show(page.locator(".quality-panel"), 3)
    defect = page.get_by_text("No acceptance criteria").first
    if defect.count():
        d.caption("They caught a real model defect: a story with no acceptance criteria.", 0.5)
        d.show(defect, 3)

    overlap = page.locator(".overlap").first
    if overlap.count():
        d.caption("Duplicate detection: this overlaps work already in the backlog. "
                  "The reviewer links it as related.", 0.5)
        d.show(overlap, 3)

    d.caption("WSJF is drafted per feature. People adjust it, with a reason, and the score "
              "updates live; the AI's numbers stay in history.", 0.5)
    size = page.locator(".wsjf-editor select[data-part='job_size']").first
    d.select(size, "3", 1)
    d.type(page.locator(".wsjf-reason").first, "Reuses the existing supplier portal")
    d.wait(1.5)

    d.caption("Assumptions are editable. Every change is versioned, with the AI's wording kept "
              "as version 1.", 0.5)
    assumption = page.locator(".note-edit-list textarea").first
    d.click(assumption, 0.3)
    page.keyboard.press("End")
    assumption.press_sequentially(" (confirmed with purchasing)", delay=35 * d.pace)
    d.wait(1.5)

    proposals = page.get_by_text("New personas proposed")
    if proposals.count():
        d.caption("The AI proposed new personas that were not in the library. "
                  "A person decides whether they join.", 0.5)
        d.show(proposals, 3)

    d.caption("Nothing reaches the tracker until an approver says so.", 0.5)
    d.click(page.get_by_role("button", name="Approve"), 0.5)
    page.wait_for_selector(".banner.success")
    d.show(page.locator(".banner.success"), 3)

    d.caption("The request lives on as an intake. Now the Director of Engineering answers "
              "a question.", 0.5)
    d.click(page.get_by_role("link", name="Open IN-1"), 0.5)
    page.wait_for_load_state("networkidle")
    d.select(page.locator(".user-switch select"), "stake", 0.5)
    page.wait_for_load_state("networkidle")
    d.caption("Riley Adams is not an approver, but can answer questions and edit assumptions.",
              2)
    question = page.locator("li.note").filter(has=page.locator("[title='Edit or answer']")).first
    d.click(question.locator("[title='Edit or answer']"), 0.8)
    d.type(page.locator("textarea[name='answer']"),
           "Yes: notify suppliers for every released revision, including fast-track changes.")
    d.click(page.get_by_role("button", name="Save version 2"), 1)
    d.caption("Answers can invalidate stories, so the change is flagged: re-draft, or record "
              "that no change is needed.", 0.5)
    d.show(page.locator(".banner.warning"), 3)
    d.click(page.locator("li.note [title='Version history']").first, 2.5)

    d.select(page.locator(".user-switch select"), "po", 0.5)
    page.wait_for_load_state("networkidle")
    d.caption("The product owner decides no backlog change is needed. The reason is logged.", 0.5)
    d.click(page.get_by_role("button", name="No backlog change needed"), 2)

    d.goto("/roadmap?product=supplier")
    d.caption("The roadmap is computed from WSJF and quarterly capacity for each product.", 3)
    d.goto("/priorities?product=supplier")
    d.caption("A customer escalation makes PPAP resubmission urgent. Re-score it...", 0.5)
    row = page.locator("tr").filter(has_text="PPAP resubmission")
    target = row.get_by_role("link", name="Re-score") if row.count() else \
        page.get_by_role("link", name="Re-score").last
    d.click(target.first, 0.8)
    for part, value in [("business_value", "20"), ("time_criticality", "20"),
                        ("risk_reduction", "13"), ("job_size", "5")]:
        d.select(page.locator(f".wsjf-form select[name='{part}']"), value, 0.4)
    d.type(page.locator(".wsjf-form input[name='reason']"), "Field escape at a customer plant")
    d.click(page.get_by_role("button", name="Save"), 1)
    d.goto("/roadmap?product=supplier")
    d.caption("...and the roadmap shows what moves, before anyone commits. Every move is logged "
              "with its cause and owner.", 4.5)
    d.show(page.locator(".product-settings").first, 0.2)
    d.show(page.get_by_text("Roadmap changes"), 3)

    d.goto("/roadmap/portfolio")
    d.caption("Leadership gets the portfolio view: every product, every quarter, capacity used.",
              5)

    d.goto("/audit")
    d.caption("Auditors can ask the log in plain English. The AI only builds the search; "
              "answers come from real events.", 1)
    d.click(page.get_by_text("Who changed WSJF scores, and what reasons did they give?"), 1)
    page.wait_for_load_state("networkidle")
    d.show(page.locator(".answer-card"), 4)

    d.caption("", 0.2)
    d.card("Backlog Pilot", ["Traced to stakeholder words · quality-checked · prioritized · "
                             "roadmapped · audited",
                             "Runs on Claude in Microsoft Foundry · pushes to Jira or Azure DevOps",
                             "Built by LaVon Rutledge · Aenea Labs"], 6)


def _stamp(seconds: float) -> str:
    m, s = divmod(max(seconds, 0.0), 60)
    return f"00:{int(m):02d}:{s:06.3f}"


def write_captions(cues: list[tuple[float, str]], end: float, vtt: Path) -> None:
    """WebVTT subtitles: each caption shows until the next one (empty captions end a cue)."""
    lines = ["WEBVTT", ""]
    for i, (start, text) in enumerate(cues):
        stop = cues[i + 1][0] if i + 1 < len(cues) else end
        if text.strip():
            lines += [str(i + 1), f"{_stamp(start)} --> {_stamp(stop)}", text, ""]
    vtt.write_text("\n".join(lines))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(Path.home() / "mission-control-suite" / "demo"
                                             / "backlog-pilot-demo.mp4"))
    parser.add_argument("--pace", type=float, default=1.0, help="<1 faster, >1 slower")
    parser.add_argument("--dark", action="store_true", help="record in the dark theme")
    args = parser.parse_args()
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    work = Path(tempfile.mkdtemp(prefix="bp-demo-"))
    port = free_port()
    env = os.environ | {"BP_LLM_PROVIDER": "replay", "BP_DB_PATH": str(work / "demo.db"),
                        "BP_TRACKER": "mock", "BP_AUTH": "demo", "BP_RECORD": "false"}
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backlog_pilot.app:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=REPO, env=env)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(base + "/", timeout=1)
                break
            except OSError:
                time.sleep(0.5)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context(
                viewport={"width": WIDTH, "height": HEIGHT},
                record_video_dir=str(work / "video"),
                record_video_size={"width": WIDTH, "height": HEIGHT},
                color_scheme="dark" if args.dark else "light")
            context.add_init_script(OVERLAY_JS)
            page = context.new_page()
            demo = Demo(page, base, args.pace)
            run_flow(demo)
            ended = time.monotonic() - demo.started
            video = page.video.path()
            context.close()
            browser.close()
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            import imageio_ffmpeg

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(video), "-c:v", "libx264",
                        "-preset", "slow", "-crf", "22", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", str(out)], check=True)
        write_captions(demo.cues, ended, out.with_suffix(".vtt"))
        print(f"Wrote {out} and {out.with_suffix('.vtt')}")
        return 0
    finally:
        server.terminate()
        server.wait(timeout=10)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
