"use strict";

if (document.body.dataset.page === "dashboard") {
  const byId = id => document.getElementById(id);
  const state = {view: "overview", csrf: "", user: null, offset: 0, loading: false, acting: false, providerRunning: false};
  const titles = {overview: "Overview", reviews: "Review queue", activity: "Activity", providers: "Providers", admins: "Admin team"};
  const eventNames = {bot_started: "Bot started", pending: "Submission sent for review", approved: "Submission approved",
    auto_approved: "Submission automatically approved", rejected: "Submission rejected", publish_failed: "Publishing failed",
    poll_failed: "Tally poll failed", moderation_failed: "AI moderation unavailable", handler_failed: "Bot request failed",
    review_delivery_failed: "Review delivery failed", reset_started: "Reset started",
    reset_complete: "Reset completed", reset_failed: "Reset failed", provider_test_started: "Provider tests started",
    provider_test_complete: "Provider tests completed"};
  const date = timestamp => timestamp ? new Date(timestamp * 1000).toLocaleString([], {dateStyle: "medium", timeStyle: "short"}) : "Not recorded yet";
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  function error(message) { byId("error").textContent = message; byId("error").hidden = !message; }
  let toastTimer;
  function toast(message) {
    clearTimeout(toastTimer); byId("toast").textContent = message; byId("toast").hidden = false;
    toastTimer = setTimeout(() => { byId("toast").hidden = true; }, 6000);
  }
  async function api(path, body) {
    const options = {credentials: "same-origin", cache: "no-store"};
    if (body !== undefined) Object.assign(options, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": state.csrf}, body: JSON.stringify(body)});
    const response = await fetch("/api/" + path, options);
    const data = await response.json();
    if (response.status === 401 || (response.status === 403 && body === undefined)) {
      window.location.replace("/"); throw new Error("Your admin session ended. Please sign in again.");
    }
    if (!response.ok) throw new Error(data.error || "Request failed. Try again.");
    return data;
  }
  function details(id, rows) {
    byId(id).replaceChildren(...rows.map(([name, value]) => {
      const row = el("div"); row.append(el("dt", name), el("dd", String(value))); return row;
    }));
  }
  function pill(id, text, type) { byId(id).textContent = text; byId(id).className = "pill " + type; }
  function empty(title, description) {
    const node = el("div", undefined, "empty-state"); node.append(el("h2", title), el("p", description)); return node;
  }
  async function loadOverview() {
    const data = await api("overview");
    byId("stat-pending").textContent = data.pending; byId("queue-badge").textContent = data.pending;
    byId("stat-approved").textContent = data.approved_today; byId("stat-rejected").textContent = data.rejected_today;
    byId("stat-errors").textContent = data.errors_today; byId("build-label").textContent = "Build " + data.build;
    pill("poll-status", data.poll_healthy ? "Polling normally" : "Needs attention", data.poll_healthy ? "good" : "warning");
    details("bot-details", [["Last successful poll", date(data.last_poll_success)], ["Last poll attempt", date(data.last_poll_attempt)],
      ["Polling interval", data.poll_interval + " seconds"], ["Review group", data.review_chat_id], ["Publish channel", data.channel_id],
      ["Started", date(data.started)], ["Disk available", data.disk_free_gb + " GB"],
      ...(data.last_poll_error ? [["Latest poll error", data.last_poll_error]] : []),
      ...(data.audit_error ? [["Activity log error", data.audit_error]] : [])]);
    const reset = data.last_reset;
    const resetStatus = data.reset_running ? "Running" : reset ? reset.status : "Scheduled";
    pill("reset-status", resetStatus, resetStatus === "complete" ? "good" : ["failed", "interrupted"].includes(resetStatus) ? "bad" : "");
    details("reset-details", [["Next scheduled reset", date(data.next_reset)], ["Schedule timezone", data.reset_timezone || "Server local time"],
      ["Last result", reset ? reset.status : "Not recorded yet"], ["Last run", reset ? date(reset.finished || reset.started) : "Not recorded yet"],
      ...(reset && reset.detail ? [["Details", reset.detail]] : [])]);
    byId("reset-button").disabled = data.reset_running;
  }
  async function loadReviews() {
    const data = await api("reviews?offset=" + state.offset);
    if (state.offset >= data.total && state.offset > 0) { state.offset = Math.max(0, state.offset - 20); return loadReviews(); }
    const cards = data.items.map(item => {
      const card = el("article", undefined, "review-card");
      const top = el("div", undefined, "review-top"); top.append(el("span", "SUBMISSION " + item.id, "review-id"), el("time", date(item.created_at)));
      card.append(top, el("p", item.text || "Files-only submission", "submission"));
      if (item.reason) { const note = el("div", undefined, "review-note"); note.append(el("strong", "Review note"), el("span", item.reason)); card.append(note); }
      if (item.moderation === "error") card.append(el("p", "AI moderation could not classify this submission. Please review it manually.", "muted small"));
      if (item.files.length) {
        const files = el("ul", undefined, "attachments");
        item.files.forEach(file => files.append(el("li", file.name + " · " + file.mime_type)));
        card.append(files);
        if (!item.telegram_url) card.append(el("p", "Open the review group in Telegram to view these attachments.", "muted small"));
      }
      const actions = el("div", undefined, "review-actions");
      for (const [decision, label, style] of [["approve", "Approve & publish", "primary"], ["reject", "Reject", "secondary"]]) {
        const button = el("button", label, "button " + style);
        button.addEventListener("click", async () => {
          if (!window.confirm(decision === "approve" ? "Approve and publish this submission to the confession channel?" : "Reject this submission?")) return;
          await perform(button, "review", {id: item.id, decision});
        }); actions.append(button);
      }
      if (item.telegram_url) {
        const link = el("a", "Open in Telegram ↗"); link.href = item.telegram_url; link.target = "_blank"; link.rel = "noopener noreferrer"; actions.append(link);
      }
      card.append(actions); return card;
    });
    byId("review-list").replaceChildren(...(cards.length ? cards : [empty("The queue is clear.", "New submissions that need a decision will appear here.")]));
    byId("queue-badge").textContent = data.total;
    byId("page-info").textContent = data.total ? `${data.offset + 1}–${Math.min(data.offset + data.limit, data.total)} of ${data.total}` : "0 submissions";
    byId("previous-page").disabled = !data.offset; byId("next-page").disabled = data.offset + data.limit >= data.total;
  }
  async function loadActivity() {
    const data = await api("activity");
    const rows = data.items.map(item => {
      const row = el("article", undefined, "activity-row"), content = el("div");
      content.append(el("strong", eventNames[item.kind] || item.kind));
      content.append(el("p", [item.actor || "Bot", item.source, item.submission_id ? "Submission " + item.submission_id : "", item.detail].filter(Boolean).join(" · ")));
      row.append(content, el("time", date(item.time))); return row;
    });
    byId("activity-list").replaceChildren(...(rows.length ? rows : [empty("No activity recorded yet.", "New bot events and admin decisions will appear here.")]));
  }
  async function loadProviders() {
    const data = await api("providers");
    state.providerRunning = data.running;
    byId("test-providers").disabled = data.running; byId("test-providers").textContent = data.running ? "Testing…" : "Test providers";
    byId("provider-test-info").textContent = data.running ? "Testing each configured provider. This can take about two minutes." : data.finished ? "Last completed test: " + date(data.finished) : "No tests have run since the bot started.";
    byId("provider-list").replaceChildren(...data.items.map(item => {
      const card = el("article", undefined, "provider-card"), heading = el("div", undefined, "panel-heading");
      heading.append(el("h2", item.name), el("span", item.configured ? "Key configured" : "Missing key", "pill " + (item.configured ? "good" : "warning")));
      card.append(heading, el("p", item.model, "provider-model"), el("p", item.result || "Not tested yet", "provider-result")); return card;
    }));
  }
  async function loadAdmins() {
    const data = await api("admins"); byId("admin-group").textContent = data.title + " · " + data.chat_id;
    byId("admin-list").replaceChildren(...data.items.map(item => {
      const row = el("article", undefined, "admin-row"), content = el("div");
      content.append(el("strong", item.name + (item.id === state.user.id ? " (you)" : "")),
        el("p", [item.username ? "@" + item.username : "", "ID: " + item.id].filter(Boolean).join(" · ")));
      row.append(content, el("span", item.role, "pill " + (item.role === "Owner" ? "good" : ""))); return row;
    }));
  }
  const loaders = {overview: loadOverview, reviews: loadReviews, activity: loadActivity, providers: loadProviders, admins: loadAdmins};
  async function refresh() {
    if (state.loading || !state.user) return;
    state.loading = true; byId("refresh").disabled = true;
    try { await loaders[state.view](); error(""); byId("last-updated").textContent = "Updated " + new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); }
    catch (err) { error(err.message); }
    finally { state.loading = false; byId("refresh").disabled = false; }
  }
  async function perform(button, path, body) {
    if (state.acting) return;
    state.acting = true; button.disabled = true;
    try { const data = await api(path, body); toast(data.message); await refresh(); }
    catch (err) { error(err.message); }
    finally { state.acting = false; button.disabled = path === "test-providers" && state.providerRunning; }
  }
  document.querySelectorAll("[data-view]").forEach(button => button.addEventListener("click", async () => {
    if (state.loading || state.acting) return;
    state.view = button.dataset.view; byId("page-title").textContent = titles[state.view];
    document.querySelectorAll(".view").forEach(view => { view.hidden = view.id !== state.view; });
    document.querySelectorAll(".nav-item").forEach(nav => {
      const active = nav.dataset.view === state.view; nav.classList.toggle("active", active);
      if (active) nav.setAttribute("aria-current", "page"); else nav.removeAttribute("aria-current");
    }); await refresh();
  }));
  byId("refresh").addEventListener("click", refresh);
  byId("previous-page").addEventListener("click", () => { state.offset = Math.max(0, state.offset - 20); refresh(); });
  byId("next-page").addEventListener("click", () => { state.offset += 20; refresh(); });
  byId("test-providers").addEventListener("click", () => {
    if (window.confirm("Test all moderation providers? This uses API quota or credits. Results will appear here.")) perform(byId("test-providers"), "test-providers", {});
  });
  byId("reset-button").addEventListener("click", () => {
    const confirmation = window.prompt("This deletes Tally submissions and clears all pending reviews. Activity history is kept. Type RESET to continue:");
    if (confirmation === "RESET") perform(byId("reset-button"), "reset", {confirmation});
  });
  byId("logout").addEventListener("click", async () => {
    try { await api("logout", {}); window.location.replace("/"); } catch (err) { error(err.message); }
  });
  api("session").then(async data => {
    state.user = data.user; state.csrf = data.csrf;
    byId("signed-in").textContent = data.user.name + " · " + data.user.role;
    await refresh();
  }).catch(err => error(err.message));
  setInterval(() => { if (!document.hidden && !state.acting) refresh(); }, 30000);
}
