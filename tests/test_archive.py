"""Tests against the game's own archive.  Set NSMBUDX_BFSAR to a dumped
romfs/Sound/Sound.bfsar (v1.0.0); they are skipped otherwise."""
import os

import numpy as np
import pytest

from nsmbudx_music import fwav
from nsmbudx_music.cli import Music

BFSAR = os.environ.get("NSMBUDX_BFSAR")
pytestmark = pytest.mark.skipif(not BFSAR or not os.path.exists(BFSAR),
                                reason="NSMBUDX_BFSAR not set")


@pytest.fixture(scope="module")
def music():
    return Music(BFSAR)


def test_counts(music):
    bf = music.bf
    assert bf.version == 0x20500
    assert len(bf.sounds) == 3502
    assert not any(s.kind == "STRM" for s in bf.sounds)


def test_unchanged_rebuild_is_identical(music):
    assert fwav.rebuild_bfsar(music.bf, {}) == music.bf.data


def test_course_bgm_layers(music):
    s = next(x for x in music.bf.sounds if x.label == "STRM_BGM_CHIJOU")
    layers = music.layers(s)
    assert [l[0] for l in layers] == ["track1_prg0", "track2_prg1", "track3_prg2"]
    assert [l[3] for l in layers] == [127, 110, 80]


def test_replace_one_wave(music):
    s = next(x for x in music.bf.sounds if x.label == "STRM_BGM_TITLE")
    (name, wavearc, index, _), = music.layers(s)
    fid, waves = music.war(wavearc)
    pcm = np.zeros((14 * 100, 2), np.int16)
    new = fwav.Wave.from_pcm(pcm, 48000, 0, workers=1).build()
    waves = list(waves)
    waves[index] = new
    out = fwav.rebuild_bfsar(music.bf, {fid: fwav.build_fwar(waves)})

    from nsmbudx_music.bfsar import Bfsar
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".bfsar", delete=False) as f:
        f.write(out)
    try:
        bf2 = Bfsar(f.name)
        changed = [i for i in range(len(bf2.files)) if bf2.file_bytes(i) != music.bf.file_bytes(i)]
        assert changed == [fid]
        assert fwav.fwar_waves(bf2.file_bytes(fid))[index] == new
    finally:
        os.unlink(f.name)
