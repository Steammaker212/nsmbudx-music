"""Structured readers (and in-place editors) for FBNK banks and FWSD wave sound data.

FBNK INFO body: Ref(0x0100) wave id table [(wave archive id, wave index)],
Ref(0x0101) instrument table (one Ref per program, 0x5900 or empty).

    Instrument -> chooser -> KeyRegion (0x5901) -> chooser -> VelocityRegion (0x5902)

Choosers: 0x6000 direct (one Ref), 0x6001 range (u32 n, u8 upper bounds[n] padded to 4,
then n Refs), 0x6002 index (u8 min, u8 max, pad, Ref per value).

VelocityRegion and FWSD NoteInfo: u32 wave id table index, then an option bit field
with one u32 per set bit: 0 original key, 1 volume, 2 pan (byte 1 surround pan), 3 pitch
(f32), 4 note parameters (bytes: ignore note-off, key group, interpolation; banks only),
8 send (FWSD), 9 envelope (offset from the region to a Ref to attack, decay, sustain,
hold, release bytes).
"""
import struct

from .bfsar import R
from .formats import blocks

DEFAULT_ADSHR = {"attack": 127, "decay": 127, "sustain": 127, "hold": 0, "release": 127}

# editable region fields: name -> (option bit, byte inside the u32, kind)
REGION_FIELDS = {
    "key": (0, 0, "u8"), "volume": (1, 0, "u8"), "pan": (2, 0, "u8"), "surround_pan": (2, 1, "s8"),
    "pitch": (3, 0, "f32"), "ignore_note_off": (4, 0, "u8"), "key_group": (4, 1, "u8"),
    "interpolation": (4, 2, "u8"),
}
ADSHR_ORDER = ("attack", "decay", "sustain", "hold", "release")


def _bitslot(r, at, bit):
    """Offset of the u32 for option `bit` in the bit field at `at`, or None."""
    flags = r.u32(at)
    if not flags & (1 << bit):
        return None
    return at + 4 + 4 * bin(flags & ((1 << bit) - 1)).count("1")


def read_region(r, o):
    """Fields of a velocity region / note info at o, with the byte offset of each."""
    reg = {"wave": r.u32(o), "offsets": {"wave": o}}
    for name, (bit, byte, kind) in REGION_FIELDS.items():
        slot = _bitslot(r, o + 4, bit)
        if slot is None:
            continue
        at = slot + byte
        reg["offsets"][name] = at
        reg[name] = (r.u8(at) if kind == "u8" else struct.unpack_from("<b", r.d, at)[0]
                     if kind == "s8" else struct.unpack_from("<f", r.d, at)[0])
    slot = _bitslot(r, o + 4, 9)
    reg["adshr"] = dict(DEFAULT_ADSHR)
    if slot is not None:
        ref = o + r.u32(slot)
        t, off = r.ref(ref)
        a = ref + off
        for k, name in enumerate(ADSHR_ORDER):
            reg["adshr"][name] = r.u8(a + k)
            reg["offsets"][name] = a + k
    reg.setdefault("key", 60)
    reg.setdefault("volume", 127)
    reg.setdefault("pan", 64)
    reg.setdefault("pitch", 1.0)
    return reg


def _chooser(r, o):
    """(chooser type, [(lo, hi, item type, item offset)]) for the chooser Ref at o.

    Entry Refs in range and index tables are relative to the table, not to themselves."""
    t, off = r.ref(o)
    if off == -1 or t == 0:
        return t, []
    tgt = o + off
    out = []
    if t == 0x6000:
        it, io = r.ref(tgt)
        if io != -1:
            out.append((0, 127, it, tgt + io))
    elif t == 0x6001:
        n = r.u32(tgt)
        refs = tgt + ((4 + n + 3) & ~3)
        lo = 0
        for k in range(n):
            hi = r.u8(tgt + 4 + k)
            it, io = r.ref(refs + 8 * k)
            if io != -1:
                out.append((lo, hi, it, tgt + io))
            lo = hi + 1
    elif t == 0x6002:
        lo, hi = r.u8(tgt), r.u8(tgt + 1)
        for k in range(hi - lo + 1):
            it, io = r.ref(tgt + 4 + 8 * k)
            if io != -1:
                out.append((lo + k, lo + k, it, tgt + io))
    return t, out


CHOOSER_NAMES = {0x6000: "direct", 0x6001: "range", 0x6002: "index"}


class Bank:
    """A parsed FBNK. `instruments[program]` is None or a list of key regions, each
    {lo, hi, velocity: [region dicts with lo/hi]}."""

    def __init__(self, blob):
        assert blob[:4] == b"FBNK", blob[:4]
        self.blob = bytearray(blob)
        r = R(self.blob)
        i = blocks(self.blob)[0x5800][0] + 8
        _, woff = r.ref(i)
        tab = i + woff
        self.waves = [(r.u32(tab + 4 + 8 * k), r.u32(tab + 8 + 8 * k)) for k in range(r.u32(tab))]
        _, ioff = r.ref(i + 8)
        self.instruments = []
        for t, o in r.ref_table(i + ioff):
            if o is None or t != 0x5900:
                self.instruments.append(None)
                continue
            kt, keys = _chooser(r, o)
            regions = []
            for lo, hi, kt2, kr in keys:
                # a key region holds a velocity chooser; some banks point straight at a region
                vt, vels = (_chooser(r, kr) if kt2 == 0x5901 else (0x6000, [(0, 127, kt2, kr)]))
                vel_regions = []
                for vlo, vhi, vt3, vr in vels:
                    if vt3 != 0x5902:
                        continue
                    reg = read_region(r, vr)
                    reg["lo"], reg["hi"] = vlo, vhi
                    vel_regions.append(reg)
                regions.append({"lo": lo, "hi": hi, "chooser": CHOOSER_NAMES.get(vt, hex(vt)),
                                "velocity": vel_regions})
            self.instruments.append({"chooser": CHOOSER_NAMES.get(kt, hex(kt)), "keys": regions})

    def region(self, program, key, velocity=127):
        inst = self.instruments[program] if 0 <= program < len(self.instruments) else None
        if not inst:
            return None
        for kr in inst["keys"]:
            if kr["lo"] <= key <= kr["hi"]:
                for vr in kr["velocity"]:
                    if vr["lo"] <= velocity <= vr["hi"]:
                        return vr
        return None

    def set_field(self, program, key_index, vel_index, name, value):
        """Change one existing field of a velocity region in place."""
        reg = self.instruments[program]["keys"][key_index]["velocity"][vel_index]
        at = reg["offsets"].get(name)
        if at is None:
            raise KeyError(f"this region stores no {name}")
        if name == "wave":
            struct.pack_into("<I", self.blob, at, int(value))
        elif name == "pitch":
            struct.pack_into("<f", self.blob, at, float(value))
        elif name == "surround_pan":
            struct.pack_into("<b", self.blob, at, int(value))
        else:
            v = int(value)
            if not 0 <= v <= 255:
                raise ValueError(f"{name} must be 0-255")
            self.blob[at] = v
        reg[name] = value

    def summary(self):
        out = []
        for p, inst in enumerate(self.instruments):
            if not inst:
                continue
            out.append({"program": p, "chooser": inst["chooser"], "keys": [
                {"lo": k["lo"], "hi": k["hi"], "chooser": k["chooser"], "velocity": [
                    {f: v[f] for f in ("lo", "hi", "wave", "key", "volume", "pan", "pitch") if f in v}
                    | {"adshr": v["adshr"], "editable": sorted(v["offsets"])} for v in k["velocity"]]}
                for k in inst["keys"]]})
        return out


class WaveSoundFile:
    """A parsed FWSD: wave id table and, per wave sound, its info, tracks and notes."""

    def __init__(self, blob):
        assert blob[:4] == b"FWSD", blob[:4]
        self.blob = bytearray(blob)
        r = R(self.blob)
        i = blocks(self.blob)[0x6800][0] + 8
        _, woff = r.ref(i)
        tab = i + woff
        self.waves = [(r.u32(tab + 4 + 8 * k), r.u32(tab + 8 + 8 * k)) for k in range(r.u32(tab))]
        _, doff = r.ref(i + 8)
        self.sounds = []
        for _, d in r.ref_table(i + doff):
            if d is None:
                self.sounds.append(None)
                continue
            _, ioff = r.ref(d)
            info = d + ioff
            sound = {"pan": 64, "pitch": 1.0, "adshr": dict(DEFAULT_ADSHR)}
            slot = _bitslot(r, info, 0)
            if slot is not None:
                sound["pan"] = r.u8(slot)
            slot = _bitslot(r, info, 1)
            if slot is not None:
                sound["pitch"] = struct.unpack_from("<f", self.blob, slot)[0]
            slot = _bitslot(r, info, 9)
            if slot is not None:
                ref = info + r.u32(slot)
                _, off = r.ref(ref)
                sound["adshr"] = {n: r.u8(ref + off + k) for k, n in enumerate(ADSHR_ORDER)}
            _, toff = r.ref(d + 8)
            tracks = []
            for _, t in r.ref_table(d + toff):
                if t is None:
                    continue
                _, eoff = r.ref(t)
                events = []
                for _, e in r.ref_table(t + eoff):
                    if e is not None:
                        pos, length = struct.unpack_from("<ff", self.blob, e)
                        events.append({"position": pos, "length": length, "note": r.u32(e + 8)})
                tracks.append(events)
            _, noff = r.ref(d + 16)
            notes = [read_region(r, n) for _, n in r.ref_table(d + noff) if n is not None]
            sound["tracks"], sound["notes"] = tracks, notes
            self.sounds.append(sound)
