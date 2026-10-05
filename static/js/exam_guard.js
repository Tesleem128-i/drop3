/* DROP secure exam page.
 *
 * What runs here: fullscreen enforcement, tab/window-switch detection, blocking of copy/paste/right-click/
 * dev-tool shortcuts, autosave, the countdown, and reporting events to the server.
 * What does NOT run here (the server owns it): the real deadline, strike counting, auto-submit,
 * and refusing every other page while an exam is running.
 */
(function () {
  "use strict";
  var cfg = window.EXAM_CFG;
  if (!cfg) return;

  var $ = function (id) { return document.getElementById(id); };
  var form = $("exam-form"), gate = $("gate"), gateTitle = $("gate-title"), gateMsg = $("gate-msg"), gateBtn = $("gate-btn");
  var timerEl = $("timer"), strikeEl = $("strikes"), answeredEl = $("answered"), toastEl = $("toast"), saveEl = $("save-state");

  var started = false, ended = false, submitting = false, dialogOpen = false, fsSupported = true;
  var strikes = cfg.violations || 0, limit = cfg.limit || 3;
  var COUNTED = { fullscreen_exit: 1, tab_hidden: 1, window_blur: 1 };
  var lastStrikeAt = 0, lastSoft = {}, dirty = false, saving = false, devHits = 0;
  var clockBase = performance.now(), clockStart = cfg.remaining;

  /* ---------- clock (the server's remaining time is the truth; we only display it) ---------- */
  function left() { return Math.max(0, clockStart - (performance.now() - clockBase) / 1000); }
  function syncClock(sec) { if (typeof sec === "number") { clockStart = sec; clockBase = performance.now(); } }
  function fmt(s) {
    s = Math.ceil(s);
    var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
    return (h ? h + ":" : "") + String(m).padStart(h ? 2 : 1, "0") + ":" + String(x).padStart(2, "0");
  }
  setInterval(function () {
    var s = left();
    timerEl.textContent = fmt(s);
    timerEl.classList.toggle("low", s <= 60);
    if (s <= 0 && !submitting && !ended) submitNow("time");
  }, 250);

  /* ---------- helpers ---------- */
  function toast(msg) {
    toastEl.textContent = msg; toastEl.style.display = "block";
    clearTimeout(toast.t); toast.t = setTimeout(function () { toastEl.style.display = "none"; }, 4500);
  }
  function post(url, body, keepalive) {
    return fetch(url, {
      method: "POST", credentials: "same-origin", keepalive: !!keepalive,
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
    }).then(function (r) { return r.json(); });
  }
  function isFS() { return !!(document.fullscreenElement || document.webkitFullscreenElement); }
  function endExam(url) {
    ended = true; submitting = true;
    try { if (isFS()) (document.exitFullscreen || document.webkitExitFullscreen).call(document); } catch (e) {}
    location.href = url || "/";
  }
  function setStrikes(n) { strikes = n; strikeEl.textContent = n; }

  /* ---------- reporting ---------- */
  function handle(resp) {
    if (!resp) return;
    if (typeof resp.count === "number") setStrikes(resp.count);
    syncClock(resp.remaining);
    if (resp.ended) endExam(resp.redirect);
  }
  function report(kind, detail) {
    return post(cfg.eventUrl, { kind: kind, detail: detail || "" }, true).then(handle).catch(function () {});
  }
  function soft(kind, detail) {                       // logged for the teacher, never a strike
    if (!started || ended) return;
    var now = Date.now();
    if (lastSoft[kind] && now - lastSoft[kind] < 4000) return;
    lastSoft[kind] = now;
    report(kind, detail);
  }
  function strike(kind, detail) {                      // counts towards auto-submit
    if (!started || ended || submitting || dialogOpen) return;
    var now = Date.now();
    if (now - lastStrikeAt > 2000) {                   // one tab switch fires several events; count once
      lastStrikeAt = now;
      if (strikes + 1 >= limit) toast("Final strike — your exam is being submitted.");
      else toast("Strike " + (strikes + 1) + " of " + limit + ": stay on the exam page in fullscreen.");
      setStrikes(strikes + 1);
    }
    report(kind, detail);
  }

  /* ---------- gate (the overlay that hides the questions) ---------- */
  function showGate(mode) {
    gate.classList.remove("hidden"); document.body.classList.add("locked");
    if (mode === "left") {
      gateTitle.textContent = "You left fullscreen";
      gateMsg.textContent = "That was recorded as a strike (" + strikes + " of " + limit + "). Return to fullscreen to continue — your timer is still running.";
      gateBtn.textContent = "Return to fullscreen";
    }
  }
  function hideGate() { gate.classList.add("hidden"); document.body.classList.remove("locked"); }

  function lockKeys() {
    try {   // Chromium: keeps Esc / Alt / Win keys inside the page while fullscreen
      if (navigator.keyboard && navigator.keyboard.lock)
        navigator.keyboard.lock(["Escape", "AltLeft", "AltRight", "MetaLeft", "MetaRight"]).catch(function () {});
    } catch (e) {}
  }
  function begin() {
    var first = !started;
    started = true; hideGate(); lockKeys();
    if (first) {
      if (window.screen && window.screen.isExtended) { soft("multi_monitor", "extended display detected"); toast("More than one display detected — noted for your teacher."); }
      history.pushState(null, "", location.href);
    }
  }
  gateBtn.addEventListener("click", function () {
    var el = document.documentElement, req = el.requestFullscreen || el.webkitRequestFullscreen;
    if (!req) {                                        // e.g. iPhone Safari
      fsSupported = false; begin();
      report("fullscreen_unsupported", navigator.userAgent.slice(0, 120));
      toast("This browser can't do fullscreen. Tab switches are still recorded.");
      return;
    }
    Promise.resolve(req.call(el)).then(begin).catch(function () {
      gateMsg.textContent = "Your browser blocked fullscreen. Allow it for this site, then click again.";
    });
  });
  function onFS() {
    if (!started || ended || submitting || !fsSupported) return;
    if (!isFS()) { strike("fullscreen_exit", "left fullscreen"); showGate("left"); }
  }
  document.addEventListener("fullscreenchange", onFS);
  document.addEventListener("webkitfullscreenchange", onFS);

  /* ---------- leaving the page / window ---------- */
  document.addEventListener("visibilitychange", function () { if (document.hidden) strike("tab_hidden", "tab or window hidden"); });
  window.addEventListener("blur", function () {
    setTimeout(function () { if (started && !document.hasFocus() && !document.hidden) strike("window_blur", "window lost focus"); }, 250);
  });
  window.addEventListener("beforeunload", function (e) {
    if (ended || submitting) return;
    e.preventDefault(); e.returnValue = "";
  });
  window.addEventListener("pagehide", function () {
    if (ended || submitting || !started) return;
    try { navigator.sendBeacon(cfg.eventUrl, new Blob([JSON.stringify({ kind: "page_left", detail: "page closed or navigated" })], { type: "application/json" })); } catch (e) {}
  });
  window.addEventListener("popstate", function () { history.pushState(null, "", location.href); soft("back_button", "back/forward pressed"); });

  /* ---------- blocking (and logging) the usual shortcuts ---------- */
  ["copy", "cut", "paste"].forEach(function (t) {
    document.addEventListener(t, function (e) { e.preventDefault(); soft(t + "_attempt", ""); });
  });
  document.addEventListener("contextmenu", function (e) { e.preventDefault(); soft("context_menu", ""); });
  document.addEventListener("dragstart", function (e) { e.preventDefault(); });
  document.addEventListener("drop", function (e) { e.preventDefault(); });
  document.addEventListener("selectstart", function (e) {
    var n = e.target.nodeType === 3 ? e.target.parentNode : e.target;
    if (!/^(INPUT|TEXTAREA)$/.test(n.nodeName)) e.preventDefault();
  });
  document.addEventListener("keydown", function (e) {
    if (!started || ended) return;
    var k = (e.key || "").toLowerCase(), ctrl = e.ctrlKey || e.metaKey, kind = null, combo = (ctrl ? "Ctrl+" : "") + (e.shiftKey ? "Shift+" : "") + (e.altKey ? "Alt+" : "") + e.key;
    if (e.key === "F12" || (ctrl && e.shiftKey && (k === "i" || k === "j" || k === "c")) || (ctrl && k === "u")) kind = "devtools_key";
    else if (ctrl && k === "p") kind = "print_attempt";
    else if (e.key === "F5" || (ctrl && k === "r")) kind = "reload_key";
    else if (e.altKey && (e.key === "ArrowLeft" || e.key === "ArrowRight")) kind = "back_button";
    else if (ctrl && (k === "s" || k === "f" || k === "g" || k === "o" || k === "l" || k === "a" || k === "c" || k === "x" || k === "v")) kind = (k === "c" || k === "x" || k === "v") ? (k === "c" ? "copy_attempt" : k === "x" ? "cut_attempt" : "paste_attempt") : "blocked_key";
    else if (e.key === "F11" || e.key === "F3" || e.key === "F6") kind = "blocked_key";
    if (kind) {
      var inField = /^(INPUT|TEXTAREA)$/.test(document.activeElement && document.activeElement.nodeName);
      if (!(ctrl && k === "a" && inField)) { e.preventDefault(); e.stopPropagation(); soft(kind, combo); }
    }
  }, true);
  document.addEventListener("keyup", function (e) {
    if (!started || ended) return;
    if (e.key === "PrintScreen") {
      try { navigator.clipboard.writeText(""); } catch (x) {}
      soft("screenshot_key", "PrintScreen pressed");
    }
  });

  /* Docked dev tools shrink the viewport inside the window; in fullscreen the two should match. Logged only (not a strike). */
  setInterval(function () {
    if (!started || ended || !isFS()) { devHits = 0; return; }
    var dw = Math.abs(window.outerWidth - window.innerWidth), dh = Math.abs(window.outerHeight - window.innerHeight);
    if (dw > 160 || dh > 200) { if (++devHits === 3) soft("devtools_suspected", "viewport differs from window by " + dw + "x" + dh); }
    else devHits = 0;
  }, 1000);

  /* ---------- answers: counter, autosave (also the heartbeat), submit ---------- */
  function collect() {
    var out = {};
    new FormData(form).forEach(function (v, k) { if (k.indexOf("answer_") === 0) out[k.slice(7)] = v; });
    return out;
  }
  function countAnswered() {
    var a = collect(), n = 0;
    Object.keys(a).forEach(function (k) { if (String(a[k]).trim()) n++; });
    answeredEl.textContent = n; return n;
  }
  function save() {
    if (saving || ended || submitting) return;
    saving = true; saveEl.textContent = "Saving…";
    post(cfg.saveUrl, { answers: collect() }).then(function (r) {
      saving = false; dirty = false; saveEl.textContent = "Saved ✓"; handle(r);
    }).catch(function () { saving = false; saveEl.textContent = "Offline — retrying…"; });
  }
  form.addEventListener("input", function () { dirty = true; countAnswered(); clearTimeout(save.t); save.t = setTimeout(save, 1500); });
  form.addEventListener("change", function () { dirty = true; countAnswered(); save(); });
  setInterval(function () { if (started && !ended) save(); }, 10000);   // heartbeat even when idle
  countAnswered();

  function submitNow(reason) {
    if (submitting) return;
    submitting = true; ended = true;
    $("reason").value = reason || "student";
    form.submit();
  }
  $("submit-btn").addEventListener("click", function () {
    var n = countAnswered();
    $("confirm-text").textContent = "You've answered " + n + " of " + cfg.total + " questions. You can't change anything after submitting.";
    dialogOpen = true; $("confirm").classList.remove("hidden");
  });
  $("confirm-no").addEventListener("click", function () { dialogOpen = false; $("confirm").classList.add("hidden"); });
  $("confirm-yes").addEventListener("click", function () { submitNow("student"); });
})();