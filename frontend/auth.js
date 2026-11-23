(function () {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const form = $("#authForm");
  const msg = $("#authMsg");
  const submit = $("#submitBtn");
  const switchBtn = $("#switchBtn");
  const title = $("#authTitle");
  const sub = $("#authSub");
  const params = new URLSearchParams(location.search);
  let mode = params.get("mode") === "signup" ? "signup" : "login";
  let cfg = null;

  function show(text, kind) {
    msg.textContent = text;
    msg.className = `pub-auth-msg show ${kind || "err"}`;
  }
  function clear() { msg.className = "pub-auth-msg"; msg.textContent = ""; }
  function paintMode() {
    const up = mode === "signup";
    title.textContent = up ? "Create your workspace." : "Welcome to NoetharaClipz.";
    sub.textContent = up ? "Find the moments your audience wants to rewatch." : "Your studio is one sign-in away.";
    submit.textContent = up ? "Create account" : "Login";
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
    switchBtn.addEventListener("click", () => { mode = mode === "login" ? "signup" : "login"; paintMode(); });
    $("#forgotLink").addEventListener("click", async (e) => {
      e.preventDefault();
      const email = $("#email").value.trim();
      if (!email) { show("Enter your email first.", "err"); return; }
      try {
        await api("/auth/v1/recover", { email });
        show("Password reset email sent. Check your inbox.", "ok");
      } catch (err) { show(err.message, "err"); }
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
      submit.disabled = true;
      submit.textContent = "Please wait…";
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
        submit.textContent = mode === "signup" ? "Create account" : "Login";
      }
    });
  }
  init();
})();
