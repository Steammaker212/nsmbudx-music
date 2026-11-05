"""Sound archive editor: browse, play, export, replace and add music in Sound.bfsar.

    python -m nsmbudx_music.gui [Sound.bfsar]

Edits are made in memory; nothing is written until Save As.
"""
import os
import queue
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import edit, formats as F, fwav
from .audio import estimate_beat, silent_wave, wav_to_fwav, write_wav
from .cli import COURSE_BGM, COURSE_NAMES, Music, course_id

TITLE = "nsmbudx-music"


class AddDialog(simpledialog.Dialog):
    """Asks for the slot, the WAV and the options of new music."""

    def __init__(self, parent, used):
        self.used = used
        self.result = None
        super().__init__(parent, "Add custom music")

    def body(self, f):
        free = next((n for n in range(100) if edit.custom_label(n) not in self.used), 0)
        self.slot = tk.IntVar(value=free)
        self.fast = tk.BooleanVar()
        self.path = tk.StringVar()
        self.bpm = tk.StringVar(value="auto")
        self.offset = tk.StringVar(value="auto")
        self.bahs = tk.StringVar()
        self.loop = tk.StringVar()
        self.noloop = tk.BooleanVar()
        rows = [
            ("Slot (0-99)", ttk.Spinbox(f, from_=0, to=99, textvariable=self.slot, width=6)),
            ("", ttk.Checkbutton(f, text="Hurry-up version (CUSTOM_BGM_nn_FAST)", variable=self.fast)),
            ("WAV file", ttk.Entry(f, textvariable=self.path, width=48)),
            ("BPM", ttk.Entry(f, textvariable=self.bpm, width=8)),
            ("First beat (seconds)", ttk.Entry(f, textvariable=self.offset, width=8)),
            ("Bah times file", ttk.Entry(f, textvariable=self.bahs, width=48)),
            ("Loop start (samples)", ttk.Entry(f, textvariable=self.loop, width=12)),
            ("", ttk.Checkbutton(f, text="Play once (no loop)", variable=self.noloop)),
        ]
        for r, (label, w) in enumerate(rows):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", padx=4, pady=3)
            w.grid(row=r, column=1, sticky="w", padx=4, pady=3)
        ttk.Button(f, text="Browse...", command=self.browse).grid(row=2, column=2, padx=4)
        ttk.Button(f, text="Browse...", command=self.browse_bahs).grid(row=5, column=2, padx=4)
        ttk.Label(f, foreground="gray", wraplength=420, justify="left", text=(
            "BPM and the first beat drive the enemies' dance; 'auto' estimates them. The bah times "
            "file places their hops (seconds [kind] per line, or Audacity labels); empty: every "
            "fourth bar. Leave the loop start empty to use the WAV's own loop, or loop the whole "
            "song.")).grid(row=len(rows), column=0, columnspan=3,
                                                                  sticky="w", padx=4, pady=6)
        return rows[2][1]

    def browse(self):
        p = filedialog.askopenfilename(parent=self, filetypes=[("WAV", "*.wav"), ("All files", "*.*")])
        if p:
            self.path.set(p)

    def browse_bahs(self):
        p = filedialog.askopenfilename(parent=self, filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if p:
            self.bahs.set(p)

    def validate(self):
        try:
            slot = int(self.slot.get())
            assert 0 <= slot <= 99
        except Exception:
            messagebox.showerror(TITLE, "The slot is a number from 0 to 99.", parent=self)
            return False
        if not os.path.isfile(self.path.get()):
            messagebox.showerror(TITLE, "Choose a WAV file.", parent=self)
            return False
        bpm, loop = self.bpm.get().strip(), self.loop.get().strip()
        if bpm not in ("", "auto") and not bpm.isdigit():
            messagebox.showerror(TITLE, "BPM is a number, or 'auto'.", parent=self)
            return False
        offset = self.offset.get().strip()
        try:
            offset = None if offset in ("", "auto") else float(offset)
        except ValueError:
            messagebox.showerror(TITLE, "The first beat is a time in seconds, or 'auto'.", parent=self)
            return False
        if loop and not loop.isdigit():
            messagebox.showerror(TITLE, "The loop start is a sample number.", parent=self)
            return False
        bahs = None
        if self.bahs.get().strip():
            try:
                with open(self.bahs.get().strip(), encoding="utf-8-sig") as fh:
                    bahs = edit.parse_bahs(fh.read())
                assert all(1 <= k <= 4 for _, k in bahs)
            except Exception:
                messagebox.showerror(TITLE, "The bah times file needs one 'seconds [kind 1-4]' per line.",
                                     parent=self)
                return False
        self.result = {
            "label": edit.custom_label(slot, self.fast.get()),
            "path": self.path.get(),
            "bpm": int(bpm) if bpm.isdigit() else None,
            "offset": offset,
            "bahs": bahs,
            "loop_start": int(loop) if loop else None,
            "loop": not self.noloop.get(),
        }
        return True


class App(tk.Tk):
    COLUMNS = ("kind", "course", "layer", "rate", "ch", "length", "loop", "bpm")

    def __init__(self, path=None):
        super().__init__()
        self.title(TITLE)
        self.geometry("1040x620")
        self.music = None
        self.path = None
        self.dirty = False
        self.rows = {}
        self.jobs = queue.Queue()
        self.busy = False
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.after(100, self._poll)
        if path:
            self.open(path)

    # --- layout ------------------------------------------------------------
    def _build(self):
        bar = ttk.Frame(self, padding=4)
        bar.pack(fill="x")
        for text, cmd in [("Open...", self.ask_open), ("Save As...", self.save_as), (None, None),
                          ("Play", self.play), ("Export WAV...", self.export_wav),
                          ("Export BFWAV...", self.export_bfwav), (None, None),
                          ("Replace...", self.replace), ("Add music...", self.add),
                          ("Remove", self.remove)]:
            if text is None:
                ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=6)
            else:
                ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=2)
        ttk.Label(bar, text="Filter").pack(side="left", padx=(16, 4))
        self.filter = tk.StringVar()
        self.filter.trace_add("write", lambda *_: self.refresh())
        ttk.Entry(bar, textvariable=self.filter, width=24).pack(side="left")
        self.only_music = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Music only", variable=self.only_music,
                        command=self.refresh).pack(side="left", padx=8)

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(body, columns=self.COLUMNS, selectmode="browse")
        self.tree.heading("#0", text="Sound")
        self.tree.column("#0", width=290)
        widths = {"kind": 50, "course": 150, "layer": 110, "rate": 60, "ch": 30, "length": 60,
                  "loop": 80, "bpm": 50}
        for c in self.COLUMNS:
            self.tree.heading(c, text=c.capitalize())
            self.tree.column(c, width=widths[c], anchor="w")
        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self.play())
        self.tree.tag_configure("custom", foreground="#1060c0")

        self.status = tk.StringVar(value="Open romfs/Sound/Sound.bfsar to start.")
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w", padding=3).pack(fill="x")

    # --- background work ---------------------------------------------------
    def run(self, label, work, done=None):
        """Run work() off the UI thread; done(result) runs back on it."""
        if self.busy:
            messagebox.showinfo(TITLE, "Still working on the last task.")
            return
        self.busy = True
        self.status.set(label + "...")
        self.config(cursor="watch")

        def worker():
            try:
                self.jobs.put((done, work(), None))
            except Exception as e:
                self.jobs.put((None, None, e))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                done, result, err = self.jobs.get_nowait()
                self.busy = False
                self.config(cursor="")
                if err:
                    self.status.set(f"Failed: {err}")
                    messagebox.showerror(TITLE, str(err))
                elif done:
                    done(result)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # --- archive -----------------------------------------------------------
    def ask_open(self):
        if not self.confirm_discard():
            return
        p = filedialog.askopenfilename(filetypes=[("Sound archive", "*.bfsar"), ("All files", "*.*")])
        if p:
            self.open(p)

    def open(self, path):
        def done(music):
            self.path, self.dirty = path, False
            self.load(music)
        self.run(f"Reading {os.path.basename(path)}", lambda: Music(path), done)

    def load(self, music):
        self.music = music
        self.custom = {m[0] for m in edit.custom_music(music.bf)}
        self.title(f"{TITLE} - {os.path.basename(self.path or '')}{' *' if self.dirty else ''}")
        self.refresh()
        bf = music.bf
        self.status.set(f"{len(bf.sounds)} sounds, {len(self.custom)} added, "
                        f"{len(bf.data) / 1048576:.1f} MiB")

    def apply(self, label, make):
        """make() returns new archive bytes; they replace the open archive (unsaved)."""
        def done(music):
            self.dirty = True
            self.load(music)
            self.status.set(label + " done. " + self.status.get())
        self.run(label, lambda: Music(make()), done)

    def save_as(self):
        if not self.music:
            return
        p = filedialog.asksaveasfilename(defaultextension=".bfsar", initialfile="Sound.bfsar",
                                         filetypes=[("Sound archive", "*.bfsar")])
        if not p:
            return
        data = self.music.bf.data

        def work():
            with open(p, "wb") as f:
                f.write(data)

        def done(_):
            self.path, self.dirty = p, False
            self.title(f"{TITLE} - {os.path.basename(p)}")
            self.status.set(f"Saved {p}")
        self.run("Saving", work, done)

    def confirm_discard(self):
        return not self.dirty or messagebox.askyesno(TITLE, "Discard the unsaved changes?")

    def quit_app(self):
        if self.confirm_discard():
            self.destroy()

    # --- list --------------------------------------------------------------
    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        if not self.music:
            return
        text = self.filter.get().lower()
        sounds = self.music.music_sounds() if self.only_music.get() else (
            s for s in self.music.bf.sounds if s.kind in ("SEQ", "WSD"))
        for s in sounds:
            if text and text not in s.label.lower():
                continue
            cid, fast = course_id(s.label)
            course = f"{cid} {COURSE_NAMES.get(COURSE_BGM[cid], COURSE_BGM[cid])}{' (hurry)' if fast else ''}" if cid else ""
            tags = ("custom",) if s.label in self.custom else ()
            try:
                layers = self.music.layers(s)
            except Exception:
                layers = []
            parent = self.tree.insert("", "end", text=s.label, open=False, tags=tags,
                                      values=(s.kind, course, f"{len(layers)} layer(s)", "", "", "", "",
                                              s.options.get(31, "")))
            self.rows[parent] = (s, None)
            for layer in layers:
                fid, waves = self.music.war(layer[1])
                info = F.fwav_info(waves[layer[2]])
                iid = self.tree.insert(parent, "end", text="", tags=tags, values=(
                    "", "", layer[0], info["rate"], info["channels"],
                    f"{info['samples'] / info['rate']:.1f}s",
                    info["loop_start"] if info["loop"] else "-", ""))
                self.rows[iid] = (s, layer)

    def selected(self, want_layer=True):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(TITLE, "Select a sound first.")
            return None, None
        s, layer = self.rows[sel[0]]
        if want_layer and layer is None:
            layers = self.music.layers(s)
            if len(layers) != 1:
                messagebox.showinfo(TITLE, f"{s.label} has {len(layers)} layers: expand it and pick one.")
                return None, None
            layer = layers[0]
        return s, layer

    def wave_bytes(self, layer):
        fid, waves = self.music.war(layer[1])
        return waves[layer[2]]

    # --- actions -----------------------------------------------------------
    def play(self):
        s, layer = self.selected()
        if not s:
            return
        blob = self.wave_bytes(layer)
        out = os.path.join(tempfile.gettempdir(), f"{s.label}.{layer[0]}.wav")

        def work():
            w = fwav.Wave.parse(blob)
            write_wav(out, w.pcm(), w.rate)
            return out
        self.run(f"Decoding {s.label}", work, lambda p: (os.startfile(p) if sys.platform == "win32"
                                                          else None, self.status.set(f"Playing {p}")))

    def export_wav(self):
        s, layer = self.selected()
        if not s:
            return
        p = filedialog.asksaveasfilename(defaultextension=".wav", initialfile=f"{s.label}.{layer[0]}.wav",
                                         filetypes=[("WAV", "*.wav")])
        if not p:
            return
        blob = self.wave_bytes(layer)

        def work():
            w = fwav.Wave.parse(blob)
            write_wav(p, w.pcm(), w.rate, w.loop_start if w.loop else None, w.samples)
        self.run(f"Exporting {s.label}", work, lambda _: self.status.set(f"Wrote {p}"))

    def export_bfwav(self):
        s, layer = self.selected()
        if not s:
            return
        p = filedialog.asksaveasfilename(defaultextension=".bfwav", initialfile=f"{s.label}.{layer[0]}.bfwav",
                                         filetypes=[("BFWAV", "*.bfwav")])
        if p:
            with open(p, "wb") as f:
                f.write(self.wave_bytes(layer))
            self.status.set(f"Wrote {p}")

    def replace(self):
        s, layer = self.selected()
        if not s:
            return
        src = filedialog.askopenfilename(title=f"Replace {s.label} {layer[0]}",
                                         filetypes=[("WAV or BFWAV", "*.wav *.bfwav"), ("All files", "*.*")])
        if not src:
            return
        mute = len(self.music.layers(s)) > 1 and messagebox.askyesno(
            TITLE, f"{s.label} has several layers played together. Silence the others?")
        music = self.music

        def make():
            if src.lower().endswith(".bfwav"):
                new = open(src, "rb").read()
            else:
                new = wav_to_fwav(src, log=lambda *_: None)[0]
            if s.label in self.custom:
                old = edit.custom_music(music.bf)
                # a new song gets a fresh beat track; the others keep theirs
                return edit.set_custom_music(music.bf, [(l, new, b, v, o, None) if l == s.label else
                                                        (l, w, b, v, o, q) for l, w, b, v, o, q in old])
            info = F.fwav_info(new)
            plan = [(layer, new)]
            if mute:
                quiet = silent_wave(info["rate"], info["channels"])
                plan += [(l, quiet) for l in music.layers(s) if l != layer]
            replace = {}
            for (name, a, i, vol), blob in plan:
                fid, waves = music.war(a)
                waves = list(replace.get(fid, waves))
                waves[i] = blob
                replace[fid] = waves
            return fwav.rebuild_bfsar(music.bf, {fid: fwav.build_fwar(w) for fid, w in replace.items()})
        self.apply(f"Replacing {s.label} {layer[0]}", make)

    def add(self):
        if not self.music:
            return
        d = AddDialog(self, {s.label for s in self.music.bf.sounds})
        r = d.result
        if not r:
            return
        bf = self.music.bf

        def make():
            blob, pcm, rate = wav_to_fwav(r["path"], r["loop_start"], r["loop"], log=lambda *_: None)
            bpm, offset = estimate_beat(pcm, rate, r["bpm"])
            if r["offset"] is not None:
                offset = r["offset"]
            return edit.add_music(bf, r["label"], blob, bpm, offset=offset, bahs=r["bahs"])
        self.apply(f"Adding {r['label']}", make)

    def remove(self):
        s, _ = self.selected(want_layer=False)
        if not s:
            return
        if s.label not in self.custom:
            messagebox.showinfo(TITLE, f"{s.label} is one of the game's own sounds; only added music "
                                       "can be removed (Replace changes the game's own).")
            return
        if messagebox.askyesno(TITLE, f"Remove {s.label}?"):
            bf = self.music.bf
            self.apply(f"Removing {s.label}", lambda: edit.remove_music(bf, s.label))


def main():
    App(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()


if __name__ == "__main__":
    main()
