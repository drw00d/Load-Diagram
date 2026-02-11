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
    }


# =============================
# Allocation + Colors
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


def allocate_to_spots(mix: list[dict], spots: int, tiers_capacity: int) -> list[list[dict]]:
    """
    Each spot holds up to tiers_capacity "units" stacked vertically.
    Returns spot_contents[spot_index] = list of {"product_id","qty"} in that spot.
    """
    spot_contents: list[list[dict]] = [[] for _ in range(spots)]
    spot_i = 0

    for m in mix:
        pid = m["product_id"]
        qty = int(m["units"])
        while qty > 0 and spot_i < spots:
            take = min(qty, tiers_capacity)
            spot_contents[spot_i].append({"product_id": pid, "qty": take})
            qty -= take
            spot_i += 1
        if qty > 0:
            break

    return spot_contents


# =============================
# Renderers
# =============================
def render_top_grid_svg(car_id: str, cols: int, rows: int, spot_contents: list[list[dict]], note: str) -> str:
    W, H = 1200, 420
    margin, header_h = 30, 45
    grid_x, grid_y = margin, margin + header_h
    grid_w, grid_h = W - 2 * margin, H - grid_y - margin
    cell_w, cell_h = grid_w / cols, grid_h / rows

    svg = []
    svg.append(f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">')
    svg.append(f'<rect x="{margin}" y="{margin}" width="{W-2*margin}" height="{H-2*margin}" fill="white" stroke="black" stroke-width="2"/>')
    svg.append(f'<text x="{margin+8}" y="{margin+22}" font-size="18" font-weight="600">Car: {car_id} — Top View</text>')
    svg.append(f'<text x="{margin+8}" y="{margin+40}" font-size="13">{note}</text>')

    for r in range(rows):
        for c in range(cols):
            idx = r * cols + c
            spot_num = idx + 1
            x = grid_x + c * cell_w
            y = grid_y + r * cell_h
            contents = spot_contents[idx] if idx < len(spot_contents) else []

            if len(contents) == 0:
                fill, label = "#ffffff", ""
            elif len(contents) == 1:
                fill = color_for_pid(contents[0]["product_id"])
                label = f'{contents[0]["product_id"]} x{contents[0]["qty"]}'
            else:
                fill, label = "#dddddd", "MIX"

            tooltip = ""
            if contents:
                tooltip = " | ".join([f'{x["product_id"]} x{x["qty"]}' for x in contents])

            svg.append(f'<rect x="{x}" y="{y}" width="{cell_w}" height="{cell_h}" fill="{fill}" stroke="#333" stroke-width="1"/>')
            if tooltip:
                svg.append(f'<title>Spot {spot_num}: {tooltip}</title>')

            svg.append(f'<text x="{x+6}" y="{y+16}" font-size="12" fill="#333">{spot_num}</text>')
            if label:
                svg.append(f'<text x="{x+6}" y="{y+42}" font-size="12" fill="#000">{label[:28]}</text>')

    svg.append("</svg>")
    return "\n".join(svg)


def build_stack_profile(spot_contents: list[list[dict]], product_lookup: dict) -> list[dict]:
    """
    Convert spot_contents into stack profiles including computed height inches.
    product_lookup: {product_id: {"unit_height_in":...}}
    Returns list of stacks for spots 1..N with:
      {"spot": int, "height_in": float, "label": str, "fill": str}
    """
    stacks = []
    for i, contents in enumerate(spot_contents):
        spot = i + 1
        if not contents:
            stacks.append({"spot": spot, "height_in": 0.0, "label": "", "fill": "#ffffff"})
            continue

        # single SKU per spot in current allocator
        if len(contents) == 1:
            pid = contents[0]["product_id"]
            qty = contents[0]["qty"]
            uh = float(product_lookup.get(pid, {}).get("unit_height_in", 0.0))
            h = qty * uh
            stacks.append({"spot": spot, "height_in": h, "label": f"{pid} x{qty}", "fill": color_for_pid(pid)})
        else:
            # mixed
            h = 0.0
            parts = []
            for item in contents:
                pid, qty = item["product_id"], item["qty"]
                uh = float(product_lookup.get(pid, {}).get("unit_height_in", 0.0))
                h += qty * uh
                parts.append(f"{pid}x{qty}")
            stacks.append({"spot": spot, "height_in": h, "label": "MIX", "fill": "#dddddd"})
    return stacks


def render_side_svg(car_id: str, stacks: list[dict], side_name: str, car_inside_height_in: float) -> str:
    """
    Side view: each spot is a vertical bar scaled by height_in.
    We render 15 spots across (matches top-view columns).
    """
    cols = 15
    W, H = 1200, 320
    margin = 30
    header_h = 35
    x0, y0 = margin, margin + header_h
    plot_w, plot_h = W - 2 * margin, H - y0 - margin
    cell_w = plot_w / cols

    svg = []
    svg.append(f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">')
    svg.append(f'<rect x="{margin}" y="{margin}" width="{W-2*margin}" height="{H-2*margin}" fill="white" stroke="black" stroke-width="2"/>')
    svg.append(f'<text x="{margin+8}" y="{margin+22}" font-size="16" font-weight="600">Car: {car_id} — {side_name}</text>')

    # reference line: car inside height
    svg.append(f'<line x1="{x0}" y1="{y0}" x2="{x0+plot_w}" y2="{y0}" stroke="#999" stroke-width="1"/>')
    svg.append(f'<text x="{x0+4}" y="{y0-6}" font-size="12" fill="#666">Top (ref)</text>')

    # bars from bottom
    base_y = y0 + plot_h
    svg.append(f'<line x1="{x0}" y1="{base_y}" x2="{x0+plot_w}" y2="{base_y}" stroke="#000" stroke-width="1"/>')

    # scale factor
    max_h = max(car_inside_height_in, max([s["height_in"] for s in stacks] + [0.0]))
    scale = plot_h / max_h if max_h > 0 else 1.0

    # only first 15 stacks for a single side span (we map 1..15; later we’ll map full length)
    # For now: show spots 1–15 as an example side section. Next iteration: map all 45 along length.
    view_stacks = stacks[:cols]

    for i, s in enumerate(view_stacks):
        bar_h = s["height_in"] * scale
        x = x0 + i * cell_w + 2
        y = base_y - bar_h
        w = cell_w - 4
        fill = s["fill"]

        svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{bar_h}" fill="{fill}" stroke="#333" stroke-width="1"/>')
        svg.append(f'<text x="{x+3}" y="{base_y+14}" font-size="11" fill="#333">{s["spot"]}</text>')

        if s["label"]:
            svg.append(f'<text x="{x+3}" y="{y+14}" font-size="11" fill="#000">{s["label"][:12]}</text>')

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

# Sidebar controls
with st.sidebar:
    st.header("Settings")
    car_id = st.text_input("Car ID", value="TBOX632012")
    scenario = st.selectbox("Scenario", ["RTD_SHTG", "BC", "SIDING"], index=0)
    tiers_capacity = st.slider("Tiers capacity per spot", 1, 12, 7)

    st.divider()
    st.header("View")
    view_mode = st.radio("Diagram view", ["Top + Both Sides", "Top only", "Sides only"], index=0)
    car_inside_height_in = st.number_input("Car inside height (in) (ref line)", min_value=60.0, value=110.0, step=1.0)

st.success(f"Product Master loaded: {len(pm):,} rows")

# Commodity primary filter
commodities = sorted(pm[COL_COMMODITY].dropna().astype(str).unique().tolist())
commodity_selected = st.selectbox("Commodity / Product Type (required)", ["(Select)"] + commodities)

if "mix" not in st.session_state:
    st.session_state.mix = []
if "selected_commodity" not in st.session_state:
    st.session_state.selected_commodity = commodity_selected
if "selected_facility" not in st.session_state:
    st.session_state.selected_facility = "(All facilities)"

# If commodity changes, clear mix
if commodity_selected != st.session_state.selected_commodity:
    if st.session_state.mix:
        st.warning("Commodity changed — clearing mix to prevent mixing.")
        st.session_state.mix = []
    st.session_state.selected_commodity = commodity_selected
    st.session_state.selected_facility = "(All facilities)"

if commodity_selected == "(Select)":
    st.info("Select a Commodity/Product Type to proceed.")
    st.stop()

pm_c = pm[pm[COL_COMMODITY].astype(str) == str(commodity_selected)].copy()

# Facility filtered by commodity
facilities = sorted(pm_c[COL_FACILITY].dropna().astype(str).unique().tolist()) if COL_FACILITY in pm_c.columns else []
facility_selected = st.selectbox("Facility Id (filtered by commodity)", ["(All facilities)"] + facilities)

if facility_selected != st.session_state.selected_facility:
    if st.session_state.mix:
        st.warning("Facility changed — clearing mix to prevent cross-facility mixing.")
        st.session_state.mix = []
    st.session_state.selected_facility = facility_selected

pm_cf = pm_c.copy()
if facility_selected != "(All facilities)" and COL_FACILITY in pm_cf.columns:
    pm_cf = pm_cf[pm_cf[COL_FACILITY].astype(str) == str(facility_selected)].copy()

# Search + sort + dedupe for picker
search = st.text_input("Search (by Product Id or Description)", value="")
if search.strip():
    s = search.strip().lower()
    pm_cf = pm_cf[
        pm_cf[COL_PRODUCT_ID].astype(str).str.lower().str.contains(s)
        | (pm_cf[COL_DESC].astype(str).str.lower().str.contains(s) if COL_DESC in pm_cf.columns else False)
    ].copy()

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
    units_to_add = st.number_input("Units to add", min_value=1, value=10, step=1)
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

    # increment if already exists
    for m in st.session_state.mix:
        if m["product_id"] == prod["product_id"]:
            m["units"] += prod["units"]
            break
    else:
        st.session_state.mix.append(prod)

# Mix summary
if not st.session_state.mix:
    st.info("Add at least one product to the mix.")
else:
    mix_df = pd.DataFrame(st.session_state.mix)
    mix_df = mix_df[["facility_id", "commodity", "product_id", "description", "unit_height_in", "unit_weight_lbs", "units"]]
    st.dataframe(mix_df, use_container_width=True)

# Build diagram inputs
COLS, ROWS = 15, 3
SPOTS = COLS * ROWS

spot_contents = allocate_to_spots(st.session_state.mix, spots=SPOTS, tiers_capacity=int(tiers_capacity)) if st.session_state.mix else [[] for _ in range(SPOTS)]
product_lookup = {m["product_id"]: m for m in st.session_state.mix}
stacks = build_stack_profile(spot_contents, product_lookup)

note = f"Commodity: {commodity_selected} | Facility: {facility_selected} | Spots: {SPOTS} (15x3) | Tiers cap: {tiers_capacity}"

# Layout
if view_mode == "Top only":
    components.html(render_top_grid_svg(car_id, COLS, ROWS, spot_contents, note), height=460, scrolling=False)

elif view_mode == "Sides only":
    colA, colB = st.columns(2)
    with colA:
        components.html(render_side_svg(car_id, stacks, "Side A", car_inside_height_in), height=340, scrolling=False)
    with colB:
        # For now we mirror the same stack set; next iteration we’ll map true opposite-side / length
        components.html(render_side_svg(car_id, stacks, "Side B", car_inside_height_in), height=340, scrolling=False)

else:
    top = render_top_grid_svg(car_id, COLS, ROWS, spot_contents, note)
    sideA = render_side_svg(car_id, stacks, "Side A", car_inside_height_in)
    sideB = render_side_svg(car_id, stacks, "Side B", car_inside_height_in)

    components.html(top, height=460, scrolling=False)
    cA, cB = st.columns(2)
    with cA:
        components.html(sideA, height=340, scrolling=False)
    with cB:
        components.html(sideB, height=340, scrolling=False)
