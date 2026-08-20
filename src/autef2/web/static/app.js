/* AUTEF v2 front end.
 *
 * Stages take minutes, so nothing here blocks: a click posts to /api/stage and
 * the page polls /api/state until the server says nothing is running. The
 * server owns the state machine; this file only draws it. */

(function () {
  "use strict";

  var token = sessionStorage.getItem("autef_token") || null;
  var poller = null;
  var mode = "url";
  var lastRunning = null;

  var $ = function (id) { return document.getElementById(id); };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function snack(message) {
    var bar = $("snackbar");
    bar.textContent = message;
    bar.hidden = false;
    clearTimeout(bar._t);
    bar._t = setTimeout(function () { bar.hidden = true; }, 4200);
  }

  function api(path, options) {
    options = options || {};
    var headers = options.headers || {};
    if (token) headers["X-Autef-Token"] = token;
    if (options.json !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.json);
    }
    return fetch(path, { method: options.method || "POST", headers: headers, body: options.body })
      .then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          if (response.status === 401 && path !== "/api/login") { signOut(true); }
          if (!response.ok) { throw new Error(data.error || ("HTTP " + response.status)); }
          return data;
        });
      });
  }

  /* ----------------------------- sign in ---------------------------- */

  $("login-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var error = $("login-error");
    error.hidden = true;
    $("login-button").disabled = true;
    api("/api/login", { json: { username: $("username").value, password: $("password").value } })
      .then(function (data) {
        token = data.token;
        sessionStorage.setItem("autef_token", token);
        enterApp();
      })
      .catch(function (err) {
        error.textContent = err.message;
        error.hidden = false;
      })
      .finally(function () { $("login-button").disabled = false; });
  });

  function signOut(silent) {
    if (token && !silent) { api("/api/logout").catch(function () {}); }
    token = null;
    sessionStorage.removeItem("autef_token");
    clearInterval(poller);
    poller = null;
    $("app").hidden = true;
    $("login-screen").hidden = false;
    $("password").value = "";
  }

  $("logout").addEventListener("click", function () { signOut(false); });

  function enterApp() {
    $("login-screen").hidden = true;
    $("app").hidden = false;
    refresh();
    startPolling();
  }

  function startPolling() {
    clearInterval(poller);
    poller = setInterval(refresh, 1500);
  }

  function refresh() {
    if (!token) return;
    fetch("/api/state", { headers: { "X-Autef-Token": token } })
      .then(function (r) { if (r.status === 401) { signOut(true); throw new Error("signed out"); } return r.json(); })
      .then(render)
      .catch(function () {});
  }

  /* --------------------------- source pane -------------------------- */

  Array.prototype.forEach.call(document.querySelectorAll(".seg-btn"), function (button) {
    button.addEventListener("click", function () {
      mode = button.dataset.mode;
      Array.prototype.forEach.call(document.querySelectorAll(".seg-btn"), function (b) {
        b.classList.toggle("active", b === button);
      });
      $("pane-url").hidden = mode !== "url";
      $("pane-upload").hidden = mode !== "upload";
      $("pane-path").hidden = mode !== "path";
    });
  });

  var dropzone = $("dropzone");
  var fileInput = $("file-input");
  var pendingFile = null;

  fileInput.addEventListener("change", function () {
    if (fileInput.files.length) {
      pendingFile = fileInput.files[0];
      $("dropzone-label").textContent = pendingFile.name;
    }
  });

  ["dragenter", "dragover"].forEach(function (name) {
    dropzone.addEventListener(name, function (e) { e.preventDefault(); dropzone.classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (name) {
    dropzone.addEventListener(name, function (e) { e.preventDefault(); dropzone.classList.remove("over"); });
  });
  dropzone.addEventListener("drop", function (event) {
    var file = event.dataTransfer.files[0];
    if (file) { pendingFile = file; $("dropzone-label").textContent = file.name; }
  });

  $("load-project").addEventListener("click", function () {
    var button = $("load-project");
    button.disabled = true;

    var done = function (message) {
      snack(message);
      button.disabled = false;
      refresh();
    };
    var failed = function (err) { snack(err.message); button.disabled = false; };

    if (mode === "upload") {
      if (!pendingFile) { button.disabled = false; return snack("Choose a .zip file first."); }
      pendingFile.arrayBuffer().then(function (buffer) {
        return api("/api/upload", {
          headers: { "X-Autef-Filename": pendingFile.name, "Content-Type": "application/zip" },
          body: buffer
        });
      }).then(function () { done("Uploaded " + pendingFile.name); }).catch(failed);
      return;
    }

    var value = (mode === "url" ? $("url-input").value : $("path-input").value).trim();
    if (!value) { button.disabled = false; return snack("Enter a URL or a path."); }
    api("/api/source", { json: { value: value } })
      .then(function () { done("Project set. Run stage 1."); })
      .catch(failed);
  });

  /* ---------------------------- settings ---------------------------- */

  $("settings-toggle").addEventListener("click", function () {
    var body = $("settings-body");
    body.hidden = !body.hidden;
    $("settings-toggle").textContent = body.hidden ? "Show" : "Hide";
  });

  var SLIDERS = {
    "set-attempts": ["val-attempts", "max_attempts", null],
    "set-tests": ["val-tests", "max_tests", "all"],
    "set-modules": ["val-modules", "max_modules", null],
    "set-covfiles": ["val-covfiles", "max_coverage_files", null],
    "set-mutants": ["val-mutants", "max_mutants", null],
    "set-survivors": ["val-survivors", "max_survivors", null]
  };

  function pushSettings() {
    var payload = {
      model: $("set-model").value,
      use_venv: $("set-venv").checked,
      use_cache: $("set-cache").checked
    };
    Object.keys(SLIDERS).forEach(function (id) {
      payload[SLIDERS[id][1]] = parseInt($(id).value, 10);
    });
    api("/api/config", { json: payload }).catch(function () {});
  }

  Object.keys(SLIDERS).forEach(function (id) {
    var spec = SLIDERS[id];
    $(id).addEventListener("input", function () {
      var value = parseInt($(id).value, 10);
      $(spec[0]).textContent = (spec[2] && value === 0) ? spec[2] : value;
    });
    $(id).addEventListener("change", pushSettings);
  });

  ["set-model", "set-venv", "set-cache"].forEach(function (id) {
    $(id).addEventListener("change", pushSettings);
  });

  /* ----------------------------- stages ----------------------------- */

  var STAGE_NOTES = {
    1: "detect layout & roots",
    2: "venv + pinned pytest",
    3: "the 'before' snapshot",
    4: "tests for untested code",
    5: "why did it fail?",
    6: "fix, re-run, escalate",
    7: "fill the gaps",
    8: "do the tests notice?",
    9: "before vs after"
  };

  function drawStages(state) {
    var host = $("stages");
    var busy = state.running !== null;
    host.innerHTML = "";

    for (var n = 1; n <= 9; n++) {
      var blocked = state.blocked[String(n)];
      var done = !!state.done[String(n)];
      var active = state.running === n;
      var queued = state.queued.indexOf(n) >= 0;

      var button = document.createElement("button");
      button.className = "stage" + (done ? " done" : "") + (active ? " active" : "");
      button.disabled = busy || !!blocked;
      button.title = blocked || (state.billed_stages.indexOf(n) >= 0
        ? "Calls the model" : "No model call");
      button.dataset.stage = String(n);

      var note = active ? "running..." : queued ? "queued" : STAGE_NOTES[n];
      button.innerHTML =
        '<span class="stage-num">' + (done && !active ? "&#10003;" : n) + "</span>" +
        '<span class="stage-body">' +
          '<span class="stage-name">' + esc(state.stage_names[String(n)]) + "</span>" +
          '<span class="stage-note">' + esc(note) + "</span>" +
        "</span>" +
        (active ? '<span class="bar"></span>' : "");

      button.addEventListener("click", function () {
        var stage = parseInt(this.dataset.stage, 10);
        api("/api/stage", { json: { stage: stage } })
          .then(function (s) { render(s); })
          .catch(function (err) { snack(err.message); });
      });
      host.appendChild(button);
    }
  }

  $("run-all").addEventListener("click", function () {
    api("/api/run-all").then(render).catch(function (err) { snack(err.message); });
  });

  $("reset").addEventListener("click", function () {
    api("/api/reset").then(function (s) { render(s); snack("Session cleared."); })
      .catch(function (err) { snack(err.message); });
  });

  /* ----------------------------- results ---------------------------- */

  function card(title, inner, extra) {
    return '<div class="card"><div class="card-head"><h2>' + esc(title) + "</h2>" +
      (extra ? '<span class="chip">' + esc(extra) + "</span>" : "") +
      "</div>" + inner + "</div>";
  }

  function metric(label, value, delta, direction) {
    return '<div class="metric"><div class="label">' + esc(label) + "</div>" +
      '<div class="value">' + esc(value) + "</div>" +
      (delta ? '<div class="delta ' + (direction || "") + '">' + esc(delta) + "</div>" : "") +
      "</div>";
  }

  function drawResults(state) {
    var html = "";

    if (state.layout) {
      var L = state.layout;
      html += card("Stage 1 — detected layout",
        '<div class="metrics">' +
          metric("Project", L.name) +
          metric("Layout", L.style) +
          metric("Test files", L.test_files) +
          metric("Dependencies", L.dependencies) +
          metric("Installable", L.installable ? "yes" : "no") +
        "</div>" +
        '<p class="hint" style="margin-bottom:0">Working copy: <code>' + esc(L.root) + "</code></p>");
    }

    if (state.environment) {
      var E = state.environment;
      html += card("Stage 2 — environment",
        '<div class="metrics">' +
          metric("Isolated venv", E.isolated ? "yes" : "no") +
          metric("Packages installed", E.installed.length) +
        "</div>" +
        (E.warnings.length
          ? '<div class="banner info" style="margin-top:12px">' +
            E.warnings.map(esc).join("<br>") + "</div>"
          : ""));
    }

    if (state.before) {
      var B = state.before;
      var A = state.after;
      var body = '<div class="metrics">' +
        metric("Passing", A ? A.passed : B.passed,
               A ? signed(A.passed - B.passed) : null,
               A && A.passed >= B.passed ? "up" : "down") +
        metric("Failing", A ? A.failed : B.failed,
               A ? signed(A.failed - B.failed) : null,
               A && A.failed <= B.failed ? "up" : "down") +
        metric("Collection errors", B.collection_errors) +
        metric("Suite time", B.duration_s + "s") +
        "</div>";
      html += card(A ? "Suite — before and after" : "Stage 3 — suite before repair", body,
        A ? "before: " + B.passed + " passed, " + B.failed + " failed" : null);
    }

    if (state.generation) {
      var G = state.generation;
      var rows = G.records.map(function (r) {
        return "<tr><td class=\"mono\">" + esc(r.file || "-") + "</td>" +
          "<td class=\"mono\">" + esc(r.module) + "</td>" +
          "<td>" + (r.kept ? '<span class="tag ok">kept</span>' : '<span class="tag bad">rejected</span>') + "</td>" +
          "<td>" + r.collected + "</td><td>" + r.passing + "</td>" +
          "<td>" + esc(r.error) + "</td></tr>";
      }).join("");
      html += card("Stage 4 — generated tests",
        '<div class="metrics">' +
          metric("Modules considered", G.considered) +
          metric("Files kept", G.accepted) +
          metric("Tests added", G.tests_added) +
        "</div>" +
        (rows ? '<div class="scroll"><table><tr><th>File</th><th>Module</th><th>Kept</th>' +
          "<th>Collected</th><th>Passing</th><th>Problem</th></tr>" + rows + "</table></div>" : "") +
        '<p class="hint" style="margin-bottom:0">A file is kept only if pytest can run it. ' +
        "Generated tests that <i>fail</i> are kept on purpose — they are the repair loop's input.</p>");
    }

    if (state.records.length) {
      var fixed = state.records.filter(function (r) { return r.fixed; }).length;
      var weak = state.records.filter(function (r) { return r.weakened; }).length;
      var regressed = state.records.filter(function (r) { return r.regression; }).length;
      var rows2 = state.records.map(function (r) {
        var strategies = r.attempts.map(function (a) { return a.strategy; }).join(" &rarr; ");
        var status = r.fixed ? '<span class="tag ok">fixed</span>'
          : r.skipped_reason ? '<span class="tag neutral">skipped</span>'
          : '<span class="tag bad">not fixed</span>';
        return "<tr><td class=\"mono\">" + esc(r.nodeid) + "</td>" +
          "<td>" + esc(r.cause || "-") + "</td>" +
          "<td>" + status + "</td>" +
          "<td>" + r.attempts.length + "</td>" +
          "<td class=\"mono\">" + (strategies || "-") + "</td></tr>";
      }).join("");
      html += card("Stages 5 & 6 — diagnosis and repair",
        '<div class="metrics">' +
          metric("Repaired", fixed + " / " + state.records.length) +
          metric("Weakened", weak) +
          metric("Regressions", regressed) +
        "</div>" +
        '<div class="scroll"><table><tr><th>Test</th><th>Root cause</th><th>Result</th>' +
        "<th>Attempts</th><th>Strategies tried</th></tr>" + rows2 + "</table></div>");
    }

    if (state.coverage) {
      var C = state.coverage;
      html += card("Stage 7 — coverage",
        C.measured
          ? '<div class="metrics">' +
              metric("Line coverage", C.line_after + "%", signed(C.line_after - C.line_before, "pts"), "up") +
              metric("Branch coverage", C.branch_after + "%", signed(C.branch_after - C.branch_before, "pts"), "up") +
              metric("Statements", C.statements) +
              metric("Tests written", C.written) +
            "</div>"
          : '<div class="banner">Coverage could not be measured' +
            (C.error ? ": " + esc(C.error) : "") + "</div>");
    }

    if (state.mutation) {
      var M = state.mutation;
      html += card("Stage 8 — mutation",
        M.measured
          ? '<div class="metrics">' +
              metric("Mutation score", M.score_after + "%", signed(M.score_after - M.score_before, "pts"), "up") +
              metric("Killed", M.killed_after + " / " + M.total) +
              metric("Newly killed", M.newly_killed) +
              metric("Killer tests kept", M.written) +
            "</div>"
          : '<div class="banner info">' + esc(M.skipped_reason || "Not measured.") + "</div>");
    }

    if (!html) {
      html = card("Results",
        '<p class="hint" style="margin-bottom:0">Load a project and run stage 1. ' +
        "Each stage's output appears here as it completes.</p>");
    }

    $("results").innerHTML = html;
  }

  function signed(value, unit) {
    var rounded = Math.round(value * 10) / 10;
    if (!rounded) return "no change";
    return (rounded > 0 ? "+" : "") + rounded + (unit ? " " + unit : "");
  }

  /* ------------------------------ render ---------------------------- */

  function render(state) {
    if (!state || state.error === "not signed in") return;

    $("user-chip").textContent = state.username;
    $("appbar-subtitle").textContent = state.source_label
      ? state.source_label
      : "No project loaded";

    var key = $("key-chip");
    key.textContent = state.api_key_present ? "Model key found" : "No model key";
    key.className = "chip " + (state.api_key_present ? "ok" : "warn");

    var usage = state.usage;
    $("cost-chip").textContent = "$" + usage.cost_usd.toFixed(4) + " · " + usage.calls + " calls";
    $("elapsed").textContent = state.elapsed_s ? state.elapsed_s + "s" : "";

    // Only overwrite the controls when the user is not mid-drag.
    if (document.activeElement === document.body || document.activeElement === null) {
      var s = state.settings;
      $("set-model").value = s.model;
      $("set-venv").checked = !!s.use_venv;
      $("set-cache").checked = !!s.use_cache;
      Object.keys(SLIDERS).forEach(function (id) {
        var spec = SLIDERS[id];
        $(id).value = s[spec[1]];
        $(spec[0]).textContent = (spec[2] && !s[spec[1]]) ? spec[2] : s[spec[1]];
      });
    }

    drawStages(state);

    var banner = $("banner");
    if (state.error) {
      banner.className = "banner";
      banner.textContent = state.error;
      banner.hidden = false;
    } else if (state.running) {
      banner.className = "banner info";
      banner.textContent = "Stage " + state.running + " — " + state.running_name +
        " is running. This can take several minutes.";
      banner.hidden = false;
    } else {
      banner.hidden = true;
    }

    $("run-all").disabled = state.running !== null || !state.has_source;
    $("reset").disabled = state.running !== null;
    $("load-project").disabled = state.running !== null;

    $("log").textContent = state.logs.length
      ? state.logs.join("\n")
      : "Nothing logged yet.";
    var log = $("log");
    log.scrollTop = log.scrollHeight;

    if (lastRunning !== null && state.running === null && !state.error) {
      snack("Stage " + lastRunning + " finished.");
    }
    lastRunning = state.running;

    drawResults(state);
  }

  if (token) { enterApp(); } else { $("username").focus(); }
})();
