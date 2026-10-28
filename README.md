# Trim - Merge - Compile

A small desktop app (Python + Tkinter + FFmpeg) that cuts selected scenes out of a movie and joins them into **one compilation video**, while keeping **every audio track and every subtitle track** intact.

You pick a video, type your timestamps (for example `00:05:32 to 00:08:56`), and click one button. Your original file is never touched.

---

## Table of contents

1. [What it can do](#1-what-it-can-do)
2. [Prerequisites](#2-prerequisites)
3. [How to use it](#3-how-to-use-it)
4. [How it works under the hood](#4-how-it-works-under-the-hood)
5. [Limitations](#5-limitations)
6. [Troubleshooting](#6-troubleshooting)
7. [License](#7-license)

---

## 1. What it can do

- **Simple GUI.** Pick a file, paste timestamps, click *Create compilation*. No command line needed.
- **Full file inspection.** On opening a video it shows container format, duration, and every stream: video codec/profile, resolution, pixel format (bit depth), frame rate, HDR type, bitrate, audio codecs/languages/channel layouts, subtitle formats/languages, and default/forced flags, each with its stream index.
- **Flexible timestamp input.** One range per line or comma-separated. Accepts `HH:MM:SS`, `MM:SS`, fractional seconds, and `to`, `-`, `–`, `until`, `->` as separators.
- **Order is preserved.** Scenes are joined in exactly the order you list them.
- **Keeps all audio and subtitles.** Every audio stream and every subtitle stream (plus embedded fonts) is copied as-is, never re-encoded and never burned into the picture. Languages, titles, and default/forced flags carry over.
- **Two cutting modes:**
  - **Accurate (re-encode video):** frame-exact cuts. Video is re-encoded with the *same codec family*, pixel format (e.g. 10-bit) and colour/HDR tags as the source. Audio and subtitles are still copied.
  - **Fast stream copy:** nothing is re-encoded. Finishes in seconds and the video is bit-identical, but cuts snap to keyframes.
- **Adjustable quality.** CRF and encoder preset controls for Accurate mode.
- **Safe by design.** All work happens in a temporary folder created next to the video. It is deleted afterwards, even on error or cancel. The final file is only written to your chosen location when everything succeeded.
- **Progress and cancel.** Progress bar with per-segment percentage and encoding speed, a log window, and a Cancel button.
- **Cross-platform.** Works on Linux (tested target: Fedora) and Windows.

---

## 2. Prerequisites

| Requirement | Why |
|---|---|
| **Python 3.8+** | Runs the app. Only the standard library is used (no `pip install` needed). |
| **Tkinter** | The GUI toolkit (bundled with Python). |
| **FFmpeg** and **ffprobe** in your `PATH` | Does the actual cutting, encoding and joining. |
| **x265/x264 encoders in your FFmpeg build** | Only needed for *Accurate* mode. |
| **Free disk space** | Roughly 2x the size of the final output, in the folder containing your video (segments, then the joined file). |

### Fedora

```bash
# Tkinter
sudo dnf install python3-tkinter

# FFmpeg: Fedora's default "ffmpeg-free" has NO x265/x264 encoders.
# Enable RPM Fusion, then swap to the full FFmpeg:
sudo dnf install https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm
sudo dnf swap ffmpeg-free ffmpeg --allowerasing
```

### Debian / Ubuntu

```bash
sudo apt install python3-tk ffmpeg
```

### Windows

1. Install Python from <https://www.python.org/downloads/> (Tkinter is included).
2. Install FFmpeg, then **restart your terminal**:
   ```
   winget install Gyan.FFmpeg
   ```

### Verify

```bash
python3 --version
ffmpeg -version
ffprobe -version
ffmpeg -encoders | grep -E "libx265|libx264"    # Linux; needed for Accurate mode
```

---

## 3. How to use it

### Start the app

```bash
python3 scene_compiler.py        # Linux
python scene_compiler.py         # Windows (or just double-click the file)
```

### Step by step

**1. Select the video.** Click **Browse…** and choose your movie. The stream list appears so you can confirm audio and subtitle tracks.

**2. Enter timestamps.** One range per line, or separated by commas, in the order you want them joined:

```
00:00:34 to 00:00:57
00:05:20 to 00:08:00, 00:17:50 to 00:22:45
01:33:48-01:34:04
```

Accepted formats: `HH:MM:SS`, `MM:SS`, `HH:MM:SS.mmm`.
Ranges whose end is before the start are rejected. An end time past the end of the video is clamped to the end.

**3. Choose options.**

| Option | Meaning |
|---|---|
| **Accurate cuts** | Frame-exact. Re-encodes video only. Slower. |
| **Fast stream copy** | No re-encode. Instant, but each cut may start a little early (see [Limitations](#5-limitations)). |
| **CRF** | Quality for Accurate mode. Lower = better quality, bigger file. |
| **Preset** | Encoder effort. Slower = smaller file at the same quality, longer wait. |
| **Output file** | Where to save the result. Defaults to `<video name>_compilation.mkv` next to the video. Must end in `.mkv`. |

**4. Click *Create compilation*.** Watch the progress bar and log. When done, a message shows the output path. Use **Cancel** at any time; temporary files are removed.

### Recommended settings

| Goal | Mode | CRF | Preset |
|---|---|---|---|
| Absolute original quality, exact cut points not critical | Fast stream copy | n/a | n/a |
| Frame-exact cuts, maximum practical quality | Accurate | 12-14 | `slow` |
| Frame-exact cuts, balanced size/time | Accurate | 18 | `medium` |
| Quick preview | Fast stream copy, or Accurate with `veryfast` | 20-23 | `veryfast` |

**Notes on quality and size**

- Audio and subtitles are always bit-for-bit copies. Only video quality depends on your settings.
- Going below CRF ~12 only grows the file; the source is already lossy, so no detail is recovered.
- At a fixed CRF, presets change time and file size far more than visible quality. `slow` is typically ~3-6% smaller than `medium` but about twice as long.
- Re-encoding already heavily compressed sources at a low CRF can produce a file *larger* than the matching share of the original.
- Accurate mode on 4K 10-bit uses all CPU cores and roughly 2-4 GB RAM; expect long encode times. Stream copy uses almost no CPU or RAM.

---

## 4. How it works under the hood

The app is a single file, `scene_compiler.py`, and is a GUI wrapper that builds and runs FFmpeg commands. It has four layers: probing, planning, cutting, and joining.

### 4.1 Probing the file (ffprobe)

When you select a video, the app runs:

```bash
ffprobe -v error -show_format -show_streams -of json <file>
```

The JSON is parsed to:

- show the stream summary (`describe()`),
- get the **duration** (used to validate/clamp your timestamps),
- decide **which streams to map** and **how to encode the video**.

HDR is detected from `color_transfer` (`smpte2084` = HDR10/PQ, `arib-std-b67` = HLG); Dolby Vision is detected from side data.

### 4.2 Parsing timestamps

A regular expression finds every `TIME <separator> TIME` pair in the text box. The text left over after removing all matches (and commas/whitespace) must be empty, otherwise the app reports exactly what it could not understand instead of silently skipping it. Times are converted to seconds, validated (end > start, start inside the video) and kept **in the order typed**.

### 4.3 Choosing the streams (`build_maps`)

Instead of guessing indexes, the app reads them from ffprobe and builds explicit `-map 0:N` arguments for:

- the main video stream (the first non-cover-art video stream),
- **all** audio streams,
- **all** subtitle streams,
- **all** attachment streams (typically fonts used by ASS/SSA subtitles).

Per-stream languages, titles and default/forced dispositions are preserved because streams are mapped, not rebuilt. `-map_metadata 0` copies global metadata; chapters are dropped (`-map_chapters -1`) because chapter times would be wrong after cutting.

### 4.4 Cutting each scene

For each range the app runs one FFmpeg process (arguments shortened):

```bash
ffmpeg -ss <start> -i <input> -t <duration> \
       -map 0:0 -map 0:1 -map 0:2 ... \
       <video args> \
       -map_metadata 0 -map_chapters -1 -avoid_negative_ts make_zero \
       seg_001.mkv
```

Key points:

- **`-ss` before `-i`** uses fast input seeking. Combined with re-encoding, FFmpeg decodes from the previous keyframe and discards frames up to the exact start, so the cut is **frame-accurate**.
- **`-t <duration>`** (not `-to`) avoids ambiguity about whether times are relative to the seek point.
- **`-avoid_negative_ts make_zero`** shifts timestamps so each segment starts at 0.
- Audio and subtitles always use `-c copy`.

**Video arguments (`video_args`)**

*Fast stream copy:* `-c copy` for everything.

*Accurate:* `-c copy` for everything, then the video overridden with:

```bash
-c:v libx265     # or libx264 if the source is H.264; HEVC and anything else -> libx265
-preset <preset> -crf <crf>
-pix_fmt <source pix_fmt>            # e.g. yuv420p10le keeps 10-bit
-color_primaries / -color_trc / -colorspace / -color_range <from source>
```

Copying the source pixel format and colour tags keeps bit depth and HDR signalling. FFmpeg's libx265 wrapper normally also forwards mastering-display and content-light-level metadata when present in the decoded frames. Dolby Vision RPU data cannot survive a re-encode.

**Why stream copy cuts can be off.** Compressed video only contains full pictures at *keyframes*; the frames between depend on them. With `-c copy`, FFmpeg can only start at a keyframe, so the clip may begin earlier than requested (up to a few seconds or more, depending on the file's keyframe spacing) and the end is also approximate. That's why Accurate mode re-encodes.

### 4.5 Joining the segments

Segments are listed in `list.txt` and joined with FFmpeg's **concat demuxer** without re-encoding:

```bash
ffmpeg -f concat -safe 0 -i list.txt -map 0 -c copy \
       -map_metadata 0 -map_chapters -1 joined.mkv
```

```
file 'seg_001.mkv'
file 'seg_002.mkv'
...
```

- This works safely because all segments come from the same source with identical codec parameters (in Accurate mode they were all encoded with the same settings).
- Relative file names are used and the process runs inside the temp folder, so spaces/special characters in your path never cause quoting problems.
- If joining fails with attachments mapped, the app retries without the font attachments (`-map 0:v -map 0:a -map 0:s?`).
- The result is written to `joined.mkv` inside the temp folder and only **moved** to your chosen output path on success, so you never end up with a half-written output file.

### 4.6 Temporary folder and cleanup

```
<folder of your video>/
├── Movie.mkv                      <- never modified
├── compilation_tmp_ab12cd/        <- created for the job (tempfile.mkdtemp)
│   ├── seg_001.mkv
│   ├── seg_002.mkv
│   ├── list.txt
│   └── joined.mkv
└── Movie_compilation.mkv          <- final result (moved out on success)
```

The temp folder is removed in a `finally:` block, so it is deleted on success, on error and on cancel.

### 4.7 GUI, threading and progress

- The UI is **Tkinter/ttk**. FFmpeg work runs in a **background thread** so the window never freezes.
- The worker thread talks to the UI through a `queue.Queue`; the UI polls it every 100 ms (Tkinter widgets must only be touched from the main thread).
- FFmpeg is started with `-progress pipe:1 -nostats -loglevel error`. The app reads the `out_time_us=` lines, divides by the segment duration for a per-segment percentage, and maps that to the overall bar. Any other output line is treated as an error message and shown if the command fails.
- **Cancel** sets a `threading.Event` and calls `terminate()` on the running FFmpeg process; the worker raises an internal `Cancelled` exception and cleanup runs.
- Before an Accurate job the app runs `ffmpeg -encoders` to confirm `libx265`/`libx264` exist and shows installation help if not.
- On Windows, the subprocess flag `CREATE_NO_WINDOW` stops console windows from flashing up.

### 4.8 Code map

| Function / class | Role |
|---|---|
| `parse_ranges()` | Turns the text box into validated `(start, end)` second pairs |
| `probe()` / `describe()` | ffprobe call and human-readable stream summary |
| `build_maps()` | Builds the `-map` list and finds the main video stream |
| `video_args()` | Chooses copy vs. encode arguments, keeps pix_fmt and HDR tags |
| `App.worker()` | The full job: temp folder → cut → join → move → cleanup |
| `App.run_ffmpeg()` | Runs FFmpeg, parses progress, supports cancel |
| `App.poll()` | Applies worker messages to the GUI |

---

## 5. Limitations

- **Output is always `.mkv`** (it can hold every audio/subtitle format safely).
- **Dolby Vision** metadata is lost in Accurate mode (HDR10/HLG are kept). Stream copy keeps it.
- **Chapters** are removed; **embedded cover art** and **data streams** are skipped.
- **Stream copy cuts snap to keyframes**; use Accurate mode when exact cut points matter.
- If the source video codec is not H.264 or HEVC (for example AV1 or VP9), Accurate mode re-encodes to HEVC (libx265).
- Accurate mode is CPU-heavy and slow on 4K. Hardware encoders (NVENC/QSV/VAAPI) are not supported.
- macOS has not been tested.

---

## 6. Troubleshooting

| Problem | Fix |
|---|---|
| "FFmpeg not found" | Install FFmpeg and make sure `ffmpeg` and `ffprobe` work in a terminal; restart the app/terminal. |
| "Encoder missing" (Fedora) | `sudo dnf swap ffmpeg-free ffmpeg --allowerasing` (needs RPM Fusion). |
| `No module named tkinter` | Fedora: `sudo dnf install python3-tkinter`. Debian/Ubuntu: `sudo apt install python3-tk`. |
| Cut starts earlier than expected | You are in stream copy mode; switch to Accurate. |
| Output bigger than expected | Raise the CRF (e.g. 20-24) or use a slower preset. |
| Window sluggish / PC slow during encode | Normal for Accurate mode: it uses all cores. Use a faster preset or stream copy. |
| Subtitles missing in a player | Make sure subtitle track selection is enabled in your player; the tracks are present in the file. Confirm with `ffprobe` on the output. |
| Job fails | Read the error in the log box; it contains the last lines from FFmpeg. |

---

## 7. License

Released under the MIT License. See [LICENSE](LICENSE).

Use this tool only with video files you have the right to edit.
# trimmerge-compy
# trimmerge-compy
