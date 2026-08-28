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


  /* ---------------------------- code viewer -------------------------

     A generated suite is one file, but the interesting unit is one test. So a
     file is listed as a header and each test it contains becomes its own
     block, collapsed, opening to just that test's code -- the same disclosure
     the repair table uses for the code an attempt wrote.

     File contents are fetched once per path and cached: /api/state polls every
     1.5 seconds and re-renders, and re-reading the file each time would be
     pointless traffic. */

  var fileCache = {};
  var pending = {};
  var openBlocks = {};

  function fetchFile(path) {
    if (fileCache[path] !== undefined || pending[path]) return;
    pending[path] = true;
    fetch("/api/file?path=" + encodeURIComponent(path),
          { headers: { "X-Autef-Token": token } })
      .then(function (r) { return r.json(); })
      .then(function (d) { fileCache[path] = d.error ? "" : d.content; })
      .catch(function () { fileCache[path] = ""; })
      .finally(function () { delete pending[path]; refresh(); });
  }

  // Split Python source into its top-level definitions. Deliberately simple:
  // a line starting at column 0 with def or class opens a block that runs to
  // the next one. Everything before the first is the file's imports and setup.
  function splitTests(source) {
    var lines = String(source || "").split("\n");
    var blocks = [];
    var header = [];
    var current = null;

    lines.forEach(function (line) {
      var match = /^(def|class)\s+([A-Za-z_]\w*)/.exec(line);
      if (match) {
        if (current) blocks.push(current);
        current = { kind: match[1], name: match[2], lines: [line] };
      } else if (current) {
        current.lines.push(line);
      } else {
        header.push(line);
      }
    });
    if (current) blocks.push(current);

    blocks.forEach(function (b) {
      b.code = b.lines.join("\n").replace(/\s+$/, "");
      b.tests = b.kind === "class"
        ? (b.code.match(/def\s+test\w*/g) || []).length
        : 1;
    });
    return {
      header: header.join("\n").trim(),
      blocks: blocks.filter(function (b) { return b.code.trim(); })
    };
  }

  function disclosure(id, title, meta, code, tone) {
    var open = !!openBlocks[id];
    return '<div class="block">' +
      '<button class="blockhead' + (tone ? " " + tone : "") +
        '" data-block="' + id + '">' +
        '<span class="caret">' + (open ? "\u25be" : "\u25b8") + "</span>" +
        '<span class="btitle mono">' + title + "</span>" +
        (meta ? '<span class="bmeta">' + meta + "</span>" : "") +
      "</button>" +
      '<pre class="code" data-body="' + id + '"' + (open ? "" : " hidden") + ">" +
        esc(code) + "</pre></div>";
  }

  // Render a code listing with line numbers, marking the line the traceback
  // blamed. Seeing *which* line broke is the point; a bare node id is not.
  function listing(code, startLine, errorLine) {
    return String(code || "").split("\n").map(function (line, i) {
      var n = (startLine || 1) + i;
      var bad = errorLine && n === errorLine;
      return '<div class="cl' + (bad ? " bad" : "") + '">' +
        '<span class="ln">' + n + "</span>" +
        '<span class="lt">' + (esc(line) || "&nbsp;") + "</span></div>";
    }).join("");
  }

  function disclosureHtml(id, title, meta, bodyHtml, tone) {
    var open = !!openBlocks[id];
    return '<div class="block">' +
      '<button class="blockhead' + (tone ? " " + tone : "") +
        '" data-block="' + id + '">' +
        '<span class="caret">' + (open ? "▾" : "▸") + "</span>" +
        '<span class="btitle mono">' + title + "</span>" +
        (meta ? '<span class="bmeta">' + meta + "</span>" : "") +
      "</button>" +
      '<div class="code listing" data-body="' + id + '"' + (open ? "" : " hidden") + ">" +
        bodyHtml + "</div></div>";
  }

  function generatedFile(entry, key) {
    var status = entry.kept
      ? '<span class="tag ok">kept</span>'
      : '<span class="tag bad">rejected</span>';

    var head = '<div class="fileband">' +
      '<span class="mono fname">' + esc(entry.file || "(not written)") + "</span>" +
      status +
      '<span class="fmeta">' + entry.collected + " collected, " +
        entry.passing + " passing</span></div>" +
      (entry.error ? '<div class="ferror">' + esc(entry.error) + "</div>" : "");

    if (!entry.path) return '<div class="filegroup">' + head + "</div>";

    fetchFile(entry.path);
    var source = fileCache[entry.path];
    if (source === undefined) {
      return '<div class="filegroup">' + head +
        '<p class="hint" style="margin:8px 0 0">Reading the file...</p></div>';
    }

    var parsed = splitTests(source);
    var body = parsed.blocks.map(function (b, i) {
      var meta = b.kind === "class" ? b.tests + " test(s)" : "test case";
      return disclosure(key + "_t" + i,
        esc(b.kind + " " + b.name), meta, b.code);
    }).join("");

    if (parsed.header) {
      body += disclosure(key + "_hdr", "imports and setup", "", parsed.header);
    }
    if (!parsed.blocks.length) {
      body = '<p class="hint" style="margin:8px 0 0">Nothing was written.</p>';
    }
    return '<div class="filegroup">' + head + body + "</div>";
  }

  function wireBlocks() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-block]"),
      function (head) {
        head.addEventListener("click", function () {
          var id = head.dataset.block;
          var body = document.querySelector('[data-body="' + id + '"]');
          if (!body) return;
          var open = body.hidden;
          body.hidden = !open;
          head.querySelector(".caret").textContent = open ? "\u25be" : "\u25b8";
          if (open) { openBlocks[id] = true; } else { delete openBlocks[id]; }
        });
      });
  }

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
        (L.no_tests
          ? '<div class="banner info" style="margin-top:12px">This project ships ' +
            "<b>no test files</b>. Nothing failing is therefore not the same as " +
            "everything passing — there is nothing to repair yet. Run " +
            "<b>stage 4</b> to write a suite for it first.</div>"
          : "") +
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
      html += card("Stage 3 — suite before repair",
        '<div class="metrics">' +
          metric("Passing", B.passed) +
          metric("Failing", B.failed) +
          metric("Collection errors", B.collection_errors) +
          metric("Suite time", B.duration_s + "s") +
        "</div>" +
        '<p class="hint" style="margin-bottom:0">The project as it arrived. ' +
        "This snapshot is never rewritten — later stages add test files, and " +
        "the final report compares against this.</p>");
    }

    if (state.generation) {
      var G = state.generation;
      var rows = G.records.map(function (r, i) {
        return generatedFile(r, "gen" + i);
      }).join("");
      html += card("Stage 4 — generated tests",
        '<div class="metrics">' +
          metric("Modules considered", G.considered) +
          metric("Files kept", G.accepted) +
          metric("Tests added", G.tests_added) +
        "</div>" +
        rows +
        '<p class="hint" style="margin-bottom:0">Click a file to read what was ' +
        "written. A file is kept only if pytest can run it. " +
        "Generated tests that <i>fail</i> are kept on purpose — they are the repair loop's input.</p>");
    }

    if (state.records.length) {
      var fixed = state.records.filter(function (r) { return r.fixed; }).length;
      var weak = state.records.filter(function (r) { return r.weakened; }).length;
      var regressed = state.records.filter(function (r) { return r.regression; }).length;
      var rows2 = state.records.map(function (r, i) {
        var strategies = r.attempts.map(function (a) { return a.strategy; }).join(" &rarr; ");
        var status = r.fixed ? '<span class="tag ok">fixed</span>'
          : r.skipped_reason ? '<span class="tag neutral">skipped</span>'
          : '<span class="tag bad">not fixed</span>';
        var failing = "";
        if (r.failing) {
          var f = r.failing;
          failing = disclosureHtml(
            "fail" + i,
            "the failing test",
            esc((f.exception || "") + (f.message ? ": " + f.message : "")),
            listing(f.code, f.start_line, f.error_line),
            "bad");
        }
        var applied = r.attempts.filter(function (a) { return a.patch; });
        var patch = applied.map(function (a, ai) {
          return disclosure(
            "rep" + i + "_" + ai,
            "attempt " + a.n + ": " + esc(a.strategy),
            a.verified ? '<span class="tag ok">verified</span>'
                       : '<span class="tag bad">rejected</span>',
            a.patch,
            a.verified ? "good" : "");
        }).join("");
        return "<tr><td class=\"mono\">" + esc(r.nodeid) + failing + patch + "</td>" +
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
              (C.repaired ? metric("Failing tests repaired", C.repaired) : "") +
            "</div>" +
            (C.files || []).map(function (f, i) { return generatedFile(f, "cov" + i); }).join("")
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
            "</div>" +
            (M.files || []).map(function (f, i) { return generatedFile(f, "mut" + i); }).join("")
          : '<div class="banner info">' + esc(M.skipped_reason || "Not measured.") + "</div>");
    }

    if (state.after) {
      var B2 = state.before || {passed: 0, failed: 0};
      var A2 = state.after;
      var fixed2 = state.records.filter(function (r) { return r.fixed; }).length;
      var weak2 = state.records.filter(function (r) { return r.weakened; }).length;
      var reg2 = state.records.filter(function (r) { return r.regression; }).length;
      html += card("Stage 9 — final report",
        '<div class="metrics">' +
          metric("Passing", A2.passed, signed(A2.passed - B2.passed),
                 A2.passed >= B2.passed ? "up" : "down") +
          metric("Failing", A2.failed, signed(A2.failed - B2.failed),
                 A2.failed <= B2.failed ? "up" : "down") +
          metric("Repaired", fixed2 + " / " + state.records.length) +
          metric("Weakened", weak2, weak2 ? "assertions gutted" : "none", weak2 ? "down" : "up") +
          metric("Regressions", reg2, reg2 ? "broke a passing test" : "none", reg2 ? "down" : "up") +
          metric("Cost", "$" + state.usage.cost_usd.toFixed(4),
                 state.usage.calls + " model calls") +
        "</div>" +
        '<div class="downloads">' +
          '<button class="btn filled small" data-dl="/api/report.html">Download full report (HTML)</button>' +
          '<button class="btn tonal small" data-dl="/api/report.json">JSON</button>' +
          '<button class="btn tonal small" data-dl="/api/project.zip">Repaired project (.zip)</button>' +
        "</div>" +
        '<p class="hint" style="margin-bottom:0">The HTML report is one ' +
        "self-contained file and carries the complete text of every test that " +
        "was written and every repair that was applied. Before: " + B2.passed +
        " passing, " + B2.failed + " failing; after: " + A2.passed +
        " passing, " + A2.failed + " failing. Weakened counts fixes that " +
        "passed only by removing an assertion — it is what makes the other " +
        "numbers trustworthy.</p>");
    }

    if (!html) {
      html = card("Results",
        '<p class="hint" style="margin-bottom:0">Load a project and run stage 1. ' +
        "Each stage's output appears here as it completes.</p>");
    }

    $("results").innerHTML = html;
    wireBlocks();
    wireDownloads();
  }

  function wireDownloads() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-dl]"), function (button) {
      button.addEventListener("click", function () {
        var label = button.textContent;
        button.disabled = true;
        button.textContent = "Preparing...";
        // Sent as a fetch rather than a link so the session header goes with
        // it; the blob is then handed to the browser to save.
        fetch(button.dataset.dl, { headers: { "X-Autef-Token": token } })
          .then(function (r) {
            if (!r.ok) throw new Error("HTTP " + r.status);
            var name = (r.headers.get("Content-Disposition") || "")
              .replace(/.*filename="?([^"]+)"?.*/, "$1") || "autef2-report";
            return r.blob().then(function (blob) { return { blob: blob, name: name }; });
          })
          .then(function (out) {
            var url = URL.createObjectURL(out.blob);
            var a = document.createElement("a");
            a.href = url;
            a.download = out.name;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
            snack("Downloaded " + out.name);
          })
          .catch(function (err) { snack("Download failed: " + err.message); })
          .finally(function () { button.disabled = false; button.textContent = label; });
      });
    });
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
