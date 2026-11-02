"""FWAV read/write, FWAR rebuild and BFSAR FILE-block rebuild.

Writers copy the layout of the game's own files (version 1.2.0 FWAV, 1.0.0
FWAR, both little-endian):
    FWAV: header 0x40 | INFO @0x40 | DATA @align32, channel samples aligned 0x20
    FWAR: header 0x40 | INFO @0x40 (u32 n + {Ref 0x1F00, u32 size}[n]) | FILE, waves aligned 0x40
"""
import struct
import numpy as np
from .bfsar import R
from . import dspadpcm, formats as F

ENC_PCM16, ENC_DSP = 1, 2


def align(n, a):
    return (n + a - 1) // a * a


# --- read ------------------------------------------------------------------
class Wave:
    """encoding, rate, loop, loop_start, samples, channels: [{data, coefs, ps, yn1, yn2, lps, lyn1, lyn2}]"""

    @classmethod
    def parse(cls, blob):
        w = cls()
        r, b = R(blob), F.blocks(blob)
        i = b[0x7000][0] + 8
        d = b[0x7001][0] + 8
        w.encoding, w.loop = r.u8(i), bool(r.u8(i + 1))
        w.rate, w.loop_start, w.samples = r.u32(i + 4), r.u32(i + 8), r.u32(i + 12)
        w.channels = []
        for _, co in r.ref_table(i + 20):
            _, soff = r.ref(co)
            at, aoff = r.ref(co + 8)
            ch = {"offset": d + soff}
            if w.encoding == ENC_DSP:
                a = co + aoff
                ch["coefs"] = list(struct.unpack_from("<16h", blob, a))
                ch["ps"], ch["yn1"], ch["yn2"], ch["lps"], ch["lyn1"], ch["lyn2"] = \
                    struct.unpack_from("<Hhh Hhh", blob, a + 32)
            w.channels.append(ch)
        size = w.channel_bytes()
        for ch in w.channels:
            ch["data"] = blob[ch["offset"]:ch["offset"] + size]
        return w

    def channel_bytes(self):
        if self.encoding == ENC_DSP:
            return dspadpcm.frames_for(self.samples) * 8
        return self.samples * (2 if self.encoding == ENC_PCM16 else 1)

    def pcm(self):
        """(samples, channels) int16."""
        cols = []
        for ch in self.channels:
            if self.encoding == ENC_DSP:
                cols.append(dspadpcm.decode(ch["data"], ch["coefs"], self.samples, ch["yn1"], ch["yn2"]))
            elif self.encoding == ENC_PCM16:
                cols.append(np.frombuffer(ch["data"], dtype="<i2"))
            else:
                cols.append(np.frombuffer(ch["data"], dtype=np.int8).astype(np.int16) << 8)
        return np.stack(cols, 1)

    # --- write -------------------------------------------------------------
    def build(self):
        nch = len(self.channels)
        info = bytearray()
        info += struct.pack("<BBHIIII", self.encoding, int(self.loop), 0, self.rate,
                            self.loop_start, self.samples, self.loop_start)
        tab = 4 + 8 * nch
        ci_size = 20
        adpcm = 0x2E if self.encoding == ENC_DSP else 0
        info += struct.pack("<I", nch)
        for c in range(nch):
            info += struct.pack("<HHi", 0x7100, 0, tab + ci_size * c)
        chan_bytes = self.channel_bytes()
        stride = align(chan_bytes, 0x20)
        for c in range(nch):
            here = tab + ci_size * c
            ad = tab + ci_size * nch + adpcm * c
            info += struct.pack("<HHi", 0x1F00, 0, 0x18 + stride * c)
            if adpcm:
                info += struct.pack("<HHi", 0x0300, 0, ad - here)
            else:
                info += struct.pack("<HHi", 0, 0, -1)
            info += struct.pack("<I", 0)
        if adpcm:
            for ch in self.channels:
                info += struct.pack("<16h", *ch["coefs"])
                info += struct.pack("<Hhh Hhh H", ch["ps"], ch["yn1"], ch["yn2"],
                                    ch["lps"], ch["lyn1"], ch["lyn2"], 0)
        info_block = b"INFO" + struct.pack("<I", align(8 + len(info), 0x20)) + bytes(info)
        info_block += b"\0" * (align(len(info_block), 0x20) - len(info_block))
        data = bytearray(b"\0" * 0x18)
        for ch in self.channels:
            data += ch["data"] + b"\0" * (stride - len(ch["data"]))
        data_block = b"DATA" + struct.pack("<I", 8 + len(data)) + bytes(data)
        info_off = 0x40
        data_off = info_off + len(info_block)
        total = data_off + len(data_block)
        hdr = struct.pack("<4sHHIIHH", b"FWAV", 0xFEFF, 0x40, 0x00010200, total, 2, 0)
        hdr += struct.pack("<HHiI", 0x7000, 0, info_off, len(info_block))
        hdr += struct.pack("<HHiI", 0x7001, 0, data_off, len(data_block))
        hdr += b"\0" * (0x40 - len(hdr))
        return hdr + info_block + data_block

    @classmethod
    def from_pcm(cls, pcm, rate, loop_start=None, workers=None):
        """pcm: (samples, channels) int16.  Encodes DSP-ADPCM with loop context.

        loop_start should be a multiple of 14 (one DSP frame); every game wave's is.
        """
        w = cls()
        w.encoding, w.rate = ENC_DSP, rate
        w.loop = loop_start is not None
        w.loop_start = loop_start or 0
        w.samples = pcm.shape[0]
        w.channels = []
        for c in range(pcm.shape[1]):
            data, coefs, dec = dspadpcm.encode(pcm[:, c], workers=workers)
            ls = w.loop_start
            w.channels.append({
                "data": data, "coefs": coefs, "ps": data[0], "yn1": 0, "yn2": 0,
                "lps": data[ls // 14 * 8] if w.loop else 0,
                "lyn1": int(dec[ls - 1]) if w.loop and ls >= 1 else 0,
                "lyn2": int(dec[ls - 2]) if w.loop and ls >= 2 else 0,
            })
        return w


# --- FWAR ------------------------------------------------------------------
def build_fwar(waves):
    """waves: list of FWAV byte strings -> FWAR bytes (game layout)."""
    n = len(waves)
    info_body = struct.pack("<I", n)
    file_body = bytearray()
    base = 0x40 + align(8 + 4 + 12 * n, 0x20) + 8      # absolute start of FILE body
    entries = []
    for wv in waves:
        start = align(base + len(file_body), 0x40) - base
        file_body += b"\0" * (start - len(file_body))
        entries.append((start, len(wv)))
        file_body += wv
    for off, size in entries:
        info_body += struct.pack("<HHiI", 0x1F00, 0, off, size)
    info = b"INFO" + struct.pack("<I", align(8 + len(info_body), 0x20)) + info_body
    info += b"\0" * (align(len(info), 0x20) - len(info))
    file_block = b"FILE" + struct.pack("<I", 8 + len(file_body)) + bytes(file_body)
    file_off = 0x40 + len(info)
    total = file_off + len(file_block)
    hdr = struct.pack("<4sHHIIHH", b"FWAR", 0xFEFF, 0x40, 0x00010000, total, 2, 0)
    hdr += struct.pack("<HHiI", 0x6800, 0, 0x40, len(info))
    hdr += struct.pack("<HHiI", 0x6801, 0, file_off, len(file_block))
    hdr += b"\0" * (0x40 - len(hdr))
    return hdr + info + file_block


def fwar_waves(blob):
    return [blob[o:o + s] for o, s in F.fwar_entries(blob)]


# --- BFSAR -----------------------------------------------------------------
def rebuild_bfsar(bf, replace):
    """replace: {file id: new bytes}.  Returns new archive bytes.

    Only the FILE block is re-laid out (files kept in id order, aligned 0x40
    absolute like the original); INFO internal-file refs/sizes and the header,
    block table and FILE block sizes are patched in place.  Groups in this
    archive embed nothing, so no FGRP needs touching.
    """
    data = bf.data
    r = bf.r
    file_blk = bf.blocks[0x2002][0]
    body = file_blk + 8
    order = sorted((f for f in bf.files if f.get("offset") is not None), key=lambda f: f["offset"])
    out_body = bytearray()
    new_loc = {}
    for f in order:
        blob = replace.get(f["id"], data[f["offset"]:f["offset"] + f["size"]])
        start = align(body + len(out_body), 0x40) - body
        out_body += b"\0" * (start - len(out_body))
        new_loc[f["id"]] = (start, len(blob))
        out_body += blob
    head = bytearray(data[:file_blk])
    # patch INFO file infos
    for i, (t, o) in enumerate(bf._section(6)):
        lt, loff = r.ref(o)
        if lt != 0x220C or i not in new_loc:
            continue
        loc = o + loff
        off, size = new_loc[i]
        struct.pack_into("<i", head, loc + 4, off)
        struct.pack_into("<I", head, loc + 8, size)
    file_block = b"FILE" + struct.pack("<I", 8 + len(out_body)) + bytes(out_body)
    total = file_blk + len(file_block)
    struct.pack_into("<I", head, 0x0C, total)
    for k in range(r.u16(0x10)):
        e = 0x14 + 12 * k
        if r.u16(e) == 0x2002:
            struct.pack_into("<I", head, e + 8, len(file_block))
    return bytes(head) + file_block
