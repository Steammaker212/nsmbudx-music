# nsmbudx-music

A sound archive studio for *New Super Mario Bros. U Deluxe* (Switch, v1.0.0): browse,
play and edit everything in the game's `romfs/Sound/Sound.bfsar`, and **add** your own
course music, with a Yoshi layer and enemies that dance to your beat. Works directly
on the archive: no hard-coded offsets, and a new song can be any length.

Added music plays in levels through the Custom Music sprite (785) of
[nsmbud-reverse](https://github.com/Steammaker212/nsmbud-reverse), with rules such as
"only while time is not running low" or "not while riding Yoshi".

No game files are included. You need your own dump of the game's romfs.

**New to this?** Read the [custom music guide](docs/custom-music-guide.md): preparing
the audio, loops, getting the enemies to dance on the beat, and using it in a level.

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

## Download

The [releases page](https://github.com/Steammaker212/nsmbudx-music/releases) has
`nsmbudx-music.exe` (the editor window) and `nsmbudx-music-cli.exe` (the commands
below) for Windows. Nothing to install: the commands below work the same with
`nsmbudx-music-cli.exe` in place of `python -m nsmbudx_music`.

## The Studio

`nsmbudx-music.exe` (or `python -m nsmbudx_music.studio [Sound.bfsar]`) opens the
Studio in its own window (Microsoft Edge's app mode; `--browser` uses your default
browser instead). Everything runs on your PC; the page talks only to a local server
on 127.0.0.1.

- **Browse** every sound, sound set, bank, wave archive, group, player and file, with
  search. Course music is named by course (Overworld, Underground, ...).
- **Play** any sequence or wave sound, rendered the way the game builds it: the
  sequence runs tick by tick through its banks and waves, with the game's envelopes.
  The player shows the loop, the tracks (mute or solo each stem, or pick a game state:
  normal or riding Yoshi), the notes, and the sequence variables as they change, with
  the bahs and beat marked and a little critter dancing along.
- **States** (course music, the game's and yours): what plays normally, what the game
  adds while Mario rides Yoshi, the third layer, and the hurry-up version. Play, replace,
  add or remove each one; an added song gets a Yoshi layer or a hurry version from here.
- **Beat, bahs and loop** (course music): add, move the kind of, and remove bahs and beat
  accents on the waveform (or press B / N along with the music), move the loop start and
  end, or make it play once. Apply rewrites the beat track and moves the layers' wave
  loops with it; works on the game's songs too.
- **Properties**: every field of every item (volume, player, priorities, pan, user
  params, 3D, banks, track flags, start offset, load flags, player limits, ...).
- **Sequence editor**: the sequence as text, following each track while it plays.
  Edit it and compile it back; sounds that start at a label follow it.
- **Bank editor**: instruments, key and velocity regions (wave, original key, volume,
  pan, pitch, attack, decay, sustain, hold, release), with a piano to play them
  (mouse, or the A-K keys).
- **Waves**: play, export to WAV and replace any wave of a wave archive.
- **Groups**: what each group loads, and its load flags.
- **Hex** view of any file.
- **Add music**: pick a WAV, then on its waveform set the loop start, the tempo (tap it
  with T) and first beat, and place the bahs by clicking or by pressing B while it
  plays. Preview it with beat clicks and bah sounds, hear the loop seam, add an
  optional Yoshi layer, and add it as `CUSTOM_BGM_nn` (or its `_FAST` version).

Nothing is written until you press **Save** or **Save as**.

## Setup (from source)

Python 3.10 or newer, and numpy.

```
pip install -r requirements.txt
```

Run everything from this folder as `python -m nsmbudx_music ...`.

## The old editor

The first, simpler editor window is still there (`python -m nsmbudx_music.gui`).
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

- `--bpm N` and `--offset SECONDS` set the tempo and the first beat the enemies dance to.
  By default both are estimated from the song; set them when the hops are off (see the
  [guide](docs/custom-music-guide.md#4-getting-the-enemies-to-dance-on-the-beat)).
- `--bahs FILE` places the enemies' hops at the times in FILE (`seconds [kind]` per line,
  or an Audacity label export) instead of every fourth bar
  ([how](docs/custom-music-guide.md#placing-the-bahs-yourself)).
- `--loop-start`, `--no-loop` and the input formats work as in `inject`.
- `--volume` is 0-255; the default (80) matches the overworld music.

- `--yoshi FILE` adds a Yoshi layer: a second WAV of the same length and rate that the
  game plays on top of the song while Mario rides Yoshi (the game's own songs add bongos).

An added song is a sequence sound like the game's course music: a silent beat track
for the enemies, the song on track 1 and the Yoshi layer on track 2, played through its
own bank. It is listed in GROUP_BOOT,
so the game loads it at boot and keeps it in its sound memory. The nsmbud-reverse code
makes that memory bigger by what you add, taken from the game's other memory, so keep
added music to a few minutes in total. A 98-second stereo song adds about 5 MB.

### Use it in a level

With the nsmbud-reverse mod installed, place a **Custom Music** sprite (785) anywhere in an area
(Yaz0moto, the steam-dx patch, "Level Controllers"):

| Setting | |
|---|---|
| Slot | 0-99: plays `CUSTOM_BGM_nn` (the Studio lists each song's slot) |
| Keep playing when time runs low | keeps the same song. Not needed when you added `CUSTOM_BGM_nn_FAST`: the sprite switches to it by itself. Off, without one: the area's own hurry-up music |
| Keep playing while riding Yoshi | not needed for a song with a Yoshi layer: it always stays, and the game turns the layer on. Off, without a layer: the area's own music, with Yoshi's drums |
| Keep playing while carrying a Baby Yoshi | off: the area's own music, with the Baby Yoshi chorus |
| Event | plays only while this event is on (0 = always) |

When the song should stop (Yoshi, time, event), the area's own music fades in, and back.
The enemies dance to the song's own beat and bahs. The sprite knows what the song was
made with from flags the tool stores with it (above the BPM in user param 0).

### Install

Put the new archive at `romfs/Sound/Sound.bfsar` in a mod:

- Atmosphère: `atmosphere/contents/0100EA80032EA000/romfs/Sound/Sound.bfsar`
- Ryujinx: `mods/contents/0100ea80032ea000/<mod name>/romfs/Sound/Sound.bfsar`
- Yuzu-likes: `load/0100EA80032EA000/<mod name>/romfs/Sound/Sound.bfsar`

### Play, and edit sequences as text

```
python -m nsmbudx_music Sound.bfsar render STRM_BGM_CHIJOU chijou.wav --mute 2,3
python -m nsmbudx_music Sound.bfsar seq STRM_BGM_CHIJOU chijou.txt
python -m nsmbudx_music Sound.bfsar seqin 65 chijou.txt -o Sound.new.bfsar
```

`render` plays a sequence or wave sound into a WAV as the game would (here the
overworld without its Yoshi and third layers). `seq` writes a sequence file as text and
`seqin` assembles it back; every sequence in the game rebuilds byte for byte.

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

- Replacing and adding music are tested in game (Ryujinx) with a 98-second song.
- A replaced course song keeps the original's beat map, so enemies hop to the old beat.
  Added songs use their BPM and first beat.
- Without `--bahs`, an added song's hop falls on beat 3 of every fourth bar, not where
  the song itself would shout.
- Course music is loaded into memory rather than streamed. Very long songs may not fit
  the game's sound heap; the original layers are 40-90 s each.
- Playback in the Studio leaves out vibrato, portamento, filters and effects, and
  applies volume, pan and pitch-bend changes at the next note. Some sound effects are
  silent there because the game sets their variables while it plays them.
- Sequence and bank editing changes values in place; it does not add or remove
  instruments or regions.
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
