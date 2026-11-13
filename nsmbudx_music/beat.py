"""Read and rewrite the beat track (bahs, beat accents, loop) and the layers of course music.

Course music, the game's and added, is a sequence whose start code opens the layer
tracks and then jumps into track 0, the beat track. Track 0 writes sequence variable 0
(the enemies' "bah", values 1-4) and variable 1 (the beat accent, 1-3) at fixed ticks
and jumps back at the end of its loop.

read_beat runs the sequence (without synthesising audio) and collects track 0's writes
in ticks. write_beat appends a new track 0 to the sequence and points the start code's
jump at it, so the layers and every other label stay as they were.
"""
import struct

from . import banks, fwav, render, seq as S
from . import formats as F
from .dspadpcm import frames_for

BAH, ACCENT = 0, 1


def _start_jump(data, start):
    """Offset of the `jump` that ends the start code at `start`, and the tracks it opens."""
    p, opened = start, []
    for _ in range(64):
        c = S.decode(data, p)
        if c.op == 0x88:
            opened.append((c.track, c.target))
        elif c.op == 0x89 and not c.cond:
            return p, c.target, opened
        elif c.op in (0xff, 0xfd) and not c.cond:
            break
        p += c.size
    raise ValueError("this sound does not start like course music (no jump into a beat track)")


def layer_tracks(bf, sound):
    """[{track, program, warc, wave, volume}] for the tracks a course sound opens."""
    blob = bf.file_bytes(sound.file_id)
    data = F.fseq_data(blob)
    start = sound.detail["seq_opts"].get(0, 0)
    _, _, opened = _start_jump(data, start)
    flags = sound.detail.get("track_flags") or 0xFFFF
    bank_ids = sound.detail["banks"]
    out = []
    for track, target in opened:
        if not flags >> track & 1:
            continue
        prg, bank_sel, p = 0, 0, target
        for _ in range(32):                     # the layer's first note, after its prg
            c = S.decode(data, p)
            if c.op == 0x81:
                prg = c.args[-1]
            elif c.op == 0xb6:
                bank_sel = c.args[-1]
            elif c.op < 0x80 or c.op in (0x89, 0xff):
                break
            p += c.size
        if bank_sel >= len(bank_ids):
            continue
        bank = banks.Bank(bf.file_bytes(bf.banks[bank_ids[bank_sel] & 0xFFFFFF]["file"]))
        reg = bank.region(prg, 60)
        if reg is None or reg["wave"] >= len(bank.waves):
            continue
        warc, wave = bank.waves[reg["wave"]]
        out.append({"track": track, "program": prg, "warc": warc, "wave": wave,
                    "volume": reg.get("volume", 127)})
    return out


def read_beat(bf, sound):
    """{tempo, timebase, events [(tick, var, value)], loop (start, end) ticks, seconds per tick}."""
    data = F.fseq_data(bf.file_bytes(sound.file_id))
    start = sound.detail["seq_opts"].get(0, 0)
    _start_jump(data, start)                    # refuses sounds without a beat track
    r = render.Sequencer(bf).run(data, start, sound.detail["banks"], max_seconds=900,
                                 track_flags=sound.detail.get("track_flags") or None,
                                 synth=False, beat_loop=True)
    tempo, timebase = 120, 48
    for _, t, tb in r.tempo:
        tempo, timebase = t, tb
    events = [(tick, var, val) for tick, (_, tr, var, val) in zip(r.var_ticks, r.vars)
              if tr == 0 and var in (BAH, ACCENT)]
    loop = r.loop_ticks
    if loop is None:                            # plays once: the beat track's own end
        loop = (None, r.ticks)
    else:
        events = [e for e in events if e[0] < loop[1]]
    return {"tempo": tempo, "timebase": timebase, "events": events, "loop": loop,
            "spt": 60.0 / (tempo * timebase)}


def beat_code(tempo, timebase, events, loop_start, end):
    """Track 0 bytecode: tempo, then the writes with waits, looping from loop_start to end."""
    code = bytearray(b"\xb0" + bytes([timebase]) + b"\xc7\x00\xe1" + struct.pack("<H", tempo) + b"\xc1\x00")
    loop_off, now = None, 0
    marks = sorted({t for t, _, _ in events if 0 <= t < end} | ({loop_start} if loop_start is not None else set()))
    by_tick = {}
    for t, var, val in sorted(events, key=lambda e: e[0]):     # same tick: keep their order
        if 0 <= t < end:
            by_tick.setdefault(t, []).append((var, val))
    for t in marks + [end]:
        if t > now:
            code += b"\x80" + S._vlq(t - now)
            now = t
        if t == end:
            break
        if t == loop_start and loop_off is None:
            loop_off = len(code)
        for var, val in by_tick.get(t, []):
            code += b"\xf0\x80" + bytes([var]) + struct.pack("<h", val)
    return code, loop_off


def write_beat(blob, start, tempo, timebase, events, loop_start, end):
    """The FSEQ with a new beat track for the sound starting at `start`."""
    data = F.fseq_data(blob)
    labels = F.fseq_labels(blob)
    last = max((c.offset + c.size for c in S.walk(data, labels.values()).values()), default=len(data))
    data = bytearray(data[:last])
    jump_at, _, _ = _start_jump(data, start)
    code, loop_off = beat_code(tempo, timebase, events, loop_start, end)
    base = len(data)
    if loop_start is None:
        code += b"\xff"                        # plays once
    else:
        code += b"\x89" + (base + loop_off).to_bytes(3, "little")
    data += code
    data[jump_at + 1:jump_at + 4] = base.to_bytes(3, "little")
    return S.build_fseq(bytes(data), labels, struct.unpack_from("<I", blob, 8)[0])


def set_wave_loop(blob, start, end=None, loop=True):
    """FWAV with its loop moved, without re-encoding: (bytes, start actually used).

    DSP-ADPCM loops start on a 14-sample frame, so `start` is rounded to the nearest
    frame; the loop context (predictor/scale and the two samples before) is taken from
    the wave itself. `end` cuts the wave there (the loop ends at the end of the wave)."""
    w = fwav.Wave.parse(blob)
    if end is not None and 0 < end < w.samples:
        w.samples = int(end)
        for ch in w.channels:
            ch["data"] = ch["data"][:w.channel_bytes()]
    start = max(0, min(int(round(start / 14.0)) * 14, (w.samples - 1) // 14 * 14)) if loop else 0
    w.loop, w.loop_start = bool(loop), start
    if w.encoding == fwav.ENC_DSP and loop:
        pcm = w.pcm()
        for c, ch in enumerate(w.channels):
            ch["lps"] = ch["data"][start // 14 * 8]
            ch["lyn1"] = int(pcm[start - 1, c]) if start >= 1 else 0
            ch["lyn2"] = int(pcm[start - 2, c]) if start >= 2 else 0
    return w.build(), start


def seconds(beat, tick):
    return None if tick is None else tick * beat["spt"]


def ticks(beat, sec):
    return None if sec is None else int(round(sec / beat["spt"]))
