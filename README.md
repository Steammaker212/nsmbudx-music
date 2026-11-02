# nsmbudx-music

List, extract and replace the music in *New Super Mario Bros. U Deluxe* (Switch,
v1.0.0). Works directly on the game's `romfs/Sound/Sound.bfsar`: no hard-coded
offsets, and a new song can be any length.

No game files are included. You need your own dump of the game's romfs.

## How the game stores music

Everything is in one file, `romfs/Sound/Sound.bfsar` (354 MB). It has no stream
sounds. The `STRM_` in names like `STRM_BGM_CHIJOU` is left over from the Wii U
version.

- **Course music** (`STRM_BGM_CHIJOU`, `STRM_BGM_CHIKA`, ...) is a *sequence*. It
  plays two or three looping stereo recordings at the same time through a bank, as
  "instruments". These are stems: the song you hear is their sum. The tool calls them
  layers (`track1`, `track2`, `track3`). The hurry-up version (`_FAST`) is a separate
  sound with its own layers.
- A silent track 0 in the same sequence carries the beat map the enemies dance to.
- **Title, jingles, boss and airship music** are single waves (`wave0`).
- All audio is Nintendo DSP-ADPCM. The tool decodes and encodes it itself.

The full reverse-engineering notes (file layout, sequence bytecode, the game's BGM
tables and addresses) are in
[nsmbud-reverse/reverse-engineering/FindingsI/AUDIO.md](https://github.com/Steammaker212/nsmbud-reverse/blob/main/reverse-engineering/FindingsI/AUDIO.md).

## Setup

Python 3.10 or newer, and numpy.

```
pip install -r requirements.txt
```

Run everything from this folder as `python -m nsmbudx_music ...`.

## Usage

### List the music

```
python -m nsmbudx_music Sound.bfsar list
python -m nsmbudx_music Sound.bfsar list CHIJOU
```

```
label            kind course layer        wavearc-file wave ch  rate  loop    seconds mixvol
STRM_BGM_CHIJOU  SEQ  1      track1_prg0           353    0  2 48000 209454     45.8    127
STRM_BGM_CHIJOU  SEQ  1      track2_prg1           353    1  2 48000 209454     45.8    110
STRM_BGM_CHIJOU  SEQ  1      track3_prg2           353    2  2 48000 209454     45.8     80
```

`course` is the music number a level's area uses (`1F` = its hurry-up version).
`loop` is the loop start in samples. `mixvol` is the volume the game plays that layer at.

### Extract

```
python -m nsmbudx_music Sound.bfsar extract out/ CHIJOU
python -m nsmbudx_music Sound.bfsar extract out/ CHIJOU --wav
```

Writes one `.bfwav` per layer (`STRM_BGM_CHIJOU.track1_prg0.bfwav`). `--wav` also
writes a 16-bit WAV with the loop point in a `smpl` chunk. Decoding is pure Python,
so expect a few seconds per minute of audio.

The filter is a regular expression, so `"^STRM_BGM_TITLE$"` picks exactly one sound.

### Replace a song

```
python -m nsmbudx_music Sound.bfsar inject STRM_BGM_CHIJOU track1 mysong.wav --silence-others -o Sound.new.bfsar
python -m nsmbudx_music Sound.new.bfsar inject STRM_BGM_CHIJOU_FAST track1 mysong_fast.wav --silence-others -o Sound.new2.bfsar
```

- Input is a 16-bit PCM WAV (mono or stereo, any sample rate; 48000 Hz like the
  game is best) or a little-endian BFWAV.
- The loop start comes from the WAV's `smpl` chunk, as written by most audio
  editors. Override it with `--loop-start SAMPLES`, or use `--no-loop` for jingles.
- `--silence-others` replaces the sound's other layers with a short silent wave.
  Use it when your song is one finished mix.
- `all` instead of `track1` puts the same file in every layer.
- The output is a complete `Sound.bfsar`. Only the changed wave archive differs from the input.

### Install

Put the new archive at `romfs/Sound/Sound.bfsar` in a mod:

- Atmosphère: `atmosphere/contents/0100EA80032EA000/romfs/Sound/Sound.bfsar`
- Ryujinx: `mods/contents/0100ea80032ea000/<mod name>/romfs/Sound/Sound.bfsar`
- Yuzu-likes: `load/0100EA80032EA000/<mod name>/romfs/Sound/Sound.bfsar`

### Dump every table

```
python -m nsmbudx_music Sound.bfsar dump tables/
```

Writes `sounds.csv`, `files.csv`, `banks.csv`, `wavearcs.csv`, `groups.csv`,
`players.csv` and `soundsets.csv`.

## Course music numbers

| # | label | # | label | # | label |
|---|---|---|---|---|---|
| 1 | CHIJOU (overworld) | 11 | HIKOUSEN (airship) | 21 | KAZAN_TIKA |
| 2 | CHIKA (underground) | 12 | BONUS | 22 | SHADOW |
| 3 | WATER | 13 | ROAD_TO_LAST | 23 | STAFF_CREDIT |
| 4 | ATHLETIC | 14 | TORIDE_BOSS | 24 | HIKOUSEN_ROUKA |
| 5 | OBAKE (ghost house) | 15 | SHIRO_BOSS | 25 | LAST_BOSS |
| 6 | SABAKU (desert) | 16 | MINIGAME | 26 | LAST_BOSS2 |
| 7 | YUKI (snow) | 17 | HIKOUSEN_BOSS | 27 | LAST_BOSS2_HARP |
| 8 | KAZAN (volcano) | 18 | MORI (forest) | 28 | SHIRO_LAST |
| 9 | TORIDE (tower) | 19 | MG_BTL | | |
| 10 | SHIRO (castle) | 20 | SANBASHI (pier) | | |

## Limitations

- **Not yet tested in game.** Output archives are checked structurally: they
  re-parse, decode, keep valid loop data, and leave every other file byte-identical.
- The beat map keeps the original song's tempo, so enemies hop to the old beat.
- Course music is loaded into memory rather than streamed. Very long songs may not fit
  the game's sound heap; the original layers are 40-90 s each.
- Wii U (big-endian) BFWAVs are refused. Convert them first, or give a WAV.

## Tests

```
pip install pytest
python -m pytest tests
```

The codec tests need nothing. The archive tests run when `NSMBUDX_BFSAR` points
at a v1.0.0 `Sound.bfsar`.

## Credits

AboodXD's [NSMBU-Music-extractor-injector](https://github.com/aboood40091/NSMBU-Music-extractor-injector/tree/deluxe)
and [BCFSTM-BCFWAV-Converter](https://github.com/aboood40091/BCFSTM-BCFWAV-Converter)
were the starting point for understanding the Deluxe archive.
