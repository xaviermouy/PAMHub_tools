# PAMHub_tools

Scripts and tools for the PAMHub cloud platform.

---

## Setup

**Requirements:** Python 3.10 or later.

1. Clone the repository:

```bash
git clone https://github.com/xaviermouy/PAMHub_tools.git
cd PAMHub_tools
```

2. (Recommended) Create and activate a virtual environment:

```bash
python -m venv venv

# Linux / macOS
source venv/bin/activate

# Windows
venv\Scripts\activate
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Run scripts from the `scripts/` directory:

```bash
cd scripts
python acoustic_metadata_scanner.py
```

Or import functions directly in your own code or Jupyter notebooks:

```python
import sys
sys.path.insert(0, "/path/to/PAMHub_tools/scripts")

from acoustic_metadata_scanner import scan_for_audio_metadata
```

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

### `acoustic_metadata_scanner.py` — Audio Metadata Scanner

Scans a directory tree (S3 bucket or local folder) for audio files (WAV,
FLAC, AIF) and builds a metadata catalogue with one row per deployment
folder. For each folder it extracts:

- **Recorder info**: serial number, model (auto-detected from the Ocean
  Instruments database), gain type, and end-to-end system gain (sysgain)
- **Recording schedule**: start/end datetimes, duty cycle interval, file
  duration
- **Audio properties**: sample rate, bit depth, number of channels, format

The main entry point is `scan_for_audio_metadata()`.

**Parameters:**

| Parameter | Default | Description |
|---|---|---|
| `root_dir` | *(required)* | Path to crawl — S3 (e.g. `"s3://my-bucket/Wellfleet"`) or local (e.g. `"/data/Wellfleet"`, `"D:\Data\Wellfleet"`) |
| `output_csv` | `None` | Path to save results as CSV. `None` = no file written. Supports resume: if the CSV already exists, previously scanned folders are skipped |
| `min_files` | `1` | Minimum audio files for a folder to be included |
| `files_num_workers` | `16` | Concurrent header reads per folder |
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

**Example usage — S3 (Jupyter notebook on Nebari):**

```python
import logging
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)

from acoustic_metadata_scanner import scan_for_audio_metadata

# Scan all deployments under a site on S3
df = scan_for_audio_metadata(
    "s3://neracoos-pam-data-ingest/Wellfleet",
    output_csv="wellfleet_metadata.csv",
    max_sample=20,
    gain_type="High",
)

df
```

**Example usage — local folder (Windows or Linux):**

```python
from acoustic_metadata_scanner import scan_for_audio_metadata

# Scan deployments stored locally
df = scan_for_audio_metadata(
    r"D:\Data\Wellfleet",
    output_csv="wellfleet_metadata.csv",
    max_sample=20,
)
```

**Dependencies:** `fsspec`, `s3fs`, `soundfile`, `pandas`, `tqdm`,
`requests`, `pyhydrophone`, `ecosound`

---

### `upload_data_to_cloud.py` — Upload Data to AWS S3

> **Status: Draft / In development — not operational**

A script for uploading local audio data to an AWS S3 bucket. Intended to support throttled, resumable transfers with concurrency control and per-file logging, replicating the behaviour of `aws s3 sync` via `boto3`.

**Dependencies:** `boto3`, `tqdm`