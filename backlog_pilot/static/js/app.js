// This app's screen behavior. Theme, phone menu, and the suite menu are in Mission Control's
// shared suite.js.
(function () {
  document.addEventListener("click", (event) => {
    // Review screen: remove (or undo removing) an AI-suggested note; delete a newly added one.
    const remove = event.target.closest(".note-remove");
    if (remove) {
      const row = remove.closest(".note-row");
      const input = row.querySelector("textarea, input");
      if (input.name.endsWith(":new")) { row.remove(); return; }
      const removed = row.classList.toggle("removed");
      input.disabled = removed;  // disabled inputs are not submitted, which marks the removal
      remove.querySelector(".material-symbols-outlined").textContent = removed ? "undo" : "close";
      remove.title = removed ? "Undo remove" : "Remove";
      return;
    }
    const add = event.target.closest(".note-add");
    if (add) {
      const row = document.createElement("div");
      row.className = "note-row";
      const input = document.createElement("textarea");
      input.rows = 1;
      input.name = add.dataset.name;
      input.placeholder = add.dataset.label;
      input.setAttribute("aria-label", add.dataset.label);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "icon-button note-remove";
      button.title = "Remove";
      button.setAttribute("aria-label", "Remove");
      button.innerHTML = '<span class="material-symbols-outlined">close</span>';
      row.append(input, button);
      add.before(row);
      input.focus();
      return;
    }
    // Review screen: jump from a story to the stakeholder words it quotes.
    const jump = event.target.closest(".trace-jump");
    if (jump) {
      const path = jump.dataset.path;
      document.querySelectorAll("mark.active, .story.active")
        .forEach((el) => el.classList.remove("active"));
      jump.closest(".story")?.classList.add("active");
      const marks = [...document.querySelectorAll("mark[data-paths]")]
        .filter((m) => m.dataset.paths.split(" ").includes(path));
      marks.forEach((m) => m.classList.add("active"));
      marks[0]?.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    const fromInbox = event.target.closest("[data-inbox]");
    if (fromInbox) {
      const input = document.getElementById("intake-text");
      input.value = fromInbox.dataset.text;
      document.getElementById("inbox-id").value = fromInbox.dataset.inbox;
      input.focus();
      return;
    }
    const scenario = event.target.closest("[data-scenario]");
    if (scenario) {
      const inboxField = document.getElementById("inbox-id");
      if (inboxField) inboxField.value = "";
      const input = document.getElementById("intake-text");
      input.value = scenario.dataset.scenario;
      input.focus();
    }
  });

  // WSJF editors: recompute the score as the reviewer changes any part.
  document.addEventListener("change", (event) => {
    const editor = event.target.closest(".wsjf-editor");
    if (!editor || !event.target.matches("select[data-part]")) return;
    const v = {};
    editor.querySelectorAll("select[data-part]").forEach((s) => { v[s.dataset.part] = Number(s.value); });
    const out = editor.querySelector(".wsjf-score");
    const ok = v.business_value && v.time_criticality && v.risk_reduction && v.job_size;
    const score = ok ? Math.round(((v.business_value + v.time_criticality + v.risk_reduction) / v.job_size) * 10) / 10 : null;
    out.textContent = score ?? "–";
    const note = editor.querySelector(".wsjf-adjusted");
    if (note) note.hidden = !out.dataset.ai || String(score) === out.dataset.ai;
    const reason = editor.querySelector(".wsjf-reason");  // a changed AI score needs a reason
    if (reason) {
      const changed = [...editor.querySelectorAll("select[data-part]")].some((s) => s.value !== s.dataset.ai);
      reason.hidden = !changed;
      reason.required = changed;
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target.matches("[data-scenario]")) event.target.click();
  });
})();
