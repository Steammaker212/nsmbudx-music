"""BFSAR (nn::atk sound archive) reader for NSMBUDX romfs/Sound/Sound.bfsar.

Layout follows nn::atk::detail::SoundArchiveFile, little-endian, version
2.5.0.  Every offset in a block is relative to the start of the block body
(block header + 8) unless noted.
"""
import csv, os, struct
from dataclasses import dataclass, field

ITEM_TYPES = {1: "sound", 2: "soundset", 3: "bank", 4: "player", 5: "wavearc", 6: "group"}
SOUND_KINDS = {0x2201: "STRM", 0x2202: "WSD", 0x2203: "SEQ"}
NULL = 0xFFFFFFFF


class R:
    def __init__(self, data):
        self.d = data

    def u8(self, o):  return self.d[o]
    def u16(self, o): return struct.unpack_from("<H", self.d, o)[0]
    def u32(self, o): return struct.unpack_from("<I", self.d, o)[0]
    def s32(self, o): return struct.unpack_from("<i", self.d, o)[0]
    def f32(self, o): return struct.unpack_from("<f", self.d, o)[0]

    def ref(self, o):
        """Reference: u16 typeId, u16 pad, s32 offset.  Returns (type, offset)."""
        return self.u16(o), self.s32(o + 4)

    def ref_table(self, o):
        """u32 count + Reference[count]; offsets relative to the table."""
        n = self.u32(o)
        return [(t, None if off == -1 else o + off)
                for t, off in (self.ref(o + 4 + 8 * i) for i in range(n))]

    def id_table(self, o):
        n = self.u32(o)
        return [self.u32(o + 4 + 4 * i) for i in range(n)]

    def options(self, o):
        """OptionParameter: u32 bitflag, then one u32 per set bit, low bit first."""
        flags, vals, p = self.u32(o), {}, o + 4
        for bit in range(32):
            if flags & (1 << bit):
                vals[bit] = self.u32(p)
                p += 4
        return vals


@dataclass
class Sound:
    id: int
    label: str
    kind: str
    file_id: int
    player_id: int
    volume: int
    remote_filter: int
    options: dict
    detail: dict = field(default_factory=dict)


class Bfsar:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        r = self.r = R(self.data)
        assert self.data[:4] == b"FSAR", "not an FSAR"
        assert r.u16(4) == 0xFEFF, "big-endian FSAR not supported"
        self.version = r.u32(8)
        nblocks = r.u16(0x10)
        self.blocks = {}
        for i in range(nblocks):
            t, off = r.ref(0x14 + 12 * i)
            self.blocks[t] = (off, r.u32(0x14 + 12 * i + 8))
        self.strg = self.blocks[0x2000][0] + 8
        self.info = self.blocks[0x2001][0] + 8
        self.file = self.blocks[0x2002][0] + 8
        self._read_strings()
        self._read_info()

    # --- STRG ------------------------------------------------------------
    def _read_strings(self):
        r, b = self.r, self.strg
        t, off = r.ref(b)
        assert t == 0x2400
        tab = b + off
        n = r.u32(tab)
        self.strings = []
        for i in range(n):
            e = tab + 4 + 12 * i
            s_off, size = r.s32(e + 4), r.u32(e + 8)
            self.strings.append(self.data[tab + s_off: tab + s_off + size - 1].decode("ascii"))

    def label(self, opts):
        sid = opts.get(0)
        return self.strings[sid] if sid is not None and sid != NULL else ""

    # --- INFO ------------------------------------------------------------
    def _section(self, i):
        t, off = self.r.ref(self.info + 8 * i)
        return self.r.ref_table(self.info + off)

    def _read_info(self):
        r = self.r
        self.sounds = []
        for i, (t, o) in enumerate(self._section(0)):
            dt, doff = r.ref(o + 12)
            opts = r.options(o + 20)
            s = Sound(0x01000000 | i, self.label(opts), SOUND_KINDS.get(dt, hex(dt)),
                      r.u32(o), r.u32(o + 4), r.u8(o + 8), r.u8(o + 9), opts)
            d = o + doff
            if dt == 0x2201:
                s.detail = self._stream_detail(d)
            elif dt == 0x2202:
                s.detail = {"index": r.u32(d), "track_count": r.u32(d + 4),
                            "wsd_opts": r.options(d + 8)}
            elif dt == 0x2203:
                bt, boff = r.ref(d)
                s.detail = {"banks": r.id_table(d + boff), "track_flags": r.u32(d + 8),
                            "seq_opts": r.options(d + 12)}
            self.sounds.append(s)

        self.soundsets = []
        for i, (t, o) in enumerate(self._section(1)):
            _, foff = r.ref(o + 8)
            self.soundsets.append({"id": 0x02000000 | i, "start": r.u32(o), "end": r.u32(o + 4),
                                   "files": r.id_table(o + foff),
                                   "label": self.label(r.options(o + 24))})
        self.banks = []
        for i, (t, o) in enumerate(self._section(2)):
            _, woff = r.ref(o + 4)
            self.banks.append({"id": 0x03000000 | i, "file": r.u32(o),
                               "wavearcs": r.id_table(o + woff), "label": self.label(r.options(o + 12))})
        self.wavearcs = []
        for i, (t, o) in enumerate(self._section(3)):
            opts = r.options(o + 8)
            self.wavearcs.append({"id": 0x05000000 | i, "file": r.u32(o),
                                  "individual": r.u8(o + 4), "label": self.label(opts),
                                  "wave_count": opts.get(1)})
        self.groups = []
        for i, (t, o) in enumerate(self._section(4)):
            self.groups.append({"id": 0x06000000 | i, "file": r.u32(o),
                                "label": self.label(r.options(o + 4))})
        self.players = []
        for i, (t, o) in enumerate(self._section(5)):
            opts = r.options(o + 4)
            self.players.append({"id": 0x04000000 | i, "max": r.u32(o),
                                 "label": self.label(opts), "heap": opts.get(1)})
        self.files = []
        for i, (t, o) in enumerate(self._section(6)):
            lt, loff = r.ref(o)
            loc = o + loff
            f = {"id": i, "loc": hex(lt)}
            if lt == 0x220C:          # internal: ref to image in FILE body, size, attached groups
                it, ioff = r.ref(loc)
                f["offset"] = None if ioff == -1 else self.file + ioff
                f["size"] = r.u32(loc + 8)
                gt, goff = r.ref(loc + 12)
                f["groups"] = r.id_table(loc + goff) if goff != -1 else []
            elif lt == 0x220D:        # external path
                end = self.data.index(b"\0", loc)
                f["path"] = self.data[loc:end].decode("ascii")
            if f.get("offset") is not None:
                f["magic"] = self.data[f["offset"]:f["offset"] + 4].decode("latin1")
            self.files.append(f)
        t, off = r.ref(self.info + 8 * 7)
        p = self.info + off
        self.player_info = dict(zip(
            ["seq_max", "seq_track_max", "strm_max", "strm_track_max", "strm_channel_max",
             "wsd_max", "wsd_track_max"], struct.unpack_from("<7H", self.data, p)))
        self.player_info["strm_buffer_times"] = r.u8(p + 14)

    def _stream_detail(self, d):
        r = self.r
        tracks = []
        tt, toff = r.ref(d + 4)
        if toff != -1:
            for _, to in r.ref_table(d + toff):
                if to is None:
                    continue
                ct, coff = r.ref(to + 4)
                chans = r.id_table(to + coff) if coff != -1 else []
                tracks.append({"volume": r.u8(to), "pan": r.u8(to + 1), "span": r.u8(to + 2),
                               "flags": r.u8(to + 3), "channels": chans})
        _, extoff = r.ref(d + 20)
        return {"track_flags": r.u16(d), "channel_count": r.u16(d + 2), "pitch": r.f32(d + 12),
                "tracks": tracks, "prefetch_file": r.u32(d + 28),
                "ext": None if extoff == -1 else d + extoff}

    # --- helpers ---------------------------------------------------------
    def file_bytes(self, fid):
        f = self.files[fid]
        if f.get("offset") is None:
            return None
        return self.data[f["offset"]:f["offset"] + f["size"]]


def dump_csv(bf, out):
    """Write sounds.csv, files.csv and one CSV per item table to out/."""
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "sounds.csv"), "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["id", "label", "kind", "file_id", "file_magic", "player", "volume",
                    "detail"])
        for s in bf.sounds:
            f = bf.files[s.file_id] if s.file_id < len(bf.files) else {}
            pl = bf.players[s.player_id & 0xFFFFFF]["label"] if s.player_id != NULL else ""
            det = dict(s.detail); det.pop("ext", None)
            w.writerow([hex(s.id), s.label, s.kind, s.file_id, f.get("magic", f.get("path", "")),
                        pl, s.volume, det])
    with open(os.path.join(out, "files.csv"), "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["id", "loc", "magic", "offset", "size", "groups", "path"])
        for f in bf.files:
            w.writerow([f["id"], f["loc"], f.get("magic", ""), hex(f["offset"]) if f.get("offset") else "",
                        f.get("size", ""), " ".join(hex(g) for g in f.get("groups", [])), f.get("path", "")])
    for name in ("soundsets", "banks", "wavearcs", "groups", "players"):
        rows = getattr(bf, name)
        with open(os.path.join(out, name + ".csv"), "w", newline="") as fp:
            w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()) if rows else ["id"])
            w.writeheader()
            for row in rows:
                w.writerow({k: hex(v) if k == "id" else v for k, v in row.items()})


def summary(bf):
    return (f"FSAR {bf.version:#x}: {len(bf.strings)} strings, {len(bf.sounds)} sounds, "
            f"{len(bf.soundsets)} sound sets, {len(bf.banks)} banks, {len(bf.wavearcs)} wave archives, "
            f"{len(bf.groups)} groups, {len(bf.players)} players, {len(bf.files)} files")
