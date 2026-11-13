import os
import shutil
import subprocess

import numpy as np
import pytest

from nsmbudx_music import banks, edit, fwav, info, render, seq
from nsmbudx_music import formats as F
from nsmbudx_music.bfsar import Bfsar

BFSAR = os.environ.get("NSMBUDX_BFSAR")
needs_archive = pytest.mark.skipif(not BFSAR or not os.path.exists(BFSAR), reason="NSMBUDX_BFSAR not set")


@pytest.fixture(scope="module")
def bf():
    return Bfsar(BFSAR)


def test_assembler_round_trip_and_errors():
    text = "Start:\n    timebase 96\n    tempo 120\n.L0:\n    cn4 127, 48\n    volume_r 100, 120\n" \
           "    setvar 0, 3\n    if jump .L0\n    wait_v 2\n    pan_t 64, 12\n    fin\n"
    data, labels = seq.assemble(text)
    assert labels == {"Start": 0}
    again = seq.Listing(data, labels).text
    assert seq.assemble(again)[0] == data
    with pytest.raises(seq.AsmError) as e:
        seq.assemble("    jump nowhere\n")
    assert e.value.line == 1


def test_envelope_shape():
    flat = render.adshr_curve({"attack": 127, "decay": 127, "sustain": 127, "hold": 0, "release": 100}, 100, 400)
    assert flat[50] == pytest.approx(1.0) and flat[399] < flat[150] < flat[100]
    # attack 90 takes ~117 ms, decay 100 then falls 0.046 dB/ms to the sustain level by ~380 ms
    soft = render.adshr_curve({"attack": 90, "decay": 100, "sustain": 64, "hold": 0, "release": 127}, 590, 700)
    assert soft[1] < 0.1 < soft[60] and soft[580] == pytest.approx(10 ** (40 * np.log10(64 / 127) / 20), rel=1e-3)
    assert soft[600] < 1e-3


@needs_archive
def test_every_sequence_rebuilds_from_text(bf):
    for fid in sorted({s.file_id for s in bf.sounds if s.kind == "SEQ"}):
        blob = bf.file_bytes(fid)
        assert seq.compile_text(seq.disassemble(blob).text, blob) == blob, fid


@needs_archive
def test_banks_and_fields(bf):
    regions = sum(len(k["velocity"]) for b in bf.banks for inst in banks.Bank(bf.file_bytes(b["file"])).instruments
                  if inst for k in inst["keys"])
    assert regions == 2557
    ed = edit.Editor(bf)
    for k, name in enumerate(info.SECTION_NAMES):
        for i in range(len(ed.sections[k])):
            info.fields(ed, name, i)
    info.set_field(ed, "sound", 0, "volume", 77)
    assert Bfsar(ed.build()).sounds[0].volume == 77


@needs_archive
def test_course_music_renders_with_its_loop(bf):
    s = next(s for s in bf.sounds if s.label == "STRM_BGM_CHIJOU")
    r = render.render(bf, s)
    assert sorted(r.tracks) == [0, 1, 2, 3]
    assert r.loop[0] == pytest.approx(4.36, abs=0.02) and r.loop[1] == pytest.approx(45.82, abs=0.02)
    assert any(v[2] == 0 for v in r.vars) and any(v[2] == 1 for v in r.vars)


@needs_archive
def test_yoshi_layer(bf):
    song = fwav.Wave.from_pcm(np.zeros((14 * 300, 2), np.int16), 48000, 0, workers=1).build()
    added = Bfsar(edit.add_music(bf, "CUSTOM_BGM_05", song, bpm=100, yoshi=song))
    s = added.sounds[-1]
    assert s.detail["track_flags"] == 0b111
    assert s.options[31] == 100 | edit.SONG_YOSHI      # BPM low, song flags for the sprite high
    assert edit.custom_music(added)[0][6] == song
    starts = [c.target for c in seq.disassemble(added.file_bytes(s.file_id)).code.values() if c.op == 0x88]
    assert len(starts) == 2
    with pytest.raises(ValueError):
        short = fwav.Wave.from_pcm(np.zeros((14 * 100, 2), np.int16), 48000, 0, workers=1).build()
        edit.add_music(bf, "CUSTOM_BGM_05", song, bpm=100, yoshi=short)


NODE = shutil.which("node") or next((p for p in (r"C:\Program Files\nodejs\node.exe",) if os.path.exists(p)), None)


@pytest.mark.skipif(not NODE, reason="Node.js not installed")
def test_studio_script_parses():
    app = os.path.join(os.path.dirname(__file__), "..", "nsmbudx_music", "studio", "static", "app.js")
    r = subprocess.run([NODE, "--check", app], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
