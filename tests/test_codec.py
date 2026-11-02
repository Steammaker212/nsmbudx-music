import numpy as np

from nsmbudx_music import dspadpcm, fwav


def tone(seconds=3, rate=48000, freqs=(262, 392)):
    t = np.arange(int(rate * seconds)) / rate
    return np.stack([0.3 * np.sin(2 * np.pi * f * t) * 32767 for f in freqs], 1).astype(np.int16)


def snr(ref, out):
    ref = ref.astype(float)
    return 10 * np.log10((ref ** 2).sum() / ((ref - out.astype(float)) ** 2).sum())


def test_encoder_tracks_decoder():
    # several chunks, so the seam repair between parallel chunks is exercised
    pcm = tone(seconds=5)[:, 0]
    data, coefs, tracked = dspadpcm.encode(pcm, chunk_frames=1024)
    decoded = dspadpcm.decode(data, coefs, len(pcm))
    assert np.array_equal(tracked, decoded)
    assert snr(pcm, decoded) > 40


def test_single_worker_matches_parallel():
    pcm = tone(seconds=2)[:, 1]
    a = dspadpcm.encode(pcm, chunk_frames=512)[0]
    b = dspadpcm.encode(pcm, chunk_frames=512, workers=1)[0]
    assert a == b


def test_fwav_roundtrip_keeps_loop_context():
    pcm = tone(seconds=2)
    loop = 14 * 3000
    blob = fwav.Wave.from_pcm(pcm, 48000, loop).build()
    w = fwav.Wave.parse(blob)
    assert (w.rate, w.loop, w.loop_start, w.samples) == (48000, True, loop, len(pcm))
    out = w.pcm()
    for c, ch in enumerate(w.channels):
        assert (ch["lyn1"], ch["lyn2"]) == (int(out[loop - 1, c]), int(out[loop - 2, c]))
        assert ch["lps"] == ch["data"][loop // 14 * 8]
    assert snr(pcm, out) > 40


def test_fwar_build_and_read():
    waves = [fwav.Wave.from_pcm(tone(seconds=0.2), 48000, 0, workers=1).build() for _ in range(3)]
    war = fwav.build_fwar(waves)
    assert fwav.fwar_waves(war) == waves
