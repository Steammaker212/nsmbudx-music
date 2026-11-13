"""nsmbudx-music Studio: a local web app for the sound archive.

The server holds the open archive (an edit.Editor plus a parsed Bfsar of its last
build), renders sounds, and serves the page in static/. It listens on 127.0.0.1 only
and every API call must carry the session token from the page URL.
"""
import json
import mimetypes
import os
import secrets
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import numpy as np

from .. import __version__, banks, edit, fwav, info, render, seq
from .. import formats as F
from ..audio import estimate_beat, pcm_to_fwav, read_wav, write_wav
from ..bfsar import Bfsar
from ..cli import COURSE_NAMES, course_id
from . import dialogs

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
VANILLA_SIZE = 354262944

GROUP_LOAD_FLAGS = {1: "sequence", 2: "wave sound set", 4: "bank", 8: "wave archive"}


class Job:
    def __init__(self, title):
        self.id, self.title = uuid.uuid4().hex[:12], title
        self.state, self.log, self.result, self.error, self.started = "running", [], None, None, time.time()

    def json(self):
        return {"id": self.id, "title": self.title, "state": self.state, "log": self.log[-8:],
                "result": self.result, "error": self.error, "seconds": round(time.time() - self.started, 1)}


class Project:
    """The open archive."""

    def __init__(self):
        self.path, self.bf, self.ed, self.waves = None, None, None, None
        self.stale, self.dirty, self.version = False, False, 0
        self.lock = threading.RLock()
        self.renders, self.local_files = {}, set()

    # --- state ---------------------------------------------------------------
    def load(self, data, path):
        with self.lock:
            self.bf = Bfsar(data)
            self.ed = edit.Editor(self.bf)
            self.waves = render.Waves(self.bf)
            self.path, self.stale, self.renders = path, False, {}
            self.version += 1

    def changed(self):
        with self.lock:
            self.stale, self.dirty = True, True
            self.version += 1
            self.renders = {}

    def sync(self):
        """Rebuild the archive after edits so reads and playback see them."""
        with self.lock:
            if self.stale:
                path, dirty = self.path, self.dirty
                self.load(self.ed.build(), path)
                self.dirty = dirty

    def require(self):
        if self.bf is None:
            raise ApiError("no archive is open")

    # --- views ---------------------------------------------------------------
    def summary(self):
        if self.bf is None:
            return {"open": False, "version": __version__}
        bf = self.bf
        added = edit.custom_music(bf)
        return {
            "open": True, "version": __version__, "path": self.path, "size": len(bf.data),
            "dirty": self.dirty, "stale": self.stale,
            "vanilla": len(bf.sounds) >= edit.VANILLA[edit.SOUND],
            "counts": {"sound": len(bf.sounds), "soundset": len(bf.soundsets), "bank": len(bf.banks),
                       "wavearc": len(bf.wavearcs), "group": len(bf.groups), "player": len(bf.players),
                       "file": len(bf.files)},
            "custom": [{"label": m[0], "bpm": m[2], "volume": m[3], "offset": round(m[4], 3),
                        "yoshi": m[6] is not None, "handBahs": bool(m[7] & edit.SONG_BAHS),
                        "sprite": sprite_settings(m[0], m[6] is not None), "seconds": round(F.fwav_info(m[1])["samples"] /
                                                                    F.fwav_info(m[1])["rate"], 1)}
                       for m in added],
            "growth": len(bf.data) - VANILLA_SIZE,
        }

    def tree(self):
        bf = self.bf
        items = []
        for i, s in enumerate(bf.sounds):
            cid, fast = course_id(s.label)
            items.append({"s": "sound", "i": i, "id": s.id, "l": s.label, "k": s.kind,
                          "c": (COURSE_NAMES.get(edit_course(cid), "") + (" (hurry)" if fast else "")) if cid else "",
                          "m": 1 if ("_BGM_" in s.label or s.label.startswith("CUSTOM_BGM")) else 0,
                          "x": 1 if i >= edit.VANILLA[edit.SOUND] else 0})
        for i, ss in enumerate(bf.soundsets):
            items.append({"s": "soundset", "i": i, "id": ss["id"], "l": ss["label"] or f"SOUNDSET {i}",
                          "k": f"{ss['end'] - ss['start'] + 1} sounds"})
        for i, b in enumerate(bf.banks):
            items.append({"s": "bank", "i": i, "id": b["id"], "l": b["label"] or f"BANK {i}", "k": f"file {b['file']}"})
        for i, w in enumerate(bf.wavearcs):
            items.append({"s": "wavearc", "i": i, "id": w["id"], "l": w["label"] or f"WARC {i}",
                          "k": f"{w['wave_count'] or '?'} waves"})
        for i, g in enumerate(bf.groups):
            items.append({"s": "group", "i": i, "id": g["id"], "l": g["label"] or f"GROUP {i}", "k": f"file {g['file']}"})
        for i, p in enumerate(bf.players):
            items.append({"s": "player", "i": i, "id": p["id"], "l": p["label"] or f"PLAYER {i}", "k": f"max {p['max']}"})
        users = self.file_users()
        for i, f in enumerate(bf.files):
            blob_kind = (bf.data[f["offset"]:f["offset"] + 4].decode("ascii", "replace")
                         if f.get("offset") is not None else "ext")
            items.append({"s": "file", "i": i, "id": i, "l": f"File {i}: {users.get(i, '')}".rstrip(": "),
                          "k": blob_kind})
        return items

    def file_users(self):
        bf, out = self.bf, {}
        for s in bf.sounds:
            out.setdefault(s.file_id, s.label)
        for b in bf.banks:
            out.setdefault(b["file"], b["label"])
        for w in bf.wavearcs:
            out.setdefault(w["file"], w["label"] or f"WARC {w['id'] & 0xFFFFFF}")
        for g in bf.groups:
            out.setdefault(g["file"], g["label"])
        return out

    def label_of(self, item_id):
        bf = self.bf
        kind, i = item_id >> 24, item_id & 0xFFFFFF
        try:
            if kind == 1:
                return bf.sounds[i].label
            if kind == 2:
                return bf.soundsets[i]["label"]
            if kind == 3:
                return bf.banks[i]["label"]
            if kind == 4:
                return bf.players[i]["label"]
            if kind == 5:
                return bf.wavearcs[i]["label"] or f"WARC {i}"
            if kind == 6:
                return bf.groups[i]["label"]
        except IndexError:
            return None
        return None

    def item(self, section, index):
        bf = self.bf
        fl = [f.json() for f in info.fields(self.ed, section, index)]
        for f in fl:
            if f.get("ref") and isinstance(f["value"], int):
                f["refLabel"] = (self.file_users().get(f["value"]) if f["ref"] == "file"
                                 else self.label_of(f["value"]))
        out = {"section": section, "index": index, "fields": fl, "tabs": ["properties"]}
        if section == "sound":
            s = bf.sounds[index]
            out.update(label=s.label, kind=s.kind, id=s.id, file=s.file_id,
                       custom=index >= edit.VANILLA[edit.SOUND])
            if s.kind in ("SEQ", "WSD"):
                out["tabs"] = ["player", "properties"]
            if s.kind == "SEQ":
                out["tabs"].append("sequence")
                out["banks"] = [{"id": b, "label": self.label_of(b), "index": b & 0xFFFFFF}
                                for b in s.detail["banks"]]
                out["startOffset"] = s.detail["seq_opts"].get(0, 0)
                out["trackFlags"] = s.detail["track_flags"]
            cid, fast = course_id(s.label)
            out["course"] = bool(cid) or s.label.startswith("CUSTOM_BGM")
        elif section == "bank":
            out.update(label=bf.banks[index]["label"], tabs=["bank", "properties"])
        elif section == "wavearc":
            out.update(label=bf.wavearcs[index]["label"] or f"WARC {index}", tabs=["waves", "properties"])
        elif section == "group":
            out.update(label=bf.groups[index]["label"], tabs=["group", "properties"])
        elif section == "soundset":
            out.update(label=bf.soundsets[index]["label"])
        elif section == "player":
            out.update(label=bf.players[index]["label"])
        elif section == "file":
            f = bf.files[index]
            magic = bf.data[f["offset"]:f["offset"] + 4] if f.get("offset") is not None else b""
            out.update(label=f"File {index}", magic=magic.decode("ascii", "replace"))
            out["tabs"] = (["sequence"] if magic == b"FSEQ" else []) + ["properties", "hex"]
        return out

    # --- rendering -----------------------------------------------------------
    def render_sound(self, index):
        key = ("sound", index, self.version)
        if key not in self.renders:
            s = self.bf.sounds[index]
            res = render.render(self.bf, s, self.waves)
            self.renders[key] = res
        return key, self.renders[key]

    def render_meta(self, key, res, extra=None):
        token = "-".join(str(k) for k in key)
        self.renders[token] = res
        steps = res.steps[:200000]
        meta = {
            "token": token, "rate": res.rate, "length": res.length,
            "loop": res.loop, "tracks": sorted(res.tracks),
            "notes": [[round(t, 4), tr, k, v, round(d or 0, 4)] for t, tr, k, v, d in res.notes[:20000]],
            "vars": [[round(t, 4), tr, var, val] for t, tr, var, val in res.vars[:50000]],
            "steps": [[round(t, 4), tr, off] for t, tr, off in steps],
            "tempo": res.tempo[:2000],
            "peaks": {str(t): peaks(b) for t, b in res.tracks.items()},
        }
        if extra:
            meta.update(extra)
        return meta


def sprite_settings(label, yoshi):
    """How to place the Custom Music sprite (785) for an added song."""
    slot = label[len("CUSTOM_BGM_"):len("CUSTOM_BGM_") + 2]
    if not slot.isdigit() or label.endswith("_FAST"):
        return None
    keep = 2 if yoshi else 0
    return {"slot": int(slot), "nybbles56": f"{int(slot):02X}", "keep": keep,
            "settings1": f"{(int(slot) << 24) | (keep << 20):08X}"}


def edit_course(cid):
    from ..cli import COURSE_BGM
    return COURSE_BGM[cid] if cid is not None and cid < len(COURSE_BGM) else None


def peaks(pcm, n=1600):
    if len(pcm) == 0:
        return []
    mono = np.abs(pcm).max(axis=1)
    step = max(1, len(mono) // n)
    m = mono[:step * (len(mono) // step)].reshape(-1, step).max(axis=1)
    return [round(float(x), 3) for x in m]


class ApiError(Exception):
    pass


PROJECT = Project()
JOBS = {}


def start_job(title, fn):
    job = Job(title)
    JOBS[job.id] = job

    def run():
        try:
            job.result = fn(job)
            job.state = "done"
        except Exception as e:      # reported to the page
            job.error = str(e) or type(e).__name__
            job.log.append(traceback.format_exc().splitlines()[-1])
            job.state = "error"
    threading.Thread(target=run, daemon=True).start()
    return job.json()


# --- API ---------------------------------------------------------------------
RECENT = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "nsmbudx-music", "recent.json")


def recent_files():
    try:
        with open(RECENT, encoding="utf-8") as f:
            return [p for p in json.load(f) if isinstance(p, str)][:8]
    except (OSError, ValueError):
        return []


def remember(path):
    paths = [path] + [p for p in recent_files() if os.path.normcase(p) != os.path.normcase(path)]
    try:
        os.makedirs(os.path.dirname(RECENT), exist_ok=True)
        with open(RECENT, "w", encoding="utf-8") as f:
            json.dump(paths[:8], f)
    except OSError:
        pass


def api_recent(q, body):
    return [{"path": p, "exists": os.path.isfile(p)} for p in recent_files()]


def api_open(q, body):
    path = (body.get("path") or "").strip().strip('"') or dialogs.open_file("bfsar", "Open Sound.bfsar")
    if not path:
        return {"cancelled": True}
    path = os.path.abspath(os.path.expandvars(path))
    if os.path.isdir(path):
        for sub in ("Sound.bfsar", os.path.join("Sound", "Sound.bfsar"), os.path.join("romfs", "Sound", "Sound.bfsar")):
            if os.path.isfile(os.path.join(path, sub)):
                path = os.path.join(path, sub)
                break
    if not os.path.isfile(path):
        raise ApiError(f"{path} does not exist")

    def work(job):
        job.log.append(f"reading {os.path.basename(path)}")
        with open(path, "rb") as f:
            data = f.read()
        if data[:4] != b"FSAR":
            raise ApiError("not a sound archive (FSAR)")
        PROJECT.load(data, path)
        PROJECT.dirty = False
        remember(path)
        return PROJECT.summary()
    return start_job("Opening archive", work)


def api_save(q, body):
    PROJECT.require()
    path = body.get("path")
    if body.get("ask") or not path:
        path = dialogs.save_file("bfsar", "Save sound archive as", PROJECT.path)
    if not path:
        return {"cancelled": True}

    def work(job):
        job.log.append("building")
        PROJECT.sync()
        data = PROJECT.bf.data
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        PROJECT.path, PROJECT.dirty = path, False
        return {"path": path, "size": len(data)}
    return start_job("Saving", work)


def api_item(q, body):
    PROJECT.require()
    return PROJECT.item(q["section"], int(q["index"]))


def api_field(q, body):
    PROJECT.require()
    with PROJECT.lock:
        v = info.set_field(PROJECT.ed, body["section"] if body["section"] != "archive" else "archive",
                           int(body.get("index", 0)), body["key"], body["value"])
        PROJECT.changed()
    return {"value": v}


def api_archive(q, body):
    PROJECT.require()
    return {"fields": [f.json() for f in info.fields(PROJECT.ed, "archive", 0)], **PROJECT.summary()}


def api_render(q, body):
    PROJECT.require()
    index = int(q["index"])

    def work(job):
        PROJECT.sync()
        s = PROJECT.bf.sounds[index]
        job.log.append(f"rendering {s.label}")
        key, res = PROJECT.render_sound(index)
        extra = {"label": s.label, "kind": s.kind}
        if s.kind == "SEQ":
            extra["listingFile"] = s.file_id
            extra["bpm"] = s.options.get(31)
        return PROJECT.render_meta(key, res, extra)
    return start_job("Rendering", work)


def api_note(q, body):
    PROJECT.require()
    PROJECT.sync()
    bank = int(q["bank"])
    res = render.render_note(PROJECT.bf, 0x03000000 | bank, int(q["program"]), int(q["key"]),
                             int(q.get("velocity", 100)), float(q.get("seconds", 1.2)), PROJECT.waves)
    return ("audio/wav", render.to_wav(res.mix(), res.rate))


def api_audio(q, body):
    res = PROJECT.renders.get(q["token"])
    if res is None:
        raise ApiError("render expired: play again")
    t = q.get("track")
    if t is None or t == "mix":
        mute = {int(x) for x in q.get("mute", "").split(",") if x}
        pcm = res.mix(mute)
    else:
        pcm = res.tracks[int(t)]
    return ("audio/wav", render.to_wav(pcm, res.rate))


def api_seq(q, body):
    PROJECT.require()
    PROJECT.sync()
    fid = int(q["file"])
    blob = PROJECT.bf.file_bytes(fid)
    if blob is None or blob[:4] != b"FSEQ":
        raise ApiError("not a sequence file")
    lst = seq.disassemble(blob)
    users = [{"label": s.label, "index": i, "start": s.detail["seq_opts"].get(0, 0)}
             for i, s in enumerate(PROJECT.bf.sounds) if s.file_id == fid]
    return {"file": fid, "text": lst.text, "lines": len(lst.lines),
            "lineOf": {str(k): v for k, v in lst.line_of.items()}, "labels": lst.labels, "sounds": users}


def api_seq_compile(q, body):
    PROJECT.require()
    fid = int(body["file"])
    with PROJECT.lock:
        old = PROJECT.ed.files[fid]
        try:
            blob = seq.compile_text(body["text"], old)
        except seq.AsmError as e:
            return {"ok": False, "line": e.line, "error": str(e)}
        # the sounds that start in this file must still find their labels
        old_labels = F.fseq_labels(old)
        new_labels = F.fseq_labels(blob)
        moved = []
        for i, s in enumerate(PROJECT.bf.sounds):
            if s.file_id != fid:
                continue
            start = s.detail["seq_opts"].get(0, 0)
            name = next((n for n, o in old_labels.items() if o == start), None)
            if name and name in new_labels and new_labels[name] != start:
                info.set_field(PROJECT.ed, "sound", i, "start_offset", new_labels[name])
                moved.append(s.label)
        PROJECT.ed.files[fid] = blob
        PROJECT.changed()
    return {"ok": True, "size": len(blob), "moved": moved}


def api_bank(q, body):
    PROJECT.require()
    PROJECT.sync()
    index = int(q["index"])
    b = PROJECT.bf.banks[index]
    bank = banks.Bank(PROJECT.bf.file_bytes(b["file"]))
    waves = []
    for warc, wi in bank.waves:
        w = PROJECT.waves.get(warc, wi) if (warc >> 24) == 5 and (warc & 0xFFFFFF) < len(PROJECT.bf.wavearcs) else None
        waves.append({"warc": warc, "index": wi, "warcLabel": PROJECT.label_of(warc),
                      "seconds": round(w["samples"] / w["rate"], 2) if w else None,
                      "rate": w["rate"] if w else None, "loop": bool(w and w["loop"])})
    users = [s.label for s in PROJECT.bf.sounds if s.kind == "SEQ" and (0x03000000 | index) in s.detail["banks"]]
    return {"index": index, "label": b["label"], "file": b["file"], "instruments": bank.summary(),
            "waves": waves, "users": users[:50]}


def api_bank_field(q, body):
    PROJECT.require()
    with PROJECT.lock:
        index = int(body["index"])
        fid = PROJECT.bf.banks[index]["file"]
        bank = banks.Bank(PROJECT.ed.files[fid])
        bank.set_field(int(body["program"]), int(body["key"]), int(body["vel"]), body["name"], body["value"])
        PROJECT.ed.files[fid] = bytes(bank.blob)
        PROJECT.changed()
    return {"ok": True}


def api_wavearc(q, body):
    PROJECT.require()
    PROJECT.sync()
    index = int(q["index"])
    wa = PROJECT.bf.wavearcs[index]
    blob = PROJECT.bf.file_bytes(wa["file"])
    out = []
    for k, w in enumerate(fwav.fwar_waves(blob) if blob else []):
        i = F.fwav_info(w)
        out.append({"index": k, "rate": i["rate"], "channels": i["channels"], "samples": i["samples"],
                    "seconds": round(i["samples"] / i["rate"], 2), "loop": i["loop"],
                    "loopStart": i["loop_start"], "encoding": F.ENCODINGS.get(i.get("encoding", 2), "?"),
                    "bytes": len(w)})
    return {"index": index, "label": wa["label"] or f"WARC {index}", "file": wa["file"], "waves": out}


def api_wave_audio(q, body):
    PROJECT.require()
    w = PROJECT.waves.get(0x05000000 | int(q["warc"]), int(q["wave"]))
    pcm = w["pcm"]
    if pcm.shape[1] == 1:
        pcm = np.repeat(pcm, 2, axis=1)
    return ("audio/wav", render.to_wav(pcm[:, :2], w["rate"]))


def api_wave_export(q, body):
    PROJECT.require()
    warc, k = int(body["warc"]), int(body["wave"])
    path = dialogs.save_file("wav", "Export wave as", f"{PROJECT.label_of(0x05000000 | warc) or 'wave'}_{k}.wav")
    if not path:
        return {"cancelled": True}
    blob = PROJECT.bf.file_bytes(PROJECT.bf.wavearcs[warc]["file"])
    w = fwav.Wave.parse(fwav.fwar_waves(blob)[k])
    write_wav(path, w.pcm(), w.rate, w.loop_start if w.loop else None, w.samples if w.loop else None)
    return {"path": path}


def api_wave_replace(q, body):
    PROJECT.require()
    warc, k = int(body["warc"]), int(body["wave"])
    path = body.get("path") or dialogs.open_file("bfwav", "Replace wave with")
    if not path:
        return {"cancelled": True}

    def work(job):
        fid = PROJECT.bf.wavearcs[warc]["file"]
        waves = fwav.fwar_waves(PROJECT.ed.files[fid])
        if path.lower().endswith(".bfwav"):
            new = open(path, "rb").read()
            if new[:4] != b"FWAV" or new[4:6] != b"\xff\xfe":
                raise ApiError("not a little-endian (Switch) BFWAV")
        else:
            job.log.append("encoding DSP-ADPCM")
            pcm, rate, loop = read_wav(path)
            old = F.fwav_info(waves[k])
            keep = body.get("keepLoop") and old["loop"]
            new = pcm_to_fwav(pcm, rate, loop if loop is not None else (0 if keep else None),
                              loop is not None or bool(keep), log=job.log.append)[0]
        waves[k] = new
        with PROJECT.lock:
            PROJECT.ed.files[fid] = fwav.build_fwar(waves)
            PROJECT.changed()
        return {"ok": True}
    return start_job("Replacing wave", work)


def api_group(q, body):
    PROJECT.require()
    PROJECT.sync()
    index = int(q["index"])
    g = PROJECT.bf.groups[index]
    blob = PROJECT.bf.file_bytes(g["file"])
    files, sets = edit.parse_fgrp(blob)
    users = PROJECT.file_users()
    return {"index": index, "label": g["label"], "file": g["file"],
            "files": [{"file": f, "label": users.get(f, "")} for f in files],
            "items": [{"id": i, "label": PROJECT.label_of(i), "flags": fl} for i, fl in sets]}


def api_group_flags(q, body):
    PROJECT.require()
    with PROJECT.lock:
        index = int(body["index"])
        fid = PROJECT.bf.groups[index]["file"]
        files, sets = edit.parse_fgrp(PROJECT.ed.files[fid])
        k = int(body["item"])
        sets[k] = (sets[k][0], int(body["flags"]) & 0xFFFFFFFF)
        PROJECT.ed.files[fid] = edit.build_fgrp(files, sets)
        PROJECT.changed()
    return {"ok": True}


def api_hex(q, body):
    PROJECT.require()
    blob = PROJECT.bf.file_bytes(int(q["file"])) or b""
    start = int(q.get("offset", 0))
    data = blob[start:start + 4096]
    return {"size": len(blob), "offset": start, "hex": data.hex()}


# --- custom music ------------------------------------------------------------
def api_pick(q, body):
    path = dialogs.open_file(body.get("kind", "wav"), body.get("title", "Choose a file"))
    if path:
        PROJECT.local_files.add(os.path.abspath(path))
    return {"path": path}


def api_analyze(q, body):
    path = os.path.abspath(body["path"])
    if not os.path.isfile(path):
        raise ApiError("file not found")
    PROJECT.local_files.add(path)
    pcm, rate, loop = read_wav(path)
    bpm, offset = estimate_beat(pcm, rate)
    x = pcm.astype(np.float32) / 32768.0
    return {"path": path, "name": os.path.basename(path), "rate": rate, "channels": pcm.shape[1],
            "samples": len(pcm), "seconds": len(pcm) / rate, "loopStart": loop, "bpm": bpm,
            "offset": round(offset, 3), "peaks": peaks(x, 2400)}


def api_bahs(q, body):
    path = os.path.abspath(body["path"])
    if path not in PROJECT.local_files:
        raise ApiError("not a file chosen in this session")
    with open(path, encoding="utf-8-sig") as f:
        return edit.parse_bahs(f.read())


def api_local_wav(q, body):
    path = os.path.abspath(q["path"])
    if path not in PROJECT.local_files:
        raise ApiError("not a file chosen in this session")
    with open(path, "rb") as f:
        return ("audio/wav", f.read())


def api_music_add(q, body):
    PROJECT.require()
    label = edit.custom_label(body["slot"], body.get("fast"))
    path = os.path.abspath(body["path"])

    def work(job):
        job.log.append("reading the song")
        pcm, rate, wav_loop = read_wav(path)
        loop = body.get("loop", True)
        ls = body.get("loopStart")
        ls = wav_loop if ls is None else int(ls)
        bpm, offset = estimate_beat(pcm, rate, body.get("bpm") or None)
        if body.get("offset") is not None:
            offset = float(body["offset"])
        job.log.append("encoding DSP-ADPCM (this takes a while)")
        blob, used, _ = pcm_to_fwav(pcm, rate, ls, loop, log=job.log.append)
        yoshi = None
        if body.get("yoshi"):
            job.log.append("encoding the Yoshi layer")
            ypcm, yrate, _ = read_wav(os.path.abspath(body["yoshi"]))
            if yrate != rate:
                raise ApiError(f"the Yoshi layer is {yrate} Hz, the song {rate} Hz: use the same rate")
            if ypcm.shape[1] != pcm.shape[1]:
                ypcm = np.repeat(ypcm[:, :1], pcm.shape[1], axis=1)
            ypcm = ypcm[:len(pcm)]
            if len(ypcm) < len(pcm):
                ypcm = np.concatenate([ypcm, np.zeros((len(pcm) - len(ypcm), pcm.shape[1]), np.int16)])
            yoshi = pcm_to_fwav(ypcm, rate, ls, loop, log=job.log.append)[0]
        pad = (len(used) - len(pcm)) / rate        # silence the encoder put in front for the loop
        bahs = body.get("bahs")
        bahs = [(float(t) + pad, int(k)) for t, k in bahs] if bahs else None
        job.log.append("building the archive")
        with PROJECT.lock:
            PROJECT.sync()
            data = edit.add_music(PROJECT.bf, label, blob, int(bpm), body.get("volume"), offset + pad, bahs, yoshi)
            dirty = True
            PROJECT.load(data, PROJECT.path)
            PROJECT.dirty = dirty
        return {"label": label, "bpm": int(bpm), "offset": round(offset, 3), "summary": PROJECT.summary()}
    return start_job(f"Adding {label}", work)


def api_music_remove(q, body):
    PROJECT.require()

    def work(job):
        with PROJECT.lock:
            PROJECT.sync()
            data = edit.remove_music(PROJECT.bf, body["label"])
            PROJECT.load(data, PROJECT.path)
            PROJECT.dirty = True
        return PROJECT.summary()
    return start_job(f"Removing {body['label']}", work)


def api_job(q, body):
    job = JOBS.get(q["id"])
    if job is None:
        raise ApiError("unknown job")
    return job.json()


ROUTES = {
    ("GET", "/api/summary"): lambda q, b: PROJECT.summary(),
    ("GET", "/api/recent"): api_recent,
    ("GET", "/api/tree"): lambda q, b: (PROJECT.require(), PROJECT.sync(), PROJECT.tree())[2],
    ("GET", "/api/item"): api_item,
    ("GET", "/api/archive"): api_archive,
    ("GET", "/api/job"): api_job,
    ("GET", "/api/audio"): api_audio,
    ("GET", "/api/note"): api_note,
    ("GET", "/api/seq"): api_seq,
    ("GET", "/api/bank"): api_bank,
    ("GET", "/api/wavearc"): api_wavearc,
    ("GET", "/api/wave"): api_wave_audio,
    ("GET", "/api/group"): api_group,
    ("GET", "/api/hex"): api_hex,
    ("GET", "/api/local"): api_local_wav,
    ("POST", "/api/open"): api_open,
    ("POST", "/api/save"): api_save,
    ("POST", "/api/field"): api_field,
    ("POST", "/api/render"): lambda q, b: api_render(b, b),
    ("POST", "/api/seq"): api_seq_compile,
    ("POST", "/api/bank"): api_bank_field,
    ("POST", "/api/wave/export"): api_wave_export,
    ("POST", "/api/wave/replace"): api_wave_replace,
    ("POST", "/api/group"): api_group_flags,
    ("POST", "/api/pick"): api_pick,
    ("POST", "/api/analyze"): api_analyze,
    ("POST", "/api/bahs"): api_bahs,
    ("POST", "/api/music/add"): api_music_add,
    ("POST", "/api/music/remove"): api_music_remove,
}


class Handler(BaseHTTPRequestHandler):
    token = None
    last_seen = time.time()
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, ctype, data, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def handle_one(self, method):
        if not self._host_ok():
            return self._send(403, "text/plain", b"forbidden")
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        if not u.path.startswith("/api/"):
            path = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
            full = os.path.normpath(os.path.join(STATIC, path))
            if not full.startswith(STATIC) or not os.path.isfile(full):
                return self._send(404, "text/plain", b"not found")
            with open(full, "rb") as f:
                data = f.read()
            return self._send(200, mimetypes.guess_type(full)[0] or "application/octet-stream", data)
        if (self.headers.get("X-Token") or q.get("t")) != Handler.token:
            return self._send(403, "application/json", b'{"error":"bad token"}')
        Handler.last_seen = time.time()
        fn = ROUTES.get((method, u.path))
        if fn is None:
            return self._send(404, "application/json", b'{"error":"no such call"}')
        body = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                body = json.loads(self.rfile.read(n))
        try:
            out = fn(q, body)
        except (ApiError, ValueError, KeyError, IndexError) as e:
            msg = str(e).strip("'\"") or type(e).__name__
            return self._send(400, "application/json", json.dumps({"error": msg}).encode())
        except Exception as e:
            traceback.print_exc()
            return self._send(500, "application/json", json.dumps({"error": f"{type(e).__name__}: {e}"}).encode())
        if isinstance(out, tuple):
            return self._send(200, out[0], out[1])
        return self._send(200, "application/json", json.dumps(out, default=_json_default).encode())

    def do_GET(self):
        self.handle_one("GET")

    def do_POST(self):
        self.handle_one("POST")


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (bytes, bytearray)):
        return o.hex()
    if isinstance(o, tuple):
        return list(o)
    raise TypeError(type(o).__name__)


def make_server(port=0):
    Handler.token = secrets.token_urlsafe(18)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    return srv
