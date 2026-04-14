"""
audio_qc_basics_UI_standalone.py — Panel + Pyodide app (self-contained)
========================================================================
All QC logic from audio_qc_basics.py is inlined here so that
`panel convert` produces a single HTML with no external zip dependency.
The resulting HTML works when double-clicked (no HTTP server needed).

Convert to standalone HTML:
    panel convert audio_qc_basics_UI_standalone.py --to pyodide --out . --requirements matplotlib

Then open audio_qc_basics_UI_standalone.html in Chrome / Edge.
"""

# ══════════════════════════════════════════════════════════════════════════════
# audio_qc_basics.py — inlined
# ══════════════════════════════════════════════════════════════════════════════

import re
import struct
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter

# ── Audio extensions ───────────────────────────────────────────────────────────

AUDIO_EXTENSIONS = {".wav", ".flac", ".aif", ".aiff"}

# ── Timestamp patterns ─────────────────────────────────────────────────────────
# Mirrors ecosound timestamp_formats.json.
# Pure stdlib (re + datetime) — Pyodide-compatible.

_PATTERNS = [
    # (name,  regex,  strptime_format)
    ("AMAR_v0",              r"\.[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}\.", ".%Y-%m-%d-%H-%M-%S."),
    ("AMAR_v1",              r"_[0-9]{8}T[0-9]{6}\.[0-9]{3}Z\.",                            "_%Y%m%dT%H%M%S.%fZ."),
    ("AMARS_v2",             r"\.[0-9]{8}T[0-9]{6}Z\.",                                     ".%Y%m%dT%H%M%SZ."),
    ("SOUNDTRAPS",           r"\.[0-9]{12}\.",                                               ".%y%m%d%H%M%S."),
    ("SOUNDTRAPS_UAberdeen", r"_[0-9]{12}\.",                                                "_%y%m%d%H%M%S."),
    ("MARU",                 r"_[0-9]{8}_[0-9]{6}\.",                                       "_%Y%m%d_%H%M%S."),
    ("MARU_with_ms",         r"_[0-9]{8}_[0-9]{6}_[0-9]{3}\.",                              "_%Y%m%d_%H%M%S_%f."),
    ("MARU_variant",         r"_[0-9]{6}_[0-9]{6}_",                                        "_%y%m%d_%H%M%S_"),
    ("PMEL",                 r"-[0-9]{6}-[0-9]{6}\.",                                       "-%y%m%d-%H%M%S."),
    ("SAMS",                 r"_[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}\.",  "_%Y-%m-%d_%H-%M-%S."),
    ("PAMGuard",             r"_[0-9]{8}_[0-9]{6}Z\.",                                      "_%Y%m%d_%H%M%SZ."),
    ("NOAA_SOUNDTRAPS_v2",   r"\.[0-9]{14}\.",                                              ".%Y%m%d%H%M%S."),
    ("Loggerhead",           r"[0-9]{8}T[0-9]{6}\.",                                        "%Y%m%dT%H%M%S."),
]

_COMBINED_RE = re.compile("|".join(p[1] for p in _PATTERNS))
_TIME_FORMATS = [p[2] for p in _PATTERNS]


def filename_to_datetime(filename):
    """
    Parse a datetime from an audio filename.

    Supports 13 common PAM recorder naming conventions (AMAR, SoundTrap,
    MARU, PMEL, SAMS, PAMGuard, Loggerhead, …).

    Pure stdlib (re + datetime) — Pyodide-compatible.

    Parameters
    ----------
    filename : str or Path

    Returns
    -------
    datetime

    Raises
    ------
    ValueError if no known pattern matches.
    """
    name = Path(filename).name
    match = _COMBINED_RE.search(name)
    if match is None:
        raise ValueError(f"No timestamp pattern found in: {name}")
    datestr = match[0]
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(datestr, fmt)
        except ValueError:
            continue
    raise ValueError(f"Could not parse timestamp '{datestr}' in: {name}")


# ── Audio header parsers (from bytes) — Pyodide-compatible ────────────────────

def _parse_80bit_float(b):
    """Convert 10-byte IEEE 754 80-bit extended float to Python float (for AIF)."""
    exponent = ((b[0] & 0x7F) << 8) | b[1]
    mantissa = int.from_bytes(b[2:10], "big")
    if exponent == 0 and mantissa == 0:
        return 0.0
    value = mantissa * (2.0 ** (exponent - 16383 - 63))
    return -value if (b[0] & 0x80) else value


def _wav_info_from_bytes(data, file_size=None):
    """Return (sample_rate, duration_sec) from WAV header bytes."""
    try:
        if data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
            return None, None
        offset = 12
        sample_rate = channels = bits = None
        while offset + 8 <= len(data):
            chunk_id = data[offset:offset + 4]
            chunk_size = struct.unpack("<I", data[offset + 4:offset + 8])[0]
            if chunk_id == b"fmt ":
                fmt = data[offset + 8:offset + 8 + chunk_size]
                if len(fmt) >= 16:
                    channels = struct.unpack("<H", fmt[2:4])[0]
                    sample_rate = struct.unpack("<I", fmt[4:8])[0]
                    bits = struct.unpack("<H", fmt[14:16])[0]
            elif chunk_id == b"data":
                if sample_rate and channels and bits:
                    bps = channels * bits // 8
                    data_bytes = chunk_size if chunk_size > 0 else (
                        (file_size - offset - 8) if file_size else None
                    )
                    if data_bytes:
                        return sample_rate, (data_bytes // bps) / sample_rate
                break
            offset += 8 + chunk_size
    except Exception:
        pass
    return None, None


def _flac_info_from_bytes(data):
    """Return (sample_rate, duration_sec) from FLAC header bytes."""
    try:
        if data[0:4] != b"fLaC":
            return None, None
        if (data[4] & 0x7F) != 0:   # first block must be STREAMINFO (type 0)
            return None, None
        si = data[8:]                 # STREAMINFO payload starts at byte 8
        if len(si) < 18:
            return None, None
        sample_rate = (si[10] << 12) | (si[11] << 4) | (si[12] >> 4)
        total_samples = (
            ((si[13] & 0x0F) << 32) | (si[14] << 24) |
            (si[15] << 16) | (si[16] << 8) | si[17]
        )
        duration = total_samples / sample_rate if sample_rate and total_samples else None
        return sample_rate, duration
    except Exception:
        return None, None


def _aif_info_from_bytes(data):
    """Return (sample_rate, duration_sec) from AIF/AIFF header bytes."""
    try:
        if data[0:4] != b"FORM" or data[8:12] not in (b"AIFF", b"AIFC"):
            return None, None
        offset = 12
        while offset + 8 <= len(data):
            chunk_id = data[offset:offset + 4]
            chunk_size = struct.unpack(">I", data[offset + 4:offset + 8])[0]
            if chunk_id == b"COMM":
                comm = data[offset + 8:]
                if len(comm) < 18:
                    break
                num_frames = struct.unpack(">I", comm[2:6])[0]
                sample_rate = int(_parse_80bit_float(comm[8:18]))
                duration = num_frames / sample_rate if sample_rate and num_frames else None
                return sample_rate, duration
            offset += 8 + chunk_size + (chunk_size % 2)  # AIF pads chunks to even size
    except Exception:
        pass
    return None, None


def get_audio_info_from_bytes(header_bytes, filename, file_size=None):
    """
    Return (sample_rate_hz, duration_sec) from raw header bytes.

    Works without file access — suitable for Pyodide/browser where only
    the first N bytes of each file are available via the File API.
    Supports WAV, FLAC, AIF/AIFF using pure stdlib (struct only).
    """
    ext = Path(filename).suffix.lower()
    if ext == ".wav":
        return _wav_info_from_bytes(header_bytes, file_size)
    if ext == ".flac":
        return _flac_info_from_bytes(header_bytes)
    if ext in (".aif", ".aiff"):
        return _aif_info_from_bytes(header_bytes)
    return None, None


# ── Audio header readers (from file path) ─────────────────────────────────────

def get_audio_info(filepath):
    """
    Return (sample_rate_hz, duration_sec) for a local audio file.
    Uses get_audio_info_from_bytes for WAV/FLAC/AIF (no external deps).
    Falls back to soundfile for any format it cannot parse.
    """
    try:
        with open(filepath, "rb") as f:
            header = f.read(512)
        sr, dur = get_audio_info_from_bytes(header, filepath, Path(filepath).stat().st_size)
        if sr is not None:
            return sr, dur
    except Exception:
        pass
    try:
        import soundfile as sf
        info = sf.info(filepath)
        return info.samplerate, info.duration
    except Exception:
        return None, None


# ── File listing ───────────────────────────────────────────────────────────────

def list_audio_files(directory):
    """Return sorted list of audio file Paths in *directory* (non-recursive)."""
    return sorted(
        f for f in Path(directory).iterdir()
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
    )


# ── QC engine ─────────────────────────────────────────────────────────────────

def _compute_qc_stats(records, total_bytes):
    """
    Compute QC statistics from a list of file record dicts.

    Each record must have: file, start (datetime), duration_sec, end, sample_rate.
    Returns the standard results dict used by print_qc_report.
    """
    if not records:
        return {"error": "No files with parseable timestamps found."}

    n_files = len(records)
    t_start_all = records[0]["start"]
    t_end_all = records[-1]["end"] or records[-1]["start"]

    # File durations
    durations = [r["duration_sec"] for r in records if r["duration_sec"] is not None]
    if durations:
        typical_dur = Counter([round(d, 1) for d in durations]).most_common(1)[0][0]
        unusual_durations = [
            r["file"] for r in records
            if r["duration_sec"] is not None and abs(round(r["duration_sec"], 1) - typical_dur) > 1.0
        ]
    else:
        typical_dur = None
        unusual_durations = []

    # Inter-file gaps
    gaps_sec, gap_events = [], []
    for i in range(1, len(records)):
        prev_end = records[i - 1]["end"]
        if prev_end is None:
            continue
        gap = (records[i]["start"] - prev_end).total_seconds()
        gaps_sec.append(gap)
        gap_events.append((gap, records[i - 1]["file"], records[i]["file"]))

    if gaps_sec:
        typical_gap = Counter([round(g, 1) for g in gaps_sec]).most_common(1)[0][0]
        gap_threshold = typical_gap * 2 + 1
        unusual_gaps = [
            (g, f1, f2) for g, f1, f2 in gap_events
            if g < -0.5 or g > gap_threshold
        ]
    else:
        typical_gap = None
        unusual_gaps = []

    # Sampling frequency
    sample_rates = [r["sample_rate"] for r in records if r["sample_rate"] is not None]
    if sample_rates:
        dominant_sr = Counter(sample_rates).most_common(1)[0][0]
        inconsistent_sr = [
            r["file"] for r in records
            if r["sample_rate"] is not None and r["sample_rate"] != dominant_sr
        ]
    else:
        dominant_sr = None
        inconsistent_sr = []

    return {
        "n_files": n_files,
        "dataset_size_gb": total_bytes / 1e9,
        "start_datetime": t_start_all,
        "end_datetime": t_end_all,
        "total_span": t_end_all - t_start_all,
        "sampling_frequency_hz": dominant_sr,
        "inconsistent_sample_rates": inconsistent_sr,
        "typical_file_duration_sec": typical_dur,
        "unusual_durations": unusual_durations,
        "duty_cycle": {"on_sec": typical_dur, "off_sec": typical_gap},
        "unusual_gaps": unusual_gaps,
    }


def run_qc(directory, verbose=False):
    """
    List audio files in *directory* and run basic QC checks.

    Parameters
    ----------
    directory : str or Path
    verbose : bool
        Print the QC report to the terminal (default: False).

    Returns
    -------
    dict — see _compute_qc_stats for key descriptions.
    """
    files = list_audio_files(directory)
    if not files:
        return {"error": f"No audio files found in {directory}"}

    records = []
    total_bytes = 0
    for f in files:
        total_bytes += f.stat().st_size
        try:
            t_start = filename_to_datetime(f)
        except ValueError as e:
            if verbose:
                print(f"  [skip] {f.name}: {e}")
            continue
        sr, dur = get_audio_info(f)
        records.append({
            "file": f.name,
            "start": t_start,
            "duration_sec": dur,
            "end": t_start + timedelta(seconds=dur) if dur is not None else None,
            "sample_rate": sr,
        })

    results = _compute_qc_stats(records, total_bytes)
    if verbose:
        print_qc_report(results)
    return results


# ── Pretty-print report ────────────────────────────────────────────────────────

def print_qc_report(results):
    if "error" in results:
        print(f"ERROR: {results['error']}")
        return

    SEP = "=" * 62
    print(SEP)
    print("  AUDIO FILE QC REPORT")
    print(SEP)
    print(f"  Number of files     : {results['n_files']}")
    print(f"  Dataset size        : {results['dataset_size_gb']:.3f} GB")
    print(f"  Start datetime      : {results['start_datetime']}")
    print(f"  End datetime        : {results['end_datetime']}")
    print(f"  Total span          : {results['total_span']}")
    print(f"  Sampling frequency  : {results['sampling_frequency_hz']} Hz")
    print()

    print("  DUTY CYCLE")
    dc = results["duty_cycle"]
    if dc["on_sec"] is not None:
        print(f"    Recording ON      : {dc['on_sec']:.1f} s  (most common file duration)")
    if dc["off_sec"] is not None:
        print(f"    Recording OFF     : {dc['off_sec']:.1f} s  (most common gap between files)")
    print()

    print("  INCONSISTENCIES")

    # Sampling frequency
    print(f"    Sampling frequency (most common: {results['sampling_frequency_hz']} Hz):")
    if results["inconsistent_sample_rates"]:
        for f in results["inconsistent_sample_rates"]:
            print(f"      {f}")
    else:
        print("      None")

    # File durations
    print(f"    File duration (most common: {results['typical_file_duration_sec']} s):")
    if results["unusual_durations"]:
        for f in results["unusual_durations"]:
            print(f"      {f}")
    else:
        print("      None")

    # Gaps between files
    print(f"    Gaps between files (most common: {results['duty_cycle']['off_sec']} s):")
    if results["unusual_gaps"]:
        for gap_sec, f1, f2 in results["unusual_gaps"]:
            label = "OVERLAP" if gap_sec < 0 else "GAP"
            print(f"      [{label}] {gap_sec:+.1f} s  between {f1}")
            print(f"               and {f2}")
    else:
        print("      None")

    print(SEP)


# ══════════════════════════════════════════════════════════════════════════════
# UI — Panel + Pyodide
# ══════════════════════════════════════════════════════════════════════════════

import io
import contextlib
import matplotlib
try:
    matplotlib.use("agg")   # no-op in Pyodide (Panel sets the backend)
except Exception:
    pass
import matplotlib.pyplot as plt
import panel as pn

try:
    from pyodide.ffi import create_proxy
    import js
    IN_PYODIDE = True
except ImportError:
    IN_PYODIDE = False

pn.extension(sizing_mode="stretch_width")

# ── Widgets ────────────────────────────────────────────────────────────────────

status = pn.pane.Markdown("*Ready. Click **Browse...** to select a directory.*")

files_display = pn.widgets.TextAreaInput(
    name="Files",
    height=500,
    placeholder="Audio files will be listed here...",
)

results_display = pn.widgets.TextAreaInput(
    name="QC Report",
    height=500,
    placeholder="QC results will appear here...",
)

# Duty cycle pie chart — keep the initial figure open so Panel can render it
_init_fig, _ax = plt.subplots(figsize=(3.5, 3.5))
_ax.set_visible(False)
_init_fig.patch.set_visible(False)
duty_chart_pane = pn.pane.Matplotlib(_init_fig, tight=True, width=300, height=300)

# Inconsistency indicator
inconsistency_indicator = pn.indicators.BooleanStatus(
    value=False, color="success", width=50, height=50,
)
inconsistency_label = pn.pane.Markdown("*Run QC to check for inconsistencies.*")

# ── Helpers ────────────────────────────────────────────────────────────────────

def _report_to_str(results):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_qc_report(results)
    return buf.getvalue()


def _update_visuals(results):
    """Update pie chart and inconsistency indicator from a QC results dict."""
    if "error" in results:
        return

    # ── Duty cycle pie chart ───────────────────────────────────────────────────
    dc = results["duty_cycle"]
    on_sec  = dc.get("on_sec")  or 0
    off_sec = dc.get("off_sec") or 0
    plt.close("all")   # release the previous figure before creating a new one
    fig, ax = plt.subplots(figsize=(3.5, 3.5))
    if on_sec + off_sec > 0:
        ax.pie(
            [on_sec, off_sec],
            labels=[f"ON\n{on_sec:.0f} s", f"OFF\n{off_sec:.0f} s"],
            colors=["#2196F3", "#B0BEC5"],
            autopct="%1.1f%%",
            startangle=90,
        )
    ax.set_title("Duty Cycle", fontsize=13, pad=12)
    duty_chart_pane.object = fig
    # Do NOT close fig here — Panel renders lazily and needs the figure to remain open

    # ── Inconsistency indicator ────────────────────────────────────────────────
    has_issues = bool(
        results.get("unusual_durations") or
        results.get("unusual_gaps") or
        results.get("inconsistent_sample_rates")
    )
    inconsistency_indicator.value = True
    if has_issues:
        inconsistency_indicator.color = "danger"
        inconsistency_label.object = "**Inconsistencies detected** — see QC report for details."
    else:
        inconsistency_indicator.color = "success"
        inconsistency_label.object = "**No inconsistencies detected.**"


# ── Callbacks + picker widget (one block per mode) ────────────────────────────

if IN_PYODIDE:
    # Browser mode: JS reads first 512 bytes of each file for header parsing,
    # passes path + size + base64 header to Python via tab-separated lines.
    def _on_files_selected(file_data_str, dir_name):
        import base64

        lines = [l for l in file_data_str.split("\n") if l]
        if not lines:
            status.object = "*No files received.*"
            return

        records = []
        total_bytes = 0
        skipped = 0
        for line in lines:
            parts = line.split("\t", 2)
            rel_path = parts[0]
            file_size = int(parts[1]) if len(parts) > 1 and parts[1] else None
            header_b64 = parts[2] if len(parts) > 2 else ""

            fname = rel_path.split("/")[-1]
            if file_size:
                total_bytes += file_size

            try:
                t_start = filename_to_datetime(fname)
            except ValueError:
                skipped += 1
                continue

            sr, dur = None, None
            if header_b64:
                try:
                    header_bytes = base64.b64decode(header_b64)
                    sr, dur = get_audio_info_from_bytes(header_bytes, fname, file_size)
                except Exception:
                    pass

            records.append({
                "file": fname,
                "start": t_start,
                "duration_sec": dur,
                "end": t_start + timedelta(seconds=dur) if dur is not None else None,
                "sample_rate": sr,
            })

        files_display.value = "\n".join(r["file"] for r in records)
        results = _compute_qc_stats(records, total_bytes)
        results_display.value = _report_to_str(results)
        _update_visuals(results)
        if "error" in results:
            status.object = f"*{results['error']}*"
        else:
            status.object = f"**{results['n_files']} file(s)** found in `{dir_name}/`"

    js.window.pySetFiles = create_proxy(_on_files_selected)

    picker_widget = pn.pane.HTML(
        """
        <input type="file" id="dir-input" webkitdirectory multiple style="display:none">
        <button
            onclick="document.getElementById('dir-input').click()"
            style="padding:10px 22px; background:#0072B5; color:white; border:none;
                   border-radius:6px; cursor:pointer; font-size:15px; font-family:sans-serif;
                   box-shadow:0 2px 4px rgba(0,0,0,.2);">
            Browse...
        </button>
        <script>
          document.getElementById('dir-input').addEventListener('change', async function(e) {
              var AUDIO_RE = /\\.(wav|flac|aif|aiff)$/i;
              var files = Array.from(e.target.files).filter(f => AUDIO_RE.test(f.name));
              if (!files.length) return;
              var dirName = files[0].webkitRelativePath.split('/')[0];

              // Read first 512 bytes of each file for header parsing
              var rows = await Promise.all(files.map(async function(f) {
                  try {
                      var buf = await f.slice(0, 512).arrayBuffer();
                      var b64 = btoa(
                          Array.from(new Uint8Array(buf), b => String.fromCharCode(b)).join('')
                      );
                      return f.webkitRelativePath + '\\t' + f.size + '\\t' + b64;
                  } catch(err) {
                      return f.webkitRelativePath + '\\t' + f.size + '\\t';
                  }
              }));

              rows.sort();
              window.pySetFiles(rows.join('\\n'), dirName);
          });
        </script>
        """,
        height=55,
    )

else:
    # Local mode: full QC via run_qc()
    import tkinter as tk
    from tkinter import filedialog

    dir_input = pn.widgets.TextInput(
        placeholder="Selected directory will appear here...",
        width=560,
        disabled=True,
    )
    browse_btn = pn.widgets.Button(name="Browse...", button_type="primary", width=120)

    def _on_browse(event):
        root_tk = tk.Tk()
        root_tk.withdraw()
        root_tk.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Select a directory")
        root_tk.destroy()
        if not path:
            return
        dir_input.value = path
        status.object = "*Running QC…*"
        files_display.value = ""
        results_display.value = ""
        files_display.value = "\n".join(f.name for f in list_audio_files(path))
        results = run_qc(path)
        results_display.value = _report_to_str(results)
        _update_visuals(results)
        if "error" in results:
            status.object = f"*Error: {results['error']}*"
        else:
            status.object = f"**{results['n_files']} file(s)** analysed in `{path}`"

    browse_btn.on_click(_on_browse)
    picker_widget = pn.Row(browse_btn, dir_input)

# ── Layout ─────────────────────────────────────────────────────────────────────

app = pn.Column(
    pn.pane.Markdown("# Audio QC"),
    pn.pane.Markdown("Select a folder containing WAV / FLAC / AIF files to run basic QC checks."),
    picker_widget,
    status,
    pn.Row(
        duty_chart_pane,
        pn.Column(
            pn.pane.Markdown("### Inconsistencies"),
            pn.Row(inconsistency_indicator, inconsistency_label, align="center"),
        ),
    ),
    pn.Row(files_display, results_display),
    max_width=1400,
    margin=(20, 20),
)

app.servable()

if __name__ == "__main__":
    pn.serve(__file__, show=True, autoreload=True)
