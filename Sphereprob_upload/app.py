import streamlit as st
import csv
import math
import io
import os
import random
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


# ── UI ──────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Gotta-Jibbootistics", page_icon="🎸", layout="centered")

st.markdown("""
    <style>
    .stApp {
        background-color: #4a7fa0;
        background-image:
            radial-gradient(circle at center, #4a7fa0 22%, #e85545 22%, #e85545 47%, #4a7fa0 47%),
            radial-gradient(circle at center, #4a7fa0 22%, #e85545 22%, #e85545 47%, #4a7fa0 47%);
        background-size: 120px 120px;
        background-position: 0 0, 60px 60px;
    }
    .block-container {
        background: rgba(13, 13, 26, 0.92);
        border-radius: 12px;
        padding: 2rem 2rem 2rem 2rem !important;
        backdrop-filter: blur(4px);
    }
    h1 { color: #F0E68C !important; font-family: Arial; }
    .stTextInput > div > div > input { background-color: #1a1a2e; color: #F0E68C; border: 1px solid #444; }
    </style>
""", unsafe_allow_html=True)

st.title("🎸 Gotta-Jibbootistics")
st.markdown('<p style="color:#e85545;font-style:italic;font-size:13px;margin-top:-12px">whatever you do, take care of your shoes</p>', unsafe_allow_html=True)
st.markdown("*Predict the most probable setlist for any city based on historical data (2008–present), gap analysis, and song position.*")

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
            header_cols = st.columns(5)
            for col, label in zip(header_cols, col_labels):
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
