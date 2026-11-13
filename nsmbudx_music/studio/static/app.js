"use strict";
/* nsmbudx-music Studio front end: talks to the local server in server.py. */

const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "html") el.innerHTML = v;
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(kid));
  return el;
};
const fmtTime = s => {
  if (!isFinite(s)) s = 0;
  const m = Math.floor(s / 60), r = s - m * 60;
  return `${m}:${r.toFixed(1).padStart(4, "0")}`;
};
const hex = (n, w = 8) => "0x" + (n >>> 0).toString(16).toUpperCase().padStart(w, "0");
const TRACK_COLORS = ["--t0", "--t1", "--t2", "--t3", "--t4", "--t5"].map(v => getComputedStyle(document.documentElement).getPropertyValue(v).trim());
const tcolor = t => TRACK_COLORS[t % TRACK_COLORS.length] || "#aaa";

/* ---------------------------------------------------------------- server */
async function api(path, { method = "GET", body, params } = {}) {
  const url = new URL(path, location.origin);
  if (params) for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  const res = await fetch(url, {
    method, headers: { "X-Token": TOKEN, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const ctype = res.headers.get("Content-Type") || "";
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).error || msg; } catch { }
    throw new Error(msg);
  }
  if (ctype.startsWith("application/json")) {
    const out = await res.json();
    if (out && out.id && out.state) return await followJob(out);
    return out;
  }
  return await res.arrayBuffer();
}
function audioUrl(path, params) {
  const url = new URL(path, location.origin);
  url.searchParams.set("t", TOKEN);
  for (const [k, v] of Object.entries(params || {})) url.searchParams.set(k, v);
  return url.toString();
}

async function followJob(job) {
  const chip = h("div", { class: "job" }, h("span", { class: "spin" }), h("span", { class: "ellipsis" }, job.title));
  $("#jobs").append(chip);
  try {
    while (true) {
      const j = await (await fetch(audioUrl("/api/job", { id: job.id }))).json();
      if (j.error && !j.state) throw new Error(j.error);
      chip.lastChild.textContent = j.title + (j.log.length ? " · " + j.log[j.log.length - 1] : "");
      if (j.state === "done") return j.result;
      if (j.state === "error") throw new Error(j.error);
      await new Promise(r => setTimeout(r, 350));
    }
  } finally { chip.remove(); }
}

function toast(msg, kind = "") {
  const t = h("div", { class: "toast " + kind }, msg);
  $("#toast").append(t);
  setTimeout(() => t.remove(), kind === "bad" ? 7000 : 3500);
}
const guard = fn => async (...a) => { try { return await fn(...a); } catch (e) { toast(e.message, "bad"); console.error(e); } };

/* ---------------------------------------------------------------- state */
const S = {
  summary: null, tree: [], cat: "music", search: "", sel: null, item: null, tab: null,
  render: null, listing: null, limit: 300,
};
const CATS = [
  ["music", "Music", it => it.s === "sound" && it.m],
  ["custom", "Added", it => it.s === "sound" && it.x],
  ["sound", "Sounds", it => it.s === "sound"],
  ["soundset", "Sound sets", it => it.s === "soundset"],
  ["bank", "Banks", it => it.s === "bank"],
  ["wavearc", "Wave archives", it => it.s === "wavearc"],
  ["group", "Groups", it => it.s === "group"],
  ["player", "Players", it => it.s === "player"],
  ["file", "Files", it => it.s === "file"],
];

/* ---------------------------------------------------------------- audio engine */
const Audio = {
  ctx: null, master: null, buffers: {}, gains: {}, sources: [], playing: false, startAt: 0, offset: 0,
  length: 0, loop: null, label: "", muted: new Set(), solo: null, onTick: [], kind: null,
  ensure() {
    if (!this.ctx) {
      this.ctx = new AudioContext();
      this.master = this.ctx.createGain();
      this.master.gain.value = +$("#tp-vol").value;
      this.master.connect(this.ctx.destination);
    }
    if (this.ctx.state === "suspended") this.ctx.resume();
  },
  async decode(url) {
    this.ensure();
    const res = await fetch(url);
    if (!res.ok) { let m = res.statusText; try { m = (await res.json()).error; } catch { } throw new Error(m); }
    return await this.ctx.decodeAudioData(await res.arrayBuffer());
  },
  async load(tracks, { label, loop, length, kind }) {
    this.stop();
    this.buffers = {}; this.gains = {};
    for (const [t, url] of Object.entries(tracks)) this.buffers[t] = await this.decode(url);
    for (const t of Object.keys(this.buffers)) {
      const g = this.ctx.createGain(); g.connect(this.master); this.gains[t] = g;
    }
    this.length = length ?? Math.max(...Object.values(this.buffers).map(b => b.duration));
    this.loop = loop || null; this.label = label; this.offset = 0; this.kind = kind || null;
    this.applyGains();
    $("#tp-play").disabled = $("#tp-stop").disabled = false;
    $("#tp-label").textContent = label;
    const lp = $("#tp-loop");
    if (this.loop) {
      lp.style.display = "block";
      lp.style.left = (100 * this.loop[0] / this.length) + "%";
      lp.style.width = (100 * (this.loop[1] - this.loop[0]) / this.length) + "%";
    } else lp.style.display = "none";
  },
  applyGains() {
    for (const [t, g] of Object.entries(this.gains)) {
      const on = this.solo != null ? +t === this.solo : !this.muted.has(+t);
      g.gain.setTargetAtTime(on ? 1 : 0, this.ctx.currentTime, 0.01);
    }
  },
  play(from = null) {
    this.ensure();
    if (from != null) this.offset = from;
    this.stopSources();
    const looping = $("#tp-loopon").checked && this.loop;
    if (!looping && this.offset >= this.length - 0.01) this.offset = 0;
    const when = this.ctx.currentTime + 0.03;
    for (const [t, buf] of Object.entries(this.buffers)) {
      const src = this.ctx.createBufferSource();
      src.buffer = buf;
      if (looping) { src.loop = true; src.loopStart = this.loop[0]; src.loopEnd = Math.min(this.loop[1], buf.duration); }
      src.connect(this.gains[t]);
      src.start(when, Math.min(this.offset, buf.duration - 0.001));
      this.sources.push(src);
    }
    this.startAt = when - this.offset; this.playing = true;
    $("#tp-play").innerHTML = "&#10074;&#10074;";
    if (this.sources[0]) this.sources[0].onended = () => { if (this.playing && !looping) { this.playing = false; this.offset = 0; this.ui(); } };
    this.tick();
  },
  pause() { if (!this.playing) return; this.offset = this.position(); this.stopSources(); this.playing = false; this.ui(); },
  stop() { this.stopSources(); this.playing = false; this.offset = 0; this.ui(); },
  stopSources() { for (const s of this.sources) { try { s.onended = null; s.stop(); } catch { } } this.sources = []; },
  position() {
    if (!this.playing) return this.offset;
    let p = this.ctx.currentTime - this.startAt;
    if ($("#tp-loopon").checked && this.loop && p > this.loop[1]) {
      const len = this.loop[1] - this.loop[0];
      p = this.loop[0] + ((p - this.loop[0]) % len);
    }
    return Math.max(0, Math.min(p, this.length));
  },
  ui() {
    const p = this.position();
    $("#tp-fill").style.width = (this.length ? 100 * p / this.length : 0) + "%";
    $("#tp-time").textContent = `${fmtTime(p)} / ${fmtTime(this.length)}`;
    if (!this.playing) $("#tp-play").innerHTML = "&#9654;";
    for (const f of this.onTick) f(p);
  },
  tick() { this.ui(); if (this.playing) requestAnimationFrame(() => this.tick()); },
  toggle() { this.playing ? this.pause() : this.play(); },
};
$("#tp-play").onclick = () => Audio.toggle();
$("#tp-stop").onclick = () => Audio.stop();
$("#tp-vol").oninput = e => { if (Audio.master) Audio.master.gain.value = +e.target.value; };
$("#tp-loopon").onchange = () => { if (Audio.playing) Audio.play(Audio.position()); };
$(".tp-bar").onclick = e => {
  if (!Audio.length) return;
  const r = e.currentTarget.getBoundingClientRect();
  const p = (e.clientX - r.left) / r.width * Audio.length;
  Audio.playing ? Audio.play(p) : (Audio.offset = p, Audio.ui());
};
document.addEventListener("keydown", e => {
  if (e.target.matches("input, textarea, select")) return;
  if (e.code === "Space" && !$("#tp-play").disabled) { e.preventDefault(); Audio.toggle(); }
});

/* one-shot preview through its own source (piano keys, wave rows) */
async function preview(url) {
  Audio.ensure();
  const buf = await Audio.decode(url);
  const src = Audio.ctx.createBufferSource();
  src.buffer = buf; src.connect(Audio.master); src.start();
  return src;
}

/* ---------------------------------------------------------------- sidebar */
function renderCats() {
  const nav = $("#cats"); nav.innerHTML = "";
  for (const [key, name, test] of CATS) {
    const n = S.tree.filter(test).length;
    if (key === "custom" && !n) continue;
    nav.append(h("button", { class: "cat" + (S.cat === key ? " on" : ""), onclick: () => { S.cat = key; S.limit = 300; renderCats(); renderList(); } },
      name, h("b", {}, n)));
  }
}
function renderList() {
  const test = CATS.find(c => c[0] === S.cat)[2];
  const q = S.search.toLowerCase();
  const items = S.tree.filter(it => test(it) && (!q || it.l.toLowerCase().includes(q) || (it.c || "").toLowerCase().includes(q)));
  const list = $("#list"); list.innerHTML = "";
  for (const it of items.slice(0, S.limit)) {
    const sel = S.sel && S.sel.s === it.s && S.sel.i === it.i;
    const tags = [];
    if (it.s === "sound") tags.push(h("span", { class: "tag " + it.k.toLowerCase() }, it.k));
    if (it.x) tags.push(h("span", { class: "tag new" }, "added"));
    list.append(h("div", { class: "row" + (sel ? " sel" : ""), onclick: () => select(it.s, it.i) },
      h("div", { class: "l" }, h("div", {}, it.l), it.c ? h("div", { class: "k" }, it.c) : (it.s !== "sound" ? h("div", { class: "k" }, it.k) : null)),
      ...tags));
  }
  if (items.length > S.limit) list.append(h("div", { class: "more" }, `${items.length - S.limit} more… `,
    h("a", { onclick: () => { S.limit += 500; renderList(); } }, "show more")));
  if (!items.length) list.append(h("div", { class: "more" }, "Nothing matches."));
}
$("#search").oninput = e => { S.search = e.target.value; S.limit = 300; renderList(); };

/* ---------------------------------------------------------------- archive */
async function refresh(keepSel = true) {
  S.summary = await api("/api/summary");
  const open = S.summary.open;
  for (const id of ["#btn-save", "#btn-saveas", "#btn-add", "#search"]) $(id).disabled = !open;
  $("#archive-name").textContent = open ? `${S.summary.path}${S.summary.dirty ? "  •  unsaved changes" : ""}` : "";
  if (open) {
    S.tree = await api("/api/tree");
    renderCats(); renderList();
  }
  if (!keepSel || !S.sel) showHome();
}
const openArchive = guard(async path => {
  const r = await api("/api/open", { method: "POST", body: path ? { path } : {} });
  if (r && r.cancelled) return;
  S.sel = null;
  await refresh(false);
  toast("Opened " + S.summary.path.split(/[\\/]/).pop(), "good");
});
$("#btn-open").onclick = () => openArchive();
$("#btn-save").onclick = guard(async () => {
  const r = await api("/api/save", { method: "POST", body: { path: S.summary.path, ask: false } });
  if (!r.cancelled) { toast(`Saved ${r.path}`, "good"); refresh(); }
});
$("#btn-saveas").onclick = guard(async () => {
  const r = await api("/api/save", { method: "POST", body: { ask: true } });
  if (!r.cancelled) { toast(`Saved ${r.path}`, "good"); refresh(); }
});
$("#btn-add").onclick = () => openAddMusic();
window.addEventListener("beforeunload", e => { if (S.summary && S.summary.dirty) { e.preventDefault(); e.returnValue = ""; } });
setInterval(() => { fetch(audioUrl("/api/summary")).catch(() => { }); }, 10000);

/* ---------------------------------------------------------------- home */
function showHome() {
  $("#home").classList.remove("hidden"); $("#item").classList.add("hidden");
  const el = $("#home"); el.innerHTML = "";
  const sm = S.summary;
  if (!sm || !sm.open) {
    el.append(h("div", { class: "hero" },
      h("h1", {}, "Sound archive studio for NSMBU Deluxe"),
      h("p", {}, "Open the game's romfs/Sound/Sound.bfsar (or a copy) to browse and play every sound, edit sequences, banks, waves and groups, and add your own course music with a Yoshi layer and enemies that dance on your beat."),
      h("button", { class: "btn accent", onclick: () => openArchive() }, "Open Sound.bfsar…"),
      h("p", { class: "dim", style: "margin-top:18px" }, `Version ${sm ? sm.version : ""} · your changes are saved only when you press Save.`)));
    return;
  }
  const c = sm.counts;
  el.append(h("div", { class: "card" }, h("h3", {}, "Archive"),
    h("div", { class: "stats" },
      ...[["Sounds", c.sound], ["Sound sets", c.soundset], ["Banks", c.bank], ["Wave archives", c.wavearc], ["Groups", c.group], ["Files", c.file]]
        .map(([k, v]) => h("div", { class: "stat" }, h("b", {}, v), h("span", { class: "dim" }, k))),
      h("div", { class: "stat" }, h("b", {}, (sm.size / 1048576).toFixed(1) + " MB"), h("span", { class: "dim" }, sm.growth > 0 ? `+${(sm.growth / 1048576).toFixed(1)} MB over the game's` : "size"))),
    sm.vanilla ? null : h("p", { class: "err" }, "This archive has fewer items than v1.0.0's Sound.bfsar; adding music needs the v1.0.0 archive.")));

  const cm = h("div", { class: "card" }, h("h3", {}, "Added music"));
  if (!sm.custom.length) cm.append(h("p", { class: "dim" }, "None yet. Added songs play in levels through the Custom Music sprite (785) of the nsmbud-reverse mod."));
  else cm.append(h("p", { class: "help" }, "Place a Custom Music sprite (785) in each area with the song's slot. The sprite reads the song: it keeps a song with a Yoshi layer while Mario rides Yoshi, plays CUSTOM_BGM_nn_FAST when time runs low if you added one, and the enemies dance to the song's own beat and bahs."));
  else {
    const t = h("table", { class: "t" }, h("tr", {}, ...["Label", "Length", "BPM", "First beat", "Bahs", "Yoshi layer", "Sprite 785", ""].map(x => h("th", {}, x))));
    for (const m of sm.custom) {
      const it = S.tree.find(x => x.s === "sound" && x.l === m.label);
      const sp = m.sprite ? h("span", { title: `settings1 ${m.sprite.settings1} · nybble 7 can add 1 (keep in hurry), 2 (keep on Yoshi), 4 (keep with Baby Yoshi)` },
        "slot ", h("b", { class: "mono" }, m.sprite.nybbles56), h("span", { class: "dim" }, " (nybbles 5–6)")) : h("span", { class: "dim" }, "plays with its slot");
      t.append(h("tr", {}, h("td", {}, h("a", { onclick: () => it && select("sound", it.i) }, m.label)), h("td", {}, m.seconds + " s"),
        h("td", {}, m.bpm), h("td", {}, m.offset + " s"), h("td", {}, m.handBahs ? "placed by hand" : "every 4th bar"),
        h("td", {}, m.yoshi ? "yes · kept on Yoshi" : "—"), h("td", {}, sp),
        h("td", {}, h("button", { class: "btn small danger", onclick: guard(async () => {
          if (!confirm(`Remove ${m.label} from the archive? (Nothing is written until you save.)`)) return;
          await api("/api/music/remove", { method: "POST", body: { label: m.label } });
          toast(`Removed ${m.label}`); refresh(false);
        }) }, "Remove"))));
    }
    cm.append(t);
  }
  cm.append(h("div", { style: "margin-top:10px" }, h("button", { class: "btn accent", onclick: () => openAddMusic() }, "+ Add music")));
  el.append(cm);

  const lim = h("div", { class: "card" }, h("h3", {}, "Sound archive player limits"), h("div", { class: "fields" }));
  el.append(lim);
  api("/api/archive").then(r => fillFields($(".fields", lim), r.fields, "archive", 0)).catch(() => { });
  el.append(h("div", { class: "card" }, h("h3", {}, "How the game plays music"), h("div", { html: GAME_NOTES })));
}
const GAME_NOTES = `
<p>Course music is a <b>sequence</b> with up to four tracks. <b>Track 0</b> is silent: it writes the beat the enemies dance to
(variable 1 every bar or half bar, variable 0 for the "bah" hops). <b>Track 1</b> is the song, held on one note so the bank's
looping wave plays through. <b>Track 2</b> is the <b>Yoshi layer</b>: the game turns it on only while Mario rides Yoshi.
Track 3 is muted by the game's track code.</p>
<p>The game loads a file only when a <b>group</b> that lists it is loaded; GROUP_BOOT loads at boot and stays, which is where added music goes.
Hurry-up versions are separate sounds ending in <span class="mono">_FAST</span>.</p>`;

/* ---------------------------------------------------------------- item */
const select = guard(async (section, index, tab) => {
  S.sel = { s: section, i: index };
  renderList();
  const it = await api("/api/item", { params: { section, index } });
  S.item = it; S.render = null; S.listing = null;
  $("#home").classList.add("hidden"); $("#item").classList.remove("hidden");
  $("#item-title").textContent = it.label || `${section} ${index}`;
  const sub = [];
  if (it.kind) sub.push(it.kind === "SEQ" ? "Sequence sound" : it.kind === "WSD" ? "Wave sound" : it.kind);
  if (it.id != null) sub.push(hex(it.id));
  if (it.file != null) sub.push(`file ${it.file}`);
  if (it.custom) sub.push("added music");
  $("#item-sub").textContent = sub.join(" · ") || section;
  const tabs = $("#tabs"); tabs.innerHTML = "";
  const names = { player: "Player", properties: "Properties", sequence: "Sequence", bank: "Bank", waves: "Waves", group: "Group", hex: "Hex" };
  for (const t of it.tabs) tabs.append(h("button", { "data-tab": t, onclick: () => showTab(t) }, names[t]));
  for (const sec of $$(".tab")) sec.innerHTML = "";
  showTab(tab && it.tabs.includes(tab) ? tab : it.tabs[0]);
});
function showTab(t) {
  S.tab = t;
  $$("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === t));
  $$(".tab").forEach(s => s.classList.toggle("on", s.id === "tab-" + t));
  const sec = $("#tab-" + t);
  if (sec.dataset.for === key()) return;
  sec.dataset.for = key();
  const fn = ({ player: tabPlayer, properties: tabProperties, sequence: tabSequence, bank: tabBank, waves: tabWaves, group: tabGroup, hex: tabHex })[t];
  Promise.resolve().then(() => fn(sec)).catch(e => { sec.dataset.for = ""; toast(e.message, "bad"); console.error(e); });
}
const key = () => S.item ? `${S.item.section}:${S.item.index}` : "";
function navRef(ref, value) {
  const sec = { file: "file", player: "player", bank: "bank", wavearc: "wavearc", sound: "sound", group: "group" }[ref];
  if (!sec) return;
  select(sec, ref === "file" ? value : value & 0xFFFFFF);
}

/* ---------------------------------------------------------------- properties */
function tabProperties(sec) {
  const groups = {};
  for (const f of S.item.fields) (groups[f.group || "General"] ||= []).push(f);
  for (const [g, fs] of Object.entries(groups)) {
    const card = h("div", { class: "card" }, h("h3", {}, g), h("div", { class: "fields" }));
    fillFields($(".fields", card), fs, S.item.section, S.item.index);
    sec.append(card);
  }
  sec.append(h("p", { class: "dim" }, "Dashed fields are read-only or not stored by this item (the game uses its default). Changes apply to the open archive; Save writes them."));
}
function fillFields(el, fields, section, index) {
  el.innerHTML = "";
  for (const f of fields) {
    let input;
    const val = f.value;
    if (f.kind === "enum") {
      input = h("select", { disabled: f.readonly }, ...f.options.map((o, i) => h("option", { value: i, selected: i === val }, o)));
    } else if (f.kind === "bool") {
      input = h("input", { type: "checkbox", disabled: f.readonly, checked: !!val });
    } else if (f.kind === "text") {
      input = h("input", { type: "text", readonly: true, value: val ?? "" });
    } else {
      const shown = f.kind === "hex" || f.kind === "id" ? (val == null ? "" : (f.kind === "id" && f.ref === "file" ? String(val) : hex(val))) :
        f.kind === "f32" ? (val == null ? "" : (+val).toFixed(4).replace(/0+$/, "").replace(/\.$/, "")) : (val == null ? "" : String(val));
      input = h("input", { type: "text", readonly: f.readonly, value: shown, title: f.help || "" });
    }
    const row = h("div", { class: "field-row" }, input);
    if (f.ref && val != null && val !== 0xFFFFFFFF) row.append(h("a", { onclick: () => navRef(f.ref, val) }, f.refLabel || "open"));
    if (f.help) row.append(h("span", { class: "help" }, f.help));
    if (!f.readonly) {
      input.addEventListener("change", guard(async () => {
        const v = input.type === "checkbox" ? (input.checked ? 1 : 0) : input.value;
        const r = await api("/api/field", { method: "POST", body: { section, index, key: f.key, value: v } });
        toast(`${f.label} = ${r.value}`, "good");
        S.summary.dirty = true; $("#archive-name").textContent = `${S.summary.path}  •  unsaved changes`;
        if (S.item) $$(".tab").forEach(s => { if (s.id !== "tab-properties") s.dataset.for = ""; });
      }));
    }
    el.append(h("label", {}, f.label), row);
  }
}

/* ---------------------------------------------------------------- player */
const ROLE = { 0: "Beat track (silent)", 1: "Song", 2: "Yoshi layer", 3: "Third layer" };
function trackName(t, course) {
  if (course) return `Track ${t} · ${ROLE[t] || "layer"}`;
  return `Track ${t}`;
}
async function tabPlayer(sec) {
  const it = S.item;
  sec.append(h("div", { class: "card" }, h("div", { class: "seq-bar" },
    h("button", { class: "btn accent", id: "pl-render", onclick: () => doRender(sec) }, "▶ Render and play"),
    h("span", { class: "dim" }, "Plays the sound the way the game builds it: the sequence, its banks and waves."))));
  if (Audio.label === it.label && S.render && S.render.label === it.label) drawPlayer(sec);
}
const doRender = guard(async sec => {
  const it = S.item;
  const meta = await api("/api/render", { method: "POST", body: { index: it.index } });
  S.render = meta;
  const tracks = {};
  for (const t of meta.tracks) tracks[t] = audioUrl("/api/audio", { token: meta.token, track: t });
  await Audio.load(tracks, { label: meta.label, loop: meta.loop, length: meta.length, kind: meta.kind });
  if (it.course) { Audio.muted = new Set(meta.tracks.filter(t => t > 1)); Audio.solo = null; Audio.applyGains(); }
  else { Audio.muted = new Set(); Audio.solo = null; Audio.applyGains(); }
  drawPlayer(sec);
  Audio.play(0);
});
function drawPlayer(sec) {
  const meta = S.render, it = S.item;
  sec.innerHTML = "";
  const top = h("div", { class: "player-top" });
  const main = h("div", { class: "card" });
  const info = [];
  info.push(`${fmtTime(meta.length)} rendered`);
  if (meta.loop) info.push(`loops ${fmtTime(meta.loop[0])} → ${fmtTime(meta.loop[1])}`);
  if (meta.bpm) info.push(`${meta.bpm} BPM`);
  info.push(`${meta.notes.length} notes`, `${meta.vars.length} variable writes`);
  main.append(h("div", { class: "seq-bar" },
    h("button", { class: "btn small", onclick: () => doRender(sec) }, "Render again"),
    h("span", { class: "dim" }, info.join(" · "))));
  const wrap = h("div", { class: "wave-wrap" });
  const cv = h("canvas", { height: 150 });
  wrap.append(cv); main.append(wrap);
  main.append(h("div", { class: "legend" },
    h("span", {}, h("i", { style: "background:var(--accent)" }), "bah (var 0)"),
    h("span", {}, h("i", { style: "background:var(--blue)" }), "beat accent (var 1)"),
    h("span", {}, h("i", { style: "background:var(--green)" }), "loop start"),
    h("span", {}, h("i", { style: "background:var(--red)" }), "loop end")));
  if (it.course) {
    const st = h("div", { class: "states", style: "margin-top:10px" }, h("span", { class: "dim" }, "Game state:"));
    const mk = (name, tracks) => h("button", { class: "btn small", onclick: e => {
      Audio.solo = null; Audio.muted = new Set(meta.tracks.filter(t => !tracks.includes(t)));
      Audio.applyGains(); $$(".states .btn", sec).forEach(b => b.classList.remove("on")); e.target.classList.add("on"); drawTracks();
    } }, name);
    const b1 = mk("Normal", [0, 1]), b2 = mk("Riding Yoshi", [0, 1, 2]), b3 = mk("All tracks", meta.tracks);
    b1.classList.add("on");
    st.append(b1, b2, b3);
    main.append(st);
  }
  const trk = h("div", { class: "tracks" });
  main.append(trk);
  top.append(main);
  const side = h("div", { class: "card" }, h("h3", {}, "On the beat"));
  const crit = h("canvas", { id: "critter", width: 230, height: 150 });
  side.append(crit);
  const vars = h("div", { class: "vars" });
  for (let v = 0; v < 16; v++) vars.append(h("div", { class: "var", "data-v": v }, h("small", {}, v === 0 ? "bah 0" : v === 1 ? "beat 1" : "var " + v), h("span", {}, "–")));
  side.append(vars);
  side.append(h("p", { class: "help" }, "The critter hops when variable 0 is written and nods on variable 1, as the enemies do in the game."));
  top.append(side);
  sec.append(top);

  const notesCard = h("div", { class: "card" }, h("h3", {}, "Notes"), h("canvas", { height: 120, id: "roll" }));
  sec.append(notesCard);

  function drawTracks() {
    trk.innerHTML = "";
    for (const t of meta.tracks) {
      const on = Audio.solo != null ? Audio.solo === t : !Audio.muted.has(t);
      const c = h("canvas", { height: 34 });
      const row = h("div", { class: "trk" },
        h("div", { class: "name" }, h("span", { class: "dot", style: `background:${tcolor(t)}` }),
          h("span", { class: "ellipsis", style: on ? "" : "opacity:.45" }, trackName(t, it.course)),
          h("button", { class: "btn small" + (Audio.muted.has(t) ? " on" : ""), title: "Mute", onclick: () => { Audio.muted.has(t) ? Audio.muted.delete(t) : Audio.muted.add(t); Audio.solo = null; Audio.applyGains(); drawTracks(); } }, "M"),
          h("button", { class: "btn small" + (Audio.solo === t ? " on" : ""), title: "Solo", onclick: () => { Audio.solo = Audio.solo === t ? null : t; Audio.applyGains(); drawTracks(); } }, "S")),
        c);
      trk.append(row);
      requestAnimationFrame(() => drawPeaks(c, meta.peaks[t] || [], tcolor(t), on));
    }
  }
  drawTracks();

  const bahs = meta.vars.filter(v => v[2] === 0), beats = meta.vars.filter(v => v[2] === 1);
  const roll = $("#roll", sec);
  const drawMain = p => {
    const W = cv.width = wrap.clientWidth * devicePixelRatio, H = cv.height = 150 * devicePixelRatio;
    cv.style.width = "100%"; cv.style.height = "150px";
    const g = cv.getContext("2d"); g.clearRect(0, 0, W, H);
    const all = mixPeaks(meta.peaks, t => (Audio.solo != null ? Audio.solo === t : !Audio.muted.has(t)));
    const x = s => s / meta.length * W;
    if (meta.loop) { g.fillStyle = "rgba(69,196,134,.07)"; g.fillRect(x(meta.loop[0]), 0, x(meta.loop[1]) - x(meta.loop[0]), H); }
    g.fillStyle = "#3d4a6a";
    const n = all.length;
    for (let i = 0; i < n; i++) { const a = all[i] * (H * 0.42); g.fillRect(i / n * W, H * 0.45 - a, Math.max(1, W / n), a * 2); }
    g.fillStyle = "rgba(90,169,255,.8)";
    for (const b of beats) g.fillRect(x(b[0]), H - 10 * devicePixelRatio, 2, 10 * devicePixelRatio);
    for (const b of bahs) { const X = x(b[0]); g.fillStyle = "#ffb02e"; g.beginPath(); g.moveTo(X - 6, 0); g.lineTo(X + 6, 0); g.lineTo(X, 11 * devicePixelRatio); g.fill(); g.fillRect(X - 0.5, 0, 1, H); }
    if (meta.loop) { g.fillStyle = "#45c486"; g.fillRect(x(meta.loop[0]), 0, 2, H); g.fillStyle = "#ff6464"; g.fillRect(x(meta.loop[1]) - 2, 0, 2, H); }
    g.fillStyle = "#fff"; g.fillRect(x(p), 0, 2, H);
  };
  const drawRoll = p => {
    const W = roll.width = roll.parentElement.clientWidth * devicePixelRatio - 32, H = roll.height = 120 * devicePixelRatio;
    roll.style.width = "100%"; roll.style.height = "120px";
    const g = roll.getContext("2d"); g.clearRect(0, 0, W, H);
    if (!meta.notes.length) return;
    const keys = meta.notes.map(n => n[2]); const lo = Math.min(...keys) - 2, hi = Math.max(...keys) + 2;
    const x = s => s / meta.length * W, y = k => H - (k - lo) / (hi - lo) * H;
    for (const [t, tr, k, v, d] of meta.notes) {
      g.fillStyle = tcolor(tr); g.globalAlpha = t <= p && p <= t + Math.max(d, .05) ? 1 : .55;
      g.fillRect(x(t), y(k) - 2, Math.max(2, x(d || .05)), 4);
    }
    g.globalAlpha = 1; g.fillStyle = "#fff"; g.fillRect(x(p), 0, 2, H);
  };
  wrap.onclick = e => {
    const r = wrap.getBoundingClientRect(); const p = (e.clientX - r.left) / r.width * meta.length;
    Audio.playing ? Audio.play(p) : (Audio.offset = p, Audio.ui());
  };
  const critter = makeCritter(crit);
  let lastP = -1;
  const varEls = $$(".var", vars);
  const onTick = p => {
    if (!document.body.contains(cv)) { Audio.onTick = Audio.onTick.filter(f => f !== onTick); return; }
    drawMain(p); drawRoll(p);
    const values = new Array(16).fill(null), hit = new Array(16).fill(false);
    for (const [t, tr, v, val] of meta.vars) {
      if (t > p) break;
      if (v < 16) { values[v] = val; hit[v] = p - t < 0.25; }
    }
    varEls.forEach((el, i) => { el.lastChild.textContent = values[i] == null ? "–" : values[i]; el.classList.toggle("hit", hit[i]); });
    const lastBah = lastBefore(bahs, p), lastBeat = lastBefore(beats, p);
    critter(p - (lastBah ? lastBah[0] : -9), lastBah ? lastBah[3] : 0, p - (lastBeat ? lastBeat[0] : -9));
    lastP = p;
  };
  Audio.onTick = Audio.onTick.filter(f => !f.player).concat(Object.assign(onTick, { player: true }));
  Audio.ui();
  window.addEventListener("resize", () => Audio.ui(), { once: true });
}
const lastBefore = (arr, p) => { let lo = 0, hi = arr.length - 1, best = null; while (lo <= hi) { const m = (lo + hi) >> 1; if (arr[m][0] <= p) { best = arr[m]; lo = m + 1; } else hi = m - 1; } return best; };
function mixPeaks(peaks, on) {
  let out = null;
  for (const [t, p] of Object.entries(peaks)) {
    if (!on(+t)) continue;
    if (!out) out = p.slice(); else for (let i = 0; i < Math.min(out.length, p.length); i++) out[i] = Math.max(out[i], p[i]);
  }
  return out || [];
}
function drawPeaks(c, peaks, color, on = true) {
  const W = c.width = c.clientWidth * devicePixelRatio, H = c.height = 34 * devicePixelRatio;
  const g = c.getContext("2d"); g.clearRect(0, 0, W, H); g.fillStyle = color; g.globalAlpha = on ? .85 : .25;
  const n = peaks.length;
  for (let i = 0; i < n; i++) { const a = Math.min(1, peaks[i]) * H * 0.45; g.fillRect(i / n * W, H / 2 - a, Math.max(1, W / n), a * 2 + 1); }
}
/* an original beat critter: a round fellow who nods on the beat and hops on a bah */
function makeCritter(c) {
  const g = c.getContext("2d");
  return (sinceBah, kind, sinceBeat) => {
    const W = c.width, H = c.height; g.clearRect(0, 0, W, H);
    const hop = sinceBah >= 0 && sinceBah < 0.45 ? Math.sin(Math.PI * sinceBah / 0.45) : 0;
    const nod = sinceBeat >= 0 && sinceBeat < 0.18 ? Math.sin(Math.PI * sinceBeat / 0.18) : 0;
    const ground = H - 22, r = 30;
    const squash = 1 - 0.18 * nod + 0.12 * hop, stretch = 1 + 0.15 * nod - 0.08 * hop;
    const cx = W / 2, cy = ground - r * squash - hop * 48;
    g.fillStyle = "rgba(0,0,0,.35)"; g.beginPath(); g.ellipse(cx, ground + 4, r * (1 - hop * .4), 6, 0, 0, 7); g.fill();
    g.fillStyle = "#c9733b"; g.beginPath(); g.ellipse(cx, cy, r * stretch, r * squash, 0, 0, 7); g.fill();
    g.fillStyle = "#f3d9b1"; g.beginPath(); g.ellipse(cx, cy + r * squash * .45, r * stretch * .7, r * squash * .45, 0, 0, 7); g.fill();
    g.fillStyle = "#fff";
    for (const s of [-1, 1]) { g.beginPath(); g.ellipse(cx + s * 11, cy - 6, 6, 8 * (hop ? .6 : 1), 0, 0, 7); g.fill(); }
    g.fillStyle = "#1a1a24";
    for (const s of [-1, 1]) { g.beginPath(); g.arc(cx + s * 11, cy - 5, 3, 0, 7); g.fill(); }
    if (hop > 0.05) {
      g.fillStyle = "#ffb02e"; g.font = "bold 16px Segoe UI"; g.textAlign = "center";
      g.fillText(kind === 1 ? "bah!" : kind === 2 ? "bah bah" : kind === 4 ? "BAH!" : "bah", cx, cy - r - 12);
    }
  };
}

/* ---------------------------------------------------------------- sequence */
async function tabSequence(sec) {
  const it = S.item;
  const fid = it.section === "file" ? it.index : it.file;
  const L = await api("/api/seq", { params: { file: fid } });
  S.listing = L;
  sec.innerHTML = "";
  const bar = h("div", { class: "seq-bar" });
  const view = h("div", { class: "code" });
  const ta = h("textarea", { class: "code hidden", spellcheck: "false" });
  const status = h("span", { class: "dim" });
  const follow = h("input", { type: "checkbox", checked: true });
  const editBtn = h("button", { class: "btn small" }, "Edit");
  const compileBtn = h("button", { class: "btn small accent hidden" }, "Compile & apply");
  const cancelBtn = h("button", { class: "btn small hidden" }, "Cancel");
  bar.append(editBtn, compileBtn, cancelBtn, h("label", { class: "chk" }, follow, "Follow playback"),
    h("span", { class: "dim" }, `file ${fid} · ${L.lines} lines · used by ${L.sounds.map(s => s.label).slice(0, 4).join(", ")}${L.sounds.length > 4 ? "…" : ""}`), status);
  sec.append(bar, view, ta,
    h("p", { class: "help" }, "Syntax: one command per line; ", h("span", { class: "mono" }, "Name:"), " exported label, ",
      h("span", { class: "mono" }, ".L0123:"), " local label; ", h("span", { class: "mono" }, "_r"), " random, ",
      h("span", { class: "mono" }, "_v"), " variable, ", h("span", { class: "mono" }, "_t/_tr/_tv"), " time argument; ",
      h("span", { class: "mono" }, "if "), "conditional; notes are ", h("span", { class: "mono" }, "cn4 velocity, length"), "."));
  const lines = L.text.split("\n");
  view.innerHTML = lines.map((l, i) => `<div class="ln" data-i="${i}"><span class="n">${i + 1}</span><span class="c">${hl(l)}</span></div>`).join("");
  const rows = $$(".ln", view);
  editBtn.onclick = () => { ta.value = L.text; view.classList.add("hidden"); ta.classList.remove("hidden"); editBtn.classList.add("hidden"); compileBtn.classList.remove("hidden"); cancelBtn.classList.remove("hidden"); ta.focus(); };
  cancelBtn.onclick = () => { ta.classList.add("hidden"); view.classList.remove("hidden"); editBtn.classList.remove("hidden"); compileBtn.classList.add("hidden"); cancelBtn.classList.add("hidden"); status.textContent = ""; };
  compileBtn.onclick = guard(async () => {
    const r = await api("/api/seq", { method: "POST", body: { file: fid, text: ta.value } });
    if (!r.ok) {
      status.innerHTML = `<span class="err">${esc(r.error)}</span>`;
      const pos = ta.value.split("\n").slice(0, r.line - 1).join("\n").length + 1;
      ta.focus(); ta.setSelectionRange(pos, pos + (ta.value.split("\n")[r.line - 1] || "").length);
      return;
    }
    toast(`Compiled: ${r.size} bytes${r.moved.length ? " · start offsets moved for " + r.moved.join(", ") : ""}`, "good");
    S.summary.dirty = true; sec.dataset.for = ""; showTab("sequence");
    $$(".tab").forEach(s => { if (s.id !== "tab-sequence") s.dataset.for = ""; });
  });
  // follow playback: each track's current command
  const steps = {};
  if (S.render && S.render.steps) for (const [t, tr, off] of S.render.steps) (steps[tr] ||= []).push([t, off]);
  let lastHl = [];
  const onTick = p => {
    if (!document.body.contains(view)) { Audio.onTick = Audio.onTick.filter(f => f !== onTick); return; }
    if (!S.render || Audio.label !== it.label || view.classList.contains("hidden")) return;
    for (const r of lastHl) { r.classList.remove("hl"); r.style.boxShadow = ""; }
    lastHl = [];
    let first = null;
    for (const [tr, arr] of Object.entries(steps)) {
      const s = lastBefore(arr, p); if (!s) continue;
      const line = L.lineOf[s[1]]; if (line == null) continue;
      const row = rows[line]; if (!row) continue;
      row.classList.add("hl"); row.style.boxShadow = `inset 3px 0 0 ${tcolor(+tr)}`; lastHl.push(row);
      if (first == null) first = row;
    }
    if (follow.checked && first && Audio.playing) {
      const top = first.offsetTop - view.scrollTop;
      if (top < 40 || top > view.clientHeight - 60) view.scrollTop = first.offsetTop - view.clientHeight / 3;
    }
  };
  Audio.onTick = Audio.onTick.filter(f => !f.seq).concat(Object.assign(onTick, { seq: true }));
  if (it.startOffset != null && L.lineOf[it.startOffset] != null) {
    const r = rows[L.lineOf[it.startOffset]]; if (r) setTimeout(() => view.scrollTop = r.offsetTop - 60, 0);
  }
}
function hl(line) {
  const [code, ...cm] = line.split(";");
  const comment = cm.length ? `<span class="cmt">;${esc(cm.join(";"))}</span>` : "";
  if (/^\S.*:$/.test(code.trim()) && !code.startsWith(" ")) return `<span class="lab">${esc(code)}</span>${comment}`;
  const m = code.match(/^(\s*)(if\s+)?(\S+)(.*)$/);
  if (!m) return esc(code) + comment;
  const [, ind, iff, word, rest] = m;
  const isNote = /^(cn|cs|dn|ds|en|fn|fs|gn|gs|an|as|bn)(m1|\d)(_|$)/.test(word);
  const args = esc(rest).replace(/(-?\b0x[0-9a-fA-F]+\b|-?\b\d+\b)/g, '<span class="num">$1</span>').replace(/(\.L[0-9A-F]{4}|\b[A-Za-z_][\w]*\b)(?![^<]*>)/g, m2 => /^(\d|0x)/.test(m2) ? m2 : `<span class="lab">${m2}</span>`);
  return `${ind}${iff ? '<span class="kw">if </span>' : ""}<span class="${isNote ? "nt" : "kw"}">${esc(word)}</span>${args}${comment}`;
}

/* ---------------------------------------------------------------- bank */
async function tabBank(sec) {
  const it = S.item;
  const B = await api("/api/bank", { params: { index: it.index } });
  sec.innerHTML = "";
  let sel = { program: B.instruments.length ? B.instruments[0].program : 0, key: 0, vel: 0 };
  const left = h("div", { class: "card" }, h("h3", {}, "Instruments"));
  const right = h("div", {});
  sec.append(h("div", { class: "bank-grid" }, left, right));
  if (B.users.length) sec.append(h("p", { class: "dim" }, "Used by " + B.users.slice(0, 8).join(", ") + (B.users.length > 8 ? "…" : "")));
  const drawList = () => {
    left.querySelectorAll(".inst").forEach(e => e.remove());
    for (const ins of B.instruments) for (const [ki, k] of ins.keys.entries()) for (const [vi, v] of k.velocity.entries()) {
      const on = sel.program === ins.program && sel.key === ki && sel.vel === vi;
      left.append(h("div", { class: "inst" + (on ? " sel" : ""), onclick: () => { sel = { program: ins.program, key: ki, vel: vi }; drawList(); drawRegion(); } },
        h("b", {}, `prg ${ins.program}`), h("span", { class: "dim" }, `  keys ${k.lo}–${k.hi}${k.velocity.length > 1 ? ` · vel ${v.lo}–${v.hi}` : ""} · wave ${v.wave}`)));
    }
  };
  const drawRegion = () => {
    right.innerHTML = "";
    const ins = B.instruments.find(i => i.program === sel.program);
    if (!ins) { right.append(h("div", { class: "card dim" }, "This bank has no instruments.")); return; }
    const reg = ins.keys[sel.key].velocity[sel.vel];
    const card = h("div", { class: "card" }, h("h3", {}, `Program ${sel.program} · region ${sel.key}.${sel.vel}`));
    const fields = h("div", { class: "fields" });
    const spec = [["wave", "Wave (index in the wave list)"], ["key", "Original key"], ["volume", "Volume"], ["pan", "Pan (64 = centre)"], ["pitch", "Pitch (ratio)"],
      ["attack", "Attack"], ["decay", "Decay"], ["sustain", "Sustain"], ["hold", "Hold"], ["release", "Release"]];
    for (const [name, label] of spec) {
      const val = ["attack", "decay", "sustain", "hold", "release"].includes(name) ? reg.adshr[name] : reg[name];
      const editable = reg.editable.includes(name);
      const inp = h("input", { type: "text", value: name === "pitch" ? (+val).toFixed(4) : val, readonly: !editable });
      if (editable) inp.onchange = guard(async () => {
        await api("/api/bank", { method: "POST", body: { index: it.index, program: sel.program, key: sel.key, vel: sel.vel, name, value: inp.value } });
        if (["attack", "decay", "sustain", "hold", "release"].includes(name)) reg.adshr[name] = +inp.value; else reg[name] = +inp.value;
        S.summary.dirty = true; toast(`${label} = ${inp.value}`, "good"); drawList();
      });
      const row = h("div", { class: "field-row" }, inp);
      if (name === "key") row.append(h("span", { class: "help" }, noteName(+val)));
      if (name === "wave" && B.waves[val]) {
        const w = B.waves[val];
        row.append(h("a", { onclick: () => navRef("wavearc", w.warc) }, `${w.warcLabel || "archive"} #${w.index}`),
          h("span", { class: "help" }, w.seconds != null ? `${w.seconds}s · ${w.rate} Hz${w.loop ? " · loops" : ""}` : ""));
      }
      fields.append(h("label", {}, label), row);
    }
    card.append(fields);
    right.append(card);
    // piano
    const pc = h("div", { class: "card" }, h("h3", {}, "Play"));
    const vel = h("input", { type: "range", min: 1, max: 127, value: 100 });
    const len = h("input", { type: "range", min: 0.2, max: 6, step: 0.1, value: 1.2 });
    pc.append(h("div", { class: "states" }, h("span", { class: "dim" }, "Velocity"), vel, h("span", { class: "dim" }, "Length"), len,
      h("span", { class: "help" }, "Keys ", h("span", { class: "kbd" }, "A"), "–", h("span", { class: "kbd" }, "K"), " play the octave from C4 (", h("span", { class: "kbd" }, "W E T Y U"), " sharps)")));
    const piano = buildPiano(36, 96, k => guard(async () => {
      await preview(audioUrl("/api/note", { bank: it.index, program: sel.program, key: k, velocity: vel.value, seconds: len.value }));
    })());
    pc.append(piano);
    right.append(pc);
    const wl = h("div", { class: "card" }, h("h3", {}, "Waves this bank uses"));
    const t = h("table", { class: "t" }, h("tr", {}, ...["#", "Wave archive", "Wave", "Length", ""].map(x => h("th", {}, x))));
    B.waves.forEach((w, i) => t.append(h("tr", {}, h("td", {}, i), h("td", {}, h("a", { onclick: () => navRef("wavearc", w.warc) }, w.warcLabel || hex(w.warc))),
      h("td", {}, w.index), h("td", {}, w.seconds != null ? w.seconds + " s" : "?"),
      h("td", {}, h("button", { class: "btn small", onclick: guard(() => preview(audioUrl("/api/wave", { warc: w.warc & 0xFFFFFF, wave: w.index }))) }, "▶")))));
    wl.append(t); right.append(wl);
  };
  drawList(); drawRegion();
}
const NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const noteName = k => `${NOTE_NAMES[k % 12]}${Math.floor(k / 12) - 1}`;
function buildPiano(lo, hi, play) {
  const wrap = h("div", { class: "piano" }), keys = h("div", { class: "keys" });
  wrap.append(keys);
  let x = 0; const els = {};
  for (let k = lo; k <= hi; k++) {
    const black = [1, 3, 6, 8, 10].includes(k % 12);
    const el = h("div", { class: black ? "b" : "w", title: noteName(k) });
    if (black) el.style.left = (x - 8) + "px"; else { el.style.left = x + "px"; if (k % 12 === 0) el.append(h("span", {}, noteName(k))); x += 24; }
    el.onmousedown = () => { el.classList.add("down"); play(k); setTimeout(() => el.classList.remove("down"), 180); };
    keys.append(el); els[k] = el;
  }
  keys.style.width = x + "px";
  const map = { a: 60, w: 61, s: 62, e: 63, d: 64, f: 65, t: 66, g: 67, y: 68, h: 69, u: 70, j: 71, k: 72 };
  const onKey = e => {
    if (!document.body.contains(wrap)) { document.removeEventListener("keydown", onKey); return; }
    if (e.repeat || e.target.matches("input, textarea, select")) return;
    const k = map[e.key.toLowerCase()];
    if (k != null && els[k]) { els[k].classList.add("down"); play(k); setTimeout(() => els[k].classList.remove("down"), 180); }
  };
  document.addEventListener("keydown", onKey);
  setTimeout(() => { if (els[60]) wrap.scrollLeft = els[60].offsetLeft - 200; }, 0);
  return wrap;
}

/* ---------------------------------------------------------------- waves */
async function tabWaves(sec) {
  const it = S.item;
  const W = await api("/api/wavearc", { params: { index: it.index } });
  sec.innerHTML = "";
  const card = h("div", { class: "card" }, h("h3", {}, `${W.waves.length} waves · file ${W.file}`));
  const t = h("table", { class: "t" }, h("tr", {}, ...["#", "Length", "Rate", "Ch", "Loop", "Size", ""].map(x => h("th", {}, x))));
  for (const w of W.waves) {
    t.append(h("tr", {}, h("td", {}, w.index), h("td", {}, w.seconds + " s"), h("td", {}, w.rate), h("td", {}, w.channels),
      h("td", {}, w.loop ? `from ${w.loopStart}` : "—"), h("td", {}, (w.bytes / 1024).toFixed(0) + " KB"),
      h("td", {}, h("div", { class: "states" },
        h("button", { class: "btn small", onclick: guard(async () => {
          await Audio.load({ 0: audioUrl("/api/wave", { warc: it.index, wave: w.index }) }, { label: `${W.label} #${w.index}`, loop: w.loop ? [w.loopStart / w.rate, w.samples / w.rate] : null });
          Audio.play(0);
        }) }, "▶ Play"),
        h("button", { class: "btn small", onclick: guard(async () => { const r = await api("/api/wave/export", { method: "POST", body: { warc: it.index, wave: w.index } }); if (!r.cancelled) toast("Exported " + r.path, "good"); }) }, "Export WAV"),
        h("button", { class: "btn small", onclick: guard(async () => {
          const r = await api("/api/wave/replace", { method: "POST", body: { warc: it.index, wave: w.index, keepLoop: true } });
          if (r && !r.cancelled) { toast("Replaced wave " + w.index, "good"); S.summary.dirty = true; sec.dataset.for = ""; showTab("waves"); }
        }) }, "Replace…")))));
  }
  card.append(t);
  sec.append(card, h("p", { class: "dim" }, "Replace takes a WAV (its loop from a smpl chunk, else the old wave's loop setting) or a Switch BFWAV. Banks and wave sounds keep pointing at the same wave number."));
}

/* ---------------------------------------------------------------- group */
const LOAD_BITS = [[1, "Sequence"], [2, "Wave sound set"], [4, "Bank"], [8, "Wave archive"]];
async function tabGroup(sec) {
  const it = S.item;
  const G = await api("/api/group", { params: { index: it.index } });
  sec.innerHTML = "";
  const items = h("div", { class: "card" }, h("h3", {}, `Items loaded with the group (${G.items.length})`));
  const t = h("table", { class: "t" }, h("tr", {}, ...["Item", "Load flags", ""].map(x => h("th", {}, x))));
  G.items.forEach((x, k) => {
    const all = h("input", { type: "checkbox", checked: x.flags === 0xFFFFFFFF });
    const bits = LOAD_BITS.map(([b, name]) => { const c = h("input", { type: "checkbox", checked: x.flags !== 0xFFFFFFFF && (x.flags & b) !== 0, disabled: x.flags === 0xFFFFFFFF }); c.dataset.b = b; return h("label", { class: "chk" }, c, name); });
    const save = guard(async () => {
      const flags = all.checked ? 0xFFFFFFFF : bits.reduce((a, l) => a | ($("input", l).checked ? +$("input", l).dataset.b : 0), 0);
      await api("/api/group", { method: "POST", body: { index: it.index, item: k, flags } });
      x.flags = flags; S.summary.dirty = true; toast(`Load flags = ${hex(flags)}`, "good"); sec.dataset.for = ""; showTab("group");
    });
    all.onchange = save; bits.forEach(l => $("input", l).onchange = save);
    t.append(h("tr", {}, h("td", {}, h("a", { onclick: () => navRef(x.id >> 24 === 1 ? "sound" : x.id >> 24 === 3 ? "bank" : x.id >> 24 === 5 ? "wavearc" : x.id >> 24 === 2 ? "soundset" : "", x.id) }, x.label || hex(x.id))),
      h("td", {}, h("div", { class: "states" }, h("label", { class: "chk" }, all, "Everything"), ...bits)), h("td", { class: "dim mono" }, hex(x.flags))));
  });
  items.append(t);
  const files = h("div", { class: "card" }, h("h3", {}, `Files listed (${G.files.length})`));
  files.append(h("div", { class: "dim", style: "columns: 3" }, ...G.files.map(f => h("div", {}, h("a", { onclick: () => select("file", f.file) }, `File ${f.file}`), " ", f.label))));
  sec.append(items, files);
}

/* ---------------------------------------------------------------- hex */
async function tabHex(sec, offset = 0) {
  const it = S.item;
  const r = await api("/api/hex", { params: { file: it.index, offset } });
  sec.innerHTML = "";
  const bytes = r.hex.match(/../g) || [];
  let out = "";
  for (let i = 0; i < bytes.length; i += 16) {
    const row = bytes.slice(i, i + 16);
    const ascii = row.map(b => { const c = parseInt(b, 16); return c >= 32 && c < 127 ? String.fromCharCode(c) : "."; }).join("");
    out += `${(offset + i).toString(16).padStart(8, "0")}  ${row.join(" ").padEnd(48)}  ${esc(ascii)}\n`;
  }
  sec.append(h("div", { class: "seq-bar" },
    h("button", { class: "btn small", disabled: offset <= 0, onclick: () => tabHex(sec, Math.max(0, offset - 4096)) }, "◀ Back"),
    h("button", { class: "btn small", disabled: offset + 4096 >= r.size, onclick: () => tabHex(sec, offset + 4096) }, "Next ▶"),
    h("span", { class: "dim" }, `${r.size} bytes · showing ${offset}–${Math.min(r.size, offset + 4096)}`)),
    h("pre", { class: "code", style: "padding:8px 12px" }, out));
}

/* ---------------------------------------------------------------- add music */
function openAddMusic() {
  if (!S.summary || !S.summary.open) return;
  const used = new Set(S.tree.filter(x => x.s === "sound").map(x => x.l));
  let slot = 0; while (used.has(`CUSTOM_BGM_${String(slot).padStart(2, "0")}`) && slot < 99) slot++;
  const st = { song: null, buf: null, yoshi: null, slot, fast: false, volume: 80, loop: true, loopStart: null, bpm: null, offset: null,
    bahMode: "auto", bahs: [], tool: "seek", src: null, startAt: 0, pos: 0, playing: false, clicks: true, taps: [] };
  const box = $("#modal-box"); box.innerHTML = ""; $("#modal").classList.remove("hidden");
  const close = () => { stopPrev(); $("#modal").classList.add("hidden"); document.removeEventListener("keydown", onKey); };
  box.append(h("h2", {}, "Add music"), h("p", { class: "dim" }, "Pick a WAV, set its loop and beat, place the bahs if you like, then add it. The song becomes CUSTOM_BGM_nn, which the Custom Music sprite plays in levels."));
  const body = h("div", {}); box.append(body);
  const foot = h("div", { class: "modal-foot" }, h("button", { class: "btn", onclick: close }, "Cancel"));
  const addBtn = h("button", { class: "btn accent", disabled: true }, "Add to archive");
  foot.append(addBtn); box.append(foot);

  const pick = h("button", { class: "btn accent" }, "Choose WAV…");
  const songName = h("span", { class: "dim" }, "no song yet");
  body.append(h("div", { class: "seq-bar" }, pick, songName));
  const editor = h("div", { class: "hidden" }); body.append(editor);
  pick.onclick = guard(async () => {
    const r = await api("/api/pick", { method: "POST", body: { kind: "wav", title: "Choose the song" } });
    if (!r.path) return;
    songName.textContent = "analysing…";
    const a = await api("/api/analyze", { method: "POST", body: { path: r.path } });
    st.song = a; st.bpm = a.bpm; st.offset = a.offset; st.loopStart = a.loopStart ?? 0;
    songName.textContent = `${a.name} · ${fmtTime(a.seconds)} · ${a.rate} Hz · ${a.channels} ch`;
    st.buf = await Audio.decode(audioUrl("/api/local", { path: a.path }));
    buildEditor(); addBtn.disabled = false;
  });

  function buildEditor() {
    editor.classList.remove("hidden"); editor.innerHTML = "";
    const a = st.song;
    const tools = h("div", { class: "states" }, h("span", { class: "dim" }, "Click tool:"));
    const toolBtn = (t, name) => { const b = h("button", { class: "btn small" + (st.tool === t ? " on" : ""), onclick: () => { st.tool = t; $$(".tool", tools).forEach(x => x.classList.toggle("on", x === b)); } }, name); b.classList.add("tool"); return b; };
    tools.append(toolBtn("seek", "Seek"), toolBtn("loop", "Set loop start"), toolBtn("beat", "Set first beat"), toolBtn("bah", "Place bah"));
    const play = h("button", { class: "btn accent small" }, "▶ Preview");
    const seam = h("button", { class: "btn small", title: "Plays the last seconds, then the loop start" }, "Hear the loop seam");
    const clicks = h("input", { type: "checkbox", checked: st.clicks });
    tools.append(play, seam, h("label", { class: "chk" }, clicks, "Beat clicks"));
    const wrap = h("div", { class: "wave-wrap", style: "margin-top:8px" }); const cv = h("canvas", { height: 170 }); wrap.append(cv);
    const posEl = h("span", { class: "mono dim" }, "0:00.0");
    editor.append(tools, wrap, h("div", { class: "seq-bar", style: "margin-top:6px" }, posEl,
      h("span", { class: "help" }, "Keys while previewing: ", h("span", { class: "kbd" }, "B"), " bah here · ", h("span", { class: "kbd" }, "T"), " tap tempo · ", h("span", { class: "kbd" }, "F"), " first beat here · right-click a bah to remove it")));

    const f = h("div", { class: "fields", style: "margin-top:12px" });
    const inp = (v, w = 90) => h("input", { type: "text", value: v ?? "", style: `width:${w}px` });
    const slotI = inp(st.slot, 60), volI = inp(st.volume, 60), bpmI = inp(st.bpm, 70), offI = inp(st.offset, 80), lsI = inp(st.loopStart, 110);
    const fastC = h("input", { type: "checkbox" }), loopC = h("input", { type: "checkbox", checked: st.loop });
    const label = h("span", { class: "mono" });
    const updLabel = () => { label.textContent = `CUSTOM_BGM_${String(+slotI.value || 0).padStart(2, "0")}${fastC.checked ? "_FAST" : ""}`; };
    slotI.oninput = updLabel; fastC.onchange = updLabel; updLabel();
    bpmI.onchange = () => { st.bpm = Math.max(1, Math.round(+bpmI.value) || st.bpm); bpmI.value = st.bpm; draw(); };
    offI.onchange = () => { st.offset = Math.max(0, +offI.value || 0); draw(); };
    lsI.onchange = () => { st.loopStart = Math.max(0, Math.min(a.samples - 1, Math.round(+lsI.value) || 0)); lsI.value = st.loopStart; draw(); };
    loopC.onchange = () => { st.loop = loopC.checked; draw(); };
    const bahMode = h("select", {}, h("option", { value: "auto" }, "Beat 3 of every fourth bar"), h("option", { value: "custom" }, "Where I place them"));
    bahMode.onchange = () => { st.bahMode = bahMode.value; draw(); drawBahs(); };
    const bahKind = h("select", {}, h("option", { value: 3 }, "3 · single bah"), h("option", { value: 1 }, "1 · first of a bah-bah"), h("option", { value: 4 }, "4 · second of a bah-bah"), h("option", { value: 2 }, "2 · underground bah"));
    const bahList = h("div", { class: "bah-list" });
    const importB = h("button", { class: "btn small" }, "Import Audacity labels…");
    importB.onclick = guard(async () => {
      const r = await api("/api/pick", { method: "POST", body: { kind: "txt", title: "Bah times or Audacity labels" } });
      if (!r.path) return;
      const list = await api("/api/bahs", { method: "POST", body: { path: r.path } });
      st.bahs = list.map(([t, k]) => ({ t, k })); st.bahMode = "custom"; bahMode.value = "custom"; draw(); drawBahs();
    });
    const clearB = h("button", { class: "btn small danger", onclick: () => { st.bahs = []; draw(); drawBahs(); } }, "Clear");
    const yoshiB = h("button", { class: "btn small" }, "Choose Yoshi layer…"), yoshiName = h("span", { class: "dim" }, "none");
    yoshiB.onclick = guard(async () => {
      const r = await api("/api/pick", { method: "POST", body: { kind: "wav", title: "Yoshi layer (same length as the song)" } });
      if (r.path) { st.yoshi = r.path; yoshiName.textContent = r.path.split(/[\\/]/).pop(); }
    });
    const row = (...k) => h("div", { class: "field-row" }, ...k);
    f.append(
      h("label", {}, "Slot"), row(slotI, h("label", { class: "chk" }, fastC, "Hurry-up version"), label),
      h("label", {}, "Volume (0–255)"), row(volI, h("span", { class: "help" }, "80 is the overworld's")),
      h("label", {}, "Loop"), row(h("label", { class: "chk" }, loopC, "Loops"), lsI, h("span", { class: "help" }, "loop start in samples; the loop ends at the end of the file")),
      h("label", {}, "Tempo (BPM)"), row(bpmI, h("button", { class: "btn small", onclick: () => { st.bpm = st.song.bpm; bpmI.value = st.bpm; draw(); } }, "Estimate"), h("button", { class: "btn small", onclick: () => { st.bpm *= 2; bpmI.value = st.bpm; draw(); } }, "×2"), h("button", { class: "btn small", onclick: () => { st.bpm = Math.max(1, Math.round(st.bpm / 2)); bpmI.value = st.bpm; draw(); } }, "÷2")),
      h("label", {}, "First beat (seconds)"), row(offI, h("button", { class: "btn small", onclick: () => { st.offset = st.song.offset; offI.value = st.offset; draw(); } }, "Estimate")),
      h("label", {}, "Bahs (enemy hops)"), row(bahMode, bahKind, importB, clearB),
      h("label", {}, ""), bahList,
      h("label", {}, "Yoshi layer"), row(yoshiB, yoshiName, h("span", { class: "help" }, "optional: plays on top while Mario rides Yoshi (bongos etc.)")));
    editor.append(f);

    const x = (t, W) => t / a.seconds * W;
    const autoBahs = () => { const out = [], spb = 60 / st.bpm; for (let t = st.offset + 14 * spb; t < a.seconds; t += 16 * spb) out.push({ t, k: 3 }); return out; };
    function draw() {
      const W = cv.width = wrap.clientWidth * devicePixelRatio, H = cv.height = 170 * devicePixelRatio;
      cv.style.width = "100%"; cv.style.height = "170px";
      const g = cv.getContext("2d"); g.clearRect(0, 0, W, H);
      const p = a.peaks, n = p.length;
      if (st.loop) { g.fillStyle = "rgba(69,196,134,.07)"; g.fillRect(x(st.loopStart / a.rate, W), 0, W, H); }
      g.fillStyle = "#3d4a6a";
      for (let i = 0; i < n; i++) { const v = p[i] * H * .4; g.fillRect(i / n * W, H * .5 - v, Math.max(1, W / n), v * 2); }
      const spb = 60 / st.bpm;
      for (let k = 0, t = st.offset; t < a.seconds; k++, t = st.offset + k * spb) {
        const bar = k % 4 === 0; g.fillStyle = bar ? "rgba(90,169,255,.55)" : "rgba(90,169,255,.18)";
        g.fillRect(x(t, W), bar ? 0 : H - 14 * devicePixelRatio, bar ? 1.5 : 1, bar ? H : 14 * devicePixelRatio);
      }
      const bahs = st.bahMode === "auto" ? autoBahs() : st.bahs;
      for (const b of bahs) {
        const X = x(b.t, W); g.fillStyle = "#ffb02e"; g.beginPath(); g.moveTo(X - 7, 0); g.lineTo(X + 7, 0); g.lineTo(X, 13 * devicePixelRatio); g.fill();
        g.fillRect(X - .5, 0, 1, H); g.font = `${11 * devicePixelRatio}px Segoe UI`; g.fillText(b.k, X + 4, 24 * devicePixelRatio);
      }
      if (st.loop) { g.fillStyle = "#45c486"; g.fillRect(x(st.loopStart / a.rate, W), 0, 2, H); }
      g.fillStyle = "#ff6464"; g.fillRect(W - 2, 0, 2, H);
      g.fillStyle = "#fff"; g.fillRect(x(st.pos, W), 0, 2, H);
      lsI.value = st.loopStart; offI.value = (+st.offset).toFixed(3);
    }
    function drawBahs() {
      bahList.innerHTML = "";
      if (st.bahMode === "auto") { bahList.append(h("span", { class: "help" }, `${autoBahs().length} hops, on beat 3 of every fourth bar from the first beat`)); return; }
      if (!st.bahs.length) bahList.append(h("span", { class: "help" }, "Use the Place bah tool, or press B while previewing, at each shout."));
      st.bahs.sort((p, q) => p.t - q.t).forEach((b, i) => bahList.append(h("span", { class: "bah" }, `${b.t.toFixed(2)}s · ${b.k}`, h("button", { onclick: () => { st.bahs.splice(i, 1); draw(); drawBahs(); } }, "×"))));
    }
    const timeAt = e => { const r = wrap.getBoundingClientRect(); return Math.max(0, Math.min(a.seconds, (e.clientX - r.left) / r.width * a.seconds)); };
    wrap.onclick = e => {
      const t = timeAt(e);
      if (st.tool === "loop") { st.loopStart = Math.round(t * a.rate); st.loop = loopC.checked = true; }
      else if (st.tool === "beat") { st.offset = +t.toFixed(3); }
      else if (st.tool === "bah") { st.bahMode = bahMode.value = "custom"; st.bahs.push({ t, k: +bahKind.value }); drawBahs(); }
      else { st.pos = t; if (st.playing) startPrev(t); }
      draw();
    };
    wrap.oncontextmenu = e => {
      e.preventDefault(); if (st.bahMode !== "custom" || !st.bahs.length) return;
      const t = timeAt(e); let best = 0;
      st.bahs.forEach((b, i) => { if (Math.abs(b.t - t) < Math.abs(st.bahs[best].t - t)) best = i; });
      if (Math.abs(st.bahs[best].t - t) < a.seconds / 80) { st.bahs.splice(best, 1); draw(); drawBahs(); }
    };
    play.onclick = () => { st.playing ? stopPrev() : startPrev(st.pos); };
    seam.onclick = () => { if (!st.loop) { toast("Turn the loop on first"); return; } startPrev(Math.max(0, a.seconds - 3), true); };
    clicks.onchange = () => { st.clicks = clicks.checked; };
    st.draw = draw; st.posEl = posEl; st.play = play; st.drawBahs = drawBahs; st.bahKind = bahKind; st.bahModeEl = bahMode; st.bpmI = bpmI;
    addBtn.onclick = guard(async () => {
      stopPrev();
      const body = { slot: +slotI.value || 0, fast: fastC.checked, path: a.path, volume: Math.max(0, Math.min(255, +volI.value || 80)),
        loop: st.loop, loopStart: st.loop ? st.loopStart : null, bpm: st.bpm, offset: +st.offset,
        bahs: st.bahMode === "custom" ? st.bahs.map(b => [b.t, b.k]) : null, yoshi: st.yoshi };
      addBtn.disabled = true;
      try {
        const r = await api("/api/music/add", { method: "POST", body });
        close();
        toast(`Added ${r.label} (${r.bpm} BPM). Save to write the archive.`, "good");
        await refresh(true);
        const it = S.tree.find(x2 => x2.s === "sound" && x2.l === r.label);
        if (it) { S.cat = "custom"; renderCats(); select("sound", it.i, "player"); }
      } finally { addBtn.disabled = false; }
    });
    draw(); drawBahs();
  }

  /* preview with a click track and bah blips, scheduled ahead */
  let timer = null, seamMode = false;
  function startPrev(from, seamTest = false) {
    stopPrev(); Audio.ensure();
    const ctx = Audio.ctx, a = st.song;
    const src = ctx.createBufferSource(); src.buffer = st.buf;
    if (st.loop) { src.loop = true; src.loopStart = st.loopStart / a.rate; src.loopEnd = st.buf.duration; }
    src.connect(Audio.master);
    const when = ctx.currentTime + 0.05;
    src.start(when, from); st.src = src; st.startAt = when - from; st.playing = true; seamMode = seamTest;
    st.play.textContent = "❚❚ Stop";
    let scheduledTo = when;
    const songT = tt => {
      let p = tt - st.startAt;
      if (st.loop && p > a.seconds) { const ls = st.loopStart / a.rate; p = ls + (p - ls) % (a.seconds - ls); }
      return p;
    };
    // events (song time) in [p0, p1): beats and bahs, as they are now set
    const events = (p0, p1) => {
      const out = [], spb = 60 / st.bpm;
      if (st.clicks) for (let k = Math.max(0, Math.ceil((p0 - st.offset) / spb)); st.offset + k * spb < p1; k++) out.push([st.offset + k * spb, k % 4 === 0 ? "bar" : "beat"]);
      const bahs = st.bahMode === "auto" ? (() => { const r = []; for (let t = st.offset + 14 * spb; t < a.seconds; t += 16 * spb) r.push(t); return r; })() : st.bahs.map(b => b.t);
      for (const t of bahs) if (t >= p0 && t < p1) out.push([t, "bah"]);
      return out;
    };
    timer = setInterval(() => {
      const now = ctx.currentTime, horizon = now + 0.2;
      // schedule [scheduledTo, horizon) in context time, split where the song loops
      let t0 = Math.max(scheduledTo, now);
      while (t0 < horizon) {
        const p0 = songT(t0);
        let p1 = p0 + (horizon - t0);
        if (st.loop && p1 > a.seconds) p1 = a.seconds;
        for (const [pt, kind] of events(p0, p1)) {
          const at = t0 + (pt - p0);
          if (kind === "bah") bah(at); else blip(at, kind === "bar" ? 1500 : 1000, .05);
        }
        t0 += Math.max(p1 - p0, 0.001);
      }
      scheduledTo = horizon;
      st.pos = songT(now);
      if (st.posEl) st.posEl.textContent = fmtTime(st.pos);
      if (st.draw) st.draw();
      if (!st.loop && st.pos >= a.seconds) stopPrev();
    }, 40);
  }
  function stopPrev() {
    if (timer) clearInterval(timer); timer = null;
    if (st.src) { try { st.src.stop(); } catch { } } st.src = null; st.playing = false;
    if (st.play) st.play.textContent = "▶ Preview";
  }
  function blip(t, f, d) {
    const ctx = Audio.ctx, o = ctx.createOscillator(), g = ctx.createGain();
    o.frequency.value = f; g.gain.setValueAtTime(.18, t); g.gain.exponentialRampToValueAtTime(.001, t + d);
    o.connect(g).connect(Audio.master); o.start(t); o.stop(t + d + .02);
  }
  function bah(t) {
    const ctx = Audio.ctx, o = ctx.createOscillator(), g = ctx.createGain(), f = ctx.createBiquadFilter();
    o.type = "sawtooth"; o.frequency.setValueAtTime(220, t); o.frequency.exponentialRampToValueAtTime(160, t + .22);
    f.type = "bandpass"; f.frequency.value = 900; f.Q.value = 3;
    g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(.35, t + .02); g.gain.exponentialRampToValueAtTime(.001, t + .25);
    o.connect(f).connect(g).connect(Audio.master); o.start(t); o.stop(t + .3);
  }
  const onKey = e => {
    if (!st.song || e.target.matches("input, textarea, select")) return;
    const k = e.key.toLowerCase();
    if (k === "b" && st.playing) { st.bahMode = "custom"; st.bahModeEl.value = "custom"; st.bahs.push({ t: st.pos, k: +st.bahKind.value }); st.draw(); st.drawBahs(); }
    if (k === "f") { st.offset = +st.pos.toFixed(3); st.draw(); }
    if (k === "t") {
      const now = performance.now(); st.taps = st.taps.filter(x2 => now - x2 < 3000); st.taps.push(now);
      if (st.taps.length >= 4) { const d = (st.taps[st.taps.length - 1] - st.taps[0]) / (st.taps.length - 1); st.bpm = Math.round(60000 / d); st.bpmI.value = st.bpm; st.draw(); toast(`Tapped ${st.bpm} BPM`); }
    }
    if (k === "escape") close();
  };
  document.addEventListener("keydown", onKey);
}

/* ---------------------------------------------------------------- start */
(async () => {
  try {
    await refresh(false);
    const open = new URLSearchParams(location.search).get("open");
    if (open && !S.summary.open) await openArchive(open);
  } catch (e) { toast(e.message, "bad"); }
})();
