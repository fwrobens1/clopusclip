(function () {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[c]);
  const pad = (n) => String(n).padStart(2, "0");
  const fmt = (sec) => {
    sec = Math.max(0, Number(sec) || 0);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  };
  const looksLikeUrl = (s) => /^https?:\/\/\S+$/i.test((s || "").trim());

  // Attach the Supabase access token to every local API call.
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init = {}) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    let next = init;
    if (url.startsWith("/api/") && window.ncAccess) {
      const headers = new Headers(init.headers || {});
      headers.set("Authorization", `Bearer ${window.ncAccess}`);
      next = Object.assign({}, init, { headers });
    }
    return nativeFetch(input, next).then((r) => {
      if (r.status === 401 && url.startsWith("/api/") && !url.startsWith("/api/auth/")) {
        location.replace("/auth");
      }
      return r;
    });
  };

  const state = {
    mode: "link",
    source: null,
    videoFile: null,
    jobId: null,
    jobs: [],
    clips: [],
    lastStatus: null,
    pollTimer: null,
    startedAt: 0,
    chatBusy: false,
    chatActiveJob: null,
    filter: "all",
    query: "",
    sortMode: "new",
    fileKind: "video",
    editorIndex: -1,
    reviewSelection: new Set(),
    libraryGroups: [],
    dictionary: {},
    presets: {},
    publishItems: [],
    reviewJobId: null,
    liveSessions: [],
    publications: [],
    editorDirty: false,
  };

  const opt = { clips: 5, length: 1, captions: true, vertical: true, review: false };
  const lengths = ["15–30s", "30–60s", "60–90s"];
  const lengthValues = ["short", "medium", "long"];
  const STAGES = ["resolve", "audio", "transcript", "select", "render"];
  const VIEW_NAMES = { studio: "Studio", agent: "Agent", clips: "Clips", library: "Library", live: "Live", editor: "Editor" };

  /* ---------------- toast ---------------- */
  function toast(msg, icon = "i-check") {
    const box = $("#toasts");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "toast";
    el.innerHTML = `<svg class="ico" aria-hidden="true"><use href="#${esc(icon)}"/></svg><span>${msg}</span>`;
    box.appendChild(el);
    while (box.children.length > 3) box.firstElementChild.remove();
    setTimeout(() => {
      el.classList.add("out");
      setTimeout(() => el.remove(), 220);
    }, 2800);
  }

  /* ---------------- clock / greeting ---------------- */
  function tick() {
    const d = new Date();
    const h = d.getHours();
    $("#clock").textContent = `${pad(h)}:${pad(d.getMinutes())}`;
    $("#date").textContent = d.toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short" }).toUpperCase();
    $("#greeting").textContent = h < 12 ? "Good morning, NOCTX." : h < 18 ? "Good afternoon, NOCTX." : "Good evening, NOCTX.";
  }
  tick();
  setInterval(tick, 30000);

  /* ---------------- navigation ---------------- */
  function go(view) {
    const ed = $("#editorVideo");
    if (view !== "editor" && ed && !ed.paused) ed.pause();
    const navView = view === "editor" ? "clips" : view;
    $$(".nav-item").forEach((b) => {
      const on = b.dataset.view === navView;
      b.classList.toggle("active", on);
      on ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current");
    });
    $$(".page").forEach((p) => { p.hidden = p.id !== `page-${view}`; });
    $("#crumbNow").textContent = view === "editor" ? "Clips / Editor" : (VIEW_NAMES[view] || view);
    document.title = view === "editor"
      ? "NoetharaClipz — Editor"
      : `NoetharaClipz — ${VIEW_NAMES[view] || "Studio"}`;
    $("#app").classList.remove("nav-open");
    $("#scroll").scrollTop = 0;
    if (view === "agent") setTimeout(scrollThreadToBottom, 0);
    if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
    if (view === "clips") setTimeout(renderAll, 0);
    if (view === "library") setTimeout(loadLibrary, 0);
    if (view === "live") setTimeout(loadLiveSessions, 0);
  }
  $$(".nav-item").forEach((b) => b.addEventListener("click", () => go(b.dataset.view)));
  const startView = (location.hash || "").replace("#", "");
  if (VIEW_NAMES[startView]) go(startView);
  window.addEventListener("hashchange", () => {
    const v = (location.hash || "").replace("#", "");
    if (VIEW_NAMES[v]) go(v);
  });
  $$("[data-goto]").forEach((b) => b.addEventListener("click", () => {
    go(b.dataset.goto);
    if (b.dataset.goto === "studio") setTimeout(() => $("#linkInput").focus(), 160);
  }));

  /* ---------------- responsive / dropdowns ---------------- */
  $("#menuBtn").addEventListener("click", () => {
    if (window.innerWidth <= 900) {
      $("#app").classList.remove("nav-rail");
      $("#app").classList.toggle("nav-open");
    } else {
      $("#app").classList.toggle("nav-rail");
    }
    closeMenus();
  });
  $("#scrim").addEventListener("click", () => $("#app").classList.remove("nav-open"));
  $("#sideClose").addEventListener("click", () => $("#app").classList.remove("nav-open"));

  function closeMenus(except) {
    $$(".dd.open").forEach((dd) => {
      if (dd === except) return;
      dd.classList.remove("open");
      const b = dd.querySelector("[data-dd-btn]");
      if (b) b.setAttribute("aria-expanded", "false");
      const m = dd.querySelector(".menu");
      if (m) m.hidden = true;
    });
    const top = $("#topAvatar");
    if (top) top.setAttribute("aria-expanded", "false");
  }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-dd-btn]");
    if (btn) {
      const dd = btn.closest(".dd");
      if ((dd.classList.contains("ws-dd") || dd.classList.contains("user-dd")) && $("#app").classList.contains("nav-rail")) {
        $("#app").classList.remove("nav-rail");
        return;
      }
      e.preventDefault();
      e.stopPropagation();
      const isOpen = dd.classList.contains("open");
      closeMenus(dd);
      dd.classList.toggle("open", !isOpen);
      btn.setAttribute("aria-expanded", String(!isOpen));
      const menu = dd.querySelector(".menu");
      if (menu) menu.hidden = isOpen;
      if (dd.classList.contains("user-dd")) {
        const top = $("#topAvatar");
        if (top) top.setAttribute("aria-expanded", String(!isOpen));
      }
      return;
    }
    if (e.target.closest(".menu-item")) closeMenus();
    else if (!e.target.closest(".menu")) closeMenus();
  });
  const topAvatar = $("#topAvatar");
  if (topAvatar) topAvatar.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const chip = $(".user-dd [data-dd-btn]");
    if (!chip) return;
    if ($("#app").classList.contains("nav-rail")) $("#app").classList.remove("nav-rail");
    if (window.innerWidth <= 900) $("#app").classList.add("nav-open");
    setTimeout(() => chip.click(), 0);
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenus(); });
  window.addEventListener("scroll", () => closeMenus(), true);

  /* ---------------- composer modes ---------------- */
  $$("[data-mode]").forEach((b) => b.addEventListener("click", () => {
    state.mode = b.dataset.mode;
    $$("[data-mode]").forEach((x) => {
      const on = x === b;
      x.classList.toggle("on", on);
      x.setAttribute("aria-selected", String(on));
    });
    $$("[data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== state.mode; });
    if (state.mode === "link") setTimeout(() => $("#linkInput").focus(), 50);
  }));

  /* ---------------- options ---------------- */
  $$("[data-clips]").forEach((b) => b.addEventListener("click", () => {
    opt.clips = Number(b.dataset.clips);
    $("#optClipsV").textContent = String(opt.clips);
    $$("[data-clips]").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
    closeMenus();
    toast(`Output set to <b>${opt.clips} clips</b>`, "i-grid");
  }));
  $$("[data-len]").forEach((b) => b.addEventListener("click", () => {
    opt.length = Number(b.dataset.len);
    $("#optLengthV").textContent = lengths[opt.length];
    $$("[data-len]").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
    closeMenus();
    toast(`Clip length <b>${lengths[opt.length]}</b>`, "i-clock");
  }));
  $$("[data-sort]").forEach((b) => b.addEventListener("click", () => {
    state.sortMode = b.dataset.sort;
    $("#sortLabel").textContent = b.querySelector("b").textContent.replace(" first", "");
    $$("[data-sort]").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
    closeMenus();
    renderAll();
  }));
  $$("[data-ws]").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.ws === "studio") {
      $$("[data-ws='studio']").forEach((x) => x.setAttribute("aria-checked", "true"));
      closeMenus();
      return;
    }
    closeMenus();
    toast("Only one local workspace is available", "i-lock");
  }));
  $$("[data-user-action]").forEach((b) => b.addEventListener("click", () => {
    closeMenus();
    const a = b.dataset.userAction;
    if (a === "Signed out") {
      if (typeof window.ncSignOut === "function") window.ncSignOut();
      else location.replace("/auth");
      return;
    }
    toast(`Account · ${a}`, "i-agent");
  }));
  function toggleOpt(btn, key) {
    btn.addEventListener("click", () => {
      opt[key] = !opt[key];
      btn.classList.toggle("is-off", !opt[key]);
      btn.setAttribute("aria-pressed", String(opt[key]));
      toast(`${key === "captions" ? "Captions" : "Vertical reframe"} ${opt[key] ? "on" : "off"}`, key === "captions" ? "i-cap" : "i-crop");
      if (btn.id === "optVertical") btn.lastChild.textContent = opt.vertical ? "9:16" : "Source";
      renderStudio();
      renderAll();
    });
  }
  toggleOpt($("#optCaptions"), "captions");
  toggleOpt($("#optVertical"), "vertical");
  $("#optReview").addEventListener("click", () => {
    opt.review = !opt.review;
    const b = $("#optReview");
    b.classList.toggle("is-off", !opt.review);
    b.setAttribute("aria-pressed", String(opt.review));
    toast(`Review before render ${opt.review ? "on" : "off"}`, "i-check");
  });

  $$(".briefs .suggest[data-brief]").forEach((b) => b.addEventListener("click", () => {
    $("#briefInput").value = b.dataset.brief;
    $("#briefInput").focus();
  }));
  $$(".chat-suggest .suggest[data-fill]").forEach((b) => b.addEventListener("click", () => {
    $("#chatInput").value = b.dataset.fill;
    $("#chatInput").focus();
  }));

  /* ---------------- source line ---------------- */
  function ensureSourceLine() {
    let el = $("#sourceLine");
    if (!el) {
      el = document.createElement("div");
      el.id = "sourceLine";
      el.className = "source-line";
      el.hidden = true;
      el.innerHTML = `
        <span class="field-ico"><svg class="ico" aria-hidden="true"><use href="#i-link"/></svg></span>
        <span class="src-copy"><b class="src-title"></b><small class="src-sub"></small></span>
        <span class="chip chip-xs src-chip">READY</span>`;
      $("#composer").appendChild(el);
    }
    return el;
  }
  function showSource(d) {
    const el = ensureSourceLine();
    const capNote = (d.subtitles?.length || d.auto_captions?.length) ? " · captions available" : "";
    el.querySelector(".src-title").textContent = d.title || d.webpage_url || "Source ready";
    el.querySelector(".src-sub").textContent = `${(d.platform || "link").toUpperCase()} · ${d.uploader || "unknown"} · ${fmt(d.duration)}${capNote}`;
    el.querySelector(".src-chip").textContent = d.is_live ? "LIVE" : d.was_live ? "VOD" : "READY";
    el.hidden = false;
  }

  /* ---------------- job start ---------------- */
  const RUN_BTN_HTML = $("#runBtn").innerHTML;
  function runBusy(on, label) {
    const btn = $("#runBtn");
    btn.disabled = on;
    btn.innerHTML = on ? `${esc(label || "Working…")}` : RUN_BTN_HTML;
  }

  function apiOptions(fd) {
    fd.append("num_clips", String(opt.clips));
    fd.append("clip_length", lengthValues[opt.length]);
    fd.append("captions", String(opt.captions));
    fd.append("vertical", String(opt.vertical));
    fd.append("review", String(opt.review));
    fd.append("draft", String(opt.review));
    fd.append("goal", $("#briefInput").value.trim());
  }

  async function startLink() {
    const url = $("#linkInput").value.trim();
    if (!looksLikeUrl(url)) {
      toast("Paste a valid YouTube, Twitch or Kick link.", "i-link");
      $("#linkInput").focus();
      return;
    }
    runBusy(true, "Resolving…");
    try {
      const r = await fetch("/api/source", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Could not read that link");
      state.source = d;
      showSource(d);
      toast("Source found — starting analysis", "i-check");

      const fd = new FormData();
      fd.append("url", url);
      apiOptions(fd);
      const res = await fetch("/api/clip-url", { method: "POST", body: fd });
      const out = await res.json();
      if (!res.ok) throw new Error(out.detail || "Could not start the job");
      attachJob(out.job_id, d.title || url);
    } catch (e) {
      toast(esc(e.message));
    } finally {
      runBusy(false);
    }
  }

  async function startUpload() {
    if (!state.videoFile) {
      pickFile("video");
      return;
    }
    runBusy(true, "Uploading…");
    try {
      const fd = new FormData();
      fd.append("file", state.videoFile);
      apiOptions(fd);
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || "Upload failed");
      attachJob(d.job_id, state.videoFile.name);
    } catch (e) {
      toast(esc(e.message));
    } finally {
      runBusy(false);
    }
  }

  async function startImport(file) {
    if (!file) return;
    runBusy(true, "Importing…");
    try {
      const fd = new FormData();
      fd.append("transcript", file);
      const url = $("#linkInput").value.trim();
      if (looksLikeUrl(url)) fd.append("url", url);
      apiOptions(fd);
      const res = await fetch("/api/import-transcript", { method: "POST", body: fd });
      const d = await res.json();
      if (!res.ok) throw new Error(d.detail || "Import failed");
      toast(`Imported <b>${d.segments} cues</b>`, "i-file");
      attachJob(d.job_id, file.name);
    } catch (e) {
      toast(esc(e.message));
    } finally {
      runBusy(false);
    }
  }

  function pickFile(kind) {
    state.fileKind = kind;
    const inp = $("#hiddenFile");
    inp.accept = kind === "transcript" ? ".json,application/json" : "video/*";
    inp.click();
  }
  $("#runBtn").addEventListener("click", () => {
    if (state.mode === "link") startLink();
    else if (state.mode === "upload") startUpload();
    else pickFile("transcript");
  });
  $("#linkInput").addEventListener("keydown", (e) => { if (e.key === "Enter") startLink(); });
  $("#dropzone").addEventListener("click", () => pickFile("video"));
  $("#browseBtn").addEventListener("click", (e) => { e.stopPropagation(); pickFile("video"); });
  $$("[data-import], #importBtn").forEach((b) => b.addEventListener("click", (e) => {
    e.stopPropagation();
    pickFile("transcript");
  }));
  ["dragenter", "dragover"].forEach((ev) => $("#dropzone").addEventListener(ev, (e) => {
    e.preventDefault();
    $("#dropzone").classList.add("drag");
  }));
  ["dragleave", "drop"].forEach((ev) => $("#dropzone").addEventListener(ev, (e) => {
    e.preventDefault();
    $("#dropzone").classList.remove("drag");
  }));
  $("#dropzone").addEventListener("drop", (e) => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (!f) return;
    if (/\.json$/i.test(f.name)) {
      startImport(f);
      return;
    }
    state.videoFile = f;
    $("#dropzone .field-title").textContent = f.name;
    $("#dropzone .field-sub").textContent = `${(f.size / 1048576).toFixed(1)} MB · ready to render`;
    toast(`Queued <b>${esc(f.name)}</b>`, "i-upload");
    if (state.mode === "upload") startUpload();
  });
  $("#hiddenFile").addEventListener("change", (e) => {
    const f = e.target.files[0];
    if (f) {
      if (state.fileKind === "transcript") startImport(f);
      else {
        state.videoFile = f;
        $("#dropzone .field-title").textContent = f.name;
        $("#dropzone .field-sub").textContent = `${(f.size / 1048576).toFixed(1)} MB · ready to render`;
        toast(`Queued <b>${esc(f.name)}</b>`, "i-upload");
        if (state.mode === "upload") startUpload();
      }
    }
    e.target.value = "";
  });

  /* ---------------- run card + polling ---------------- */
  function setReadyRunCard() {
    const card = $("#runCard");
    card.classList.add("ready");
    card.classList.remove("done");
    $("#runTitle").textContent = "Ready when you are";
    $("#runSource").textContent = "Paste a link, drop a video, or import a transcript";
    $("#runPct").textContent = "0%";
    $("#runBar").style.width = "0%";
    $("#runElapsed").textContent = "00:00";
    $("#runMsg").textContent = "Nothing running locally right now.";
    $("#cancelRun").textContent = "Open clips";
    paintStages(-1, false);
  }
  function attachJob(id, label) {
    state.jobId = id;
    state.reviewJobId = null;
    state.reviewSelection.clear();
    $("#reviewCard").classList.add("hidden");
    let job = state.jobs.find((j) => j.id === id);
    if (!job) {
      job = { id, label, status: "queued", clips: 0 };
      state.jobs.unshift(job);
    } else {
      job.label = label || job.label;
      job.status = "queued";
    }
    try { localStorage.setItem("nc_job", id); } catch (e) { /* private mode */ }
    state.startedAt = Date.now();
    const card = $("#runCard");
    card.classList.remove("ready", "done");
    $("#runSource").textContent = label || "Local job";
    $("#runTitle").textContent = "Starting…";
    $("#runPct").textContent = "0%";
    $("#runBar").style.width = "0%";
    $("#runElapsed").textContent = "00:00";
    $("#runMsg").textContent = "Queued locally…";
    $("#cancelRun").textContent = "Running";
    paintStages(0, false);
    renderRecent();
    renderQueue();
    startPolling();
  }
  function stageIndex(stage) {
    const map = { resolve: 0, audio: 1, transcribe: 2, analyze: 3, select: 3, render: 4 };
    return map[stage] ?? 0;
  }
  function paintStages(active, done) {
    $$("#stages .stage").forEach((el, i) => {
      el.classList.toggle("done", done || i < active);
      el.classList.toggle("active", !done && i === active);
    });
  }
  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(poll, 1500);
    poll();
  }
  function stopPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
  async function poll() {
    if (!state.jobId) return;
    try {
      const r = await fetch(`/api/status/${state.jobId}`);
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Status failed");
      state.lastStatus = d;

      const pct = Math.max(0, Math.min(100, Number(d.progress) || 0));
      const status = String(d.status || "");
      const terminal = ["done", "error", "needs_review", "cancelled"].includes(status);
      $("#runBar").style.width = `${pct}%`;
      $("#runPct").textContent = `${Math.round(pct)}%`;
      $("#runTitle").textContent = status === "error" ? "Job failed"
        : status === "needs_review" ? "Moments ready for review"
        : status === "cancelled" ? "Job cancelled"
        : (d.message || "Working…");
      $("#runMsg").textContent = status === "error"
        ? (d.error || "Something went wrong")
        : `Stage · ${d.stage || "preparing"}`;
      const el = Math.floor((Date.now() - state.startedAt) / 1000);
      $("#runElapsed").textContent = fmt(el);
      paintStages(stageIndex(d.stage), status === "done");
      $("#cancelRun").textContent = status === "needs_review" ? "Review" : terminal ? "Open clips" : "Cancel";

      const job = state.jobs.find((j) => j.id === d.id);
      if (job) {
        job.status = d.status;
        job.clips = (d.clips || []).length;
      }
      renderRecent();
      renderQueue();

      if (status === "needs_review") {
        stopPolling();
        $("#runCard").classList.remove("ready", "done");
        renderData(d);
        renderReview(d);
        if (state.chatActiveJob === d.id) addJobMessageToChat(d);
      } else if (terminal) {
        stopPolling();
        $("#runCard").classList.toggle("done", status === "done");
        $("#runCard").classList.remove("ready");
        $("#runPct").textContent = status === "done" ? "100%" : `${Math.round(pct)}%`;
        $("#runBar").style.width = status === "done" ? "100%" : `${pct}%`;
        $("#reviewCard").classList.add("hidden");
        renderData(d);
        if (state.chatActiveJob === d.id) addJobMessageToChat(d);
      }
    } catch (e) {
      stopPolling();
      toast(esc(e.message));
    }
  }
  $("#cancelRun").addEventListener("click", async () => {
    if (state.pollTimer && state.jobId) {
      try {
        const r = await fetch(`/api/cancel/${state.jobId}`, { method: "POST" });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || "Cancel failed");
        toast("Cancelling after the current clip…", "i-x");
      } catch (e) {
        toast(esc(e.message));
      }
      return;
    }
    if (state.lastStatus?.status === "needs_review") {
      renderReview(state.lastStatus);
      $("#reviewCard").scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    go("clips");
  });

  /* ---------------- clips ---------------- */
  function score100(v) {
    const n = Number(v) || 0;
    return Math.max(0, Math.min(100, Math.round(n <= 10 ? n * 10 : n)));
  }
  function renderData(d) {
    const platform = (d.source_info?.platform || (d.source_url ? "link" : "local")).toUpperCase();
    state.clips = (d.clips || []).map((c, i) => {
      const st = (c.render_status === "done" || c.render_status === "draft")
        ? "ready" : c.render_status === "error" ? "failed"
        : c.render_status === "pending" ? "pending" : "rendering";
      const words = (c.reason || c.title || "").split(/\s+/).filter(Boolean).slice(0, 12);
      return {
        _idx: i,
        title: c.title || `Clip ${i + 1}`,
        platform,
        start: fmt(c.start),
        end: fmt(c.end),
        score: score100(c.score),
        cap: words.join(" "),
        hot: 1,
        status: st,
        progress: 0,
        url: c.url || "",
        error: c.error || "",
        renderError: c.render_error || "",
        captions: !!c.captions,
        vertical: d.settings?.vertical !== false,
        reason: c.reason || "",
        note: d.notes?.[String(i)] || "",
        hooks: c.hooks || [],
        hookTitle: c.hook_title || "",
        scoreReason: c.score_reason || "",
        excerpt: c.transcript_excerpt || "",
        tracks: c.tracks || [],
        config: c.config || {},
        focus: c.focus || null,
        face_shift_ms: c.face_shift_ms || 150,
        watermark: c.watermark || "@NOCTX",
      };
    });
    renderStudio();
    renderAll();
    renderStats(d);
  }
  function renderStats(d) {
    const ready = state.clips.filter((c) => c.status === "ready").length;
    const rail = $(".rail .rc");
    if (rail) {
      const num = rail.querySelector(".stat-num");
      if (num) num.textContent = String(state.clips.length);
      const delta = rail.querySelector(".delta");
      if (delta) delta.textContent = `+${state.clips.length}`;
      const titleCount = rail.querySelector(".rail-title .mono");
      if (titleCount) titleCount.textContent = String(state.clips.length).padStart(2, "0");
      const cells = rail.querySelectorAll(".stat-cell b");
      if (cells[0]) cells[0].textContent = fmt(d.duration || 0);
      if (cells[1]) cells[1].textContent = String(
        state.clips.length ? Math.round(state.clips.reduce((a, c) => a + (c.score || 0), 0) / state.clips.length) : 0
      );
    }
    const navCount = $(".nav-item[data-view='clips'] .nav-count");
    if (navCount) navCount.textContent = String(state.clips.length);
    const allFilter = $("#clipFilters .seg-btn[data-filter='all'] .n");
    const readyFilter = $("#clipFilters .seg-btn[data-filter='ready'] .n");
    const renderingFilter = $("#clipFilters .seg-btn[data-filter='rendering'] .n");
    const failedFilter = $("#clipFilters .seg-btn[data-filter='failed'] .n");
    if (allFilter) allFilter.textContent = String(state.clips.length);
    if (readyFilter) readyFilter.textContent = String(ready);
    if (renderingFilter) renderingFilter.textContent = String(state.clips.filter((c) => c.status === "rendering").length);
    if (failedFilter) failedFilter.textContent = String(state.clips.filter((c) => c.status === "failed").length);
  }
  function resetStats() {
    const rail = $(".rail .rc");
    if (rail) {
      const num = rail.querySelector(".stat-num");
      if (num) num.textContent = "0";
      const delta = rail.querySelector(".delta");
      if (delta) delta.textContent = "+0";
      const titleCount = rail.querySelector(".rail-title .mono");
      if (titleCount) titleCount.textContent = "00";
      const cells = rail.querySelectorAll(".stat-cell b");
      if (cells[0]) cells[0].textContent = "0:00";
      if (cells[1]) cells[1].textContent = "0";
    }
    const navCount = $(".nav-item[data-view='clips'] .nav-count");
    if (navCount) navCount.textContent = "0";
    ["all", "ready", "rendering", "failed"].forEach((f) => {
      const n = $(`#clipFilters .seg-btn[data-filter='${f}'] .n`);
      if (n) n.textContent = "0";
    });
  }
  function captionHTML(c) {
    if (!c.cap) return "";
    return c.cap.split(" ").map((w, i) => i === c.hot ? `<b>${esc(w)}</b>` : esc(w)).join(" ");
  }
  function clipCard(c) {
    const watchable = !!c.url;
    const ready = c.status === "ready" || watchable;
    const cls = c.status === "failed" ? "failed" : c.status === "rendering" ? "rendering" : "";
    const note = c.note
      ? `<span class="note-chip ${c.note === "keep" ? "keep" : c.note === "flop" ? "flop" : ""}">${esc(c.note)}</span>`
      : "";
    return `<article class="clip ${cls}">
      <div class="clip-media"${ready ? ` data-edit="${c._idx}"` : ""}>
        ${ready ? `<video class="clip-thumb" src="${esc(c.url)}#t=0.4" muted playsinline preload="metadata"></video>` : ""}
        <span class="grain" aria-hidden="true"></span>
        <div class="media-top">
          <span class="pf">${esc(c.platform)}</span>
          ${ready ? `<span class="score" title="Moment score">${c.score || "—"}</span>` : ""}
        </div>
        ${ready ? `<button class="play" data-edit="${c._idx}" aria-label="Open editor"><svg class="ico" aria-hidden="true"><use href="#i-play"/></svg></button>` : ""}
        ${ready && opt.captions && c.cap ? `<div class="cap">${captionHTML(c)}</div>` : ""}
        ${c.status === "rendering" ? `<div class="render-ov"><span class="rp"><span class="spin"></span>Rendering</span><div class="mini-track"><span style="width:24%"></span></div></div>` : ""}
        ${c.status === "pending" ? `<div class="pending-ov">Not rendered</div>` : ""}
        ${c.status === "failed" ? `<div class="fail-ov"><svg class="ico" aria-hidden="true"><use href="#i-x"/></svg>Render failed</div>` : ""}
      </div>
      <div class="clip-body">
        <div class="clip-h">
          <h3>${esc(c.title)}</h3>
          <div class="clip-x">
            ${ready ? `<button class="act" data-edit="${c._idx}" aria-label="Edit clip"><svg class="ico" aria-hidden="true"><use href="#i-edit"/></svg></button>` : ""}
            ${ready ? `<a class="act" href="${esc(c.url)}" download="clip_${c._idx + 1}.mp4" aria-label="Download clip"><svg class="ico" aria-hidden="true"><use href="#i-download"/></svg></a>` : ""}
            <button class="act" data-clip-menu="${c._idx}" aria-label="More actions"><svg class="ico" aria-hidden="true"><use href="#i-more"/></svg></button>
          </div>
        </div>
        <div class="clip-sub">
          <span>${c.start}–${c.end}</span><i></i><span>${c.vertical ? "9:16" : "Source"}</span><i></i><span>${c.status === "failed" ? "Failed" : c.status === "rendering" ? "Rendering" : c.status === "pending" ? "Queued" : (c.captions ? "Captions" : "Clean")}</span>${note ? `<i></i>${note}` : ""}
        </div>
        ${c.scoreReason ? `<p class="clip-why">${esc(c.scoreReason.slice(0, 130))}</p>` : ""}
        ${c.renderError ? `<p class="clip-err">${esc(c.renderError.slice(0, 140))}</p>` : ""}
        ${c.status === "failed" && c.error ? `<p class="clip-err">${esc(c.error.slice(0, 140))}</p>` : ""}
      </div>
    </article>`;
  }
  function emptyCard(msg) {
    return `<div class="clip-empty">${esc(msg)}</div>`;
  }
  function renderStudio() {
    const ready = state.clips.filter((c) => c.status === "ready").slice(0, 4);
    $("#studioClips").innerHTML = ready.length ? ready.map(clipCard).join("") : emptyCard("No clips yet. Run a job in Composer or ask the Agent.");
  }
  function renderAll() {
    let list = state.clips.filter((c) => (state.filter === "all" || c.status === state.filter) && c.title.toLowerCase().includes(state.query));
    if (state.sortMode === "old") list = list.slice().reverse();
    else if (state.sortMode === "score") list = list.slice().sort((a, b) => (b.score || 0) - (a.score || 0));
    $("#allClips").innerHTML = list.length ? list.map(clipCard).join("") : emptyCard("No clips match that filter.");
    $("#clipsEmpty").style.display = state.clips.length ? "none" : "block";
  }
  /* ---------------- review / continue / regenerate ---------------- */
  function renderReview(d) {
    const card = $("#reviewCard");
    const list = $("#reviewList");
    if (!card || !list) return;
    if (!d || !d.clips || !d.clips.length) {
      card.classList.add("hidden");
      return;
    }
    state.lastStatus = d;
    if (state.reviewJobId !== d.id) {
      state.reviewJobId = d.id;
      state.reviewSelection = new Set(d.clips.map((_, i) => i));
    }
    list.innerHTML = d.clips.map((c, i) => {
      const checked = state.reviewSelection.has(i) ? "checked" : "";
      const score = score100(c.score);
      const status = c.render_status === "draft" ? "draft" : c.render_status === "done" ? "final" : "unrendered";
      return `<label class="review-item">
        <input type="checkbox" data-review-check="${i}" ${checked} />
        <span class="review-copy">
          <span class="review-top"><b>${esc(c.title || `Clip ${i + 1}`)}</b><span class="review-score">${score}</span><span class="note-chip">${status}</span></span>
          ${c.hook_title ? `<span class="hook-title">${esc(c.hook_title)}</span>` : ""}
          <span class="review-meta">${fmt(c.start)}–${fmt(c.end)} · score ${score}/100</span>
          <span class="review-reason">${esc(c.score_reason || c.reason || "Selected by the moment scorer.")}</span>
          ${c.transcript_excerpt ? `<span class="review-excerpt">“${esc(c.transcript_excerpt.slice(0, 180))}”</span>` : ""}
        </span>
        <button type="button" class="review-regen" data-review-regen="${i}">regenerate</button>
      </label>`;
    }).join("");
    card.classList.remove("hidden");
  }
  $("#reviewList").addEventListener("change", (e) => {
    const box = e.target.closest("[data-review-check]");
    if (!box) return;
    const i = Number(box.dataset.reviewCheck);
    if (box.checked) state.reviewSelection.add(i);
    else state.reviewSelection.delete(i);
  });
  $("#reviewList").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-review-regen]");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const i = Number(btn.dataset.reviewRegen);
    if (!state.jobId) return;
    btn.disabled = true;
    try {
      const r = await fetch(`/api/regenerate/${state.jobId}/${i}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ goal: $("#briefInput").value.trim() }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Could not regenerate that moment");
      toast(`Regenerating clip ${i + 1} with a fresh take`, "i-refresh");
      attachJob(state.jobId, state.jobs.find((j) => j.id === state.jobId)?.label || "Regenerated job");
    } catch (err) {
      toast(esc(err.message));
      btn.disabled = false;
    }
  });
  $("#renderSelected").addEventListener("click", async () => {
    const d = state.lastStatus;
    if (!d || !d.id || !state.reviewSelection.size) {
      toast("Select at least one moment first", "i-lock");
      return;
    }
    const indices = Array.from(state.reviewSelection).sort((a, b) => a - b);
    const hooks = {};
    indices.forEach((i) => { hooks[String(i)] = Number(d.clips[i]?.hook_index || 0); });
    try {
      const r = await fetch(`/api/render-selected/${d.id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          indices,
          captions: d.settings?.captions !== false,
          vertical: d.settings?.vertical !== false,
          draft: false,
          hooks,
        }),
      });
      const out = await r.json();
      if (!r.ok) throw new Error(out.detail || "Render failed");
      $("#reviewCard").classList.add("hidden");
      toast(`Rendering ${out.rendering || indices.length} selected clips`, "i-bolt");
      attachJob(d.id, state.jobs.find((j) => j.id === d.id)?.label || "Selected clips");
    } catch (e) {
      toast(esc(e.message));
    }
  });
  async function showReviewForCurrentJob() {
    if (!state.jobId) { toast("Run a job first", "i-lock"); return; }
    let d = state.lastStatus?.id === state.jobId ? state.lastStatus : null;
    if (!d) {
      try {
        const r = await fetch(`/api/jobs/${state.jobId}`);
        d = await r.json();
        if (!r.ok) throw new Error(d.detail || "Job not found");
      } catch (e) { toast(esc(e.message)); return; }
    }
    if (!d.clips || !d.clips.length) { toast("No moments to review yet", "i-lock"); return; }
    go("studio");
    renderReview(d);
    setTimeout(() => $("#reviewCard").scrollIntoView({ behavior: "smooth", block: "start" }), 80);
  }
  $("#reviewRun").addEventListener("click", showReviewForCurrentJob);
  async function continueFromCurrentJob() {
    if (!state.jobId) { toast("Run a job first", "i-lock"); return; }
    try {
      const r = await fetch(`/api/continue/${state.jobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          num_clips: opt.clips,
          goal: $("#briefInput").value.trim(),
          auto_render: true,
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Could not continue clipping");
      toast(`Found ${d.added} more moments from this source`, "i-refresh");
      attachJob(state.jobId, state.jobs.find((j) => j.id === state.jobId)?.label || "Continued run");
    } catch (e) {
      toast(esc(e.message));
    }
  }
  $("#continueRun").addEventListener("click", continueFromCurrentJob);
  $("#continueClips").addEventListener("click", continueFromCurrentJob);
  $("#retryFailed").addEventListener("click", async () => {
    if (!state.jobId) { toast("Open a job first", "i-lock"); return; }
    try {
      const r = await fetch(`/api/retry-failed/${state.jobId}`, { method: "POST" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Could not retry failed clips");
      if (!d.retrying) { toast("All clips are rendered", "i-check"); return; }
      toast(`Rendering ${d.retrying} remaining clips`, "i-refresh");
      attachJob(state.jobId, state.jobs.find((j) => j.id === state.jobId)?.label || "Retry");
    } catch (e) { toast(esc(e.message)); }
  });

  /* ---------------- persistent library / settings ---------------- */
  async function loadLibrary() {
    try {
      const [lib, dict, presets, queue] = await Promise.all([
        fetch("/api/library"), fetch("/api/dictionary"),
        fetch("/api/presets"), fetch("/api/publish-queue"),
      ]);
      const [libJson, dictJson, presetJson, queueJson] = await Promise.all([
        lib.json(), dict.json(), presets.json(), queue.json(),
      ]);
      state.libraryGroups = libJson.groups || [];
      state.dictionary = dictJson.entries || {};
      state.presets = presetJson.presets || {};
      state.publishItems = queueJson.items || [];
      renderLibrary();
      renderDictionary(state.dictionary);
      renderPresets(state.presets);
      renderPublish();
      const count = $("#navLibraryCount");
      if (count) count.textContent = String(state.libraryGroups.length);
    } catch (e) {
      toast(esc(e.message));
    }
  }
  function renderLibrary() {
    const root = $("#libraryGroups");
    if (!root) return;
    if (!state.libraryGroups.length) {
      root.innerHTML = emptyCard("No saved sources yet. Every run will be filed here automatically.");
      return;
    }
    root.innerHTML = state.libraryGroups.map((g) => `<article class="channel-card">
      <div class="channel-head">
        <span class="channel-av">${esc(String(g.channel || "?").slice(0, 1).toUpperCase())}</span>
        <span><div class="channel-name">${esc(g.channel)}</div><div class="channel-sub">${g.count} run${g.count === 1 ? "" : "s"} · ${g.clips} clips</div></span>
      </div>
      ${(g.jobs || []).map((j) => `<div class="channel-job">
        <span class="rd ${j.status === "done" ? "done" : j.status === "error" ? "err" : "run"}"></span>
        <span class="j-title" title="${esc(j.title)}">${esc(j.title)}</span>
        <span class="j-meta">${j.clips || 0} clips</span>
        <button class="btn btn-ghost btn-xs" data-open-job="${esc(j.id)}" data-job-title="${esc(j.title)}">Open</button>
        <button class="btn btn-ghost btn-xs" data-reclip-job="${esc(j.id)}" data-job-title="${esc(j.title)}" data-job-goal="${esc(j.goal || "")}">Reclip</button>
      </div>`).join("")}
    </article>`).join("");
  }
  $("#libraryGroups").addEventListener("click", (e) => {
    const open = e.target.closest("[data-open-job]");
    if (open) {
      attachJob(open.dataset.openJob, open.dataset.jobTitle);
      go("clips");
      return;
    }
    const reclip = e.target.closest("[data-reclip-job]");
    if (reclip) {
      attachJob(reclip.dataset.reclipJob, reclip.dataset.jobTitle);
      $("#briefInput").value = reclip.dataset.jobGoal || `more moments from ${reclip.dataset.jobTitle}`;
      go("studio");
      toast("Source loaded — set a new brief and run it again", "i-spark");
    }
  });
  $("#refreshLibrary").addEventListener("click", loadLibrary);
  function renderDictionary(entries) {
    const root = $("#dictionaryRows");
    if (!root) return;
    const rows = Object.entries(entries || {}).map(([from, to]) => `<div class="dict-row">
      <input value="${esc(from)}" data-dict-from aria-label="ASR text" />
      <span>→</span>
      <input value="${esc(to)}" data-dict-to aria-label="Corrected text" />
      <button class="btn btn-ghost btn-xs" data-remove-dict aria-label="Remove">×</button>
    </div>`).join("");
    root.innerHTML = `${rows}<div class="dict-row">
      <input placeholder="asr spelling" data-dict-from aria-label="ASR text" />
      <span>→</span>
      <input placeholder="correct name" data-dict-to aria-label="Corrected text" />
      <button class="btn btn-ghost btn-xs" data-add-dict aria-label="Add">+</button>
    </div>`;
  }
  $("#dictionaryRows").addEventListener("click", (e) => {
    if (!e.target.closest("[data-add-dict]")) return;
    renderDictionary(state.dictionary);
    const rows = $$("#dictionaryRows .dict-row");
    const last = rows[rows.length - 1];
    if (last) last.querySelector("input")?.focus();
  });
  $("#saveDictionary").addEventListener("click", async () => {
    const entries = {};
    $$("#dictionaryRows .dict-row").forEach((row) => {
      const from = row.querySelector("[data-dict-from]")?.value.trim();
      const to = row.querySelector("[data-dict-to]")?.value.trim();
      if (from && to) entries[from] = to;
    });
    try {
      const r = await fetch("/api/dictionary", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entries }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Save failed");
      state.dictionary = d.entries || entries;
      renderDictionary(state.dictionary);
      toast(`Saved ${Object.keys(state.dictionary).length} corrections`, "i-check");
    } catch (e) { toast(esc(e.message)); }
  });
  function renderPresets(presets) {
    const root = $("#presetRows");
    if (!root) return;
    const rows = Object.entries(presets || {}).map(([channel, p]) => `<div class="preset-row" data-preset-channel="${esc(channel)}">
      <b>${esc(channel)}</b>
      <input type="number" step="0.01" value="${Number(p.cx ?? 0.10).toFixed(2)}" data-preset-cx title="Facecam centre X" />
      <input type="number" step="0.01" value="${Number(p.cy ?? 0.30).toFixed(2)}" data-preset-cy title="Facecam centre Y" />
      <input type="number" step="10" value="${Number(p.face_shift_ms ?? 150)}" data-preset-shift title="Facecam shift ms" />
    </div>`).join("");
    root.innerHTML = `${rows || '<div class="settings-note">No crop presets saved yet.</div>'}
      <button class="btn btn-secondary btn-sm" id="saveAllPresets">Save preset values</button>`;
  }
  document.addEventListener("click", async (e) => {
    if (!e.target.closest("#saveAllPresets")) return;
    const presets = {};
    $$("#presetRows .preset-row").forEach((row) => {
      const channel = row.dataset.presetChannel;
      if (!channel) return;
      presets[channel] = {
        cx: Number(row.querySelector("[data-preset-cx]")?.value || 0.10),
        cy: Number(row.querySelector("[data-preset-cy]")?.value || 0.30),
        face_shift_ms: Number(row.querySelector("[data-preset-shift]")?.value || 150),
        watermark: "@NOCTX",
      };
    });
    try {
      const r = await fetch("/api/presets/bulk", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ presets }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Save failed");
      state.presets = d.presets || presets;
      renderPresets(state.presets);
      toast("Crop presets saved", "i-check");
    } catch (err) { toast(esc(err.message)); }
  });
  $("#refreshPresets").addEventListener("click", async () => {
    try {
      const r = await fetch("/api/presets");
      const d = await r.json();
      state.presets = d.presets || {};
      renderPresets(state.presets);
      toast("Presets refreshed", "i-refresh");
    } catch (e) { toast(esc(e.message)); }
  });
  $("#saveCropPreset").addEventListener("click", async () => {
    const c = state.clips[state.editorIndex];
    if (!c || !state.jobId) { toast("Open a clip first", "i-lock"); return; }
    try {
      const r = await fetch(`/api/jobs/${state.jobId}`);
      const job = await r.json();
      const channel = job.channel || "Unsorted";
      const focus = c.focus || { cx: 0.10, cy: 0.30 };
      const res = await fetch("/api/presets", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          channel,
          focus: { ...focus, watermark: "@NOCTX" },
          face_shift_ms: Number(c.face_shift_ms || 150),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Save failed");
      state.presets = data.presets || {};
      renderPresets(state.presets);
      toast(`Crop preset saved for ${channel}`, "i-check");
    } catch (e) { toast(esc(e.message)); }
  });

  /* ---------------- live sessions ---------------- */
  function liveCountdown(value) {
    if (!value) return "—";
    const t = typeof value === "number" ? value * 1000 : Date.parse(value);
    if (!Number.isFinite(t)) return "—";
    const left = Math.max(0, Math.round((t - Date.now()) / 1000));
    if (!left) return "due now";
    return left >= 60 ? `${Math.floor(left / 60)}m ${left % 60}s` : `${left}s`;
  }
  async function loadLiveSessions() {
    const root = $("#liveSessions");
    if (!root) return;
    try {
      const r = await fetch("/api/live/sessions");
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Could not load live sessions");
      state.liveSessions = d.sessions || [];
      renderLiveSessions();
    } catch (e) {
      root.innerHTML = `<div class="live-empty">${esc(e.message)}</div>`;
    }
  }
  function renderLiveSessions() {
    const root = $("#liveSessions");
    if (!root) return;
    const sessions = state.liveSessions;
    const active = sessions.filter((s) => s.state === "live").length;
    const badge = $("#navLiveCount");
    if (badge) badge.textContent = String(active);
    if (!sessions.length) {
      root.innerHTML = `<div class="live-empty">No live sessions yet. Start one above — only audio from the moment you join can be clipped.</div>`;
      return;
    }
    root.innerHTML = sessions.map((s) => {
      const st = String(s.state || "stopped");
      const coverage = Math.max(0, Math.min(100,
        Math.round((Number(s.clipped_until_seconds || 0) / Math.max(1, Number(s.max_duration_seconds || 43200))) * 100)));
      const stats = [
        ["Passes", String(s.pass_count || 0)],
        ["Covered", fmt(s.clipped_until_seconds || 0)],
        ["Target", String(s.target_clips || 0) + "/pass"],
        ["Next pass", liveCountdown(s.next_pass_at)],
      ];
      const note = s.last_error
        ? `<p class="live-note err">${esc(String(s.last_error).slice(0, 180))}</p>`
        : st === "live"
          ? `<p class="live-note">Recording and clipping every ${Math.round((Number(s.pass_window_seconds || 1800)) / 60)} minutes. New moments appear in the review feed automatically.</p>`
          : `<p class="live-note">Session ${esc(s.stop_reason || "stopped")}. Recorded moments stay in the job library.</p>`;
      return `<article class="live-card">
        <div class="live-card-head">
          <span class="live-card-icon"><svg class="ico" aria-hidden="true"><use href="#i-spark"/></svg></span>
          <div style="min-width:0">
            <div class="live-card-title" title="${esc(s.src_url || "")}">${esc(s.provider || "Live")} session</div>
            <div class="live-card-sub">${esc(s.curation_prompt || "")}</div>
          </div>
          <span class="live-state ${esc(st)}">${esc(st)}</span>
        </div>
        <div class="live-metrics">${stats.map(([k, v]) => `<div class="live-metric"><span>${k}</span><b>${esc(v)}</b></div>`).join("")}</div>
        <div class="live-track"><i style="width:${coverage}%"></i></div>
        ${note}
        <div class="live-actions">
          ${s.job_id ? `<button class="btn btn-secondary btn-sm" data-live-action="open" data-live-id="${esc(s.id)}">Open clips</button>` : ""}
          ${st === "live" ? `<button class="btn btn-secondary btn-sm" data-live-action="pass" data-live-id="${esc(s.id)}">Run pass now</button>` : ""}
          ${st === "live" ? `<button class="btn btn-ghost btn-sm" data-live-action="stop" data-live-id="${esc(s.id)}">Stop</button>` : ""}
        </div>
      </article>`;
    }).join("");
  }
  $("#refreshLive")?.addEventListener("click", loadLiveSessions);
  $("#liveStart")?.addEventListener("click", async () => {
    const url = $("#liveUrl").value.trim();
    if (!looksLikeUrl(url)) { toast("Paste a valid live URL", "i-lock"); return; }
    const btn = $("#liveStart");
    btn.disabled = true;
    try {
      const r = await fetch("/api/live/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url,
          goal: $("#liveGoal").value.trim(),
          num_clips: Number($("#liveTarget").value || 5),
          first_pass_seconds: Number($("#liveFirstPass").value || 900),
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Could not start live session");
      toast("Recording started · first pass in " + fmt(d.first_pass_in), "i-bolt");
      $("#liveUrl").value = "";
      attachJob(d.job_id, "Live session");
      await loadLiveSessions();
    } catch (e) {
      toast(esc(e.message));
    } finally {
      btn.disabled = false;
    }
  });
  $("#liveSessions")?.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-live-action]");
    if (!btn) return;
    const action = btn.dataset.liveAction;
    const id = btn.dataset.liveId;
    const session = state.liveSessions.find((s) => s.id === id);
    if (action === "open") {
      if (!session?.job_id) return;
      attachJob(session.job_id, "Live session");
      go("studio");
      return;
    }
    btn.disabled = true;
    try {
      const r = await fetch(`/api/live/${encodeURIComponent(id)}/${action === "stop" ? "stop" : "pass"}`, { method: "POST" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Live command failed");
      if (action === "pass") {
        toast(d.skipped ? "Not enough new audio yet" : `Pass ${d.pass || ""} added ${d.added || 0} moments`, "i-refresh");
        if (session?.job_id) attachJob(session.job_id, "Live session");
      } else {
        toast("Live recording stopped", "i-x");
      }
      await loadLiveSessions();
    } catch (err) {
      toast(esc(err.message));
      btn.disabled = false;
    }
  });
  setInterval(() => {
    if ($("#page-live") && !$("#page-live").hidden) renderLiveSessions();
  }, 5000);

  /* ---------------- publish queue ---------------- */
  const DEFAULT_TAGS = "nickeh30,gta6,gta6clips,familyfriendly,fyp";
  function makePublishItem(c) {
    const title = c.title || "Clip";
    return {
      id: `${state.jobId || "job"}:${c._idx}`,
      job_id: state.jobId || "",
      index: c._idx,
      title,
      url: c.url || "",
      tiktok_title: title.slice(0, 90),
      tiktok_copy: `${title}. no context needed.`,
      youtube_title: title.slice(0, 90),
      youtube_copy: `${title}. no context needed.\n\n#Shorts`,
      tags: DEFAULT_TAGS,
      status: "draft",
    };
  }
  function renderPublish() {
    const root = $("#publishList");
    if (!root) return;
    if (!state.publishItems.length) {
      root.innerHTML = emptyCard("Add ready clips to build a TikTok + YouTube Shorts post package.");
      return;
    }
    root.innerHTML = state.publishItems.map((item) => `<article class="publish-item" data-publish-id="${esc(item.id)}">
      <div class="publish-title">${esc(item.title)} <span class="note-chip">${esc(item.status || "draft")}</span></div>
      <label class="insp-label">TikTok title</label>
      <input class="publish-input" value="${esc(item.tiktok_title || "")}" data-publish-field="tiktok_title" />
      <label class="insp-label">TikTok caption</label>
      <textarea data-publish-field="tiktok_copy">${esc(item.tiktok_copy || "")}</textarea>
      <label class="insp-label">TikTok tags · exactly 5</label>
      <input class="publish-input" value="${esc(item.tags || "")}" data-publish-field="tags" />
      <div class="publish-tags">${esc((item.tags || "").split(",").filter(Boolean).map((t) => `#${t.trim().replace(/^#/, "")}`).join(" "))}</div>
      <label class="insp-label">YouTube Shorts title</label>
      <input class="publish-input" value="${esc(item.youtube_title || "")}" data-publish-field="youtube_title" />
      <label class="insp-label">YouTube description</label>
      <textarea data-publish-field="youtube_copy">${esc(item.youtube_copy || "")}</textarea>
    </article>`).join("");
  }
  $("#publishList").addEventListener("input", (e) => {
    const field = e.target.dataset.publishField;
    const id = e.target.closest("[data-publish-id]")?.dataset.publishId;
    if (!field || !id) return;
    const item = state.publishItems.find((x) => x.id === id);
    if (item) item[field] = e.target.value;
    if (field === "tags") {
      const tags = e.target.closest(".publish-item")?.querySelector(".publish-tags");
      if (tags) tags.textContent = e.target.value.split(",").filter(Boolean).map((t) => `#${t.trim().replace(/^#/, "")}`).join(" ");
    }
  });
  $("#addPublish").addEventListener("click", async () => {
    const panel = $("#publishPanel");
    panel.classList.toggle("hidden");
    if (panel.classList.contains("hidden")) return;
    if (!state.publishItems.length) {
      const ready = state.clips.filter((c) => c.status === "ready");
      if (!ready.length) { toast("Render a clip first", "i-lock"); return; }
      state.publishItems = ready.map(makePublishItem);
      await savePublishQueue(false);
    }
    renderPublish();
    loadPublications();
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("#closePublish").addEventListener("click", () => $("#publishPanel").classList.add("hidden"));
  async function savePublishQueue(showToast = true) {
    try {
      const r = await fetch("/api/publish-queue", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: state.publishItems }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Save failed");
      state.publishItems = d.items || state.publishItems;
      if (showToast) toast(`Saved ${state.publishItems.length} queued posts`, "i-check");
    } catch (e) { toast(esc(e.message)); }
  }
  $("#savePublish").addEventListener("click", () => savePublishQueue());

  async function loadPublications() {
    const root = $("#scheduleList");
    if (!root) return;
    try {
      const r = await fetch("/api/publications");
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Could not load scheduled posts");
      state.publications = d.items || [];
      renderPublications();
    } catch (e) {
      root.innerHTML = `<div class="live-empty">${esc(e.message)}</div>`;
    }
  }
  function publicationTitle(item) {
    const job = state.jobs.find((j) => j.id === item.job_id);
    const clip = state.clips.find((c) => c._idx === Number(item.clip_idx));
    return clip?.title || item.caption?.split("\n")[0] || job?.label || `Clip ${Number(item.clip_idx || 0) + 1}`;
  }
  function renderPublications() {
    const root = $("#scheduleList");
    if (!root) return;
    if (!state.publications.length) {
      root.innerHTML = `<div class="live-empty">Nothing scheduled. Approve a post time above to queue TikTok + Shorts.</div>`;
      return;
    }
    root.innerHTML = state.publications.map((p) => {
      const when = p.scheduled_at
        ? new Date(p.scheduled_at).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })
        : "As soon as approved";
      const status = String(p.status || "scheduled");
      return `<div class="schedule-item" data-pub-id="${esc(p.id || p.uid || "")}">
        <div class="schedule-main">
          <b>${esc(publicationTitle(p))}</b>
          <span>${esc(String(p.platform || "").toUpperCase())} · ${esc(when)}</span>
        </div>
        <span class="schedule-status ${esc(status)}">${esc(status)}</span>
        ${status !== "posted" ? `<button class="btn btn-ghost btn-xs" data-pub-action="post">Post now</button>` : ""}
        <button class="icon-btn" data-pub-action="delete" aria-label="Cancel scheduled post"><svg class="ico" aria-hidden="true"><use href="#i-x"/></svg></button>
      </div>`;
    }).join("");
  }
  $("#schedulePublish")?.addEventListener("click", async () => {
    const when = $("#publishScheduleAt").value;
    if (!when) { toast("Pick a schedule time first", "i-lock"); return; }
    if (!state.publishItems.length) { toast("Build a publish queue first", "i-lock"); return; }
    const btn = $("#schedulePublish");
    btn.disabled = true;
    try {
      await savePublishQueue(false);
      const scheduledAt = new Date(when).toISOString();
      let count = 0;
      for (const item of state.publishItems) {
        if (!item.job_id) continue;
        const tags = String(item.tags || "").split(",").map((t) => t.trim().replace(/^#/, "")).filter(Boolean);
        const targets = [
          { platform: "tiktok", caption: item.tiktok_copy || item.title, hashtags: tags.slice(0, 5) },
          { platform: "youtube", caption: item.youtube_copy || item.title, hashtags: ["shorts", ...tags.slice(0, 2)] },
        ];
        for (const t of targets) {
          const r = await fetch("/api/publications", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              job_id: item.job_id, clip_idx: Number(item.index || 0),
              platform: t.platform, caption: t.caption, hashtags: t.hashtags,
              scheduled_at: scheduledAt, approved: true,
            }),
          });
          const out = await r.json();
          if (!r.ok) throw new Error(out.detail || "Could not schedule post");
          count += 1;
        }
      }
      toast(`Approved and scheduled ${count} posts`, "i-check");
      await loadPublications();
    } catch (e) {
      toast(esc(e.message));
    } finally {
      btn.disabled = false;
    }
  });
  $("#scheduleList")?.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-pub-action]");
    if (!btn) return;
    const row = btn.closest("[data-pub-id]");
    const id = row?.dataset.pubId;
    if (!id) return;
    const action = btn.dataset.pubAction;
    btn.disabled = true;
    try {
      const r = await fetch(`/api/publications/${encodeURIComponent(id)}${action === "post" ? "/post" : ""}`, {
        method: action === "post" ? "POST" : "DELETE",
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Publication action failed");
      if (d.result?.needs_integration) toast(d.result.error || "WoopSocial API is not configured", "i-lock");
      else toast(action === "post" ? "Post sent to WoopSocial" : "Scheduled post removed", "i-check");
      await loadPublications();
    } catch (err) {
      toast(esc(err.message));
      btn.disabled = false;
    }
  });

  $("#copyPublish").addEventListener("click", async () => {
    if (!state.publishItems.length) { toast("The queue is empty", "i-lock"); return; }
    const text = state.publishItems.map((item) => {
      const clip = item.url ? (item.url.startsWith("http") ? item.url : `${location.origin}${item.url}`) : "clip pending";
      const tags = (item.tags || "").split(",").filter(Boolean).map((t) => `#${t.trim().replace(/^#/, "")}`).join(" ");
      return [`=== ${item.title} ===`, `CLIP: ${clip}`, "", "TIKTOK", `title: ${item.tiktok_title || item.title}`, item.tiktok_copy || "", tags || DEFAULT_TAGS.split(",").map((t) => `#${t}`).join(" "), "", "YOUTUBE SHORTS", `title: ${item.youtube_title || item.title}`, item.youtube_copy || ""].join("\n");
    }).join("\n\n---\n\n");
    try {
      await navigator.clipboard.writeText(text);
      toast("Post package copied for TikTok + Shorts", "i-file");
    } catch (e) {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      toast("Post package copied", "i-file");
    }
  });

  $$("#clipFilters .seg-btn").forEach((b) => b.addEventListener("click", () => {
    state.filter = b.dataset.filter;
    $$("#clipFilters .seg-btn").forEach((x) => {
      const on = x === b;
      x.classList.toggle("on", on);
      x.setAttribute("aria-selected", String(on));
    });
    renderAll();
  }));
  $("#clipSearch").addEventListener("input", (e) => {
    state.query = e.target.value.trim().toLowerCase();
    renderAll();
  });
  $("#topSearch").addEventListener("click", () => {
    go("clips");
    setTimeout(() => $("#clipSearch").focus(), 120);
  });

  /* ---------------- sidebar recent / queue ---------------- */
  function renderRecent() {
    const el = $(".side-recent");
    if (!el) return;
    el.innerHTML = "";
    const newLabel = document.createElement("div");
    newLabel.className = "nav-label";
    newLabel.innerHTML = `<span>Recent</span><span class="mono">${state.jobs.length ? String(state.jobs.length).padStart(2, "0") : "0"}</span>`;
    el.appendChild(newLabel);
    if (!state.jobs.length) {
      const empty = document.createElement("div");
      empty.className = "recent-item";
      empty.innerHTML = `<span class="rd"></span><span class="recent-name">No jobs yet</span>`;
      el.appendChild(empty);
      return;
    }
    state.jobs.slice(0, 4).forEach((j) => {
      const item = document.createElement("button");
      item.className = "recent-item";
      const dot = j.status === "done" ? "done" : j.status === "error" ? "err" : "run";
      item.innerHTML = `<span class="rd ${dot}"></span><span class="recent-name">${esc(j.label)}</span><span class="recent-n">${j.clips || ""}</span>`;
      item.addEventListener("click", () => {
        state.jobId = j.id;
        attachJob(j.id, j.label);
        go("studio");
      });
      el.appendChild(item);
    });
  }
  function renderQueue() {
    const card = $$(".rail .rc")[2];
    if (!card) return;
    card.querySelectorAll(".q").forEach((q) => q.remove());
    const jobs = state.jobs.slice(0, 3);
    if (!jobs.length) {
      const q = document.createElement("div");
      q.className = "q";
      q.innerHTML = `<div class="q-top"><span class="qd"></span><span class="q-name">No jobs yet</span></div><div class="q-sub">Paste a link to start</div>`;
      card.appendChild(q);
      return;
    }
    jobs.forEach((j) => {
      const q = document.createElement("div");
      q.className = "q";
      const label = j.status === "done" ? "done" : j.status === "error" ? "failed" : j.status;
      const dot = j.status === "done" ? "done" : j.status === "error" ? "err" : "run";
      q.innerHTML = `<div class="q-top"><span class="qd ${dot}"></span><span class="q-name">${esc(j.label)}</span><span class="q-time">now</span></div>
        <div class="q-sub">${esc(label)} · ${j.clips || 0} clips</div>`;
      card.appendChild(q);
    });
  }

  /* ---------------- agent chat ---------------- */
  function scrollThreadToBottom() {
    const thread = $("#thread");
    if (thread) thread.scrollTop = thread.scrollHeight;
  }
  function addUser(text) {
    const el = document.createElement("div");
    el.className = "msg user";
    el.innerHTML = `<div class="msg-main"><div class="msg-name"><span class="msg-time">now</span> You</div><div class="bubble">${esc(text)}</div></div>`;
    $("#thread").appendChild(el);
    scrollThreadToBottom();
  }
  function addTyping() {
    const el = document.createElement("div");
    el.className = "msg";
    el.innerHTML = `<div class="msg-a"><svg class="ico" aria-hidden="true"><use href="#i-bolt"/></svg></div><div class="msg-main"><div class="msg-name">NoetharaClipz Agent <span class="msg-time">now</span></div><div class="typing"><i></i><i></i><i></i></div></div>`;
    $("#thread").appendChild(el);
    scrollThreadToBottom();
    return el;
  }
  function addAgent(text, tools) {
    const el = document.createElement("div");
    el.className = "msg";
    const toolHTML = (tools || []).map((t) => `<span class="tool${t.ok ? " ok" : ""}">${esc(t.text)}</span>`).join("");
    el.innerHTML = `<div class="msg-a"><svg class="ico" aria-hidden="true"><use href="#i-bolt"/></svg></div>
      <div class="msg-main"><div class="msg-name">NoetharaClipz Agent <span class="msg-time">now</span></div>
      <div class="msg-text">${esc(text)}</div>${toolHTML ? `<div class="tools">${toolHTML}</div>` : ""}</div>`;
    $("#thread").appendChild(el);
    scrollThreadToBottom();
    return el;
  }
  async function sendChat() {
    if (state.chatBusy) return;
    const input = $("#chatInput");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    state.chatBusy = true;
    $("#chatSend").disabled = true;
    addUser(text);
    const typing = addTyping();
    try {
      const fd = new FormData();
      fd.append("message", text);
      if (state.jobId) fd.append("job_id", state.jobId);
      const r = await fetch("/api/chat", { method: "POST", body: fd });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Agent failed");
      typing.remove();
      // Only show job chips when the agent actually parsed/started a clipping job.
      // Conversational answers (questions, capability checks) should read like a
      // normal reply, not a fake status report.
      const isJob = d.parsed?.action === "clip" || d.parsed?.action === "rerender" || !!d.tool_result;
      const tools = isJob ? [{ text: d.parsed?.action === "rerender" ? "re-render" : "brief parsed", ok: true }] : [];
      if (isJob && d.parsed?.num_clips) tools.push({ text: `${d.parsed.num_clips} clips` });
      if (isJob && d.parsed?.vertical) tools.push({ text: "9:16" });
      if (isJob && d.parsed?.captions) tools.push({ text: "captions" });
      addAgent(d.reply || "Done.", tools);
      if (d.job_id && d.parsed?.action === "clip") {
        state.chatActiveJob = d.job_id;
        attachJob(d.job_id, d.parsed.goal || d.parsed.url || "Agent job");
        toast("Agent started a job", "i-bolt");
      }
    } catch (e) {
      typing.remove();
      addAgent(`Sorry — ${e.message}`);
    } finally {
      state.chatBusy = false;
      $("#chatSend").disabled = false;
    }
  }
  $("#chatSend").addEventListener("click", sendChat);
  $("#chatInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
  });
  $("#agentCaptions").addEventListener("click", (e) => {
    const b = e.currentTarget;
    b.classList.toggle("is-off");
    b.style.opacity = b.classList.contains("is-off") ? ".5" : "1";
    opt.captions = !b.classList.contains("is-off");
    $("#optCaptions").classList.toggle("is-off", !opt.captions);
    $("#optCaptions").setAttribute("aria-pressed", String(opt.captions));
    toast("Agent captions toggled", "i-cap");
  });
  function addJobMessageToChat(d) {
    state.chatActiveJob = null;
    const names = (d.clips || []).slice(0, 5).map((c) => c.title).join(" · ");
    addAgent(d.status === "error"
      ? `That job failed: ${d.error}`
      : `Done — ${(d.clips || []).length} clips ready. ${names}. Open Clips to preview and download.`,
      [{ text: d.status === "error" ? "failed" : "completed", ok: d.status !== "error" }, { text: `${(d.clips || []).length} clips` }]);
  }

  /* ---------------- editor ---------------- */
  const edVideo = $("#editorVideo");
  function fmtTC(sec) {
    if (!isFinite(sec) || sec < 0) return "00:00";
    return `${pad(Math.floor(sec / 60))}:${pad(Math.floor(sec % 60))}`;
  }
  function paintTime() {
    const d = edVideo.duration || 0;
    const t = edVideo.currentTime || 0;
    const p = d ? Math.max(0, Math.min(1, t / d)) : 0;
    $("#tcNow").textContent = fmtTC(t);
    $("#tcEnd").textContent = fmtTC(d);
    $("#tFill").style.width = `${p * 100}%`;
    $("#tKnob").style.left = `${p * 100}%`;
    $("#tlPlayhead").style.left = `${p * 100}%`;
  }
  function setPlayIcon(playing) {
    ["#playBtn", "#stagePlay"].forEach((sel) => {
      const btn = $(sel);
      const use = btn && btn.querySelector("use");
      if (use) use.setAttribute("href", playing ? "#i-pause" : "#i-play");
      if (btn) btn.setAttribute("aria-label", playing ? "Pause" : "Play");
    });
    $("#stagePlay").classList.toggle("hidden", playing);
  }
  function togglePlay() {
    if (!edVideo) return;
    if (!edVideo.paused) { edVideo.pause(); return; }
    const p = edVideo.play();
    if (p && p.catch) p.catch(() => {
      edVideo.muted = true;
      const use = $("#muteBtn").querySelector("use");
      if (use) use.setAttribute("href", "#i-mute");
      edVideo.play().catch(() => toast("Preview could not start", "i-film"));
    });
  }
  function setHookOverlay(text) {
    const frame = $("#stageFrame");
    if (!frame) return;
    let overlay = $("#hookOverlay");
    if (!overlay) {
      overlay = document.createElement("div");
      overlay.id = "hookOverlay";
      overlay.className = "hook-overlay";
      frame.appendChild(overlay);
    }
    overlay.textContent = text || "";
  }
  function renderHooks(c) {
    const root = $("#hookChoices");
    if (!root) return;
    const hooks = (c.hooks && c.hooks.length) ? c.hooks : [c.title || "Watch this"];
    root.innerHTML = hooks.map((h, i) => `<button type="button" class="hook-choice ${i === Number(c.hook_index || 0) ? "on" : ""}" data-hook="${i}">${esc(h)}</button>`).join("");
  }
  function renderEditorTracks(c) {
    const root = $("#editorTracks");
    if (!root) return;
    const tracks = c.tracks || [];
    const count = $("#trackCount");
    if (count) count.textContent = String(tracks.length);
    root.innerHTML = tracks.length ? tracks.map((t) => {
      const items = t.clips || [];
      const label = items.length ? (items[0].text || items[0].type || items[0].role || "clip") : "empty";
      return `<div class="editor-track-item"><b>${esc(t.kind || "track")}</b><span>${esc(String(label).slice(0, 42))}</span><span>${items.length}</span></div>`;
    }).join("") : `<div class="editor-track-item"><b>No tracks</b><span>Open a rendered clip</span></div>`;
  }
  function parseTimeValue(value) {
    const parts = String(value || "").split(":").map(Number);
    if (parts.some((x) => !Number.isFinite(x))) return null;
    return parts.reduce((acc, x) => acc * 60 + x, 0);
  }
  async function saveEditorState() {
    const c = state.clips[state.editorIndex];
    if (!c || !state.jobId) { toast("Open a rendered clip first", "i-lock"); return; }
    const btn = $("#editorSave");
    if (btn) btn.disabled = true;
    try {
      const tracks = JSON.parse(JSON.stringify(c.tracks || []));
      const textTrack = tracks.find((t) => t.kind === "text");
      if (textTrack?.clips?.[0]) textTrack.clips[0].text = (c.hooks || [])[Number(c.hook_index || 0)] || c.title || "";
      const format = $(".seg-sm .on[data-format]")?.dataset.format || "9x16";
      const config = JSON.parse(JSON.stringify(c.config || {}));
      config.aspect_ratio = format === "9x16" ? 0.5625 : format === "1x1" ? 1 : 16 / 9;
      config.captions = Object.assign({}, config.captions, {
        enabled: !!$("[data-switch='captions']")?.classList.contains("on"),
      });
      config.reframe = Object.assign({}, config.reframe, {
        guides: !!$("[data-switch='guides']")?.classList.contains("on"),
        punch: Number($("#punch").value || 1),
      });
      const r = await fetch(`/api/clips/${state.jobId}/${state.editorIndex}/editor`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tracks,
          config,
          trim_start: parseTimeValue($("#trimIn").value),
          trim_end: parseTimeValue($("#trimOut").value),
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Could not save editor project");
      c.tracks = d.tracks || tracks;
      c.config = d.config || config;
      state.editorDirty = false;
      renderEditorTracks(c);
      $("#editorState").textContent = "Saved";
      toast("Editor project saved to Supabase", "i-check");
    } catch (e) {
      toast(esc(e.message));
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  $("#editorSave")?.addEventListener("click", saveEditorState);
  $("#page-editor")?.addEventListener("input", () => { state.editorDirty = true; });
  $("#page-editor")?.addEventListener("click", (e) => {
    if (e.target.closest("button") && !e.target.closest("#editorSave")) state.editorDirty = true;
  });
  $("#hookChoices").addEventListener("click", (e) => {
    const b = e.target.closest("[data-hook]");
    const c = state.clips[state.editorIndex];
    if (!b || !c) return;
    c.hook_index = Number(b.dataset.hook);
    state.editorDirty = true;
    renderHooks(c);
    setHookOverlay((c.hooks || [])[c.hook_index] || c.title || "");
    toast("Hook selected — re-render to apply it", "i-cap");
  });

  // Build timeline filmstrip + caption chips from the real clip (no baked-in placeholders).
  function paintFilmstrip(c) {
    const frames = $$(".tl-film .ed-f0, .tl-film .ed-f1, .tl-film .ed-f2");
    const capsRoot = $("#tlCaps");
    const meta = $("#tlMeta");
    if (meta) meta.textContent = `${Number(c.end) - Number(c.start) || 0}s · ${c.vertical ? "9:16" : "source"}`;
    if (capsRoot) {
      const words = String(c.cap || (c.hooks || [])[Number(c.hook_index || 0)] || "")
        .split(/\s+/).filter(Boolean).slice(0, 4);
      capsRoot.innerHTML = words.map((w, i) =>
        `<span class="tl-cap" style="left:${4 + i * 24}%;width:${Math.min(22, w.length * 1.7 + 6)}%">${esc(w)}</span>`
      ).join("");
    }
    if (!c.url || !frames.length) return;
    // Try to sample real frames; if CORS blocks canvas, the neutral gradient stays.
    const probe = document.createElement("video");
    probe.muted = true;
    probe.playsInline = true;
    probe.preload = "auto";
    probe.crossOrigin = "anonymous";
    probe.src = c.url;
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    const count = frames.length;
    let done = 0;
    const grab = (idx) => new Promise((resolve) => {
      const seek = () => {
        const d = probe.duration || 0;
        if (!d) return resolve();
        probe.currentTime = Math.min(Math.max(0.1, d * (idx + 0.5) / count), d - 0.05);
      };
      probe.removeEventListener("seeked", onSeeked);
      const onSeeked = () => {
        try {
          canvas.width = 72; canvas.height = 128;
          ctx.drawImage(probe, 0, 0, canvas.width, canvas.height);
          frames[idx].style.backgroundImage = `url("${canvas.toDataURL("image/jpeg", 0.6)}")`;
        } catch (e) { /* CORS tainted canvas — keep neutral placeholder */ }
        resolve();
      };
      probe.addEventListener("seeked", onSeeked, { once: true });
      seek();
      setTimeout(resolve, 4000);
    });
    probe.addEventListener("loadeddata", async () => {
      for (let i = 0; i < count; i++) await grab(i);
      probe.removeAttribute("src");
      probe.load();
    }, { once: true });
    probe.addEventListener("error", () => { probe.removeAttribute("src"); }, { once: true });
  }

  function openEditor(i) {
    const c = state.clips[i];
    if (!c || c.status !== "ready") return;
    state.editorIndex = i;
    edVideo.pause();
    edVideo.src = c.url;
    edVideo.currentTime = 0;
    $("#editorTitle").textContent = c.title;
    $("#editorMeta").innerHTML = `<span class="dot"></span>Clip ${String(i + 1).padStart(2, "0")} · ${esc(c.platform)} · ${c.start}–${c.end}`;
    $("#edSource").textContent = `${c.platform} · local render`;
    $("#edRange").textContent = `${c.start}–${c.end}`;
    $("#edScore").textContent = c.score ? `${c.score} / 100` : "—";
    $("#editorState").textContent = "Ready";
    $("#stageSize").textContent = c.vertical ? "9:16 · source render" : "Source render";
    $("#stageFrame").classList.remove("f-1x1", "f-16x9");
    $$("[data-format]").forEach((x) => x.classList.toggle("on", x.dataset.format === "9x16"));
    $("#trimIn").value = "00:00.0";
    $("#trimOut").value = c.end;
    $$("[data-insp]").forEach((x) => {
      const on = x.dataset.insp === "edit";
      x.classList.toggle("on", on);
      x.setAttribute("aria-selected", String(on));
    });
    $$("[data-insp-panel]").forEach((p) => { p.hidden = p.dataset.inspPanel !== "edit"; });
    go("editor");
    renderHooks(c);
    renderEditorTracks(c);
    state.editorDirty = false;
    $("#editorState").textContent = "Ready";
    setHookOverlay((c.hooks || [])[Number(c.hook_index || 0)] || c.title || "");
    paintFilmstrip(c);
    paintTime();
  }
  if (edVideo) {
    edVideo.addEventListener("loadedmetadata", paintTime);
    edVideo.addEventListener("timeupdate", paintTime);
    edVideo.addEventListener("play", () => setPlayIcon(true));
    edVideo.addEventListener("pause", () => setPlayIcon(false));
    edVideo.addEventListener("ended", () => setPlayIcon(false));
    edVideo.addEventListener("click", togglePlay);
  }
  $("#playBtn").addEventListener("click", togglePlay);
  $("#stagePlay").addEventListener("click", (e) => { e.stopPropagation(); togglePlay(); });
  $("#muteBtn").addEventListener("click", () => {
    edVideo.muted = !edVideo.muted;
    const use = $("#muteBtn").querySelector("use");
    use.setAttribute("href", edVideo.muted ? "#i-mute" : "#i-volume");
    $("#muteBtn").setAttribute("aria-label", edVideo.muted ? "Unmute" : "Mute");
  });
  $("#fullBtn").addEventListener("click", () => {
    const el = $("#stageFrame");
    if (document.fullscreenElement) document.exitFullscreen();
    else if (el.requestFullscreen) el.requestFullscreen().catch(() => toast("Fullscreen is not available here", "i-expand"));
  });
  let edDragging = false;
  function seekFromX(clientX) {
    const r = $("#tTrack").getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    if (edVideo.duration) edVideo.currentTime = x * edVideo.duration;
    paintTime();
  }
  $("#tTrack").addEventListener("pointerdown", (e) => {
    edDragging = true;
    try { $("#tTrack").setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
    seekFromX(e.clientX);
  });
  $("#tTrack").addEventListener("pointermove", (e) => { if (edDragging) seekFromX(e.clientX); });
  $("#tTrack").addEventListener("pointerup", (e) => {
    edDragging = false;
    try { $("#tTrack").releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ }
  });
  $("#tlTracks").addEventListener("click", (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    if (edVideo.duration) edVideo.currentTime = x * edVideo.duration;
    paintTime();
  });
  $$("[data-insp]").forEach((b) => b.addEventListener("click", () => {
    const k = b.dataset.insp;
    $$("[data-insp]").forEach((x) => {
      const on = x === b;
      x.classList.toggle("on", on);
      x.setAttribute("aria-selected", String(on));
    });
    $$("[data-insp-panel]").forEach((p) => { p.hidden = p.dataset.inspPanel !== k; });
    $("#inspBody").scrollTop = 0;
  }));
  $$("[data-format]").forEach((b) => b.addEventListener("click", () => {
    const f = b.dataset.format;
    $$("[data-format]").forEach((x) => x.classList.toggle("on", x === b));
    const frame = $("#stageFrame");
    frame.classList.remove("f-1x1", "f-16x9");
    if (f === "1x1") frame.classList.add("f-1x1");
    if (f === "16x9") frame.classList.add("f-16x9");
    $("#stageSize").textContent = f === "9x16" ? "9:16 · source render" : f === "1x1" ? "1:1 · preview" : "16:9 · preview";
    toast(`Preview format · ${f.replace("x", ":")}`, "i-crop");
  }));
  $$("[data-switch]").forEach((b) => b.addEventListener("click", () => {
    const on = !b.classList.contains("on");
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", String(on));
    if (b.dataset.switch === "guides") $("#stageFrame").classList.toggle("guides", on);
  }));
  $("#trimReset").addEventListener("click", () => {
    const c = state.clips[state.editorIndex];
    $("#trimIn").value = "00:00.0";
    $("#trimOut").value = c ? c.end : "00:29";
    toast("Trim reset to the full clip", "i-scissors");
  });
  $$("[data-capstyle]").forEach((b) => b.addEventListener("click", () => {
    $("#capStyleV").textContent = b.dataset.capstyle;
    $$("[data-capstyle]").forEach((x) => x.setAttribute("aria-checked", String(x === b)));
    closeMenus();
    toast(`Caption style · <b>${b.dataset.capstyle}</b>`, "i-cap");
  }));
  $$("[data-swatch]").forEach((b) => b.addEventListener("click", () => {
    $$("[data-swatch]").forEach((x) => x.classList.toggle("on", x === b));
  }));
  $("#punch").addEventListener("input", (e) => {
    $("#punchV").textContent = `${Number(e.target.value).toFixed(2).replace(/0$/, "")}×`;
  });
  $$("[data-ai-chip]").forEach((b) => b.addEventListener("click", () => {
    $("#aiPrompt").value = b.dataset.aiChip;
    $("#aiPrompt").focus();
  }));
  $("#aiRun").addEventListener("click", () => {
    const q = $("#aiPrompt").value.trim();
    if (!q) { toast("Add a note or pick a suggestion", "i-spark"); return; }
    go("agent");
    $("#chatInput").value = q;
    sendChat();
  });
  $$("[data-apply]").forEach((b) => b.addEventListener("click", () => {
    toast(`Editor controls are preview-only · ${b.dataset.apply}`, "i-lock");
  }));

  async function rerenderCurrentJob(indices = null) {
    if (!state.jobId) { toast("Run a job first", "i-lock"); return; }
    const targets = (indices && indices.length) ? indices : (state.editorIndex >= 0 ? [state.editorIndex] : null);
    try {
      let d;
      if (targets) {
        const hooks = {};
        targets.forEach((i) => { hooks[String(i)] = Number(state.clips[i]?.hook_index || 0); });
        const r = await fetch(`/api/render-selected/${state.jobId}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            indices: targets,
            captions: opt.captions,
            vertical: opt.vertical,
            draft: false,
            hooks,
          }),
        });
        d = await r.json();
        if (!r.ok) throw new Error(d.detail || "Re-render failed");
      } else {
        const p = new URLSearchParams({ captions: String(opt.captions), vertical: String(opt.vertical) });
        const r = await fetch(`/api/render/${state.jobId}?${p}`, { method: "POST" });
        d = await r.json();
        if (!r.ok) throw new Error(d.detail || "Re-render failed");
      }
      toast("Re-render started", "i-refresh");
      const label = state.jobs.find((j) => j.id === state.jobId)?.label || "Re-render";
      attachJob(state.jobId, label);
    } catch (e) {
      toast(esc(e.message));
    }
  }
  $(".insp-foot .btn-primary").addEventListener("click", () => rerenderCurrentJob());
  $(".editor-actions .btn-primary").addEventListener("click", () => {
    const c = state.clips[state.editorIndex];
    if (!c) { toast("Open a clip first", "i-lock"); return; }
    const a = document.createElement("a");
    a.href = c.url;
    a.download = `clip_${c._idx + 1}.mp4`;
    a.click();
  });

  /* ---------------- clip action menu + delegated events ---------------- */
  const clipMenu = $("#clipMenu");
  function closeClipMenu() { if (clipMenu) clipMenu.hidden = true; }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-clip-menu]");
    if (btn) {
      e.preventDefault();
      e.stopPropagation();
      clipMenu.dataset.i = btn.dataset.clipMenu;
      clipMenu.hidden = false;
      closeMenus();
      const r = btn.getBoundingClientRect();
      const mw = clipMenu.offsetWidth;
      const mh = clipMenu.offsetHeight;
      const left = Math.min(window.innerWidth - mw - 8, Math.max(8, r.right - mw));
      let top = r.bottom + 5;
      if (top + mh > window.innerHeight - 8) top = r.top - mh - 5;
      clipMenu.style.left = `${left}px`;
      clipMenu.style.top = `${top}px`;
      return;
    }
    if (!e.target.closest("#clipMenu")) closeClipMenu();
  });
  async function saveClipNote(i, note) {
    if (!state.jobId) { toast("Run a job first", "i-lock"); return; }
    try {
      const r = await fetch(`/api/clips/${state.jobId}/${i}/note`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "Could not save note");
      state.clips[i].note = note;
      renderStudio();
      renderAll();
      toast(note === "keep" ? "Marked as kept" : note === "flop" ? "Marked as flopped" : "Note saved", "i-check");
    } catch (e) { toast(esc(e.message)); }
  }
  $$("[data-clip-action]").forEach((b) => b.addEventListener("click", async () => {
    const i = Number(clipMenu.dataset.i);
    const c = state.clips[i];
    const action = b.dataset.clipAction;
    closeClipMenu();
    if (!c) return;
    if (action === "open") openEditor(i);
    else if (action === "download") {
      const a = document.createElement("a");
      a.href = c.url;
      a.download = `clip_${i + 1}.mp4`;
      a.click();
    } else if (action === "rerender") rerenderCurrentJob([i]);
    else if (action === "retry") {
      try {
        const r = await fetch(`/api/retry/${state.jobId}/${i}`, { method: "POST" });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || "Retry failed");
        toast("Retrying this section with a fresh source URL", "i-refresh");
        attachJob(state.jobId, state.jobs.find((j) => j.id === state.jobId)?.label || "Retry");
      } catch (e) { toast(esc(e.message)); }
    }
    else if (action === "keep") saveClipNote(i, "keep");
    else if (action === "flop") saveClipNote(i, "flop");
    else toast("Delete is not connected to the local renderer", "i-lock");
  }));
  window.addEventListener("scroll", closeClipMenu, true);
  document.addEventListener("click", (e) => {
    const ed = e.target.closest("[data-edit]");
    if (ed) { e.preventDefault(); openEditor(Number(ed.dataset.edit)); return; }
    const t = e.target.closest("[data-toast]");
    if (t) { toast(t.dataset.toast, "i-check"); }
  });
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      go("clips");
      setTimeout(() => $("#clipSearch").focus(), 120);
      return;
    }
    if (e.key === "Escape") {
      closeClipMenu();
      $("#app").classList.remove("nav-open");
      return;
    }
    const tag = String(e.target?.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || e.target?.isContentEditable || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "1") { go("studio"); return; }
    if (e.key === "2") { go("agent"); return; }
    if (e.key === "3") { go("clips"); return; }
    if (!$("#page-editor").hidden) {
      const k = e.key.toLowerCase();
      if (k === "j") { e.preventDefault(); edVideo.currentTime = Math.max(0, edVideo.currentTime - 5); }
      else if (k === "l") { e.preventDefault(); edVideo.currentTime = Math.min(edVideo.duration || 0, edVideo.currentTime + 5); }
      else if (k === "k") { e.preventDefault(); togglePlay(); }
    }
  });

  /* ---------------- ASR status ---------------- */
  async function checkAsr() {
    try {
      const r = await fetch("/api/asr/status");
      const d = await r.json();
      const chip = $$(".top-right .chip")[0];
      if (!chip) return;
      if (d.active === "groq") {
        chip.innerHTML = `<span class="dot"></span>Groq turbo`;
        chip.title = `Groq ${d.groq_model} · ${d.max_workers} parallel ${d.chunk_seconds}s chunks`;
      } else {
        chip.innerHTML = `<span class="dot"></span>Local Whisper`;
        chip.title = "Set GROQ_API_KEY in .env for fast ASR";
      }
    } catch (e) { /* ignore */ }
  }

  /* ---------------- init ---------------- */
  Promise.resolve(window.ncAuthReady).then(function () {
    setReadyRunCard();
    resetStats();
    renderRecent();
    renderQueue();
    renderStudio();
    renderAll();
    checkAsr();
    loadLibrary();
    loadLiveSessions();
    loadPublications();

    (function restoreJob() {
      const shared = new URLSearchParams(location.search).get("job");
      let id = shared || null;
      if (!id) {
        try { id = localStorage.getItem("nc_job") || localStorage.getItem("cf_job"); } catch (e) { /* ignore */ }
      }
      if (!id) return;
      attachJob(id, shared ? "Shared job" : "Previous job");
    })();
  });
})();
