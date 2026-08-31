# PAMHub_tools

Scripts and tools for the PAMHub cloud platform.

---

## Scripts

### `audio_qc_basics_UI.py` — Audio QC Web App

A browser-based tool for running basic quality control checks on a local directory of audio files (WAV, FLAC, AIF). Built with [Panel](https://panel.holoviz.org/) and [Pyodide](https://pyodide.org/) so it runs entirely in the browser — no server or Python installation required for end users.

**What it does:**
- Lets the user select a local folder containing audio recordings
- Extracts metadata (filename, start time, duration, sample rate) from file headers without uploading any data
- Reports: number of files, total dataset size, recording duration, duty cycle (on/off time), gaps between files, and any inconsistencies (unexpected durations, gap anomalies, mixed sample rates)
- Displays a duty cycle pie chart and an inconsistency indicator

**Live demo:**
[https://xaviermouy.github.io/PAMHub_tools/audio_qc_basics_UI.html](https://xaviermouy.github.io/PAMHub_tools/audio_qc_basics_UI.html)

**To regenerate the HTML after editing the Python source:**

```bash
panel convert audio_qc_basics_UI.py --to pyodide --out . --resources audio_qc_basics.py --requirements matplotlib
```

This produces two files that must be deployed together:
- `audio_qc_basics_UI.html` — the app
- `audio_qc_basics_UI.resources.zip` — bundled Python dependency (`audio_qc_basics.py`) fetched by Pyodide at startup

**Dependencies (Python source):** `panel`, `matplotlib`

---

### `s3_acoustic_metadata_scanner.py` — S3 Audio Metadata Scanner

Scans an S3 bucket for audio files (WAV, FLAC, AIF) and builds a metadata
catalogue with one row per deployment folder. For each folder it extracts:

- **Recorder info**: serial number, model (auto-detected from the Ocean
  Instruments database), gain type, and end-to-end system gain (sysgain)
- **Recording schedule**: start/end datetimes, duty cycle interval, file
  duration
- **Audio properties**: sample rate, bit depth, number of channels, format

The main entry point is `scan_bucket_for_audio_metadata()`.

**Parameters:**

| Parameter | Default | Description |
|---|---|---|
| `root_dir` | *(required)* | S3 path to crawl, e.g. `"s3://my-bucket/Wellfleet"` |
| `output_csv` | `None` | Path to save results as CSV. `None` = no file written. Supports resume: if the CSV already exists, previously scanned folders are skipped |
| `min_files` | `1` | Minimum audio files for a folder to be included |
| `files_num_workers` | `16` | Concurrent S3 header reads per folder |
| `subsampling_fraction` | `None` | Fraction of files to read (0.0–1.0). `None` = all files (still subject to `max_sample` cap) |
| `max_sample` | `20` | Maximum number of files to read per folder. Caps the sample regardless of `subsampling_fraction`. Set to `None` to disable |
| `recorder_type` | `"SoundTrap"` | Recorder type for filename parsing. Only `"SoundTrap"` currently supported |
| `gain_type` | `"High"` | Gain setting (`"High"` or `"Low"`) |

**Output columns:**

`folder`, `n_files`, `recorder_type`, `recorder_serial_number`,
`recorder_model`, `gain_type`, `sysgain`, `recording_interval_sec`,
`recording_start_datetime`, `recording_end_datetime`,
`recording_sample_rate_hz`, `recording_n_channels`, `recording_bit_depth`,
`recording_format`, `recording_duration_sec`, `n_errors`, `error`,
`warnings`

**Example usage (Jupyter notebook on Nebari):**

```python
import logging
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)

from s3_acoustic_metadata_scanner import scan_bucket_for_audio_metadata

# Scan all deployments under a site, reading up to 20 file headers per folder
df = scan_bucket_for_audio_metadata(
    "s3://neracoos-pam-data-ingest/Wellfleet",
    output_csv="wellfleet_metadata.csv",
    max_sample=20,
    gain_type="High",
)

df
```

**Dependencies:** `s3fs`, `soundfile`, `pandas`, `tqdm`, `requests`,
`pyhydrophone`, `ecosound`

---

### `upload_data_to_cloud.py` — Upload Data to AWS S3

> **Status: Draft / In development — not operational**

A script for uploading local audio data to an AWS S3 bucket. Intended to support throttled, resumable transfers with concurrency control and per-file logging, replicating the behaviour of `aws s3 sync` via `boto3`.

**Dependencies:** `boto3`, `tqdm`