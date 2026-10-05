// DROP — shared front-end behavior

document.addEventListener("DOMContentLoaded", () => {
  // Mobile sidebar toggle
  const toggle = document.querySelector(".mobile-toggle");
  const sidebar = document.querySelector(".sidebar");
  if (toggle && sidebar) {
    toggle.addEventListener("click", () => sidebar.classList.toggle("open"));
    document.addEventListener("click", (e) => {
      if (!sidebar.contains(e.target) && !toggle.contains(e.target)) {
        sidebar.classList.remove("open");
      }
    });
  }

  // Auto-dismiss flash messages
  document.querySelectorAll(".flash-item").forEach((el, i) => {
    setTimeout(() => {
      el.style.transition = "opacity 0.3s ease, transform 0.3s ease";
      el.style.opacity = "0";
      el.style.transform = "translateX(20px)";
      setTimeout(() => el.remove(), 300);
    }, 4500 + i * 300);
  });

  // Theme toggle (persists via /settings form + localStorage for instant switch)
  const themeToggle = document.querySelector("[data-theme-toggle]");
  if (themeToggle) {
    themeToggle.addEventListener("click", () => {
      const html = document.documentElement;
      const current = html.getAttribute("data-theme") || "light";
      const next = current === "light" ? "dark" : "light";
      html.setAttribute("data-theme", next);
      localStorage.setItem("drop-theme", next);
      document.dispatchEvent(new CustomEvent("drop-theme-change", { detail: next }));
      fetch("/settings", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: `form_type=theme&theme=${next}`,
      });
    });
  }

  // Join classroom modal helper
  document.querySelectorAll("[data-copy]").forEach((el) => {
    el.addEventListener("click", () => {
      navigator.clipboard.writeText(el.getAttribute("data-copy"));
      const original = el.textContent;
      el.textContent = "Copied!";
      setTimeout(() => (el.textContent = original), 1200);
    });
  });

  initTutorChat();
  initLessonBot();
  initStudyTimer();
});

/* ---------------------------------------------------------------------
   Study-time tracker.
   A page opts in with: <div id="study-timer" data-kind="lesson|study_session" data-id="123">
   Time only counts while the tab is visible AND the student has interacted
   (mouse / key / scroll / touch) in the last 60s, so a forgotten open tab
   doesn't inflate study time. Flushed every 30s and when the page is hidden.
   --------------------------------------------------------------------- */
function initStudyTimer() {
  const el = document.getElementById("study-timer");
  if (!el) return;
  const payload = { kind: el.dataset.kind, id: Number(el.dataset.id) };
  const IDLE_LIMIT_MS = 60000;
  let lastActivity = Date.now();
  let pending = 0;
  let firstFlush = true;
  let stopped = false;  // set when the server says we're logged out / not allowed

  ["mousemove", "keydown", "scroll", "click", "touchstart"].forEach((evt) =>
    window.addEventListener(evt, () => { lastActivity = Date.now(); }, { passive: true })
  );

  setInterval(() => {
    if (!stopped && document.visibilityState === "visible" && Date.now() - lastActivity < IDLE_LIMIT_MS) pending += 1;
  }, 1000);

  function flush(useBeacon) {
    if (stopped || pending < 1) return;
    const body = JSON.stringify({ ...payload, seconds: Math.min(pending, 120), new_session: firstFlush });
    pending = Math.max(0, pending - 120);
    firstFlush = false;
    if (useBeacon && navigator.sendBeacon) {
      navigator.sendBeacon("/api/track/time", new Blob([body], { type: "application/json" }));
    } else {
      fetch("/api/track/time", { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true })
        .then((r) => { if ([401, 403, 404].includes(r.status)) stopped = true; })
        .catch(() => {});
    }
  }
  setInterval(() => flush(false), 30000);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flush(true); });
  window.addEventListener("pagehide", () => flush(true));
}

function initLessonBot() {
  const form = document.getElementById("lesson-bot-form");
  if (!form) return;

  const input = document.getElementById("lesson-bot-input");
  const windowEl = document.getElementById("lesson-bot-window");
  const emptyEl = document.getElementById("lesson-bot-empty");
  const sendBtn = document.getElementById("lesson-bot-send");
  const lessonTitle = form.dataset.lessonTitle || "";

  function appendMini(role, content) {
    if (emptyEl) emptyEl.remove();
    const row = document.createElement("div");
    row.style.marginBottom = "8px";
    row.style.fontSize = "12.5px";
    row.style.lineHeight = "1.5";
    if (role === "user") {
      row.innerHTML = `<strong>You:</strong> `;
    } else {
      row.innerHTML = `<strong style="color:var(--violet);">AI:</strong> `;
    }
    row.appendChild(document.createTextNode(content));
    windowEl.appendChild(row);
    windowEl.scrollTop = windowEl.scrollHeight;
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const message = input.value.trim();
    if (!message) return;

    appendMini("user", message);
    input.value = "";
    sendBtn.disabled = true;
    appendMini("assistant", "Thinking…");
    const thinkingRow = windowEl.lastChild;

    try {
      const res = await fetch("/api/tutor/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, mode: "default", context: lessonTitle }),
      });
      const data = await res.json();
      thinkingRow.remove();
      appendMini("assistant", data.reply || "Sorry, something went wrong.");
    } catch (err) {
      thinkingRow.remove();
      appendMini("assistant", "Network error — please try again.");
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  });
}

function initTutorChat() {
  const form = document.getElementById("tutor-form");
  if (!form) return;

  const input = document.getElementById("tutor-input");
  const windowEl = document.getElementById("chat-window");
  const sendBtn = document.getElementById("tutor-send");
  let mode = "default";

  document.querySelectorAll(".mode-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll(".mode-chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      mode = chip.dataset.mode;
    });
  });

  function scrollToBottom() {
    windowEl.scrollTop = windowEl.scrollHeight;
  }

  function appendBubble(role, content) {
    const row = document.createElement("div");
    row.className = `chat-row ${role}`;
    const avatar =
      role === "assistant"
        ? '<div class="ai-avatar" style="width:30px;height:30px;font-size:12px;">AI</div>'
        : "";
    row.innerHTML = `${avatar}<div class="chat-bubble ${role}"></div>`;
    row.querySelector(".chat-bubble").textContent = content;
    windowEl.appendChild(row);
    scrollToBottom();
    return row.querySelector(".chat-bubble");
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const message = input.value.trim();
    if (!message) return;

    appendBubble("user", message);
    input.value = "";
    sendBtn.disabled = true;

    const thinkingRow = document.createElement("div");
    thinkingRow.className = "chat-row assistant";
    thinkingRow.innerHTML =
      '<div class="ai-avatar thinking" style="width:30px;height:30px;font-size:12px;">AI</div><div class="chat-bubble assistant">Thinking…</div>';
    windowEl.appendChild(thinkingRow);
    scrollToBottom();

    try {
      const res = await fetch("/api/tutor/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, mode }),
      });
      const data = await res.json();
      thinkingRow.remove();
      appendBubble("assistant", data.reply || "Sorry, something went wrong.");
    } catch (err) {
      thinkingRow.remove();
      appendBubble("assistant", "Network error — please try again.");
    } finally {
      sendBtn.disabled = false;
      input.focus();
    }
  });
}