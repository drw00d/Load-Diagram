# streamlit_app.py
import streamlit as st
import pandas as pd
import streamlit.components.v1 as components

st.set_page_config(page_title="Load Diagram Optimizer", layout="wide")
st.title("Load Diagram Optimizer")

MASTER_PATH = "data/Ortec SP Product Master.xlsx"

# --- Product Master columns ---
COL_COMMODITY = "Product Type"      # primary filter
COL_FACILITY = "Facility Id"
COL_PRODUCT_ID = "Sales Product Id"
COL_DESC = "Short Descrip"          # fallback to "Descrip"
COL_ACTIVE = "Active"
COL_UNIT_H = "Unit Height (In)"
COL_UNIT_WT = "Unit Weight (lbs)"
COL_HALF_PACK = "Half Pack"

# sorting / size fields
COL_THICK = "Panel Thickness"
COL_WIDTH = "Width"
COL_LENGTH = "Length"


# =============================
# Load + Normalize Product Master
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
# Plan model: 15 floor spots, each spot holds up to max_tiers.
# Each spot has a list of layers: [{"product_id": str, "tiers": int}]
# =============================
FLOOR_SPOTS = 15


def init_plan() -> list[list[dict]]:
    return [[] for _ in range(FLOOR_SPOTS)]


def spot_tiers(spot_layers: list[dict]) -> int:
    return int(sum(int(x["tiers"]) for x in spot_layers))


def add_layers_to_plan(plan: list[list[dict]], product_id: str, tiers_to_add: int, max_tiers: int) -> None:
    """
    Fill tiers across spots left->right.
    Allows mixing SKUs inside the same spot by tiers.
    """
    remaining = int(tiers_to_add)
    if remaining <= 0:
        return

    for i in range(len(plan)):
        if remaining <= 0:
            break

        used = spot_tiers(plan[i])
        capacity = max_tiers - used
        if capacity <= 0:
            continue

        take = min(remaining, capacity)

        # if SKU already exists in this spot, increment tiers; else append new layer
        for layer in plan[i]:
            if layer["product_id"] == product_id:
                layer["tiers"] += take
                break
        else:
            plan[i].append({"product_id": product_id, "tiers": take})

        remaining -= take


# =============================
# Diagram helpers
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


def plan_payload_lbs(plan: list[list[dict]], product_lookup: dict) -> float:
    total = 0.0
    for spot in plan:
        for layer in spot:
            pid = layer["product_id"]
            tiers = int(layer["tiers"])
            wt = float(product_lookup.get(pid, {}).get("unit_weight_lbs", 0.0))
            total += tiers * wt
    return total


def render_top_svg_1x15(
    *,
    car_id: str,
    plan: list[list[dict]],
    note: str,
    airbag_between_spots: int,     # 1..14 means between spot k and k+1
    airbag_gap_in: float,          # 6..9
    unit_length_ref_in: float,     # used to scale airbag width vs spot
) -> str:
    cols = FLOOR_SPOTS
    W, H = 1200, 240
    margin = 30
    header_h = 60

    x0, y0 = margin, margin + header_h
    w = W - 2 * margin
    h = H - y0 - margin
    cell_w = w / cols

    svg = []
    svg.append(f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">')
    svg.append(f'<rect x="{margin}" y="{margin}" width="{W-2*margin}" height="{H-2*margin}" fill="white" stroke="black" stroke-width="2"/>')
    svg.append(f'<text x="{margin+8}" y="{margin+24}" font-size="18" font-weight="600">Car: {car_id} — Top View (15 floor spots)</text>')
    svg.append(f'<text x="{margin+8}" y="{margin+46}" font-size="13">{note}</text>')

    # Airbag / Door gap band (red) between k and k+1
    # scale gap inches to a fraction of a spot width using unit_length_ref_in
    frac = 0.0 if unit_length_ref_in <= 0 else (float(airbag_gap_in) / float(unit_length_ref_in))
    band_w = max(6.0, min(cell_w * 0.9, cell_w * frac))  # clamp so it's visible but not insane
    k = int(airbag_between_spots)
    k = max(1, min(14, k))
    boundary_x = x0 + k * cell_w
    band_x = boundary_x - band_w / 2

    svg.append(f'<rect x="{band_x}" y="{y0}" width="{band_w}" height="{h}" fill="none" stroke="#d00000" stroke-width="4"/>')
    svg.append(f'<text x="{band_x+4}" y="{y0-8}" font-size="12" fill="#d00000">Airbag gap {airbag_gap_in:.1f}"</text>')

    # Draw 15 boxes
    for i in range(cols):
        x = x0 + i * cell_w
        y = y0
        spot_num = i + 1
        layers = plan[i]

        fill = "#ffffff" if not layers else color_for_pid(layers[0]["product_id"])
        svg.append(f'<rect x="{x}" y="{y}" width="{cell_w}" height="{h}" fill="{fill}" opacity="0.55" stroke="#333" stroke-width="1"/>')
        svg.append(f'<text x="{x+6}" y="{y+16}" font-size="12" fill="#333">{spot_num}</text>')

        # label lines per layer (truncate)
        if layers:
            # Show up to 3 lines; tooltip shows full
            tooltip = " | ".join([f'{ly["product_id"]} x{ly["tiers"]}' for ly in layers])
            svg.append(f"<title>Spot {spot_num}: {tooltip}</title>")

            max_lines = 3
            for li, ly in enumerate(layers[:max_lines]):
                txt = f'{ly["product_id"]} x{ly["tiers"]}'
                svg.append(f'<text x="{x+6}" y="{y+42 + li*16}" font-size="12" fill="#000">{txt[:22]}</text>')

            if len(layers) > max_lines:
                svg.append(f'<text x="{x+6}" y="{y+42 + max_lines*16}" font-size="12" fill="#000">+{len(layers)-max_lines} more</text>')

    svg.append("</svg>")
    return "\n".join(svg)


def render_side_svg_1x15(
    *,
    car_id: str,
    plan: list[list[dict]],
    product_lookup: dict,
    side_name: str,
    car_inside_height_in: float,
    mirror_layers: bool,
) -> str:
    cols = FLOOR_SPOTS
    W, H = 1200, 360
    margin = 30
    header_h = 45

    x0, y0 = margin, margin + header_h
    plot_w = W - 2 * margin
    plot_h = H - y0 - margin
    cell_w = plot_w / cols
    base_y = y0 + plot_h

    # Compute tallest stack (inches)
    max_stack_in = 0.0
    for layers in plan:
        stack_in = 0.0
        for ly in layers:
            pid = ly["product_id"]
            tiers = int(ly["tiers"])
            uh = float(product_lookup.get(pid, {}).get("unit_height_in", 0.0))
            stack_in += tiers * uh
        max_stack_in = max(max_stack_in, stack_in)

    ref_h = max(float(car_inside_height_in), max_stack_in, 1.0)
    scale = plot_h / ref_h

    svg = []
    svg.append(f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">')
    svg.append(f'<rect x="{margin}" y="{margin}" width="{W-2*margin}" height="{H-2*margin}" fill="white" stroke="black" stroke-width="2"/>')
    svg.append(f'<text x="{margin+8}" y="{margin+26}" font-size="16" font-weight="600">Car: {car_id} — {side_name}</text>')

    # Base line
    svg.append(f'<line x1="{x0}" y1="{base_y}" x2="{x0+plot_w}" y2="{base_y}" stroke="#000" stroke-width="1"/>')

    # Inside height reference line
    top_ref_y = base_y - float(car_inside_height_in) * scale
    svg.append(f'<line x1="{x0}" y1="{top_ref_y}" x2="{x0+plot_w}" y2="{top_ref_y}" stroke="#999" stroke-width="1" />')
    svg.append(f'<text x="{x0+4}" y="{top_ref_y-6}" font-size="12" fill="#666">Inside height ref</text>')

    for i in range(cols):
        layers = plan[i]
        spot_num = i + 1
        x = x0 + i * cell_w + 2
        w = cell_w - 4

        # Spot number on baseline
        svg.append(f'<text x="{x+2}" y="{base_y+14}" font-size="11" fill="#333">{spot_num}</text>')

        if not layers:
            continue

        # stack segments bottom-up
        segs = layers[::-1] if mirror_layers else layers[:]  # mirror = reverse tier order visually
        y_cursor = base_y

        # Tooltip for full composition
        tooltip = " | ".join([f'{ly["product_id"]} x{ly["tiers"]}' for ly in layers])
        svg.append(f"<title>Spot {spot_num}: {tooltip}</title>")

        for ly in segs:
            pid = ly["product_id"]
            tiers = int(ly["tiers"])
            uh = float(product_lookup.get(pid, {}).get("unit_height_in", 0.0))
            seg_h = tiers * uh * scale

            y_cursor -= seg_h
            fill = color_for_pid(pid)
            svg.append(f'<rect x="{x}" y="{y_cursor}" width="{w}" height="{seg_h}" fill="{fill}" stroke="#333" stroke-width="1"/>')

            # label the segment (short)
            label = f"{pid} x{tiers}"
            svg.append(f'<text x="{x+3}" y="{y_cursor+14}" font-size="11" fill="#000">{label[:16]}</text>')

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

# Session init
if "plan" not in st.session_state:
    st.session_state.plan = init_plan()
if "selected_commodity" not in st.session_state:
    st.session_state.selected_commodity = "(Select)"
if "selected_facility" not in st.session_state:
    st.session_state.selected_facility = "(All facilities)"

# Sidebar
with st.sidebar:
    st.header("Settings")
    car_id = st.text_input("Car ID", value="TBOX632012")
    scenario = st.selectbox("Scenario", ["RTD_SHTG", "BC", "SIDING"], index=0)

    st.divider()
    st.header("Stacking / Geometry")
    max_tiers = st.slider("Max tiers per floor spot", 1, 8, 4)  # you said 3–4 typical
    car_inside_height_in = st.number_input("Inside height ref (in)", min_value=60.0, value=110.0, step=1.0)

    st.divider()
    st.header("Doorway / Airbag")
    airbag_between_spots = st.slider("Airbag location (between spot k and k+1)", 1, 14, 7)
    airbag_gap_in = st.slider("Airbag gap (in)", 6.0, 9.0, 9.0, 0.5)
    unit_length_ref_in = st.number_input("Unit length reference (in) (for drawing gap)", min_value=1.0, value=96.0, step=1.0)

    st.divider()
    st.header("Diagram")
    view_mode = st.radio("View", ["Top + Both Sides", "Top only", "Sides only"], index=0)
    mirror_side_b = st.checkbox("Mirror Side B tier order (optional)", value=False)

st.success(f"Product Master loaded: {len(pm):,} rows")

# Commodity primary filter
commodities = sorted(pm[COL_COMMODITY].dropna().astype(str).unique().tolist())
commodity_selected = st.selectbox("Commodity / Product Type (required)", ["(Select)"] + commodities)

# Clear plan on commodity change
if commodity_selected != st.session_state.selected_commodity:
    if any(st.session_state.plan):
        st.warning("Commodity changed — clearing plan to prevent OSB/PLY mixing.")
    st.session_state.plan = init_plan()
    st.session_state.selected_commodity = commodity_selected
    st.session_state.selected_facility = "(All facilities)"

if commodity_selected == "(Select)":
    st.info("Select a Commodity/Product Type to proceed.")
    st.stop()

pm_c = pm[pm[COL_COMMODITY].astype(str) == str(commodity_selected)].copy()

# Facility filtered by commodity
facilities = sorted(pm_c[COL_FACILITY].dropna().astype(str).unique().tolist()) if COL_FACILITY in pm_c.columns else []
facility_selected = st.selectbox("Facility Id (filtered by commodity)", ["(All facilities)"] + facilities)

# Clear plan on facility change
if facility_selected != st.session_state.selected_facility:
    if any(st.session_state.plan):
        st.warning("Facility changed — clearing plan to prevent cross-facility mixing.")
    st.session_state.plan = init_plan()
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

c1, c2, c3, c4 = st.columns([2, 1, 1, 1], vertical_alignment="bottom")
with c1:
    tiers_to_add = st.number_input("Tiers to add (packs)", min_value=1, value=4, step=1)
with c2:
    add_btn = st.button("Add to Plan", disabled=(selected_label is None))
with c3:
    clear_btn = st.button("Clear Plan")
with c4:
    fill_btn = st.button("Auto-fill (demo)", help="Fills remaining spots with the selected SKU for quick testing.", disabled=(selected_label is None))

if clear_btn:
    st.session_state.plan = init_plan()

# Apply add
if add_btn and selected_label:
    idx = labels.index(selected_label)
    pid = options[idx][COL_PRODUCT_ID]
    add_layers_to_plan(st.session_state.plan, str(pid), int(tiers_to_add), int(max_tiers))

# Auto-fill demo
if fill_btn and selected_label:
    idx = labels.index(selected_label)
    pid = options[idx][COL_PRODUCT_ID]
    # Fill enough tiers to load all spots to max_tiers (demo)
    add_layers_to_plan(st.session_state.plan, str(pid), int(max_tiers * FLOOR_SPOTS), int(max_tiers))

# Build lookup of product properties used in side view and payload
# (We pull from master so heights/weights are always correct even if not in current picker list)
product_ids_in_plan = sorted({ly["product_id"] for spot in st.session_state.plan for ly in spot})
product_lookup = {pid: lookup_product(pm, pid) for pid in product_ids_in_plan} if product_ids_in_plan else {}

payload = plan_payload_lbs(st.session_state.plan, product_lookup)

# Summary / spot table
st.subheader("Plan Summary")
left, right = st.columns([2, 1])

with right:
    st.metric("Payload (lbs)", f"{payload:,.0f}")
    st.caption("Payload is computed as: sum(tiers × Unit Weight) from Product Master.")

with left:
    rows = []
    for i, spot in enumerate(st.session_state.plan):
        comp = " + ".join([f'{ly["product_id"]} x{ly["tiers"]}' for ly in spot]) if spot else ""
        tiers_total = spot_tiers(spot)
        height_in = 0.0
        for ly in spot:
            pid = ly["product_id"]
            tiers = int(ly["tiers"])
            uh = float(product_lookup.get(pid, {}).get("unit_height_in", 0.0))
            height_in += tiers * uh
        rows.append({"Spot": i + 1, "Tiers": tiers_total, "Height (in)": round(height_in, 2), "Composition": comp})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=260)

# Basic gap compliance (slider enforces it, but show status like Load Xpert)
if 6.0 <= float(airbag_gap_in) <= 9.0:
    st.success(f"Airbag gap OK: {airbag_gap_in:.1f}\" (target 6–9\")")
else:
    st.error(f"Airbag gap out of range: {airbag_gap_in:.1f}\" (target 6–9\")")

# =============================
# Render diagrams
# =============================
note = (
    f"Commodity: {commodity_selected} | Facility: {facility_selected} | "
    f"Floor spots: {FLOOR_SPOTS} | Max tiers/spot: {max_tiers} | "
    f"Airbag between {airbag_between_spots}&{airbag_between_spots+1} @ {airbag_gap_in:.1f}\""
)

top_svg = render_top_svg_1x15(
    car_id=car_id,
    plan=st.session_state.plan,
    note=note,
    airbag_between_spots=int(airbag_between_spots),
    airbag_gap_in=float(airbag_gap_in),
    unit_length_ref_in=float(unit_length_ref_in),
)

side_a_svg = render_side_svg_1x15(
    car_id=car_id,
    plan=st.session_state.plan,
    product_lookup=product_lookup,
    side_name="Side A",
    car_inside_height_in=float(car_inside_height_in),
    mirror_layers=False,
)

side_b_svg = render_side_svg_1x15(
    car_id=car_id,
    plan=st.session_state.plan,
    product_lookup=product_lookup,
    side_name="Side B",
    car_inside_height_in=float(car_inside_height_in),
    mirror_layers=bool(mirror_side_b),
)

st.subheader("Diagram View")

if view_mode == "Top only":
    components.html(top_svg, height=260, scrolling=False)

elif view_mode == "Sides only":
    ca, cb = st.columns(2)
    with ca:
        components.html(side_a_svg, height=380, scrolling=False)
    with cb:
        components.html(side_b_svg, height=380, scrolling=False)

else:
    components.html(top_svg, height=260, scrolling=False)
    ca, cb = st.columns(2)
    with ca:
        components.html(side_a_svg, height=380, scrolling=False)
    with cb:
        components.html(side_b_svg, height=380, scrolling=False)
