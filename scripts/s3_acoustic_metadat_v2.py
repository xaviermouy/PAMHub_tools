import s3fs
from ecosound.core.tools import filename_to_datetime
import pandas as pd
from collections import Counter
from itertools import pairwise
import warnings
from pathlib import Path
import soundfile as sf
import dask.bag as db
from pyhydrophone.soundtrap import SoundTrap
warnings.simplefilter("always")


deployment_dir="s3://neracoos-pam-data-ingest/Wellfleet/Wellfleet (1) April 22 2014 - May 21 2014/Soundtrap May 20 2014 Retrieval/Soundtrap May 20 2014 retrieval/"



def list_audio_files_s3(deployment_dir, audio_extensions=(".wav", ".flac", ".aif", ".aiff")):
    """
    List audio files in a deployment directory on S3.

    Lists the contents of an S3 prefix and keeps only the entries whose
    names end with one of the given audio extensions. Anything else
    (metadata files, logs, calibration files, subdirectories) is ignored.

    Parameters
    ----------
    deployment_dir : str
        S3 path of the deployment directory, e.g.
        "my-bucket/deployments/SB_2021_03". Non-recursive: only entries
        directly under this prefix are returned.
    audio_extensions : tuple of str, optional
        Extensions to keep, given in lowercase and including the leading
        dot. Matching is case-insensitive. Default is
        (".wav", ".flac", ".aif", ".aiff").

    Returns
    -------
    list of str
        Full S3 paths of the matching audio files, sorted. Empty if the
        directory contains no audio files.

    Notes
    -----
    Uses anonymous or environment-based S3 credentials, whichever
    `s3fs.S3FileSystem` picks up by default.

    Examples
    --------
    >>> list_audio_files_s3("my-bucket/deployments/SB_2021_03")
    ['my-bucket/deployments/SB_2021_03/67416022.210310130000.wav', ...]
    """
    fs = s3fs.S3FileSystem()
    return _filter_audio_files(fs.ls(deployment_dir), audio_extensions)


def list_audio_folders_s3(root_dir, audio_extensions=(".wav", ".flac", ".aif", ".aiff"),
                          min_files=1, maxdepth=None):
    """
    Find all folders containing audio files under an S3 prefix.

    Walks the directory tree below `root_dir` and calls
    `list_audio_files_s3` on each folder. Folders holding at least
    `min_files` audio files are kept. Intended as the entry point for
    batch processing: each returned folder is treated as one deployment.

    Parameters
    ----------
    root_dir : str
        S3 path to crawl, e.g. "s3://neracoos-pam-data-ingest/Wellfleet".
    audio_extensions : tuple of str, optional
        Extensions to keep, given in lowercase and including the leading
        dot. Matching is case-insensitive. Default is
        (".wav", ".flac", ".aif", ".aiff").
    min_files : int, optional
        Minimum number of audio files a folder must contain to be
        returned. Raise it to skip stray test files or calibration
        recordings sitting outside a deployment. Default is 1.
    maxdepth : int, optional
        Maximum directory depth to descend, counted from `root_dir`.
        Useful for inspecting the layout of an unfamiliar bucket without
        crawling it all. Default is None (no limit).

    Returns
    -------
    dict
        Maps folder path to the sorted list of audio files it contains,
        as returned by `list_audio_files_s3`. Folders are in sorted
        order. Empty if no audio files are found under `root_dir`.

    Notes
    -----
    Only folders that directly contain audio files are returned; a
    parent folder whose audio lives in subfolders is not itself listed.
    A deployment split across subfolders therefore appears as several
    entries, so it is worth reviewing the folder list and file counts
    before treating each entry as one deployment.

    Examples
    --------
    >>> folders = list_audio_folders_s3("s3://my-bucket/Wellfleet")
    >>> for folder, audio_files in folders.items():
    ...     print(folder, len(audio_files))
    """
    fs = s3fs.S3FileSystem()

    folders = {}
    for folder, _dirs, files in fs.walk(root_dir, maxdepth=maxdepth):
        # Reuse the filesystem and file list from the walk to avoid an extra
        # ls() call and a new S3FileSystem object per folder.
        full_paths = [f"{folder}/{f}" for f in files]
        audio_files = _filter_audio_files(full_paths, audio_extensions)
        if len(audio_files) >= min_files:
            folders[folder] = audio_files

    return dict(sorted(folders.items()))

def retrieve_recording_interval(audio_files):
    """Estimate the nominal time interval between consecutive recordings.

    Computes the gaps between consecutive timestamps, rounds them to the
    nearest second, and returns the most frequently occurring value. A
    warning is issued if that value does not account for at least 90% of
    the gaps, which usually indicates missing files or a recorder whose
    duty cycle changed mid-deployment.

    Parameters
    ----------
    audio_files : sequence of str or pathlib.Path
        Audio file paths or names from a single deployment.

    Returns
    -------
    int or None
        Most common interval between consecutive files, in seconds.
        None if the interval cannot be determined, which happens when
        fewer than two files are given (a single continuous recording
        has no interval) or when fewer than two timestamps could be
        parsed from the file names.

    Warns
    -----
    UserWarning
        If the most common interval covers less than 90% of the gaps. The
        message reports the coverage and lists the next most frequent
        intervals, which distinguishes missing files (large multiples of
        the nominal interval) from timestamp jitter (neighbouring values).
    UserWarning
        If the interval cannot be determined, in which case None is
        returned.

    Examples
    --------
    >>> audio_files = [
    ...         '67432472.140422081906.wav',
    ...         '67432472.140422091906.wav',
    ...         '67432472.140422101906.wav']
    >>> retrieve_recording_interval(audio_files)
    3600
    """
    if len(audio_files) < 2:
        warnings.warn(
            f"Cannot determine a recording interval from {len(audio_files)} file(s); "
            f"at least 2 are needed. Returning None.",
            stacklevel=2,
        )
        return None

    audio_files_datetime = filename_to_datetime([Path(f).name for f in audio_files])
    times = sorted(t for t in audio_files_datetime if pd.notna(t))

    if len(times) < 2:
        warnings.warn(
            f"Only {len(times)} of {len(audio_files)} file names yielded a valid "
            f"timestamp; cannot determine a recording interval. Returning None.",
            stacklevel=2,
        )
        return None

    intervals = [(b - a).total_seconds() for a, b in pairwise(times)]
    BIN = 1  # seconds
    binned = [round(x / BIN) * BIN for x in intervals]
    return _dominant_value(binned, "recording interval", threshold=0.9)

def retrieve_recorder_serial_number(audio_files, recorder_type="SoundTrap"):
    """
    Extract the recorder serial number from a list of audio files.

    Parses the serial number from each file name and returns the value
    shared by all files. For SoundTrap the naming convention is
    <serial>.<timestamp>.<ext> (e.g. 67416022.210310130000.wav → '67416022').

    Currently only supports SoundTrap file names.

    Parameters
    ----------
    audio_files : sequence of str or pathlib.Path
        Audio file paths or names from a single deployment.
    recorder_type : str, optional
        Recorder model whose naming convention should be used. Only
        "SoundTrap" is currently implemented. Default is "SoundTrap".

    Returns
    -------
    str
        Serial number shared by all files.

    Raises
    ------
    NotImplementedError
        If `recorder_type` is anything other than "SoundTrap".
    ValueError
        If `audio_files` is empty, if no file name yields a valid serial
        number, or if more than one distinct serial number is found. The
        latter usually means the file list spans more than one deployment.

    Warns
    -----
    UserWarning
        If some file names could not be parsed while others could. The
        message reports how many were skipped and gives an example.

    Examples
    --------
    >>> retrieve_recorder_serial_number(["67416022.210310130000.wav",
    ...                                  "67416022.210310140000.wav"])
    '67416022'
    """
    if recorder_type != "SoundTrap":
        raise NotImplementedError(
            f"Recorder type {recorder_type!r} is not supported. "
            f"Only 'SoundTrap' is currently implemented."
        )

    if len(audio_files) == 0:
        raise ValueError("No audio files provided.")

    SN, unparsed = [], []
    for f in audio_files:
        name = Path(f).name
        candidate = name.split(".")[0]
        if candidate.isdigit():
            SN.append(candidate)
        else:
            unparsed.append(name)

    if not SN:
        raise ValueError(
            f"Could not extract a serial number from any of the {len(audio_files)} "
            f"file names. Expected the SoundTrap convention "
            f"<serial>.<timestamp>.<ext>, but got e.g. {unparsed[0]!r}."
        )

    if unparsed:
        warnings.warn(
            f"{len(unparsed)} of {len(audio_files)} file names could not be parsed "
            f"and were ignored (e.g. {unparsed[0]!r}).",
            stacklevel=2,
        )

    SN_counts = Counter(SN)

    if len(SN_counts) > 1:
        breakdown = ", ".join(f"{s} ×{c}" for s, c in SN_counts.most_common())
        raise ValueError(
            f"Multiple serial numbers found in the same file list: {breakdown}. "
            f"Files from different recorders should be processed separately."
        )

    return SN_counts.most_common(1)[0][0]

def summarize_audio_files_metadata(df, threshold=0.9):
    """
    Reduce per-file audio metadata to a single set of deployment values.

    Takes the most frequent value of each acoustic setting across the
    deployment. File durations are rounded to the nearest second before
    counting, since recorded lengths jitter slightly and would otherwise
    never repeat. A warning is issued for any setting whose most frequent
    value does not cover at least `threshold` of the files, which usually
    means the recorder was reconfigured mid-deployment or the file list
    spans more than one deployment.

    Parameters
    ----------
    df : pandas.DataFrame
        Per-file metadata, as returned by `retrieve_audio_files_metadata`.
    threshold : float, optional
        Minimum fraction of files a value must cover before a warning is
        issued. Default is 0.9.

    Returns
    -------
    dict
        Keys 'samplerate', 'channels', 'bit_depth', 'format', and
        'duration_sec', plus 'n_files' and 'n_errors'.

    Warns
    -----
    UserWarning
        Once per inconsistent setting, reporting the coverage and the
        next most frequent values.
    """
    n_errors = int(df["error"].notna().sum()) if "error" in df else 0
    ok = df[df["error"].isna()] if "error" in df else df

    summary = {
        field: _dominant_value(ok[field], field, threshold)
        for field in ("samplerate", "channels", "bit_depth", "format")
    }
    summary["duration_sec"] = _dominant_value(
        ok["duration_sec"].round(), "duration_sec", threshold
    )
    summary["n_files"] = len(df)
    summary["n_errors"] = n_errors
    return summary

def retrieve_audio_files_metadata(audio_files, num_workers=16, npartitions=64):
    """
    Read sample rate, bit depth, and duration from audio files on S3.

    Reads only file headers, so no audio data is transferred. Files are
    read concurrently using Dask's threaded scheduler, which suits this
    workload because it is dominated by network latency rather than
    computation. No Dask client or cluster is required.

    Parameters
    ----------
    audio_files : sequence of str
        S3 paths of the audio files, as returned by `list_audio_files`.
    num_workers : int, optional
        Number of concurrent header reads. Default is 16. Higher values
        tend to hit S3 throttling rather than improve throughput.
    npartitions : int, optional
        Number of chunks the file list is split into. Should be at least
        `num_workers` so every thread has work; a few times larger gives
        better load balancing. Default is 64.

    Returns
    -------
    pandas.DataFrame
        One row per file, in the order given, with columns 'path',
        'samplerate', 'channels', 'samples', 'duration_sec', 'subtype',
        'format', and 'bit_depth'. Files that could not be read have
        the reason in an 'error' column and NaN elsewhere.
    """
    fs = s3fs.S3FileSystem()

    def read_one(path):
        try:
            with fs.open(path, "rb") as f:
                info = sf.info(f)
        except Exception as e:
            return {"path": path, "error": str(e)}

        return {
            "path": path,
            "samplerate_hz": info.samplerate,
            "channels": info.channels,
            "samples": info.frames,
            "duration_sec": info.duration,
            "subtype": info.subtype,
            "format": info.format,
            "bit_depth": _bit_depth(info.subtype),
        }

    bag = db.from_sequence(audio_files, npartitions=min(npartitions, len(audio_files)))
    rows = bag.map(read_one).compute(scheduler="threads", num_workers=num_workers)
    return pd.DataFrame(rows)

_sysgain_cache = {}


def lookup_soundtrap_sysgain(serial_number, gain_type="High", model="ST300HF"):
    """
    Look up the factory calibration sysgain for a SoundTrap.

    Queries the Ocean Instruments calibration database via pyhydrophone
    and returns the end-to-end system sysgain for the requested gain
    setting. Results are cached in memory for the lifetime of the
    process, so repeated lookups for the same recorder cost nothing.

    Parameters
    ----------
    serial_number : str or int
        SoundTrap recorder serial number, as parsed from the file names
        by `retrieve_recorder_serial_number` (e.g. '67432472').
    gain_type : {'High', 'Low'}, optional
        Gain setting used during the deployment, as recorded in the
        SoundTrap XML log. Default is 'High'.
    model : str, optional
        Recorder model (e.g. 'ST300HF', 'ST500', 'ST600'). Recorded as
        metadata only; it does not affect which calibration is returned.
        Default is 'ST300HF'.

    Returns
    -------
    float or None
        End-to-end system gain in dB re 1 µPa (full scale), returned as
        a negative number (e.g. -176.0). None if the serial number is
        not in the database or the query fails.

    Raises
    ------
    ValueError
        If `gain_type` is not 'High' or 'Low'.

    Warns
    -----
    UserWarning
        If the serial number is not in the database, or if the query
        fails for any other reason. None is returned in both cases.

    Notes
    -----
    A None return means the calibration is unknown, not that it is zero.
    Callers should treat it as a hard stop before computing absolute
    sound levels rather than substituting a nominal value.

    Delegating to pyhydrophone means the Ocean Instruments endpoint and
    response schema are maintained upstream. Requires pyhydrophone 0.4.0
    or later, which uses the current data.oceaninstruments.co.nz API;
    earlier versions query a legacy endpoint.

    Examples
    --------
    >>> lookup_soundtrap_sysgain("67432472")
    -176.0
    """
    if gain_type not in ("High", "Low"):
        raise ValueError(f"gain_type must be 'High' or 'Low', got {gain_type!r}.")

    cache_key = (str(serial_number), gain_type)
    if cache_key in _sysgain_cache:
        return _sysgain_cache[cache_key]

    try:
        st = SoundTrap(
            name="SoundTrap",
            model=model,
            serial_number=serial_number,
            sysgain=None,      # triggers the online lookup
            gain_type=gain_type,
        )
        sysgain = float(st.sensitivity)
    except TypeError:
        warnings.warn(
            f"Serial {serial_number} not found in the Ocean Instruments database. "
            f"Returning None.",
            stacklevel=2,
        )
        return None
    except Exception as e:
        warnings.warn(
            f"Sensitivity lookup failed for serial {serial_number} "
            f"({gain_type} gain): {type(e).__name__}: {e}. Returning None.",
            stacklevel=2,
        )
        return None

    _sysgain_cache[cache_key] = sysgain
    return sysgain

def _filter_audio_files(files, audio_extensions):
    """
    Keep only audio files from a list of paths.

    Parameters
    ----------
    files : sequence of str
        File paths to filter.
    audio_extensions : tuple of str
        Lowercase file extensions to keep, including the leading dot.
        Matching is case-insensitive, so ".WAV" files are kept.

    Returns
    -------
    list of str
        Matching paths, sorted.
    """
    return sorted(f for f in files if f.lower().endswith(audio_extensions))

def _bit_depth(subtype):
    if subtype.startswith("PCM_"):
        return int(subtype.split("_")[1])
    return {"FLOAT": 32, "DOUBLE": 64}.get(subtype)

def _dominant_value(values, label, threshold=0.9):
    """
    Return the most frequent value in a sequence, warning if it is not dominant.

    Parameters
    ----------
    values : sequence
        Values to summarize. Missing values (NaN, None) are ignored.
    label : str
        Name of the quantity, used in the warning message.
    threshold : float, optional
        Minimum fraction of values the most frequent one must represent
        before a warning is issued. Default is 0.9.

    Returns
    -------
    object or None
        Most frequent value, or None if `values` contains nothing usable.

    Warns
    -----
    UserWarning
        If the most frequent value covers less than `threshold` of the
        values, or if all values are missing.
    """
    usable = [v for v in values if pd.notna(v)]

    if not usable:
        warnings.warn(f"No usable {label} values found.", stacklevel=2)
        return None

    counts = Counter(usable)
    most_common, n = counts.most_common(1)[0]
    fraction = n / len(usable)

    if fraction < threshold:
        others = ", ".join(f"{v} ×{c}" for v, c in counts.most_common(4)[1:])
        warnings.warn(
            f"Inconsistent {label}: {most_common} covers {fraction:.1%} "
            f"of {len(usable)} files. Also seen: {others}",
            stacklevel=2,
        )

    return most_common


def _scan_one_folder(item, num_workers=8):
    """Collect metadata for a single deployment folder. Never raises."""
    folder, audio_files = item
    row = {"folder": folder, "n_files": len(audio_files), "error": None}

    try:
        row["serial_number"] = retrieve_recorder_serial_number(
            audio_files, recorder_type="SoundTrap"
        )
        row["sysgain"] = lookup_soundtrap_sysgain(row["serial_number"], gain_type="High")
        row["recording_interval_sec"] = retrieve_recording_interval(audio_files)

        metadata = retrieve_audio_files_metadata(audio_files, num_workers=num_workers)
        row.update(summarize_audio_files_metadata(metadata))
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"

    return row


def scan_bucket_for_metadata(root_dir, min_files=1, num_workers=4, files_num_workers=8):
    """
    Build a metadata catalogue for every deployment folder under an S3 prefix.

    Finds folders containing audio files, then extracts the recorder
    serial number, calibration gain, recording interval, and audio file
    properties for each one. Folders are processed concurrently.

    Parameters
    ----------
    root_dir : str
        S3 path to crawl, e.g. "s3://neracoos-pam-data-ingest/Wellfleet".
    min_files : int, optional
        Minimum number of audio files a folder must contain to be
        included. Default is 1.
    num_workers : int, optional
        Number of folders processed concurrently. Default is 4.
    files_num_workers : int, optional
        Number of concurrent header reads within each folder. Default
        is 8. The product of this and `num_workers` is the total number
        of concurrent S3 connections; much above ~64 tends to trigger
        throttling.

    Returns
    -------
    pandas.DataFrame
        One row per folder, with columns 'folder', 'n_files',
        'serial_number', 'sysgain', 'recording_interval_sec', the
        summary fields from `summarize_audio_files_metadata`, and
        'error'. Folders that failed have the reason in 'error' and
        NaN elsewhere.
    """
    folders = list_audio_folders_s3(root_dir, min_files=min_files)

    if not folders:
        return pd.DataFrame(columns=["folder", "n_files", "error"])

    rows = (
        db.from_sequence(list(folders.items()), npartitions=len(folders))
        .map(_scan_one_folder, num_workers=files_num_workers)
        .compute(scheduler="threads", num_workers=num_workers)
    )

    return pd.DataFrame(rows)


if __name__ == "__main__":
    df = scan_bucket_for_metadata(deployment_dir, min_files=1, num_workers=4, files_num_workers=8)
    print(df)