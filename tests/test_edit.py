import os
import struct
import tempfile

import numpy as np
import pytest

from nsmbudx_music import edit, fwav
from nsmbudx_music.audio import read_wav
from nsmbudx_music.bfsar import Bfsar
from nsmbudx_music.cli import Music

BFSAR = os.environ.get("NSMBUDX_BFSAR")
needs_archive = pytest.mark.skipif(not BFSAR or not os.path.exists(BFSAR), reason="NSMBUDX_BFSAR not set")


def test_tree_finds_every_key():
    labels = ["SE_A", "SE_AB", "STRM_BGM_CHIJOU", "STRM_BGM_CHIJOU_FAST", "CUSTOM_BGM_00", "X"]
    tree = edit.build_tree([(s.encode(), i, 0x1000000 | i) for i, s in enumerate(labels)])
    for i, s in enumerate(labels):
        assert edit.tree_lookup(tree, s) == (i, 0x1000000 | i)
    root, count = struct.unpack_from("<II", tree)
    assert count == 2 * len(labels) - 1


def write_raw_wav(path, tag, bits, data, ch=2, rate=48000):
    fmt = struct.pack("<HHIIHH", tag, ch, rate, rate * ch * bits // 8, ch * bits // 8, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", len(body)) + body)


@pytest.mark.parametrize("tag,bits,dtype", [(3, 32, "<f4"), (1, 32, "<i4"), (1, 24, None)])
def test_read_wav_formats(tmp_path, tag, bits, dtype):
    x = np.array([[0.5, -0.5], [0.25, -1.0]])
    if dtype == "<f4":
        data = x.astype("<f4").tobytes()
    elif dtype == "<i4":
        data = (x * 2147483647).astype("<i4").tobytes()
    else:
        v = (x * 8388607).astype(np.int32).ravel()
        data = b"".join(int(n & 0xFFFFFF).to_bytes(3, "little") for n in v)
    p = tmp_path / "t.wav"
    write_raw_wav(p, tag, bits, data)
    pcm, rate, loop = read_wav(str(p))
    assert rate == 48000 and loop is None
    assert np.allclose(pcm, np.clip(x * 32767, -32768, 32767), atol=2)


def test_beat_seq():
    from nsmbudx_music import formats as F
    blob, start = edit.build_beat_seq("CUSTOM_BGM_01", 120, 10.0, loop=2.0)
    assert F.fseq_labels(blob) == {"CUSTOM_BGM_01": start}
    data = F.fseq_data(blob)
    code = dict(F.seq_disasm(data[:start + 9], 0, 400))
    assert code[start].startswith("opentrack 1") and code[start + 5] == "jump -> 0"
    # 10 s at 120 BPM is 1920 ticks: two-bar pattern at 0, 384, 576, 768, ...; loop at 384
    jumps = [c for c in code.values() if c.startswith("jump")]
    assert len(jumps) == 3


@needs_archive
def test_add_and_remove_music():
    bf = Bfsar(BFSAR)
    wave = fwav.Wave.from_pcm(np.zeros((14 * 200, 2), np.int16), 48000, 0, workers=1).build()
    added = Bfsar(edit.add_music(bf, "CUSTOM_BGM_07", wave, bpm=128, offset=0.0390625))
    tpl = next(s for s in bf.sounds if s.label == edit.TEMPLATE)
    assert edit.custom_music(added) == [("CUSTOM_BGM_07", wave, 128, tpl.volume, 0.0390625)]
    s = added.sounds[-1]
    assert s.label == "CUSTOM_BGM_07" and s.kind == "SEQ"
    assert s.detail["banks"] == [added.banks[-1]["id"]] and s.detail["track_flags"] == 3
    # the game's own files are untouched, except GROUP_BOOT, which now lists the new ones
    boot = next(g["file"] for g in bf.groups if g["label"] == "GROUP_BOOT")
    assert all(added.file_bytes(i) == bf.file_bytes(i) for i in range(len(bf.files)) if i != boot)
    files, sets = edit.parse_fgrp(added.file_bytes(boot))
    old_files, old_sets = edit.parse_fgrp(bf.file_bytes(boot))
    n = len(bf.files)
    assert files == old_files + [n, n + 1, n + 2]
    assert sets == old_sets + [(s.id, 0xFFFFFFFF), (added.banks[-1]["id"], 0xFFFFFFFF)]
    assert added.files[-1]["groups"] == [next(g["id"] for g in bf.groups if g["label"] == "GROUP_BOOT")]
    # adding the same label again replaces it; removing gives the game's archive back
    again = Bfsar(edit.add_music(added, "CUSTOM_BGM_07", wave, bpm=90))
    assert [m[0] for m in edit.custom_music(again)] == ["CUSTOM_BGM_07"]
    assert edit.remove_music(again, "CUSTOM_BGM_07") == edit.Editor(bf).build()
