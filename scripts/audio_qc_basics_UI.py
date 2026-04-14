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
    # Browser mode: the file input is created dynamically in document.body so it
    # lives outside Panel's shadow DOM and is immediately clickable.
    # window.pyOpenFilePicker is callable from any onclick context (even shadow DOM).

    picker_widget = pn.pane.HTML(
        """
        <button
            onclick="if(window.pyOpenFilePicker)window.pyOpenFilePicker(event)"
            style="padding:10px 22px; background:#0072B5; color:white; border:none;
                   border-radius:6px; cursor:pointer; font-size:15px; font-family:sans-serif;
                   box-shadow:0 2px 4px rgba(0,0,0,.2);">
            Browse...
        </button>
        """,
        height=55,
    )

    import asyncio

    async def _on_files_changed(event):
        """Handle the file-input change event entirely in Python."""
        from datetime import timedelta

        file_list = event.target.files
        n = file_list.length
        if n == 0:
            status.object = "*No files received.*"
            return

        audio_exts = {"wav", "flac", "aif", "aiff"}
        js_files = [
            file_list.item(i) for i in range(n)
            if file_list.item(i).name.rsplit(".", 1)[-1].lower() in audio_exts
        ]
        if not js_files:
            status.object = "*No audio files found.*"
            return

        dir_name = js_files[0].webkitRelativePath.split("/")[0]

        records = []
        total_bytes = 0
        for f in js_files:
            fname = f.name
            file_size = int(f.size)
            total_bytes += file_size

            try:
                t_start = filename_to_datetime(fname)
            except ValueError:
                continue

            sr, dur = None, None
            try:
                array_buf = await f.slice(0, 512).arrayBuffer()
                header_bytes = bytes(js.Uint8Array.new(array_buf).to_py())
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
            n_found = results["n_files"]
            status.object = f"**{n_found} file(s)** found in `{dir_name}/`"

    def _open_file_picker(e=None):
        """Create a temporary <input webkitdirectory> in document.body and click it.
        Appending to body keeps the element outside Panel's shadow DOM so the
        browser's user-gesture check passes and the change event fires normally."""
        inp = js.document.createElement("input")
        inp.type = "file"
        inp.multiple = True
        inp.setAttribute("webkitdirectory", "")
        inp.style.display = "none"
        js.document.body.appendChild(inp)

        def _inp_onchange(change_event):
            asyncio.ensure_future(_on_files_changed(change_event))
            try:
                js.document.body.removeChild(inp)
            except Exception:
                pass

        inp.onchange = create_proxy(_inp_onchange)
        inp.click()

    js.window.pyOpenFilePicker = create_proxy(_open_file_picker)

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

if __name__ == "__main__" and not IN_PYODIDE:
    pn.serve(__file__, show=True, autoreload=True)
