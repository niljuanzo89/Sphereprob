import streamlit as st
import csv
import math
import io
import os
import ssl
import json
import difflib
import random
import datetime
import urllib.request
import urllib.parse
from collections import Counter, defaultdict
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
# reportlab imported lazily inside build_bingo_pdf

FILEPATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phish_net_setlists_2016_2026.csv")
START_YEAR = 2008

TIER_TARGETS = {
    "Staple":     0.036,
    "Common":     0.390,
    "Occasional": 0.333,
    "Rare":       0.240,
}

def get_tier(pct):
    if pct >= 15: return "Staple"
    if pct >= 5:  return "Common"
    if pct >= 1:  return "Occasional"
    return "Rare"

def avg_position(pos_list):
    return sum(pos_list) / len(pos_list)

@st.cache_data
def load_data():
    global_counter = Counter()
    global_shows = 0
    city_data = {}
    song_last_show = {}
    song_positions = defaultdict(list)

    with open(FILEPATH, newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["date"][:4].isdigit() and int(r["date"][:4]) >= START_YEAR]

    show_index = 0
    for row in rows:
        setlist = row["setlist"].strip()
        if not setlist:
            continue
        songs = [s.strip() for s in setlist.split("|") if s.strip()]
        if not songs:
            continue

        global_shows += 1
        global_counter.update(songs)
        total = len(songs)
        loc = row["location"].strip()

        if loc not in city_data:
            city_data[loc] = {"counter": Counter(), "shows": 0, "lengths": [], "positions": defaultdict(list)}
        city_data[loc]["counter"].update(songs)
        city_data[loc]["shows"] += 1
        city_data[loc]["lengths"].append(total)

        for i, song in enumerate(songs):
            norm_pos = i / max(total - 1, 1)
            song_positions[song].append(norm_pos)
            city_data[loc]["positions"][song].append(norm_pos)
            song_last_show[song] = show_index

        show_index += 1

    total_shows = show_index
    current_gap = {song: total_shows - last - 1 for song, last in song_last_show.items()}
    return global_counter, global_shows, city_data, current_gap, total_shows, song_positions


def generate_setlist(city):
    global_counter, global_shows, city_data, current_gap, total_shows, song_positions = load_data()

    matches = {loc: v for loc, v in city_data.items() if city.lower() in loc.lower()}
    if not matches:
        return None, None, None

    city_counter = Counter()
    city_shows = 0
    all_lengths = []
    all_positions = defaultdict(list)
    matched_locations = []

    for loc, d in matches.items():
        city_counter.update(d["counter"])
        city_shows += d["shows"]
        all_lengths.extend(d["lengths"])
        matched_locations.append(loc)
        for song, positions in d["positions"].items():
            all_positions[song].extend(positions)

    avg_length = round(sum(all_lengths) / len(all_lengths))

    scores = {}
    for song, count in city_counter.items():
        freq = count / city_shows
        gap = current_gap.get(song, total_shows)
        gap_boost = 1 + math.log(gap + 1) / math.log(total_shows + 1)
        scores[song] = freq * gap_boost

    tier_buckets = defaultdict(list)
    for song in scores:
        gpct = (global_counter[song] / global_shows) * 100
        tier_buckets[get_tier(gpct)].append(song)
    for t in tier_buckets:
        tier_buckets[t].sort(key=lambda s: scores[s], reverse=True)

    slots = {}
    remaining = avg_length
    for t in ["Staple", "Common", "Occasional"]:
        n = min(round(avg_length * TIER_TARGETS[t]), len(tier_buckets[t]))
        slots[t] = n
        remaining -= n
    slots["Rare"] = max(0, min(remaining, len(tier_buckets["Rare"])))

    selected = []
    for t in ["Staple", "Common", "Occasional", "Rare"]:
        selected.extend(tier_buckets[t][:slots[t]])

    # Enforce max 1 opener (pos < 0.15) and 1 closer (pos > 0.85)
    def song_pos(s):
        return avg_position(all_positions.get(s, song_positions.get(s, [0.5])))

    openers = [s for s in selected if song_pos(s) < 0.15]
    closers  = [s for s in selected if song_pos(s) > 0.85]

    removed = set()
    for group in [openers, closers]:
        if len(group) > 1:
            group.sort(key=lambda s: scores[s], reverse=True)
            for s in group[1:]:
                selected.remove(s)
                removed.add(s)

    already = set(selected)
    for t in ["Staple", "Common", "Occasional", "Rare"]:
        for s in tier_buckets[t]:
            if len(selected) >= avg_length:
                break
            if s not in already and s not in removed and 0.15 <= song_pos(s) <= 0.85:
                selected.append(s)
                already.add(s)

    selected.sort(key=lambda s: song_pos(s))

    rows = []
    for i, song in enumerate(selected, 1):
        base_pct = (city_counter[song] / city_shows) * 100
        gpct = (global_counter[song] / global_shows) * 100
        gap = current_gap.get(song, total_shows)
        adj = scores[song] * 100
        pos = avg_position(all_positions.get(song, song_positions.get(song, [0.5])))
        pos_label = "Closer" if pos > 0.85 else ("Opener" if pos < 0.15 else f"{pos:.0%} thru")
        rows.append({
            "#": i,
            "Song": song,
            "Tier": get_tier(gpct),
            "City Freq": f"{base_pct:.1f}%",
            "Shows Since Last Played": gap,
            "Adj Score": f"{adj:.1f}%",
            "Show Position": pos_label,
            "_adj": adj,
            "_gap": gap,
            "_pos": pos,
        })

    return rows, city_shows, matched_locations


def generate_bingo(city):
    global_counter, global_shows, city_data, current_gap, total_shows, song_positions = load_data()
    rows, city_shows, locations = generate_setlist(city)
    if rows is None:
        return None

    def gap_score(song):
        gap = current_gap.get(song, total_shows)
        return (global_counter[song] / global_shows) * (1 + math.log(gap + 1) / math.log(total_shows + 1))

    setlist_picks = [r["Song"] for r in sorted(rows, key=lambda r: r["_adj"], reverse=True)[:10]]
    used = set(setlist_picks)

    common_pool = sorted(
        [(s, gap_score(s)) for s, c in global_counter.items()
         if 5 <= (c / global_shows) * 100 < 15 and s not in used],
        key=lambda x: x[1], reverse=True
    )
    common_picks = [s for s, _ in common_pool[:10]]
    used |= set(common_picks)

    occasional_pool = sorted(
        [(s, gap_score(s)) for s, c in global_counter.items()
         if 1 <= (c / global_shows) * 100 < 5 and s not in used],
        key=lambda x: x[1], reverse=True
    )
    rare_pool = sorted(
        [(s, gap_score(s)) for s, c in global_counter.items()
         if (c / global_shows) * 100 < 1 and s not in used],
        key=lambda x: x[1], reverse=True
    )
    rare_picks = [s for s, _ in occasional_pool[:3]] + [s for s, _ in rare_pool[:2]]

    all_songs = setlist_picks + common_picks + rare_picks
    random.shuffle(all_songs)

    setlist_set = set(setlist_picks)
    common_set  = set(common_picks)
    return [{"song": s,
             "cat": "setlist" if s in setlist_set else "common" if s in common_set else "rare"}
            for s in all_songs]


def generate_sphere_setlist(target_date, sphere_songs_played):
    """Predict a setlist for an upcoming Sphere show.

    Uses the same tier/gap/position logic as generate_setlist, but:
    - Based on Las Vegas city history (Sphere is in Las Vegas)
    - Excludes any song already played at the Sphere 2026 run so far
    - Adds a boost for songs the band played 10-15 days before target_date
      (captures "current rotation" songs)
    """
    global_counter, global_shows, city_data, current_gap, total_shows, song_positions = load_data()

    already_played = set(sphere_songs_played.keys())

    # Use Las Vegas data for the Sphere (fall back to global if sparse)
    matches = {loc: v for loc, v in city_data.items() if "las vegas" in loc.lower()}

    if matches:
        city_counter = Counter()
        city_shows_n = 0
        all_lengths = []
        all_positions = defaultdict(list)
        matched_locations = []
        for loc, d in matches.items():
            city_counter.update(d["counter"])
            city_shows_n += d["shows"]
            all_lengths.extend(d["lengths"])
            matched_locations.append(loc)
            for song, positions in d["positions"].items():
                all_positions[song].extend(positions)
        avg_length = round(sum(all_lengths) / len(all_lengths))
        source = f"Las Vegas ({city_shows_n} shows)"
    else:
        city_counter = global_counter
        city_shows_n = global_shows
        all_positions = song_positions
        avg_length = 22
        matched_locations = ["Global"]
        source = f"Global ({global_shows} shows)"

    # Find songs played in the 10-15 shows prior to target_date — "recent rotation" boost.
    # (Looks at the 10th through 15th most recent shows before target_date, chronologically.)
    all_shows = []
    with open(FILEPATH, newline="") as f:
        for row in csv.DictReader(f):
            if not row["date"][:4].isdigit():
                continue
            songs_here = [s.strip() for s in row["setlist"].split("|") if s.strip()]
            if songs_here:
                all_shows.append((row["date"], songs_here))
    all_shows.sort(key=lambda x: x[0])

    prior_shows = [s for s in all_shows if s[0] < target_date]
    # Window = shows indexed [-15 .. -10] from the end of prior_shows
    window_shows = prior_shows[-15:-9] if len(prior_shows) >= 10 else prior_shows[:max(0, len(prior_shows)-9)]

    recent_songs = set()
    recent_dates = []
    for date_str, songs_here in window_shows:
        recent_songs.update(songs_here)
        recent_dates.append(date_str)

    # Score songs (excluding already-played)
    scores = {}
    for song, count in city_counter.items():
        if song in already_played:
            continue
        freq = count / city_shows_n
        gap = current_gap.get(song, total_shows)
        gap_boost    = 1 + math.log(gap + 1) / math.log(total_shows + 1)
        recent_boost = 1.6 if song in recent_songs else 1.0
        scores[song] = freq * gap_boost * recent_boost

    # Tier buckets (based on global frequency, as in generate_setlist)
    tier_buckets = defaultdict(list)
    for song in scores:
        gpct = (global_counter[song] / global_shows) * 100
        tier_buckets[get_tier(gpct)].append(song)
    for t in tier_buckets:
        tier_buckets[t].sort(key=lambda s: scores[s], reverse=True)

    # Quota per tier
    slots = {}
    remaining = avg_length
    for t in ["Staple", "Common", "Occasional"]:
        n = min(round(avg_length * TIER_TARGETS[t]), len(tier_buckets[t]))
        slots[t] = n
        remaining -= n
    slots["Rare"] = max(0, min(remaining, len(tier_buckets["Rare"])))

    selected = []
    for t in ["Staple", "Common", "Occasional", "Rare"]:
        selected.extend(tier_buckets[t][:slots[t]])

    # Enforce 1 opener / 1 closer
    def song_pos(s):
        return avg_position(all_positions.get(s, song_positions.get(s, [0.5])))

    openers = [s for s in selected if song_pos(s) < 0.15]
    closers = [s for s in selected if song_pos(s) > 0.85]

    removed = set()
    for group in [openers, closers]:
        if len(group) > 1:
            group.sort(key=lambda s: scores[s], reverse=True)
            for s in group[1:]:
                selected.remove(s)
                removed.add(s)

    already_in_sel = set(selected)
    for t in ["Staple", "Common", "Occasional", "Rare"]:
        for s in tier_buckets[t]:
            if len(selected) >= avg_length:
                break
            if s not in already_in_sel and s not in removed and 0.15 <= song_pos(s) <= 0.85:
                selected.append(s)
                already_in_sel.add(s)

    selected.sort(key=lambda s: song_pos(s))

    rows = []
    for i, song in enumerate(selected, 1):
        base_pct = (city_counter[song] / city_shows_n) * 100
        gpct     = (global_counter[song] / global_shows) * 100
        gap      = current_gap.get(song, total_shows)
        adj      = scores[song] * 100
        pos      = song_pos(song)
        pos_label = "Closer" if pos > 0.85 else ("Opener" if pos < 0.15 else f"{pos:.0%} thru")
        rows.append({
            "#": i,
            "Song": song,
            "Tier": get_tier(gpct),
            "Vegas/Global Freq": f"{base_pct:.1f}%",
            "Global Freq": f"{gpct:.1f}%",
            "Shows Since Last Played": gap,
            "Adj Score": f"{adj:.1f}%",
            "Show Position": pos_label,
            "Recent": song in recent_songs,
            "_adj": adj,
            "_gap": gap,
            "_pos": pos,
        })

    return {
        "rows": rows,
        "source": source,
        "city_shows": city_shows_n,
        "excluded": sorted(already_played),
        "recent_songs": sorted(recent_songs),
        "recent_dates": sorted(set(recent_dates)),
        "window_count": len(window_shows),
    }


def build_bingo_pdf(cards, city):
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=0.6*inch, rightMargin=0.6*inch,
        topMargin=0.5*inch, bottomMargin=0.5*inch,
    )
    CAT_BG = {
        "setlist": colors.HexColor("#2a2a10"),
        "common":  colors.HexColor("#0d2235"),
        "rare":    colors.HexColor("#2a0d35"),
    }
    CAT_FG = {
        "setlist": colors.HexColor("#F0E68C"),
        "common":  colors.HexColor("#7ec8e3"),
        "rare":    colors.HexColor("#ce93d8"),
    }
    BORDER    = colors.HexColor("#444444")
    HEADER_BG = colors.HexColor("#1a1a2e")
    HEADER_FG = colors.HexColor("#F0E68C")

    story = []
    story.append(Paragraph("🎸 Gotta-Jibbootistics", ParagraphStyle(
        "t", fontSize=20, textColor=HEADER_FG, alignment=TA_CENTER,
        fontName="Helvetica-Bold", spaceAfter=2)))
    story.append(Paragraph("whatever you do, take care of your shoes", ParagraphStyle(
        "s", fontSize=9, textColor=colors.HexColor("#e85545"), alignment=TA_CENTER,
        fontName="Helvetica-Oblique", spaceAfter=4)))
    story.append(Paragraph(f"Bingo Card — {city.title()}", ParagraphStyle(
        "c", fontSize=11, textColor=colors.HexColor("#aaaacc"), alignment=TA_CENTER,
        fontName="Helvetica", spaceAfter=10)))

    col_w, row_h, hdr_h = 1.26*inch, 0.85*inch, 0.38*inch

    header_row = [Paragraph(ch, ParagraphStyle(
        "h", fontSize=18, textColor=HEADER_FG, alignment=TA_CENTER,
        fontName="Helvetica-Bold")) for ch in "BINGO"]

    table_data = [header_row]
    for row_i in range(5):
        row = []
        for col_i in range(5):
            card = cards[row_i * 5 + col_i]
            row.append(Paragraph(card["song"], ParagraphStyle(
                f"cell", fontSize=7.5, textColor=CAT_FG[card["cat"]],
                alignment=TA_CENTER, fontName="Helvetica-Bold", leading=10)))
        table_data.append(row)

    tbl = Table(table_data, colWidths=[col_w]*5, rowHeights=[hdr_h]+[row_h]*5)
    ts = [
        ("BACKGROUND", (0, 0), (4, 0), HEADER_BG),
        ("GRID",       (0, 0), (-1, -1), 1.5, BORDER),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
    ]
    for row_i in range(5):
        for col_i in range(5):
            ts.append(("BACKGROUND", (col_i, row_i+1), (col_i, row_i+1),
                        CAT_BG[cards[row_i*5+col_i]["cat"]]))
    tbl.setStyle(TableStyle(ts))
    story.append(tbl)
    story.append(Spacer(1, 0.1*inch))
    story.append(Paragraph(
        "🟡 From predicted setlist  |  🔵 Globally common  |  🟣 Rare / uncommon",
        ParagraphStyle("leg", fontSize=7.5, textColor=colors.HexColor("#888888"),
                       alignment=TA_CENTER, fontName="Helvetica")))
    doc.build(story)
    buf.seek(0)
    return buf


def build_xlsx(rows, city, city_shows):
    wb = Workbook()
    ws = wb.active
    ws.title = f"{city} Setlist"

    thin = Side(style="thin", color="CCCCCC")
    bdr = Border(left=thin, right=thin, top=thin, bottom=thin)

    ws.merge_cells("A1:G1")
    c = ws["A1"]
    c.value = f"PHISH  —  {city.upper()}"
    c.font = Font(name="Arial", bold=True, size=18, color="F0E68C")
    c.fill = PatternFill("solid", fgColor="1A1A2E")
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 40

    ws.merge_cells("A2:G2")
    c = ws["A2"]
    c.value = f"Predicted Setlist  |  Based on {city_shows} shows  |  Gap-adjusted · Position-ordered"
    c.font = Font(name="Arial", italic=True, size=10, color="AAAACC")
    c.fill = PatternFill("solid", fgColor="1A1A2E")
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 20
    ws.row_dimensions[3].height = 6

    headers = ["#", "Song", "Tier", "City Freq\n(Base %)", "Shows Since\nLast Played", "Adj Score", "Show Position\n(Avg % thru)"]
    col_widths = [5, 32, 12, 14, 16, 12, 16]
    for col, (hdr, width) in enumerate(zip(headers, col_widths), 1):
        c = ws.cell(row=4, column=col, value=hdr)
        c.font = Font(name="Arial", bold=True, size=10, color="F0E68C")
        c.fill = PatternFill("solid", fgColor="1A1A2E")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = bdr
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[4].height = 34

    for i, row in enumerate(rows):
        r = i + 5
        is_closer  = row["_pos"] > 0.85
        is_bustout = row["_gap"] >= 150
        is_alt     = i % 2 == 1

        if is_closer:   bg, fg = "2E1A1A", "F4C2C2"
        elif is_bustout: bg, fg = "1A2E1A", "B9F6CA"
        else:           bg, fg = ("EAEAF5" if is_alt else "FFFFFF"), "111111"

        adj_color = ("1B5E20" if row["_adj"] >= 30 else ("0D47A1" if row["_adj"] >= 25 else fg))
        if is_closer or is_bustout: adj_color = fg

        row_data = [row["#"], row["Song"], row["Tier"], row["City Freq"],
                    row["Shows Since Last Played"], row["Adj Score"], row["Show Position"]]
        aligns = ["center", "left", "center", "center", "center", "center", "center"]

        for col, (val, align) in enumerate(zip(row_data, aligns), 1):
            c = ws.cell(row=r, column=col, value=val)
            c.fill = PatternFill("solid", fgColor=bg)
            c.alignment = Alignment(horizontal=align, vertical="center")
            c.border = bdr
            fc = adj_color if col == 6 else fg
            c.font = Font(name="Arial", size=10, color=fc, bold=(col in [2, 6]))
        ws.row_dimensions[r].height = 18

    lr = len(rows) + 6
    ws.merge_cells(f"A{lr}:G{lr}")
    c = ws.cell(row=lr, column=1,
        value="Green adj score ≥ 30%  |  Blue adj score ≥ 25%  |  Dark green = bustout (gap ≥ 150 shows)  |  Dark red = closer")
    c.font = Font(name="Arial", size=8, italic=True, color="888888")
    c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[lr].height = 20

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ── Top 50 / Sphere helpers ─────────────────────────────────────────────────

API_KEY = "C5A172D7DD1198D7BB1C"

def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

@st.cache_data(ttl=3600)
def fetch_sphere_songs_st():
    ctx = _ssl_ctx()
    sphere_dates = []
    with open(FILEPATH, newline="") as f:
        for row in csv.DictReader(f):
            if "sphere" in row.get("venue","").lower() and row["date"].startswith("2026"):
                sphere_dates.append(row["date"])
    current_year = datetime.date.today().year
    for year in [current_year, current_year-1]:
        url = f"https://api.phish.net/v5/shows/query.json?apikey={API_KEY}&year={year}"
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                data = json.loads(r.read())
            for show in data.get("data",[]):
                if "sphere" in show.get("venue","").lower():
                    d = show.get("showdate","")
                    if d and d.startswith("2026") and d not in sphere_dates:
                        sphere_dates.append(d)
        except: pass

    sphere_dates = sorted(set(sphere_dates))
    song_dates = {}
    for date in sphere_dates:
        url = f"https://api.phish.net/v5/setlists/showdate/{date}.json?apikey={API_KEY}"
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=10) as r:
                data = json.loads(r.read())
            for s in data.get("data",[]):
                name = s["song"]
                song_dates.setdefault(name, [])
                if date not in song_dates[name]:
                    song_dates[name].append(date)
        except: pass
    return song_dates, sphere_dates

@st.cache_data(ttl=3600)
def build_top50_st():
    global_counter = Counter()
    global_shows = 0
    with open(FILEPATH, newline="") as f:
        for row in csv.DictReader(f):
            if not row["date"][:4].isdigit() or int(row["date"][:4]) < START_YEAR:
                continue
            songs = [s.strip() for s in row["setlist"].split("|") if s.strip()]
            if not songs: continue
            global_shows += 1
            global_counter.update(songs)
    return global_counter, global_shows

def _build_top50_xlsx_buf(top50, global_shows, sphere_songs):
    """Build the Top 50 xlsx in memory and return a BytesIO buffer."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Top 50 Phish Songs"

    thin = Side(style="thin", color="333333")
    bdr  = Border(left=thin, right=thin, top=thin, bottom=thin)
    generated = datetime.datetime.now().strftime("%B %d, %Y  %I:%M %p")

    ws.merge_cells("A1:F1")
    c = ws["A1"]
    c.value     = "PHISH  —  Top 50 Most Played Songs (2008–Present)"
    c.font      = Font(name="Arial", bold=True, size=16, color="F0E68C")
    c.fill      = PatternFill("solid", fgColor="1A1A2E")
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 36

    ws.merge_cells("A2:F2")
    c = ws["A2"]
    c.value     = (f"Based on {global_shows} shows  |  % = chance on any given night  |  "
                   f"★ = played at Sphere  |  Updated: {generated}")
    c.font      = Font(name="Arial", italic=True, size=9, color="AAAACC")
    c.fill      = PatternFill("solid", fgColor="1A1A2E")
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 16
    ws.row_dimensions[3].height = 5

    headers    = ["Rank", "Song", "Times Played", "% Any Night", "Sphere Dates", "YouTube"]
    col_widths = [7, 36, 16, 14, 28, 14]
    for col, (h, w) in enumerate(zip(headers, col_widths), 1):
        c = ws.cell(row=4, column=col, value=h)
        c.font      = Font(name="Arial", bold=True, size=11, color="F0E68C")
        c.fill      = PatternFill("solid", fgColor="1A1A2E")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border    = bdr
        ws.column_dimensions[get_column_letter(col)].width = w
    ws.row_dimensions[4].height = 22

    TIER_FILL_X = {
        "green":  ("1B4D1B", "90EE90"),
        "orange": ("4D2E00", "FFB347"),
        "purple": ("2E0050", "CE93D8"),
    }
    SPHERE_BG_X, SPHERE_FG_X = "7B5800", "FFFACD"

    for rank, (song, count) in enumerate(top50, 1):
        r   = rank + 4
        pct = count / global_shows * 100
        key = "green" if rank <= 10 else ("orange" if rank <= 25 else "purple")
        bg, fg = TIER_FILL_X[key]
        dates_played = sphere_songs.get(song, [])
        sphere_label = ("★  " + ",  ".join(d[5:] for d in sorted(dates_played))
                        if dates_played else "")
        yt_query = urllib.parse.quote_plus(f"Phish {song} Sphere Las Vegas 2026")
        yt_url   = f"https://www.youtube.com/results?search_query={yt_query}"

        row_data = [rank, song, count, f"{pct:.1f}%", sphere_label]
        aligns   = ["center", "left", "center", "center", "center"]
        for col, (val, align) in enumerate(zip(row_data, aligns), 1):
            c = ws.cell(row=r, column=col, value=val)
            if col == 5 and dates_played:
                c.fill = PatternFill("solid", fgColor=SPHERE_BG_X)
                c.font = Font(name="Arial", size=10, color=SPHERE_FG_X, bold=True)
            else:
                c.fill = PatternFill("solid", fgColor=bg)
                c.font = Font(name="Arial", size=10, color=fg, bold=(col in [2, 4]))
            c.alignment = Alignment(horizontal=align, vertical="center")
            c.border    = bdr

        yt_cell = ws.cell(row=r, column=6)
        if dates_played:
            yt_cell.value     = "▶ Watch"
            yt_cell.hyperlink = yt_url
            yt_cell.fill      = PatternFill("solid", fgColor=SPHERE_BG_X)
            yt_cell.font      = Font(name="Arial", size=10, color="4FC3F7", bold=True, underline="single")
        else:
            yt_cell.fill = PatternFill("solid", fgColor=bg)
            yt_cell.font = Font(name="Arial", size=10, color=fg)
        yt_cell.alignment = Alignment(horizontal="center", vertical="center")
        yt_cell.border    = bdr
        ws.row_dimensions[r].height = 18

    lr = 57
    ws.merge_cells(f"A{lr}:F{lr}")
    c = ws.cell(row=lr, column=1,
                value="Green = Top 10  |  Orange = Ranks 11–25  |  Purple = Ranks 26–50  |  Gold = played at Sphere 2026")
    c.font      = Font(name="Arial", size=8, italic=True, color="888888")
    c.alignment = Alignment(horizontal="left", vertical="center")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def ask_trey_st(question, global_counter, global_shows):
    all_songs = list(global_counter.keys())
    q_lower = question.lower()
    match = None
    best_score = 0
    for song in all_songs:
        if song.lower() in q_lower and len(song) > best_score:
            match = song
            best_score = len(song)
    if not match:
        words = question.replace("?","").replace(",","").split()
        for length in range(min(6,len(words)),0,-1):
            for start in range(len(words)-length+1):
                phrase = " ".join(words[start:start+length])
                close = difflib.get_close_matches(phrase, all_songs, n=1, cutoff=0.55)
                if close:
                    match = close[0]; break
            if match: break
    if not match:
        return None, "I didn't catch a song name — try something like 'Will you play Sand?' and I'll give you the real numbers.", {}

    count = global_counter[match]
    pct   = count / global_shows * 100
    sphere_songs, sphere_dates = fetch_sphere_songs_st()
    sphere_played = sphere_songs.get(match, [])
    shows_done  = len([d for d in sphere_dates if d <= datetime.date.today().isoformat()])
    shows_left  = len(sphere_dates) - shows_done

    song_last_idx = {}
    show_idx = 0
    with open(FILEPATH, newline="") as f:
        for row in csv.DictReader(f):
            if not row["date"][:4].isdigit() or int(row["date"][:4]) < START_YEAR:
                continue
            songs_row = [s.strip() for s in row["setlist"].split("|") if s.strip()]
            if not songs_row:
                continue  # skip rows with no setlist (future/scheduled shows)
            for s in songs_row:
                song_last_idx[s] = show_idx
            show_idx += 1
    gap = max(0, global_shows - song_last_idx.get(match, 0) - 1)
    log_denom = math.log(global_shows + 1) if global_shows > 0 else 1
    adj_pct = min(pct * (1 + math.log(gap + 1) / log_denom), 99.9)

    if sphere_played:
        if len(sphere_played) > 1:
            answer = f"Ha — {match}! We've already played that {len(sphere_played)} times this run ({', '.join(d[5:] for d in sphere_played)}). Historically it shows up in {pct:.1f}% of our shows."
        else:
            answer = f"{match} — yeah, we played that on {sphere_played[0][5:]}. It's in our setlist about {pct:.1f}% of the time overall. Could we revisit it? Maybe — but we don't like to repeat too much in a single run."
    elif gap > 200:
        answer = f"{match} — oh man. That one's been sitting in the vault for {gap} shows. Historically {pct:.1f}% of nights, but gap-adjusted it's up around {adj_pct:.1f}%. We've got {shows_left} nights left at the Sphere... keep your ears open."
    elif pct >= 15:
        answer = f"{match} is basically a staple — we play it {pct:.1f}% of shows. It hasn't shown up yet this Sphere run and we've got {shows_left} nights left. I'd feel confident betting on this one."
    elif pct >= 5:
        answer = f"Good question. {match} shows up in about {pct:.1f}% of our shows — a song we genuinely love. Last played {gap} shows ago. Adjusted probability: {adj_pct:.1f}%. There's a real shot."
    elif pct >= 1:
        answer = f"{match} — that's a deep cut. About {pct:.1f}% of shows, {gap} shows since we last played it. These Sphere shows feel special though. Adjusted probability: {adj_pct:.1f}%. Stranger things have happened."
    else:
        answer = f"Oh wow, {match} — now THAT would be something. We've played it {count} time{'s' if count!=1 else ''} since 2008. The Sphere feels like the right place to dust off something unexpected."

    stats = {"pct": round(pct,1), "gap": gap, "adj": round(adj_pct,1), "sphere": [d[5:] for d in sphere_played]}
    return match, answer, stats


# ── UI ──────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Gotta-Jibbootistics", page_icon="🎸", layout="centered")

st.markdown("""
    <style>
    /* Soft dark base with a very subtle donut pattern */
    .stApp {
        background-color: #12121f;
        background-image:
            radial-gradient(circle at center, transparent 23%, rgba(232, 128, 110, 0.06) 23%, rgba(232, 128, 110, 0.06) 47%, transparent 47%),
            radial-gradient(circle at center, transparent 23%, rgba(120, 160, 200, 0.05) 23%, rgba(120, 160, 200, 0.05) 47%, transparent 47%),
            linear-gradient(135deg, #14142a 0%, #0e0e1c 50%, #14142a 100%);
        background-size: 180px 180px, 180px 180px, 100% 100%;
        background-position: 0 0, 90px 90px, 0 0;
        background-attachment: fixed;
    }

    /* Refined content card */
    .block-container {
        background: rgba(20, 20, 36, 0.78);
        border-radius: 16px;
        padding: 2.5rem 2.5rem 2.5rem 2.5rem !important;
        border: 1px solid rgba(255, 255, 255, 0.06);
        box-shadow: 0 8px 40px rgba(0, 0, 0, 0.4);
        backdrop-filter: blur(8px);
    }

    /* Brighter, cleaner typography */
    html, body, [class*="stApp"], .stMarkdown, .stMarkdown p,
    .stMarkdown li, .stMarkdown span, label, .stCaption,
    div[data-testid="stMarkdownContainer"] p {
        color: #F5F5F7 !important;
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", Arial, sans-serif !important;
    }
    h1 {
        color: #FFF3B0 !important;
        font-weight: 700 !important;
        letter-spacing: -0.01em;
    }
    h2, h3, h4 {
        color: #FFE98A !important;
        font-weight: 600 !important;
    }
    .stMarkdown small, .stCaption, div[data-testid="stCaptionContainer"] {
        color: #B8B8C8 !important;
    }

    /* Inputs */
    .stTextInput > div > div > input,
    .stSelectbox > div > div {
        background-color: #1e1e34 !important;
        color: #FFF3B0 !important;
        border: 1px solid rgba(255, 255, 255, 0.12) !important;
        border-radius: 8px !important;
    }
    .stTextInput > div > div > input:focus {
        border-color: #FFD166 !important;
        box-shadow: 0 0 0 2px rgba(255, 209, 102, 0.2) !important;
    }

    /* Buttons */
    .stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
        background: linear-gradient(135deg, #2a2a4a 0%, #1e1e34 100%) !important;
        color: #FFF3B0 !important;
        border: 1px solid rgba(255, 209, 102, 0.3) !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        transition: all 0.15s ease !important;
    }
    .stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {
        border-color: #FFD166 !important;
        box-shadow: 0 0 12px rgba(255, 209, 102, 0.25) !important;
        transform: translateY(-1px);
    }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        border-bottom: 1px solid rgba(255, 255, 255, 0.08);
    }
    .stTabs [data-baseweb="tab"] {
        color: #B8B8C8 !important;
        font-weight: 500 !important;
        padding: 10px 16px !important;
    }
    .stTabs [aria-selected="true"] {
        color: #FFF3B0 !important;
    }

    /* Tables & dividers */
    table { border-radius: 10px; overflow: hidden; }
    hr { border-color: rgba(255, 255, 255, 0.08) !important; }

    /* Alerts softened */
    div[data-testid="stAlert"] {
        border-radius: 10px;
        background: rgba(30, 30, 52, 0.85) !important;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🎸 Gotta-Jibbootistics")
st.markdown('<p style="color:#e85545;font-style:italic;font-size:13px;margin-top:-12px">whatever you do, take care of your shoes</p>', unsafe_allow_html=True)
st.markdown("*Predict the most probable setlist for any city based on historical data (2008–present), gap analysis, and song position.*")

tab1, tab2, tab3 = st.tabs(["🎸 Setlist Predictor", "🏆 Top 50 · Sphere 2026", "🔮 Sphere Predictor"])

# ═══════════════════════════════════════════════════════════
# TAB 1 — Setlist Predictor
# ═══════════════════════════════════════════════════════════
with tab1:
    st.divider()

    city = st.text_input("Enter a city:", placeholder="e.g. Albany, Chicago, Noblesville")

    if city:
        with st.spinner(f"Generating setlist for {city}..."):
            rows, city_shows, locations = generate_setlist(city)

        if rows is None:
            st.error(f"No shows found for '{city}'. Try a different city name.")
        else:
            st.success(f"Found **{city_shows} shows** in {', '.join(locations)} — setlist has **{len(rows)} songs**")

            # Color-code the table
            tier_colors = {
                "Staple":     "#FFD700",
                "Common":     "#4FC3F7",
                "Occasional": "#A5D6A7",
                "Rare":       "#CE93D8",
            }

            header_cols = ["#", "Song", "Tier", "City Freq", "Gap", "Adj Score", "Position"]
            col_widths_pct = [4, 30, 12, 10, 8, 10, 12]

            header_html = "".join(
                f'<th style="background:#1A1A2E;color:#F0E68C;padding:8px;text-align:center;width:{w}%">{h}</th>'
                for h, w in zip(header_cols, col_widths_pct)
            )

            rows_html = ""
            for row in rows:
                is_closer  = row["_pos"] > 0.85
                is_bustout = row["_gap"] >= 150
                if is_closer:    bg, fg = "#2E1A1A", "#F4C2C2"
                elif is_bustout: bg, fg = "#1A2E1A", "#B9F6CA"
                else:            bg, fg = "#1a1a2e" if row["#"] % 2 == 0 else "#16213e", "#EEEEEE"

                tier_color = tier_colors.get(row["Tier"], "#FFFFFF")
                adj_color = "#66BB6A" if row["_adj"] >= 30 else ("#42A5F5" if row["_adj"] >= 25 else fg)

                rows_html += f"""
                <tr style="background:{bg};color:{fg}">
                    <td style="text-align:center;padding:6px">{row['#']}</td>
                    <td style="padding:6px;font-weight:bold">{row['Song']}</td>
                    <td style="text-align:center;padding:6px;color:{tier_color}">{row['Tier']}</td>
                    <td style="text-align:center;padding:6px">{row['City Freq']}</td>
                    <td style="text-align:center;padding:6px">{row['Shows Since Last Played']}</td>
                    <td style="text-align:center;padding:6px;color:{adj_color};font-weight:bold">{row['Adj Score']}</td>
                    <td style="text-align:center;padding:6px">{row['Show Position']}</td>
                </tr>"""

            table_html = f"""
            <table style="width:100%;border-collapse:collapse;font-family:Arial;font-size:14px">
                <thead><tr>{header_html}</tr></thead>
                <tbody>{rows_html}</tbody>
            </table>
            """
            st.markdown(table_html, unsafe_allow_html=True)

            st.markdown("""
            <div style="font-size:11px;color:#666;margin-top:8px">
            🟡 Staple &nbsp;|&nbsp; 🔵 Common &nbsp;|&nbsp; 🟢 Occasional &nbsp;|&nbsp; 🟣 Rare &nbsp;|&nbsp;
            <span style="background:#1A2E1A;color:#B9F6CA;padding:1px 4px">Dark green = bustout (gap ≥ 150)</span> &nbsp;|&nbsp;
            <span style="background:#2E1A1A;color:#F4C2C2;padding:1px 4px">Dark red = closer</span>
            </div>
            """, unsafe_allow_html=True)

            st.divider()

            # Highlights
            top = max(rows, key=lambda r: r["_adj"])
            bustouts = [r for r in rows if r["_gap"] >= 150]
            closer = next((r for r in reversed(rows) if r["_pos"] > 0.85), rows[-1])

            st.markdown(f"**Top pick:** {top['Song']} ({top['Adj Score']} adj score) — the most probable song based on city history and gap.")
            if bustouts:
                st.markdown(f"**Bustout watch:** {', '.join(r['Song'] for r in bustouts)} — each overdue by {', '.join(str(r['_gap']) for r in bustouts)} shows respectively.")
            st.markdown(f"**Expected closer:** {closer['Song']}")

            st.divider()

            # Download button
            xlsx_buf = build_xlsx(rows, city, city_shows)
            st.download_button(
                label="⬇️ Download Spreadsheet (.xlsx)",
                data=xlsx_buf,
                file_name=f"{city.replace(' ', '_')}_Setlist.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

            st.divider()

            # Bingo card
            st.subheader("🎲 Bingo Card")
            st.caption("🟡 From predicted setlist · 🔵 Globally common · 🟣 Rare / uncommon")
            if st.button("Generate Bingo Card"):
                cards = generate_bingo(city)
                if cards:
                    st.session_state["bingo_cards"] = cards
                    st.session_state["bingo_city"]  = city

            if "bingo_cards" in st.session_state and st.session_state.get("bingo_city") == city:
                cards = st.session_state["bingo_cards"]
                cat_styles = {
                    "setlist": ("background:#2a2a10;color:#F0E68C;border:1px solid #555522", "🟡"),
                    "common":  ("background:#0d2235;color:#7ec8e3;border:1px solid #1a4a66", "🔵"),
                    "rare":    ("background:#2a0d35;color:#ce93d8;border:1px solid #5a2a6a", "🟣"),
                }
                cell_style = "padding:8px 4px;text-align:center;font-size:12px;font-weight:bold;border-radius:6px;min-height:60px;display:flex;align-items:center;justify-content:center;word-break:break-word;"

                col_labels = ["B", "I", "N", "G", "O"]
                header_cols_b = st.columns(5)
                for col, label in zip(header_cols_b, col_labels):
                    col.markdown(f'<div style="text-align:center;font-size:22px;font-weight:bold;color:#F0E68C">{label}</div>', unsafe_allow_html=True)

                for row_i in range(5):
                    cols = st.columns(5)
                    for col_i, col in enumerate(cols):
                        card = cards[row_i * 5 + col_i]
                        bg_style, _ = cat_styles[card["cat"]]
                        col.markdown(
                            f'<div style="{bg_style};{cell_style}">{card["song"]}</div>',
                            unsafe_allow_html=True
                        )

                st.markdown("")
                pdf_buf = build_bingo_pdf(cards, city)
                st.download_button(
                    label="🖨️ Download Printable PDF",
                    data=pdf_buf,
                    file_name=f"{city.replace(' ', '_')}_Bingo.pdf",
                    mime="application/pdf",
                )


# ═══════════════════════════════════════════════════════════
# TAB 2 — Top 50 · Sphere 2026 Tracker
# ═══════════════════════════════════════════════════════════
with tab2:
    st.markdown("### 🏟️ Phish at the Sphere — Las Vegas 2026")
    st.caption("Top 50 most-played songs since 2008 · Live Sphere setlist data from phish.net · Updates hourly")

    with st.spinner("Loading Top 50 data & Sphere setlists..."):
        global_counter_t50, global_shows_t50 = build_top50_st()
        sphere_songs_t50, sphere_dates_t50 = fetch_sphere_songs_st()

    top50 = global_counter_t50.most_common(50)
    today_str = datetime.date.today().isoformat()
    shows_done = len([d for d in sphere_dates_t50 if d <= today_str])
    shows_left = len(sphere_dates_t50) - shows_done

    # ── Possum insight bubble ──────────────────────────────
    sphere_played_songs = [s for s in global_counter_t50 if sphere_songs_t50.get(s)]
    top10_names = [s for s, _ in top50[:10]]
    top10_at_sphere = [s for s in top10_names if sphere_songs_t50.get(s)]
    most_played_sphere = sorted(sphere_songs_t50.items(), key=lambda x: len(x[1]), reverse=True)

    insights = []
    if top10_at_sphere:
        insights.append(f"Of the Top 10 all-time songs, **{len(top10_at_sphere)}** have already been played at the Sphere: {', '.join(top10_at_sphere)}.")
    if most_played_sphere:
        song_mp, dates_mp = most_played_sphere[0]
        insights.append(f"**{song_mp}** has been played the most times this Sphere run — {len(dates_mp)} night{'s' if len(dates_mp)>1 else ''}.")
    unplayed_top10 = [s for s in top10_names if not sphere_songs_t50.get(s)]
    if unplayed_top10:
        insights.append(f"Still waiting on these Top-10 staples to appear at the Sphere: **{', '.join(unplayed_top10[:3])}**{'...' if len(unplayed_top10)>3 else ''}.")
    if shows_left > 0:
        insights.append(f"**{shows_left} show{'s' if shows_left>1 else ''}** left in the Sphere run. Still plenty of time for surprises.")
    if shows_left == 0:
        insights.append("The Sphere run is complete! What a historic stretch of shows.")
    sphere_only = [s for s, _ in top50 if s not in [x for x,_ in top50[:10]] and sphere_songs_t50.get(s)]
    if sphere_only:
        insights.append(f"Some deeper cuts showed up at the Sphere: **{', '.join(sphere_only[:3])}** — the band is digging into the catalog.")

    insight_text = random.choice(insights) if insights else "The numbers don't lie — this Sphere run is something special."

    st.markdown(f"""
    <div style="display:flex;align-items:flex-start;gap:14px;margin-bottom:20px">
        <div style="font-size:48px;line-height:1">🐀</div>
        <div style="background:#1a1a2e;border:1px solid #444;border-radius:12px;border-top-left-radius:2px;padding:14px 18px;max-width:640px">
            <div style="font-size:11px;color:#888;margin-bottom:4px;font-style:italic">Possum Insight</div>
            <div style="color:#F0E68C;font-size:14px;line-height:1.6">{insight_text}</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    # ── Ask Trey section ───────────────────────────────────
    st.markdown("#### 🍩 Ask Trey — Will they play it at the Sphere?")

    with st.form("ask_trey_form", clear_on_submit=False):
        trey_q = st.text_input(
            "Ask about any song:",
            placeholder="e.g. Will you play Sand? What about Tweezer?",
            key="trey_input",
        )
        trey_submit = st.form_submit_button("Ask Trey 🎸")

    if trey_submit:
        if trey_q.strip():
            try:
                matched, answer, stats = ask_trey_st(trey_q, global_counter_t50, global_shows_t50)
                st.session_state["trey_response"] = {
                    "matched": matched,
                    "answer": answer,
                    "stats": stats,
                }
            except Exception as e:
                st.session_state["trey_response"] = {
                    "matched": None,
                    "answer": f"Something broke on my end — {type(e).__name__}: {e}",
                    "stats": {},
                }
        else:
            st.session_state["trey_response"] = {
                "matched": None,
                "answer": "Type a song name to ask Trey!",
                "stats": {},
            }

    # Render stored response (persists across reruns)
    resp = st.session_state.get("trey_response")
    if resp:
        if resp["matched"]:
            st.markdown(f"""
            <div style="display:flex;align-items:flex-start;gap:14px;margin:12px 0">
                <div style="font-size:40px;line-height:1">🍩</div>
                <div style="background:#1a1a2e;border:1px solid #444;border-radius:12px;border-top-left-radius:2px;padding:14px 18px;max-width:600px">
                    <div style="font-size:11px;color:#888;margin-bottom:6px;font-style:italic">Trey on <b style="color:#F0E68C">{resp['matched']}</b></div>
                    <div style="color:#eeeeee;font-size:14px;line-height:1.6;font-style:italic">"{resp['answer']}"</div>
                </div>
            </div>
            """, unsafe_allow_html=True)
            stats = resp["stats"] or {}
            chip_style = "display:inline-block;background:#2a2a4a;color:#7ec8e3;border:1px solid #444;border-radius:20px;padding:3px 12px;font-size:12px;margin:4px"
            chips_html = ""
            if stats.get("pct") is not None:
                chips_html += f'<span style="{chip_style}">📊 {stats["pct"]}% of shows</span>'
            if stats.get("gap") is not None:
                chips_html += f'<span style="{chip_style}">⏳ {stats["gap"]} shows since last played</span>'
            if stats.get("adj") is not None:
                chips_html += f'<span style="{chip_style}">🎯 {stats["adj"]}% gap-adjusted</span>'
            if stats.get("sphere"):
                chips_html += f'<span style="display:inline-block;background:#3a2800;color:#FFFACD;border:1px solid #7B5800;border-radius:20px;padding:3px 12px;font-size:12px;margin:4px">★ Sphere: {", ".join(stats["sphere"])}</span>'
            if chips_html:
                st.markdown(f'<div style="margin-left:58px">{chips_html}</div>', unsafe_allow_html=True)
        else:
            st.warning(resp["answer"])

    st.divider()

    # ── Top 50 table ───────────────────────────────────────
    st.markdown("#### 🎵 Top 50 Most-Played Songs (2008–Present)")
    generated_ts = datetime.datetime.now().strftime("%B %d, %Y")
    st.caption(f"Based on {global_shows_t50} shows · % = chance on any given night · ★ = played at Sphere 2026 · Updated {generated_ts}")

    TIER_FILL_T50 = {
        "green":  ("#1B4D1B", "#90EE90"),
        "orange": ("#4D2E00", "#FFB347"),
        "purple": ("#2E0050", "#CE93D8"),
    }
    SPHERE_BG_T50, SPHERE_FG_T50 = "#3a2800", "#FFFACD"

    t50_header = """
    <tr>
        <th style="background:#1A1A2E;color:#F0E68C;padding:8px;text-align:center;width:6%">Rank</th>
        <th style="background:#1A1A2E;color:#F0E68C;padding:8px;text-align:left;width:36%">Song</th>
        <th style="background:#1A1A2E;color:#F0E68C;padding:8px;text-align:center;width:14%">Times Played</th>
        <th style="background:#1A1A2E;color:#F0E68C;padding:8px;text-align:center;width:12%">% Any Night</th>
        <th style="background:#1A1A2E;color:#F0E68C;padding:8px;text-align:center;width:20%">Sphere 2026</th>
        <th style="background:#1A1A2E;color:#F0E68C;padding:8px;text-align:center;width:12%">YouTube</th>
    </tr>"""

    t50_rows = ""
    for rank, (song, count) in enumerate(top50, 1):
        pct = count / global_shows_t50 * 100
        tier_key = "green" if rank <= 10 else ("orange" if rank <= 25 else "purple")
        bg, fg = TIER_FILL_T50[tier_key]

        dates_played = sphere_songs_t50.get(song, [])
        if dates_played:
            sphere_label = "★  " + ",  ".join(d[5:] for d in sorted(dates_played))
            row_bg, row_fg = SPHERE_BG_T50, SPHERE_FG_T50
        else:
            sphere_label = ""
            row_bg, row_fg = bg, fg

        yt_query = urllib.parse.quote_plus(f"Phish {song} Sphere Las Vegas 2026")
        yt_url   = f"https://www.youtube.com/results?search_query={yt_query}"
        yt_cell  = f'<a href="{yt_url}" target="_blank" style="color:#4FC3F7;font-weight:bold;text-decoration:none">▶ Watch</a>' if dates_played else ""

        t50_rows += f"""
        <tr>
            <td style="background:{bg};color:{fg};text-align:center;padding:6px;font-size:13px">{rank}</td>
            <td style="background:{row_bg};color:{row_fg};padding:6px;font-weight:bold;font-size:13px">{song}</td>
            <td style="background:{bg};color:{fg};text-align:center;padding:6px;font-size:13px">{count}</td>
            <td style="background:{bg};color:{fg};text-align:center;padding:6px;font-size:13px">{pct:.1f}%</td>
            <td style="background:{row_bg};color:{row_fg};text-align:center;padding:6px;font-size:13px;font-weight:{'bold' if dates_played else 'normal'}">{sphere_label}</td>
            <td style="background:{row_bg};text-align:center;padding:6px;font-size:13px">{yt_cell}</td>
        </tr>"""

    t50_table = f"""
    <table style="width:100%;border-collapse:collapse;font-family:Arial">
        <thead>{t50_header}</thead>
        <tbody>{t50_rows}</tbody>
    </table>
    """
    st.markdown(t50_table, unsafe_allow_html=True)

    st.markdown("""
    <div style="font-size:11px;color:#666;margin-top:10px">
    <span style="background:#1B4D1B;color:#90EE90;padding:2px 6px;border-radius:4px">Green = Top 10</span> &nbsp;
    <span style="background:#4D2E00;color:#FFB347;padding:2px 6px;border-radius:4px">Orange = Ranks 11–25</span> &nbsp;
    <span style="background:#2E0050;color:#CE93D8;padding:2px 6px;border-radius:4px">Purple = Ranks 26–50</span> &nbsp;
    <span style="background:#3a2800;color:#FFFACD;padding:2px 6px;border-radius:4px">★ Gold = Played at Sphere 2026</span>
    </div>
    """, unsafe_allow_html=True)

    # Download Top 50 as xlsx (built in memory)
    st.markdown("")
    t50_xlsx_buf = _build_top50_xlsx_buf(top50, global_shows_t50, sphere_songs_t50)
    st.download_button(
        label="⬇️ Download Top 50 Spreadsheet (.xlsx)",
        data=t50_xlsx_buf,
        file_name="Top_50_Phish_Songs_Sphere.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="dl_top50"
    )


# ═══════════════════════════════════════════════════════════
# TAB 3 — Sphere Setlist Predictor
# ═══════════════════════════════════════════════════════════
with tab3:
    st.markdown("### 🔮 Sphere Setlist Predictor")
    st.caption("Predicts setlists for upcoming Sphere shows · Excludes songs already played in this run · "
               "Boosts songs played in the last 10–15 shows prior")

    with st.spinner("Loading Sphere schedule..."):
        sphere_songs_p, sphere_dates_p = fetch_sphere_songs_st()

    today_iso  = datetime.date.today().isoformat()
    played     = sorted(d for d in sphere_dates_p if d <= today_iso)
    upcoming   = sorted(d for d in sphere_dates_p if d > today_iso)

    # Summary of run so far
    st.markdown(f"""
    <div style="background:#1a1a2e;border:1px solid #444;border-radius:10px;padding:12px 16px;margin:12px 0">
        <div style="color:#F0E68C;font-size:14px;font-weight:bold">🏟️ Sphere Run 2026</div>
        <div style="color:#aaaacc;font-size:12px;margin-top:4px">
            ✓ <b>{len(played)}</b> show{'s' if len(played)!=1 else ''} played &nbsp;·&nbsp;
            🔜 <b>{len(upcoming)}</b> upcoming &nbsp;·&nbsp;
            🎵 <b>{len(sphere_songs_p)}</b> unique songs played so far
        </div>
    </div>
    """, unsafe_allow_html=True)

    if not upcoming:
        st.info("No remaining Sphere shows on the schedule. Check back after the next tour is announced!")
    else:
        target_date = st.selectbox(
            "Select an upcoming Sphere show:",
            upcoming,
            format_func=lambda d: datetime.date.fromisoformat(d).strftime("%A, %B %d, %Y")
        )

        if st.button("🎸 Generate Sphere Prediction", key="gen_sphere"):
            st.session_state["sphere_result"] = generate_sphere_setlist(target_date, sphere_songs_p)
            st.session_state["sphere_target"] = target_date

        if st.session_state.get("sphere_result") and st.session_state.get("sphere_target") == target_date:
            result = st.session_state["sphere_result"]
            rows = result["rows"]

            pretty_date = datetime.date.fromisoformat(target_date).strftime("%B %d, %Y")
            st.success(f"🎯 Prediction for **{pretty_date}** — {len(rows)} songs "
                       f"· based on {result['source']} · {len(result['excluded'])} excluded (already played)")

            if result["recent_dates"]:
                window_label = f"{result['recent_dates'][0]} → {result['recent_dates'][-1]}"
                st.caption(f"⚡ Recent rotation boost applied from {result['window_count']} shows "
                           f"({window_label}) — {len(result['recent_songs'])} songs flagged.")
            else:
                st.caption("⚡ No shows found in the 10–15-shows-prior window (tour hasn't run that recently).")

            # Color-coded table
            tier_colors_p = {
                "Staple":     "#FFD700",
                "Common":     "#4FC3F7",
                "Occasional": "#A5D6A7",
                "Rare":       "#CE93D8",
            }

            hdrs = ["#", "Song", "Tier", "Vegas Freq", "Global", "Gap", "Adj Score", "Position", "🔥"]
            widths = [4, 26, 11, 11, 9, 7, 10, 12, 5]
            header_html_p = "".join(
                f'<th style="background:#1A1A2E;color:#F0E68C;padding:8px;text-align:center;width:{w}%">{h}</th>'
                for h, w in zip(hdrs, widths)
            )

            body_html = ""
            for row in rows:
                is_closer  = row["_pos"] > 0.85
                is_bustout = row["_gap"] >= 150
                is_recent  = row["Recent"]

                if is_recent:    bg, fg = "#3a1f00", "#FFCC80"
                elif is_closer:  bg, fg = "#2E1A1A", "#F4C2C2"
                elif is_bustout: bg, fg = "#1A2E1A", "#B9F6CA"
                else:            bg, fg = ("#1a1a2e" if row["#"] % 2 == 0 else "#16213e"), "#EEEEEE"

                tier_color = tier_colors_p.get(row["Tier"], "#FFFFFF")
                adj_color = "#66BB6A" if row["_adj"] >= 30 else ("#42A5F5" if row["_adj"] >= 25 else fg)
                recent_mark = "🔥" if is_recent else ""

                body_html += f"""
                <tr style="background:{bg};color:{fg}">
                    <td style="text-align:center;padding:6px">{row['#']}</td>
                    <td style="padding:6px;font-weight:bold">{row['Song']}</td>
                    <td style="text-align:center;padding:6px;color:{tier_color}">{row['Tier']}</td>
                    <td style="text-align:center;padding:6px">{row['Vegas/Global Freq']}</td>
                    <td style="text-align:center;padding:6px">{row['Global Freq']}</td>
                    <td style="text-align:center;padding:6px">{row['Shows Since Last Played']}</td>
                    <td style="text-align:center;padding:6px;color:{adj_color};font-weight:bold">{row['Adj Score']}</td>
                    <td style="text-align:center;padding:6px">{row['Show Position']}</td>
                    <td style="text-align:center;padding:6px;font-size:16px">{recent_mark}</td>
                </tr>"""

            table_p = f"""
            <table style="width:100%;border-collapse:collapse;font-family:Arial;font-size:13px">
                <thead><tr>{header_html_p}</tr></thead>
                <tbody>{body_html}</tbody>
            </table>
            """
            st.markdown(table_p, unsafe_allow_html=True)

            st.markdown("""
            <div style="font-size:11px;color:#666;margin-top:8px">
            🔥 = played in the last 10–15 shows (rotation boost applied) &nbsp;|&nbsp;
            <span style="background:#3a1f00;color:#FFCC80;padding:1px 4px">Orange row = recent rotation</span> &nbsp;|&nbsp;
            <span style="background:#1A2E1A;color:#B9F6CA;padding:1px 4px">Green = bustout (gap ≥ 150)</span> &nbsp;|&nbsp;
            <span style="background:#2E1A1A;color:#F4C2C2;padding:1px 4px">Red = closer</span>
            </div>
            """, unsafe_allow_html=True)

            # Highlights
            st.divider()
            top_p = max(rows, key=lambda r: r["_adj"])
            bustouts_p = [r for r in rows if r["_gap"] >= 150]
            closer_p = next((r for r in reversed(rows) if r["_pos"] > 0.85), rows[-1] if rows else None)
            recent_hits = [r for r in rows if r["Recent"]]

            st.markdown(f"**🎯 Top pick:** {top_p['Song']} ({top_p['Adj Score']} adj score)")
            if recent_hits:
                st.markdown(f"**🔥 Current rotation hits:** {', '.join(r['Song'] for r in recent_hits[:6])}"
                            f"{'...' if len(recent_hits)>6 else ''}")
            if bustouts_p:
                st.markdown(f"**💎 Bustout watch:** {', '.join(r['Song'] for r in bustouts_p)}")
            if closer_p:
                st.markdown(f"**🎬 Expected closer:** {closer_p['Song']}")

            # Excluded list (already played at Sphere)
            if result["excluded"]:
                with st.expander(f"🚫 Excluded — {len(result['excluded'])} songs already played at Sphere 2026"):
                    st.write(", ".join(result["excluded"]))

            # Download xlsx
            st.divider()
            sphere_xlsx = build_xlsx(
                [{**r, "City Freq": r["Vegas/Global Freq"]} for r in rows],
                f"Sphere {pretty_date}",
                result["city_shows"],
            )
            st.download_button(
                label="⬇️ Download Sphere Prediction (.xlsx)",
                data=sphere_xlsx,
                file_name=f"Sphere_{target_date}_Prediction.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_sphere_xlsx",
            )
