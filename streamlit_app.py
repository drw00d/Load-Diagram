# streamlit_app.py
import streamlit as st
import pandas as pd
import streamlit.components.v1 as components

st.set_page_config(page_title="Load Diagram Optimizer", layout="wide")
st.title("Load Diagram Optimizer")

MASTER_PATH = "data/Ortec SP Product Master.xlsx"

# --- columns ---
COL_COMMODITY = "Product Type"      # primary filter
COL_FACILITY = "Facility Id"
COL_PRODUCT_ID = "Sales Product Id"
COL_DESC = "Short Descrip"          # fallback to "Descrip"
COL_ACTIVE = "Active"
COL_UNIT_H = "Unit Height (In)"
COL_UNIT_WT = "Unit Weight (lbs)"
COL_HALF_PACK = "Half Pack"

COL_THICK = "Panel Thickness"
COL_WIDTH = "Width"
COL_LENGTH = "Length"


# =============================
# Load + Normalize
# =============================
@st.cache_data(show_spinner=False)
def load_product_master(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, engine="openpyxl")
    df.columns = [c.strip() for c in df.columns]

    global COL_DESC
    if COL_DESC not in df.columns and "Descrip" in df.columns:
        COL_DESC = "Descrip"

    required = [COL_PRODUCT_ID, COL_UNIT_H, COL_UNIT_WT, COL_COMMODITY]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Product Master missing required columns: {missing}")

    df[COL_PRODUCT_ID] = df[COL_PRODUCT_ID].astype(str).str.strip()
    df[COL_COMMODITY] = df[COL_COMMODITY].astype(str).str.strip()

    if COL_FACILITY in df.columns:
        df[COL_FACILITY] = df[COL_FACILITY].astype(str).str.strip()
    if COL_DESC in df.columns:
        df[COL_DESC] = df[COL_DESC].astype(str)

    for c in [COL_UNIT_H, COL_UNIT_WT, COL_THICK, COL_WIDTH, COL_LENGTH]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    if COL_HALF_PACK in df.columns:
        hp = df[COL_HALF_PACK].astype(str).str.strip().str.upper()
        df[COL_HALF_PACK] = hp.isin(["Y", "YES", "TRUE", "1"])

    if COL_ACTIVE in df.columns:
        act = df[COL_ACTIVE].astype(str).str.strip().str.upper()
        df = df[act.isin(["Y", "YES", "TRUE", "1", "ACTIVE"])].copy()

    df = df.dropna(subset=[COL_UNIT_H, COL_UNIT_WT])
    return df


def lookup_product(df: pd.DataFrame, product_id: str) -> dict:
    pid = str(product_id).strip()
    row = df.loc[df[COL_PRODUCT_ID] == pid]
    if row.empty:
        raise KeyError(f"Sales Product Id not found: {pid}")
    r = row.iloc[0]
    return {
        "product_id": pid,
        "commodity": r[COL_COMMODITY] if COL_COMMODITY in df.columns else "",
        "facility_id": r[COL_FACILITY] if COL_FACILITY in df.columns else "",
        "description": r[COL_DESC] if COL_DESC in df.columns else "",
        "unit_height_in": float(r[COL_UNIT_H]),
        "unit_weight_lbs": float(r[COL_UNIT_WT]),
        "half_pack": bool(r[COL_HALF_PACK]) if COL_HALF_PACK in df.columns else False,
        "thickness": float(r[COL_THICK]) if COL_THICK in df.columns and pd.notna(r[COL_THICK]) else None,
        "width": float(r[COL_WIDTH]) if COL_WIDTH in df.columns and pd.notna(r[COL_WIDTH]) else None,
        "length": float(r[COL_LENGTH]) if COL_LENGTH in df.columns and pd.notna(r[COL_LENGTH]) else None,
    }


# =============================
# Allocation (15 floor spots)
# =============================
def allocate_to_floor_spots(mix: list[dict], floor_spots: int, max_tiers: int) -> list[dict]:
    """
    Create a 1x15 plan: each spot is a vertical stack.
    We fill spot 1..15 in order. A spot can hold up to max_tiers tiers.

    Returns list length=floor_spots of:
      {"spot": i, "product_id": str|None, "tiers": int, "unit_height_in": float}
    """
    plan = [{"spot": i + 1, "product_id": None, "tiers": 0, "unit_height_in": 0.0} for i in range(floor_spots)]
    spot_i = 0

    for m in mix:
        pid = m["product_id"]
        remaining = int(m["units"])
        uh = float(m["unit_height_in"])

        while remaining > 0 and spot_i < floor_spots:
            take = min(remaining, max_tiers)
            plan[spot_i] = {"spot": spot_i + 1, "product_id": pid, "tiers": take, "unit_height_in": uh}
            remaining -= take
            spot_i += 1

        if remaining > 0:
            break

    return plan


# =============================
# SVG Helpers
# =============================
def color_for_pid(pid: str) -> str:
    palette = [
        "#d9ecff", "#ffe3d9", "#e6ffd9", "#f2e6ff", "#fff5cc",
        "#d9fff7", "#ffd9f1", "#e0e0ff", "#ffe0b2", "#d7ffd9",
    ]
    h = 0
    for ch in pid:
        h = (h * 31 + ord(ch)) % 10_000
    return palette[h % len(palette)]


def render_top_1x15_svg(car_id: str, plan: list[dict], note: str) -> str:
    """
    1 row, 15 columns (like Load Xpert top view for 1-wide loads).
    """
    cols = len(plan)
    W, H = 1200, 220
    margin = 30
    header_h = 55
    x0, y0 = margin, margin + header_h
    w = W - 2 * margin
    h = H - y0 - margin
    cell_w = w / cols

    svg = []
    svg.append(f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">')
    svg.append(f'<rect x="{margin}" y="{margin}" width="{W-2*margin}" height="{H-2*margin}" fill="white" stroke="black" stroke-width="2"/>')
    svg.append(f'<text x="{margin+8}" y="{margin+22}" font-size="18" font-weight="600">Car: {car_id} — Top View (15 floor spots)</text>')
    svg.append(f'<text x="{margin+8}" y="{margin+42}" font-size="13">{note}</text>')

    # draw 15 boxes
    for i, s in enumerate(plan):
        x = x0 + i * cell_w
        y = y0
        pid = s["product_id"]
        tiers = s["tiers"]

        if not pid:
            fill = "#ffffff"
            label = ""
        else:
            fill = color_for_pid(pid)
            label = f"{pid} x{tiers}"

        svg.append(f'<rect x="{x}" y="{y}" width="{cell_w}" height="{h}" fill="{fill}" stroke="#333" stroke-width="1"/>')
        svg.append(f'<text x="{x+6}" y="{y+16}" font-size="12" fill="#333">{s["spot"]}</text>')

        if label:
            svg.append(f'<text x="{x+6}" y="{y+44}" font-size="12" fill="#000">{label}</text>')

    svg.append("</svg>")
    return "\n".join(svg)


def render_side_1x15_svg(car_id: str, plan: list[dict], side_name: str, car_inside_height_in: float) -> str:
    """
    Side view: 15 stacks across.
    Bar height = tiers * unit_height_in, scaled to inside height.
    """
    cols = len(plan)
    W, H = 1200, 320
    margin = 30
    header_h = 40

    x0, y0 = margin, margin + header_h
    plot_w = W - 2 * margin
    plot_h = H - y0 - margin
    cell_w = plot_w / cols
    base_y = y0 + plot_h

    # max stack height (inches)
    max_stack_in = 0.0
    for s in plan:
        if s["product_id"]:
            max_stack_in = max(max_stack_in, float(s["tiers"]) * float(s["unit_height_in"]))

    ref_h = max(car_inside_height_in, max_stack_in, 1.0)
    scale = plot_h / ref_h

    svg = []
    svg.append(f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">')
    svg.append(f'<rect x="{margin}" y="{margin}" width="{W-2*margin}" height="{H-2*margin}" fill="white" stroke="black" stroke-width="2"/>')
    svg.append(f'<text x="{margin+8}" y="{margin+24}" font-size="16" font-weight="600">Car: {car_id} — {side_name}</text>')

    # base line
    svg.append(f'<line x1="{x0}" y1="{base_y}" x2="{x0+plot_w}" y2="{base_y}" stroke="#000" stroke-width="1"/>')
    # top ref line
    top_ref_y = base_y - car_inside_height_in * scale
    svg.append(f'<line x1="{x0}" y1="{top_ref_y}" x2="{x0+plot_w}" y2="{top_ref_y}" stroke="#999" stroke-width="1" />')
    svg.append(f'<text x="{x0+4}" y="{top_ref_y-6}" font-size="12" fill="#666">Inside height ref</text>')

    for i, s in enumerate(plan):
        x = x0 + i * cell_w + 2
        w = cell_w - 4

        pid = s["product_id"]
        tiers = s["tiers"]
        uh = s["unit_height_in"]

        if not pid:
            bar_h = 0
            fill = "#ffffff"
            label = ""
        else:
            bar_h = float(tiers) * float(uh) * scale
            fill = color_for_pid(pid)
            label = f"{pid} x{tiers}"

        y = base_y - bar_h
        svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{bar_h}" fill="{fill}" stroke="#333" stroke-width="1"/>')

        # spot number
        svg.append(f'<text x="{x+3}" y="{base_y+14}" font-size="11" fill="#333">{s["spot"]}</text>')

        # label near top of bar
        if label:
            svg.append(f'<text x="{x+3}" y="{y+14}" font-size="11" fill="#000">{label[:16]}</text>')

    svg.append("</svg>")
    return "\n".join(svg)


# =============================
# App
# =============================
try:
    pm = load_product_master(MASTER_PATH)
except Exception as e:
    st.error(f"Could not load Product Master at '{MASTER_PATH}'. Error: {e}")
    st.stop()

with st.sidebar:
    st.header("Settings")
    car_id = st.text_input("Car ID", value="TBOX632012")
    scenario = st.selectbox("Scenario", ["RTD_SHTG", "BC", "SIDING"], index=0)
    max_tiers = st.slider("Max tiers per spot (stack height)", 1, 6, 4)  # <-- you said 3 or 4
    car_inside_height_in = st.number_input("Inside height ref (in)", min_value=60.0, value=110.0, step=1.0)

    st.divider()
    view_mode = st.radio("Diagram view", ["Top + Both Sides", "Top only", "Sides only"], index=0)

st.success(f"Product Master loaded: {len(pm):,} rows")

# Commodity is primary filter
commodities = sorted(pm[COL_COMMODITY].dropna().astype(str).unique().tolist())
commodity_selected = st.selectbox("Commodity / Product Type (required)", ["(Select)"] + commodities)

if "mix" not in st.session_state:
    st.session_state.mix = []
if "selected_commodity" not in st.session_state:
    st.session_state.selected_commodity = commodity_selected
if "selected_facility" not in st.session_state:
    st.session_state.selected_facility = "(All facilities)"

# clear mix if commodity changes
if commodity_selected != st.session_state.selected_commodity:
    if st.session_state.mix:
        st.warning("Commodity changed — clearing mix.")
        st.session_state.mix = []
    st.session_state.selected_commodity = commodity_selected
    st.session_state.selected_facility = "(All facilities)"

if commodity_selected == "(Select)":
    st.info("Select a Commodity/Product Type to proceed.")
    st.stop()

pm_c = pm[pm[COL_COMMODITY].astype(str) == str(commodity_selected)].copy()

# Facility list filtered by commodity
facilities = sorted(pm_c[COL_FACILITY].dropna().astype(str).unique().tolist()) if COL_FACILITY in pm_c.columns else []
facility_selected = st.selectbox("Facility Id (filtered by commodity)", ["(All facilities)"] + facilities)

if facility_selected != st.session_state.selected_facility:
    if st.session_state.mix:
        st.warning("Facility changed — clearing mix.")
        st.session_state.mix = []
    st.session_state.selected_facility = facility_selected

pm_cf = pm_c.copy()
if facility_selected != "(All facilities)" and COL_FACILITY in pm_cf.columns:
    pm_cf = pm_cf[pm_cf[COL_FACILITY].astype(str) == str(facility_selected)].copy()

# Search
search = st.text_input("Search (by Product Id or Description)", value="")
if search.strip():
    s = search.strip().lower()
    pm_cf = pm_cf[
        pm_cf[COL_PRODUCT_ID].astype(str).str.lower().str.contains(s)
        | (pm_cf[COL_DESC].astype(str).str.lower().str.contains(s) if COL_DESC in pm_cf.columns else False)
    ].copy()

# Sort by thickness/size (best first), then dedupe
sort_cols, ascending = [], []
if COL_THICK in pm_cf.columns:
    sort_cols.append(COL_THICK); ascending.append(False)
if COL_WIDTH in pm_cf.columns:
    sort_cols.append(COL_WIDTH); ascending.append(False)
if COL_LENGTH in pm_cf.columns:
    sort_cols.append(COL_LENGTH); ascending.append(False)
sort_cols.append(COL_PRODUCT_ID); ascending.append(True)

pm_cf = pm_cf.sort_values(by=sort_cols, ascending=ascending, na_position="last")
pm_cf = pm_cf.drop_duplicates(subset=[COL_PRODUCT_ID], keep="first").head(5000)

def label_row(r: dict) -> str:
    pid = r.get(COL_PRODUCT_ID, "")
    desc = r.get(COL_DESC, "")
    thick = r.get(COL_THICK, None)
    w = r.get(COL_WIDTH, None)
    l = r.get(COL_LENGTH, None)
    parts = [str(pid)]
    if pd.notna(thick):
        parts.append(f'{thick:g}"')
    if pd.notna(w) and pd.notna(l):
        parts.append(f"{int(w)}x{int(l)}")
    if str(desc).strip():
        parts.append(str(desc).strip())
    return " | ".join(parts)

options = pm_cf.to_dict("records")
labels = [label_row(r) for r in options]
selected_label = st.selectbox("Pick a Product", labels) if labels else None

c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="bottom")
with c1:
    units_to_add = st.number_input("Units to add (total packs)", min_value=1, value=10, step=1)
with c2:
    add_btn = st.button("Add to Mix", disabled=(selected_label is None))
with c3:
    clear_btn = st.button("Clear Mix")

if clear_btn:
    st.session_state.mix = []

if add_btn and selected_label:
    idx = labels.index(selected_label)
    pid = options[idx][COL_PRODUCT_ID]
    prod = lookup_product(pm, pid)
    prod["units"] = int(units_to_add)

    # increment if exists
    for m in st.session_state.mix:
        if m["product_id"] == prod["product_id"]:
            m["units"] += prod["units"]
            break
    else:
        st.session_state.mix.append(prod)

# Mix table
if st.session_state.mix:
    mix_df = pd.DataFrame(st.session_state.mix)
    mix_df = mix_df[["facility_id", "commodity", "product_id", "description", "unit_height_in", "unit_weight_lbs", "units"]]
    st.dataframe(mix_df, use_container_width=True)
else:
    st.info("Add at least one product to the mix.")

# Build 15-spot plan
FLOOR_SPOTS = 15
plan = allocate_to_floor_spots(st.session_state.mix, floor_spots=FLOOR_SPOTS, max_tiers=int(max_tiers)) if st.session_state.mix else allocate_to_floor_spots([], FLOOR_SPOTS, int(max_tiers))

note = f"Commodity: {commodity_selected} | Facility: {facility_selected} | Floor spots: {FLOOR_SPOTS} | Max tiers: {max_tiers}"

# Render
top_svg = render_top_1x15_svg(car_id, plan, note)
sideA_svg = render_side_1x15_svg(car_id, plan, "Side A", car_inside_height_in)
sideB_svg = render_side_1x15_svg(car_id, plan, "Side B", car_inside_height_in)

if view_mode == "Top only":
    components.html(top_svg, height=240, scrolling=False)
elif view_mode == "Sides only":
    ca, cb = st.columns(2)
    with ca:
        components.html(sideA_svg, height=340, scrolling=False)
    with cb:
        components.html(sideB_svg, height=340, scrolling=False)
else:
    components.html(top_svg, height=240, scrolling=False)
    ca, cb = st.columns(2)
    with ca:
        components.html(sideA_svg, height=340, scrolling=False)
    with cb:
        components.html(sideB_svg, height=340, scrolling=False)
