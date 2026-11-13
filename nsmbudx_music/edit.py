"""Rebuild a sound archive with new items.

The writer keeps every existing item byte for byte. INFO is rebuilt section by
section: each section is a reference table followed by its entries, and an entry
together with the data it points to forms one contiguous chunk, so chunks can be
moved as a whole (their internal offsets are relative to themselves). STRG gets
a fresh string table and patricia tree; FILE is laid out as in fwav.rebuild_bfsar.

New music is added the way the game builds its course music: a sequence sound
whose track 0 is a silent beat map (the enemies dance to the sequence variables it
writes) and whose track 1 plays the song through a bank with one wave archive.
The new files, the sound and the bank are listed in GROUP_BOOT with every load
flag: the game finds a file only once a group it has loaded lists it (otherwise
StartSound fails with error 8), and GROUP_BOOT is loaded at boot and never
unloaded.
"""
import struct

from .bfsar import Bfsar, R
from . import formats as F

SECTION_TYPES = [0x2100, 0x2104, 0x2101, 0x2103, 0x2105, 0x2102, 0x2106]
SOUND, SOUNDSET, BANK, WAVEARC, GROUP, PLAYER, FILE = range(7)
ITEM_TYPE = {SOUND: 1, SOUNDSET: 2, BANK: 3, PLAYER: 4, WAVEARC: 5, GROUP: 6}
NO_STRING = 0xFFFFFFFF
HOME_GROUP = "GROUP_BOOT"
# template for new music: a course music sequence and its bank
TEMPLATE = "STRM_BGM_CHIJOU"
LOAD_ALL = 0xFFFFFFFF      # group item load flags: sequence, bank and wave archive
# User param 0 of added music: the BPM in the low 16 bits (the game reads the tempo from
# them, and its beat length from the low 10), song flags for the Custom Music sprite above.
# The game's other readers use bits 0-7 (sound effect handles) and 31 (GamePad speaker).
SONG_YOSHI = 1 << 24       # track 2 holds a Yoshi layer: keep the song while riding Yoshi
SONG_BAHS = 1 << 25        # the bahs were placed by hand
BPM_MASK = 0xFFFF


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
        gid = self._group_file(HOME_GROUP)
        self.files[gid] = build_fgrp(*_strip_group(*parse_fgrp(self.files[gid])))
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
    def add_seq_music(self, label, fwav, bpm, volume=None, offset=0.0, bahs=None, seq=None,
                      yoshi=None, flags=0, template=TEMPLATE):
        """Add `label` as a sequence sound playing `fwav`, like the game's own course music.

        Track 0 of the new sequence is a silent beat map at `bpm` (the enemies' "bah"), track 1
        plays the song through a one-wave bank. `offset` is the first beat in seconds,
        `bahs` the hops [(seconds, kind)] (default: every fourth bar). `seq` reuses a beat
        track built before (from custom_music). `yoshi` is an optional second FWAV of the
        same length: the Yoshi layer, played on track 2, which the game turns on only while
        Mario rides Yoshi (SndBgmMgr::changeBgmTrack). Returns the sound id.
        """
        from .fwav import build_fwar
        bf = self.bf
        tpl = next(s for s in bf.sounds if s.label == template)
        tpl_bank = tpl.detail["banks"][0] & 0xFFFFFF

        sound_id = self.next_id(SOUND)
        bank_id = self.next_id(BANK)
        war_id = self.next_id(WAVEARC)

        info = F.fwav_info(fwav)
        loop = info["loop_start"] if info["loop"] else None
        if seq is None:
            seq, start = build_beat_seq(label, bpm, info["samples"] / info["rate"],
                                        None if loop is None else loop / info["rate"], offset, bahs,
                                        yoshi is not None)
        else:
            start = F.fseq_labels(seq)[label]
        war_file = self.add_file(build_fwar([fwav] if yoshi is None else [fwav, yoshi]))
        bank_file = self.add_file(self._one_wave_bank(bf.banks[tpl_bank]["file"], war_id,
                                                      yoshi is not None))
        seq_file = self.add_file(seq)

        # wave archive info: file id, not loaded individually, no options (as the game's own)
        self.sections[WAVEARC].append(struct.pack("<IBxxxI", war_file, 0, 0))

        # bank info cloned from the template's bank
        chunk = bytearray(self.sections[BANK][tpl_bank])
        struct.pack_into("<I", chunk, 0, bank_file)
        self._set_option(chunk, 12, 0, len(self.strings))
        self.sections[BANK].append(bytes(chunk))
        self.add_string("BANK_" + label, bank_id)

        # sound info cloned from the template, then re-pointed
        chunk = bytearray(self.sections[SOUND][tpl.id & 0xFFFFFF])
        struct.pack_into("<I", chunk, 0, seq_file)
        if volume is not None:
            chunk[8] = volume
        dt, doff = struct.unpack_from("<Hxxi", chunk, 12)
        assert dt == 0x2203
        _, boff = struct.unpack_from("<Hxxi", chunk, doff)
        assert struct.unpack_from("<I", chunk, doff + boff)[0] == 1
        struct.pack_into("<I", chunk, doff + boff + 4, bank_id)
        struct.pack_into("<I", chunk, doff + 8, 0b111 if yoshi is not None else 0b11)
        self._set_option(chunk, doff + 12, 0, start)           # sequence start offset
        self._set_option(chunk, 20, 0, len(self.strings))
        flags |= SONG_YOSHI if yoshi is not None else 0
        flags |= SONG_BAHS if bahs else 0
        self._set_option(chunk, 20, 31, (bpm & BPM_MASK) | (flags & ~BPM_MASK & 0x7FFFFFFF))
        self.sections[SOUND].append(bytes(chunk))
        self.add_string(label, sound_id)

        group_id = next(g["id"] for g in bf.groups if g["label"] == HOME_GROUP)
        while len(self.sections[FILE]) < len(self.files):
            self.sections[FILE].append(new_file_info([group_id]))
        gid = self._group_file(HOME_GROUP)
        items, sets = parse_fgrp(self.files[gid])
        self.files[gid] = build_fgrp(items + [war_file, bank_file, seq_file],
                                     sets + [(sound_id, LOAD_ALL), (bank_id, LOAD_ALL)])
        return sound_id

    def _group_file(self, label):
        return next(g["file"] for g in self.bf.groups if g["label"] == label)

    def _set_option(self, chunk, at, bit, value):
        flags, = struct.unpack_from("<I", chunk, at)
        assert flags & (1 << bit), f"template lacks option bit {bit}"
        slot = bin(flags & ((1 << bit) - 1)).count("1")
        struct.pack_into("<I", chunk, at + 4 + 4 * slot, value)

    def _one_wave_bank(self, file_id, war_id, yoshi=False):
        """The template bank with every wave id table entry pointing at wave 0 of war_id,
        except entry 1 (program 1, the Yoshi layer) at wave 1 when there is one. Programs 0
        and 1 play at full volume."""
        from .banks import Bank
        blob = bytearray(self.bf.file_bytes(file_id))
        r, b = R(blob), F.blocks(blob)
        i = b[0x5800][0] + 8
        _, toff = r.ref(i)
        tab = i + toff
        for k in range(r.u32(tab)):
            struct.pack_into("<II", blob, tab + 4 + 8 * k, war_id, 1 if yoshi and k == 1 else 0)
        bank = Bank(bytes(blob))
        for prg in (0, 1):
            inst = bank.instruments[prg] if prg < len(bank.instruments) else None
            for kr, keys in enumerate(inst["keys"] if inst else ()):
                for vr, reg in enumerate(keys["velocity"]):
                    if "volume" in reg["offsets"]:
                        bank.set_field(prg, kr, vr, "volume", 127)
        return bytes(bank.blob)

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



def new_file_info(groups=()):
    """FileInfo of an internal file; build() fills in offset and size.

    Same shape as the game's: Ref(0x220C) to an internal location 4 bytes on,
    {Ref(0x1F00) offset in FILE, u32 size, Ref(0x0100) table of the groups it is in}.
    """
    return (struct.pack("<Hxxi", 0x220C, 12) + b"\0" * 4 +
            struct.pack("<HxxiIHxxi", 0x1F00, 0, 0, 0x0100, 0x14) +
            struct.pack("<I", len(groups)) + b"".join(struct.pack("<I", g) for g in groups))


# --- FSEQ --------------------------------------------------------------------
TIMEBASE = 96          # ticks per quarter note, as the game's sequences
# The course music's beat track, after STRM_BGM_CHIJOU's. Sequence variable 1 (read by
# SndAudioMgr::getChorusAccentSign) is set to 3 on each bar pair's downbeat and 2 on beats
# 5 and 7. Variable 0 (getBgmAccentSign) is the "bah": the enemies hop when it is set; the
# game's songs set it a few times per loop, 1-4 picking the kind of hop. Here: one 3, the
# overworld's single "bah", on beat 3 of every fourth bar. (tick in four bars, variable, value)
BEAT_PERIOD = 16 * TIMEBASE
BEAT_PATTERN = [(0, 1, 3), (384, 1, 2), (576, 1, 2),
                (768, 1, 3), (1152, 1, 2), (1344, 1, 2), (1344, 0, 3)]


def _varlen(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def _u24(n):
    return n.to_bytes(3, "little")


def build_beat_seq(label, bpm, length, loop=None, offset=0.0, bahs=None, yoshi=False):
    """FSEQ for added music: (file bytes, start offset of `label` in DATA).

    Track 0 is silent and only writes BEAT_PATTERN at `bpm` over the song's `length`
    seconds, from the first beat at `offset` seconds, jumping back to `loop` seconds at
    the end, so it stays on the song's beat. `bahs` [(seconds, kind 1-4)] puts the hops
    (variable 0) at those times in the song instead of BEAT_PATTERN's.
    Track 1 plays program 0 note 60 forever (the song itself, through the bank), and
    the start code opens track 1 and runs track 0, like the game's own course music.
    With `yoshi`, track 2 plays program 1 (the Yoshi layer) the same way.
    """
    if not 1 <= bpm <= 0xFFFF:
        raise ValueError("music needs a tempo (BPM) for the beat track")
    tick = lambda s: round(s * bpm * TIMEBASE / 60)
    end, loop_at, first = max(tick(length), 1), tick(loop or 0), tick(offset)
    sets = {}
    pattern = BEAT_PATTERN if bahs is None else [e for e in BEAT_PATTERN if e[1] != 0]
    for k in range(first, end, BEAT_PERIOD):
        for t, var, val in pattern:
            if t + k < end:
                sets.setdefault(t + k, []).append((var, val))
    for when, kind in bahs or ():
        if not 1 <= kind <= 4:
            raise ValueError(f"bah kind {kind}: use 1-4")
        if tick(when) < end:
            sets.setdefault(tick(when), []).append((0, kind))
    events = sorted(set(sets) | {loop_at, end})

    data = bytearray(b"\xb0\x60\xc7\x00\xe1" + struct.pack("<H", bpm) + b"\xc1\x00")
    loop_off, now = None, 0
    for t in events:
        if t > now:
            data += b"\x80" + _varlen(t - now)
            now = t
        if t == loop_at and loop_off is None:
            loop_off = len(data)
        if t == end:
            break
        for var, val in sets.get(t, []):
            data += b"\xf0\x80" + bytes([var]) + struct.pack("<h", val)
    data += b"\x89" + _u24(loop_off)

    track1 = len(data)
    data += b"\xe1" + struct.pack("<H", bpm) + b"\xb0\x60\x81\x00\x3c\x7f\x00\x89" + _u24(track1)
    track2 = len(data)
    if yoshi:
        data += b"\xe1" + struct.pack("<H", bpm) + b"\xb0\x60\x81\x01\x3c\x7f\x00\x89" + _u24(track2)
    start = len(data)
    data += b"\x88\x01" + _u24(track1)
    if yoshi:
        data += b"\x88\x02" + _u24(track2)
    data += b"\x89" + _u24(0)

    data_block = b"DATA" + struct.pack("<I", align(8 + len(data), 0x20)) + bytes(data)
    data_block += b"\0" * (align(len(data_block), 0x20) - len(data_block))
    name = label.encode("ascii")
    entry = struct.pack("<HxxiI", 0x1F00, start, len(name)) + name + b"\0"
    entry += b"\0" * (align(len(entry), 4) - len(entry))
    labl = struct.pack("<IHxxi", 1, 0x5100, 12) + entry
    labl_block = b"LABL" + struct.pack("<I", align(8 + len(labl), 0x20)) + labl
    labl_block += b"\0" * (align(len(labl_block), 0x20) - len(labl_block))
    labl_off = 0x40 + len(data_block)
    total = labl_off + len(labl_block)
    hdr = struct.pack("<4sHHIIHH", b"FSEQ", 0xFEFF, 0x40, 0x00020000, total, 2, 0)
    hdr += struct.pack("<HHiI", 0x5000, 0, 0x40, len(data_block))
    hdr += struct.pack("<HHiI", 0x5001, 0, labl_off, len(labl_block))
    hdr += b"\0" * (0x40 - len(hdr))
    return hdr + data_block + labl_block, start


# --- FGRP --------------------------------------------------------------------
def parse_fgrp(blob):
    """([file ids], [(item id, load flags)]) of a group with no embedded files.

    INFO (0x7800) lists the files: {u32 file id, Ref(0, -1), u32 -1} each. INFX (0x7802)
    lists the sound sets / banks the group stands for: {u32 item id, u32 load flags}.
    """
    r, b = R(blob), F.blocks(blob)
    info = b[0x7800][0] + 8
    items = []
    for _, o in r.ref_table(info):
        if r.s32(o + 8) != -1:
            raise ValueError("group embeds files; not supported")
        items.append(r.u32(o))
    infx = b[0x7802][0] + 8
    sets = [(r.u32(o), r.u32(o + 4)) for _, o in r.ref_table(infx)]
    return items, sets


def _strip_group(items, sets):
    """Drop what add_seq_music (or the older wave sound adder) put in a group."""
    return ([f for f in items if f < VANILLA[FILE]],
            [(i, f) for i, f in sets
             if not any(i >> 24 == ITEM_TYPE[k] and (i & 0xFFFFFF) >= VANILLA[k]
                        for k in (SOUND, SOUNDSET, BANK))])


def build_fgrp(items, sets):
    """FGRP in the game's layout: INFO padded to 0x20, the empty FILE block up to the next
    0x40, INFX padded to 0x40 in the header but stored only up to its data."""
    def table(ref_type, entries, size):
        body = struct.pack("<I", len(entries))
        first = 4 + 8 * len(entries)
        body += b"".join(struct.pack("<Hxxi", ref_type, first + size * k) for k in range(len(entries)))
        return body + b"".join(entries)

    info = table(0x7900, [struct.pack("<IHxxiI", f, 0, -1, 0xFFFFFFFF) for f in items], 16)
    infx = table(0x7901, [struct.pack("<II", i, f) for i, f in sets], 8)
    info_block = b"INFO" + struct.pack("<I", align(8 + len(info), 0x20)) + info
    info_block += b"\0" * (align(len(info_block), 0x20) - len(info_block))
    info_off = 0x40
    file_off = info_off + len(info_block)
    infx_off = align(file_off + 8, 0x40)
    file_block = b"FILE" + struct.pack("<I", infx_off - file_off) + b"\0" * (infx_off - file_off - 8)
    infx_size = align(8 + len(infx), 0x40)
    infx_block = b"INFX" + struct.pack("<I", infx_size) + infx
    hdr = struct.pack("<4sHHIIHH", b"FGRP", 0xFEFF, 0x40, 0x00010000, infx_off + infx_size, 3, 0)
    hdr += struct.pack("<HHiI", 0x7800, 0, info_off, len(info_block))
    hdr += struct.pack("<HHiI", 0x7801, 0, file_off, len(file_block))
    hdr += struct.pack("<HHiI", 0x7802, 0, infx_off, infx_size)
    hdr += b"\0" * (0x40 - len(hdr))
    return hdr + info_block + file_block + infx_block


# v1.0.0's own item counts. Everything after them was added by this module.
VANILLA = {SOUND: 3502, SOUNDSET: 120, BANK: 87, WAVEARC: 134, FILE: 380}
VANILLA_STRINGS = 3768


def custom_label(name, fast=False):
    """A slot number ("3") becomes CUSTOM_BGM_03, the label the Custom Music sprite plays."""
    label = f"CUSTOM_BGM_{int(name):02d}" if str(name).isdigit() else str(name)
    return label + "_FAST" if fast and not label.endswith("_FAST") else label


def beat_offset(seq, bpm):
    """Seconds to the first beat of a beat track built by build_beat_seq."""
    data = F.fseq_data(seq)
    if data[9] != 0x80:                    # first event right after the 9-byte track header
        return 0.0
    ticks, p = 0, 10
    while True:
        ticks = ticks << 7 | data[p] & 0x7F
        if not data[p] & 0x80:
            return ticks * 60 / (bpm * TIMEBASE)
        p += 1


def parse_bahs(text):
    """Bah times from text: one per line, `seconds [kind]`, or an Audacity label export
    (`start<TAB>end<TAB>label`, the label being the kind). Kind defaults to 3."""
    out = []
    for line in text.splitlines():
        parts = line.replace(",", " ").split()
        if not parts or parts[0].startswith("#"):
            continue
        kind = next((int(p) for p in parts[1:] if p.isdigit()), 3)
        out.append((float(parts[0]), kind))
    return out


def custom_music(bf):
    """[(label, fwav bytes, bpm, volume, first beat seconds, beat track FSEQ or None,
    Yoshi layer fwav bytes or None)] of the music added to `bf`."""
    out = []
    for s in bf.sounds[VANILLA[SOUND]:]:
        if s.kind == "SEQ":
            bank = bf.banks[s.detail["banks"][0] & 0xFFFFFF]
            war_id, index = F.fbnk_waves(bf.file_bytes(bank["file"]))[0]
            seq = bf.file_bytes(s.file_id)
            offset = beat_offset(seq, s.options.get(31, 120) & BPM_MASK)
        elif s.kind == "WSD":      # added by older versions
            war_id, index = F.fwsd_sound_waves(bf.file_bytes(s.file_id), s.detail["index"])[0]
            offset, seq = 0.0, None
        else:
            continue
        war = bf.file_bytes(bf.wavearcs[war_id & 0xFFFFFF]["file"])
        entries = F.fwar_entries(war)
        off, size = entries[index]
        yoshi = None
        if s.kind == "SEQ" and len(entries) > 1:
            yo, ys = entries[1]
            yoshi = war[yo:yo + ys]
        param = s.options.get(31, 0)
        out.append((s.label, war[off:off + size], (param & BPM_MASK) or 120, s.volume, offset, seq,
                    yoshi, param & ~BPM_MASK & 0x7FFFFFFF))
    return out


def set_custom_music(bf, music):
    """Archive bytes with exactly `music` (as custom_music gives it) as the added music."""
    ed = Editor(bf)
    ed.truncate_to_vanilla()
    for label, fwav, bpm, volume, offset, seq, yoshi, flags in music:
        ed.add_seq_music(label, fwav, bpm, volume, offset, seq=seq, yoshi=yoshi, flags=flags)
    return ed.build()


def add_music(bf, label, fwav, bpm, volume=None, offset=0.0, bahs=None, yoshi=None):
    """Archive bytes with `label` added, or replaced if it was added before."""
    music = [m for m in custom_music(bf) if m[0] != label]
    if label in bf.strings and label not in [m[0] for m in custom_music(bf)]:
        raise ValueError(f"{label} is one of the game's own sounds; use inject to replace it")
    info = F.fwav_info(fwav)
    if yoshi is not None:
        yi = F.fwav_info(yoshi)
        if (yi["samples"], yi["rate"], yi["loop"], yi["loop_start"]) != \
                (info["samples"], info["rate"], info["loop"], info["loop_start"]):
            raise ValueError("the Yoshi layer must have the song's length, rate and loop")
    seq, _ = build_beat_seq(label, bpm, info["samples"] / info["rate"],
                            info["loop_start"] / info["rate"] if info["loop"] else None, offset, bahs,
                            yoshi is not None)
    flags = SONG_BAHS if bahs else 0
    return set_custom_music(bf, music + [(label, fwav, bpm, volume, offset, seq, yoshi, flags)])


def set_custom_layer(bf, label, layer, fwav):
    """Archive bytes with one layer of added music replaced: "song" (track 1) or "yoshi"
    (track 2; None removes it). The beat track keeps its bahs, accents and loop."""
    from . import beat
    music = custom_music(bf)
    i = next((k for k, m in enumerate(music) if m[0] == label), None)
    if i is None:
        raise ValueError(f"{label} is not added music")
    _, song, bpm, volume, offset, _, yoshi, flags = music[i]
    sound = next(s for s in bf.sounds if s.label == label)
    old = beat.read_beat(bf, sound)
    if layer == "song":
        if fwav is None:
            raise ValueError("a song needs its song layer; remove the song instead")
        song = fwav
    elif layer == "yoshi":
        yoshi = fwav
    else:
        raise ValueError(f"no layer {layer}")
    info = F.fwav_info(song)
    if yoshi is not None:
        yi = F.fwav_info(yoshi)
        if (yi["samples"], yi["rate"]) != (info["samples"], info["rate"]):
            raise ValueError("the Yoshi layer must have the song's length and sample rate")
    length = info["samples"] / info["rate"]
    loop = info["loop_start"] / info["rate"] if info["loop"] else None
    seq, start = build_beat_seq(label, bpm, length, loop, offset, None, yoshi is not None)
    tick = lambda sec: round(sec * bpm * TIMEBASE / 60)
    end = max(tick(length), 1)
    seq = beat.write_beat(seq, start, bpm, TIMEBASE, [e for e in old["events"] if e[0] < end],
                          None if loop is None else tick(loop), end)
    music[i] = (label, song, bpm, volume, offset, seq, yoshi, flags)
    return set_custom_music(bf, music)


def remove_music(bf, label):
    music = custom_music(bf)
    if label not in [m[0] for m in music]:
        raise ValueError(f"{label} is not added music")
    return set_custom_music(bf, [m for m in music if m[0] != label])
