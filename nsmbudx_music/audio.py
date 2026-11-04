"""WAV in and out, tempo estimate, and turning a WAV into a game wave (FWAV)."""
import struct
import wave as wavmod

import numpy as np

from . import fwav

FORMAT_PCM, FORMAT_FLOAT, FORMAT_EXTENSIBLE = 1, 3, 0xFFFE


def write_wav(path, pcm, rate, loop_start=None, loop_end=None):
    """16-bit WAV; a loop goes in a 'smpl' chunk, as audio editors write it."""
    with wavmod.open(path, "wb") as w:
        w.setnchannels(pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype("<i2").tobytes())
    if loop_start is not None:
        smpl = struct.pack("<9I", 0, 0, int(1e9 / rate), 60, 0, 0, 0, 1, 0)
        smpl += struct.pack("<6I", 0, 0, loop_start, loop_end - 1, 0, 0)
        with open(path, "r+b") as f:
            f.seek(0, 2)
            f.write(b"smpl" + struct.pack("<I", len(smpl)) + smpl)
            size = f.tell()
            f.seek(4)
            f.write(struct.pack("<I", size - 8))


def read_wav(path):
    """Returns (pcm (n, ch) int16, rate, loop_start or None).

    Reads 8/16/24/32-bit integer and 32/64-bit float WAV (also WAVE_FORMAT_EXTENSIBLE),
    and a loop from a 'smpl' chunk if there is one.
    """
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"{path}: not a RIFF WAVE file")
    p, fmt, data, loop = 12, None, None, None
    while p + 8 <= len(raw):
        cid, size = raw[p:p + 4], struct.unpack_from("<I", raw, p + 4)[0]
        body = raw[p + 8:p + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack_from("<HHIIHH", body)
            if fmt[0] == FORMAT_EXTENSIBLE and len(body) >= 26:
                fmt = (struct.unpack_from("<H", body, 24)[0],) + fmt[1:]
        elif cid == b"data":
            data = body
        elif cid == b"smpl" and len(body) >= 36 and struct.unpack_from("<I", body, 28)[0] > 0:
            loop = struct.unpack_from("<I", body, 36 + 8)[0]
        p += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise ValueError(f"{path}: no fmt or data chunk")
    tag, ch, rate, _, _, bits = fmt
    width = bits // 8
    data = data[:len(data) // (width * ch) * width * ch]
    if tag == FORMAT_FLOAT and bits in (32, 64):
        x = np.frombuffer(data, dtype="<f4" if bits == 32 else "<f8").astype(np.float64) * 32767.0
    elif tag == FORMAT_PCM and bits == 8:
        x = (np.frombuffer(data, dtype=np.uint8).astype(np.float64) - 128.0) * 256.0
    elif tag == FORMAT_PCM and bits == 16:
        return np.frombuffer(data, dtype="<i2").reshape(-1, ch), rate, loop
    elif tag == FORMAT_PCM and bits == 24:
        b = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | b[:, 1] << 8 | b[:, 2] << 16
        x = (np.where(v & 0x800000, v - 0x1000000, v) >> 8).astype(np.float64)
    elif tag == FORMAT_PCM and bits == 32:
        x = (np.frombuffer(data, dtype="<i4") >> 16).astype(np.float64)
    else:
        raise ValueError(f"{path}: unsupported WAV format {tag}, {bits} bits")
    pcm = np.clip(np.round(x), -32768, 32767).astype(np.int16)
    return pcm.reshape(-1, ch), rate, loop


def estimate_bpm(pcm, rate, lo=70, hi=180):
    """Rough tempo from the onset envelope's autocorrelation: good enough for beat sync."""
    mono = pcm.astype(np.float64).mean(axis=1)
    hop = rate // 100                                  # 100 envelope frames a second
    n = len(mono) // hop
    if n < 400:
        return 120
    energy = np.log1p((mono[:n * hop].reshape(n, hop) ** 2).sum(axis=1))
    onset = np.maximum(np.diff(energy), 0)
    onset -= onset.mean()
    spec = np.fft.rfft(onset, 2 * len(onset))
    ac = np.fft.irfft(spec * np.conj(spec))[:len(onset)]
    lags = np.arange(int(6000 / hi), int(6000 / lo) + 1)
    best = lags[np.argmax(ac[lags])]
    return int(round(6000 / best))


def wav_to_fwav(path, loop_start=None, loop=True, log=print):
    """Encode a WAV to FWAV bytes. Without a loop start: the WAV's own loop, else the whole song.

    DSP-ADPCM loops start on a 14-sample frame; a loop start that is not is moved by
    padding the song's start with up to 13 samples of silence.
    """
    pcm, rate, wav_loop = read_wav(path)
    ls = None
    if loop:
        ls = loop_start if loop_start is not None else (wav_loop or 0)
    if ls and ls % 14:
        pad = 14 - ls % 14
        pcm = np.concatenate([np.zeros((pad, pcm.shape[1]), np.int16), pcm])
        ls += pad
    log(f"encoding {pcm.shape[0]} samples x {pcm.shape[1]} ch @ {rate} Hz, loop {ls}")
    return fwav.Wave.from_pcm(pcm, rate, ls).build(), pcm, rate


def silent_wave(rate, channels):
    """A 14-sample looping silent wave: mutes a layer at almost no memory cost."""
    return fwav.Wave.from_pcm(np.zeros((14, channels), np.int16), rate, 0, workers=1).build()
