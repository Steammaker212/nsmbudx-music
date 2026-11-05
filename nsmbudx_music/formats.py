"""Readers for the files inside an FSAR: FWAR, FWAV, FBNK, FSEQ, FWSD, FGRP.

All are NW4F/nn::atk binary files with the common FileHeader:
    0x00 magic, 0x04 BOM 0xFEFF, 0x06 header size, 0x08 version,
    0x0C file size, 0x10 u16 block count, 0x14 block refs {u16 type, s32 offset, u32 size}.
"""
import struct
from .bfsar import R

DSP_ADPCM, PCM8, PCM16 = 2, 0, 1
ENCODINGS = {0: "PCM8", 1: "PCM16", 2: "DSP-ADPCM", 3: "IMA-ADPCM"}


def blocks(blob):
    r = R(blob)
    n = r.u16(0x10)
    return {r.u16(0x14 + 12 * i): (r.s32(0x18 + 12 * i), r.u32(0x1C + 12 * i)) for i in range(n)}


# --- FWAR ------------------------------------------------------------------
def fwar_entries(blob):
    """[(offset, size)] of each FWAV inside a wave archive, offsets into blob."""
    assert blob[:4] == b"FWAR", blob[:4]
    r, b = R(blob), blocks(blob)
    info, file_ = b[0x6800][0], b[0x6801][0]
    n = r.u32(info + 8)
    out = []
    for i in range(n):
        e = info + 12 + 12 * i
        off = r.s32(e + 4)
        out.append((file_ + 8 + off, r.u32(e + 8)))
    return out


# --- FWAV ------------------------------------------------------------------
def fwav_info(blob):
    """Header facts of an FWAV: encoding, rate, loop, sample count, channels."""
    assert blob[:4] == b"FWAV", blob[:4]
    r, b = R(blob), blocks(blob)
    i = b[0x7000][0] + 8
    return {
        "encoding": ENCODINGS.get(r.u8(i), r.u8(i)),
        "loop": bool(r.u8(i + 1)),
        "rate": r.u32(i + 4),
        "loop_start": r.u32(i + 8),
        "samples": r.u32(i + 12),          # loop end == sample count
        "channels": r.u32(i + 20),
        "size": r.u32(0x0C),
        "version": hex(r.u32(8)),
    }


# --- FBNK ------------------------------------------------------------------
def fbnk_waves(blob):
    """Wave id table of a bank: [(wave archive item id, wave index)]."""
    assert blob[:4] == b"FBNK", blob[:4]
    r, b = R(blob), blocks(blob)
    i = b[0x5800][0] + 8
    t, off = r.ref(i)            # 0x0100 wave id table
    tab = i + off
    n = r.u32(tab)
    return [(r.u32(tab + 4 + 8 * k), r.u32(tab + 8 + 8 * k)) for k in range(n)]


def fbnk_instruments(blob):
    """Per program number: [velocity regions {wave (index into wave id table), key, volume, pan}].

    Instrument 0x5900 -> chooser -> KeyRegion 0x5901 -> chooser -> VelocityRegion 0x5902.
    Choosers: 0x6000 direct (one Ref), 0x6001 range table, 0x6002 index table; we walk them
    all.  VelocityRegion: u32 wave id table index, OptionParameter (bit0 original key,
    bit1 volume, bit2 pan, bit3 pitch f32, ...).
    """
    r, b = R(blob), blocks(blob)
    i = b[0x5800][0] + 8
    t, off = r.ref(i + 8)        # 0x0101 instrument ref table

    def walk(o, depth=0):
        """o points at a chooser (0x6000/1/2 Ref).  Yields velocity region offsets."""
        if depth > 6:
            return
        t, off = r.ref(o)
        if off == -1:
            return
        tgt = o + off
        if t == 0x6000:                       # direct: chooser body is one Ref
            yield from walk_item(tgt, depth)
        elif t == 0x6001:                     # range: u32 n, u8 end keys[n] aligned, n bare Refs
            n = r.u32(tgt)
            rt = tgt + align4(4 + n)
            for k in range(n):
                if rt + 8 * k + 8 > len(blob):
                    break
                yield from walk_item(rt + 8 * k, depth)
        elif t == 0x6002:                     # index: u8 min, u8 max, pad, Ref[]
            lo, hi = r.u8(tgt), r.u8(tgt + 1)
            for k in range(hi - lo + 1):
                it, io = r.ref(tgt + 4 + 8 * k)
                if io != -1:
                    yield from walk_item(tgt + io, depth)

    def walk_item(o, depth):
        t, off = r.ref(o)
        if off == -1:
            return
        if t == 0x5902:
            yield o + off
        else:                                 # 0x5901 key region / nested chooser
            yield from walk(o + off if t in (0x5901,) else o, depth + 1)

    out = []
    for t, o in r.ref_table(i + off):
        regions = []
        if o is not None and t == 0x5900:
            for v in walk(o):
                opt = r.options(v + 4)
                regions.append({"wave": r.u32(v), "key": opt.get(0), "volume": opt.get(1),
                                "pan": opt.get(2)})
        out.append(regions)
    return out


def align4(n):
    return (n + 3) & ~3


# --- FSEQ ------------------------------------------------------------------
def fseq_labels(blob):
    """{label: offset into DATA body} from the LABL block."""
    assert blob[:4] == b"FSEQ", blob[:4]
    r, b = R(blob), blocks(blob)
    lab = b[0x5001][0] + 8
    out = {}
    for t, o in r.ref_table(lab):
        _, doff = r.ref(o)
        n = r.u32(o + 8)
        out[blob[o + 12:o + 12 + n].decode("ascii")] = doff
    return out


def fseq_data(blob):
    b = blocks(blob)
    off, size = b[0x5000]
    return blob[off + 8: off + size]


# --- FWSD ------------------------------------------------------------------
def fwsd_waves(blob):
    """Wave id table of a wave-sound-data file: [(wave archive item id, wave index)]."""
    assert blob[:4] == b"FWSD", blob[:4]
    r, b = R(blob), blocks(blob)
    i = b[0x6800][0] + 8
    t, off = r.ref(i)            # 0x0100 wave id table
    tab = i + off
    n = r.u32(tab)
    return [(r.u32(tab + 4 + 8 * k), r.u32(tab + 8 + 8 * k)) for k in range(n)]


def fwsd_sound_waves(blob, index):
    """Waves played by wave sound `index`: its note infos -> wave id table entries.

    WaveSoundData: Ref info, Ref track table, Ref note table;
    NoteInfo: u32 wave id table index, OptionParameter.
    """
    r, b = R(blob), blocks(blob)
    i = b[0x6800][0] + 8
    waves = fwsd_waves(blob)
    _, doff = r.ref(i + 8)       # 0x0101 wave sound data table
    d = r.ref_table(i + doff)[index][1]
    _, noff = r.ref(d + 16)
    return [waves[r.u32(o)] for _, o in r.ref_table(d + noff)]


# --- FSEQ disassembly ------------------------------------------------------
_SEQ = {0x80: "wait", 0x81: "prg", 0x88: "opentrack", 0x89: "jump", 0x8A: "call",
        0xFE: "alloctrack", 0xFF: "fin", 0xFD: "ret", 0xFC: "loop_end", 0xD4: "loop_start"}
_C0 = {0xB0: "timebase", 0xC0: "pan", 0xC1: "volume", 0xC2: "main_volume", 0xC3: "transpose", 0xC4: "pitchbend",
       0xC5: "bendrange", 0xC6: "prio", 0xC7: "notewait", 0xC8: "tie", 0xC9: "porta",
       0xCA: "mod_depth", 0xCB: "mod_speed", 0xCC: "mod_type", 0xCD: "mod_range",
       0xCE: "porta_sw", 0xCF: "porta_time", 0xD0: "attack", 0xD1: "decay", 0xD2: "sustain",
       0xD3: "release", 0xD4: "loop_start", 0xD5: "volume2", 0xD6: "printvar",
       0xD7: "span", 0xD8: "lpf_cutoff", 0xD9: "fxsend_a", 0xDA: "fxsend_b",
       0xDB: "mainsend", 0xDC: "init_pan", 0xDD: "mute", 0xDE: "fxsend_c", 0xDF: "damper"}


def _varlen(d, p):
    v = 0
    while True:
        c = d[p]; p += 1
        v = (v << 7) | (c & 0x7F)
        if not c & 0x80:
            return v, p


def seq_disasm(data, start, limit=200):
    """Linear disassembly of nn::atk sequence bytecode from `start` (DATA body offset)."""
    out, p = [], start
    while p < len(data) and len(out) < limit:
        at, c = p, data[p]; p += 1
        if c < 0x80:
            if p + 1 >= len(data):   # zero padding after the last command
                break
            vel = data[p]; p += 1
            ln, p = _varlen(data, p)
            out.append((at, f"note {c} vel {vel} len {ln}"))
        elif c in (0x80, 0x81):
            v, p = _varlen(data, p)
            out.append((at, f"{_SEQ[c]} {v}"))
        elif c == 0x88:
            out.append((at, f"opentrack {data[p]} -> {int.from_bytes(data[p+1:p+4], 'little')}")); p += 4
        elif c in (0x89, 0x8A):
            out.append((at, f"{_SEQ[c]} -> {int.from_bytes(data[p:p+3], 'little')}")); p += 3
        elif 0xB0 <= c <= 0xDF:  # 1-byte arg; 0xB0 = timebase
            out.append((at, f"{_C0.get(c, hex(c))} {data[p]}")); p += 1
        elif c == 0xE0:
            out.append((at, f"lfo_delay {int.from_bytes(data[p:p+2], 'little')}")); p += 2
        elif c == 0xE1:
            out.append((at, f"tempo {int.from_bytes(data[p:p+2], 'little')}")); p += 2
        elif c == 0xE3:
            out.append((at, f"sweep_pitch {int.from_bytes(data[p:p+2], 'little', signed=True)}")); p += 2
        elif c == 0xF0:
            sub = data[p]; p += 1
            out.append((at, f"ext {sub:#x} {data[p:p+3].hex()}")); p += 3
        elif c == 0xFE:
            out.append((at, f"alloctrack {int.from_bytes(data[p:p+2], 'little'):#06x}")); p += 2
        elif c in (0xFC, 0xFD, 0xFF):
            out.append((at, _SEQ[c]))
            if c == 0xFF:
                break
        elif c in (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5):
            out.append((at, f"prefix {c:#x}"))       # random/var/if/time prefixes
        else:
            out.append((at, f"?? {c:#x}"))
    return out


# --- FGRP ------------------------------------------------------------------
def fgrp_items(blob):
    """[(file id, offset, size)] of files embedded in a group."""
    assert blob[:4] == b"FGRP", blob[:4]
    r, b = R(blob), blocks(blob)
    i = b[0x7800][0] + 8
    file_ = b[0x7801][0] + 8
    out = []
    for t, o in r.ref_table(i):
        fid = r.u32(o)
        _, off = r.ref(o + 4)
        out.append((fid, None if off == -1 else file_ + off, r.u32(o + 12)))
    return out
