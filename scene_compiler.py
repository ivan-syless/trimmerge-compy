#!/usr/bin/env python3
"""
Scene Compiler - cut selected scenes from a video with FFmpeg and join them
into one compilation, keeping ALL audio tracks and ALL subtitle tracks.

* Your original file is never touched.
* Everything happens in a temporary folder created next to the video;
  the folder is deleted when the job finishes (or fails / is cancelled).
* Requires: Python 3 with Tkinter, and ffmpeg + ffprobe in PATH.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from fractions import Fraction
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# ----------------------------------------------------------------------------
# Time helpers
# ----------------------------------------------------------------------------
TIME = r"\d+(?::\d+){1,2}(?:\.\d+)?"
PAIR_RE = re.compile(rf"({TIME})\s*(?:to|until|->|-|–|—|→)\s*({TIME})", re.I)


def to_seconds(text):
    secs = 0.0
    for part in text.split(":"):
        secs = secs * 60 + float(part)
    return secs


def fmt_time(sec):
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".rstrip("0").rstrip(".")


def parse_ranges(text, duration):
    """Return (list of (start, end) in seconds, list of error strings)."""
    errors = []
    leftovers = PAIR_RE.sub(" ", text)
    leftovers = re.sub(r"[\s,;]+", " ", leftovers).strip()
    if leftovers:
        errors.append(f"Could not understand this part of the timestamps:\n  {leftovers}")

    ranges = []
    for n, (a, b) in enumerate(PAIR_RE.findall(text), 1):
        start, end = to_seconds(a), to_seconds(b)
        if end <= start:
            errors.append(f"Range {n} ({a} to {b}): end must be after start.")
            continue
        if duration and start >= duration:
            errors.append(f"Range {n} ({a} to {b}): starts after the end of the video.")
            continue
        if duration and end > duration:
            end = duration
        ranges.append((start, end))
    if not ranges and not errors:
        errors.append("No timestamps found. Example:  00:05:32 to 00:08:56")
    return ranges, errors


# ----------------------------------------------------------------------------
# ffprobe helpers
# ----------------------------------------------------------------------------
def probe(ffprobe, path):
    res = subprocess.run(
        [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW,
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "ffprobe failed")
    return json.loads(res.stdout)


def nice_bitrate(value):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return f"{v / 1_000_000:.2f} Mb/s" if v >= 1_000_000 else f"{v / 1000:.0f} kb/s"


def describe(info):
    fmt = info.get("format", {})
    lines = [
        f"Container : {fmt.get('format_long_name', fmt.get('format_name', '?'))}",
        f"Duration  : {fmt_time(float(fmt.get('duration') or 0))}",
    ]
    br = nice_bitrate(fmt.get("bit_rate"))
    if br:
        lines.append(f"Overall   : {br}")
    lines.append("")
    for s in info.get("streams", []):
        t = s.get("codec_type", "?")
        tags = s.get("tags", {})
        lang = tags.get("language", "und")
        title = tags.get("title", "")
        disp = s.get("disposition", {})
        flags = [n for n in ("default", "forced") if disp.get(n)]
        head = f"#{s['index']:<2} {t:<10} {s.get('codec_name', '?')}"
        parts = []
        if t == "video":
            if s.get("profile"):
                parts.append(s["profile"])
            parts.append(f"{s.get('width')}x{s.get('height')}")
            if s.get("pix_fmt"):
                parts.append(s["pix_fmt"])
            try:
                parts.append(f"{float(Fraction(s.get('avg_frame_rate', '0/1'))):.3f} fps")
            except (ValueError, ZeroDivisionError):
                pass
            trc = s.get("color_transfer")
            if trc == "smpte2084":
                parts.append("HDR10 (PQ)")
            elif trc == "arib-std-b67":
                parts.append("HLG")
            if any(sd.get("side_data_type", "").startswith("DOVI") for sd in s.get("side_data_list", [])):
                parts.append("Dolby Vision")
            if s.get("disposition", {}).get("attached_pic"):
                parts.append("(cover art - skipped)")
        elif t == "audio":
            parts.append(f"{s.get('channels')}ch {s.get('channel_layout', '')}".strip())
            if s.get("sample_rate"):
                parts.append(f"{s['sample_rate']} Hz")
            parts.append(f"lang={lang}")
        elif t == "subtitle":
            parts.append(f"lang={lang}")
        elif t == "attachment":
            parts.append(tags.get("filename", ""))
        b = nice_bitrate(s.get("bit_rate") or tags.get("BPS"))
        if b:
            parts.append(b)
        if title:
            parts.append(f'"{title}"')
        if flags:
            parts.append("[" + ", ".join(flags) + "]")
        lines.append(head + "  " + ", ".join(p for p in parts if p))
    return "\n".join(lines)


def build_maps(info, include_attachments=True):
    """Map the main video + every audio / subtitle (+ attachment) stream."""
    maps, main_video, seen_video = [], None, False
    for s in info.get("streams", []):
        t = s.get("codec_type")
        if t == "video":
            if s.get("disposition", {}).get("attached_pic") or seen_video:
                continue
            seen_video, main_video = True, s
            maps += ["-map", f"0:{s['index']}"]
        elif t in ("audio", "subtitle") or (t == "attachment" and include_attachments):
            maps += ["-map", f"0:{s['index']}"]
    return maps, main_video


def subtitle_args(info):
    """MP4 'mov_text' subtitles can't be stored in Matroska: convert just those tracks to SRT."""
    args, k = [], 0
    for s in info.get("streams", []):
        if s.get("codec_type") == "subtitle":
            if s.get("codec_name") == "mov_text":
                args += [f"-c:s:{k}", "srt"]
            k += 1
    return args


def video_args(main, mode, preset, crf):
    if mode == "copy" or main is None:
        return ["-c", "copy"]
    enc = "libx264" if main.get("codec_name") == "h264" else "libx265"
    args = ["-c", "copy", "-c:v", enc, "-preset", preset, "-crf", str(crf)]
    if main.get("pix_fmt"):
        args += ["-pix_fmt", main["pix_fmt"]]  # keeps 10-bit etc.
    for key, opt in (("color_primaries", "-color_primaries"),
                     ("color_transfer", "-color_trc"),
                     ("color_space", "-colorspace"),
                     ("color_range", "-color_range")):
        val = main.get(key)
        if val and val != "unknown":
            args += [opt, val]  # keeps HDR / colour tagging
    if enc == "libx265":
        args += ["-x265-params", "log-level=error"]
    return args


PROGRESS_KEYS = {"frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms",
                 "out_time", "dup_frames", "drop_frames", "speed", "progress"}


class Cancelled(Exception):
    pass


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title("Scene Compiler")
        root.geometry("900x860")
        root.minsize(760, 700)

        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        self.info = None
        self.src = None
        self.duration = 0.0
        self.q = queue.Queue()
        self.cancel_evt = threading.Event()
        self.proc = None
        self.thread = None
        self.running = False

        self.build_ui()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)
        if not (self.ffmpeg and self.ffprobe):
            root.after(300, lambda: messagebox.showerror(
                "FFmpeg not found",
                "ffmpeg / ffprobe were not found in PATH.\n\n"
                "Fedora:   sudo dnf install ffmpeg   (needs RPM Fusion, see instructions)\n"
                "Windows:  winget install Gyan.FFmpeg   (then restart this program)"))

    # -- layout ---------------------------------------------------------------
    def build_ui(self):
        pad = {"padx": 10, "pady": 4}
        main = ttk.Frame(self.root)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        # 1. file
        f = ttk.LabelFrame(main, text="1. Video file")
        f.grid(row=0, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        self.file_var = tk.StringVar()
        ttk.Entry(f, textvariable=self.file_var, state="readonly").grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        ttk.Button(f, text="Browse…", command=self.browse_video).grid(row=0, column=1, padx=6, pady=6)
        self.info_box = scrolledtext.ScrolledText(f, height=9, font=("monospace", 9), state="disabled")
        self.info_box.grid(row=1, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))

        # 2. timestamps
        t = ttk.LabelFrame(main, text="2. Timestamps (in the order you want them joined)")
        t.grid(row=1, column=0, sticky="ew", **pad)
        t.columnconfigure(0, weight=1)
        ttk.Label(t, text="One per line or separated by commas.  Format: HH:MM:SS to HH:MM:SS   (MM:SS also works)"
                  ).grid(row=0, column=0, sticky="w", padx=6, pady=(4, 0))
        self.ts_text = scrolledtext.ScrolledText(t, height=8, font=("monospace", 10))
        self.ts_text.grid(row=1, column=0, sticky="ew", padx=6, pady=6)
        self.ts_text.insert("1.0", "00:05:32 to 00:08:56\n00:25:12 to 00:38:42")

        # 3. options
        o = ttk.LabelFrame(main, text="3. Options")
        o.grid(row=2, column=0, sticky="ew", **pad)
        o.columnconfigure(1, weight=1)
        self.mode_var = tk.StringVar(value="reencode")
        ttk.Radiobutton(o, text="Accurate cuts (re-encode video, copy audio + subtitles)  — frame-exact, slower",
                        variable=self.mode_var, value="reencode").grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=2)
        ttk.Radiobutton(o, text="Fast stream copy (no re-encode)  — instant, but cuts snap to keyframes (may be a few seconds off)",
                        variable=self.mode_var, value="copy").grid(row=1, column=0, columnspan=4, sticky="w", padx=6, pady=2)
        row = ttk.Frame(o)
        row.grid(row=2, column=0, columnspan=4, sticky="w", padx=6, pady=2)
        ttk.Label(row, text="Quality (CRF, lower = better):").pack(side="left")
        self.crf_var = tk.IntVar(value=18)
        ttk.Spinbox(row, from_=0, to=35, width=4, textvariable=self.crf_var).pack(side="left", padx=(4, 16))
        ttk.Label(row, text="Preset:").pack(side="left")
        self.preset_var = tk.StringVar(value="medium")
        ttk.Combobox(row, textvariable=self.preset_var, width=10, state="readonly",
                     values=["ultrafast", "superfast", "veryfast", "faster", "fast",
                             "medium", "slow", "slower", "veryslow"]).pack(side="left", padx=4)
        ttk.Label(o, text="Output file:").grid(row=3, column=0, sticky="w", padx=6, pady=6)
        self.out_var = tk.StringVar()
        ttk.Entry(o, textvariable=self.out_var).grid(row=3, column=1, sticky="ew", padx=6, pady=6)
        ttk.Button(o, text="Browse…", command=self.browse_output).grid(row=3, column=2, padx=6, pady=6)

        # 4. run
        r = ttk.Frame(main)
        r.grid(row=3, column=0, sticky="ew", **pad)
        r.columnconfigure(0, weight=1)
        self.start_btn = ttk.Button(r, text="Create compilation", command=self.start)
        self.start_btn.grid(row=0, column=1, padx=4)
        self.cancel_btn = ttk.Button(r, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=2, padx=4)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(r, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.bar = ttk.Progressbar(main, maximum=100)
        self.bar.grid(row=4, column=0, sticky="ew", **pad)

        self.log_box = scrolledtext.ScrolledText(main, height=10, font=("monospace", 9), state="disabled")
        self.log_box.grid(row=5, column=0, sticky="nsew", **pad)
        main.rowconfigure(5, weight=1)

    # -- small helpers -----------------------------------------------------------
    def log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def set_info(self, text):
        self.info_box.configure(state="normal")
        self.info_box.delete("1.0", "end")
        self.info_box.insert("1.0", text)
        self.info_box.configure(state="disabled")

    def browse_video(self):
        path = filedialog.askopenfilename(
            title="Select video",
            filetypes=[("Video files", "*.mkv *.mp4 *.m4v *.mov *.avi *.webm *.ts *.m2ts"), ("All files", "*.*")])
        if not path:
            return
        if not self.ffprobe:
            messagebox.showerror("FFmpeg missing", "ffprobe was not found in PATH.")
            return
        try:
            info = probe(self.ffprobe, path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Could not read file", str(e))
            return
        self.info = info
        self.src = Path(path)
        self.duration = float(info.get("format", {}).get("duration") or 0)
        self.file_var.set(path)
        self.set_info(describe(info))
        self.out_var.set(str(self.src.with_name(self.src.stem + "_compilation.mkv")))

    def browse_output(self):
        initial = Path(self.out_var.get()) if self.out_var.get() else None
        path = filedialog.asksaveasfilename(
            title="Save compilation as", defaultextension=".mkv",
            initialdir=str(initial.parent) if initial else None,
            initialfile=initial.name if initial else "compilation.mkv",
            filetypes=[("Matroska", "*.mkv")])
        if path:
            self.out_var.set(path)

    # -- run control -------------------------------------------------------------
    def start(self):
        if self.running:
            return
        if not (self.ffmpeg and self.ffprobe):
            messagebox.showerror("FFmpeg missing", "ffmpeg / ffprobe were not found in PATH.")
            return
        if not self.info or not self.src:
            messagebox.showerror("No video", "Please select a video file first.")
            return
        ranges, errors = parse_ranges(self.ts_text.get("1.0", "end"), self.duration)
        if errors:
            messagebox.showerror("Check your timestamps", "\n\n".join(errors))
            return
        out = Path(self.out_var.get()).expanduser()
        if out.suffix.lower() != ".mkv":
            messagebox.showerror("Output", "Output file must end in .mkv (keeps every audio/subtitle format safely).")
            return
        if out.resolve() == self.src.resolve():
            messagebox.showerror("Output", "Output file must be different from the original video.")
            return
        if out.exists() and not messagebox.askyesno("Overwrite?", f"{out.name} already exists. Overwrite it?"):
            return
        mode = self.mode_var.get()
        if mode == "reencode":
            enc = subprocess.run([self.ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
                                 creationflags=NO_WINDOW).stdout
            main = build_maps(self.info)[1]
            need = "libx264" if main and main.get("codec_name") == "h264" else "libx265"
            if need not in enc:
                messagebox.showerror(
                    "Encoder missing",
                    f"Your FFmpeg build has no {need} encoder.\n\n"
                    "On Fedora install the full FFmpeg from RPM Fusion:\n"
                    "  sudo dnf swap ffmpeg-free ffmpeg --allowerasing")
                return

        self.cancel_evt.clear()
        self.running = True
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.bar["value"] = 0
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        args = (self.src, ranges, mode, self.preset_var.get(), int(self.crf_var.get()), out)
        self.thread = threading.Thread(target=self.worker, args=args, daemon=True)
        self.thread.start()

    def cancel(self):
        self.cancel_evt.set()
        if self.proc:
            try:
                self.proc.terminate()
            except OSError:
                pass
        self.status_var.set("Cancelling…")

    def on_close(self):
        if self.running:
            if not messagebox.askyesno("Quit", "A job is running. Cancel it and quit?"):
                return
            self.cancel()
            if self.thread:
                self.thread.join(timeout=8)
        self.root.destroy()

    # -- worker thread -----------------------------------------------------------
    def say(self, kind, *payload):
        self.q.put((kind, *payload))

    def run_ffmpeg(self, cmd, cwd, seg_dur=None, base=0.0, span=100.0, label=""):
        self.proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        errors = []
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            key, _, val = line.partition("=")
            if key in PROGRESS_KEYS or key.startswith("stream_"):
                if key == "out_time_us" and seg_dur:
                    try:
                        frac = min(1.0, int(val) / 1_000_000 / seg_dur)
                        self.say("progress", base + span * frac, f"{label}  {frac * 100:.0f}%")
                    except ValueError:
                        pass
            else:
                errors.append(line)
        rc = self.proc.wait()
        self.proc = None
        if self.cancel_evt.is_set():
            raise Cancelled()
        return rc, errors

    def worker(self, src, ranges, mode, preset, crf, out):
        tmp = None
        base_cmd = [self.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                    "-nostats", "-progress", "pipe:1"]
        try:
            tmp = Path(tempfile.mkdtemp(prefix="compilation_tmp_", dir=src.parent))
            self.say("log", f"Temporary folder: {tmp}")
            maps, main = build_maps(self.info)
            vargs = video_args(main, mode, preset, crf)
            sargs = subtitle_args(self.info)
            if sargs:
                self.say("log", "Note: mov_text subtitles (MP4) will be converted to SRT for MKV.")
            n = len(ranges)
            segs = []

            for i, (a, b) in enumerate(ranges, 1):
                if self.cancel_evt.is_set():
                    raise Cancelled()
                seg = tmp / f"seg_{i:03d}.mkv"
                dur = b - a
                self.say("log", f"[{i}/{n}] Cutting {fmt_time(a)} → {fmt_time(b)}")
                cmd = (base_cmd + ["-ss", f"{a:.3f}", "-i", str(src), "-t", f"{dur:.3f}"]
                       + maps + vargs + sargs
                       + ["-map_metadata", "0", "-map_chapters", "-1",
                          "-avoid_negative_ts", "make_zero", str(seg)])
                rc, errs = self.run_ffmpeg(cmd, tmp, dur, base=(i - 1) / n * 100, span=100 / n,
                                           label=f"Segment {i}/{n}")
                if rc != 0:
                    raise RuntimeError(f"FFmpeg failed on segment {i}:\n" + "\n".join(errs[-15:]))
                segs.append(seg.name)

            self.say("progress", 99, "Joining segments…")
            self.say("log", "Joining all segments…")
            (tmp / "list.txt").write_text("".join(f"file '{s}'\n" for s in segs), encoding="utf-8")
            joined = tmp / "joined.mkv"

            def concat(map_args):
                cmd = (base_cmd + ["-f", "concat", "-safe", "0", "-i", "list.txt"]
                       + map_args + ["-c", "copy", "-map_metadata", "0", "-map_chapters", "-1", str(joined)])
                return self.run_ffmpeg(cmd, tmp)

            rc, errs = concat(["-map", "0"])
            if rc != 0:
                self.say("log", "Retrying join without attachments (fonts)…")
                rc, errs = concat(["-map", "0:v", "-map", "0:a", "-map", "0:s?"])
            if rc != 0:
                raise RuntimeError("FFmpeg failed while joining:\n" + "\n".join(errs[-15:]))

            if out.exists():
                out.unlink()
            shutil.move(str(joined), str(out))
            self.say("progress", 100, "Done.")
            self.say("done", True, f"Finished!\nSaved to:\n{out}")
        except Cancelled:
            self.say("done", False, "Cancelled. Temporary files were removed.")
        except Exception as e:  # noqa: BLE001
            self.say("done", False, f"Error:\n{e}")
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
                self.say("log", "Temporary folder deleted.")

    # -- queue polling (GUI thread) -----------------------------------------------
    def poll(self):
        try:
            while True:
                kind, *p = self.q.get_nowait()
                if kind == "log":
                    self.log(p[0])
                elif kind == "progress":
                    self.bar["value"] = p[0]
                    self.status_var.set(p[1])
                elif kind == "done":
                    ok, msg = p
                    self.running = False
                    self.start_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                    self.status_var.set("Done." if ok else "Stopped.")
                    self.log(msg)
                    (messagebox.showinfo if ok else messagebox.showwarning)("Scene Compiler", msg)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
