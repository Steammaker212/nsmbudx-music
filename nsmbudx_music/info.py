"""Every field of every INFO item, as a list of editable properties.

Works on edit.Editor's chunks: each item is a self-contained byte chunk (offsets are
relative to the chunk), so a field is (offset in chunk, type) and editing it is a
pack_into. Fields stored as option values exist only when the item sets that option
bit; those an item leaves out show the game's default and cannot be edited here.
"""
import struct

from . import edit as E

PAN_MODES = ["Dual", "Balance"]
PAN_CURVES = ["Sqrt", "Sqrt 0dB", "Sqrt 0dB clamp", "SinCos", "SinCos 0dB", "SinCos 0dB clamp",
              "Linear", "Linear 0dB", "Linear 0dB clamp"]
DECAY_CURVES = {1: "Logarithmic", 2: "Linear"}
DETAIL_KINDS = {0x2201: "Stream", 0x2202: "Wave sound", 0x2203: "Sequence"}
SECTION_NAMES = ["sound", "soundset", "bank", "wavearc", "group", "player", "file"]
SECTION_INDEX = {n: i for i, n in enumerate(SECTION_NAMES)}


def _slot(chunk, at, bit):
    flags, = struct.unpack_from("<I", chunk, at)
    if not flags & (1 << bit):
        return None
    return at + 4 + 4 * bin(flags & ((1 << bit) - 1)).count("1")


def _ref(chunk, at):
    t, off = struct.unpack_from("<Hxxi", chunk, at)
    return t, off


class Field:
    def __init__(self, key, label, kind, at, value=None, readonly=False, group=None, **extra):
        self.key, self.label, self.kind, self.at = key, label, kind, at
        self.value, self.readonly, self.group, self.extra = value, readonly or at is None, group, extra

    def json(self):
        d = {"key": self.key, "label": self.label, "kind": self.kind, "value": self.value,
             "readonly": self.readonly}
        if self.group:
            d["group"] = self.group
        d.update(self.extra)
        return d


def _read(chunk, kind, at):
    if at is None:
        return None
    fmt = {"u8": "<B", "s8": "<b", "u16": "<H", "u32": "<I", "f32": "<f", "bool": "<B",
           "enum": "<B", "id": "<I", "hex": "<I"}[kind]
    return struct.unpack_from(fmt, chunk, at)[0]


def _f(chunk, key, label, kind, at, default=None, **kw):
    v = _read(chunk, kind, at)
    if kind == "bool" and v is not None:
        v = bool(v)
    return Field(key, label, kind, at, default if v is None else v, **kw)


def _user_params(chunk, at, out):
    for k, bit in enumerate((31, 30, 29, 28)):
        s = _slot(chunk, at, bit)
        out.append(_f(chunk, f"user{k}", f"User param {k}" + (" (BPM in NSMBUDX music)" if k == 0 else ""),
                      "u32", s, 0xFFFFFFFF, group="User params"))


def sound_fields(chunk, bf):
    f = [
        _f(chunk, "file_id", "File", "id", 0, group="Sound", ref="file"),
        _f(chunk, "player_id", "Player", "id", 4, group="Sound", ref="player"),
        _f(chunk, "volume", "Volume", "u8", 8, group="Sound", min=0, max=255),
        _f(chunk, "remote_filter", "Remote filter", "u8", 9, group="Sound", min=0, max=127),
    ]
    opt = 0x14
    pan = _slot(chunk, opt, 1)
    f.append(_f(chunk, "pan_mode", "Pan mode", "enum", pan, 0, group="Sound", options=PAN_MODES))
    f.append(_f(chunk, "pan_curve", "Pan curve", "enum", None if pan is None else pan + 1, 0,
                group="Sound", options=PAN_CURVES))
    pp = _slot(chunk, opt, 2)
    f.append(_f(chunk, "player_priority", "Player priority", "u8", pp, 64, group="Sound", min=0, max=127))
    f.append(_f(chunk, "actor_player", "Actor player id", "u8", None if pp is None else pp + 1, 0,
                group="Sound", min=0, max=3))
    _user_params(chunk, opt, f)
    o3 = _slot(chunk, opt, 8)
    if o3 is not None:
        base = struct.unpack_from("<I", chunk, o3)[0]
        flags = struct.unpack_from("<I", chunk, base)[0]
        for bit, name in enumerate(("volume", "priority", "pan", "span", "filter")):
            f.append(Field(f"3d_{name}", f"3D {name}", "bool", None, bool(flags & (1 << bit)),
                           group="3D", readonly=True))
        f.append(_f(chunk, "3d_decay_ratio", "3D decay ratio", "f32", base + 4, group="3D"))
        f.append(_f(chunk, "3d_decay_curve", "3D decay curve", "u8", base + 8, group="3D", min=1, max=2))
        f.append(_f(chunk, "3d_doppler", "3D doppler factor", "u8", base + 9, group="3D", min=0, max=255))
    dt, doff = _ref(chunk, 12)
    f.append(Field("kind", "Kind", "text", None, DETAIL_KINDS.get(dt, hex(dt)), group="Detail"))
    if dt == 0x2203:
        _, boff = _ref(chunk, doff)
        n = struct.unpack_from("<I", chunk, doff + boff)[0]
        for k in range(n):
            f.append(_f(chunk, f"bank{k}", f"Bank {k}", "id", doff + boff + 4 + 4 * k, group="Detail", ref="bank"))
        f.append(_f(chunk, "track_flags", "Track flags", "hex", doff + 8, group="Detail",
                    help="bit n = track n is opened"))
        so = _slot(chunk, doff + 12, 0)
        f.append(_f(chunk, "start_offset", "Start offset", "u32", so, 0, group="Detail",
                    help="where the sound starts in the sequence (a label)"))
        pr = _slot(chunk, doff + 12, 1)
        f.append(_f(chunk, "channel_priority", "Channel priority", "u8", pr, 64, group="Detail", min=0, max=127))
        f.append(_f(chunk, "release_priority_fix", "Fix priority at release", "bool",
                    None if pr is None else pr + 1, False, group="Detail"))
    elif dt == 0x2202:
        f.append(_f(chunk, "wsd_index", "Wave sound index", "u32", doff, group="Detail"))
        f.append(_f(chunk, "wsd_tracks", "Allocated tracks", "u32", doff + 4, group="Detail"))
        pr = _slot(chunk, doff + 8, 0)
        f.append(_f(chunk, "channel_priority", "Channel priority", "u8", pr, 64, group="Detail", min=0, max=127))
        f.append(_f(chunk, "release_priority_fix", "Fix priority at release", "bool",
                    None if pr is None else pr + 1, False, group="Detail"))
    return f


def soundset_fields(chunk, bf):
    f = [_f(chunk, "start", "First sound", "id", 0, readonly=True, ref="sound"),
         _f(chunk, "end", "Last sound", "id", 4, readonly=True, ref="sound")]
    _, foff = _ref(chunk, 8)
    n = struct.unpack_from("<I", chunk, foff)[0]
    for k in range(n):
        f.append(_f(chunk, f"file{k}", f"File {k}", "id", foff + 4 + 4 * k, readonly=True, ref="file"))
    dt, doff = _ref(chunk, 16)
    if dt == 0x2205 and doff not in (-1, 0):
        _, woff = _ref(chunk, doff)
        wn = struct.unpack_from("<I", chunk, doff + woff)[0]
        for k in range(wn):
            f.append(_f(chunk, f"warc{k}", f"Wave archive {k}", "id", doff + woff + 4 + 4 * k,
                        readonly=True, ref="wavearc"))
    return f


def bank_fields(chunk, bf):
    f = [_f(chunk, "file_id", "File", "id", 0, ref="file")]
    _, woff = _ref(chunk, 4)          # relative to the item, like every item Ref
    n = struct.unpack_from("<I", chunk, woff)[0]
    for k in range(n):
        f.append(_f(chunk, f"warc{k}", f"Wave archive {k}", "id", woff + 4 + 4 * k, ref="wavearc"))
    return f


def wavearc_fields(chunk, bf):
    return [_f(chunk, "file_id", "File", "id", 0, ref="file"),
            _f(chunk, "individual", "Load individually", "bool", 4),
            _f(chunk, "wave_count", "Wave count", "u32", _slot(chunk, 8, 1), readonly=True)]


def group_fields(chunk, bf):
    return [_f(chunk, "file_id", "File", "id", 0, ref="file")]


def player_fields(chunk, bf):
    return [_f(chunk, "sound_max", "Playable sound max", "u32", 0, min=1, max=255),
            _f(chunk, "heap_size", "Player heap size", "u32", _slot(chunk, 4, 1), 0)]


def file_fields(chunk, bf):
    t, off = _ref(chunk, 0)
    f = [Field("location", "Location", "text", None, {0x220C: "Inside the archive",
                                                         0x220D: "External file"}.get(t, hex(t)))]
    if t == 0x220C:
        loc = off
        f.append(_f(chunk, "size", "Size (bytes)", "u32", loc + 8, readonly=True))
        _, goff = _ref(chunk, loc + 12)       # relative to the location struct
        if goff != -1:
            n = struct.unpack_from("<I", chunk, loc + goff)[0]
            for k in range(n):
                f.append(_f(chunk, f"group{k}", f"Group {k}", "id", loc + goff + 4 + 4 * k,
                            readonly=True, ref="group"))
    elif t == 0x220D:
        end = chunk.index(b"\0", off)
        f.append(Field("path", "Path", "text", None, chunk[off:end].decode("ascii", "replace")))
    return f


PLAYER_INFO = [("sequence_sound_max", "Sequence sound max", 0), ("sequence_track_max", "Sequence track max", 2),
               ("stream_sound_max", "Stream sound max", 4), ("stream_track_max", "Stream track max", 6),
               ("stream_channel_max", "Stream channel max", 8), ("wave_sound_max", "Wave sound max", 10),
               ("wave_track_max", "Wave track max", 12)]

READERS = [sound_fields, soundset_fields, bank_fields, wavearc_fields, group_fields, player_fields, file_fields]


def fields(ed, section, index):
    """[Field] of an item; section is an edit section number or name."""
    if section == "archive":
        f = [Field(k, label, "u16", at, struct.unpack_from("<H", ed.player_info, at)[0], min=0, max=999)
             for k, label, at in PLAYER_INFO]
        f.append(Field("stream_buffer_times", "Stream buffer times", "u8", 14, ed.player_info[14], min=1, max=8))
        return f
    k = SECTION_INDEX.get(section, section)
    return READERS[k](ed.sections[k][index], ed.bf)


def set_field(ed, section, index, key, value):
    """Change one field in place. Returns the new value as stored."""
    fl = {f.key: f for f in fields(ed, section, index)}
    if key not in fl:
        raise KeyError(f"no field {key}")
    f = fl[key]
    if f.readonly:
        raise ValueError(f"{f.label} cannot be edited" + (" (this item does not store it)" if f.at is None else ""))
    if section == "archive":
        chunk = bytearray(ed.player_info)
    else:
        k = SECTION_INDEX.get(section, section)
        chunk = bytearray(ed.sections[k][index])
    kind = f.kind
    if kind == "f32":
        v = float(value)
        struct.pack_into("<f", chunk, f.at, v)
    else:
        if isinstance(value, str):
            value = value.strip()
            if kind == "enum" and not value.lstrip("-").isdigit():
                value = f.extra["options"].index(value)
            else:
                value = int(value, 0)
        v = int(value)
        lo, hi = f.extra.get("min"), f.extra.get("max")
        if kind == "bool":
            v = 1 if v else 0
        if lo is not None and v < lo or hi is not None and v > hi:
            raise ValueError(f"{f.label} must be {lo}-{hi}")
        fmt = {"u8": "<B", "s8": "<b", "u16": "<H", "u32": "<I", "bool": "<B", "enum": "<B",
               "id": "<I", "hex": "<I"}[kind]
        struct.pack_into(fmt, chunk, f.at, v & {"<B": 0xFF, "<b": 0xFF, "<H": 0xFFFF}.get(fmt, 0xFFFFFFFF)
                         if fmt != "<b" else v)
    if section == "archive":
        ed.player_info = bytes(chunk)
    else:
        ed.sections[k][index] = bytes(chunk)
    return v
