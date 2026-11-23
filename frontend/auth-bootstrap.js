(function () {
  "use strict";
  let usingLocalStorage = true;
  const store = {
    get() {
      const localRaw = localStorage.getItem("nc_session");
      const sessionRaw = sessionStorage.getItem("nc_session");
      usingLocalStorage = !!localRaw || !sessionRaw;
      const raw = localRaw || sessionRaw;
      try { return JSON.parse(raw || "null"); } catch (e) { return null; }
    },
    save(data) {
      const expiresAt = data.expires_at || (Math.floor(Date.now() / 1000) + (data.expires_in || 3600));
      const session = { access_token: data.access_token, refresh_token: data.refresh_token, expires_at: expiresAt, user: data.user || null };
      const target = usingLocalStorage ? localStorage : sessionStorage;
      const other = usingLocalStorage ? sessionStorage : localStorage;
      target.setItem("nc_session", JSON.stringify(session));
      target.setItem("nc_access_token", data.access_token);
      other.removeItem("nc_session");
      other.removeItem("nc_access_token");
      return session;
    },
    clear() {
      localStorage.removeItem("nc_session");
      localStorage.removeItem("nc_access_token");
      sessionStorage.removeItem("nc_session");
      sessionStorage.removeItem("nc_access_token");
    },
  };

  function redirectToAuth() {
    const back = encodeURIComponent(location.pathname + location.search);
    location.replace(`/auth?returnTo=${back}`);
  }

  async function exchange(url, key, body) {
    const r = await fetch(`${url}${body.path}`, {
      method: "POST",
      headers: { "apikey": key, "Content-Type": "application/json" },
      body: JSON.stringify(body.payload),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.msg || data.message || "Session refresh failed");
    return data;
  }

  window.ncAuthReady = (async function () {
    let cfg;
    try { cfg = await fetch("/api/public-config").then((r) => r.json()); }
    catch (e) { return false; }
    if (!cfg.enabled) return false;

    let session = store.get();
    if (!session?.access_token) { redirectToAuth(); return false; }
    const expired = !session.expires_at || Number(session.expires_at) < Math.floor(Date.now() / 1000) - 30;
    if (expired && session.refresh_token) {
      try {
        const data = await exchange(cfg.supabaseUrl, cfg.anonKey, {
          path: "/auth/v1/token?grant_type=refresh_token",
          payload: { refresh_token: session.refresh_token },
        });
        session = store.save(data);
      } catch (e) { store.clear(); redirectToAuth(); return false; }
    }

    const verified = await fetch("/api/auth/verify", {
      method: "POST",
      headers: { "Authorization": `Bearer ${session.access_token}` },
    }).then(async (r) => r.ok ? r.json() : null).catch(() => null);
    if (!verified?.ok) { store.clear(); redirectToAuth(); return false; }

    window.ncUser = verified.user;
    window.ncAccess = session.access_token;
    window.ncSignOut = async function () {
      try {
        await fetch(`${cfg.supabaseUrl}/auth/v1/logout`, {
          method: "POST",
          headers: { "apikey": cfg.anonKey, "Authorization": `Bearer ${session.access_token}` },
        });
      } catch (e) { /* ignore */ }
      store.clear();
      location.replace("/auth");
    };

    const name = (verified.user.email || "creator").split("@")[0];
    const initial = name.slice(0, 1).toUpperCase();
    document.querySelectorAll(".avatar").forEach((el) => {
      el.textContent = initial;
      el.title = `Signed in as ${verified.user.email}`;
    });
    document.querySelectorAll(".user-chip .u-av, .menu-user .u-av").forEach((el) => { el.textContent = initial; });
    document.querySelectorAll(".user-chip .u-name, .menu-user b").forEach((el) => { el.textContent = name; });
    document.querySelectorAll(".user-chip .u-sub, .menu-user small").forEach((el) => { el.textContent = verified.user.email; });
    const greeting = document.querySelector("#greeting");
    if (greeting) greeting.textContent = `Good to see you, ${name}.`;
    return true;
  })();
})();
