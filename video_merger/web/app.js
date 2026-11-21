// Video Merger page: shows the places to look in, the videos in parts found there and the merge, and sends what the
// user does (see web.py).
"use strict";
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const hms = s => {
    s = Math.max(0, Math.round(s || 0));
    return `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  };
  const size = b => (b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round((b || 0) / 1e6))} MB`);
  const baseName = p => String(p || "").split(/[\\/]/).pop();
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  let state = null;
  let lastLine = 0;
  let stopped = false;
  const open = new Set();
  const seen = new Map(); // row id -> last state (a video that starts merging or fails opens by itself)

  // -- Dark or light: the system's choice until the user picks one (kept in this browser only) -----------------

  const THEME_KEY = "video-merger-theme";
  const systemTheme = () => (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  const shownTheme = () => document.documentElement.dataset.theme || systemTheme();
  function applyTheme(theme) {
    if (theme === "dark" || theme === "light") document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
    const label = `Switch to ${shownTheme() === "dark" ? "light" : "dark"}`;
    $("#themeBtn").title = label;
    $("#themeBtn").setAttribute("aria-label", label);
  }
  try { applyTheme(localStorage.getItem(THEME_KEY)); } catch { applyTheme(null); }
  $("#themeBtn").addEventListener("click", () => {
    const next = shownTheme() === "dark" ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch { /* not kept: fine */ }
  });

  // -- talking to Video Merger --------------------------------------------------------------------

  async function api(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Video-Merger": "1" },
      body: JSON.stringify(body || {}),
      credentials: "same-origin",
    });
    let data = {};
    try { data = await res.json(); } catch { /* empty answer */ }
    if (!res.ok) throw new Error(data.error || `Video Merger answered ${res.status}.`);
    return data;
  }

  async function act(path, body, okText) {
    try {
      const data = await api(path, body);
      await refresh();
      if (okText) {
        const text = typeof okText === "function" ? okText(data) : okText;
        if (text) toast(text, true);
      }
      return data;
    } catch (err) {
      toast(err.message);
      await refresh();
      return null;
    }
  }

  let toastTimer = 0;
  function toast(text, ok = false) {
    const el = $("#toast");
    el.textContent = text;
    el.classList.toggle("ok", ok);
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, ok ? Math.max(3500, text.length * 70) : 8000);
  }

  let pollTimer = 0;
  let failures = 0;
  async function refresh() {
    clearTimeout(pollTimer);
    if (stopped) return;
    try {
      const res = await fetch(`/api/state?after=${lastLine}`, { credentials: "same-origin" });
      if (res.status === 403) { stopped = true; document.body.textContent = "Open Video Merger with the link shown in its window."; return; }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      state = await res.json();
      failures = 0;
      $("#goodbye").hidden = true;
      render();
    } catch {
      failures++;
      if (failures >= 3) $("#goodbye").hidden = false;
    }
    if (stopped) return;
    const busy = state && (state.running || state.scan.running || state.starting);
    pollTimer = setTimeout(refresh, failures ? 3000 : busy ? 600 : 1500);
  }

  // Redraws only on a change: replacing a control every second could swallow a click on it.
  const shown = new WeakMap();
  function setHtml(el, html) {
    if (shown.get(el) === html) return;
    el.innerHTML = html;
    shown.set(el, html);
  }
  // Fills a field from Video Merger's state unless the user is typing in it.
  function fill(id, value) {
    const el = $("#" + id);
    if (document.activeElement !== el && !el.dataset.dirty) el.value = value ?? "";
  }
  function check(id, value) {
    $("#" + id).checked = !!value;
  }
  function setDot(el, text, cls) {
    el.textContent = text;
    el.className = `dot ${cls || ""}`;
  }

  // -- header, options, places --------------------------------------------------------------------

  function renderStatus() {
    $("#version").textContent = `v${state.version}`;
    const t = state.tools;
    if (state.starting) setDot($("#ffmpegStatus"), "Starting…", "off");
    else if (t.ffmpeg && t.ffprobe) setDot($("#ffmpegStatus"), `FFmpeg${t.versions.ffmpeg ? " " + t.versions.ffmpeg : ""}`, "");
    else setDot($("#ffmpegStatus"), "FFmpeg missing", "bad");
    setDot($("#asfbinStatus"), t.asfbin ? `AsfBin${t.versions.asfbin ? " " + t.versions.asfbin : ""}` : "", "");
  }

  function renderOptions() {
    const o = state.options;
    const busy = state.running;
    check("out-next", o.save_to === "next");
    check("out-folder", o.save_to === "folder");
    check("fmt-mkv", o.container === "mkv");
    check("fmt-mp4", o.container === "mp4");
    check("optSub", o.subfolders);
    check("optAgain", o.merge_again);
    check("optDelete", o.delete_parts);
    fill("outPath", o.out_dir);
    $$("#tab-merge aside input").forEach(el => { el.disabled = busy; });
    $("#outPath").disabled = busy || o.save_to !== "folder";
    $("#outBrowse").disabled = busy || o.save_to !== "folder";
    $("#deleteNote").hidden = !o.delete_parts;
  }

  const REMOVE_ICON = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.2 3.1 8 6.9l3.8-3.8 1.1 1.1L9.1 8l3.8 3.8-1.1 1.1L8 9.1l-3.8 3.8-1.1-1.1L6.9 8 3.1 4.2Z"/></svg>`;
  function renderPlaces() {
    const busy = state.running || state.scan.running;
    const kind = p => (p.kind === "folder" ? (state.options.subfolders ? "folder + subfolders" : "folder") : "file");
    setHtml($("#sources"), state.places.map(p => `<div class="source"><span class="mono">${esc(p.path)}</span><span class="kind">${kind(p)}</span>`
      + `<button type="button" class="iconbtn" data-remove="${esc(p.path)}" aria-label="Remove ${esc(p.path)}" title="Remove" ${busy ? "disabled" : ""}>${REMOVE_ICON}</button></div>`).join(""));
  }

  // -- the videos found ---------------------------------------------------------------------------

  const PILLS = {
    running: ["run", "Merging"], waiting: ["grey", "Waiting"], done: ["ok", "Merged"], failed: ["bad", "Failed"],
    problem: ["bad", "Can't merge"], again: ["ready", "Merge again"], ready: ["ready", "Ready to merge"], already: ["grey", "Already merged"],
    left_out: ["grey", "Left out"],
  };
  const willMerge = v => v.state === "ready" || v.state === "again";
  const pill = v => {
    const [cls, label] = PILLS[v.state] || ["grey", v.label];
    return `<span class="pill ${cls}">${esc(label)}</span>`;
  };
  const scanText = scan => (scan.total ? `Reading ${Math.min(scan.done + 1, scan.total)} of ${scan.total} files…` : "Looking for video files…");

  function side(v) {
    if (v.state === "running" && v.progress != null) {
      const pct = Math.min(100, v.progress * 100);
      return `${pill(v)}<progress max="100" value="${pct.toFixed(1)}"></progress><span class="eta">${Math.round(pct)}% · ${hms(v.secs * v.progress)} / ${hms(v.secs)}</span>`;
    }
    return pill(v);
  }

  function detail(v) {
    let html = `<div class="tablewrap"><table><thead><tr><th>Part</th><th>File</th><th class="num">Length</th><th class="num">Size</th></tr></thead><tbody>`
      + v.parts.map(p => `<tr><td>${p.number}</td><td class="file">${esc(p.file)}</td><td class="num">${hms(p.secs)}</td><td class="num">${size(p.bytes)}</td></tr>`).join("")
      + `</tbody></table></div>`;
    html += `<dl class="kv"><dt>Found by</dt><dd>${v.by === "dmm" ? "DMM's tag in the files (content id and part number)" : "the file names, in one folder"}</dd>`;
    if (v.target && (willMerge(v) || ["running", "waiting", "done", "failed", "left_out"].includes(v.state))) {
      const saved = v.state === "done" && v.saved_as ? v.saved_as : v.target;
      html += `<dt>${v.state === "done" ? "Saved as" : "Will be saved as"}</dt><dd class="mono">${esc(saved)}</dd>`
        + `<dt>Chapters</dt><dd>${esc(v.chapters.join(", "))}</dd>`;
    }
    html += `</dl>`;
    const box = cls => `<div class="box ${cls}"><b>${esc(v.why)}</b>${v.fix ? `<span>${esc(v.fix)}</span>` : ""}</div>`;
    if (v.state === "problem" || v.state === "failed") html += box("bad");
    else if (v.state === "skip" || v.state === "already") html += box("info");
    if (v.warnings.length && v.state !== "done") html += v.warnings.map(w => `<div class="box warn"><span>⚠ ${esc(w)}</span></div>`).join("");
    if (willMerge(v) || v.state === "waiting") {
      html += `<div class="rowactions"><button type="button" class="btn small" data-leave="${v.id}" data-out="1">Leave out</button>`
        + `<span>Not merged when you press “Merge”, e.g. when these files are different videos, not parts. The files are not changed.</span></div>`;
    } else if (v.state === "left_out") {
      html += `<div class="box info"><b>Left out: not merged when you press “Merge”.</b><span>The files are not changed. Kept until Video Merger is closed, also after “Look again”.</span></div>`
        + `<div class="rowactions"><button type="button" class="btn small" data-leave="${v.id}" data-out="0">Put back</button></div>`;
    }
    if (v.steps.length) {
      html += `<ul class="steps">${v.steps.map((s, i) => `<li class="${v.state === "running" && i === v.steps.length - 1 ? "now" : ""}">${esc(s)}</li>`).join("")}</ul>`;
    }
    if (v.state === "done") {
      html += `<div class="box ok"><b>Merged and checked: ${esc(baseName(v.saved_as))}</b><span>Length ${hms(v.merged_secs)} (parts together ${hms(v.secs)}), `
        + `size ${size(v.merged_bytes)}, every track kept.${v.deleted ? ` The ${v.deleted} parts were deleted.` : " The parts are unchanged."}</span>`
        + `${v.note ? `<span>⚠ ${esc(v.note)}</span>` : ""}${v.warnings.map(w => `<span>⚠ ${esc(w)}</span>`).join("")}</div>`;
    }
    return html;
  }

  function rowHtml(v) {
    const isOpen = open.has(v.id);
    const chips = (v.wmv ? `<span class="chip wmv">WMV</span>` : "") + v.chips.map(c => `<span class="chip">${esc(c)}</span>`).join("")
      + `<span class="chip how">${v.by === "dmm" ? "DMM tag" : "by name"}</span>`;
    return `<div class="item"><div class="itemrow" data-row="${v.id}" role="button" tabindex="0" aria-expanded="${isOpen}">
      <div class="name"><span class="cid ${v.by === "name" ? "plain" : ""}">${esc(v.name)}</span><span class="parts">${v.parts.length} parts · ${hms(v.secs)} · ${size(v.bytes)}</span></div>
      <div class="chips chipcell">${chips}</div>
      <div class="side">${side(v)}</div></div>
      ${isOpen ? `<div class="detail">${detail(v)}</div>` : ""}</div>`;
  }

  function othersHtml() {
    const o = state.others;
    const list = items => `<ul>${items.map(p => `<li>${esc(p)}</li>`).join("")}</ul>`;
    const bits = [];
    if (o.whole.length) bits.push(`<details><summary>${plural(o.whole.length, "whole video")} (not in parts)</summary>${list(o.whole)}</details>`);
    if (o.ignored.length) {
      const kinds = [...new Set(o.ignored.map(p => (String(p).match(/\.[^.\\/]+$/) || [""])[0].toLowerCase()))].sort().join(", ");
      const note = o.wmv
        ? "Only .mkv, .mp4 and .wmv parts are merged; AVI, TS, VOB and ISO files are left alone."
        : "Only .mkv and .mp4 parts are merged; WMV, AVI, TS, VOB and ISO files are left alone. AsfBin (Windows) lets Video Merger join WMV parts too: see the Settings tab.";
      bits.push(`<details><summary>${plural(o.ignored.length, "file")} ignored (${esc(kinds)})</summary><span>${note}</span>${list(o.ignored)}</details>`);
    }
    if (o.unreadable.length) {
      bits.push(`<details><summary>${plural(o.unreadable.length, "file")} couldn't be read</summary>${list(o.unreadable.map(u => `${u.path}: ${u.error}`))}</details>`);
    }
    if (o.missing.length) bits.push(`<details><summary>${plural(o.missing.length, "place")} not found</summary>${list(o.missing)}</details>`);
    return bits.length ? `<div class="others">${bits.join("")}</div>` : "";
  }

  function renderList() {
    const scan = state.scan;
    for (const v of state.rows) {
      const before = seen.get(v.id);
      if (before !== v.state && ["running", "failed"].includes(v.state)) open.add(v.id);
      seen.set(v.id, v.state);
    }
    const list = $("#list");
    if (!list.firstElementChild || list.firstElementChild.id !== "rows") list.innerHTML = `<div id="rows"></div><div id="others"></div>`;
    let rows;
    if (!state.scanned) {
      rows = `<div class="empty">${scan.running ? esc(scanText(scan)) : "Add the folders that hold your downloads, then “Look for videos”. Nothing is changed until you press “Merge”."}</div>`;
    } else if (state.rows.length) {
      rows = state.rows.map(rowHtml).join("");
    } else {
      rows = `<div class="empty">${state.others.whole.length ? "No videos in parts here: every video file found is a whole video." : "No video files found in these places."}</div>`;
    }
    setHtml($("#rows"), rows);
    setHtml($("#others"), state.scanned ? othersHtml() : "");

    const n = test => state.rows.filter(test).length;
    const bits = [];
    if (scan.running) {
      bits.push(`<span>${esc(scanText(scan))}</span>`);
    } else if (state.scanned) {
      const toMerge = n(v => willMerge(v) || v.state === "waiting");
      if (toMerge) bits.push(`<span class="pill ready">${toMerge} to merge</span>`);
      if (n(v => v.state === "running")) bits.push(`<span class="pill run">1 merging</span>`);
      if (n(v => v.state === "done")) bits.push(`<span class="pill ok">${n(v => v.state === "done")} merged</span>`);
      const bad = n(v => v.state === "problem" || v.state === "failed");
      if (bad) bits.push(`<span class="pill bad">${bad} can't merge</span>`);
      const left = n(v => v.state === "skip" || v.state === "already");
      if (left) bits.push(`<span class="pill grey">${left} left as they are</span>`);
      const out = n(v => v.state === "left_out");
      if (out) bits.push(`<span class="pill grey">${out} left out</span>`);
      if (!bits.length) bits.push(`<span>Nothing to merge</span>`);
    } else {
      bits.push(`<span>${plural(state.places.length, "place")} to look in</span>`);
    }
    setHtml($("#counts"), bits.join(""));

    const ready = n(willMerge);
    const busy = state.running || scan.running;
    $("#mergeBtn").disabled = busy || !ready;
    $("#mergeBtn").textContent = ready ? `Merge ${plural(ready, "video")}` : "Merge";
    $("#scanBtn").disabled = busy || state.starting || !state.places.length;
    $("#scanBtn").textContent = scan.running ? "Looking…" : state.scanned ? "Look again" : "Look for videos";
    $("#stopBtn").hidden = !state.running;
    $("#stopBtn").disabled = state.stopping;
    $("#stopBtn").textContent = state.stopping ? "Stopping after this one" : "Stop after this one";
    $("#clearBtn").hidden = busy || !state.scanned;
  }

  // -- settings, log ------------------------------------------------------------------------------

  function toolRow(label, path, version, error, quiet) {
    const found = `<span class="ok-t">found</span> <span class="path">${esc(path)}</span>${version ? ` · ${esc(version)}` : ""}`;
    const missing = quiet ? `<span class="off-t">not found</span> <span class="path">${esc(quiet)}</span>` : `<span class="bad-t">${esc(error || "not found")}</span>`;
    return `<span>${label}</span><span>${path ? found : missing}</span>`;
  }

  function renderSettings() {
    const t = state.tools;
    const s = state.settings;
    setHtml($("#progTools"), state.starting ? `<span>Programs</span><span>looking for them…</span>`
      : toolRow("FFmpeg", t.ffmpeg, t.versions.ffmpeg, t.errors.ffmpeg) + toolRow("ffprobe", t.ffprobe, t.versions.ffprobe, t.errors.ffprobe)
        + toolRow("AsfBin", t.asfbin, t.versions.asfbin, t.errors.asfbin, s.asfbin ? "" : "only needed for WMV parts"));
    fill("setFfmpeg", s.ffmpeg);
    fill("setFfprobe", s.ffprobe);
    fill("setAsfbin", s.asfbin);
    $("#footLog").textContent = s.log_path;
    $("#footAddr").textContent = `Runs on your computer only, at ${location.host}; closing the window stops Video Merger (a merge in progress is removed, the parts are never changed).`;
  }

  function renderLog() {
    const el = $("#log");
    // Only lines not shown yet: two answers asked for at once (a poll and an action's refresh) carry the same lines.
    const lines = state.log.filter(([no]) => no > lastLine);
    if (!lines.length) {
      if (!lastLine) el.textContent = "Nothing yet.";
      return;
    }
    if (!lastLine) el.textContent = "";
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    el.insertAdjacentHTML("beforeend", lines.map(([, time, text]) => `<span class="t">${esc(time)}</span>  ${esc(text)}\n`).join(""));
    lastLine = lines[lines.length - 1][0];
    if (atBottom) el.scrollTop = el.scrollHeight;
  }

  function render() {
    renderStatus();
    renderOptions();
    renderPlaces();
    renderList();
    renderSettings();
    renderLog();
  }

  // -- the folder browser -------------------------------------------------------------------------

  let browseFor = "add"; // add (a place to look in), out (the folder for merged videos)
  let browsePath = "";
  let browseFile = "";

  function browseButton() {
    $("#browseOk").textContent = browseFile ? "Add this file" : "Use this folder";
    $("#browseOk").disabled = !browseFile && !browsePath;
  }

  async function browseTo(path) {
    try {
      const res = await fetch(`/api/browse?path=${encodeURIComponent(path)}&files=${browseFor === "add" ? "video" : ""}`, { credentials: "same-origin" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error);
      browsePath = data.path;
      browseFile = "";
      $("#browsePath").textContent = data.path || "This computer";
      $("#browseUp").disabled = data.parent == null;
      $("#browseUp").dataset.path = data.parent ?? "";
      const list = $("#browseList");
      list.innerHTML = data.entries.map((e, i) => (e.dir
        ? `<div class="entry">📁 <button type="button" data-go="${i}">${esc(e.name)}</button></div>`
        : `<label class="entry"><input type="radio" name="browsefile" value="${i}"> 🎞 ${esc(e.name)}<span class="sz">${size(e.size)}</span></label>`)).join("")
        || `<div class="entry">(no folders${browseFor === "add" ? " or video files" : ""})</div>`;
      list.entries = data.entries;
      browseButton();
    } catch (err) {
      toast(err.message);
    }
  }

  function openBrowse(mode) {
    browseFor = mode;
    $("#browseTitle").textContent = mode === "add" ? "Choose a folder to look in" : "Choose the folder for merged videos";
    $("#browseHint").textContent = mode === "add" ? "Open folders with a click; pick a video file to add just that file." : "Open folders with a click.";
    $("#browse").hidden = false;
    $("#browseCancel").focus();
    const last = state.places.length ? state.places[state.places.length - 1].path : "";
    browseTo(mode === "add" ? last : state.options.out_dir);
  }

  // -- events -------------------------------------------------------------------------------------

  $$(".tabs button").forEach(b => b.addEventListener("click", () => {
    $$(".tabs button").forEach(x => x.setAttribute("aria-selected", String(x === b)));
    ["merge", "settings", "log"].forEach(t => { $("#tab-" + t).hidden = t !== b.dataset.tab; });
  }));

  const outPath = $("#outPath");
  $$("input[name=out]").forEach(r => r.addEventListener("change", () => {
    const body = r.value === "folder" ? { save_to: "folder", out_dir: outPath.value } : { save_to: "next" };
    delete outPath.dataset.dirty;
    act("/api/options", body);
  }));
  $$("input[name=fmt]").forEach(r => r.addEventListener("change", () => act("/api/options", { container: r.value })));
  $("#optSub").addEventListener("change", e => act("/api/options", { subfolders: e.target.checked },
    d => (d.look_again ? "Press “Look again” to look with this change." : "")));
  $("#optAgain").addEventListener("change", e => act("/api/options", { merge_again: e.target.checked }));
  $("#optDelete").addEventListener("change", e => act("/api/options", { delete_parts: e.target.checked }));
  outPath.addEventListener("input", () => { outPath.dataset.dirty = "1"; });
  const saveOut = () => {
    if (!outPath.dataset.dirty) return;
    delete outPath.dataset.dirty;
    act("/api/options", { out_dir: outPath.value }, "Folder saved.");
  };
  outPath.addEventListener("change", saveOut);
  outPath.addEventListener("keydown", e => { if (e.key === "Enter") saveOut(); });

  async function addPath(path) {
    path = String(path || "").trim().replace(/^"|"$/g, "");
    if (!path) { toast("Paste a folder or a video file first."); return; }
    const data = await act("/api/places/add", { path }, () => (state.scanned ? "Added: press “Look again” to look there too." : ""));
    if (data) $("#addPath").value = "";
  }
  $("#addBtn").addEventListener("click", () => addPath($("#addPath").value));
  $("#addPath").addEventListener("keydown", e => { if (e.key === "Enter") addPath($("#addPath").value); });
  $("#sources").addEventListener("click", e => {
    const b = e.target.closest("[data-remove]");
    if (b && !b.disabled) act("/api/places/remove", { path: b.dataset.remove });
  });

  $("#scanBtn").addEventListener("click", () => act("/api/look", {}));
  $("#mergeBtn").addEventListener("click", () => act("/api/start", {}));
  $("#stopBtn").addEventListener("click", () => act("/api/stop", {}, "Stops after the video being merged."));
  $("#clearBtn").addEventListener("click", () => { open.clear(); act("/api/clear", {}); });

  const toggle = row => {
    const id = +row.dataset.row;
    open.has(id) ? open.delete(id) : open.add(id);
    renderList();
  };
  $("#list").addEventListener("click", e => {
    const leave = e.target.closest("[data-leave]");
    if (leave) {
      act("/api/leave-out", { id: +leave.dataset.leave, out: leave.dataset.out === "1" });
      return;
    }
    if (e.target.closest("a, button, input, summary")) return;
    const row = e.target.closest("[data-row]");
    if (row) toggle(row);
  });
  $("#list").addEventListener("keydown", e => {
    const row = e.target.closest("[data-row]");
    if (row && e.target === row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggle(row); }
  });

  $$("#tab-settings input").forEach(el => el.addEventListener("input", () => { el.dataset.dirty = "1"; }));
  $("#progSave").addEventListener("click", async () => {
    const ids = [["setFfmpeg", "ffmpeg"], ["setFfprobe", "ffprobe"], ["setAsfbin", "asfbin"]];
    const data = await act("/api/settings", Object.fromEntries(ids.map(([id, name]) => [name, $("#" + id).value])), "Saved.");
    if (data) ids.forEach(([id]) => delete $("#" + id).dataset.dirty);
  });
  $("#doctorBtn").addEventListener("click", () => act("/api/doctor", {}, () => {
    const t = state.tools;
    const ffmpeg = t.ffmpeg && t.ffprobe ? `FFmpeg and ffprobe found${t.versions.ffmpeg ? ` (${t.versions.ffmpeg})` : ""}` : "FFmpeg is missing";
    return `${ffmpeg}; AsfBin ${t.asfbin ? "found" : "not found"}.`;
  }));
  $("#quitBtn").addEventListener("click", async () => {
    if (state && state.running && !confirm("A video is being merged. Quit anyway? Its unfinished file is removed; the parts are not changed.")) return;
    const ok = await act("/api/quit", {});
    if (ok) {
      stopped = true;
      clearTimeout(pollTimer);
      $("#goodbye").textContent = "Video Merger has stopped. You can close this page.";
      $("#goodbye").hidden = false;
    }
  });

  $("#addBrowse").addEventListener("click", () => openBrowse("add"));
  $("#outBrowse").addEventListener("click", () => openBrowse("out"));
  $("#browseList").addEventListener("click", e => {
    const go = e.target.closest("[data-go]");
    if (go) browseTo($("#browseList").entries[+go.dataset.go].path);
  });
  $("#browseList").addEventListener("change", e => {
    const pick = e.target.closest("input[name=browsefile]");
    if (pick) { browseFile = $("#browseList").entries[+pick.value].path; browseButton(); }
  });
  $("#browseUp").addEventListener("click", e => browseTo(e.currentTarget.dataset.path));
  $("#browseCancel").addEventListener("click", () => { $("#browse").hidden = true; });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#browse").hidden) $("#browse").hidden = true; });
  $("#browseOk").addEventListener("click", () => {
    if (browseFor === "add") addPath(browseFile || browsePath);
    else act("/api/options", { save_to: "folder", out_dir: browsePath }, "Folder saved.");
    $("#browse").hidden = true;
  });

  refresh();
})();
