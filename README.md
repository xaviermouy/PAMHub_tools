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

### `upload_data_to_cloud.py` — Upload Data to AWS S3

> **Status: Draft / In development — not operational**

A script for uploading local audio data to an AWS S3 bucket. Intended to support throttled, resumable transfers with concurrency control and per-file logging, replicating the behaviour of `aws s3 sync` via `boto3`.

**Dependencies:** `boto3`, `tqdm`