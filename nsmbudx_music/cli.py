"""NSMBUDX music tool: list, extract and inject music in romfs/Sound/Sound.bfsar.

How the game stores music (the full write-up is nsmbud-reverse's
reverse-engineering/FindingsI/AUDIO.md):
  * Level music (STRM_BGM_CHIJOU, ...) is a SEQ sound.  Its sequence opens tracks
    1..3, each playing one note on program N of a BANK_BGM_* bank; program N is a
    full-length looping stereo DSP-ADPCM wave ("layer").  Track 0 is a silent
    beat map that drives enemy "bah" sync.  *_FAST uses programs 3..5.
  * Jingles / boss / menu music marked STRM_ or WSD_ are mostly WSD sounds: one
    wave each in a shared wave archive.

    python -m nsmbudx_music <Sound.bfsar> list [regex]
    python -m nsmbudx_music <Sound.bfsar> extract <out_dir> [regex] [--wav]
    python -m nsmbudx_music <Sound.bfsar> inject <label> <layer|all> <in.wav|in.bfwav>
                            -o <out.bfsar> [--loop-start SAMPLES | --no-loop] [--silence-others]
    python -m nsmbudx_music <Sound.bfsar> add <slot> <in.wav> -o <out.bfsar> [--fast] [--bpm N] [--offset S] [--bahs FILE]
    python -m nsmbudx_music <Sound.bfsar> remove <slot> -o <out.bfsar>
    python -m nsmbudx_music <Sound.bfsar> dump <out_dir>      # every table as CSV
"""
import argparse, os, re, sys
from .bfsar import Bfsar, dump_csv, summary
from . import edit, formats as F, fwav
from .audio import estimate_beat, read_wav, silent_wave, wav_to_fwav, write_wav

# SndBgmMgr course music tables in main v1.0.0 (32-bit): {name, id} x 30, 1-based.
# 0x60c28460 normal, 0x60c28370 _FAST, 0x60c28640 control _V, 0x60c28550 control _FAST_V.
COURSE_BGM = [None, "CHIJOU", "CHIKA", "WATER", "ATHLETIC", "OBAKE", "SABAKU", "YUKI", "KAZAN",
              "TORIDE", "SHIRO", "HIKOUSEN", "BONUS", "ROAD_TO_LAST", "TORIDE_BOSS", "SHIRO_BOSS",
              "MINIGAME", "HIKOUSEN_BOSS", "MORI", "MG_BTL", "SANBASHI", "KAZAN_TIKA", "SHADOW",
              "STAFF_CREDIT", "HIKOUSEN_ROUKA", "LAST_BOSS", "LAST_BOSS2", "LAST_BOSS2_HARP",
              "SHIRO_LAST", "DUMMY"]


# What each course music is, for people.
COURSE_NAMES = {
    "CHIJOU": "Overworld", "CHIKA": "Underground", "WATER": "Underwater", "ATHLETIC": "Athletic",
    "OBAKE": "Ghost House", "SABAKU": "Desert", "YUKI": "Snow", "KAZAN": "Volcano", "TORIDE": "Tower",
    "SHIRO": "Castle", "HIKOUSEN": "Airship", "BONUS": "Bonus room", "ROAD_TO_LAST": "Final world",
    "TORIDE_BOSS": "Tower boss", "SHIRO_BOSS": "Castle boss", "MINIGAME": "Minigame",
    "HIKOUSEN_BOSS": "Airship boss", "MORI": "Forest", "MG_BTL": "Boost/coin battle",
    "SANBASHI": "Beach", "KAZAN_TIKA": "Volcano cave", "SHADOW": "Shadow chase",
    "STAFF_CREDIT": "Credits", "HIKOUSEN_ROUKA": "Airship corridor", "LAST_BOSS": "Bowser",
    "LAST_BOSS2": "Bowser (2)", "LAST_BOSS2_HARP": "Bowser (harp)", "SHIRO_LAST": "Final castle",
}


def course_id(label):
    m = re.match(r"(?:STRM|WSD)_BGM_(.+?)(_FAST)?$", label)
    if m and m.group(1) in COURSE_BGM:
        return COURSE_BGM.index(m.group(1)), bool(m.group(2))
    return None, False


class Music:
    def __init__(self, source):
        self.bf = Bfsar(source)
        self._war = {}
        self.wsd_by_sound = {}

    def war(self, wavearc_id):
        wa = self.bf.wavearcs[wavearc_id & 0xFFFFFF]
        fid = wa["file"]
        if fid not in self._war:
            self._war[fid] = fwav.fwar_waves(self.bf.file_bytes(fid))
        return fid, self._war[fid]

    def layers(self, s):
        """[(layer name, wavearc id, wave index, mix volume)] for a music sound."""
        bf = self.bf
        blob = bf.file_bytes(s.file_id)
        if s.kind == "WSD":
            return [(f"wave{k}", a, i, None)
                    for k, (a, i) in enumerate(F.fwsd_sound_waves(blob, s.detail["index"]))]
        if s.kind != "SEQ":
            return []
        data = F.fseq_data(blob)
        start = s.detail["seq_opts"].get(0, 0)
        progs, targets = [], []
        for at, t in F.seq_disasm(data, start, 12):
            if t.startswith("opentrack"):
                targets.append(int(t.split("->")[1]))
            elif t.startswith("jump") or t == "fin":
                break
        for tgt in targets:
            for at, t in F.seq_disasm(data, tgt, 8):
                if t.startswith("prg"):
                    progs.append(int(t.split()[1]))
                    break
        out = []
        for bid in s.detail["banks"]:
            bblob = bf.file_bytes(bf.banks[bid & 0xFFFFFF]["file"])
            waves = F.fbnk_waves(bblob)
            inst = F.fbnk_instruments(bblob)
            for n, p in enumerate(progs):
                for vr in inst[p] if p < len(inst) else []:
                    a, i = waves[vr["wave"]]
                    out.append((f"track{n + 1}_prg{p}", a, i, vr["volume"]))
        return out

    def music_sounds(self, rx=None):
        for s in self.bf.sounds:
            if "BGM" not in s.label or s.kind not in ("SEQ", "WSD"):
                continue
            if s.label.startswith("SEQ_BGM") and s.label.endswith("_V"):
                continue                       # silent control sequences
            if rx and not re.search(rx, s.label):
                continue
            yield s


def cmd_list(m, rx):
    print(f"{'label':34s} {'kind':4s} {'course':6s} layer           wavearc-file wave  ch  rate   loop      seconds  mixvol")
    for s in m.music_sounds(rx):
        cid, fast = course_id(s.label)
        tag = f"{cid}{'F' if fast else ''}" if cid else ""
        for name, a, i, vol in m.layers(s):
            fid, waves = m.war(a)
            info = F.fwav_info(waves[i])
            loop = info["loop_start"] if info["loop"] else "-"
            print(f"{s.label:34s} {s.kind:4s} {tag:6s} {name:15s} {fid:12d} {i:4d}  {info['channels']:2d} {info['rate']:6d} "
                  f"{str(loop):9s} {info['samples'] / info['rate']:7.1f}  {vol if vol is not None else ''}")


def cmd_extract(m, out, rx, as_wav):
    os.makedirs(out, exist_ok=True)
    n = 0
    for s in m.music_sounds(rx):
        for name, a, i, vol in m.layers(s):
            fid, waves = m.war(a)
            base = os.path.join(out, f"{s.label}.{name}")
            with open(base + ".bfwav", "wb") as f:
                f.write(waves[i])
            if as_wav:
                w = fwav.Wave.parse(waves[i])
                write_wav(base + ".wav", w.pcm(), w.rate,
                          w.loop_start if w.loop else None, w.samples)
            n += 1
    print(f"wrote {n} layer(s) to {out}")


def load_input(path, loop_start, no_loop):
    if path.lower().endswith(".bfwav"):
        blob = open(path, "rb").read()
        if blob[:4] != b"FWAV":
            sys.exit(f"{path}: not an FWAV")
        if blob[4:6] != b"\xff\xfe":
            sys.exit("big-endian (Wii U) BFWAV: convert it to little-endian/Switch first")
        return blob
    return wav_to_fwav(path, loop_start, not no_loop)[0]


def cmd_inject(m, label, layer, src, out, loop_start, no_loop, silence_others):
    s = next((x for x in m.bf.sounds if x.label == label), None)
    if s is None:
        sys.exit(f"no sound {label}")
    layers = m.layers(s)
    pick = layers if layer == "all" else [l for l in layers if l[0] == layer or l[0].startswith(layer)]
    if not pick:
        sys.exit(f"{label} layers: {[l[0] for l in layers]}")
    new = load_input(src, loop_start, no_loop)
    info = F.fwav_info(new)
    plan = [(l, new, os.path.basename(src)) for l in pick]
    if silence_others:
        quiet = silent_wave(info["rate"], info["channels"])
        plan += [(l, quiet, "silence") for l in layers if l not in pick]
    replace = {}
    for (name, a, i, vol), blob, what in plan:
        fid, waves = m.war(a)
        waves = list(replace.get(fid, waves))
        waves[i] = blob
        replace[fid] = waves
        print(f"  {label}.{name}: wave archive file {fid} wave {i} <- {what}")
    blobs = {fid: fwav.build_fwar(w) for fid, w in replace.items()}
    data = fwav.rebuild_bfsar(m.bf, blobs)
    with open(out, "wb") as f:
        f.write(data)
    print(f"wrote {out} ({len(data):,} bytes)")


def cmd_add(m, name, src, out, fast, bpm, offset, bahs, loop_start, no_loop, volume):
    label = edit.custom_label(name, fast)
    if src.lower().endswith(".bfwav"):
        blob = load_input(src, None, False)
        rate_bpm, offset = bpm or 120, offset or 0.0
    else:
        blob, pcm, rate = wav_to_fwav(src, loop_start, not no_loop)
        found_bpm, found_offset = estimate_beat(pcm, rate, bpm)
        rate_bpm = bpm or found_bpm
        offset = found_offset if offset is None else offset
    if bahs:
        with open(bahs, encoding="utf-8-sig") as f:
            bahs = edit.parse_bahs(f.read())
        print(f"{len(bahs)} bahs from the file")
    data = edit.add_music(m.bf, label, blob, rate_bpm, volume, offset, bahs or None)
    with open(out, "wb") as f:
        f.write(data)
    print(f"{label}: {os.path.basename(src)}, {rate_bpm} BPM, first beat {offset:.3f} s -> {out} "
          f"({len(data):,} bytes)")


def cmd_remove(m, name, out):
    data = edit.remove_music(m.bf, edit.custom_label(name))
    with open(out, "wb") as f:
        f.write(data)
    print(f"removed {edit.custom_label(name)} -> {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bfsar")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("regex", nargs="?")
    p = sub.add_parser("dump"); p.add_argument("out")
    p = sub.add_parser("extract"); p.add_argument("out"); p.add_argument("regex", nargs="?")
    p.add_argument("--wav", action="store_true", help="also decode to .wav (slow: pure Python)")
    p = sub.add_parser("inject"); p.add_argument("label"); p.add_argument("layer")
    p.add_argument("input"); p.add_argument("-o", "--out", required=True)
    p.add_argument("--loop-start", type=int); p.add_argument("--no-loop", action="store_true")
    p.add_argument("--silence-others", action="store_true",
                   help="replace this sound's other stem layers with silence")
    p = sub.add_parser("add", help="add new music (CUSTOM_BGM_<slot>) for the Custom Music sprite")
    p.add_argument("slot", help="slot number (0-99) or a full label"); p.add_argument("input")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--fast", action="store_true", help="the hurry-up version (CUSTOM_BGM_<slot>_FAST)")
    p.add_argument("--bpm", type=int, help="tempo for the enemies' beat (default: estimated)")
    p.add_argument("--offset", type=float, help="seconds to the song's first beat (default: estimated)")
    p.add_argument("--bahs", help="text file of times (seconds [kind]) for the enemies' hops, "
                                  "or an Audacity label export (default: every fourth bar)")
    p.add_argument("--volume", type=int, help="0-255 (default 80, like the overworld music)")
    p.add_argument("--loop-start", type=int); p.add_argument("--no-loop", action="store_true")
    p = sub.add_parser("remove", help="remove added music"); p.add_argument("slot")
    p.add_argument("-o", "--out", required=True)
    a = ap.parse_args()
    m = Music(a.bfsar)
    if a.cmd == "list":
        cmd_list(m, a.regex)
    elif a.cmd == "dump":
        dump_csv(m.bf, a.out)
        print(summary(m.bf))
    elif a.cmd == "extract":
        cmd_extract(m, a.out, a.regex, a.wav)
    elif a.cmd == "add":
        cmd_add(m, a.slot, a.input, a.out, a.fast, a.bpm, a.offset, a.bahs, a.loop_start, a.no_loop, a.volume)
    elif a.cmd == "remove":
        cmd_remove(m, a.slot, a.out)
    else:
        cmd_inject(m, a.label, a.layer, a.input, a.out, a.loop_start, a.no_loop, a.silence_others)
