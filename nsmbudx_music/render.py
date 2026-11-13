"""Offline playback of sequence and wave sounds: a software take on nn::atk's player.

The sequencer runs the FSEQ bytecode tick by tick like the game (tempo, timebase,
note-wait, waits for a held note to end, calls, loops, variables and conditions) and
records every note it starts. The notes are then synthesised from the banks' waves:
pitch from the key against the region's original key, the region's ADSHR envelope
(as the game computes it, in 1 ms steps), velocity and volumes squared, and pan.

Each track is rendered to its own buffer, so a player can mute and solo tracks (the
course music's stems) without rendering again. The log keeps what a viewer needs:
notes per track, variable writes (the enemies' beat), and which command each track
ran when, for following the sequence text during playback.

Approximations: volume, pan and pitch bend changes take effect at the next note;
modulation (vibrato), portamento, filters and effects sends are not rendered.
"""
import math

import numpy as np

from . import banks, fwav, seq as S
from .formats import fseq_data

RATE = 48000
MAX_SECONDS = 360.0


def _db_to_amp(db):
    return np.power(10.0, db / 20.0)


ATTACK_TABLE = (0, 1, 5, 14, 26, 38, 51, 63, 73, 84, 92, 100, 109, 116, 123, 127, 132, 137, 143)


def _env_rate(v):
    """Decay/release speed in 0.1 dB per millisecond."""
    if v == 127:
        return 65535.0
    if v == 126:
        return 24.0
    return (v * 2 + 1) / 128.0 / 5.0 if v < 50 else 60.0 / (126 - v) / 5.0


def adshr_curve(adshr, note_ms, total_ms, release_override=None):
    """Envelope gain per millisecond (length total_ms), note released at note_ms.

    The game's curve, in 0.1 dB: attack multiplies -90.4 dB by a factor each ms until
    it is near 0 dB, hold keeps 0 dB, decay falls linearly to the sustain level
    (40 log10(sustain / 127)), and release falls linearly from wherever it is.
    """
    a, d, s, h, r = (adshr[k] for k in ("attack", "decay", "sustain", "hold", "release"))
    if release_override is not None:
        r = release_override
    x = (255 - a) if a < 109 else ATTACK_TABLE[127 - a]
    attack = (x / 256.0) ** 0.2
    sustain = max(-723.0, 400.0 * math.log10(s / 127.0)) if s > 0 else -723.0
    hold = max(1, (h + 1) * (h + 1) // 4)
    if attack <= 0:
        n_att = 0
    else:
        n_att = max(0, int(math.floor(math.log((1 / 32) / 904.0) / math.log(attack))))
    seg_att = -904.0 * attack ** np.arange(1, n_att + 1)
    n_dec = int(math.ceil(-sustain / _env_rate(d))) if sustain < 0 else 0
    seg_dec = np.maximum(-_env_rate(d) * np.arange(1, n_dec + 1), sustain)
    pre = np.concatenate([seg_att, np.zeros(hold), seg_dec])
    out = np.full(total_ms, sustain, np.float64)
    n = min(len(pre), total_ms)
    out[:n] = pre[:n]
    if note_ms < total_ms:
        start = out[note_ms - 1] if note_ms > 0 else -904.0
        tail = start - _env_rate(r) * np.arange(1, total_ms - note_ms + 1)
        out[note_ms:] = np.maximum(tail, -9040.0)
        out[note_ms:][tail <= -904.0] = -9040.0
    return _db_to_amp(out / 10.0).astype(np.float32)


class Waves:
    """Decoded waves of an archive, cached: float32 (samples, channels) in -1..1."""

    def __init__(self, bf):
        self.bf, self._war, self._wave = bf, {}, {}

    def get(self, warc_id, index):
        key = (warc_id & 0xFFFFFF, index)
        if key not in self._wave:
            wa = self.bf.wavearcs[key[0]]
            fid = wa["file"]
            if fid not in self._war:
                blob = self.bf.file_bytes(fid)
                self._war[fid] = fwav.fwar_waves(blob) if blob else []
            waves = self._war[fid]
            if index >= len(waves):
                self._wave[key] = None
            else:
                w = fwav.Wave.parse(waves[index])
                self._wave[key] = {"pcm": w.pcm().astype(np.float32) / 32768.0, "rate": w.rate,
                                   "loop": w.loop, "loop_start": w.loop_start, "samples": w.samples}
        return self._wave[key]


class Voice:
    __slots__ = ("track", "start", "end", "key", "velocity", "region", "wave", "ratio", "gain",
                 "pan", "adshr", "release", "remaining", "offset")


class Result:
    def __init__(self, rate):
        self.rate = rate
        self.tracks = {}          # track -> float32 (n, 2)
        self.length = 0.0
        self.loop = None          # (start, end) seconds
        self.notes = []           # (time, track, key, velocity, duration)
        self.vars = []            # (time, track, var, value)
        self.steps = []           # (time, track, DATA offset)
        self.tempo = []           # (time, bpm, timebase)

    def mix(self, mute=()):
        n = max((len(b) for b in self.tracks.values()), default=0)
        out = np.zeros((n, 2), np.float32)
        for t, b in self.tracks.items():
            if t not in mute:
                out[:len(b)] += b
        return out


class _Track:
    def __init__(self, no, pc):
        self.no, self.pc, self.wait, self.active = no, pc, 0, True
        self.stack, self.loops = [], []
        self.notewait, self.tie, self.mute = True, False, False
        self.volume, self.volume2, self.pan, self.transpose = 127, 127, 64, 0
        self.bend, self.bend_range, self.prg, self.bank = 0, 2, 0, 0
        self.cmp, self.finish_wait = True, None
        self.env = {}
        self.vars = [-1] * 16
        self.velocity_range = 127


class Sequencer:
    """Runs a sequence and synthesises its notes."""

    def __init__(self, bf, waves=None, rate=RATE):
        self.bf, self.rate = bf, rate
        self.waves = waves or Waves(bf)
        self._banks = {}

    def bank(self, bank_id):
        i = bank_id & 0xFFFFFF
        if i not in self._banks:
            info = self.bf.banks[i] if i < len(self.bf.banks) else None
            blob = self.bf.file_bytes(info["file"]) if info else None
            self._banks[i] = banks.Bank(blob) if blob else None
        return self._banks[i]

    def run(self, data, start, bank_ids, max_seconds=MAX_SECONDS, track_flags=None):
        res = Result(self.rate)
        player_vars, global_vars = [-1] * 16, [-1] * 16
        tracks = {0: _Track(0, start)}
        tempo, timebase, now, ticks = 120, 48, 0.0, 0
        voices, finished = [], []
        loop = None
        held_loop = None

        def var_ref(no, track):
            if no < 16:
                return player_vars, no
            if no < 32:
                return global_vars, no - 16
            return track.vars, (no - 32) & 15

        def arg_value(c, track):
            if c.argtype == 0xa1:
                arr, i = var_ref(c.args[-1], track)
                return arr[i]
            if c.argtype == 0xa0:
                lo, hi = c.args[-2], c.args[-1]
                return (lo + hi) // 2
            return c.args[-1]

        def note_on(track, c):
            nonlocal held_loop
            key = max(0, min(127, c.op + track.transpose))
            vel = c.args[0] * track.velocity_range // 127
            length = arg_value(c, track)
            bank_id = bank_ids[track.bank] if track.bank < len(bank_ids) else None
            bk = self.bank(bank_id) if bank_id is not None else None
            reg = bk.region(track.prg, key, vel) if bk else None
            v = None
            if reg is not None and not track.mute and reg["wave"] < len(bk.waves):
                warc, index = bk.waves[reg["wave"]]
                wave = self.waves.get(warc, index)
                if wave is not None:
                    v = Voice()
                    v.track, v.start, v.key, v.velocity, v.region, v.wave = track.no, now, key, vel, reg, wave
                    semis = key - reg["key"] + track.bend * track.bend_range / 128.0
                    v.ratio = 2 ** (semis / 12.0) * reg["pitch"] * wave["rate"] / self.rate
                    vol = (track.volume / 127) * (track.volume2 / 127)
                    v.gain = (vol * vol) * (vel / 127) ** 2 * (reg["volume"] / 127)
                    v.pan = max(0, min(127, track.pan + reg["pan"] - 64))
                    v.adshr = dict(reg["adshr"])
                    v.adshr.update({k: x for k, x in track.env.items() if x is not None})
                    v.release, v.offset = None, c.offset
                    v.remaining = length if length > 0 else -1
                    v.end = None
                    voices.append(v)
                    res.notes.append([now, track.no, key, vel, None])
                    v.offset = len(res.notes) - 1
                    if v.remaining < 0 and wave["loop"] and held_loop is None:
                        r = wave["rate"] * v.ratio * self.rate / wave["rate"]   # source samples/s
                        held_loop = (now + wave["loop_start"] / r, now + wave["samples"] / r)
            if track.notewait:
                track.wait = length
                if length == 0:
                    track.finish_wait = v if v is not None else None
                    if v is None:
                        track.wait = 0
            return v

        def voice_done(v):
            """Whether a voice has ended (non-looping wave played out) by `now`."""
            if v.end is not None:
                return now >= v.end
            if v.remaining < 0 and not v.wave["loop"]:
                dur = v.wave["samples"] / (v.wave["rate"] * v.ratio * self.rate / v.wave["rate"])
                return now >= v.start + dur
            return False

        def step(track):
            nonlocal tempo, timebase, loop
            guard = 0
            while track.active and track.wait == 0 and track.finish_wait is None:
                guard += 1
                if guard > 10000:           # a loop with no wait: give up on this track
                    track.active = False
                    return
                if not 0 <= track.pc < len(data):
                    track.active = False
                    return
                c = S.decode(data, track.pc)
                res.steps.append((now, track.no, c.offset))
                track.pc += c.size
                if c.cond and not track.cmp:
                    continue
                op = c.op
                if op < 0x80:
                    note_on(track, c)
                elif op == 0x80:
                    track.wait = max(0, arg_value(c, track))
                elif op == 0x81:
                    track.prg = arg_value(c, track)
                elif op == 0x88:
                    if c.track not in tracks and (track_flags is None or track_flags >> c.track & 1):
                        tracks[c.track] = _Track(c.track, c.target)
                        new_tracks.append(c.track)
                elif op == 0x89:
                    # the first backward jump of track 0 marks the loop, unless a held
                    # looping wave (course music) already defines it
                    if c.target < c.offset and track.no == 0 and loop is None and held_loop is None:
                        t0 = self._first_time(res.steps, 0, c.target)
                        if t0 is not None:
                            loop = (t0, now)
                    track.pc = c.target
                elif op == 0x8a:
                    if len(track.stack) < 3:
                        track.stack.append(track.pc)
                        track.pc = c.target
                elif op == 0xfd:
                    if track.stack:
                        track.pc = track.stack.pop()
                    else:
                        track.active = False
                elif op == 0xff:
                    track.active = False
                elif op == 0xfe:
                    pass
                elif op == 0xd4:
                    track.loops.append((track.pc, arg_value(c, track)))
                elif op == 0xfc:
                    if track.loops:
                        pc, count = track.loops[-1]
                        if count == 0:
                            track.pc = pc
                        elif count > 1:
                            track.loops[-1] = (pc, count - 1)
                            track.pc = pc
                        else:
                            track.loops.pop()
                elif op == 0xe1:
                    tempo = max(1, arg_value(c, track))
                    res.tempo.append((now, tempo, timebase))
                elif op == 0xb0:
                    timebase = max(1, arg_value(c, track))
                    res.tempo.append((now, tempo, timebase))
                elif op == 0xc1:
                    track.volume = arg_value(c, track)
                elif op == 0xd5:
                    track.volume2 = arg_value(c, track)
                elif op == 0xc0:
                    track.pan = arg_value(c, track)
                elif op == 0xc3:
                    track.transpose = arg_value(c, track)
                elif op == 0xc4:
                    track.bend = arg_value(c, track)
                elif op == 0xc5:
                    track.bend_range = arg_value(c, track)
                elif op == 0xc7:
                    track.notewait = arg_value(c, track) != 0
                elif op == 0xc8:
                    track.tie = arg_value(c, track) != 0
                elif op == 0xdd:
                    track.mute = arg_value(c, track) != 0
                elif op == 0xb6:
                    track.bank = arg_value(c, track)
                elif op == 0xb3:
                    track.velocity_range = arg_value(c, track)
                elif op in (0xd0, 0xd1, 0xd2, 0xd3, 0xb1):
                    track.env[{0xd0: "attack", 0xd1: "decay", 0xd2: "sustain", 0xd3: "release",
                               0xb1: "hold"}[op]] = arg_value(c, track)
                elif op == 0xfb:
                    track.env = {}
                elif op == 0xf0 and c.ex is not None and c.ex < 0xa0:
                    arr, i = var_ref(c.args[0], track)
                    v = arg_value(c, track)
                    ex, cur = c.ex, arr[i]
                    if ex == 0x80: arr[i] = v
                    elif ex == 0x81: arr[i] = cur + v
                    elif ex == 0x82: arr[i] = cur - v
                    elif ex == 0x83: arr[i] = cur * v
                    elif ex == 0x84: arr[i] = int(cur / v) if v else cur
                    elif ex == 0x85: arr[i] = cur << v if v >= 0 else cur >> -v
                    elif ex == 0x86: arr[i] = v // 2
                    elif ex == 0x87: arr[i] = cur & v
                    elif ex == 0x88: arr[i] = cur | v
                    elif ex == 0x89: arr[i] = cur ^ v
                    elif ex == 0x8a: arr[i] = ~v
                    elif ex == 0x8b: arr[i] = cur % v if v else cur
                    elif ex == 0x90: track.cmp = cur == v
                    elif ex == 0x91: track.cmp = cur >= v
                    elif ex == 0x92: track.cmp = cur > v
                    elif ex == 0x93: track.cmp = cur <= v
                    elif ex == 0x94: track.cmp = cur < v
                    elif ex == 0x95: track.cmp = cur != v
                    if ex < 0x90:
                        arr[i] = (arr[i] + 0x8000) % 0x10000 - 0x8000
                        res.vars.append((now, track.no, c.args[0], arr[i]))
            return

        while now < max_seconds:
            new_tracks = []
            for no in sorted(tracks):
                t = tracks[no]
                if not t.active:
                    continue
                if t.wait > 0:
                    t.wait -= 1
                    if t.wait > 0:
                        continue
                if t.finish_wait is not None:
                    if voice_done(t.finish_wait):
                        t.finish_wait = None
                    else:
                        continue
                step(t)
            for no in new_tracks:
                step(tracks[no])
            # note lengths count down in ticks
            for v in voices:
                if v.end is None and v.remaining > 0:
                    v.remaining -= 1
                    if v.remaining == 0:
                        v.end = now + 60.0 / (tempo * timebase)
            if loop is not None:
                break
            if held_loop is not None and now >= held_loop[1]:
                loop = held_loop
                break
            if not any(t.active for t in tracks.values()) and all(voice_done(v) for v in voices):
                break
            now += 60.0 / (tempo * timebase)
            ticks += 1

        if loop is None and held_loop is not None and now >= held_loop[1]:
            loop = held_loop
        res.loop = loop
        end = loop[1] if loop else now
        self._synth(res, voices, end, tails=loop is None)
        return res

    @staticmethod
    def _first_time(steps, track, offset):
        for t, tn, off in steps:
            if tn == track and off == offset:
                return t
        return None

    def _synth(self, res, voices, end, tails):
        rate = self.rate
        total = int(math.ceil(end * rate)) + (rate * 2 if tails else 0)
        for v in voices:
            buf = res.tracks.setdefault(v.track, np.zeros((total, 2), np.float32))
            start = int(round(v.start * rate))
            if start >= total:
                continue
            wave = v.wave
            pcm = wave["pcm"]
            if pcm.shape[1] == 1:
                pcm = np.repeat(pcm, 2, axis=1)
            elif pcm.shape[1] > 2:
                pcm = pcm[:, :2]
            note_end = None if v.end is None else int(round(v.end * rate))
            if note_end is None:
                limit = total
            else:
                limit = min(total, note_end + rate * 4)
            n_out = limit - start
            if n_out <= 0:
                continue
            pos = np.arange(n_out, dtype=np.float64) * v.ratio
            if wave["loop"] and wave["samples"] > wave["loop_start"]:
                ls, le = wave["loop_start"], wave["samples"]
                over = pos >= le
                pos[over] = ls + np.mod(pos[over] - ls, le - ls)
            else:
                keep = pos < len(pcm) - 1
                n_out = int(keep.sum())
                pos = pos[:n_out]
                if n_out == 0:
                    continue
            i = pos.astype(np.int64)
            frac = (pos - i).astype(np.float32)[:, None]
            j = np.minimum(i + 1, len(pcm) - 1)
            if wave["loop"]:
                j = np.where(i + 1 >= wave["samples"], wave["loop_start"], j)
            out = pcm[i] * (1 - frac) + pcm[j] * frac
            # envelope at 1 ms steps
            total_ms = int(math.ceil(n_out * 1000 / rate)) + 1
            note_ms = total_ms + 1 if note_end is None else max(0, (note_end - start) * 1000 // rate)
            env = adshr_curve(v.adshr, note_ms, total_ms)
            env_s = np.interp(np.arange(n_out) * 1000.0 / rate, np.arange(total_ms), env).astype(np.float32)
            nz = np.nonzero(env_s > 1e-5)[0]
            if len(nz) == 0:
                continue
            n_out = min(n_out, int(nz[-1]) + 1)
            p = v.pan / 127.0
            left, right = min(1.0, 2 * (1 - p)), min(1.0, 2 * p)
            out = out[:n_out] * (env_s[:n_out, None] * v.gain)
            out[:, 0] *= left
            out[:, 1] *= right
            buf[start:start + n_out] += out
            if res.notes and v.offset is not None and v.offset < len(res.notes):
                res.notes[v.offset][4] = n_out / rate
        res.length = total / rate


def render_sequence(bf, sound, waves=None, max_seconds=MAX_SECONDS, rate=RATE):
    """Render a SEQ sound (a bfsar Sound)."""
    data = fseq_data(bf.file_bytes(sound.file_id))
    start = sound.detail["seq_opts"].get(0, 0)
    flags = sound.detail.get("track_flags")
    return Sequencer(bf, waves, rate).run(data, start, sound.detail["banks"], max_seconds,
                                          flags if flags else None)


def render_note(bf, bank_id, program, key, velocity=100, seconds=1.5, waves=None, rate=RATE):
    """One note of a bank instrument, as a piano key would play it."""
    sq = Sequencer(bf, waves, rate)
    bpm, tb = 120, 48
    length = max(1, int(seconds * bpm * tb / 60))
    data = bytes([0xb0, tb, 0xe1]) + bpm.to_bytes(2, "little") + bytes([0x81, program & 0x7f, key, velocity]) \
        + S._vlq(length) + bytes([0xff])
    return sq.run(data, 0, [bank_id], seconds + 6)


def render_wave_sound(bf, sound, waves=None, rate=RATE, max_seconds=MAX_SECONDS):
    """Render a WSD sound from its note events."""
    waves = waves or Waves(bf)
    wsd = banks.WaveSoundFile(bf.file_bytes(sound.file_id))
    ws = wsd.sounds[sound.detail["index"]]
    res = Result(rate)
    voices, held_loop, end = [], None, 0.0
    for tno, events in enumerate(ws["tracks"]):
        for e in events:
            if e["note"] >= len(ws["notes"]):
                continue
            note = ws["notes"][e["note"]]
            if note["wave"] >= len(wsd.waves):
                continue
            wave = waves.get(*wsd.waves[note["wave"]])
            if wave is None:
                continue
            v = Voice()
            v.track, v.start, v.key, v.velocity, v.region, v.wave = tno, e["position"], 60, 127, note, wave
            v.ratio = 2 ** ((60 - note["key"]) / 12.0) * note["pitch"] * ws["pitch"] * wave["rate"] / rate
            v.gain = (note["volume"] / 127) * (sound.volume / 127)
            v.pan = max(0, min(127, ws["pan"] + note["pan"] - 64))
            v.adshr = note["adshr"] if note.get("offsets", {}).get("attack") else ws["adshr"]
            v.release, v.offset, v.remaining = None, None, -1
            v.end = e["position"] + e["length"] if e["length"] > 0 else None
            if v.end is None and wave["loop"] and held_loop is None:
                r = wave["rate"] * v.ratio * rate / wave["rate"]
                held_loop = (v.start + wave["loop_start"] / r, v.start + wave["samples"] / r)
            dur = wave["samples"] / (wave["rate"] * v.ratio * rate / wave["rate"])
            end = max(end, v.end if v.end is not None else v.start + dur)
            voices.append(v)
            res.notes.append([v.start, tno, 60, 127, None])
    res.loop = held_loop
    end = held_loop[1] if held_loop else min(end, max_seconds)
    Sequencer(bf, waves, rate)._synth(res, voices, end, tails=held_loop is None)
    return res


def render(bf, sound, waves=None, rate=RATE):
    if sound.kind == "SEQ":
        return render_sequence(bf, sound, waves, rate=rate)
    if sound.kind == "WSD":
        return render_wave_sound(bf, sound, waves, rate=rate)
    raise ValueError(f"{sound.label}: {sound.kind} sounds cannot be played")


def to_wav(pcm, rate):
    """16-bit stereo WAV bytes."""
    import io, struct as st
    x = np.clip(pcm, -1.0, 1.0)
    data = (x * 32767).astype("<i2").tobytes()
    ch = pcm.shape[1]
    fmt = st.pack("<HHIIHH", 1, ch, rate, rate * ch * 2, ch * 2, 16)
    body = b"WAVE" + b"fmt " + st.pack("<I", len(fmt)) + fmt + b"data" + st.pack("<I", len(data)) + data
    return b"RIFF" + st.pack("<I", len(body)) + body
