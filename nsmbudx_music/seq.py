"""nn::atk sequence (FSEQ) bytecode: decode, disassemble to text, assemble from text.

Byte layout (Switch, little-endian), per command:

    [a2 if] [a3 time s16 | a4 time random | a5 time variable] [a0 random | a1 variable] op args

* notes 0x00-0x7f: velocity u8, length (MIDI-style variable length)
* 0x80 wait, 0x81 prg: variable length; 0x88 opentrack u8 + u24; 0x89 jump / 0x8a call u24
* 0xb0-0xdf: one u8 (transpose and pitch_bend are signed); 0xe0-0xef: one s16
* 0xf0 extended: 0x80-0x9f var u8 + s16, 0xa0-0xbf u8, 0xe0-0xef u16
* 0xfe alloctrack u16; 0xfb-0xff otherwise no arguments

An `a0` prefix replaces the main argument by s16 min, s16 max (a random value); `a1`
replaces it by a variable number (u8). The time prefixes add a trailing argument of
that kind.

Text form (one command per line, `;` comments):

    Name:                   exported label (kept in the LABL block)
    .L09BA:                 local label (jump/call/opentrack targets)
        timebase 96
        cn4 127, 36         note: key, velocity, length
        volume_v 3          main argument read from variable 3
        wait_r 48, 96       main argument random 48..96
        pan_t 64, 12        trailing time argument (_t s16, _tr min max, _tv var)
        if jump .L0002      conditional (runs when the last cmp_* was true)
        .byte 0x00, 0x01    bytes no code path reaches
"""
import re
import struct

from . import formats as F

KEYS = [n + o for o in ("m1", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9")
        for n in ("cn", "cs", "dn", "ds", "en", "fn", "fs", "gn", "gs", "an", "as", "bn")][:128]
KEY_INDEX = {k: i for i, k in enumerate(KEYS)}

U8_NAMES = {
    0xb0: "timebase", 0xb1: "env_hold", 0xb2: "monophonic", 0xb3: "velocity_range",
    0xb4: "biquad_type", 0xb5: "biquad_value", 0xb6: "bank_select", 0xbd: "mod_phase",
    0xbe: "mod_curve", 0xbf: "front_bypass", 0xc0: "pan", 0xc1: "volume", 0xc2: "main_volume",
    0xc3: "transpose", 0xc4: "pitch_bend", 0xc5: "bend_range", 0xc6: "prio", 0xc7: "notewait",
    0xc8: "tie", 0xc9: "porta", 0xca: "mod_depth", 0xcb: "mod_speed", 0xcc: "mod_type",
    0xcd: "mod_range", 0xce: "porta_sw", 0xcf: "porta_time", 0xd0: "attack", 0xd1: "decay",
    0xd2: "sustain", 0xd3: "release", 0xd4: "loop_start", 0xd5: "volume2", 0xd6: "printvar",
    0xd7: "span", 0xd8: "lpf_cutoff", 0xd9: "fxsend_a", 0xda: "fxsend_b", 0xdb: "mainsend",
    0xdc: "init_pan", 0xdd: "mute", 0xde: "fxsend_c", 0xdf: "damper",
}
SIGNED_U8 = {0xc3, 0xc4}
S16_NAMES = {0xe0: "mod_delay", 0xe1: "tempo", 0xe3: "sweep_pitch", 0xe4: "mod_period"}
PLAIN_NAMES = {0xfb: "env_reset", 0xfc: "loop_end", 0xfd: "ret", 0xff: "fin"}
EX_NAMES = {
    0x80: "setvar", 0x81: "addvar", 0x82: "subvar", 0x83: "mulvar", 0x84: "divvar",
    0x85: "shiftvar", 0x86: "randvar", 0x87: "andvar", 0x88: "orvar", 0x89: "xorvar",
    0x8a: "notvar", 0x8b: "modvar", 0x90: "cmp_eq", 0x91: "cmp_ge", 0x92: "cmp_gt",
    0x93: "cmp_le", 0x94: "cmp_lt", 0x95: "cmp_ne", 0xe0: "userproc",
}
for _n, _base in ((2, 0xa0), (3, 0xa6), (4, 0xac)):
    for _k, _w in enumerate(("curve", "phase", "depth", "speed", "type", "range")):
        EX_NAMES[_base + _k] = f"mod{_n}_{_w}"
for _n, _base in ((2, 0xe1), (3, 0xe3), (4, 0xe5)):
    EX_NAMES[_base], EX_NAMES[_base + 1] = f"mod{_n}_delay", f"mod{_n}_period"

ARG_SUFFIX = {0xa0: "_r", 0xa1: "_v"}
TIME_SUFFIX = {0xa3: "_t", 0xa4: "_tr", 0xa5: "_tv"}


class Command:
    """One decoded command. `args` are ints; `target` is a DATA offset for jumps."""
    __slots__ = ("offset", "size", "cond", "argtype", "timetype", "op", "ex", "name",
                 "args", "time", "target", "track")

    def __init__(self, offset):
        self.offset, self.cond, self.argtype, self.timetype = offset, False, None, None
        self.op, self.ex, self.args, self.time, self.target, self.track = 0, None, [], [], None, None

    @property
    def flows_on(self):
        """Whether the next command runs after this one."""
        if self.cond:
            return True
        return not (self.op == 0x89 or (self.ex is None and self.op in (0xfd, 0xff)))

    def text(self, label_of=None):
        name = self.name
        if self.argtype is not None:
            name += ARG_SUFFIX[self.argtype]
        if self.timetype is not None:
            name += TIME_SUFFIX[self.timetype]
        parts = [str(a) for a in self.args]
        if self.target is not None:
            t = label_of(self.target) if label_of else f"0x{self.target:x}"
            parts = parts[:-1] + [t] if self.op == 0x88 else [t]
        parts += [str(a) for a in self.time]
        s = name + (" " + ", ".join(parts) if parts else "")
        return ("if " if self.cond else "") + s


def _varlen(data, p):
    v = 0
    while True:
        b = data[p]
        p += 1
        v = (v << 7) | (b & 0x7f)
        if not b & 0x80:
            return v, p


def _arg(data, p, kind):
    """Read one argument of `kind` ('u8', 's8', 's16', 'u16', 'vlq', 0xa0 random, 0xa1 var)."""
    if kind == 0xa0:
        lo, hi = struct.unpack_from("<hh", data, p)
        return [lo, hi], p + 4
    if kind == 0xa1:
        return [data[p]], p + 1
    if kind == "u8":
        return [data[p]], p + 1
    if kind == "s8":
        return [struct.unpack_from("<b", data, p)[0]], p + 1
    if kind == "s16":
        return [struct.unpack_from("<h", data, p)[0]], p + 2
    if kind == "u16":
        return [struct.unpack_from("<H", data, p)[0]], p + 2
    v, p = _varlen(data, p)
    return [v], p


def decode(data, p):
    """The command at DATA offset p."""
    c = Command(p)
    op = data[p]
    p += 1
    if op == 0xa2:
        c.cond = True
        op, p = data[p], p + 1
    if op in (0xa3, 0xa4, 0xa5):
        c.timetype = op
        op, p = data[p], p + 1
    if op in (0xa0, 0xa1):
        c.argtype = op
        op, p = data[p], p + 1
    c.op = op
    main = lambda default: c.argtype if c.argtype is not None else default
    if op < 0x80:
        c.name = KEYS[op]
        c.args = [data[p]]
        p += 1
        a, p = _arg(data, p, main("vlq"))
        c.args += a
    elif op in (0x80, 0x81):
        c.name = "wait" if op == 0x80 else "prg"
        c.args, p = _arg(data, p, main("vlq"))
    elif op == 0x88:
        c.name = "opentrack"
        c.track = data[p]
        c.target = data[p + 1] | data[p + 2] << 8 | data[p + 3] << 16
        c.args = [c.track, c.target]
        p += 4
    elif op in (0x89, 0x8a):
        c.name = "jump" if op == 0x89 else "call"
        c.target = data[p] | data[p + 1] << 8 | data[p + 2] << 16
        c.args = [c.target]
        p += 3
    elif 0xb0 <= op <= 0xdf:
        c.name = U8_NAMES.get(op, f"cmd_{op:02x}")
        c.args, p = _arg(data, p, main("s8" if op in SIGNED_U8 else "u8"))
    elif 0xe0 <= op <= 0xef:
        c.name = S16_NAMES.get(op, f"cmd_{op:02x}")
        c.args, p = _arg(data, p, main("s16"))
    elif op == 0xf0:
        ex = data[p]
        p += 1
        c.ex = ex
        c.name = EX_NAMES.get(ex, f"ex_{ex:02x}")
        hi = ex & 0xf0
        if hi in (0xa0, 0xb0):
            c.args, p = [data[p]], p + 1
        elif hi == 0xe0:
            c.args, p = _arg(data, p, main("u16"))
        elif hi in (0x80, 0x90):
            c.args = [data[p]]
            a, p = _arg(data, p + 1, main("s16"))
            c.args += a
    elif op == 0xfe:
        c.name = "alloctrack"
        c.args, p = _arg(data, p, "u16")
    else:
        c.name = PLAIN_NAMES.get(op, f"cmd_{op:02x}")
    if c.timetype is not None:
        c.time, p = _arg(data, p, {0xa3: "s16", 0xa4: 0xa0, 0xa5: 0xa1}[c.timetype])
    c.size = p - c.offset
    return c


def walk(data, starts):
    """{offset: Command} for all code reachable from `starts`."""
    seen, todo = {}, list(starts)
    while todo:
        p = todo.pop()
        while 0 <= p < len(data) and p not in seen:
            try:
                c = decode(data, p)
            except (IndexError, struct.error):
                break
            if c.offset + c.size > len(data):
                break
            seen[p] = c
            if c.target is not None:
                todo.append(c.target)
            if not c.flows_on:
                break
            p += c.size
    return seen


class Listing:
    """A disassembled DATA block: text, plus offset <-> line maps."""

    def __init__(self, data, labels):
        self.data, self.labels = data, dict(labels)
        self.code = walk(data, self.labels.values())
        names = {}
        for name, off in sorted(self.labels.items(), key=lambda x: x[1]):
            names.setdefault(off, []).append(name)
        local = {c.target for c in self.code.values() if c.target is not None} - set(names)
        for off in local:
            names[off] = [f".L{off:04X}"]
        self.names = names
        lines, self.line_of, self.offset_of = [], {}, {}
        p, end = 0, len(data)
        # drop the block's zero padding after the last reachable command
        last = max((c.offset + c.size for c in self.code.values()), default=0)
        while end > last and data[end - 1] == 0:
            end -= 1
        self.padding = len(data) - end
        while p < end:
            for n in names.get(p, ()):
                lines.append(f"{n}:")
            if p in self.code:
                c = self.code[p]
                self.line_of[p] = len(lines)
                self.offset_of[len(lines)] = p
                lines.append("    " + c.text(self.label_of))
                p += c.size
            else:
                q = p + 1
                while q < end and q not in self.code and q not in names:
                    q += 1
                chunk = data[p:q]
                for k in range(0, len(chunk), 16):
                    lines.append("    .byte " + ", ".join(f"0x{b:02x}" for b in chunk[k:k + 16]))
                p = q
        self.lines = lines

    def label_of(self, off):
        return self.names[off][0] if off in self.names else f"0x{off:x}"

    @property
    def text(self):
        return "\n".join(self.lines) + "\n"


def disassemble(blob):
    """Listing of an FSEQ file."""
    return Listing(F.fseq_data(blob), F.fseq_labels(blob))


# --- assembler ---------------------------------------------------------------
NAME_OP = {}
for _op, _n in list(U8_NAMES.items()) + list(S16_NAMES.items()) + list(PLAIN_NAMES.items()):
    NAME_OP[_n] = ("op", _op)
for _ex, _n in EX_NAMES.items():
    NAME_OP[_n] = ("ex", _ex)
NAME_OP.update({"wait": ("op", 0x80), "prg": ("op", 0x81), "opentrack": ("op", 0x88),
                "jump": ("op", 0x89), "call": ("op", 0x8a), "alloctrack": ("op", 0xfe)})


class AsmError(ValueError):
    def __init__(self, line, msg):
        super().__init__(f"line {line}: {msg}")
        self.line = line


def _vlq(n):
    if n < 0:
        raise ValueError("negative length")
    out = [n & 0x7f]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7f))
        n >>= 7
    return bytes(reversed(out))


def _int(s):
    return int(s, 0)


def _put(kind, vals):
    if kind == 0xa0:
        return struct.pack("<hh", *vals)
    if kind == 0xa1 or kind == "u8":
        return bytes([vals[0] & 0xff])
    if kind == "s8":
        return struct.pack("<b", vals[0])
    if kind == "s16":
        return struct.pack("<h", vals[0])
    if kind == "u16":
        return struct.pack("<H", vals[0] & 0xffff)
    return _vlq(vals[0])


def _count(kind):
    return 2 if kind == 0xa0 else 1


_SUFFIX = re.compile(r"^(?P<base>.+?)(?P<arg>_r|_v)?(?P<time>_t|_tr|_tv)?$")


def _parse_name(word):
    if word in NAME_OP or word in KEY_INDEX:
        return word, None, None
    m = _SUFFIX.match(word)
    base = m.group("base")
    if base not in NAME_OP and base not in KEY_INDEX and not re.match(r"^(cmd|ex)_[0-9a-f]{2}$", base):
        return word, None, None
    argtype = {"_r": 0xa0, "_v": 0xa1}.get(m.group("arg"))
    timetype = {"_t": 0xa3, "_tr": 0xa4, "_tv": 0xa5}.get(m.group("time"))
    return base, argtype, timetype


def assemble(text):
    """(DATA bytes, {exported label: offset}) from listing text."""
    items = []          # (line no, kind, payload)
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        if line.endswith(":") and " " not in line:
            items.append((no, "label", line[:-1]))
            continue
        items.append((no, "cmd", line))

    def encode(no, line, labels):
        cond = False
        if line.startswith("if "):
            cond, line = True, line[3:].strip()
        word, _, rest = line.partition(" ")
        args = [a.strip() for a in rest.split(",")] if rest.strip() else []
        if word == ".byte":
            return bytes(_int(a) & 0xff for a in args)
        base, argtype, timetype = _parse_name(word)
        out = bytearray()
        if cond:
            out.append(0xa2)
        if timetype:
            out.append(timetype)
        if argtype:
            out.append(argtype)
        tkind = {0xa3: "s16", 0xa4: 0xa0, 0xa5: 0xa1}.get(timetype)
        tail = b""
        if tkind is not None:
            n = _count(tkind)
            if len(args) < n:
                raise AsmError(no, "missing time argument")
            tail = _put(tkind, [_int(a) for a in args[-n:]])
            args = args[:-n]

        def target(s):
            if s in labels:
                return labels[s]
            try:
                return _int(s)
            except ValueError:
                raise AsmError(no, f"unknown label {s}")

        def main(default, vals):
            kind = argtype if argtype is not None else default
            if len(vals) != _count(kind):
                raise AsmError(no, f"{word}: wrong number of arguments")
            return _put(kind, [_int(v) for v in vals])

        try:
            if base in KEY_INDEX:
                out.append(KEY_INDEX[base])
                out.append(_int(args[0]) & 0xff)
                out += main("vlq", args[1:])
            elif base.startswith("cmd_") or base.startswith("ex_"):
                op = int(base[-2:], 16)
                if base.startswith("ex_"):
                    out += bytes([0xf0, op])
                    hi = op & 0xf0
                    if hi in (0xa0, 0xb0):
                        out.append(_int(args[0]) & 0xff)
                    elif hi == 0xe0:
                        out += main("u16", args)
                    elif hi in (0x80, 0x90):
                        out.append(_int(args[0]) & 0xff)
                        out += main("s16", args[1:])
                else:
                    out.append(op)
                    if 0xb0 <= op <= 0xdf:
                        out += main("u8", args)
                    elif 0xe0 <= op <= 0xef:
                        out += main("s16", args)
            else:
                kind, op = NAME_OP[base]
                if kind == "ex":
                    out += bytes([0xf0, op])
                    hi = op & 0xf0
                    if hi in (0xa0, 0xb0):
                        out.append(_int(args[0]) & 0xff)
                    elif hi == 0xe0:
                        out += main("u16", args)
                    else:
                        out.append(_int(args[0]) & 0xff)
                        out += main("s16", args[1:])
                elif op in (0x80, 0x81):
                    out.append(op)
                    out += main("vlq", args)
                elif op == 0x88:
                    out += bytes([op, _int(args[0]) & 0xff])
                    out += target(args[1]).to_bytes(3, "little")
                elif op in (0x89, 0x8a):
                    out.append(op)
                    out += target(args[0]).to_bytes(3, "little")
                elif op == 0xfe:
                    out.append(op)
                    out += _put("u16", [_int(args[0])])
                elif 0xb0 <= op <= 0xdf:
                    out.append(op)
                    out += main("s8" if op in SIGNED_U8 else "u8", args)
                elif 0xe0 <= op <= 0xef:
                    out.append(op)
                    out += main("s16", args)
                else:
                    out.append(op)
        except (IndexError, KeyError):
            raise AsmError(no, f"cannot read '{line}'")
        except (ValueError, struct.error) as e:
            if isinstance(e, AsmError):
                raise
            raise AsmError(no, f"bad value in '{line}': {e}")
        return bytes(out + tail)

    # every command's size is known without its targets: lay out with placeholders first
    placeholder = {payload: 0 for _, kind, payload in items if kind == "label"}
    labels, p = {}, 0
    for no, kind, payload in items:
        if kind == "label":
            if payload in labels:
                raise AsmError(no, f"label {payload} defined twice")
            labels[payload] = p
        else:
            p += len(encode(no, payload, placeholder))
    out = bytearray()
    for no, kind, payload in items:
        if kind == "cmd":
            out += encode(no, payload, labels)
    exported = {k: v for k, v in labels.items() if not k.startswith(".")}
    return bytes(out), exported


def build_fseq(data, labels, version=0x00020000):
    """An FSEQ file from DATA bytes and {label: offset}."""
    align = lambda n, a: (n + a - 1) // a * a
    data_block = b"DATA" + struct.pack("<I", align(8 + len(data), 0x20)) + data
    data_block += b"\0" * (align(len(data_block), 0x20) - len(data_block))
    names = sorted(labels.items())        # the game's files list labels by name (byte order)
    table_size = 4 + 8 * len(names)
    entries, refs, pos = b"", b"", table_size
    for name, off in names:
        raw = name.encode("ascii")
        e = struct.pack("<HxxiI", 0x1F00, off, len(raw)) + raw + b"\0"
        e += b"\0" * (align(len(e), 4) - len(e))
        refs += struct.pack("<HxxI", 0x5100, pos)
        entries += e
        pos += len(e)
    labl = struct.pack("<I", len(names)) + refs + entries
    labl_block = b"LABL" + struct.pack("<I", align(8 + len(labl), 0x20)) + labl
    labl_block += b"\0" * (align(len(labl_block), 0x20) - len(labl_block))
    labl_off = 0x40 + len(data_block)
    total = labl_off + len(labl_block)
    hdr = struct.pack("<4sHHIIHH", b"FSEQ", 0xFEFF, 0x40, version, total, 2, 0)
    hdr += struct.pack("<HHiI", 0x5000, 0, 0x40, len(data_block))
    hdr += struct.pack("<HHiI", 0x5001, 0, labl_off, len(labl_block))
    hdr += b"\0" * (0x40 - len(hdr))
    return hdr + data_block + labl_block


def compile_text(text, blob=None):
    """FSEQ bytes from listing text (keeping `blob`'s version when given)."""
    data, labels = assemble(text)
    version = struct.unpack_from("<I", blob, 8)[0] if blob else 0x00020000
    return build_fseq(data, labels, version)
