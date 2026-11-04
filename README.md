# nsmbudx-music

List, extract, replace and **add** music in *New Super Mario Bros. U Deluxe* (Switch,
v1.0.0), from the command line or a small editor window. Works directly on the game's
`romfs/Sound/Sound.bfsar`: no hard-coded offsets, and a new song can be any length.

Added music plays in levels through the Custom Music sprite (785) of
[nsmbud-reverse](https://github.com/Steammaker212/nsmbud-reverse), with rules such as
"only while time is not running low" or "not while riding Yoshi".

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

## The editor

```
python -m nsmbudx_music.gui
python -m nsmbudx_music.gui Sound.bfsar
```

Open `Sound.bfsar`, then pick a sound (or one of its layers) in the list:

- **Play** decodes it and opens it in your default player. Double-clicking does the same.
- **Export WAV / Export BFWAV** saves it.
- **Replace** swaps in a WAV or BFWAV. For a song with several layers, it can silence the others.
- **Add music** adds a new song in a slot (`CUSTOM_BGM_00` to `CUSTOM_BGM_99`), or its
  hurry-up version (`CUSTOM_BGM_nn_FAST`).
- **Remove** takes an added song out again. Added songs are shown in blue.
- **Save As** writes the archive. Nothing is written before that.

Changes are made in memory, and each takes a few seconds (encoding a long WAV takes longer).

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

- Input is a WAV (8/16/24/32-bit or float, mono or stereo, any sample rate; 48000 or
  44100 Hz like the game's own) or a little-endian BFWAV.
- The loop start comes from the WAV's `smpl` chunk, as written by most audio
  editors. Override it with `--loop-start SAMPLES`, or use `--no-loop` for jingles.
- `--silence-others` replaces the sound's other layers with a short silent wave.
  Use it when your song is one finished mix.
- `all` instead of `track1` puts the same file in every layer.
- The output is a complete `Sound.bfsar`. Only the changed wave archive differs from the input.

### Add new music

```
python -m nsmbudx_music Sound.bfsar add 0 mysong.wav -o Sound.new.bfsar
python -m nsmbudx_music Sound.new.bfsar add 0 --fast mysong_fast.wav -o Sound.new2.bfsar
python -m nsmbudx_music Sound.new2.bfsar remove 0 -o Sound.new3.bfsar
```

`add 0` stores the song as `CUSTOM_BGM_00`, the label the Custom Music sprite plays for
slot 0. `--fast` adds the version for when time runs low. Adding a slot again replaces
it. Options:

- `--bpm N` sets the tempo the enemies hop to. By default it is estimated from the song;
  set it when the estimate is off (it may find half or double the real tempo).
- `--loop-start`, `--no-loop` and the input formats work as in `inject`.
- `--volume` is 0-255; the default (90) matches the title music.

The game loads the whole archive into its sound memory. The nsmbud-reverse code makes
that memory bigger by what you add, taken from the game's other memory, so keep added
music to a few minutes in total. A 98-second stereo song adds about 5 MB.

### Use it in a level

With the nsmbud-reverse mod installed, place a **Custom Music** sprite (785) anywhere in an area
(Yaz0moto, the steam-dx patch, "Level Controllers"):

| Setting | |
|---|---|
| Slot | 0-99: plays `CUSTOM_BGM_nn` |
| Keep playing when time runs low | plays `CUSTOM_BGM_nn_FAST` if you added one, else the same song. Off: the area's own hurry-up music |
| Keep playing while riding Yoshi | off: the area's own music, with Yoshi's drums |
| Keep playing while carrying a Baby Yoshi | off: the area's own music, with the Baby Yoshi chorus |
| Event | plays only while this event is on (0 = always) |

When the song should stop (Yoshi, time, event), the area's own music fades in, and back.

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

- Replacing music works in game. Added music is tested to boot (Ryujinx) with a
  98-second song; playing it in a level has not been checked yet.
- A replaced course song keeps the original's beat map, so enemies hop to the old beat.
  Added songs use their BPM.
- Course music is loaded into memory rather than streamed. Very long songs may not fit
  the game's sound heap; the original layers are 40-90 s each.
- Wii U (big-endian) BFWAVs are refused. Convert them first, or give a WAV.

## Tests

```
pip install pytest
python -m pytest tests
```

The codec, WAV and label-tree tests need nothing. The archive tests run when
`NSMBUDX_BFSAR` points at a v1.0.0 `Sound.bfsar`.

## Credits

AboodXD's [NSMBU-Music-extractor-injector](https://github.com/aboood40091/NSMBU-Music-extractor-injector/tree/deluxe)
and [BCFSTM-BCFWAV-Converter](https://github.com/aboood40091/BCFSTM-BCFWAV-Converter)
were the starting point for understanding the Deluxe archive.
