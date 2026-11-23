(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const form = $("#authForm");
  const msg = $("#authMsg");
  const submit = $("#submitBtn");
  const switchBtn = $("#switchBtn");
  const title = $("#authTitle");
  const sub = $("#authSub");
  const tabLogin = $("#tabLogin");
  const tabSignup = $("#tabSignup");
  const pwToggle = $("#pwToggle");
  const params = new URLSearchParams(location.search);
  let mode = params.get("mode") === "signup" ? "signup" : "login";
  let cfg = null;

  function show(text, kind) {
    msg.textContent = text;
    msg.className = `auth-msg show ${kind || "err"}`;
  }
  function clear() { msg.className = "auth-msg"; msg.textContent = ""; }

  function setMode(next) {
    mode = next;
    const url = new URL(location.href);
    if (mode === "signup") url.searchParams.set("mode", "signup");
    else url.searchParams.delete("mode");
    history.replaceState(null, "", url);
    paintMode();
  }

  function paintMode() {
    const up = mode === "signup";
    if (tabLogin) {
      tabLogin.classList.toggle("active", !up);
      tabLogin.setAttribute("aria-selected", String(!up));
    }
    if (tabSignup) {
      tabSignup.classList.toggle("active", up);
      tabSignup.setAttribute("aria-selected", String(up));
    }
    title.textContent = up ? "Create your studio." : "Welcome back.";
    sub.textContent = up
      ? "Start with a VOD. Leave with a queue of clips."
      : "Sign in to open your clipping studio.";
    submit.textContent = up ? "Create account" : "Sign in";
    switchBtn.textContent = up ? "Sign in instead" : "Create an account";
    document.title = up ? "Create account — NoetharaClipz" : "Sign in — NoetharaClipz";
    $("#password").autocomplete = up ? "new-password" : "current-password";
    clear();
  }

  function preferredStore() {
    const remember = $("#remember");
    return remember && !remember.checked ? sessionStorage : localStorage;
  }
  function saveSession(data) {
    const expiresAt = data.expires_at || (Math.floor(Date.now() / 1000) + (data.expires_in || 3600));
    const session = {
      access_token: data.access_token,
      refresh_token: data.refresh_token,
      expires_at: expiresAt,
      user: data.user || null,
    };
    const store = preferredStore();
    const other = store === localStorage ? sessionStorage : localStorage;
    store.setItem("nc_session", JSON.stringify(session));
    store.setItem("nc_access_token", data.access_token);
    other.removeItem("nc_session");
    other.removeItem("nc_access_token");
  }
  async function api(path, body) {
    const r = await fetch(`${cfg.supabaseUrl}${path}`, {
      method: "POST",
      headers: { "apikey": cfg.anonKey, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.msg || data.message || data.error_description || "Request failed");
    return data;
  }
  async function refreshIfPossible() {
    let session;
    const raw = localStorage.getItem("nc_session") || sessionStorage.getItem("nc_session");
    try { session = JSON.parse(raw || "null"); } catch (e) { return false; }
    if (!session?.refresh_token) return false;
    try {
      const data = await api("/auth/v1/token?grant_type=refresh_token", { refresh_token: session.refresh_token });
      saveSession(data);
      return true;
    } catch (e) { return false; }
  }

  async function init() {
    try {
      const r = await fetch("/api/public-config");
      cfg = await r.json();
    } catch (e) {
      show("Could not load authentication config. Refresh the page.", "err");
      return;
    }
    paintMode();

    if (tabLogin) tabLogin.addEventListener("click", () => setMode("login"));
    if (tabSignup) tabSignup.addEventListener("click", () => setMode("signup"));
    if (switchBtn) switchBtn.addEventListener("click", () => setMode(mode === "login" ? "signup" : "login"));

    if (pwToggle) {
      pwToggle.addEventListener("click", () => {
        const visible = $("#password").type === "text";
        $("#password").type = visible ? "password" : "text";
        pwToggle.setAttribute("aria-pressed", String(!visible));
        pwToggle.setAttribute("aria-label", visible ? "Show password" : "Hide password");
      });
    }

    const forgot = $("#forgotLink");
    if (forgot) forgot.addEventListener("click", async (e) => {
      e.preventDefault();
      const email = $("#email").value.trim();
      if (!email) { show("Enter your email first.", "err"); return; }
      try {
        await api("/auth/v1/recover", { email });
        show("Password reset email sent. Check your inbox.", "ok");
      } catch (err) { show(err.message, "err"); }
    });

    const video = $(".av-phone video");
    if (video) document.addEventListener("visibilitychange", () => {
      if (document.hidden) video.pause();
      else video.play().catch(() => {});
    });

    const existingToken = localStorage.getItem("nc_access_token") || sessionStorage.getItem("nc_access_token");
    if (existingToken) {
      const ok = await fetch("/api/auth/verify", {
        method: "POST",
        headers: { "Authorization": `Bearer ${existingToken}` },
      }).then((r) => r.ok).catch(() => false);
      if (ok || await refreshIfPossible()) {
        location.replace(params.get("returnTo") || "/app");
        return;
      }
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      clear();
      if (!$("#email").value.trim()) { show("Enter your email address.", "err"); return; }
      if ($("#password").value.length < 6) { show("Password needs at least 6 characters.", "err"); return; }
      submit.disabled = true;
      submit.textContent = mode === "signup" ? "Creating…" : "Signing in…";
      const email = $("#email").value.trim();
      const password = $("#password").value;
      try {
        let data;
        if (mode === "login") {
          data = await api("/auth/v1/token?grant_type=password", { email, password });
        } else {
          const createdRes = await fetch("/api/auth/signup", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email, password }),
          });
          const created = await createdRes.json().catch(() => ({}));
          if (!createdRes.ok) throw new Error(created.detail || created.error || "Could not create account");
          data = await api("/auth/v1/token?grant_type=password", { email, password });
        }
        if (!data.access_token) throw new Error("Sign in complete, but no session was returned.");
        saveSession(data);
        location.replace(params.get("returnTo") || "/app");
      } catch (err) {
        show(err.message, "err");
      } finally {
        submit.disabled = false;
        submit.textContent = mode === "signup" ? "Create account" : "Sign in";
      }
    });
  }
  init();
})();
