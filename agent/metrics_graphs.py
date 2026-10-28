"""One-page per-node metric graphs (report Appendix 2).

Ported from slowInvest ``sl_plot/graphs.py`` (``plot_all_metricsForProcess`` /
``plot_metric_group``): one panel per configured metric group, every metric of the group on the
same panel.  Unlike slowInvest, all panels are laid out on a single A4 page so the whole node is
readable at a glance.

Pure functions over the stored ``metrics.json`` envelope; matplotlib is imported lazily so the
rest of the pipeline keeps working (with an explicit "graph unavailable" note) without it.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from .metrics_catalog import MEASUREMENT_ALIASES, selected_groups

A4_PORTRAIT_INCHES = (8.27, 11.69)
COLUMNS = 3
_BYTE_FACTORS = {"BYTES": 1, "KILOBYTES": 1024, "MEGABYTES": 1024 ** 2, "GIGABYTES": 1024 ** 3}
_REVERSE_ALIASES = {v: k for k, v in MEASUREMENT_ALIASES.items()}


def _walk_measurements(obj, depth=0):
    """Yield measurement dicts ({name, units, dataPoints}) from any envelope/page nesting."""
    if depth > 10:
        return
    if isinstance(obj, dict):
        if isinstance(obj.get("name"), str) and isinstance(obj.get("dataPoints"), list):
            yield obj
            return
        for value in obj.values():
            if isinstance(value, (dict, list)):
                yield from _walk_measurements(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_measurements(value, depth + 1)


def _ts(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)


def load_series(metrics_payload) -> dict:
    """``{api_name: {"units": str, "points": [(datetime, float), ...]}}`` (nulls dropped, merged)."""
    series: dict[str, dict] = {}
    for item in _walk_measurements(metrics_payload):
        entry = series.setdefault(item["name"], {"units": item.get("units") or "SCALAR", "points": {}})
        for point in item["dataPoints"]:
            if not isinstance(point, dict):
                continue
            value, when = point.get("value"), _ts(point.get("timestamp"))
            if when is None or isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if math.isfinite(value):
                entry["points"][when] = float(value)
    return {name: {"units": e["units"], "points": sorted(e["points"].items())} for name, e in series.items()}


def _lookup(series, template_name):
    for candidate in (template_name, MEASUREMENT_ALIASES.get(template_name), _REVERSE_ALIASES.get(template_name)):
        if candidate and candidate in series:
            return series[candidate]
    return None


def plan_panels(series: dict, config: dict | None = None) -> tuple[list[dict], list[str]]:
    """Return (panels with data, selected groups without any non-zero value)."""
    panels, empty = [], []
    for group, names in selected_groups(config):
        lines = []
        for name in names:
            found = _lookup(series, name)
            if found and found["points"]:
                lines.append({"name": name, "units": found["units"], "points": found["points"]})
        if any(v for line in lines for _, v in line["points"]):
            panels.append({"group": group, "lines": lines})
        else:
            empty.append(group)
    return panels, empty


def _family(units):
    units = (units or "SCALAR").upper()
    if units in _BYTE_FACTORS:
        return "bytes"
    if units.startswith(tuple(_BYTE_FACTORS)) and units.endswith("_PER_SECOND"):
        return "bytes/s"
    return units


def _scale(units):
    units = (units or "").upper()
    for prefix, factor in _BYTE_FACTORS.items():
        if units == prefix or units == prefix + "_PER_SECOND":
            return factor
    return 1


def _human(value, family):
    if family in ("bytes", "bytes/s"):
        suffix = "/s" if family == "bytes/s" else ""
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if abs(value) < 1024 or unit == "TiB":
                return f"{value:.0f}{unit}{suffix}" if unit == "B" else f"{value:.1f}{unit}{suffix}"
            value /= 1024
    if family == "PERCENT":
        return f"{value:.0f}%"
    for limit, unit in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= limit:
            return f"{value / limit:.1f}{unit}"
    if value == 0:
        return "0"
    return f"{value:.3g}" if abs(value) < 10 else f"{value:.0f}"


_FAMILY_LABEL = {"bytes": "bytes", "bytes/s": "bytes/s", "PERCENT": "%", "MILLISECONDS": "ms", "SECONDS": "seconds",
                 "SCALAR_PER_SECOND": "per second", "SCALAR": "count"}


def _short(name, group_names):
    """Drop the prefix shared by the group's metric names so legends stay compact."""
    parts = [n.split("_") for n in group_names]
    common = 0
    while all(len(p) > common + 1 and p[common] == parts[0][common] for p in parts):
        common += 1
    return "_".join(name.split("_")[common:]) or name


def render_one_page(panels, title, png_path, svg_path=None, subtitle=None):
    """Render every panel onto one A4 portrait page; returns written paths."""
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import dates as mdates, pyplot as plt
    from matplotlib.ticker import FuncFormatter

    rows = max(1, math.ceil(len(panels) / COLUMNS))
    fig, axes = plt.subplots(rows, COLUMNS, figsize=A4_PORTRAIT_INCHES, squeeze=False)
    fig.suptitle(title, fontsize=10, fontweight="bold", y=0.996)
    if subtitle:
        fig.text(0.5, 0.981, subtitle, ha="center", va="top", fontsize=6.5, color="#555555")
    colors = plt.get_cmap("tab10").colors
    for index, ax in enumerate(axes.flat):
        if index >= len(panels):
            ax.axis("off")
            continue
        panel = panels[index]
        families = []
        for line in panel["lines"]:
            if _family(line["units"]) not in families:
                families.append(_family(line["units"]))
        axes_by_family = {families[0]: ax}
        if len(families) > 1:
            twin = ax.twinx()
            for family in families[1:]:
                axes_by_family[family] = twin
        names = [line["name"] for line in panel["lines"]]
        handles = []
        for number, line in enumerate(panel["lines"]):
            family = _family(line["units"])
            target = axes_by_family[family]
            factor = _scale(line["units"])
            xs = [t for t, _ in line["points"]]
            ys = [v * factor for _, v in line["points"]]
            style = "-" if target is ax else "--"
            (handle,) = target.plot(xs, ys, style, linewidth=0.8, color=colors[number % len(colors)],
                                    label=_short(line["name"], names))
            handles.append(handle)
        legend_rows = math.ceil(len(handles) / (2 if len(handles) > 4 else 1))
        headroom = 1 + 0.11 * legend_rows  # keep the legend band above the data
        for family, target in axes_by_family.items():
            target.yaxis.set_major_formatter(FuncFormatter(lambda v, _p, f=family: _human(v, f)))
            target.tick_params(axis="y", labelsize=5, pad=1)
            low, high = target.get_ylim()
            low = 0 if low >= 0 or all(v >= 0 for line in panel["lines"] for _, v in line["points"]) else low
            target.set_ylim(low, low + (high - low) * headroom if high > low else 1)
        ax.set_title(panel["group"].replace("_", " ").title(), fontsize=7, pad=2)
        label = _FAMILY_LABEL.get(families[0], families[0].lower())
        if len(families) > 1:
            label += "  (dashed: " + ", ".join(_FAMILY_LABEL.get(f, f.lower()) for f in families[1:]) + ")"
        ax.set_ylabel(label, fontsize=5, labelpad=1)
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=5))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=timezone.utc))
        ax.tick_params(axis="x", labelsize=5, pad=1)
        ax.grid(True, linewidth=0.3, alpha=0.5)
        ax.legend(handles=handles, fontsize=4.5, loc="upper left", frameon=True, framealpha=0.7,
                  handlelength=1.2, borderpad=0.3, labelspacing=0.2, ncol=2 if len(handles) > 4 else 1)
    fig.tight_layout(rect=(0, 0, 1, 0.975 if subtitle else 0.988), h_pad=0.8, w_pad=0.6)
    fig.subplots_adjust(top=0.952 if subtitle else 0.962)
    written = []
    fig.savefig(png_path, dpi=170)
    written.append(png_path)
    if svg_path:
        fig.savefig(svg_path, format="svg")
        written.append(svg_path)
    plt.close(fig)
    return written


def appendix_markdown(png_name, panels, empty, payload, svg_name=None, unavailable_reason=None):
    """Markdown body for Appendix 2 (heading is added by the caller)."""
    window = payload.get("window", {}) if isinstance(payload, dict) else {}
    plan = payload.get("measurement_plan", {}) if isinstance(payload, dict) else {}
    lines = []
    if window:
        lines.append(f"Window (UTC): {window.get('since', '?')} to {window.get('until', '?')}, "
                     f"granularity {window.get('granularity', 'n/a')}. This is the rolling API window "
                     "at collection time and may differ from the log day.")
        lines.append("")
    if unavailable_reason:
        lines.append(f"Metric graphs unavailable: {unavailable_reason}.")
    else:
        alt = "Per-node metric graphs: " + ", ".join(p["group"] for p in panels)
        lines.append(f"![{alt}]({png_name})")
        if svg_name:
            lines.extend(["", f"Vector version: [{svg_name}]({svg_name})"])
    if empty:
        lines.extend(["", "Groups without non-zero data in the window (not drawn): " + ", ".join(empty) + "."])
    if plan.get("unsupported"):
        lines.extend(["", "Configured metrics not offered by the measurements API (not requested): "
                      + ", ".join(plan["unsupported"]) + "."])
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if errors:
        failed = sorted({m for e in errors for m in e.get("metrics", [])})
        lines.extend(["", "Metrics whose request failed: " + ", ".join(failed) + "."])
    return "\n".join(lines)
