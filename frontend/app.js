const $ = (id) => document.getElementById(id);
const state = {
  mode: 'link',
  file: null,
  source: null,
  jobId: null,
  pollTimer: null,
  startedAt: 0,
  jobs: [],
  chatBusy: false,
};

function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), 3000);
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmt(sec) {
  sec = Number(sec) || 0;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` : `${m}:${String(s).padStart(2,'0')}`;
}
function looksLikeUrl(s) { return /^https?:\/\/\S+$/i.test((s || '').trim()); }

/* ---------------- Views ---------------- */
const VIEW_NAMES = { studio: 'Studio', agent: 'Agent', clips: 'Clips' };
document.querySelectorAll('.nav-item').forEach((btn) => {
  btn.addEventListener('click', () => switchView(btn.dataset.view));
});
function switchView(name) {
  document.querySelectorAll('.nav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === name));
  document.querySelectorAll('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
  $('crumbView').textContent = VIEW_NAMES[name] || name;
  if (name === 'clips') renderAllClips();
}

/* ---------------- Modes ---------------- */
document.querySelectorAll('.mode-tab').forEach((tab) => {
  tab.addEventListener('click', () => setMode(tab.dataset.mode));
});
function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll('.mode-tab').forEach((t) => t.classList.toggle('active', t.dataset.mode === mode));
  ['link', 'upload', 'agent'].forEach((m) => $('panel-' + m).classList.toggle('hidden', m !== mode));
  $('createBtn').innerHTML = mode === 'agent' ? 'Send to agent <span class="arrow">→</span>' : 'Generate clips <span class="arrow">→</span>';
  $('runNote').textContent = mode === 'link' ? 'Link mode · no upload needed'
    : mode === 'upload' ? 'Local file · stays on your machine'
    : 'Agent mode · natural language request';
}
$('openAgentBtn').addEventListener('click', () => switchView('agent'));
$('resetBtn').addEventListener('click', resetAll);
$('newJobBtn').addEventListener('click', resetAll);
function resetAll() {
  stopPolling();
  state.file = null; state.source = null; state.jobId = null;
  $('sourceCard').classList.add('hidden');
  $('results').classList.add('hidden');
  $('progressCard').classList.add('hidden');
  $('filePill').classList.add('hidden');
  $('urlInput').value = ''; $('goalInput').value = ''; $('goalInput2').value = ''; $('agentInput').value = '';
  $('errorBox').classList.add('hidden');
  setMode('link');
}

/* ---------------- Suggestions ---------------- */
document.querySelectorAll('.suggest').forEach((b) => {
  b.addEventListener('click', () => {
    const val = b.dataset.fill;
    if (looksLikeUrl(val)) {
      setMode('link'); $('urlInput').value = val; previewUrl();
    } else {
      switchView('agent'); $('chatInput').value = val; $('chatInput').focus();
    }
  });
});

/* ---------------- Command bar ---------------- */
$('commandSend').addEventListener('click', runCommand);
$('commandInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') runCommand(); });
async function runCommand() {
  const val = $('commandInput').value.trim();
  if (!val) { toast('Paste a link or describe what you want.'); return; }
  if (looksLikeUrl(val)) {
    setMode('link');
    $('urlInput').value = val;
    await previewUrl();
    $('createBtn').focus();
  } else {
    switchView('agent');
    $('chatInput').value = val;
    sendChat();
  }
}

/* ---------------- URL preview ---------------- */
$('previewBtn').addEventListener('click', previewUrl);
$('urlInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') previewUrl(); });
async function previewUrl() {
  const url = $('urlInput').value.trim();
  if (!looksLikeUrl(url)) { toast('Enter a valid http(s) link.'); return; }
  $('previewBtn').disabled = true;
  $('previewBtn').textContent = 'Reading…';
  try {
    const res = await fetch('/api/source', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ url }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || 'Could not read link');
    state.source = d;
    $('sourceThumb').src = d.thumbnail || '';
    $('sourceThumb').style.display = d.thumbnail ? '' : 'none';
    $('sourcePlatform').textContent = (d.platform || 'LINK').toUpperCase();
    $('sourceLive').textContent = d.is_live ? 'LIVE' : (d.was_live ? 'VOD' : 'VIDEO');
    $('sourceTitle').textContent = d.title;
    const captionNote = (d.subtitles?.length || d.auto_captions?.length) ? ' · captions available' : '';
    $('sourceMeta').textContent = `${d.uploader || 'unknown'} · ${fmt(d.duration)}${captionNote}`;
    $('sourceCard').classList.remove('hidden');
    $('runNote').textContent = captionNote ? 'Captions found · instant analysis' : 'Audio-only download · local transcription';
  } catch (e) {
    toast(e.message);
    state.source = null;
    $('sourceCard').classList.add('hidden');
  } finally {
    $('previewBtn').disabled = false;
    $('previewBtn').textContent = 'Preview';
  }
}
$('changeSourceBtn').addEventListener('click', () => { $('sourceCard').classList.add('hidden'); $('urlInput').focus(); });

/* ---------------- Upload ---------------- */
const dropzone = $('dropzone');
dropzone.addEventListener('click', () => $('fileInput').click());
['dragenter','dragover'].forEach((e) => dropzone.addEventListener(e, (ev) => { ev.preventDefault(); dropzone.classList.add('drag'); }));
['dragleave','drop'].forEach((e) => dropzone.addEventListener(e, (ev) => { ev.preventDefault(); dropzone.classList.remove('drag'); }));
dropzone.addEventListener('drop', (ev) => { if (ev.dataTransfer.files[0]) setFile(ev.dataTransfer.files[0]); });
$('fileInput').addEventListener('change', () => { if ($('fileInput').files[0]) setFile($('fileInput').files[0]); });
function setFile(f) {
  if (!f.type.startsWith('video/') && !/\.(mp4|mov|webm|mkv|avi)$/i.test(f.name)) { toast('Please choose a video file.'); return; }
  state.file = f;
  $('filePill').textContent = `◉ ${f.name}  ·  ${(f.size / 1048576).toFixed(1)} MB`;
  $('filePill').classList.remove('hidden');
}

/* ---------------- Start job ---------------- */
$('createBtn').addEventListener('click', async () => {
  if (state.mode === 'agent') {
    const text = $('agentInput').value.trim();
    if (!text) { toast('Describe what you want.'); return; }
    switchView('agent'); $('chatInput').value = text; sendChat(); return;
  }
  if (state.mode === 'upload') {
    if (!state.file) { toast('Choose a video first.'); return; }
    await startUpload();
  } else {
    let url = $('urlInput').value.trim();
    if (!looksLikeUrl(url)) { toast('Paste a valid link.'); return; }
    if (!state.source || state.source.webpage_url !== url) {
      $('urlInput').value = url;
      await previewUrl();
      if (!state.source) return;
    }
    await startUrl(url);
  }
});

function options() {
  return {
    num_clips: $('numClips').value,
    clip_length: $('clipLength').value,
    captions: $('captions').checked,
    vertical: $('vertical').checked,
  };
}
function goalText() { return (state.mode === 'upload' ? $('goalInput2').value : $('goalInput').value).trim(); }

async function startUrl(url) {
  const o = options();
  $('results').classList.add('hidden');
  const fd = new FormData();
  fd.append('url', url);
  fd.append('num_clips', o.num_clips); fd.append('clip_length', o.clip_length);
  fd.append('captions', o.captions); fd.append('vertical', o.vertical);
  fd.append('goal', goalText());
  try {
    const res = await fetch('/api/clip-url', { method: 'POST', body: fd });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || 'Failed to start');
    beginJob(d.job_id, state.source?.title || url);
  } catch (e) { toast(e.message); }
}
async function startUpload() {
  const o = options();
  const fd = new FormData();
  fd.append('file', state.file);
  fd.append('num_clips', o.num_clips); fd.append('clip_length', o.clip_length);
  fd.append('captions', o.captions); fd.append('vertical', o.vertical);
  fd.append('goal', goalText());
  $('createBtn').disabled = true; $('createBtn').textContent = 'Uploading…';
  try {
    const res = await fetch('/api/upload', { method: 'POST', body: fd });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || 'Failed to start');
    beginJob(d.job_id, state.file.name);
  } catch (e) { toast(e.message); }
  finally { $('createBtn').disabled = false; $('createBtn').innerHTML = 'Generate clips <span class="arrow">→</span>'; }
}

function beginJob(jobId, label) {
  state.jobId = jobId;
  state.startedAt = Date.now();
  state.jobs.unshift({ id: jobId, label, status: 'queued', clips: 0 });
  renderRecent();
  $('progressCard').classList.remove('hidden');
  $('results').classList.add('hidden');
  $('progressCard').scrollIntoView({behavior:'smooth', block:'center'});
  startPolling();
}

/* ---------------- Polling ---------------- */
function startPolling() { stopPolling(); state.pollTimer = setInterval(poll, 1400); poll(); }
function stopPolling() { if (state.pollTimer) clearInterval(state.pollTimer); state.pollTimer = null; }

async function poll() {
  if (!state.jobId) return;
  try {
    const res = await fetch(`/api/status/${state.jobId}`);
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || 'Status failed');

    $('progressPct').textContent = `${d.progress}%`;
    $('progressFill').style.width = `${d.progress}%`;
    $('progressTitle').textContent = d.message || 'Working…';
    $('progressDetail').textContent = d.stage || 'preparing';
    const el = Math.floor((Date.now() - state.startedAt) / 1000);
    $('elapsed').textContent = `${Math.floor(el/60)}:${String(el%60).padStart(2,'0')}`;

    const order = ['resolve','audio','transcribe','analyze','render'];
    const idx = order.indexOf(d.stage);
    document.querySelectorAll('.task-row').forEach((row) => {
      const si = order.indexOf(row.dataset.stage);
      row.classList.toggle('active', si === idx && d.status === 'processing');
      row.classList.toggle('done', si < idx || d.status === 'done');
      row.querySelector('.task-state').textContent = si < idx || d.status === 'done' ? 'done' : (si === idx && d.status === 'processing' ? 'running' : 'pending');
    });

    const job = state.jobs.find((j) => j.id === d.id);
    if (job) { job.status = d.status; job.clips = d.clips.length; }

    if (d.status === 'done' || d.status === 'error') {
      stopPolling();
      renderRecent();
      renderResults(d);
      if (state.chatActiveJob === d.id) addJobMessageToChat(d);
    }
  } catch (e) { stopPolling(); toast(e.message); }
}

/* ---------------- Results ---------------- */
function renderResults(d) {
  $('progressCard').classList.add('hidden');
  $('results').classList.remove('hidden');
  const grid = $('clipsGrid');
  grid.innerHTML = '';
  const errBox = $('errorBox');
  errBox.classList.add('hidden');

  if (d.status === 'error') {
    errBox.textContent = d.error || 'Something went wrong.';
    errBox.classList.remove('hidden');
    return;
  }
  const src = d.source_info?.title || 'your video';
  $('resultsMeta').textContent = `${d.clips.length} clips · ${fmt(d.duration)} source · ${d.transcript_source === 'subtitles' ? 'source captions' : (d.language || 'auto')} language · ${src}`;
  $('reCaptions').checked = d.settings?.captions ?? true;
  $('reVertical').checked = d.settings?.vertical ?? true;

  d.clips.forEach((c, i) => grid.appendChild(clipCard(c, i)));
  renderAllClips();
}
function clipCard(c, i) {
  const div = document.createElement('div');
  div.className = 'clip-card' + (c.render_status === 'error' ? ' error-card' : '');
  if (c.render_status === 'error') {
    div.innerHTML = `<div class="clip-body"><div class="clip-top"><span class="clip-num">CLIP ${String(i+1).padStart(2,'0')}</span></div><p class="clip-title">${esc(c.title)}</p><div class="clip-error">${esc((c.error||'').slice(0,140))}</div></div>`;
  } else {
    div.innerHTML = `
      <video class="clip-video" src="${c.url}" controls playsinline preload="metadata"></video>
      <div class="clip-body">
        <div class="clip-top"><span class="clip-num">CLIP ${String(i+1).padStart(2,'0')} · ${fmt(c.start)}–${fmt(c.end)}</span><span class="clip-score">★ ${esc(c.score ?? 7)}</span></div>
        <p class="clip-title">${esc(c.title)}</p>
        <p class="clip-reason">${esc(c.reason || '')}</p>
        <div class="clip-foot">${c.captions ? 'CAPTIONS' : 'NO CAPTIONS'} · ${c.vertical !== false ? '9:16' : 'SOURCE'}</div>
        <a class="btn btn-secondary btn-sm" href="${c.url}" download="clip_${i+1}.mp4">Download ↓</a>
      </div>`;
  }
  return div;
}
function renderAllClips() {
  const grid = $('allClipsGrid');
  if (!grid) return;
  grid.innerHTML = '';
  let count = 0;
  state.jobs.forEach((j) => {
    // results are fetched live from status when available
  });
  const lastGrid = $('clipsGrid');
  if (lastGrid && lastGrid.children.length) {
    Array.from(lastGrid.children).forEach((c) => grid.appendChild(c.cloneNode(true)));
    count = lastGrid.children.length;
  }
  $('clipsEmpty').classList.toggle('hidden', count > 0);
  $('navClipCount').textContent = count;
}

$('reRenderToggle').addEventListener('click', () => $('rerenderBar').classList.toggle('hidden'));
$('reRenderBtn').addEventListener('click', async () => {
  if (!state.jobId) return;
  const btn = $('reRenderBtn'); btn.disabled = true; btn.textContent = 'Working…';
  const p = new URLSearchParams({ captions: $('reCaptions').checked, vertical: $('reVertical').checked });
  try {
    await fetch(`/api/render/${state.jobId}?${p}`, {method:'POST'});
    $('progressCard').classList.remove('hidden'); $('results').classList.add('hidden');
    state.startedAt = Date.now(); startPolling();
  } catch (e) { toast(e.message); }
  finally { btn.disabled = false; btn.textContent = 'Re-render'; }
});

/* ---------------- Recent jobs ---------------- */
function renderRecent() {
  const el = $('recentJobs');
  el.innerHTML = '';
  if (!state.jobs.length) { el.innerHTML = '<div class="empty-hint">No jobs yet</div>'; return; }
  state.jobs.slice(0, 6).forEach((j) => {
    const item = document.createElement('div');
    item.className = 'recent-item';
    const dot = j.status === 'done' ? 'done' : j.status === 'error' ? 'error' : '';
    item.innerHTML = `<span class="recent-dot ${dot}"></span><span class="recent-name">${esc(j.label)}</span><span class="recent-clips">${j.clips || ''}</span>`;
    item.addEventListener('click', () => {
      if (j.id === state.jobId && $('results').children.length) { switchView('studio'); $('results').scrollIntoView({behavior:'smooth'}); }
      else { state.jobId = j.id; startPolling(); switchView('studio'); }
    });
    el.appendChild(item);
  });
}

/* ---------------- Agent chat ---------------- */
$('chatSend').addEventListener('click', sendChat);
$('chatInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
});
async function sendChat() {
  if (state.chatBusy) return;
  const text = $('chatInput').value.trim();
  if (!text) return;
  $('chatInput').value = '';
  state.chatBusy = true; $('chatSend').disabled = true;

  addMsg('user', text);
  const typing = addTyping();

  const fd = new FormData();
  fd.append('message', text);
  if (state.jobId) fd.append('job_id', state.jobId);
  try {
    const res = await fetch('/api/chat', { method: 'POST', body: fd });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || 'Agent failed');
    typing.remove();

    const chips = [];
    if (d.parsed?.url) chips.push(`<span class="tool-chip link">↗ ${esc(d.parsed.url.slice(0, 60))}</span>`);
    if (d.parsed?.action === 'clip') chips.push(`<span class="tool-chip">◈ ${d.parsed.num_clips} clips · ${d.parsed.clip_length}</span>`);
    if (d.parsed?.goal) chips.push(`<span class="tool-chip">“${esc(d.parsed.goal.slice(0, 50))}”</span>`);
    addMsg('agent', d.reply, chips.join(''));

    if (d.job_id && d.parsed?.action === 'clip') {
      state.chatActiveJob = d.job_id;
      state.jobId = d.job_id;
      state.startedAt = Date.now();
      state.jobs.unshift({ id: d.job_id, label: d.parsed.goal || d.parsed.url.slice(0, 32), status: 'queued', clips: 0 });
      renderRecent();
      addMsg('agent', 'I started the job — progress will appear here when it finishes.', '<span class="tool-chip">◷ running</span>');
      switchView('studio');
      $('progressCard').classList.remove('hidden');
      startPolling();
    }
  } catch (e) {
    typing.remove(); addMsg('agent', `Sorry — ${e.message}`);
  } finally {
    state.chatBusy = false; $('chatSend').disabled = false;
  }
}
function addMsg(role, text, chips = '') {
  const wrap = document.createElement('div');
  wrap.className = `msg ${role}`;
  const now = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  wrap.innerHTML = role === 'agent'
    ? `<div class="msg-avatar">◈</div><div class="msg-body"><div class="bubble">${esc(text)}</div>${chips ? `<div class="tool-chips">${chips}</div>` : ''}<div class="msg-time">${now}</div></div>`
    : `<div class="msg-avatar">◉</div><div class="msg-body"><div class="bubble">${esc(text)}</div><div class="msg-time">${now}</div></div>`;
  $('chatMessages').appendChild(wrap);
  $('chatMessages').scrollTop = $('chatMessages').scrollHeight;
  return wrap;
}
function addTyping() {
  const wrap = document.createElement('div');
  wrap.className = 'msg agent';
  wrap.innerHTML = `<div class="msg-avatar">◈</div><div class="msg-body"><div class="bubble typing"><span></span><span></span><span></span></div></div>`;
  $('chatMessages').appendChild(wrap);
  $('chatMessages').scrollTop = $('chatMessages').scrollHeight;
  return wrap;
}
function addJobMessageToChat(d) {
  state.chatActiveJob = null;
  const lines = d.clips.map((c, i) => `${i+1}. ${c.title} (${fmt(c.start)}–${fmt(c.end)})`).join('\n');
  addMsg('agent', d.status === 'error'
    ? `That job failed: ${d.error}`
    : `Done — ${d.clips.length} clips ready. ${d.clips.map((c) => c.title).join(' · ')}. Open Studio to preview and download.`,
    `<span class="tool-chip done">✓ completed</span><span class="tool-chip">${d.clips.length} clips</span>`);
}

// init
setMode('link');
renderRecent();
