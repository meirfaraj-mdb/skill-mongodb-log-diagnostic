"""Optional node-report graph: log-side load timeline and hot-document incident windows.

One PNG (embedded in the report Markdown as a base64 data URI, like APPENDIX 1):

* top panel   - the whole log day per UTC minute: slow operations/min (left axis) and write
                conflicts/min (right axis), detected incident windows shaded;
* one panel per incident window (max 3, +/- ``pad_minutes``): writes/min to the window's top hot
                document as bars, write conflicts/min and slow operations/min as lines.

Only the parser >= 1.6 ``timeline`` and ``concurrency.hot_write_targets`` are read (no metrics
needed, so the graph also helps nodes without Atlas metrics). matplotlib is imported lazily;
``ImportError`` is handled by the caller.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import concurrency_findings as cf

HEADING = "### Write contention timeline (log)"


def _t(label: str) -> datetime:
    return datetime.fromisoformat(label.replace("Z", "+00:00"))


def render_png(extract: dict, findings: dict, png_path: str, title: str, pad_minutes: int = 15) -> bool:
    """Write the PNG; False when there is nothing to draw (no timeline or no incident window)."""
    minutes = cf.timeline_minutes(extract)
    windows = [w for w in (findings or {}).get("incident_windows") or []][:3]
    if not minutes or not windows:
        return False
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import dates as mdates, pyplot as plt

    targets = {t.get("target_id"): cf.target_minutes(t) for t in
               ((extract.get("concurrency") or {}).get("hot_write_targets") or [])}
    rows = 1 + len(windows)
    fig, axes = plt.subplots(rows, 1, figsize=(11, 2.9 * rows), squeeze=False)
    xs = sorted(minutes)
    slow = [cf._num(minutes[x].get("slow")) for x in xs]
    conf = [cf._num(minutes[x].get("writeConflicts")) for x in xs]

    ax = axes[0][0]
    ax.plot(xs, slow, color="#1f6f8b", lw=0.9, label="slow operations / min")
    ax.set_ylabel("slow ops / min", color="#1f6f8b")
    ax2 = ax.twinx()
    ax2.plot(xs, conf, color="#c0392b", lw=0.9, alpha=0.85, label="write conflicts / min")
    ax2.set_ylabel("write conflicts / min", color="#c0392b")
    for w in windows:
        ax.axvspan(_t(w["start_utc"]), _t(w["end_utc"]) + timedelta(minutes=1),
                   color="#f39c12" if w["verdict"] == cf.LIKELY else "#bbbbbb", alpha=0.3)
    ax.set_title("Whole log day (UTC) — shaded: detected incident windows (orange = hot-document contention likely)",
                 fontsize=9, loc="left")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %H:%M"))
    ax.grid(alpha=0.25)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=7, loc="upper left")

    for i, w in enumerate(windows, start=1):
        ax = axes[i][0]
        a = _t(w["start_utc"]) - timedelta(minutes=pad_minutes)
        b = _t(w["end_utc"]) + timedelta(minutes=pad_minutes)
        span = [a + timedelta(minutes=k) for k in range(int((b - a).total_seconds() // 60) + 1)]
        hot = (w.get("hot_documents_in_window") or [{}])[0]
        tmin = targets.get(hot.get("target_id"), {})
        bars = [cf._num((tmin.get(x) or {}).get("occur")) for x in span]
        ax.bar(span, bars, width=1 / 1440 * 0.85, color="#f39c12", alpha=0.85,
               label=f"writes/min to hot document {hot.get('target_id', '-')}" if tmin else "hot-document writes/min")
        ax.set_ylabel("hot-doc writes / min", color="#b9770e")
        ax2 = ax.twinx()
        ax2.plot(span, [cf._num((minutes.get(x) or {}).get("writeConflicts")) for x in span], color="#c0392b",
                 lw=1.4, label="write conflicts / min (all)")
        ax3 = ax.twinx()
        ax3.spines["right"].set_position(("axes", 1.08))
        ax3.plot(span, [cf._num((minutes.get(x) or {}).get("slow")) for x in span], color="#1f6f8b", lw=1.2,
                 ls="--", label="slow operations / min")
        ax2.set_ylabel("conflicts / min", color="#c0392b")
        ax3.set_ylabel("slow ops / min", color="#1f6f8b")
        ax.axvspan(_t(w["start_utc"]), _t(w["end_utc"]) + timedelta(minutes=1), color="#f39c12", alpha=0.12)
        verdict = "hot-document contention likely" if w["verdict"] == cf.LIKELY else w["verdict"].replace("_", " ")
        share = hot.get("share_of_window_conflicts")
        ax.set_title(f"{w['start_utc'][:16].replace('T', ' ')}–{w['end_utc'][11:16]} UTC · {verdict}"
                     + (f" · {hot.get('namespace')} · {share:.0%} of window conflicts on one document" if share else ""),
                     fontsize=9, loc="left")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax.grid(alpha=0.25)
        hs, ls = [], []
        for axis in (ax, ax2, ax3):
            h, l = axis.get_legend_handles_labels()
            hs += h
            ls += l
        ax.legend(hs, ls, fontsize=7, loc="upper left")
    fig.suptitle(title, fontsize=11, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(png_path, dpi=130)
    plt.close(fig)
    return True


def section_markdown(data_uri: str | None, findings: dict, unavailable_reason: str | None = None) -> str:
    lines = [HEADING, ""]
    if data_uri:
        lines += [f"![Write contention timeline]({data_uri})", "",
                  "_Log-side counts per UTC minute (slow operations, write conflicts, writes to the hot "
                  "document); fast operations below the slow threshold are not logged, so values are lower bounds._"]
    else:
        lines.append(f"Graph not available: {unavailable_reason or 'no incident window'}.")
    return "\n".join(lines) + "\n"
