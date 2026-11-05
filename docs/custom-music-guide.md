# Making custom music for NSMBU Deluxe

This guide covers getting a song from your audio editor into a level: preparing the
file, setting the loop, getting the enemies to dance on the beat, and what the tool
builds in the archive. The [README](../README.md) has the command reference.

## 1. What you need

- Your own dump of the game's `romfs/Sound/Sound.bfsar` (v1.0.0, 354,262,944 bytes).
- nsmbudx-music: the `nsmbudx-music.exe` editor from the releases page, or the Python
  package.
- The [nsmbud-reverse](https://github.com/Steammaker212/nsmbud-reverse) mod. It plays
  added music through the Custom Music sprite (785) and makes the game's sound memory
  big enough for it.
- A level editor with the steam-dx Yaz0moto patch, to place the sprite.
- An audio editor that can show sample positions (Audacity, ocenaudio, Wavosaur, ...).

## 2. Preparing the audio

| | Use | Why |
|---|---|---|
| Format | WAV, 16-bit PCM | 8/24/32-bit and float are accepted and converted to 16-bit |
| Channels | stereo | mono works; more than two channels is not supported |
| Sample rate | 48000 or 44100 Hz | the game's own music uses these; other rates play, but cost the same memory for less quality |
| Loudness | peaks around -1 dBFS, similar loudness to the game's songs | the in-game mix assumes it; `--volume` (0-255, default 80) scales it further |
| Length | a minute or two | see the memory budget below |

The tool encodes to Nintendo DSP-ADPCM itself. You never need a BFWAV converter.

**Memory budget.** Added music is loaded at boot and stays in the sound memory for the
whole game. DSP-ADPCM is about 4.5 bits per sample: one minute of 48 kHz stereo is
about 3.3 MB. The mod grows the sound memory by what you add, taking it from the game's
other memory, so keep all added music together to a few minutes.

## 3. Loops

Course music loops forever. A song is an optional intro, then a loop body that repeats:

```
|---- intro ----|============ loop body ============|
0          loop start                        end of file
```

- **The loop end is the end of the file.** Cut the file exactly where the loop body
  ends, so that the last sample flows into the loop start.
- **The loop start** is a sample number. The tool takes it from, in this order:
  1. `--loop-start SAMPLES` (CLI) or the "Loop start" field (editor);
  2. the WAV's `smpl` chunk, which Wavosaur, Loop Auditioner, Sound Forge, FL Studio's
     Edison and similar tools write when you set a loop;
  3. otherwise the whole song loops from sample 0.
- `--no-loop` (or "Play once") is for jingles.

To find the loop start in Audacity: set "Selection" to show samples, click where the
loop body starts, and read the number.

Make the seam clean:

- The loop body should start and end on the same musical beat, at the same tempo.
- Cut on zero crossings, or let a reverb tail from the end of the body also be present
  at the start of the body, so the jump does not click.
- Listen to the seam: play the last second of the file, then the second from the loop
  start, back to back.

DSP-ADPCM loops must start on a 14-sample frame. When yours does not, the tool pads
the beginning of the song with up to 13 samples of silence (under 0.3 ms), which moves
the loop onto a frame without changing what you hear.

## 4. Getting the enemies to dance on the beat

Goombas, Koopas and other enemies hop to the course music. The game does not listen to
the audio. Each course song has a silent beat track next to the music, and the enemies
react to what that track writes:

- a **beat** every quarter note, counted from the track's tempo;
- a **chorus** value on the bar pair's downbeat and on beats 5 and 7;
- a **"bah"**: the hop. The game's own songs place it by hand where the band shouts
  "bah". An added song gets one on beat 3 of every fourth bar.

The tool writes this track from two numbers:

| | CLI | Editor | Default |
|---|---|---|---|
| Tempo | `--bpm N` | BPM | estimated from the song (90-180) |
| First beat | `--offset SECONDS` | First beat (seconds) | estimated from the song |

The beat track starts at the first beat and repeats every four bars. When the song
loops, the beat track jumps back to the same place as the audio, so it stays in step
however long the level lasts.

**When the hops are off:**

- Hops drift further and further from the music: the BPM is wrong. Find it in your DAW,
  or tap along with an online BPM counter, and set it with `--bpm`.
- Hops come at twice or half the rate you expect: the estimate found double or half the
  tempo. Set `--bpm` to the one that feels right.
- Hops keep a steady rhythm but land off the beat: the first beat is wrong. In your audio
  editor, find the time of the first downbeat (the first kick or bass note of the first
  full bar) and set `--offset`.
- The tempo must be a whole number. A song at 127.5 BPM is best time-stretched to 128
  (or 127) before adding it; a small error is pulled back into step at every loop.
- The tempo should not change during the song.

### Placing the bahs yourself

By default the hop comes on beat 3 of every fourth bar. To make the enemies hop where
your song actually has its accents (a shout, a drum hit, the end of a phrase), give a
**bah times file**: one hop per line, the time in seconds from the start of the WAV,
then optionally the kind of hop (1-4, default 3).

```
# bahs.txt: seconds  kind
5.43
12.10  3
19.86  2
26.20  1
26.50  4
```

With a file, only these hops are used (the every-fourth-bar ones are dropped); the
steady beat keeps coming from the BPM and first beat.

The easiest way to make one is in Audacity:

1. Open the WAV and play it.
2. Press **Ctrl+M** (Add Label at Playback Position) at each spot the enemies should hop.
   Type a kind (1-4) as the label text if you like, or leave it empty for 3.
3. **File > Export > Export Labels...** and save the `.txt`.

Then add the song with it:

```
nsmbudx-music Sound.bfsar add 0 mysong.wav --bahs bahs.txt -o Sound.new.bfsar
```

In the editor, choose the file in **Bah times file**.

- Times are in the song as you hear it. Hops in the intro (before the loop start) happen
  once; hops in the loop body happen every time round.
- The kinds are the values the game's own songs use. The overworld uses 3 for a single
  "bah", and 1 followed by 4 three-quarters of a beat later for "bah-bah"; the
  underground uses 2. Try them in game to see which looks right.
- Times snap to the beat track's grid (1/96 of a beat, about 5 ms at 120 BPM).
- The hops are stored in the archive with the song, so adding or removing other songs
  later keeps them. Adding the same slot again builds the song anew: pass the file again.

The tool prints what it used:

```
CUSTOM_BGM_00: YI-Flower.wav, 120 BPM, first beat 0.175 s -> Sound.new.bfsar
```

## 5. The hurry-up version

When the timer drops under 100, the game switches to its `_FAST` music. For an added
song, add a faster version in the same slot with `--fast` (`CUSTOM_BGM_nn_FAST`) and tick
"Keep playing when time runs low" on the sprite. Speed the song up in your editor
without changing its pitch (Audacity: Effect > Change Tempo). The overworld goes from
110 to 132 BPM, 1.2 times as fast. Give the fast version its own BPM: 120 becomes 144.

Without a `_FAST` version, the sprite either keeps the normal song or hands over to
the area's own hurry-up music, whichever you choose.

## 6. Adding the song

Editor: **Open** your `Sound.bfsar`, **Add music**, choose the slot, the WAV and the
loop and beat settings, then **Save As**. Never overwrite the original dump.

CLI:

```
nsmbudx-music Sound.bfsar add 0 mysong.wav -o Sound.new.bfsar
nsmbudx-music Sound.new.bfsar add 0 --fast mysong_fast.wav -o Sound.new2.bfsar
```

(With the Python package, use `python -m nsmbudx_music` in place of `nsmbudx-music`.)

Adding a slot again replaces it, and `remove` takes it out. The archive always keeps
the game's own sounds byte for byte.

## 7. Using it in a level

1. Install the archive: `romfs/Sound/Sound.bfsar` in your mod
   (Atmosphère: `atmosphere/contents/0100EA80032EA000/romfs/Sound/Sound.bfsar`).
2. Install the nsmbud-reverse mod (its `subsdk0` and exefs patch).
3. In the level editor, place a **Custom Music** sprite (785, "Level Controllers") in
   each area that should play the song. Set the slot, and which of time running low,
   Yoshi, Baby Yoshi and an event should hand back to the area's own music.

The song replaces the area's music from the moment the level starts. When a rule
says it should stop, the area's own music fades in, and back again later.

## 8. What the tool builds

Each added song is built the way the game builds its own course music:

| Item | Name | What it is |
|---|---|---|
| Sound | `CUSTOM_BGM_nn` | a sequence sound, cloned from `STRM_BGM_CHIJOU`. Its user parameter is the BPM, which the game reads for the beat. |
| Sequence file (FSEQ) | | track 0: the silent beat track (tempo, then `setvar` 0/1 events and waits, then a jump back to the loop point). Track 1: program 0, note 60, held forever. |
| Bank | `BANK_CUSTOM_BGM_nn` | a copy of the overworld bank whose instruments all point at the new wave, at its original pitch |
| Wave archive (FWAR) | | one wave |
| Wave (FWAV) | | the song, DSP-ADPCM, with its loop |
| GROUP_BOOT | | lists the three new files, the sound and the bank, so the game loads them at boot |

The game only finds a file once a group it has loaded lists it. Course groups are
loaded and dropped per level, but GROUP_BOOT is loaded at boot and never dropped, so
added music can play in any level.

The archive format, the sequence bytecode and the game's music code are documented
in [nsmbud-reverse's AUDIO.md](https://github.com/Steammaker212/nsmbud-reverse/blob/main/reverse-engineering/FindingsI/AUDIO.md).

## 9. Troubleshooting

| Symptom | Cause |
|---|---|
| The area's own music plays | no Custom Music sprite in that area, a wrong slot, or a rule (time, Yoshi, event) is handing back |
| Silence | the archive or the mod is missing; the log (Ryujinx: `Logs/`) says `custom music: no CUSTOM_BGM_nn in Sound.bfsar` when the slot is empty |
| The game does not boot | the archive is not built from v1.0.0, or too much music was added for memory |
| A click at the loop | the loop start and the end of the file do not join cleanly (see Loops) |
| Enemies off the beat | BPM or first beat (see section 4) |
