import logging
import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import fsspec
import s3fs
from ecosound.core.tools import filename_to_datetime
import pandas as pd
from collections import Counter
from itertools import pairwise
import soundfile as sf
from tqdm.auto import tqdm
from pyhydrophone.soundtrap import SoundTrap

logger = logging.getLogger(__name__)


def _get_fs(path):
    """Return an fsspec-compatible filesystem for the given path."""
    if path.startswith(("s3://", "s3a://")):
        return s3fs.S3FileSystem()
    return fsspec.filesystem("file")

def list_audio_files(deployment_dir, audio_extensions=(".wav", ".flac", ".aif", ".aiff")):
    """
    List audio files in a deployment directory (S3 or local).

    Lists the contents of a directory and keeps only the entries whose
    names end with one of the given audio extensions. Anything else
    (metadata files, logs, calibration files, subdirectories) is ignored.

    Parameters
    ----------
    deployment_dir : str
        Path of the deployment directory. Can be an S3 path (e.g.
        "s3://my-bucket/deployments/SB_2021_03") or a local path (e.g.
        "/data/deployments/SB_2021_03"). Non-recursive: only entries
        directly under this directory are returned.
    audio_extensions : tuple of str, optional
        Extensions to keep, given in lowercase and including the leading
        dot. Matching is case-insensitive. Default is
        (".wav", ".flac", ".aif", ".aiff").

    Returns
    -------
    list of str
        Full paths of the matching audio files, sorted. Empty if the
        directory contains no audio files.

    Examples
    --------
    >>> list_audio_files("s3://my-bucket/deployments/SB_2021_03")
    ['my-bucket/deployments/SB_2021_03/67416022.210310130000.wav', ...]
    >>> list_audio_files("/data/deployments/SB_2021_03")
    ['/data/deployments/SB_2021_03/67416022.210310130000.wav', ...]
    """
    fs = _get_fs(deployment_dir)
    return _filter_audio_files(fs.ls(deployment_dir), audio_extensions)


def list_audio_folders(root_dir, audio_extensions=(".wav", ".flac", ".aif", ".aiff"),
                       min_files=1, maxdepth=None):
    """
    Find all folders containing audio files under a root directory (S3 or local).

    Walks the directory tree below `root_dir` and keeps folders holding
    at least `min_files` audio files. Intended as the entry point for
    batch processing: each returned folder is treated as one deployment.

    Parameters
    ----------
    root_dir : str
        Path to crawl. Can be an S3 path (e.g.
        "s3://neracoos-pam-data-ingest/Wellfleet") or a local path
        (e.g. "/data/Wellfleet" or "D:\\Data\\Wellfleet").
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
        as returned by `list_audio_files`. Folders are in sorted
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
    >>> folders = list_audio_folders("s3://my-bucket/Wellfleet")
    >>> folders = list_audio_folders("/data/Wellfleet")
    >>> for folder, audio_files in folders.items():
    ...     print(folder, len(audio_files))
    """
    fs = _get_fs(root_dir)

    folders = {}
    with tqdm(desc="Walking directories", unit=" dirs", leave=True) as pbar:
        for folder, _dirs, files in fs.walk(root_dir, maxdepth=maxdepth):
            full_paths = [f"{folder}/{f}" for f in files]
            audio_files = _filter_audio_files(full_paths, audio_extensions)
            if len(audio_files) >= min_files:
                folders[folder] = audio_files
            pbar.update(1)
            pbar.set_postfix({"audio folders": len(folders)})

    folders = dict(sorted(folders.items()))

    print(f"\nFound {len(folders)} folder(s) with audio files:")
    for folder, audio_files in folders.items():
        print(f"  {folder}  ({len(audio_files)} files)")

    return folders

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

    Logs
    ----
    WARNING
        If the most common interval covers less than 90% of the gaps. The
        message reports the coverage and lists the next most frequent
        intervals, which distinguishes missing files (large multiples of
        the nominal interval) from timestamp jitter (neighbouring values).
    WARNING
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
        logger.warning(
            "Cannot determine a recording interval from %d file(s); "
            "at least 2 are needed. Returning None.",
            len(audio_files),
        )
        return None

    audio_files_datetime = filename_to_datetime([os.path.basename(f) for f in audio_files])
    times = sorted(t for t in audio_files_datetime if pd.notna(t))

    if len(times) < 2:
        logger.warning(
            "Only %d of %d file names yielded a valid timestamp; "
            "cannot determine a recording interval. Returning None.",
            len(times), len(audio_files),
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

    Logs
    ----
    WARNING
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
        name = os.path.basename(f)
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
        logger.warning(
            "%d of %d file names could not be parsed and were ignored (e.g. %r).",
            len(unparsed), len(audio_files), unparsed[0],
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
        Keys 'recording_sample_rate_hz', 'recording_n_channels',
        'recording_bit_depth', 'recording_format', and
        'recording_duration_sec', plus 'n_errors'.

    Logs
    ----
    WARNING
        Once per inconsistent setting, reporting the coverage and the
        next most frequent values.
    """
    n_errors = int(df["error"].notna().sum()) if "error" in df else 0
    ok = df[df["error"].isna()] if "error" in df else df

    summary = {
        field: _dominant_value(ok[field], field, threshold)
        for field in ("recording_sample_rate_hz", "recording_n_channels", "recording_bit_depth", "recording_format")
    }
    summary["recording_duration_sec"] = _dominant_value(
        ok["recording_duration_sec"].round(), "recording_duration_sec", threshold
    )
    summary["n_errors"] = n_errors
    return summary

def retrieve_audio_files_metadata(audio_files, num_workers=16,
                                  subsampling_fraction=None, min_sample=10,
                                  max_sample=20):
    """
    Read sample rate, bit depth, and duration from audio files (S3 or local).

    Reads only file headers, so no audio data is transferred. Files are
    read concurrently using a thread pool, which suits S3 workloads
    (dominated by network latency) and helps with local disk I/O too.

    Parameters
    ----------
    audio_files : sequence of str
        Paths of the audio files, as returned by `list_audio_files`.
        Can be S3 paths or local paths.
    num_workers : int, optional
        Number of concurrent header reads. Default is 16. Higher values
        tend to hit S3 throttling rather than improve throughput.
    subsampling_fraction : float or None, optional
        Fraction of files to read (0.0–1.0). If None or 1.0, all files
        are read. A random subset is selected, which is usually sufficient
        since recorder settings are constant within a deployment. Default
        is None (read all files).
    min_sample : int, optional
        Minimum number of files to read regardless of `subsampling_fraction`.
        Prevents degenerate samples on small deployments. Default is 10.
    max_sample : int or None, optional
        Maximum number of files to read per folder. When set, the sample
        size is capped at this value regardless of `subsampling_fraction`.
        Reading 15–20 files is usually enough to detect inconsistencies
        while keeping the header phase O(folders) instead of O(files).
        Default is 20. Set to None to disable the cap.

    Returns
    -------
    pandas.DataFrame
        One row per sampled file with columns 'path',
        'recording_sample_rate_hz', 'recording_n_channels', 'samples',
        'recording_duration_sec', 'subtype', 'recording_format', and
        'recording_bit_depth'. Files that could not be read have the reason in an
        'error' column and NaN elsewhere.
    """
    audio_files = list(audio_files)
    n_total = len(audio_files)

    # Determine sample size: start from fraction if given, then apply caps
    n_sample = n_total
    if subsampling_fraction is not None and subsampling_fraction < 1.0:
        n_sample = max(min_sample, int(round(n_total * subsampling_fraction)))
    if max_sample is not None:
        n_sample = min(n_sample, max_sample)
    n_sample = min(n_sample, n_total)  # can't sample more than we have
    if n_sample < n_total:
        audio_files = random.sample(audio_files, n_sample)
        logger.info(
            "Subsampling %d of %d files (%.0f%%) for metadata read.",
            n_sample, n_total, n_sample / n_total * 100,
        )

    fs = _get_fs(audio_files[0])
    use_s3_opts = isinstance(fs, s3fs.S3FileSystem)

    def read_one(path):
        try:
            if use_s3_opts:
                with fs.open(path, "rb", block_size=64 * 1024, cache_type="bytes") as f:
                    info = sf.info(f)
            else:
                info = sf.info(path)
        except Exception as e:
            return {"path": path, "error": str(e)}

        return {
            "path": path,
            "recording_sample_rate_hz": info.samplerate,
            "recording_n_channels": info.channels,
            "samples": info.frames,
            "recording_duration_sec": info.duration,
            "subtype": info.subtype,
            "recording_format": info.format,
            "recording_bit_depth": _bit_depth(info.subtype),
        }

    rows = []
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(read_one, path): path for path in audio_files}
        folder_label = os.path.basename(os.path.dirname(next(iter(audio_files))))
        desc = f"  {folder_label}"
        if len(audio_files) < n_total:
            desc += f" ({len(audio_files)}/{n_total})"
        with tqdm(
            total=len(audio_files),
            desc=desc,
            unit=" files",
            leave=False,
        ) as pbar:
            for future in as_completed(futures):
                rows.append(future.result())
                pbar.update(1)

    return pd.DataFrame(rows)

_model_cache = {}

_OI_DEVICE_SEARCH_URL = (
    "http://oceaninstruments.azurewebsites.net/api/Devices/Search/{serial_number}"
)


def lookup_soundtrap_model(serial_number):
    """
    Look up the recorder model for a SoundTrap serial number.

    Queries the Ocean Instruments device database and returns the model
    name (e.g. "SoundTrap 300 HF"). Results are cached per serial number.

    Parameters
    ----------
    serial_number : str or int
        SoundTrap recorder serial number.

    Returns
    -------
    str or None
        Model name as reported by Ocean Instruments, or None if the
        serial number is not found or the query fails.
    """
    serial_number = str(serial_number)
    if serial_number in _model_cache:
        return _model_cache[serial_number]

    try:
        resp = requests.get(
            _OI_DEVICE_SEARCH_URL.format(serial_number=serial_number),
            timeout=15,
        )
        resp.raise_for_status()
        devices = resp.json()
    except Exception as e:
        logger.warning(
            "Model lookup failed for serial %s: %s: %s. Returning None.",
            serial_number, type(e).__name__, e,
        )
        return None

    if not devices:
        logger.warning(
            "Serial %s not found in the Ocean Instruments database. "
            "Cannot determine recorder model. Returning None.",
            serial_number,
        )
        return None

    # If multiple devices match, pick the one whose serialNo matches exactly
    matches = [d for d in devices if d.get("serialNo") == serial_number]
    device = matches[0] if matches else devices[0]
    model = device.get("modelName")

    _model_cache[serial_number] = model
    return model


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

    Logs
    ----
    WARNING
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
        logger.warning(
            "Serial %s not found in the Ocean Instruments database. Returning None.",
            serial_number,
        )
        return None
    except Exception as e:
        logger.warning(
            "Sensitivity lookup failed for serial %s (%s gain): %s: %s. Returning None.",
            serial_number, gain_type, type(e).__name__, e,
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

    Logs
    ----
    WARNING
        If the most frequent value covers less than `threshold` of the
        values, or if all values are missing.
    """
    usable = [v for v in values if pd.notna(v)]

    if not usable:
        logger.warning("No usable %s values found.", label)
        return None

    counts = Counter(usable)
    most_common, n = counts.most_common(1)[0]
    fraction = n / len(usable)

    if fraction < threshold:
        others = ", ".join(f"{v} ×{c}" for v, c in counts.most_common(4)[1:])
        logger.warning(
            "Inconsistent %s: %s covers %.1f%% of %d files. Also seen: %s",
            label, most_common, fraction * 100, len(usable), others,
        )

    return most_common



class _WarningCollector(logging.Handler):
    """Log handler that collects WARNING messages into a list."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.messages = []

    def emit(self, record):
        if record.levelno >= logging.WARNING:
            self.messages.append(self.format(record))

    def clear(self):
        self.messages = []


def scan_for_audio_metadata(root_dir, output_csv=None, min_files=1,
                            files_num_workers=16,
                            subsampling_fraction=None, max_sample=20,
                            recorder_type="SoundTrap", gain_type="High"):
    """
    Build a metadata catalogue for every deployment folder under a root directory.

    Finds folders containing audio files, then extracts the recorder
    serial number, calibration gain, recording interval, and audio file
    properties for each one. The recorder model is looked up automatically
    from the Ocean Instruments database using the serial number. Folders
    are processed one at a time so that the per-file progress bar renders
    correctly in Jupyter notebooks. File headers within each folder are
    read concurrently.

    Parameters
    ----------
    root_dir : str
        Path to crawl. Can be an S3 path (e.g.
        "s3://neracoos-pam-data-ingest/Wellfleet") or a local path
        (e.g. "/data/Wellfleet" or "D:\\Data\\Wellfleet").
    output_csv : str or None, optional
        Path to save the resulting DataFrame as a CSV file. If None, no
        file is written. Default is None.
    min_files : int, optional
        Minimum number of audio files a folder must contain to be
        included. Default is 1.
    files_num_workers : int, optional
        Number of concurrent header reads per folder. Default is 16.
        Higher values tend to hit S3 throttling rather than improve speed.
    subsampling_fraction : float or None, optional
        Fraction of files to read per folder (0.0–1.0). If None, all
        files are read. Useful for large deployments where recorder
        settings are constant and reading every header is wasteful.
        Passed through to `retrieve_audio_files_metadata`. Default is
        None.
    max_sample : int or None, optional
        Maximum number of files to read per folder, regardless of
        `subsampling_fraction`. Default is 20. Set to None to disable.
    recorder_type : str, optional
        Recorder type, used for serial number parsing and calibration
        lookup. Only "SoundTrap" is currently supported. Default is
        "SoundTrap".
    gain_type : {'High', 'Low'}, optional
        Gain setting used during the deployments, passed to
        `lookup_soundtrap_sysgain`. Default is "High".

    Returns
    -------
    pandas.DataFrame
        One row per folder, with columns 'folder', 'n_files',
        'recorder_model', 'recorder_serial_number', 'gain_type',
        'sysgain', 'recording_interval_sec',
        'recording_start_datetime', 'recording_end_datetime', the
        summary fields from `summarize_audio_files_metadata`, and
        'error'. 'n_files' is always the total file count in the
        folder, regardless of sampling. Folders that failed have the
        reason in 'error' and NaN elsewhere.
    """
    folders = list_audio_folders(root_dir, min_files=min_files)

    if not folders:
        return pd.DataFrame(columns=["folder", "n_files", "error"])

    # Resume from a previous run if the CSV already exists
    rows = []
    already_scanned = set()
    if output_csv is not None:
        if os.path.exists(output_csv):
            previous = pd.read_csv(output_csv)
            already_scanned = set(previous["folder"])
            rows = previous.to_dict("records")
            print(f"\nResuming: {len(already_scanned)} folder(s) already scanned, "
                  f"{len(folders) - len(already_scanned)} remaining.")

    # Handler that captures warning messages per folder
    warning_collector = _WarningCollector()
    logger.addHandler(warning_collector)

    with tqdm(total=len(folders), desc="Folders", unit=" folder",
              initial=len(already_scanned)) as pbar:
        for folder, audio_files in folders.items():
            if folder in already_scanned:
                continue
            pbar.set_postfix_str(os.path.basename(folder), refresh=True)
            warning_collector.clear()
            row = {"folder": folder, "n_files": len(audio_files), "error": None}
            try:
                row["recorder_type"] = recorder_type
                row["recorder_serial_number"] = retrieve_recorder_serial_number(
                    audio_files, recorder_type=recorder_type
                )
                row["recorder_model"] = lookup_soundtrap_model(
                    row["recorder_serial_number"]
                )
                row["gain_type"] = gain_type
                row["sysgain"] = lookup_soundtrap_sysgain(
                    row["recorder_serial_number"], gain_type=gain_type,
                    model=row["recorder_model"] or "ST300HF",
                )
                row["recording_interval_sec"] = retrieve_recording_interval(audio_files)

                # Extract start and end datetimes from file names
                times = sorted(
                    t for t in filename_to_datetime(
                        [os.path.basename(f) for f in audio_files]
                    )
                    if pd.notna(t)
                )
                row["recording_start_datetime"] = times[0] if times else None
                row["recording_end_datetime"] = times[-1] if times else None

                metadata = retrieve_audio_files_metadata(
                    audio_files, num_workers=files_num_workers,
                    subsampling_fraction=subsampling_fraction,
                    max_sample=max_sample,
                )
                row.update(summarize_audio_files_metadata(metadata))
            except Exception as e:
                row["error"] = f"{type(e).__name__}: {e}"
            row["warnings"] = "; ".join(warning_collector.messages) or None
            rows.append(row)
            pbar.update(1)

            # Write CSV after each folder so partial results survive interrupts
            if output_csv is not None:
                pd.DataFrame(rows).to_csv(output_csv, index=False)

    logger.removeHandler(warning_collector)

    df = pd.DataFrame(rows)

    if output_csv is not None:
        print(f"\nMetadata saved to {output_csv}")

    return df


# Backward-compatible aliases for the old S3-only names
list_audio_files_s3 = list_audio_files
list_audio_folders_s3 = list_audio_folders
scan_bucket_for_audio_metadata = scan_for_audio_metadata


if __name__ == "__main__":
    import datetime
    log_file = f"s3_metadata_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        handlers=[
            logging.StreamHandler(),          # console
            logging.FileHandler(log_file),    # timestamped log file
        ],
    )

    deployment_dir = "s3://neracoos-pam-data-ingest/Wellfleet"
    output_csv = "s3://neracoos-pam-output/tests_xavier/wellfleet_metadata.csv"

    df = scan_for_audio_metadata(deployment_dir,
                                  min_files=1,
                                  files_num_workers=32,
                                  subsampling_fraction=0.3,
                                  max_sample=30,
                                  recorder_type="SoundTrap",
                                  gain_type="High",
                                  output_csv=output_csv)
    print(df)