"""
audio_qc.py
===========
List audio files in a directory (WAV, FLAC, AIF/AIFF) and run basic QC checks:
  - Number of files
  - Dataset size (GB)
  - Start / end datetime
  - Duty cycle (recording ON duration, recording OFF duration)
  - Sampling frequency consistency
  - Unusual data gaps
  - Unusual file durations

Timestamp parsing is pure stdlib (re + datetime) — compatible with Pyodide.
Audio header reading uses struct for WAV (stdlib) and soundfile for FLAC/AIF.

Usage (command line):
    python audio_qc.py <directory>
"""

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
                    # Use chunk_size from header if non-zero, else estimate from file size
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
    # Fallback: soundfile (handles edge cases and compressed formats)
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


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    # ── Test directory (used when no argument is passed) ──────────────────────
    TEST_DIR = "C:/Users/xavier.mouy/Documents/Projects/pbp_test_dataset/data/Galapagos_data/WHOI_Galapagos_202305_Caseta/6478"

    parser = argparse.ArgumentParser(description="Audio file QC checks.")
    parser.add_argument("directory", nargs="?", default=TEST_DIR,
                        help="Directory containing audio files (default: test dataset)")
    args = parser.parse_args()

    if args.directory == TEST_DIR:
        print(f"No directory provided — using test directory:\n  {TEST_DIR}\n")

    results = run_qc(args.directory, verbose=True)
