"""
s3_acoustic_metadata.py
========================
Crawl an S3 bucket and extract metadata from acoustic deployments.

For each folder that directly contains audio files (WAV, FLAC, AIF/AIFF),
the script extracts:
  - S3 folder path (deployment)
  - Number of audio files
  - Start and end datetime (parsed from filenames)
  - Recorder type (SoundTrap, AMAR, MARU, PMEL, …)
  - Serial number (first part of SoundTrap filenames; filename stem for others)
  - Duty cycle: recording ON (min) and OFF (min)
  - Sampling frequency (Hz)
  - Bit depth
  - Sensitivity / end-to-end calibration (dB re 1 V/µPa)
    → queried automatically from Ocean Instruments API for SoundTraps

Output: pandas DataFrame (and optionally saved to CSV or .xlsx).

Example (Python / Jupyter):
    from s3_acoustic_metadata import scan_bucket

    df = scan_bucket(
        bucket      = "neracoos-pam-data-ingest",
        prefix      = "Wellfleet/",
        output      = "metadata.xlsx",   # optional — omit to skip saving
        gain_type   = "High",
        max_workers = 16,
        sample_files= 5,
    )
    df.head()

Dependencies:
    boto3, pandas, openpyxl (for .xlsx), requests
    Optional: pyhydrophone (SoundTrap sensitivity lookup)

AWS credentials must be configured (IAM role on Nebari, or ~/.aws/credentials).
"""

import re
import struct
import logging
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath

import boto3
import pandas as pd
import requests

log = logging.getLogger(__name__)

# ── Audio extensions ───────────────────────────────────────────────────────────

AUDIO_EXTENSIONS = {".wav", ".flac", ".aif", ".aiff"}

# ── Timestamp patterns ─────────────────────────────────────────────────────────
# Mirrors the patterns in audio_qc_basics.py / ecosound timestamp_formats.json.

_PATTERNS = [
    # (recorder_type,  regex,  strptime_format)
    ("AMAR",        r"\.[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}\.", ".%Y-%m-%d-%H-%M-%S."),
    ("AMAR",        r"_[0-9]{8}T[0-9]{6}\.[0-9]{3}Z\.",                            "_%Y%m%dT%H%M%S.%fZ."),
    ("AMAR",        r"\.[0-9]{8}T[0-9]{6}Z\.",                                     ".%Y%m%dT%H%M%SZ."),
    ("SoundTrap",   r"\.[0-9]{12}\.",                                               ".%y%m%d%H%M%S."),
    ("SoundTrap",   r"_[0-9]{12}\.",                                                "_%y%m%d%H%M%S."),
    ("MARU",        r"_[0-9]{8}_[0-9]{6}\.",                                       "_%Y%m%d_%H%M%S."),
    ("MARU",        r"_[0-9]{8}_[0-9]{6}_[0-9]{3}\.",                              "_%Y%m%d_%H%M%S_%f."),
    ("MARU",        r"_[0-9]{6}_[0-9]{6}_",                                        "_%y%m%d_%H%M%S_"),
    ("PMEL",        r"-[0-9]{6}-[0-9]{6}\.",                                       "-%y%m%d-%H%M%S."),
    ("SAMS",        r"_[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}\.", "_%Y-%m-%d_%H-%M-%S."),
    ("PAMGuard",    r"_[0-9]{8}_[0-9]{6}Z\.",                                      "_%Y%m%d_%H%M%SZ."),
    ("SoundTrap",   r"\.[0-9]{14}\.",                                               ".%Y%m%d%H%M%S."),
    ("Loggerhead",  r"[0-9]{8}T[0-9]{6}\.",                                        "%Y%m%dT%H%M%S."),
]

_COMBINED_RE = re.compile("|".join(p[1] for p in _PATTERNS))


def filename_to_datetime(filename):
    """
    Parse datetime and recorder type from an audio filename.

    Returns
    -------
    (datetime, recorder_type_str)

    Raises
    ------
    ValueError if no known pattern matches.
    """
    name = Path(filename).name
    for recorder_type, pattern, fmt in _PATTERNS:
        m = re.search(pattern, name)
        if m is None:
            continue
        datestr = m[0]
        try:
            return datetime.strptime(datestr, fmt), recorder_type
        except ValueError:
            continue
    raise ValueError(f"No timestamp pattern matched: {name}")


def extract_serial_number(filename, recorder_type):
    """
    Extract the serial / instrument ID from a filename.

    For SoundTrap the convention is <serial>.<timestamp>.<ext>
    (e.g. 67416022.210310130000.wav → serial = '67416022').
    For other recorders the stem before the first separator is returned,
    or None if nothing useful can be determined.
    """
    name = Path(filename).name
    if recorder_type == "SoundTrap":
        # Serial number = everything before the first '.'
        parts = name.split(".")
        if parts:
            candidate = parts[0]
            if candidate.isdigit():
                return candidate
    # For AMAR / MARU / etc. the serial is often embedded differently or absent
    # Return the first underscore-separated token as a best-effort fallback
    stem = Path(filename).stem
    token = re.split(r"[_.\-]", stem)[0]
    return token if token else None


# ── Audio header parsers (pure stdlib, same approach as audio_qc_basics.py) ───

def _parse_80bit_float(b):
    exponent = ((b[0] & 0x7F) << 8) | b[1]
    mantissa = int.from_bytes(b[2:10], "big")
    if exponent == 0 and mantissa == 0:
        return 0.0
    value = mantissa * (2.0 ** (exponent - 16383 - 63))
    return -value if (b[0] & 0x80) else value


def _wav_info_from_bytes(data, file_size=None):
    """Return (sample_rate, duration_sec, bit_depth) from WAV header bytes."""
    try:
        if data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
            return None, None, None
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
                        return sample_rate, (data_bytes // bps) / sample_rate, bits
                break
            offset += 8 + chunk_size
    except Exception:
        pass
    return None, None, None


def _flac_info_from_bytes(data):
    """Return (sample_rate, duration_sec, bit_depth) from FLAC header bytes."""
    try:
        if data[0:4] != b"fLaC":
            return None, None, None
        if (data[4] & 0x7F) != 0:
            return None, None, None
        si = data[8:]
        if len(si) < 18:
            return None, None, None
        sample_rate = (si[10] << 12) | (si[11] << 4) | (si[12] >> 4)
        # Bits per sample: bits 76-80 of STREAMINFO (5 bits, 0-indexed from STREAMINFO start)
        # Byte layout: si[12] holds lower 4 bits of sample_rate | 3 bits of channels | 1 bit of bits_per_sample
        # si[13] holds remaining 4 bits of bits_per_sample | upper 4 bits of total_samples
        bits_per_sample = (((si[12] & 0x01) << 4) | (si[13] >> 4)) + 1
        total_samples = (
            ((si[13] & 0x0F) << 32) | (si[14] << 24) |
            (si[15] << 16) | (si[16] << 8) | si[17]
        )
        duration = total_samples / sample_rate if sample_rate and total_samples else None
        return sample_rate, duration, bits_per_sample
    except Exception:
        return None, None, None


def _aif_info_from_bytes(data):
    """Return (sample_rate, duration_sec, bit_depth) from AIF/AIFF header bytes."""
    try:
        if data[0:4] != b"FORM" or data[8:12] not in (b"AIFF", b"AIFC"):
            return None, None, None
        offset = 12
        while offset + 8 <= len(data):
            chunk_id = data[offset:offset + 4]
            chunk_size = struct.unpack(">I", data[offset + 4:offset + 8])[0]
            if chunk_id == b"COMM":
                comm = data[offset + 8:]
                if len(comm) < 18:
                    break
                bits = struct.unpack(">H", comm[6:8])[0]
                num_frames = struct.unpack(">I", comm[2:6])[0]
                sample_rate = int(_parse_80bit_float(comm[8:18]))
                duration = num_frames / sample_rate if sample_rate and num_frames else None
                return sample_rate, duration, bits
            offset += 8 + chunk_size + (chunk_size % 2)
    except Exception:
        pass
    return None, None, None


def get_audio_info_from_bytes(header_bytes, filename, file_size=None):
    """Return (sample_rate_hz, duration_sec, bit_depth) from raw header bytes."""
    ext = Path(filename).suffix.lower()
    if ext == ".wav":
        return _wav_info_from_bytes(header_bytes, file_size)
    if ext == ".flac":
        return _flac_info_from_bytes(header_bytes)
    if ext in (".aif", ".aiff"):
        return _aif_info_from_bytes(header_bytes)
    return None, None, None


# ── S3 helpers ─────────────────────────────────────────────────────────────────

def list_audio_objects(s3_client, bucket, prefix):
    """
    List all audio objects under *prefix* in *bucket*.

    Returns
    -------
    dict mapping each "deployment folder" (parent prefix) to a list of
    (s3_key, file_size_bytes) tuples, sorted by key name.
    """
    paginator = s3_client.get_paginator("list_objects_v2")
    folders = defaultdict(list)
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            ext = PurePosixPath(key).suffix.lower()
            if ext not in AUDIO_EXTENSIONS:
                continue
            # Parent "folder" = everything up to and including the last "/"
            parent = str(PurePosixPath(key).parent)
            if parent == ".":
                parent = ""
            folders[parent].append((key, obj["Size"]))
    # Sort each folder's files by key name (chronological for timestamp-named files)
    return {folder: sorted(files, key=lambda x: x[0]) for folder, files in folders.items()}


def fetch_header_bytes(s3_client, bucket, key, file_size, n_bytes=512):
    """Fetch the first *n_bytes* of an S3 object without downloading the full file."""
    try:
        end = min(n_bytes - 1, file_size - 1)
        resp = s3_client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{end}")
        return resp["Body"].read()
    except Exception as e:
        log.warning(f"Could not fetch header for {key}: {e}")
        return None


# ── SoundTrap sensitivity lookup ───────────────────────────────────────────────

_sensitivity_cache = {}   # serial_number → sensitivity (dB re 1V/µPa)


def lookup_soundtrap_sensitivity(serial_number, gain_type="High"):
    """
    Query the Ocean Instruments calibration API for a SoundTrap serial number.

    Returns
    -------
    float or None — sensitivity in dB re 1 V/µPa
    """
    cache_key = (str(serial_number), gain_type)
    if cache_key in _sensitivity_cache:
        return _sensitivity_cache[cache_key]
    try:
        from pyhydrophone.soundtrap import SoundTrap
        st = SoundTrap(
            name="SoundTrap",
            model="ST300HF",    # model is only used when multiple serial matches exist
            serial_number=serial_number,
            sensitivity=None,
            gain_type=gain_type,
        )
        _sensitivity_cache[cache_key] = st.sensitivity
        return st.sensitivity
    except Exception:
        pass
    # Direct API fallback (in case pyhydrophone is not installed)
    try:
        url = f"http://oceaninstruments.azurewebsites.net/api/Devices/Search/{serial_number}"
        resp = requests.get(url, timeout=10).json()
        if not resp:
            return None
        device_id = resp[0]["deviceId"]
        cal_url = f"http://oceaninstruments.azurewebsites.net/api/Calibrations/Device/{device_id}"
        cal = requests.get(cal_url, timeout=10).json()
        if not cal:
            return None
        cal0 = cal[0]
        if gain_type == "High":
            sensitivity = -cal0.get("highFreq")
        else:
            sensitivity = -cal0.get("lowFreq")
        _sensitivity_cache[cache_key] = sensitivity
        return sensitivity
    except Exception as e:
        log.warning(f"Sensitivity lookup failed for serial {serial_number}: {e}")
        return None


# ── Per-folder metadata extraction ────────────────────────────────────────────

def process_folder(s3_client, bucket, folder, files, gain_type="High",
                   sample_files=5):
    """
    Extract all deployment metadata for one S3 folder.

    Parameters
    ----------
    s3_client      : boto3 S3 client
    bucket         : str
    folder         : str — S3 prefix (the "deployment folder")
    files          : list of (key, size) tuples, sorted by key name
    gain_type      : str — 'High' or 'Low' (SoundTrap gain setting)
    sample_files   : int — number of file headers to read for sr/bit-depth
                     (first, last, and evenly-spaced middle files)

    Returns
    -------
    dict with all metadata fields, or None if no timestamps could be parsed.
    """
    n_files = len(files)

    # ── Parse timestamps from filenames ───────────────────────────────────────
    records = []
    recorder_types = []
    serial_numbers = []
    for key, size in files:
        fname = PurePosixPath(key).name
        try:
            dt, rec_type = filename_to_datetime(fname)
            recorder_types.append(rec_type)
            sn = extract_serial_number(fname, rec_type)
            serial_numbers.append(sn)
            records.append({"key": key, "size": size, "start": dt})
        except ValueError:
            pass

    if not records:
        log.warning(f"  No parseable timestamps in {folder} — skipping.")
        return None

    records.sort(key=lambda r: r["start"])
    recorder_type = Counter(recorder_types).most_common(1)[0][0]
    serial_number = Counter(s for s in serial_numbers if s).most_common(1)[0][0] \
        if serial_numbers else None

    # ── Read audio headers (sample a subset of files) ─────────────────────────
    # Pick indices: first, last, and evenly-spaced middle files
    indices = sorted(set(
        [0, len(records) - 1] +
        [round(i * (len(records) - 1) / (sample_files - 1)) for i in range(sample_files)]
    ))
    sample_rates, bit_depths, durations = [], [], []
    for idx in indices:
        r = records[idx]
        header = fetch_header_bytes(s3_client, bucket, r["key"], r["size"])
        if header is None:
            continue
        sr, dur, bits = get_audio_info_from_bytes(header, r["key"], r["size"])
        if sr is not None:
            sample_rates.append(sr)
        if bits is not None:
            bit_depths.append(bits)
        if dur is not None:
            durations.append(dur)
            records[idx]["duration_sec"] = dur

    sample_rate_hz = Counter(sample_rates).most_common(1)[0][0] if sample_rates else None
    bit_depth = Counter(bit_depths).most_common(1)[0][0] if bit_depths else None

    # ── Compute start / end datetimes ─────────────────────────────────────────
    start_dt = records[0]["start"]
    # End datetime = start of last file + its duration (if known)
    last_rec = records[-1]
    last_dur = last_rec.get("duration_sec")
    if last_dur is None and durations:
        last_dur = Counter([round(d, 1) for d in durations]).most_common(1)[0][0]
    end_dt = last_rec["start"] + timedelta(seconds=last_dur) if last_dur else last_rec["start"]

    # ── Duty cycle ────────────────────────────────────────────────────────────
    # ON  = most common file duration
    # OFF = most common inter-file gap
    if durations:
        on_sec = Counter([round(d, 1) for d in durations]).most_common(1)[0][0]
        on_min = round(on_sec / 60, 2)
    else:
        on_min = None

    gaps = []
    for i in range(1, len(records)):
        t0 = records[i - 1]["start"]
        t0_dur = records[i - 1].get("duration_sec")
        if t0_dur is None and on_min is not None:
            t0_dur = on_min * 60
        if t0_dur is not None:
            gap = (records[i]["start"] - t0 - timedelta(seconds=t0_dur)).total_seconds()
            gaps.append(gap)

    if gaps:
        off_sec = Counter([round(g, 1) for g in gaps]).most_common(1)[0][0]
        off_min = round(off_sec / 60, 2) if off_sec > 0 else 0.0
    else:
        off_min = None

    # ── Sensitivity / calibration ─────────────────────────────────────────────
    sensitivity_db = None
    if recorder_type == "SoundTrap" and serial_number:
        sensitivity_db = lookup_soundtrap_sensitivity(serial_number, gain_type)

    # ── Total size ────────────────────────────────────────────────────────────
    total_size_gb = sum(s for _, s in files) / 1e9

    return {
        "folder":             folder,
        "n_files":            n_files,
        "start_datetime":     start_dt,
        "end_datetime":       end_dt,
        "recorder":           recorder_type,
        "serial_number":      serial_number,
        "duty_cycle_on_min":  on_min,
        "duty_cycle_off_min": off_min,
        "sample_rate_hz":     sample_rate_hz,
        "bit_depth":          bit_depth,
        "sensitivity_dB":     sensitivity_db,
        "total_size_gb":      round(total_size_gb, 3),
    }


# ── Public API ────────────────────────────────────────────────────────────────

def scan_bucket(
    bucket,
    prefix="",
    output=None,
    gain_type="High",
    max_workers=8,
    sample_files=5,
    verbose=True,
):
    """
    Crawl an S3 bucket and extract acoustic deployment metadata.

    Parameters
    ----------
    bucket : str
        S3 bucket name (e.g. "neracoos-pam-data-ingest").
    prefix : str, optional
        S3 key prefix to restrict the crawl (e.g. "Wellfleet/").
        Default: entire bucket.
    output : str or Path, optional
        If provided, save the result to this path.
        Extension determines format: ".csv" → CSV, ".xlsx"/".xls" → Excel.
        Default: None (return DataFrame only, do not save).
    gain_type : str, optional
        SoundTrap gain setting for sensitivity lookup: "High" or "Low".
        Default: "High".
    max_workers : int, optional
        Number of parallel threads for S3 header reads. Default: 8.
    sample_files : int, optional
        Number of file headers to read per folder to determine sample rate
        and bit depth (first, last, and evenly-spaced middle files).
        Default: 5.
    verbose : bool, optional
        Print progress to stdout. Default: True.

    Returns
    -------
    pandas.DataFrame
        One row per deployment folder, sorted chronologically by start_datetime.
        Columns: folder, n_files, start_datetime, end_datetime, recorder,
                 serial_number, duty_cycle_on_min, duty_cycle_off_min,
                 sample_rate_hz, bit_depth, sensitivity_dB, total_size_gb.
        Returns an empty DataFrame if no audio files are found.
    """
    if verbose:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s  %(levelname)-8s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            force=True,
        )

    s3 = boto3.client("s3")

    log.info(f"Listing audio objects in s3://{bucket}/{prefix} ...")
    folders = list_audio_objects(s3, bucket, prefix)
    log.info(f"Found {len(folders)} folder(s) with audio files.")

    if not folders:
        log.warning("No audio files found.")
        return pd.DataFrame()

    rows = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                process_folder, s3, bucket, folder, files, gain_type, sample_files
            ): folder
            for folder, files in folders.items()
        }
        for i, future in enumerate(as_completed(futures), 1):
            folder = futures[future]
            try:
                result = future.result()
                if result:
                    rows.append(result)
                    log.info(
                        f"  [{i}/{len(folders)}] {folder}  →  {result['n_files']} files  "
                        f"{result['start_datetime']} → {result['end_datetime']}"
                    )
                else:
                    log.warning(f"  [{i}/{len(folders)}] {folder}  →  skipped (no parseable timestamps)")
            except Exception as e:
                log.error(f"  [{i}/{len(folders)}] {folder}  →  ERROR: {e}")

    if not rows:
        log.warning("No metadata could be extracted.")
        return pd.DataFrame()

    df = pd.DataFrame(rows).sort_values("start_datetime").reset_index(drop=True)

    if output is not None:
        output_path = Path(output)
        if output_path.suffix.lower() in (".xlsx", ".xls"):
            df.to_excel(output_path, index=False)
        else:
            df.to_csv(output_path, index=False)
        log.info(f"Saved {len(df)} deployment(s) to {output_path}")

    return df
