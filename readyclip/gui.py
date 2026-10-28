"""Tkinter front end: add videos, pick a preset, press Convert."""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from readyclip import APP_NAME, __version__
from readyclip.core import (
    CODECS, CUSTOM, FPS_CAPS, JOBS, PRESETS, QUALITIES, RESOLUTIONS, SIZE_MODES,
    Settings, apply_preset, collect_videos, convert_many, detect_preset,
    find_tool, load_settings, output_path, pick_jobs, save_settings,
)

BRAND = "#F2602F"
BRAND_DARK = "#D44A1D"
INK = "#1B1D22"
MUTED = "#6B7079"
SURFACE = "#F6F6F8"
CARD = "#FFFFFF"
LINE = "#E3E4E8"

STATUS_TEXT = {"done": "Done", "skipped": "Skipped (exists)", "failed": "Failed",
               "cancelled": "Cancelled"}

CUSTOM_LABEL = "Custom"
NO_LIMIT = "No limit"
SIZE_CHOICES = [NO_LIMIT, "16", "20", "25", "50", "100", "500", "2000"]


def asset(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "assets" / name


def _eta(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    return f"{seconds // 3600:.0f} h {seconds % 3600 / 60:.0f} min"


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} - phone-ready videos")
        height = min(860, self.winfo_screenheight() - 80)  # room for More options
        self.geometry(f"960x{height}")
        self.minsize(800, min(600, height))
        self.configure(bg=SURFACE)
        try:
            self._icon = tk.PhotoImage(file=str(asset("readyclip.png")))
            self.iconphoto(True, self._icon)
            self._logo = self._icon.subsample(12, 12)
        except tk.TclError:
            self._icon = self._logo = None

        self.settings = load_settings()
        self.files: dict[str, Path] = {}   # tree item id -> path
        self.events: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.worker: threading.Thread | None = None
        self.last_output: Path | None = None
        self._errors: list[str] = []
        self._fracs: dict[str, float] = {}  # tree item id -> progress 0..1
        self._started: dict[str, float] = {}
        self._jobs = 1
        self._applying = False  # True while code (not the user) fills option fields

        self._style()
        self._build()
        self._load_settings_into_ui()
        self._refresh_empty_hint()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self.after(200, self._check_ffmpeg)

    # ------------------------------------------------------------ layout

    def _style(self) -> None:
        st = ttk.Style(self)
        st.theme_use("clam")
        base = ("Segoe UI", 10)
        st.configure(".", font=base, background=SURFACE, foreground=INK)
        st.configure("Card.TFrame", background=CARD)
        st.configure("Card.TLabel", background=CARD, foreground=INK)
        st.configure("Step.TLabel", background=CARD, foreground=INK,
                     font=("Segoe UI Semibold", 11))
        st.configure("Muted.TLabel", background=CARD, foreground=MUTED)
        for kind in ("Card.TCheckbutton", "Card.TRadiobutton"):
            st.configure(kind, background=CARD)
            st.map(kind, background=[("active", CARD)])
        st.configure("TButton", padding=(12, 6), background="#ECEDF0",
                     bordercolor=LINE, lightcolor="#ECEDF0", darkcolor="#ECEDF0")
        st.map("TButton", background=[("active", "#E1E2E6")])
        st.configure("Accent.TButton", font=("Segoe UI Semibold", 11),
                     padding=(26, 10), foreground="white", background=BRAND,
                     bordercolor=BRAND, lightcolor=BRAND, darkcolor=BRAND)
        st.map("Accent.TButton",
               background=[("disabled", "#F3B9A4"), ("active", BRAND_DARK)],
               foreground=[("disabled", "white")])
        st.configure("Brand.Horizontal.TProgressbar", troughcolor="#ECEDF0",
                     background=BRAND, bordercolor="#ECEDF0",
                     lightcolor=BRAND, darkcolor=BRAND, thickness=8)
        st.configure("Treeview", rowheight=26, background=CARD,
                     fieldbackground=CARD, bordercolor=LINE)
        st.configure("Treeview.Heading", font=("Segoe UI Semibold", 9),
                     background="#F0F1F3", foreground=MUTED, relief="flat")
        st.map("Treeview", background=[("selected", "#FDE3D8")],
               foreground=[("selected", INK)])
        st.configure("TCombobox", padding=4)

    def _card(self, parent, step: str, title: str) -> ttk.Frame:
        # Packed from the bottom so the file list, not these, gives up space.
        outer = tk.Frame(parent, bg=LINE, bd=0)
        outer.pack(side="bottom", fill="x", padx=16, pady=(0, 12))
        card = ttk.Frame(outer, style="Card.TFrame", padding=(16, 12))
        card.pack(fill="both", expand=True, padx=1, pady=1)
        ttk.Label(card, text=f"{step}  {title}", style="Step.TLabel").pack(anchor="w")
        return card

    def _build(self) -> None:
        header = tk.Frame(self, bg=INK, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        if self._logo:
            tk.Label(header, image=self._logo, bg=INK).pack(side="left", padx=(16, 10))
        tk.Label(header, text=APP_NAME, bg=INK, fg="white",
                 font=("Segoe UI Semibold", 17)).pack(side="left")
        tk.Label(header, text="  Make action-cam videos play and share on any phone",
                 bg=INK, fg="#A9ADB6", font=("Segoe UI", 10)).pack(side="left", pady=(5, 0))
        tk.Label(header, text=f"v{__version__}", bg=INK, fg="#6B7079",
                 font=("Segoe UI", 9)).pack(side="right", padx=16)

        body = tk.Frame(self, bg=SURFACE)
        body.pack(fill="both", expand=True, pady=(14, 0))

        # Bottom-up: step 3 first, then step 2, then the list takes the rest.
        self._build_convert(body)
        self._build_options(body)

        # Step 1: files
        outer = tk.Frame(body, bg=LINE)
        outer.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        files = ttk.Frame(outer, style="Card.TFrame", padding=(16, 12))
        files.pack(fill="both", expand=True, padx=1, pady=1)
        top = ttk.Frame(files, style="Card.TFrame")
        top.pack(fill="x")
        ttk.Label(top, text="1  Pick your videos", style="Step.TLabel").pack(side="left")
        self.count_label = ttk.Label(top, text="", style="Muted.TLabel")
        self.count_label.pack(side="left", padx=10)
        for text, cmd in (("Clear", self._clear), ("Remove", self._remove),
                          ("Add folder...", self._add_folder),
                          ("Add videos...", self._add_files)):
            ttk.Button(top, text=text, command=cmd).pack(side="right", padx=(6, 0))

        tree_box = ttk.Frame(files, style="Card.TFrame")
        tree_box.pack(fill="both", expand=True, pady=(10, 0))
        self.tree = ttk.Treeview(tree_box, columns=("folder", "size", "status"),
                                 show="tree headings", selectmode="extended", height=4)
        self.tree.heading("#0", text="File", anchor="w")
        self.tree.heading("folder", text="Folder", anchor="w")
        self.tree.heading("size", text="Size", anchor="e")
        self.tree.heading("status", text="Status", anchor="w")
        self.tree.column("#0", width=200, stretch=True)
        self.tree.column("folder", width=220, stretch=True)
        self.tree.column("size", width=80, anchor="e", stretch=False)
        self.tree.column("status", width=260, stretch=False)
        scroll = ttk.Scrollbar(tree_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Delete>", lambda e: self._remove())
        self.empty_hint = tk.Label(
            tree_box, bg=CARD, fg=MUTED, font=("Segoe UI", 10), justify="center",
            text="No videos yet.\nClick \"Add folder...\" to convert a whole SD-card folder,\n"
                 "or \"Add videos...\" to pick individual clips.")

    def _build_options(self, body) -> None:
        opts = self._card(body, "2", "Choose how they should look")
        row = ttk.Frame(opts, style="Card.TFrame")
        row.pack(fill="x", pady=(8, 0))
        self.preset_var = tk.StringVar()
        self.res_var = tk.StringVar()
        self.quality_var = tk.StringVar()
        self.codec_var = tk.StringVar()
        preset_labels = [label for label, _ in PRESETS.values()] + [CUSTOM_LABEL]
        self._combo(row, "Made for", self.preset_var, preset_labels, 0, 30)
        self._combo(row, "Resolution", self.res_var, list(RESOLUTIONS.values()), 1, 16)
        self._combo(row, "Quality", self.quality_var, list(QUALITIES.values()), 2, 13)
        self._combo(row, "Format", self.codec_var, list(CODECS.values()), 3, 30)

        self.adv_open = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="More options", variable=self.adv_open,
                        style="Card.TCheckbutton",
                        command=self._toggle_advanced).pack(anchor="w", pady=(10, 0))
        self.adv = ttk.Frame(opts, style="Card.TFrame")
        self.fps_var = tk.StringVar()
        self.gpu_var = tk.BooleanVar()
        self.skip_var = tk.BooleanVar()
        self.sub_var = tk.StringVar()
        self.jobs_var = tk.StringVar()
        self.size_var = tk.StringVar()
        self.size_mode_var = tk.StringVar()
        self.outmode_var = tk.StringVar(value="sub")
        self.outdir_var = tk.StringVar()
        self._combo(self.adv, "Frame rate", self.fps_var, list(FPS_CAPS.values()), 0, 14)
        self._combo(self.adv, "Files at once", self.jobs_var, list(JOBS.values()), 1, 12)
        ttk.Label(self.adv, text="Max size per file (MB)", style="Muted.TLabel").grid(
            row=0, column=2, sticky="w", padx=(0, 12))
        ttk.Combobox(self.adv, textvariable=self.size_var, values=SIZE_CHOICES,
                     width=12).grid(row=1, column=2, sticky="w", padx=(0, 12))
        self._combo(self.adv, "If a file is bigger", self.size_mode_var,
                    list(SIZE_MODES.values()), 3, 28)

        save = ttk.Frame(self.adv, style="Card.TFrame")
        save.grid(row=2, column=0, columnspan=4, sticky="we", pady=(10, 0))
        ttk.Label(save, text="Save to", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Radiobutton(save, text="a subfolder next to each video, named",
                        variable=self.outmode_var, value="sub",
                        style="Card.TRadiobutton").grid(row=0, column=1, sticky="w")
        ttk.Entry(save, textvariable=self.sub_var, width=14).grid(
            row=0, column=2, sticky="w", padx=(6, 0))
        ttk.Radiobutton(save, text="one folder:", variable=self.outmode_var,
                        value="dir", style="Card.TRadiobutton").grid(
            row=1, column=1, sticky="w", pady=(4, 0))
        ttk.Entry(save, textvariable=self.outdir_var, width=44).grid(
            row=1, column=2, sticky="we", padx=(6, 0), pady=(4, 0))
        ttk.Button(save, text="Browse...", command=self._choose_outdir).grid(
            row=1, column=3, sticky="w", padx=(6, 0), pady=(4, 0))

        checks = ttk.Frame(self.adv, style="Card.TFrame")
        checks.grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Checkbutton(checks, text="Use NVIDIA GPU (much faster)",
                        variable=self.gpu_var, style="Card.TCheckbutton").pack(
            side="left", padx=(0, 16))
        ttk.Checkbutton(checks, text="Skip already converted",
                        variable=self.skip_var, style="Card.TCheckbutton").pack(side="left")

    def _build_convert(self, body) -> None:
        go = self._card(body, "3", "Convert")
        bar = ttk.Frame(go, style="Card.TFrame")
        bar.pack(fill="x", pady=(8, 0))
        self.convert_btn = ttk.Button(bar, text="Convert", style="Accent.TButton",
                                      command=self._start)
        self.convert_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=self._stop,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=8)
        self.open_btn = ttk.Button(bar, text="Open output folder",
                                   command=self._open_output, state="disabled")
        self.open_btn.pack(side="left")
        prog = ttk.Frame(bar, style="Card.TFrame")
        prog.pack(side="left", fill="x", expand=True, padx=(16, 0))
        self.status_label = ttk.Label(prog, text="Ready.", style="Muted.TLabel")
        self.status_label.pack(anchor="w")
        self.overall = ttk.Progressbar(prog, style="Brand.Horizontal.TProgressbar",
                                       maximum=1.0)
        self.overall.pack(fill="x", pady=(4, 0))

    def _combo(self, parent, label, var, values, col, width) -> None:
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(
            row=0, column=col, sticky="w", padx=(0, 12))
        ttk.Combobox(parent, textvariable=var, values=values, state="readonly",
                     width=width).grid(row=1, column=col, sticky="w", padx=(0, 12))

    def _toggle_advanced(self) -> None:
        if self.adv_open.get():
            self.adv.pack(fill="x", pady=(8, 0))
            self._keep_list_visible()
        else:
            self.adv.pack_forget()

    def _keep_list_visible(self, rows: int = 4) -> None:
        """Grow the window if the option panels squeezed the file list away."""
        self.update_idletasks()
        need = (rows + 1) * 26 + 4  # rows plus the heading
        short = need - self.tree.winfo_height()
        if short > 0:
            height = min(self.winfo_height() + short, self.winfo_screenheight() - 80)
            self.geometry(f"{self.winfo_width()}x{height}")

    # ------------------------------------------------------------ settings

    def _load_settings_into_ui(self) -> None:
        s = self.settings
        self._applying = True  # filling fields must not flip the preset to Custom
        self.res_var.set(RESOLUTIONS.get(s.resolution, RESOLUTIONS[2160]))
        self.quality_var.set(QUALITIES[s.quality])
        self.codec_var.set(CODECS[s.codec])
        self.fps_var.set(FPS_CAPS[s.max_fps])
        self.size_var.set(str(s.max_size_mb) if s.max_size_mb else NO_LIMIT)
        self.size_mode_var.set(SIZE_MODES[s.size_mode])
        self.jobs_var.set(JOBS[s.jobs])
        self.gpu_var.set(s.use_gpu)
        self.skip_var.set(s.skip_existing)
        self.sub_var.set(s.subfolder)
        self.outdir_var.set(s.output_dir)
        self.outmode_var.set("dir" if s.output_dir else "sub")
        preset = detect_preset(s)
        self.preset_var.set(PRESETS[preset][0] if preset in PRESETS else CUSTOM_LABEL)
        self._applying = False
        if not getattr(self, "_traced", False):
            self._traced = True
            self.preset_var.trace_add("write", lambda *_: self._on_preset())
            for var in (self.res_var, self.quality_var, self.codec_var,
                        self.fps_var, self.size_var, self.size_mode_var):
                var.trace_add("write", lambda *_: self._on_field())

    def _on_preset(self) -> None:
        if self._applying:
            return
        label = self.preset_var.get()
        name = next((k for k, (lbl, _) in PRESETS.items() if lbl == label), None)
        if name is None:
            return  # "Custom": leave the fields as they are
        try:
            current = self._settings_from_ui(check=False)
        except ValueError:
            current = self.settings
        self.settings = apply_preset(current, name)
        self._load_settings_into_ui()
        if PRESETS[name][1]["max_size_mb"] and not self.adv_open.get():
            self.adv_open.set(True)  # show where the size limit lives
            self._toggle_advanced()

    def _on_field(self) -> None:
        if self._applying:
            return
        try:
            preset = detect_preset(self._settings_from_ui(check=False))
        except ValueError:
            preset = CUSTOM  # e.g. half-typed size
        self._applying = True
        self.preset_var.set(PRESETS[preset][0] if preset in PRESETS else CUSTOM_LABEL)
        self._applying = False

    def _choose_outdir(self) -> None:
        folder = filedialog.askdirectory(title="Save converted videos to")
        if folder:
            self.outdir_var.set(folder)
            self.outmode_var.set("dir")

    def _settings_from_ui(self, check: bool = True) -> Settings:
        key = lambda table, label: next(k for k, v in table.items() if v == label)  # noqa: E731
        size_text = self.size_var.get().strip()
        if size_text in ("", NO_LIMIT, "0"):
            max_size = 0
        else:
            try:
                max_size = int(float(size_text.split()[0]))
            except ValueError:
                raise ValueError("Max size must be a number of MB, or \"No limit\".") from None
        output_dir = self.outdir_var.get().strip() if self.outmode_var.get() == "dir" else ""
        if check and self.outmode_var.get() == "dir" and not output_dir:
            raise ValueError("Choose the folder to save to, or switch back to a subfolder.")
        s = Settings(
            resolution=key(RESOLUTIONS, self.res_var.get()),
            quality=key(QUALITIES, self.quality_var.get()),
            codec=key(CODECS, self.codec_var.get()),
            max_fps=key(FPS_CAPS, self.fps_var.get()),
            use_gpu=self.gpu_var.get(),
            subfolder=self.sub_var.get().strip(),
            skip_existing=self.skip_var.get(),
            audio_kbps=self.settings.audio_kbps,
            jobs=key(JOBS, self.jobs_var.get()),
            max_size_mb=max_size,
            size_mode=key(SIZE_MODES, self.size_mode_var.get()),
            output_dir=output_dir,
        )
        s.preset = detect_preset(s)
        if check:
            s.validate()
        return s

    # ------------------------------------------------------------ file list

    def _add_paths(self, paths, recursive=False) -> None:
        known = {str(p.resolve()).lower() for p in self.files.values()}
        sub = self.sub_var.get().strip() or "converted"
        out_dir = self.outdir_var.get().strip() if self.outmode_var.get() == "dir" else None
        for p in collect_videos(paths, recursive, sub, out_dir or None):
            if str(p.resolve()).lower() in known:
                continue
            try:
                size = human_size(p.stat().st_size)
            except OSError:
                size = "?"
            iid = self.tree.insert("", "end", text=p.name,
                                   values=(str(p.parent), size, "Waiting"))
            self.files[iid] = p
        self._refresh_empty_hint()

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Choose videos",
            filetypes=[("Videos", "*.mp4 *.MP4 *.mov *.MOV"), ("All files", "*.*")])
        self._add_paths(paths)

    def _add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Choose a folder with videos")
        if not folder:
            return
        before = len(self.files)
        self._add_paths([folder])
        if len(self.files) == before:
            if messagebox.askyesno(APP_NAME, "No videos directly in that folder.\n"
                                   "Look inside its subfolders too?"):
                self._add_paths([folder], recursive=True)

    def _remove(self) -> None:
        if self._busy():
            return
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.files.pop(iid, None)
        self._refresh_empty_hint()

    def _clear(self) -> None:
        if self._busy():
            return
        self.tree.delete(*self.tree.get_children())
        self.files.clear()
        self._refresh_empty_hint()

    def _refresh_empty_hint(self) -> None:
        n = len(self.files)
        if n:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.place(relx=0.5, rely=0.55, anchor="center")
        self.count_label.config(text=f"{n} video{'s' if n != 1 else ''}" if n else "")

    # ------------------------------------------------------------ running

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _start(self) -> None:
        if self._busy():
            return
        if not self.files:
            messagebox.showinfo(APP_NAME, "Add some videos or a folder first.")
            return
        try:
            settings = self._settings_from_ui()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self.settings = settings
        try:
            save_settings(settings)
        except OSError as exc:
            self.status_label.config(text=f"Could not save settings: {exc}")

        items = list(self.files.items())
        for iid, _ in items:
            self.tree.set(iid, "status", "Waiting")
        self.cancel.clear()
        self._errors = []
        self._fracs = {iid: 0.0 for iid, _ in items}
        self._started = {}  # tree item id -> time its first progress arrived
        self._jobs = 1
        self.convert_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.open_btn.config(state="disabled")
        self.overall.config(value=0)
        self.status_label.config(text="Checking GPU and videos..." if settings.use_gpu
                                 else "Starting...")
        self.last_output = output_path(items[0][1], settings).parent
        self.worker = threading.Thread(target=self._work, args=(items, settings),
                                       daemon=True)
        self.worker.start()

    def _work(self, items, settings: Settings) -> None:
        ids = [iid for iid, _ in items]
        paths = [p for _, p in items]
        try:
            jobs = pick_jobs(paths, settings)  # probes files and the GPU: keep off the UI thread
            self.events.put(("jobs", jobs))
            results = convert_many(
                paths, settings,
                on_file=lambda i, p: self.events.put(("file", ids[i])),
                on_progress=lambda i, frac: self.events.put(("progress", ids[i], frac)),
                on_result=lambda i, res: self.events.put(("result", ids[i], res)),
                cancel=self.cancel, jobs=jobs)
        except Exception as exc:  # surface anything unexpected instead of dying silently
            self.events.put(("crash", repr(exc)))
            return
        self.events.put(("finished", results))

    def _poll(self) -> None:
        progress = {}  # newest tick per file; older ticks are redundant
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "progress":
                    progress[ev[1]] = ev
                    continue
                for pending in progress.values():  # keep order: never land after "result"
                    self._handle(pending)
                progress.clear()
                self._handle(ev)
        except queue.Empty:
            pass
        for pending in progress.values():
            self._handle(pending)
        self.after(100, self._poll)

    def _update_overall(self) -> None:
        total = len(self._fracs) or 1
        self.overall.config(value=sum(self._fracs.values()) / total)
        running = sum(0 < f < 1 for f in self._fracs.values())
        done = sum(f >= 1 for f in self._fracs.values())
        at_once = f", {self._jobs} at a time" if self._jobs > 1 else ""
        self.status_label.config(
            text=f"Converted {done} of {len(self._fracs)}{at_once}"
                 + (f" ({running} running)" if running else ""))

    def _handle(self, ev) -> None:
        kind = ev[0]
        if kind == "jobs":
            self._jobs = ev[1]
            self._update_overall()
        elif kind == "file":
            _, iid = ev
            self.tree.set(iid, "status", "Converting...")
            self.tree.see(iid)
        elif kind == "progress":
            _, iid, frac = ev
            started = self._started.setdefault(iid, time.monotonic())
            if frac >= 0.995:
                # Big files sit here a while: writing the index, or splitting.
                text = "Finishing..."
            else:
                text = f"Converting... {frac * 100:.0f}%"
                elapsed = time.monotonic() - started
                if frac > 0.02 and elapsed > 10:
                    text += f", {_eta(elapsed * (1 - frac) / frac)} left"
            self.tree.set(iid, "status", text)
            self._fracs[iid] = min(frac, 0.999)  # 1.0 only once the result is in
            self._update_overall()
        elif kind == "result":
            _, iid, res = ev
            self._fracs[iid] = 1.0
            self._update_overall()
            text = STATUS_TEXT.get(res.status, res.status)
            if res.status == "done" and res.message:
                text += f" ({res.message})"
            self.tree.set(iid, "status", text)
            if res.output:
                self.last_output = res.output.parent
            if res.status == "failed":
                self.tree.item(iid, tags=("failed",))
                self.tree.tag_configure("failed", foreground="#C62828")
                self._errors.append(f"{res.source.name}: {res.message}")
        elif kind in ("finished", "crash"):
            self.convert_btn.config(state="normal")
            self.cancel_btn.config(state="disabled")
            if kind == "crash":
                self.status_label.config(text="Stopped by an unexpected error.")
                messagebox.showerror(APP_NAME, ev[1])
                return
            results = ev[1]
            done = sum(r.status == "done" for r in results)
            skipped = sum(r.status == "skipped" for r in results)
            failed = sum(r.status == "failed" for r in results)
            cancelled = self.cancel.is_set()
            parts = [f"{done} converted"]
            if skipped:
                parts.append(f"{skipped} skipped")
            if failed:
                parts.append(f"{failed} failed")
            prefix = "Cancelled: " if cancelled else "Finished: "
            self.status_label.config(text=prefix + ", ".join(parts) + ".")
            if not cancelled:
                self.overall.config(value=1.0)
            if self.last_output and self.last_output.exists():
                self.open_btn.config(state="normal")
            if self._errors:
                messagebox.showwarning(APP_NAME, "Some videos failed:\n\n"
                                       + "\n\n".join(self._errors[:5]))

    def _stop(self) -> None:
        self.cancel.set()
        self.status_label.config(text="Cancelling...")

    def _open_output(self) -> None:
        if self.last_output and self.last_output.exists():
            os.startfile(self.last_output)  # noqa: S606 - Windows only, user folder

    def _check_ffmpeg(self) -> None:
        try:
            find_tool("ffmpeg")
            find_tool("ffprobe")
        except FileNotFoundError as exc:
            messagebox.showerror(APP_NAME, f"{exc}\n\nDownload the full ReadyClip zip "
                                 "from the Releases page; it includes ffmpeg.")
            self.convert_btn.config(state="disabled")

    def _on_close(self) -> None:
        if self._busy():
            if not messagebox.askyesno(APP_NAME, "A conversion is running. Stop it and quit?"):
                return
            self.cancel.set()
            self.worker.join(timeout=5)
        self.destroy()


def main() -> None:
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
        try:  # own taskbar entry and icon instead of python.exe's
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ReadyClip.App")
        except (AttributeError, OSError):
            pass
    App().mainloop()
