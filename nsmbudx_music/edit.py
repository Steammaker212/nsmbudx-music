"""Rebuild a sound archive with new items.

The writer keeps every existing item byte for byte. INFO is rebuilt section by
section: each section is a reference table followed by its entries, and an entry
together with the data it points to forms one contiguous chunk, so chunks can be
moved as a whole (their internal offsets are relative to themselves). STRG gets
a fresh string table and patricia tree; FILE is laid out as in fwav.rebuild_bfsar.

New music is added as a wave sound (WSD), the kind the game already uses for
airship and boss music: one FWSD file with one wave sound, one wave archive with
one wave, and a wave sound set covering the new sound.
"""
import struct

from .bfsar import Bfsar, R
from . import formats as F

SECTION_TYPES = [0x2100, 0x2104, 0x2101, 0x2103, 0x2105, 0x2102, 0x2106]
SOUND, SOUNDSET, BANK, WAVEARC, GROUP, PLAYER, FILE = range(7)
ITEM_TYPE = {SOUND: 1, SOUNDSET: 2, BANK: 3, PLAYER: 4, WAVEARC: 5, GROUP: 6}
NO_STRING = 0xFFFFFFFF
# template for new music: a looping two-channel BGM wave sound
TEMPLATE = "STRM_BGM_TITLE"


def align(n, a):
    return (n + a - 1) // a * a


# --- patricia tree ---------------------------------------------------------
def _bit(s, i):
    pos = i >> 3
    return pos < len(s) and bool(s[pos] & (0x80 >> (i & 7)))


def build_tree(keys):
    """keys: [(label bytes, string index, item id)].  Returns tree bytes.

    nn::atk walks from the root: at an inner node, bit `n` of the key (MSB first,
    0 past the end) picks right (1) or left (0); a leaf holds the string index
    and item id, and the caller compares the label itself.
    """
    nodes = []

    def build(group):
        if len(group) == 1:
            s, si, item = group[0]
            nodes.append((1, 0xFFFF, NO_STRING, NO_STRING, si, item))
            return len(nodes) - 1
        width = max(len(k[0]) for k in group) * 8
        bit = next(i for i in range(width) if len({_bit(k[0], i) for k in group}) > 1)
        idx = len(nodes)
        nodes.append(None)
        left = build([k for k in group if not _bit(k[0], bit)])
        right = build([k for k in group if _bit(k[0], bit)])
        nodes[idx] = (0, bit, left, right, NO_STRING, NO_STRING)
        return idx

    root = build(sorted(keys))
    out = struct.pack("<II", root, len(nodes))
    return out + b"".join(struct.pack("<HHIIII", *n) for n in nodes)


def tree_lookup(tree, label):
    root, = struct.unpack_from("<I", tree, 0)
    s = label.encode()
    i = root
    while True:
        flags, bit, left, right, si, item = struct.unpack_from("<HHIIII", tree, 8 + 20 * i)
        if flags & 1:
            return si, item
        i = right if _bit(s, bit) else left


# --- archive model ---------------------------------------------------------
class Editor:
    def __init__(self, bf):
        self.bf = bf
        r, info = bf.r, bf.info
        self.strings = list(bf.strings)
        self.string_items = self._string_items()
        offs = [info + r.ref(info + 8 * k)[1] for k in range(8)]
        self.sections = []
        for k in range(7):
            ents = [o for _, o in r.ref_table(offs[k])]
            assert ents == sorted(ents), f"section {k} entries out of order"
            ends = ents[1:] + [offs[k + 1]]
            self.sections.append([bf.data[a:b] for a, b in zip(ents, ends)])
        # SoundArchivePlayerInfo: 7 u16 limits, u8 stream buffer times, pad, u32 options.
        # The bytes after it are the block's padding, which build() adds again.
        self.player_info = bf.data[offs[7]:offs[7] + 0x14]
        self.files = [bf.file_bytes(i) for i in range(len(bf.files))]

    def _string_items(self):
        bf, r = self.bf, self.bf.r
        tree = bf.strg + r.ref(bf.strg + 8)[1]
        n = r.u32(tree + 4)
        out = {}
        for i in range(n):
            flags, bit, left, right, si, item = struct.unpack_from("<HHIIII", bf.data, tree + 8 + 20 * i)
            if flags & 1:
                out[si] = item
        return out

    def truncate_to_vanilla(self):
        for k, n in VANILLA.items():
            if len(self.sections[k]) < n:
                raise ValueError("not the v1.0.0 Sound.bfsar: fewer items than the game's own")
            self.sections[k] = self.sections[k][:n]
        self.files = self.files[:VANILLA[FILE]]
        self.strings = self.strings[:VANILLA_STRINGS]
        self.string_items = {i: v for i, v in self.string_items.items() if i < VANILLA_STRINGS}

    # --- building blocks -----------------------------------------------
    def add_string(self, label, item_id):
        assert label not in self.strings, f"label {label} already exists"
        self.strings.append(label)
        self.string_items[len(self.strings) - 1] = item_id
        return len(self.strings) - 1

    def add_file(self, blob):
        self.files.append(blob)
        return len(self.files) - 1

    def next_id(self, section):
        return (ITEM_TYPE[section] << 24) | len(self.sections[section])

    # --- new music -------------------------------------------------------
    def add_wave_music(self, label, fwav, bpm=0, volume=None, template=TEMPLATE):
        """Add `label` as a wave sound playing `fwav` (FWAV bytes). Returns the sound id."""
        from .fwav import build_fwar
        bf = self.bf
        tpl = next(s for s in bf.sounds if s.label == template)
        tpl_set = next(i for i, ss in enumerate(bf.soundsets) if ss["start"] <= tpl.id <= ss["end"])

        sound_id = self.next_id(SOUND)
        set_id = self.next_id(SOUNDSET)
        war_id = self.next_id(WAVEARC)

        war_file = self.add_file(build_fwar([fwav]))
        wsd_file = self.add_file(self._single_fwsd(tpl, war_id))

        # wave archive info: file id, not loaded individually, no options (as the game's own)
        self.sections[WAVEARC].append(struct.pack("<IBxxxI", war_file, 0, 0))

        # sound info cloned from the template, then re-pointed
        chunk = bytearray(self.sections[SOUND][tpl.id & 0xFFFFFF])
        struct.pack_into("<I", chunk, 0, wsd_file)
        if volume is not None:
            chunk[8] = volume
        dt, doff = struct.unpack_from("<Hxxi", chunk, 12)
        assert dt == 0x2202
        struct.pack_into("<I", chunk, doff, 0)           # wave sound index 0 in the new FWSD
        self._set_option(chunk, 20, 0, len(self.strings))
        self._set_option(chunk, 20, 31, bpm)               # user param 0: BPM for beat sync
        self.sections[SOUND].append(bytes(chunk))
        self.add_string(label, sound_id)

        # sound set cloned from the template's set: one sound, one file, one wave archive
        self.sections[SOUNDSET].append(self._single_soundset(tpl_set, sound_id, wsd_file, war_id))
        self.add_string("WSDSET_" + label, set_id)
        while len(self.sections[FILE]) < len(self.files):
            self.sections[FILE].append(new_file_info())
        return sound_id

    def _set_option(self, chunk, at, bit, value):
        flags, = struct.unpack_from("<I", chunk, at)
        assert flags & (1 << bit), f"template lacks option bit {bit}"
        slot = bin(flags & ((1 << bit) - 1)).count("1")
        struct.pack_into("<I", chunk, at + 4 + 4 * slot, value)

    def _single_fwsd(self, tpl, war_id):
        """The template's FWSD cut down to the template's own wave sound, pointing at war_id."""
        blob = bytearray(self.bf.file_bytes(tpl.file_id))
        r, b = R(blob), F.blocks(blob)
        i = b[0x6800][0] + 8
        # wave id table: point every entry at our archive, wave 0
        _, toff = r.ref(i)
        tab = i + toff
        for k in range(r.u32(tab)):
            struct.pack_into("<II", blob, tab + 4 + 8 * k, war_id, 0)
        # wave sound data table: keep only the template's entry by moving it to slot 0
        _, doff = r.ref(i + 8)
        dtab = i + doff
        n = r.u32(dtab)
        index = tpl.detail["index"]
        ref = blob[dtab + 4 + 8 * index: dtab + 12 + 8 * index]
        t, off = struct.unpack_from("<Hxxi", ref)
        struct.pack_into("<I", blob, dtab, 1)
        struct.pack_into("<Hxxi", blob, dtab + 4, t, off + 8 * index)
        # its note infos all name wave id table entry 0 or more; all now point at our wave
        return bytes(blob)

    def _single_soundset(self, tpl_set, sound_id, wsd_file, war_id):
        chunk = bytearray(self.sections[SOUNDSET][tpl_set])
        struct.pack_into("<II", chunk, 0, sound_id, sound_id)
        r = R(chunk)
        _, foff = r.ref(8)
        n = r.u32(foff)
        assert n >= 1
        struct.pack_into("<II", chunk, foff, 1, wsd_file)
        dt, doff = r.ref(16)
        if dt == 0x2205 and doff != -1:
            _, woff = r.ref(doff)
            wtab = doff + woff
            if r.u32(wtab) >= 1:
                struct.pack_into("<II", chunk, wtab, 1, war_id)
        self._set_option(chunk, 24, 0, len(self.strings))    # its label is added next
        return bytes(chunk)

    # --- write -------------------------------------------------------------
    def build_strg(self):
        n = len(self.strings)
        tab = bytearray(struct.pack("<I", n))
        blob = bytearray()
        first = 4 + 12 * n
        for s in self.strings:
            b = s.encode("ascii") + b"\0"
            tab += struct.pack("<HHiI", 0x1F01, 0, first + len(blob), len(b))
            blob += b
        strings = bytes(tab) + bytes(blob)
        keys = [(s.encode(), i, self.string_items[i]) for i, s in enumerate(self.strings)
                if i in self.string_items]
        tree = build_tree(keys)
        tab_off = 16
        tree_off = tab_off + align(len(strings), 4)
        body = struct.pack("<HHiHHi", 0x2400, 0, tab_off, 0x2401, 0, tree_off)
        body += strings + b"\0" * (tree_off - tab_off - len(strings)) + tree
        size = align(8 + len(body), 0x20)
        return b"STRG" + struct.pack("<I", size) + body + b"\0" * (size - 8 - len(body))

    def build_info(self):
        body = bytearray(b"\0" * 64)
        for k, entries in enumerate(self.sections):
            start = len(body)
            table = 4 + 8 * len(entries)
            struct.pack_into("<Hxxi", body, 8 * k, SECTION_TYPES[k], start)
            refs, data, at = bytearray(struct.pack("<I", len(entries))), bytearray(), table
            etype = self._entry_type(k)
            for e in entries:
                refs += struct.pack("<Hxxi", etype, at)
                data += e
                at += len(e)
            body += refs + data
        struct.pack_into("<Hxxi", body, 56, 0x220B, len(body))
        body += self.player_info
        size = align(8 + len(body), 0x20)
        self._file_info_offsets = None
        return b"INFO" + struct.pack("<I", size) + bytes(body) + b"\0" * (size - 8 - len(body))

    def _entry_type(self, k):
        r, info = self.bf.r, self.bf.info
        off = info + r.ref(info + 8 * k)[1]
        return r.u16(off + 4)

    def build(self):
        strg = self.build_strg()
        info = bytearray(self.build_info())
        strg_off = 0x40
        info_off = strg_off + len(strg)
        file_off = info_off + len(info)
        body = file_off + 8
        out_body = bytearray()
        locs = []
        for blob in self.files:
            start = align(body + len(out_body), 0x40) - body
            out_body += b"\0" * (start - len(out_body))
            locs.append((start, len(blob)))
            out_body += blob
        # patch every internal file info inside the rebuilt INFO
        r = R(bytes(info))
        ib = 8
        ftab = ib + r.ref(ib + 8 * FILE)[1]
        for i, (_, o) in enumerate(r.ref_table(ftab)):
            lt, loff = r.ref(o)
            if lt == 0x220C:
                struct.pack_into("<iI", info, o + loff + 4, locs[i][0], locs[i][1])
        file_block = b"FILE" + struct.pack("<I", 8 + len(out_body)) + bytes(out_body)
        total = file_off + len(file_block)
        hdr = bytearray(self.bf.data[:0x40])
        struct.pack_into("<I", hdr, 0x0C, total)
        for k, (t, off, size) in enumerate([(0x2000, strg_off, len(strg)), (0x2001, info_off, len(info)),
                                             (0x2002, file_off, len(file_block))]):
            struct.pack_into("<HHiI", hdr, 0x14 + 12 * k, t, 0, off, size)
        return bytes(hdr) + strg + bytes(info) + file_block



def new_file_info():
    """FileInfo of an internal file in no group; build() fills in offset and size.

    Same shape as the game's: Ref(0x220C) to an internal location 4 bytes on,
    {Ref(0x1F00) offset in FILE, u32 size, Ref(0x0100) attached group table}.
    """
    return (struct.pack("<Hxxi", 0x220C, 12) + b"\0" * 4 +
            struct.pack("<HxxiIHxxiI", 0x1F00, 0, 0, 0x0100, 0x14, 0))


# v1.0.0's own item counts. Everything after them was added by add_wave_music.
VANILLA = {SOUND: 3502, SOUNDSET: 120, WAVEARC: 134, FILE: 380}
VANILLA_STRINGS = 3768


def custom_label(name, fast=False):
    """A slot number ("3") becomes CUSTOM_BGM_03, the label the Custom Music sprite plays."""
    label = f"CUSTOM_BGM_{int(name):02d}" if str(name).isdigit() else str(name)
    return label + "_FAST" if fast and not label.endswith("_FAST") else label


def custom_music(bf):
    """[(label, fwav bytes, bpm, volume)] of the music added to `bf`, in order."""
    out = []
    for s in bf.sounds[VANILLA[SOUND]:]:
        if s.kind != "WSD":
            continue
        (war_id, index), = F.fwsd_sound_waves(bf.file_bytes(s.file_id), s.detail["index"])[:1]
        war = bf.file_bytes(bf.wavearcs[war_id & 0xFFFFFF]["file"])
        off, size = F.fwar_entries(war)[index]
        out.append((s.label, war[off:off + size], s.options.get(31, 0), s.volume))
    return out


def set_custom_music(bf, music):
    """Archive bytes with exactly `music` [(label, fwav, bpm, volume)] as the added music."""
    ed = Editor(bf)
    ed.truncate_to_vanilla()
    for label, fwav, bpm, volume in music:
        ed.add_wave_music(label, fwav, bpm, volume)
    return ed.build()


def add_music(bf, label, fwav, bpm=0, volume=None):
    """Archive bytes with `label` added, or replaced if it was added before."""
    music = [m for m in custom_music(bf) if m[0] != label]
    if label in bf.strings and label not in [m[0] for m in custom_music(bf)]:
        raise ValueError(f"{label} is one of the game's own sounds; use inject to replace it")
    return set_custom_music(bf, music + [(label, fwav, bpm, volume)])


def remove_music(bf, label):
    music = custom_music(bf)
    if label not in [m[0] for m in music]:
        raise ValueError(f"{label} is not added music")
    return set_custom_music(bf, [m for m in music if m[0] != label])
