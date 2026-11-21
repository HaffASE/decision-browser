"""Render short and long browser results together, keeping the two cohorts separate."""

from __future__ import annotations

import csv
import json
import math
import subprocess
import tempfile
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from statistics import median
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "docs" / "benchmarks"
ENGINES = ("terra", "decision-browser", "jev-ultrafast", "jev-ra")
ROUTES = ("openai", "opencode")
LABELS = ("Standalone Terra", "Decision Browser Core", "jev-ultrafast", "jev-ra")
MODELS = {"openai": "GPT-6 Luna", "opencode": "Jev 1.13", "responses": "GPT-6.1 Terra"}
LONG_LABELS = {
    "rail_itinerary": "Rail", "library_research": "Library", "equipment_pickup": "Equipment",
    "support_handoff": "Support", "exhibit_planner": "Exhibit",
}
BG, FG, MUTED, RULE = "#12222D", "#F2F6F6", "#B9C9D1", "#415460"
GREEN, AMBER = "#7AE0BC", "#FFC786"
COLORS = ("#F9BB8A", GREEN, "#99BFF5", "#C6ADEA")
WIDTH, HEIGHT = 1080, 680


def read_rows(path):
    with path.open(newline="") as source:
        return list(csv.DictReader(source))


def historical():
    """Select exactly the seven historical series shown in the previous GIF."""
    tasks = json.loads((BENCH / "2026-11-03-main-providers/summary.json").read_text())["tasks"]
    rows = []
    for study, columns in [
        ("terra-medium", ("arm", "terra")),
        ("decision-browser-article-cost", None),
        ("main-providers", ("route", "opencode")),
        ("browser-providers", ("route", "opencode")),
    ]:
        for r in read_rows(BENCH / f"2026-11-03-{study}/runs.csv"):
            if columns and r[columns[0]] != columns[1]:
                continue
            engine = r.get("engine", "terra")
            if study == "browser-providers" and engine == "decision-browser":
                continue
            route = "responses" if engine == "terra" else r.get("route", "openai")
            rows.append({
                "case": r["case"], "repetition": r["repetition"], "engine": engine,
                "route": route, "wall_ms": r.get("wall_ms", r.get("wall_process_ms")),
                "confirmed": r["confirmed"], "estimated_usd": r.get("estimated_usd", r.get("known_estimated_usd")),
            })
    return tasks, rows


def indexed(rows, cases):
    cells = {}
    for r in rows:
        key = r["engine"], r["route"], r["case"], int(r["repetition"])
        if key in cells or r["confirmed"] not in {"True", "False"} or int(r["wall_ms"]) <= 0:
            raise ValueError(f"Invalid or duplicate chart cell {key}")
        cells[key] = r
    required = {(engine, route, c, rep) for engine in ENGINES
                for route in (("responses",) if engine == "terra" else ROUTES)
                for c in cases for rep in (1, 2, 3)}
    if cells.keys() != required:
        raise ValueError(f"Comparison matrix differs from plan: {cells.keys() ^ required}")
    return cells


def totals(rows):
    costs = [Decimal(r["estimated_usd"]) for r in rows if r["estimated_usd"] not in ("", None)]
    known = sum(Decimal(r.get("known_estimated_usd") or r["estimated_usd"] or 0) for r in rows)
    return {
        "attempts": len(rows), "confirmed": sum(r["confirmed"] == "True" for r in rows),
        "median_wall_ms": median(int(r["wall_ms"]) for r in rows), "priced": len(costs),
        "estimated_usd": str(sum(costs)) if len(costs) == len(rows) else None, "known_estimated_usd": str(known),
    }


def money(total):
    return f"${Decimal(total):.5f}" if total is not None else "unknown"


def seconds(ms):
    value = (Decimal(str(ms)) / 1000).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{value:.2f} s"


def display_cost(arm):
    if arm["estimated_usd"] is not None:
        return money(arm["estimated_usd"])
    # Floor the lower bound so rounded figures do not overstate known charges.
    bound = Decimal(arm["known_estimated_usd"]).quantize(Decimal("0.00001"), rounding="ROUND_FLOOR")
    return f"≥ ${bound:.5f}"


def txt(x, y, value, size=16, color=FG, bold=False, anchor="start"):
    return (f'<text x="{x}" y="{y}" fill="{color}" font-family="Arial" font-size="{size}" '
            f'font-weight="{700 if bold else 400}" text-anchor="{anchor}">{escape(str(value))}</text>')


def rect(x, y, w, h, color, radius=0):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{color}"/>'


def center(index, rep, long):
    return (350 + 150 * index + 16 * (rep - 2)) if long else (260 + 51 * index + 10 * (rep - 1))


def frame(cases, cells, long, route, page, cap):
    matrix = []
    for engine in ENGINES:
        actual_route = "responses" if engine == "terra" else route
        matrix.append([cells[engine, actual_route, c, rep] for c in cases for rep in (1, 2, 3)])
    stats = [totals(rows) for rows in matrix]
    count = len(cases) * 3
    cohort = "Long workflows" if long else "Short tasks"
    a = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}">',
        rect(0, 0, WIDTH, HEIGHT, BG), rect(32, 25, 24, 5, COLORS[0], 2),
        txt(66, 35, "FOUR BROWSER AGENTS", 15, COLORS[0], True),
        txt(1048, 35, f"03 NOV 2026   /   {page} OF 4", 15, MUTED, True, "end"),
        txt(30, 87, f"{cohort}  /  {MODELS[route]} libraries", 43, FG, True),
        txt(32, 123, f"{len(cases)} tasks × 3 repetitions per agent. Both provider views share the same Terra runs.", 20, MUTED),
        rect(32, 145, 1016, 1, RULE), txt(32, 175, "AGENT", 14, MUTED, True),
        txt(326, 175, "FULL PROCESS TIME", 14, MUTED, True),
        txt(782, 175, "MEDIAN", 14, MUTED, True, "end"), txt(915, 175, "CONFIRMED", 14, MUTED, True, "end"),
        txt(1048, 175, f"COST / {count}*", 14, MUTED, True, "end"),
    ]
    x0, width = 326, 320
    for n in range(6):
        x = x0 + n * width / 5
        a.extend([rect(x, 189, 1, 210, RULE), txt(x, 419, f"{n*cap/5:g} s", 13, MUTED, anchor="middle")])
    for j, (label, arm) in enumerate(zip(LABELS, stats, strict=True)):
        y = 214 + j * 54
        time = arm["median_wall_ms"] / 1000
        detail = "GPT-6.1 Terra · medium" if j == 0 else {"openai": "OpenAI Decisions", "opencode": "OpenCode"}[route]
        a.extend([txt(32, y, label, 20, FG, True), txt(32, y+20, detail, 15, MUTED),
                  rect(x0, y-9, width, 10, "#354854", 5), rect(x0, y-9, width*time/cap, 10, COLORS[j], 5),
                  txt(782, y+4, seconds(arm["median_wall_ms"]), 22, FG, True, "end"),
                  txt(915, y+4, f"{arm['confirmed']} / {count}", 21, FG, anchor="end"),
                  txt(1048, y+4, display_cost(arm), 17, COLORS[j], anchor="end")])
    a.extend([rect(32, 433, 1016, 1, RULE), txt(32, 464, "Every scored attempt", 23, FG, True),
              txt(704, 464, "●  Confirmed", 15, GREEN), txt(837, 464, "×  Unconfirmed", 15, AMBER),
              txt(32, 492, "SCENARIO" if long else "TASK NUMBER", 14, MUTED, True)])
    markers = []
    for j, rows in enumerate(matrix):
        y = 519 + j * 25
        a.append(txt(32, y, LABELS[j], 16, FG, True))
        for i, c in enumerate(cases):
            if not j:
                label = LONG_LABELS[c] if long else f"{i + 1:02}"
                a.append(txt(center(i, 2, long), 492, label, 14, MUTED, anchor="middle"))
            for rep in (1, 2, 3):
                row = rows[3*i + rep-1]
                ok = row["confirmed"] == "True"
                x, marker_y = center(i, rep, long), y - 5
                if ok:
                    a.append(f'<circle cx="{x}" cy="{marker_y}" r="3.5" fill="{GREEN}"/>')
                else:
                    a.append(f'<path d="M{x-3} {marker_y-3} l6 6 m0 -6 l-6 6" stroke="{AMBER}" stroke-width="2"/>')
                markers.append((x, marker_y, ok))
    protocol = ("Per attempt: 24 native steps, 24 model calls (including helpers), 180 s and 512 KiB of request data."
                if long else "Historical short suite. The libraries and Terra ran in separate benchmark series.")
    a.extend([rect(32, 614, 1016, 1, RULE), txt(32, 640, protocol, 15, MUTED),
              txt(32, 664, "* Estimated cost of scored calls (≥ = partial usage). Medians include early stops, startup and cleanup.", 15, MUTED), "</svg>"])
    return "".join(a), markers


def verify_frames(path, markers):
    meta = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                      "stream=width,height,nb_frames,duration", "-of", "json", str(path)]))["streams"][0]
    if (meta["width"], meta["height"], int(meta["nb_frames"]), float(meta["duration"])) != (WIDTH, HEIGHT, 4, 28.0):
        raise ValueError(f"Unexpected GIF stream: {meta}")
    pixels = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-fps_mode", "passthrough",
                                      "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
    if len(pixels) != 4 * WIDTH * HEIGHT * 3:
        raise ValueError("Incomplete GIF pixel stream")
    for page, points in enumerate(markers):
        for x, y, confirmed in points:
            colors = []
            for py in range(y-2, y+3):
                for px in range(x-2, x+3):
                    offset = 3 * (page * WIDTH * HEIGHT + py * WIDTH + px)
                    colors.append(pixels[offset:offset+3])
            green = any(g-r > 40 and g > 170 for r, g, b in colors)
            amber = any(r-g > 30 and r > 180 for r, g, b in colors)
            if green == amber or green != confirmed:
                raise ValueError(f"Encoded GIF marker mismatch on frame {page+1} at {(x, y)}")


def render(long_rows, output):
    historical_cases, short_rows = historical()
    data = [(list(LONG_LABELS), indexed(long_rows, LONG_LABELS), True),
            (historical_cases, indexed(short_rows, historical_cases), False)]
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        markers = []
        for cases, cells, long in data:
            medians = [
                median(int(cells[engine, route, case, rep]["wall_ms"]) for case in cases for rep in (1, 2, 3))
                for engine in ENGINES for route in (("responses",) if engine == "terra" else ROUTES)
            ]
            cap = max(10, 10 * math.ceil(max(medians) / 10_000))
            for route in ROUTES:
                svg, points = frame(cases, cells, long, route, len(markers)+1, cap)
                target = work / f"frame-{len(markers)}.png"
                subprocess.run(["rsvg-convert", "-o", str(target)], input=svg.encode(), check=True)
                markers.append(points)
        gif = work / "comparison.gif"
        filt = "split[a][b];[a]palettegen=max_colors=224[p];[b][p]paletteuse=dither=none:diff_mode=rectangle"
        subprocess.run(["ffmpeg", "-v", "error", "-framerate", "1/7", "-i", str(work / "frame-%d.png"),
                        "-vf", filt, "-loop", "0", "-final_delay", "700", str(gif)], check=True)
        verify_frames(gif, markers)
        output.write_bytes(gif.read_bytes())
