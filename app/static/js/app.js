/* Sponsor Job Agent -- lightweight progressive enhancement.
   No framework, no build step. Bounded polling (never SSE/websockets) is
   the "lightest architecture" choice for this local, single-user app --
   reuses the existing read-only JSON endpoints (/agent/status,
   /api/pipeline/summary), never introduces new business logic on the
   client. Every enhancement here is additive: the page is fully
   functional (forms submit, links navigate) with JavaScript disabled. */

(function () {
  "use strict";

  function setText(id, value) {
    var el = document.getElementById(id);
    if (el && value !== undefined && value !== null) el.textContent = value;
  }

  function fmtStage(stage) {
    var labels = {
      discovering: "Discovering fresh jobs",
      generating_resumes: "Generating one-page resumes",
      preparing_applications: "Preparing applications",
      executing_applications: "Filling/submitting applications",
      starting: "Starting up",
    };
    return labels[stage] || stage || "Working";
  }

  function pollAgentStatus() {
    // The status chip lives in the topbar on every page; the detailed
    // current-activity/heartbeat/last-cycle fields only exist on the
    // Dashboard -- setText()/querySelectorAll() below no-op safely when
    // those elements aren't present on the current page.
    fetch("/agent/status", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (body) {
        if (!body) return;
        var o = body.orchestrator || {};
        var chip = document.querySelectorAll("[data-agent-chip]");
        chip.forEach(function (el) {
          el.textContent = o.actual_state || "STOPPED";
          el.classList.remove("is-running", "is-error");
          if (o.actual_state === "RUNNING") el.classList.add("is-running");
          if (o.actual_state === "ERROR") el.classList.add("is-error");
        });
        setText("live-current-activity", o.cycle_in_progress
          ? fmtStage(o.current_stage) + (o.current_job_label ? " — " + o.current_job_label : "")
          : "Waiting until next cycle.");
        setText("live-last-cycle", o.last_cycle_finished_at || (o.cycle_in_progress ? "in progress" : "never"));
        setText("live-next-cycle", o.next_cycle_at || (o.cycle_in_progress ? "in progress" : "pending"));
        var hb = document.getElementById("live-heartbeat");
        if (hb) {
          hb.textContent = o.heartbeat_stale
            ? "stale (" + Math.round(o.heartbeat_age_seconds || 0) + "s)"
            : "ok";
          hb.classList.toggle("stale", !!o.heartbeat_stale);
        }
      })
      .catch(function () { /* best-effort -- a transient poll failure never disrupts the page */ });
  }

  function pollSummary() {
    var root = document.querySelector("[data-live-summary]");
    if (!root) return;
    fetch("/api/pipeline/summary", { headers: { Accept: "application/json" } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (body) {
        if (!body) return;
        Object.keys(body).forEach(function (key) {
          setText("stat-" + key, body[key]);
        });
      })
      .catch(function () { /* best-effort */ });
  }

  function startPolling() {
    pollAgentStatus();
    pollSummary();
    setInterval(pollAgentStatus, 5000);
    setInterval(pollSummary, 12000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startPolling);
  } else {
    startPolling();
  }

  // Close the mobile nav <details> after a link inside it is activated.
  document.addEventListener("click", function (evt) {
    var link = evt.target.closest(".primary-nav-mobile a");
    if (!link) return;
    var details = link.closest("details.nav-toggle");
    if (details) details.removeAttribute("open");
  });

  // Debounced auto-submit for the search box on the Jobs page (progressive
  // enhancement only -- the Search button next to it works with JS off).
  var searchInput = document.querySelector("[data-auto-search]");
  if (searchInput) {
    var timer = null;
    searchInput.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        searchInput.form.requestSubmit();
      }, 450);
    });
  }
})();
