"""
audio_qc_basics_UI.py — Panel + Pyodide app
============================================
Run audio QC checks on a selected local directory and display results.

Convert to standalone HTML (audio_qc_basics.py must be in the same folder):
    panel convert audio_qc_basics_UI.py --to pyodide --out . --resources audio_qc_basics.py --requirements matplotlib

Then open audio_qc_basics_UI.html in Chrome / Edge.
"""

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

from audio_qc_basics import (
    run_qc, print_qc_report, filename_to_datetime,
    get_audio_info_from_bytes, _compute_qc_stats,
)

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
        from datetime import timedelta

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
        from audio_qc_basics import list_audio_files
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
