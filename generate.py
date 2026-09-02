#!/usr/bin/env python3
"""Generate beautiful SVG status badges from a Better Stack status page.

Stdlib only. Fetches `<status_url>/index.json` and renders:
  - a light SVG card
  - a dark SVG card
  - a single adaptive SVG that switches via `prefers-color-scheme`
"""

import argparse
import base64
import html
import json
import re
import struct
import urllib.request
import zlib
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# State palette
# --------------------------------------------------------------------------

STATES = {
    "operational": {
        "label": "All Systems Operational",
        "base": "#22C55E",
        "dot_a": "#4ADE80",
        "dot_b": "#16A34A",
        "light_text": "#15803D",
        "dark_text": "#4ADE80",
        "icon": "check",
    },
    "degraded": {
        "label": "Degraded Performance",
        "base": "#F59E0B",
        "dot_a": "#FCD34D",
        "dot_b": "#D97706",
        "light_text": "#B45309",
        "dark_text": "#FBBF24",
        "icon": "alert",
    },
    "downtime": {
        "label": "Service Disruption",
        "base": "#EF4444",
        "dot_a": "#F87171",
        "dot_b": "#DC2626",
        "light_text": "#B91C1C",
        "dark_text": "#F87171",
        "icon": "cross",
    },
    "maintenance": {
        "label": "Under Maintenance",
        "base": "#3B82F6",
        "dot_a": "#60A5FA",
        "dot_b": "#2563EB",
        "light_text": "#1D4ED8",
        "dark_text": "#60A5FA",
        "icon": "clock",
    },
    "unknown": {
        "label": "Status Unavailable",
        "base": "#9CA3AF",
        "dot_a": "#D1D5DB",
        "dot_b": "#6B7280",
        "light_text": "#6B7280",
        "dark_text": "#9CA3AF",
        "icon": "alert",
    },
}

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

HEIGHT = 68
ACCENT_W = 4.5
DOT_CX = 28
TEXT_X = 46
PAD_R = 20
ROW1_BASELINE = 29
ROW2_BASELINE = 52


# --------------------------------------------------------------------------
# Data fetching
# --------------------------------------------------------------------------

def fetch_status(base_url: str, timeout: float):
    url = base_url.rstrip("/") + "/index.json"

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "status-badge-action/2.0",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)

    attrs = payload["data"]["attributes"]

    uptime = None
    online = None
    total = 0
    resources = []

    included = payload.get("included", [])
    by_id = {item["id"]: item for item in included
             if item.get("type") == "status_page_resource"}

    # Canonical order: the relationships array mirrors the page layout
    # (sections in order, position within each section).
    rel = payload["data"].get("relationships", {}).get("resources", {})
    ordered_ids = [r["id"] for r in rel.get("data", [])
                   if r["id"] in by_id] or list(by_id)

    section_ids = [s["id"] for s in payload["data"].get("relationships", {})
                   .get("sections", {}).get("data", [])]
    section_rank = {}
    section_names = {}
    sec_items = [i for i in included
                 if i.get("type") == "status_page_section"]

    def _sec_key(item):
        pos = item.get("attributes", {}).get("position")
        if not isinstance(pos, (int, float)):
            pos = 999
        rel_index = section_ids.index(item["id"]) if item["id"] in section_ids \
            else 999
        return (pos, rel_index)

    for rank, item in enumerate(sorted(sec_items, key=_sec_key)):
        section_rank[item["id"]] = rank
        attrs_sec = item.get("attributes", {})
        section_names[item["id"]] = str(attrs_sec.get("name")
                                        or attrs_sec.get("public_name") or "")

    for index, rid in enumerate(ordered_ids):
        res = by_id[rid].get("attributes", {})
        if res.get("status") == "not_monitored":
            continue
        total += 1
        if res.get("status") == "operational":
            online = (online or 0) + 1
        availability = res.get("availability")
        if isinstance(availability, (int, float)):
            uptime = (uptime or 0.0) + availability

        sec_id = str(res.get("status_page_section_id") or "")
        resources.append({
            "name": str(res.get("public_name") or "Service"),
            "status": str(res.get("status") or "unknown"),
            "availability": availability if isinstance(availability, (int, float)) else None,
            "history": list(res.get("status_history") or []),
            "position": res.get("position") or 0,
            "section": section_names.get(sec_id, ""),
            "order": section_rank.get(sec_id, len(section_rank)),
            "index": index,
        })

    resources.sort(key=lambda r: (r["order"], r["position"], r["index"]))

    if uptime is not None and total:
        uptime = uptime / total * 100.0

    announcement = _clean_text(attrs.get("announcement"))
    reports = []
    for item in included:
        if item.get("type") != "status_report":
            continue
        ra = item.get("attributes", {})
        title = _clean_text(ra.get("title"))
        if not title:
            continue
        reports.append({
            "source": "report",
            "text": title,
            "state": str(ra.get("aggregate_state") or "").lower(),
            "starts_at": ra.get("starts_at"),
            "ends_at": ra.get("ends_at"),
        })
    reports.sort(key=lambda r: _iso_dt(r["starts_at"])
                 or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    if announcement:
        event = {"source": "announcement", "text": announcement,
                 "state": "", "starts_at": None, "ends_at": None}
    elif reports:
        event = reports[0]
    else:
        event = None

    return {
        "state": str(attrs.get("aggregate_state", "unknown")).lower(),
        "updated_at": attrs.get("updated_at"),
        "uptime": uptime,
        "online": online,
        "total": total,
        "resources": resources,
        "logo_url": attrs.get("logo_url"),
        "event": event,
    }


# --------------------------------------------------------------------------
# Logo embedding (pure stdlib: PNG decode + downscale + re-encode)
# --------------------------------------------------------------------------

LOGO_BOX = 22          # rendered diameter in the card
LOGO_MAX_SIDE = 64     # downscaled size before embedding
LOGO_RAW_LIMIT = 30_000  # embed non-PNG logos raw only if smaller than this

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _decode_png_rgba(data):
    """Decode an 8-bit non-interlaced PNG into (w, h, bytearray RGBA)."""
    if len(data) < 33 or data[:8] != _PNG_SIG:
        return None
    pos = 8
    ihdr = plte = trns = None
    idat = bytearray()
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", chunk)
        elif ctype == b"PLTE":
            plte = chunk
        elif ctype == b"tRNS":
            trns = chunk
        elif ctype == b"IDAT":
            idat.extend(chunk)
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if not ihdr or not idat:
        return None
    w, h, depth, color, _, _, interlace = ihdr
    if depth != 8 or interlace != 0:
        return None

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if channels is None:
        return None

    raw = zlib.decompress(bytes(idat))
    stride = w * channels
    out = bytearray(w * h * channels)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        if pos >= len(raw):
            return None
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if len(line) < stride:
            return None
        for x in range(stride):
            a = line[x - channels] if x >= channels else 0
            b = prev[x]
            c = prev[x - channels] if x >= channels else 0
            if ftype == 1:
                line[x] = (line[x] + a) & 0xFF
            elif ftype == 2:
                line[x] = (line[x] + b) & 0xFF
            elif ftype == 3:
                line[x] = (line[x] + (a + b) // 2) & 0xFF
            elif ftype == 4:
                line[x] = (line[x] + _paeth(a, b, c)) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line

    rgba = bytearray(w * h * 4)
    if color == 6:
        rgba[:] = out
    elif color == 2:
        for i in range(w * h):
            rgba[i * 4:i * 4 + 3] = out[i * 3:i * 3 + 3]
            rgba[i * 4 + 3] = 255
    elif color == 4:
        for i in range(w * h):
            rgba[i * 4] = rgba[i * 4 + 1] = rgba[i * 4 + 2] = out[i * 2]
            rgba[i * 4 + 3] = out[i * 2 + 1]
    elif color == 0:
        for i in range(w * h):
            rgba[i * 4] = rgba[i * 4 + 1] = rgba[i * 4 + 2] = out[i]
            rgba[i * 4 + 3] = 255
    elif color == 3 and plte:
        alphas = bytes(trns) if trns else b""
        for i in range(w * h):
            idx = out[i]
            rgba[i * 4:i * 4 + 3] = plte[idx * 3:idx * 3 + 3]
            rgba[i * 4 + 3] = alphas[idx] if idx < len(alphas) else 255
    else:
        return None
    return w, h, rgba


def _encode_png_rgba(w, h, rgba):
    def chunk(ctype, payload):
        c = struct.pack(">I", len(payload)) + ctype + payload
        return c + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)

    stride = w * 4
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw.extend(rgba[y * stride:(y + 1) * stride])
    return (b"".join((
        _PNG_SIG,
        chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)),
        chunk(b"IDAT", zlib.compress(bytes(raw), 9)),
        chunk(b"IEND", b""),
    )))


def _downscale_rgba(w, h, rgba, box):
    step = max(1, round(max(w, h) / box))
    ow, oh = max(w // step, 1), max(h // step, 1)
    out = bytearray(ow * oh * 4)
    for y in range(oh):
        sy = min(y * step + step // 2, h - 1)
        for x in range(ow):
            sx = min(x * step + step // 2, w - 1)
            si = (sy * w + sx) * 4
            di = (y * ow + x) * 4
            out[di:di + 4] = rgba[si:si + 4]
    return ow, oh, out


def fetch_logo(logo_url, timeout):
    """Download and inline the status page logo as a small PNG data URI."""
    if not logo_url:
        return None
    try:
        request = urllib.request.Request(
            logo_url, headers={"User-Agent": "status-badge-action/2.0"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            ctype = (response.headers.get("Content-Type") or "").split(";")[0]

        decoded = _decode_png_rgba(data)
        if decoded:
            w, h, rgba = decoded
            if max(w, h) > LOGO_MAX_SIDE:
                w, h, rgba = _downscale_rgba(w, h, rgba, LOGO_MAX_SIDE)
            payload = _encode_png_rgba(w, h, rgba)
            mime = "image/png"
        elif len(data) <= LOGO_RAW_LIMIT:
            mime = ctype if ctype.startswith("image/") else "image/png"
            payload = data
        else:
            print(f"logo skipped: unsupported or too large ({len(data)} bytes)")
            return None

        if len(payload) > LOGO_RAW_LIMIT:
            print(f"logo skipped: still too large after downscale")
            return None

        return ("data:%s;base64,%s" % (mime, base64.b64encode(payload)
                                       .decode("ascii")))
    except Exception as error:
        print(f"logo fetch failed: {error}")
        return None


def format_updated(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        dt = dt.astimezone(timezone.utc)
        return f"Updated {MONTHS[dt.month - 1]} {dt.day:02d} · {dt:%H:%M} UTC"
    except Exception:
        return None


def _clean_text(value):
    """Strip HTML tags / entities / extra whitespace from a text field."""
    if isinstance(value, dict):
        for key in ("message", "text", "title", "body"):
            value = value.get(key)
            if isinstance(value, str) and value.strip():
                break
        else:
            return None
    if not isinstance(value, str):
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip() or None


def _iso_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _fmt_day_short(value):
    dt = _iso_dt(value)
    if not dt:
        return None
    dt = dt.astimezone(timezone.utc)
    return f"{MONTHS[dt.month - 1]} {dt.day:02d}"


REPORT_STATE_MAP = {
    "operational": "operational",
    "resolved": "operational",
    "downtime": "downtime",
    "identified": "downtime",
    "investigating": "degraded",
    "degraded": "degraded",
    "monitoring": "degraded",
    "maintenance": "maintenance",
    "scheduled": "maintenance",
    "in_progress": "maintenance",
}


def build_event(data):
    """Pick what to show on the card's event line: announcement or latest report."""
    ev = data.get("event")
    if not ev:
        return None

    if ev["source"] == "announcement":
        cfg = STATES["maintenance"]          # informational -> blue info styling
        date_label = None
    else:
        cfg = STATES.get(REPORT_STATE_MAP.get(ev["state"], "unknown"),
                         STATES["unknown"])
        date_label = _fmt_day_short(ev.get("starts_at"))
        if date_label and not ev.get("ends_at"):
            date_label = f"since {date_label}"

    return {
        "text": ev["text"],
        "icon": cfg["icon"],
        "base": cfg["base"],
        "date_label": date_label,
        "hover": ev["text"],
    }


# --------------------------------------------------------------------------
# History chart (Better Stack style daily bars)
# --------------------------------------------------------------------------

BAR_GREEN = "#22C55E"
BAR_MAINT = "#A9E5C6"
BAR_RED = "#EF4444"

DAY_COLORS = {
    "operational": BAR_GREEN,
    "degraded": "#F59E0B",
    "downtime": BAR_RED,
    "maintenance": BAR_MAINT,
    "upcoming_maintenance": BAR_MAINT,
}

LEGEND = [
    ("operational", "Operational", BAR_GREEN),
    ("maintenance", "Maintenance", BAR_MAINT),
    ("downtime", "Downtime", BAR_RED),
    ("nodata", "No data", None),
]

SHORT_LABEL = {
    "operational": "Operational",
    "degraded": "Degraded",
    "downtime": "Downtime",
    "maintenance": "Maintenance",
    "unknown": "Unknown",
}

H_PAD = 24
BAR_H = 28
BAR_GAP = 3
ROW_H = 82
HEADER_H = 72
GROUP_H = 30
DAY_SECONDS = 86400


def fmt_duration(seconds):
    seconds = int(seconds)
    if seconds >= 86400:
        days, rem = divmod(seconds, 86400)
        hours = rem // 3600
        return f"{days}d{f' {hours}h' if hours else ''}"
    if seconds >= 3600:
        minutes = (seconds % 3600) // 60
        return f"{seconds // 3600}h{f' {minutes}m' if minutes else ''}"
    return f"{max(seconds // 60, 1)}m" if seconds else "0m"


def fmt_uptime(value):
    text = f"{value * 100:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def badge_icon(kind: str, base: str, cx: float, cy: float, r: float = 5.5):
    """Filled state-colored circle with a white glyph."""
    s = r / 5.5
    if kind == "check":
        glyph = (f'<path d="M{-2.7 * s:.2f} {0.2 * s:.2f} '
                 f'L{-0.8 * s:.2f} {2.1 * s:.2f} '
                 f'L{2.8 * s:.2f} {-2.0 * s:.2f}" fill="none" stroke="#fff" '
                 f'stroke-width="{1.7 * s:.2f}" stroke-linecap="round" '
                 f'stroke-linejoin="round"/>')
    elif kind == "cross":
        glyph = (f'<path d="M{-2.1 * s:.2f} {-2.1 * s:.2f} '
                 f'L{2.1 * s:.2f} {2.1 * s:.2f} '
                 f'M{2.1 * s:.2f} {-2.1 * s:.2f} L{-2.1 * s:.2f} {2.1 * s:.2f}" '
                 f'fill="none" stroke="#fff" stroke-width="{1.7 * s:.2f}" '
                 f'stroke-linecap="round"/>')
    elif kind == "clock":
        glyph = (f'<circle cx="0" cy="0" r="{2.5 * s:.2f}" fill="none" '
                 f'stroke="#fff" stroke-width="{1.4 * s:.2f}"/>'
                 f'<path d="M0 {-1.3 * s:.2f} L0 {0.3 * s:.2f} '
                 f'L{1.3 * s:.2f} {1.1 * s:.2f}" fill="none" stroke="#fff" '
                 f'stroke-width="{1.3 * s:.2f}" stroke-linecap="round" '
                 f'stroke-linejoin="round"/>')
    else:  # alert
        glyph = (f'<path d="M0 {-3 * s:.2f} L0 {0.7 * s:.2f}" fill="none" '
                 f'stroke="#fff" stroke-width="{1.7 * s:.2f}" '
                 f'stroke-linecap="round"/>'
                 f'<circle cx="0" cy="{2.9 * s:.2f}" r="{0.95 * s:.2f}" '
                 f'fill="#fff"/>')

    return (f'<g transform="translate({cx:.1f} {cy:.1f})">'
            f'<circle cx="0" cy="0" r="{r}" fill="{base}"/>{glyph}</g>')


def _fmt_day(day_str):
    try:
        dt = datetime.strptime(day_str, "%Y-%m-%d")
        return f"{MONTHS[dt.month - 1]} {dt.day:02d}"
    except Exception:
        return None


def ellipsize(text, size, weight, max_w):
    if text_width(text, size, weight) <= max_w:
        return text
    out = text
    while out and text_width(out + "…", size, weight) > max_w:
        out = out[:-1]
    return out + "…"


def pick_history_resources(resources, limit=2):
    """Choose which rows the history card shows (default: the first two).

    If any service is not operational, one failing service is promoted
    into the second slot so the card always surfaces an outage.
    """
    if len(resources) <= limit:
        return list(resources)

    first = resources[0]
    second = resources[1]
    if first["status"] == "operational":
        for r in resources[1:]:
            if r["status"] != "operational":
                second = r
                break
    return [first, second]


def build_history_svg(*, title, resources, days=90, updated=None,
                      state="unknown", width=800,
                      adaptive=False, dark=False, uid="bsbh", logo=None,
                      link=None):
    if adaptive:
        bg, border, primary, secondary = ("var(--bg)", "var(--border)",
                                          "var(--primary)", "var(--secondary)")
        nodata = "var(--nodata)"
    elif dark:
        bg, border, primary, secondary = ("#0D1117", "#30363D",
                                          "#F0F6FC", "#8B949E")
        nodata = "#262D36"
    else:
        bg, border, primary, secondary = ("#FFFFFF", "#E4E7EC",
                                          "#101828", "#667085")
        nodata = "#E5E7EB"

    cfg = STATES["operational"]

    content_w = width - H_PAD * 2
    bar_area = content_w
    slot = bar_area / days
    gap = BAR_GAP if slot >= BAR_GAP * 2.8 else round(min(BAR_GAP, slot * 0.35), 2)
    bar_w = (bar_area - (days - 1) * gap) / days
    bar_rx = min(bar_w / 2, 3)

    rows = []
    for r in resources:
        hist = [h for h in r["history"] if h.get("day")][-days:]
        pad = days - len(hist)
        series = ([None] * pad if pad > 0 else []) + hist
        status = r["status"] if r["status"] in STATES else "unknown"
        up_text = (f"{fmt_uptime(r['availability'])}% uptime"
                   if r["availability"] is not None else None)
        rows.append({
            "name": r["name"],
            "status": status,
            "up_text": up_text,
            "series": series,
            "section": str(r.get("section") or ""),
        })

    if not rows:
        rows = [{"name": "No monitored services",
                 "status": "unknown", "up_text": None,
                 "series": [None] * days, "section": ""}]

    if logo:
        mark = f"""
  <g>
    <clipPath id="{uid}-logoclip">
      <circle cx="{DOT_CX}" cy="24" r="11"/>
    </clipPath>
    <image x="17" y="13" width="22" height="22" href="{logo}"
      xlink:href="{logo}" clip-path="url(#{uid}-logoclip)"
      preserveAspectRatio="xMidYMid meet"/>
    <circle cx="{DOT_CX}" cy="24" r="11" fill="none" stroke="{border}"/>
  </g>"""
    else:
        mark = f"""
  <g>
    <circle cx="{DOT_CX}" cy="24" r="5.5" fill="url(#{uid}-dot)"/>
  </g>"""

    # ---- header: title + subtitle + aggregate state pill
    range_label = f"Last {days} days"
    if updated:
        range_label = f"{range_label} · {updated}"
    range_label = html.escape(range_label)
    pill_cfg = STATES.get(state, STATES["unknown"])
    pill_text = SHORT_LABEL.get(state, "Unknown")
    pill_w = 10 + 11 + 5 + text_width(pill_text, 11, 600) + 12
    pill_x = width - PAD_R - pill_w
    name_max = content_w - pill_w - 24
    title_show = html.escape(ellipsize(title, 14.5, 700, name_max))
    title_aria = html.escape(title)
    pill = f"""
  <g>
    <rect x="{pill_x:.1f}" y="12" width="{pill_w:.1f}" height="22" rx="11"
      fill="{pill_cfg["base"]}" opacity="0.12"/>
    <rect x="{pill_x:.1f}" y="12" width="{pill_w:.1f}" height="22" rx="11"
      fill="none" stroke="{pill_cfg["base"]}" stroke-opacity="0.25"/>
  </g>
  {badge_icon(pill_cfg["icon"], pill_cfg["base"], pill_x + 15.5, 23)}
  <text x="{pill_x + 26:.1f}" y="27" fill="{pill_cfg["base"]}"
    font-family="{FONT}" font-size="11" font-weight="600">{pill_text}</text>"""

    # ---- body
    body_parts = []

    def render_row(row, xb, y):
        """Render one resource row; xb = left edge of the bar area."""
        row_cfg = STATES.get(row["status"], STATES["unknown"])
        up_fill = ("var(--stext)" if adaptive else
                   (row_cfg["dark_text"] if dark else row_cfg["light_text"])
                   if row["status"] == "operational" else primary)

        up_text = html.escape(row["up_text"]) if row["up_text"] else ""
        up_w = (text_width(row["up_text"], 10.5, 600) + 20
                if row["up_text"] else 0)

        name = html.escape(ellipsize(row["name"], 12.5, 600,
                                     bar_area - 18 - up_w))

        bars = []
        for i, entry in enumerate(row["series"]):
            bx = xb + i * (bar_w + gap)
            by = y + 21

            if not entry:
                bars.append(f'<rect x="{bx:.2f}" y="{by}" '
                            f'width="{bar_w:.2f}" height="{BAR_H}" '
                            f'rx="{bar_rx:.2f}" fill="{nodata}">'
                            f'<title>No data</title></rect>')
                continue

            st = str(entry.get("status") or "unknown")
            down = min(max(float(entry.get("downtime_duration") or 0), 0.0),
                       DAY_SECONDS) / DAY_SECONDS
            maint = min(max(float(entry.get("maintenance_duration") or 0),
                            0.0), DAY_SECONDS) / DAY_SECONDS
            if down + maint > 1:
                total = down + maint
                down, maint = down / total, maint / total
            up = 1 - down - maint

            tip_day = _fmt_day(entry.get("day")) or entry.get("day") or ""
            tips = [SHORT_LABEL.get(st, st)]
            if down > 0:
                tips.append(f"down {fmt_duration(down * DAY_SECONDS)}")
            if maint > 0:
                tips.append(f"maint {fmt_duration(maint * DAY_SECONDS)}")
            tip = html.escape(f"{tip_day} · {' · '.join(tips)}")

            if down <= 0 and maint <= 0:
                color = DAY_COLORS.get(st, nodata)
                bars.append(f'<rect x="{bx:.2f}" y="{by}" '
                            f'width="{bar_w:.2f}" height="{BAR_H}" '
                            f'rx="{bar_rx:.2f}" fill="{color}">'
                            f'<title>{tip}</title></rect>')
                continue

            segments = []
            cy = by
            for fraction, color in ((up, BAR_GREEN),
                                    (maint, BAR_MAINT),
                                    (down, BAR_RED)):
                if fraction <= 0:
                    continue
                sh = fraction * BAR_H
                segments.append(f'<rect x="{bx:.2f}" y="{cy:.2f}" '
                                f'width="{bar_w:.2f}" height="{sh:.2f}" '
                                f'fill="{color}"/>')
                cy += sh
            bars.append(f'<g><title>{tip}</title>{"".join(segments)}</g>')

        body_parts.append(f"""
  {badge_icon(row_cfg["icon"], row_cfg["base"], xb + 5.5, y + 9)}
  <text x="{xb + 17:.1f}" y="{y + 13.5}" fill="{primary}" font-family="{FONT}"
    font-size="12.5" font-weight="600">{name}</text>
  <text x="{xb + bar_area:.1f}" y="{y + 13.5}" text-anchor="end"
    fill="{up_fill}" font-family="{FONT}" font-size="10.5"
    font-weight="600">{up_text}</text>
  {chr(10).join(bars)}
  <text x="{xb:.1f}" y="{y + 63}" fill="{secondary}" font-family="{FONT}"
    font-size="10.5">{days} days ago</text>
  <text x="{xb + bar_area:.1f}" y="{y + 63}" text-anchor="end"
    fill="{secondary}" font-family="{FONT}" font-size="10.5">Today</text>""")

    # ---- group subtitle above each change of section
    y = HEADER_H
    prev_section = None

    for row in rows:
        section = row["section"]
        if section != prev_section:
            if section:
                body_parts.append(f"""
  <text x="{H_PAD}" y="{y + 12}" fill="{secondary}" font-family="{FONT}"
    font-size="11" font-weight="600">{html.escape(section)}</text>""")
                y += GROUP_H
            prev_section = section
        render_row(row, H_PAD, y)
        y += ROW_H

    body = ["".join(body_parts)]
    content_bottom = y - ROW_H + 63

    # ---- legend
    legend_items = []
    lx = 0
    for key, label, color in LEGEND:
        color = nodata if key == "nodata" else color
        item_w = 8 + 6 + text_width(label, 10.5) + 26
        legend_items.append((lx, color, label, item_w))
        lx += item_w
    legend_total = lx - 26
    lgx = (width - legend_total) / 2

    legend = []
    for off, color, label, item_w in legend_items:
        cx = lgx + off + 4
        tx = lgx + off + 8 + 6
        legend.append(f'<circle cx="{cx:.1f}" cy="{content_bottom + 22:.0f}" '
                      f'r="4" fill="{color}"/>'
                      f'<text x="{tx:.1f}" y="{content_bottom + 26:.0f}" '
                      f'fill="{secondary}" font-family="{FONT}" '
                      f'font-size="10.5">{label}</text>')

    height = content_bottom + 42

    style = ""
    if adaptive:
        style = f"""  <style>
    svg {{{chr(10)}      --bg: #FFFFFF; --border: #E4E7EC; --primary: #101828;{chr(10)}      --secondary: #667085; --nodata: #E5E7EB; --stext: #15803D;{chr(10)}    }}{chr(10)}    @media (prefers-color-scheme: dark) {{{chr(10)}      svg {{{chr(10)}        --bg: #0D1117; --border: #30363D; --primary: #F0F6FC;{chr(10)}        --secondary: #8B949E; --nodata: #262D36; --stext: #4ADE80;{chr(10)}      }}{chr(10)}    }}{chr(10)}  </style>"""

    if link:
        href = html.escape(link, quote=True)
        anchor_open = (f'\n  <a href="{href}" xlink:href="{href}" '
                       f'target="_blank" rel="noopener">')
        anchor_close = "\n  </a>"
    else:
        anchor_open = anchor_close = ""

    return f"""\
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{width}" height="{height:.0f}"
  viewBox="0 0 {width} {height:.0f}" role="img" aria-label="{title_aria}: status history">
{style}{anchor_open}
  <defs>
    <radialGradient id="{uid}-dot" cx="35%" cy="30%" r="75%">
      <stop offset="0%" stop-color="{cfg["dot_a"]}"/>
      <stop offset="100%" stop-color="{cfg["dot_b"]}"/>
    </radialGradient>
    <linearGradient id="{uid}-accent" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{cfg["base"]}" stop-opacity="0.95"/>
      <stop offset="100%" stop-color="{cfg["base"]}" stop-opacity="0.25"/>
    </linearGradient>
    <clipPath id="{uid}-clip">
      <rect x="0.5" y="0.5" width="{width}" height="{height:.0f}" rx="13"/>
    </clipPath>
  </defs>

  <rect x="0.5" y="0.5" width="{width}" height="{height:.0f}" rx="13"
    fill="{bg}" stroke="{border}"/>

  <g clip-path="url(#{uid}-clip)">
    <rect x="0" y="0" width="{ACCENT_W}" height="{height:.0f}"
      fill="url(#{uid}-accent)"/>
  </g>
{mark}
  <text x="{TEXT_X}" y="{ROW1_BASELINE}" fill="{primary}"
    font-family="{FONT}" font-size="14.5" font-weight="700">{title_show}</text>
  <text x="{TEXT_X}" y="49" fill="{secondary}" font-family="{FONT}"
    font-size="10.5">{range_label}</text>

  {pill}

  <line x1="{H_PAD}" y1="{HEADER_H - 14}" x2="{width - H_PAD}" y2="{HEADER_H - 14}"
    stroke="{border}" stroke-width="1"/>
{chr(10).join(body)}

  {chr(10).join(legend)}{anchor_close}
</svg>
"""


# --------------------------------------------------------------------------
# Text metrics (approximate, good enough for layout)
# --------------------------------------------------------------------------

NARROW = set("iljtfr.,'|!:;'`i")
WIDE = set("mwMW@")


def text_width(text: str, size: float, weight: int = 400) -> float:
    units = 0.0
    for ch in text:
        code = ord(ch)
        if code >= 0x2E80:            # CJK & fullwidth
            units += 1.0
        elif ch in WIDE:
            units += 0.88
        elif ch in NARROW:
            units += 0.31
        elif ch.isupper() or ch.isdigit():
            units += 0.63
        elif ch.islower():
            units += 0.53
        elif ch == " ":
            units += 0.30
        else:
            units += 0.58
    factor = 1.045 if weight >= 600 else 1.0
    return units * size * factor


# --------------------------------------------------------------------------
# Icons (12x12, drawn around origin, stroke = state color)
# --------------------------------------------------------------------------

def icon_svg(kind: str, color: str, adaptive: bool):
    stroke = "var(--stext)" if adaptive else color

    if kind == "check":
        shape = (f'<path d="M-3.6 0.2 L-1 2.8 L3.8 -2.6" fill="none" '
                 f'stroke="{stroke}" stroke-width="2.1" '
                 f'stroke-linecap="round" stroke-linejoin="round"/>')
    elif kind == "cross":
        shape = (f'<path d="M-3 -3 L3 3 M3 -3 L-3 3" fill="none" '
                 f'stroke="{stroke}" stroke-width="2.1" stroke-linecap="round"/>')
    elif kind == "clock":
        shape = (f'<circle cx="0" cy="0" r="3.6" fill="none" stroke="{stroke}" '
                 f'stroke-width="1.8"/>'
                 f'<path d="M0 -1.8 L0 0.4 L1.8 1.4" fill="none" '
                 f'stroke="{stroke}" stroke-width="1.6" '
                 f'stroke-linecap="round" stroke-linejoin="round"/>')
    else:  # alert
        shape = (f'<path d="M0 -4 L0 1.1" fill="none" stroke="{stroke}" '
                 f'stroke-width="2.1" stroke-linecap="round"/>'
                 f'<circle cx="0" cy="3.9" r="1.25" fill="{stroke}"/>')

    return shape


# --------------------------------------------------------------------------
# Renderer
# --------------------------------------------------------------------------

def build_svg(*, title, state, host, updated, uptime, online, total,
              adaptive=False, dark=False, animate=True, min_width=440,
              uid="bsb", logo=None, event=None, link=None):
    cfg = STATES.get(state, STATES["unknown"])

    if adaptive:
        bg = "var(--bg)"
        border = "var(--border)"
        primary = "var(--primary)"
        secondary = "var(--secondary)"
        status_text = "var(--stext)"
    elif dark:
        bg = "#0D1117"
        border = "#30363D"
        primary = "#F0F6FC"
        secondary = "#8B949E"
        status_text = cfg["dark_text"]
    else:
        bg = "#FFFFFF"
        border = "#E4E7EC"
        primary = "#101828"
        secondary = "#667085"
        status_text = cfg["light_text"]

    title = html.escape(title)
    label = html.escape(cfg["label"])
    host = html.escape(host)

    # ---- row 1: title (left) + status label with icon (right)
    label_w = text_width(cfg["label"], 12.5, 700)
    icon_gap = 17.0
    status_right = None  # x of label right edge, filled after width known

    # ---- row 2: host · updated (left), uptime pill (right)
    meta_left = host
    if updated:
        meta_left = f"{host}  ·  {updated}"
    meta_left = html.escape(meta_left)
    meta_w = text_width(meta_left, 10.5)

    pill_text = None
    if uptime is not None and total:
        pill_text = f"{uptime:.2f}% uptime"
        if online is not None:
            pill_text = f"{online}/{total} online · {uptime:.2f}% uptime"
    pill_w = None
    if pill_text:
        pill_text = html.escape(pill_text)
        pill_w = text_width(pill_text, 10.5, 600) + 20

    # ---- width
    row1_w = TEXT_X + text_width(title, 14.5, 700) + 28 + icon_gap + label_w
    row2_w = TEXT_X + meta_w
    if pill_w:
        row2_w += 18 + pill_w
    width = max(min_width, row1_w + PAD_R, row2_w + PAD_R)
    status_right = width - PAD_R
    icon_x = status_right - label_w - icon_gap

    # ---- event line (announcement / latest incident report)
    height = HEIGHT
    event_row = ""
    if event and event.get("text"):
        height += 26
        ev_cy = HEIGHT + 15          # vertical center of the event strip
        date_label = html.escape(event["date_label"]) \
            if event.get("date_label") else None
        date_w = text_width(event["date_label"], 10.5) if date_label else 0
        ev_max = width - TEXT_X - PAD_R - (date_w + 12 if date_label else 0)
        ev_show = html.escape(ellipsize(event["text"], 11, 600, ev_max))
        ev_hover = html.escape(event["hover"])
        date_text = ""
        if date_label:
            date_text = (f'\n    <text x="{width - PAD_R:.1f}" '
                         f'y="{ev_cy + 4:.1f}" text-anchor="end" '
                         f'fill="{secondary}" font-family="{FONT}" '
                         f'font-size="10.5">{date_label}</text>')
        event_row = f"""
  <line x1="{TEXT_X}" y1="{HEIGHT - 4}" x2="{width:.0f}" y2="{HEIGHT - 4}"
    stroke="{border}" stroke-width="1"/>
  <g>
    <title>{ev_hover}</title>
    {badge_icon(event["icon"], event["base"], 34, ev_cy)}
    <text x="{TEXT_X}" y="{ev_cy + 4:.1f}" fill="{primary}" font-family="{FONT}"
      font-size="11" font-weight="600">{ev_show}</text>{date_text}
  </g>"""

    # ---- animation
    anim = ""
    if animate:
        anim = (f'<animate attributeName="r" values="8;17" dur="2.4s" '
                f'repeatCount="indefinite"/>'
                f'<animate attributeName="opacity" values="0.5;0" dur="2.4s" '
                f'repeatCount="indefinite"/>')

    # ---- adaptive style block
    style = ""
    if adaptive:
        style = f"""  <style>
    svg {{{chr(10)}      --bg: #FFFFFF; --border: #E4E7EC; --primary: #101828;{chr(10)}      --secondary: #667085; --stext: {cfg["light_text"]};{chr(10)}    }}{chr(10)}    @media (prefers-color-scheme: dark) {{{chr(10)}      svg {{{chr(10)}        --bg: #0D1117; --border: #30363D; --primary: #F0F6FC;{chr(10)}        --secondary: #8B949E; --stext: {cfg["dark_text"]};{chr(10)}      }}{chr(10)}    }}{chr(10)}  </style>"""

    if link:
        href = html.escape(link, quote=True)
        anchor_open = (f'\n  <a href="{href}" xlink:href="{href}" '
                       f'target="_blank" rel="noopener">')
        anchor_close = "\n  </a>"
    else:
        anchor_open = anchor_close = ""

    # ---- status mark: inline logo when available, pulsing dot otherwise
    if logo:
        mark = f"""
  <g>
    <clipPath id="{uid}-logoclip">
      <circle cx="{DOT_CX}" cy="24" r="11"/>
    </clipPath>
    <image x="17" y="13" width="22" height="22" href="{logo}"
      xlink:href="{logo}" clip-path="url(#{uid}-logoclip)"
      preserveAspectRatio="xMidYMid meet"/>
    <circle cx="{DOT_CX}" cy="24" r="11" fill="none" stroke="{border}"/>
  </g>"""
    else:
        mark = f"""
  <g>
    <circle cx="{DOT_CX}" cy="24" r="8" fill="{cfg["base"]}" opacity="0.16"/>
    <circle cx="{DOT_CX}" cy="24" r="8" fill="none"
      stroke="{cfg["base"]}" stroke-width="2" opacity="0.5">
      {anim}
    </circle>
    <circle cx="{DOT_CX}" cy="24" r="5.5" fill="url(#{uid}-dot)"/>
  </g>"""

    # ---- uptime pill
    pill = ""
    if pill_w:
        px = width - PAD_R - pill_w
        pill_bg = "var(--pillbg)" if adaptive else cfg["base"]
        pill_fg = status_text
        pill = f"""
  <g>
    <rect x="{px:.1f}" y="39" width="{pill_w:.1f}" height="19" rx="9.5"
      fill="{pill_bg}" opacity="0.12"/>
    <text x="{px + pill_w / 2:.1f}" y="51.8" text-anchor="middle"
      fill="{pill_fg}" font-family="{FONT}" font-size="10.5"
      font-weight="600">{pill_text}</text>
  </g>"""

    return f"""\
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{width:.0f}" height="{height}"
  viewBox="0 0 {width:.0f} {height}" role="img"
  aria-label="{title}: {label}">
{style}{anchor_open}
  <defs>
    <radialGradient id="{uid}-dot" cx="35%" cy="30%" r="75%">
      <stop offset="0%" stop-color="{cfg["dot_a"]}"/>
      <stop offset="100%" stop-color="{cfg["dot_b"]}"/>
    </radialGradient>
    <linearGradient id="{uid}-accent" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{cfg["base"]}" stop-opacity="0.95"/>
      <stop offset="100%" stop-color="{cfg["base"]}" stop-opacity="0.25"/>
    </linearGradient>
    <clipPath id="{uid}-clip">
      <rect x="0.5" y="0.5" width="{width:.0f}" height="{height - 1}" rx="13"/>
    </clipPath>
  </defs>

  <rect x="0.5" y="0.5" width="{width:.0f}" height="{height - 1}" rx="13"
    fill="{bg}" stroke="{border}"/>

  <g clip-path="url(#{uid}-clip)">
    <rect x="0" y="0" width="{ACCENT_W}" height="{height}"
      fill="url(#{uid}-accent)"/>
  </g>
{mark}
  <text x="{TEXT_X}" y="{ROW1_BASELINE}" fill="{primary}"
    font-family="{FONT}" font-size="14.5" font-weight="700">{title}</text>

  <g transform="translate({icon_x:.1f} 24.5)">{icon_svg(cfg["icon"], cfg["base"], adaptive)}</g>
  <text x="{status_right:.1f}" y="{ROW1_BASELINE}" text-anchor="end"
    fill="{status_text}" font-family="{FONT}" font-size="12.5"
    font-weight="700">{label}</text>

  <text x="{TEXT_X}" y="{ROW2_BASELINE}" fill="{secondary}"
    font-family="{FONT}" font-size="10.5">{meta_left}</text>
{pill}{event_row}{anchor_close}
</svg>
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate SVG status badges from a Better Stack status page"
    )
    parser.add_argument("--url", required=True)
    parser.add_argument("--title", default="System status")
    parser.add_argument("--output", default="status.svg")
    parser.add_argument("--output-dark", default="status-dark.svg")
    parser.add_argument("--output-adaptive", default=None,
                        help="single self-adapting SVG (light + dark)")
    parser.add_argument("--output-history", default=None,
                        help="90-day daily status history card")
    parser.add_argument("--output-history-dark", default=None)
    parser.add_argument("--output-history-adaptive", default=None)
    parser.add_argument("--days", type=int, default=90,
                        help="history window in days")
    parser.add_argument("--history-width", type=int, default=800,
                        help="history card width in px")
    parser.add_argument("--min-width", type=int, default=440)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--no-uptime", action="store_true")
    parser.add_argument("--no-updated", action="store_true")
    parser.add_argument("--no-logo", action="store_true",
                        help="keep the state dot instead of the site logo")
    parser.add_argument("--no-events", action="store_true",
                        help="hide the latest announcement / incident line")
    parser.add_argument("--link", default=None, metavar="URL",
                        help="URL the SVG links to when opened directly "
                             "(default: the status page URL)")
    parser.add_argument("--no-link", action="store_true",
                        help="do not embed a hyperlink in the SVG")
    parser.add_argument("--static", action="store_true",
                        help="disable pulse animation")
    args = parser.parse_args()

    host = args.url
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    host = host.rstrip("/")

    try:
        data = fetch_status(args.url, args.timeout)
    except Exception as error:
        print(f"status fetch failed: {error}")
        data = {"state": "unknown", "updated_at": None,
                "uptime": None, "online": None, "total": 0,
                "resources": [], "event": None}

    state = data["state"] if data["state"] in STATES else "unknown"
    updated = None if args.no_updated else format_updated(data["updated_at"])
    uptime = None if args.no_uptime else data["uptime"]
    logo = None if args.no_logo else fetch_logo(data.get("logo_url"),
                                                args.timeout)
    event = None if args.no_events else build_event(data)
    link = None if args.no_link else (args.link or args.url)

    common = dict(
        title=args.title,
        state=state,
        host=host,
        updated=updated,
        uptime=uptime,
        online=data["online"],
        total=data["total"],
        min_width=args.min_width,
        animate=not args.static,
        logo=logo,
        event=event,
        link=link,
    )

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(build_svg(**common, adaptive=False, dark=False, uid="bsbl"))

    with open(args.output_dark, "w", encoding="utf-8") as f:
        f.write(build_svg(**common, adaptive=False, dark=True, uid="bsbd"))

    if args.output_adaptive:
        with open(args.output_adaptive, "w", encoding="utf-8") as f:
            f.write(build_svg(**common, adaptive=True, uid="bsba"))

    history_common = dict(
        title=args.title,
        resources=pick_history_resources(data["resources"]),
        days=args.days,
        updated=format_updated(data["updated_at"]),
        state=state,
        width=args.history_width,
        logo=logo,
        link=link,
    )

    if args.output_history:
        with open(args.output_history, "w", encoding="utf-8") as f:
            f.write(build_history_svg(**history_common, uid="bsbhl"))
    if args.output_history_dark:
        with open(args.output_history_dark, "w", encoding="utf-8") as f:
            f.write(build_history_svg(**history_common, dark=True, uid="bsbhd"))
    if args.output_history_adaptive:
        with open(args.output_history_adaptive, "w", encoding="utf-8") as f:
            f.write(build_history_svg(**history_common, adaptive=True,
                                      uid="bsbha"))

    print(f"generated {args.output}"
          f"{f', {args.output_dark}' if args.output_dark else ''}"
          f"{f', {args.output_adaptive}' if args.output_adaptive else ''}"
          f"{f', {args.output_history}' if args.output_history else ''}"
          f"{f', {args.output_history_dark}' if args.output_history_dark else ''}"
          f"{f', {args.output_history_adaptive}' if args.output_history_adaptive else ''}: "
          f"{state}"
          + (f" ({data['online']}/{data['total']} online, "
             f"{uptime:.2f}% uptime)" if uptime is not None else ""))


if __name__ == "__main__":
    main()
