"""Nintendo DSP-ADPCM (GC/Wii/Switch "DSP-ADPCM", FWAV encoding 2) codec.

Frame = 8 bytes: header (high nibble coef index 0-7, low nibble scale shift),
then 14 signed 4-bit samples.  Decode:
    s = ((nib << scale) << 11) + 1024 + c1*h1 + c2*h2 >> 11, clamped to s16
Coefficients are 8 (c1, c2) pairs in 5.11 fixed point.

The encoder is our own: per-frame least-squares predictors clustered to 8
pairs (LBG/k-means), then a quantisation search over every coef pair and the
two nearest scales.  Channels are split into chunks encoded in parallel.
"""
import os
import numpy as np
from concurrent.futures import ProcessPoolExecutor

SAMPLES_PER_FRAME = 14
BYTES_PER_FRAME = 8


def frames_for(samples):
    return (samples + SAMPLES_PER_FRAME - 1) // SAMPLES_PER_FRAME


# --- decode ----------------------------------------------------------------
def decode(data, coefs, samples, h1=0, h2=0):
    """data: bytes of one channel; coefs: 16 ints.  Returns int16 ndarray."""
    out = np.empty(samples, dtype=np.int16)
    c = [(coefs[2 * i], coefs[2 * i + 1]) for i in range(8)]
    n = 0
    for f in range(frames_for(samples)):
        hdr = data[f * 8]
        c1, c2 = c[hdr >> 4]
        scale = 1 << (hdr & 0xF)
        for k in range(SAMPLES_PER_FRAME):
            if n >= samples:
                break
            b = data[f * 8 + 1 + k // 2]
            nib = (b >> 4) if k % 2 == 0 else (b & 0xF)
            if nib >= 8:
                nib -= 16
            s = ((nib * scale) << 11) + 1024 + c1 * h1 + c2 * h2 >> 11
            s = -32768 if s < -32768 else 32767 if s > 32767 else s
            out[n] = s
            h2, h1 = h1, s
            n += 1
    return out


# --- coefficients ----------------------------------------------------------
def _frame_predictors(pcm):
    """Least-squares (c1, c2) per 14-sample frame, with frame energy as weight."""
    x = np.concatenate([np.zeros(2), pcm.astype(np.float64)])
    nf = frames_for(len(pcm))
    x = np.concatenate([x, np.zeros(nf * 14 + 2 - len(x))])
    idx = np.arange(nf)[:, None] * 14 + np.arange(14)[None, :] + 2
    y, a, b = x[idx], x[idx - 1], x[idx - 2]
    raa, rbb, rab = (a * a).sum(1), (b * b).sum(1), (a * b).sum(1)
    rya, ryb = (y * a).sum(1), (y * b).sum(1)
    det = raa * rbb - rab * rab
    ok = np.abs(det) > 1e-6
    c1 = np.where(ok, (rya * rbb - ryb * rab) / np.where(ok, det, 1), 0.0)
    c2 = np.where(ok, (ryb * raa - rya * rab) / np.where(ok, det, 1), 0.0)
    # keep the 2-pole filter stable and in 5.11 range
    c2 = np.clip(c2, -0.999, 0.999)
    c1 = np.clip(c1, -(1 - c2) * 0.999, (1 - c2) * 0.999)
    w = (y * y).sum(1) + 1.0
    return np.stack([c1, c2], 1), w


def compute_coefs(pcm, iters=12):
    """8 (c1, c2) pairs as 16 ints in 5.11 fixed point."""
    p, w = _frame_predictors(pcm)
    if len(p) == 0:
        return [0] * 16
    w = np.log1p(w)
    # LBG split from the weighted mean
    cents = [np.average(p, axis=0, weights=w)]
    while len(cents) < 8:
        cents = [c * f for c in cents for f in (1.01, 0.99)]
        C = np.array(cents)
        for _ in range(iters):
            lab = ((p[:, None, :] - C[None]) ** 2).sum(2).argmin(1)
            for k in range(len(C)):
                m = lab == k
                if m.any():
                    C[k] = np.average(p[m], axis=0, weights=w[m])
        cents = list(C)
    C = np.clip(np.array(cents) * 2048, -32768, 32767).round().astype(int)
    return [int(v) for pair in C for v in pair]


# --- encode ----------------------------------------------------------------
def _encode_frame(x, h1, h2, C1, C2):
    """One 14-sample frame given decoder history.  Returns (8 bytes, decoded[14])."""
    # scale guess per coef from the unquantised residual, then try guess and guess+1
    p1, p2 = h1, h2
    res = []
    for k in range(14):
        res.append(x[k] * 2048 - (C1[0::2] * p1 + C2[0::2] * p2))
        p2, p1 = p1, int(x[k])
    mx = np.abs(np.array(res)).max(0) / 2048.0
    s0 = np.zeros(8, dtype=np.int64)
    for s in range(12):
        s0 += (mx > 7 * (1 << s))
    S = np.minimum(np.repeat(s0, 2) + np.tile([0, 1], 8), 12)
    step = (1 << S) << 11
    a1 = np.full(16, h1, dtype=np.int64)
    a2 = np.full(16, h2, dtype=np.int64)
    err = np.zeros(16, dtype=np.int64)
    nibs = np.empty((14, 16), dtype=np.int64)
    outs = np.empty((14, 16), dtype=np.int64)
    for k in range(14):
        pred = C1 * a1 + C2 * a2 + 1024
        q = np.clip(np.floor_divide(x[k] * 2048 - pred + step // 2, step), -8, 7)
        s = np.clip((q * step + pred) >> 11, -32768, 32767)
        d = s - x[k]
        err += d * d
        nibs[k], outs[k] = q, s
        a2, a1 = a1, s
    best = int(err.argmin())
    nb = nibs[:, best] & 0xF
    frame = bytes([(best // 2) << 4 | int(S[best])] +
                  [int(nb[2 * k] << 4 | nb[2 * k + 1]) for k in range(7)])
    return frame, outs[:, best]


def _cands(coefs):
    C1 = np.repeat(np.array(coefs[0::2], dtype=np.int64), 2)      # 8 pairs x 2 scales
    C2 = np.repeat(np.array(coefs[1::2], dtype=np.int64), 2)
    return C1, C2


def _padded(pcm):
    nf = frames_for(len(pcm))
    pad = np.zeros(nf * 14, dtype=np.int64)
    pad[:len(pcm)] = pcm
    return pad, nf


def _encode_chunk(args):
    pcm, coefs, h1, h2 = args
    pad, nf = _padded(pcm)
    C1, C2 = _cands(coefs)
    out = bytearray(nf * 8)
    dec = np.empty(nf * 14, dtype=np.int64)
    for f in range(nf):
        frame, o = _encode_frame(pad[f * 14:f * 14 + 14], h1, h2, C1, C2)
        out[f * 8:f * 8 + 8] = frame
        dec[f * 14:f * 14 + 14] = o
        h1, h2 = int(o[13]), int(o[12])
    return bytes(out), dec


def encode(pcm, coefs=None, workers=None, chunk_frames=4096):
    """pcm: int16 ndarray (one channel).  Returns (data, coefs, decoded int16)."""
    pcm = np.asarray(pcm, dtype=np.int16)
    if coefs is None:
        coefs = compute_coefs(pcm)
    n = chunk_frames * 14
    jobs = []
    for i in range(0, len(pcm), n):
        h1 = int(pcm[i - 1]) if i >= 1 else 0
        h2 = int(pcm[i - 2]) if i >= 2 else 0
        jobs.append((pcm[i:i + n], coefs, h1, h2))
    if len(jobs) > 1 and workers != 1:
        with ProcessPoolExecutor(max_workers=workers or os.cpu_count()) as ex:
            parts = [[bytearray(d), o] for d, o in ex.map(_encode_chunk, jobs)]
    else:
        parts = [[bytearray(d), o] for d, o in map(_encode_chunk, jobs)]
    # Seam repair: chunk k was encoded from source history, but the decoder will carry the
    # decoded history of chunk k-1.  Re-encode frames with the true history until the
    # history matches the parallel pass again; from there the frames are identical, so
    # the result equals a single sequential encode.
    C1, C2 = _cands(coefs)
    for k in range(1, len(parts)):
        prev = parts[k - 1][1]
        h1, h2 = int(prev[-1]), int(prev[-2])
        pad, nf = _padded(jobs[k][0])
        data_k, dec_k = parts[k]
        for f in range(nf):
            frame, o = _encode_frame(pad[f * 14:f * 14 + 14], h1, h2, C1, C2)
            data_k[f * 8:f * 8 + 8] = frame
            same = int(o[13]) == int(dec_k[f * 14 + 13]) and int(o[12]) == int(dec_k[f * 14 + 12])
            dec_k[f * 14:f * 14 + 14] = o
            h1, h2 = int(o[13]), int(o[12])
            if same:                             # history re-synced: rest of chunk is identical
                break
    data = b"".join(bytes(p[0]) for p in parts)
    dec = (np.concatenate([p[1] for p in parts])[:len(pcm)].astype(np.int16)
           if parts else np.zeros(0, np.int16))
    return data, coefs, dec
