# streamlit_app.py
import streamlit as st
import pandas as pd
import streamlit.components.v1 as components

st.set_page_config(page_title="Load Diagram Optimizer", layout="wide")
st.title("Load Diagram Optimizer")

MASTER_PATH = "data/Ortec SP Product Master.xlsx"

# --- columns in your Product Master ---
COL_COMMODITY = "Product Type"      # primary filter
COL_FACILITY = "Facility Id"
COL_PRODUCT_ID = "Sales Product Id"
COL_DESC = "Short Descrip"          # fallback to "Descrip" if needed
COL_ACTIVE = "Active"
COL_UNIT_H = "Unit Height (In)"
COL_UNIT_WT = "Unit Weight (lbs)"
COL_HALF_PACK = "Half Pack"

# sorting / size fields
COL_THICK = "Panel Thickness"
COL_WIDTH = "Width"
COL_LENGTH = "Length"


@st.cache_data(show_spinner=False)
def load_product_master(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, engine="openpyxl")
    df.columns = [c.strip() for c in df.columns]

    # Fallback for description column
    global COL_DESC
    if COL_DESC not in df.columns and "Descrip" in df.columns:
        COL_DESC = "Descrip"

    required = [COL_PRODUCT_ID, COL_UNIT_H, COL_UNIT_WT, COL_COMMODITY]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Product Master missing required columns: {missing}")

    # Clean
    df[COL_PRODUCT_ID] = df[COL_PRODUCT_ID].astype(str).str.strip()
    df[COL_COMMODITY] = df[COL_COMMODITY].astype(str).str.strip()

    if COL_FACILITY in df.columns:
        df[COL_FACILITY] = df[COL_FACILITY].astype(str).str.strip()
    if COL_DESC in df.columns:
        df[COL_DESC] = df[COL_DESC].astype(str)

    # numeric fields
    for c in [COL_UNIT_H, COL_UNIT_WT, COL_THICK, COL_WIDTH, COL_LENGTH]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # Half pack -> bool
    if COL_HALF_PACK in df.columns:
        hp = df[COL_HALF_PACK].astype(str).str.strip().str.upper()
        df[COL_HALF_PACK] = hp.isin(["Y", "YES", "TRUE", "1"])

    # Active filter
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


def allocate_to_spots(mix: list[dict], spots: int, capacity_per_spot: int) -> list[list[dict]]:
    spot_contents: list[list[dict]] = [[] for _ in range(spots)]
    remaining = [{"product_id": m["product_id"], "qty": int(m["units"])} for m in mix]

    spot_i = 0
    for item in remaining:
        pid = item["product_id"]
        qty = item["qty"]
        while qty > 0 and spot_i < spots:
            take = min(qty, capacity_per_spot)
            spot_contents[spot_i].append({"product_id": pid, "qty": take})
            qty -= take
            spot_i += 1
        if qty > 0:
            break
    return spot_contents


def color_for_pid(pid: str) -> str:
    palette = [
        "#d9ecff", "#ffe3d9", "#e6ffd9", "#f2e6ff", "#fff5cc",
        "#d9fff7", "#ffd9f1", "#e0e0ff", "#ffe0b2", "#d7ffd9",
    ]
    h = 0
    for ch in pid:
        h = (h * 31 + ord(ch)) % 10_000
    return palette[h % len(palette)]


def render_spot_grid_svg(
    *,
    car_id: str,
    cols: int,
    rows: int,
    spot_contents: list[list[dict]],
    title_note: str,
) -> str:
    W = 1200
    H = 420
    margin = 30
    header_h = 45

    grid_x = margin
    grid_y = margin + header_h
    grid_w = W - 2 * margin
    grid_h = H - grid_y - margin

    cell_w = grid_w / cols
    cell_h = grid_h / rows

    svg_parts = []
    svg_parts.append(f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">')
    svg_parts.append(f'<rect x="{margin}" y="{margin}" width="{W-2*margin}" height="{H-2*margin}" fill="white" stroke="black" stroke-width="2"/>')
    svg_parts.append(f'<text x="{margin+8}" y="{margin+22}" font-size="18" font-weight="600">Car: {car_id} — Top View (Spots 1–45)</text>')
    svg_parts.append(f'<text x="{margin+8}" y="{margin+40}" font-size="13">{title_note}</text>')

    for r in range(rows):
        for c in range(cols):
            idx = r * cols + c
            spot_num = idx + 1
            x = grid_x + c * cell_w
            y = grid_y + r * cell_h

            contents = spot_contents[idx] if idx < len(spot_contents) else []

            if len(contents) == 0:
                fill = "#ffffff"
                label = ""
            elif len(contents) == 1:
                fill = color_for_pid(contents[0]["product_id"])
                label = f'{contents[0]["product_id"]} x{contents[0]["qty"]}'
            else:
                fill = "#dddddd"
                label = "MIX"

            tooltip = ""
            if contents:
                tooltip_lines = [f'{x["product_id"]} x{x["qty"]}' for x in contents]
                tooltip = " | ".join(tooltip_lines)

            svg_parts.append(f'<rect x="{x}" y="{y}" width="{cell_w}" height="{cell_h}" fill="{fill}" stroke="#333" stroke-width="1"/>')
            if tooltip:
                svg_parts.append(f'<title>Spot {spot_num}: {tooltip}</title>')

            svg_parts.append(f'<text x="{x+6}" y="{y+16}" font-size="12" fill="#333">{spot_num}</text>')
            if label:
                if len(label) > 18:
                    line1 = label[:18]
                    line2 = label[18:]
                    svg_parts.append(f'<text x="{x+6}" y="{y+38}" font-size="12" fill="#000">{line1}</text>')
                    svg_parts.append(f'<text x="{x+6}" y="{y+54}" font-size="12" fill="#000">{line2}</text>')
                else:
                    svg_parts.append(f'<text x="{x+6}" y="{y+42}" font-size="12" fill="#000">{label}</text>')

    svg_parts.append("</svg>")
    return "\n".join(svg_parts)


# =============================
# Load Product Master
# =============================
try:
    pm = load_product_master(MASTER_PATH)
except Exception as e:
    st.error(f"Could not load Product Master at '{MASTER_PATH}'. Error: {e}")
    st.stop()

st.success(f"Product Master loaded: {len(pm):,} active rows")


# =============================
# Sidebar Inputs
# =============================
with st.sidebar:
    st.header("Car / Scenario")
    car_id = st.text_input("Car ID", value="TBOX632012")
    max_load_lbs = st.number_input("Car max load (lbs)", min_value=0, value=180000, step=1000)
    scenario = st.selectbox("Scenario", ["RTD_SHTG", "BC", "SIDING"], index=0)

    st.divider()
    st.header("Grid Capacity (placeholder)")
    tiers_capacity = st.slider("Capacity per spot (tiers)", min_value=1, max_value=12, value=7)


# =============================
# Product Mix
# =============================
st.subheader("Product Mix (multiple products per car)")

# --- Commodity is the MAIN filter ---
commodities = sorted(pm[COL_COMMODITY].dropna().astype(str).unique().tolist())
commodity_selected = st.selectbox("Commodity / Product Type (required)", ["(Select)"] + commodities)

if "mix" not in st.session_state:
    st.session_state.mix = []
if "selected_commodity" not in st.session_state:
    st.session_state.selected_commodity = commodity_selected
if "selected_facility" not in st.session_state:
    st.session_state.selected_facility = "(All facilities for this commodity)"

# If commodity changes, clear mix + reset facility
if commodity_selected != st.session_state.selected_commodity:
    if st.session_state.mix:
        st.warning("Commodity changed — clearing mix to prevent OSB/PLY mixing.")
        st.session_state.mix = []
    st.session_state.selected_commodity = commodity_selected
    st.session_state.selected_facility = "(All facilities for this commodity)"

# Stop until commodity chosen
if commodity_selected == "(Select)":
    st.info("Select a Commodity/Product Type first. Then you can pick a Facility and Products.")
    st.stop()

# Now filter master to this commodity
pm_c = pm[pm[COL_COMMODITY].astype(str) == str(commodity_selected)].copy()

# Facility dropdown is built ONLY from this commodity set
if COL_FACILITY in pm_c.columns:
    facilities = sorted(pm_c[COL_FACILITY].dropna().astype(str).unique().tolist())
    facility_selected = st.selectbox(
        "Facility Id (filtered by commodity)",
        ["(All facilities for this commodity)"] + facilities,
        index=0 if st.session_state.selected_facility not in facilities else (facilities.index(st.session_state.selected_facility) + 1),
    )
else:
    facility_selected = "(All facilities for this commodity)"
    st.warning("Column 'Facility Id' not found. Facility filter disabled.")

# If facility changes, clear mix (prevents accidental mixing)
if facility_selected != st.session_state.selected_facility:
    if st.session_state.mix:
        st.warning("Facility changed — clearing mix to prevent cross-facility mixing.")
        st.session_state.mix = []
    st.session_state.selected_facility = facility_selected

# Apply facility filter if selected
pm_cf = pm_c.copy()
if facility_selected != "(All facilities for this commodity)" and COL_FACILITY in pm_cf.columns:
    pm_cf = pm_cf[pm_cf[COL_FACILITY].astype(str) == str(facility_selected)].copy()

# Search within filtered set
search = st.text_input("Search (by Product Id or Description)", value="")
if search.strip():
    s = search.strip().lower()
    pm_cf = pm_cf[
        pm_cf[COL_PRODUCT_ID].astype(str).str.lower().str.contains(s)
        | (pm_cf[COL_DESC].astype(str).str.lower().str.contains(s) if COL_DESC in pm_cf.columns else False)
    ].copy()

# Sort by thickness / size automatically
sort_cols, ascending = [], []
if COL_THICK in pm_cf.columns:
    sort_cols.append(COL_THICK); ascending.append(False)
if COL_WIDTH in pm_cf.columns:
    sort_cols.append(COL_WIDTH); ascending.append(False)
if COL_LENGTH in pm_cf.columns:
    sort_cols.append(COL_LENGTH); ascending.append(False)
sort_cols.append(COL_PRODUCT_ID); ascending.append(True)

pm_cf = pm_cf.sort_values(by=sort_cols, ascending=ascending, na_position="last")

# De-dupe by Sales Product Id after sorting
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

    # Guardrail: enforce commodity + facility consistency
    if st.session_state.mix:
        ex_com = st.session_state.mix[0].get("commodity", "")
        ex_fac = st.session_state.mix[0].get("facility_id", "")
        if prod.get("commodity", "") != ex_com:
            st.error(f"Cannot mix commodities: mix is '{ex_com}', selected is '{prod.get('commodity','')}'.")
        elif facility_selected != "(All facilities for this commodity)" and prod.get("facility_id", "") != ex_fac:
            st.error(f"Cannot mix facilities: mix is '{ex_fac}', selected is '{prod.get('facility_id','')}'.")
        else:
            for m in st.session_state.mix:
                if m["product_id"] == prod["product_id"]:
                    m["units"] += prod["units"]
                    break
            else:
                st.session_state.mix.append(prod)
    else:
        st.session_state.mix.append(prod)

# Summary
if not st.session_state.mix:
    st.info("Add at least one product to the mix.")
else:
    mix_df = pd.DataFrame(st.session_state.mix)
    mix_df = mix_df[["facility_id", "commodity", "product_id", "description", "half_pack", "unit_height_in", "unit_weight_lbs", "units"]]
    st.dataframe(mix_df, use_container_width=True)

    total_units = int(mix_df["units"].sum())
    total_weight = float((mix_df["units"] * mix_df["unit_weight_lbs"]).sum())

    k1, k2, k3 = st.columns(3)
    k1.metric("Commodity", str(mix_df["commodity"].iloc[0]))
    k2.metric("Facility", str(mix_df["facility_id"].iloc[0]))
    k3.metric("Total Weight (lbs)", f"{total_weight:,.0f}")

# Top View
st.subheader("Top View — Spot Grid (1–45)")
COLS, ROWS = 15, 3
SPOTS = COLS * ROWS

if st.session_state.mix:
    spot_contents = allocate_to_spots(st.session_state.mix, spots=SPOTS, capacity_per_spot=int(tiers_capacity))
    title_note = f"Commodity: {commodity_selected} | Facility: {facility_selected} | Capacity/spot: {tiers_capacity}"
else:
    spot_contents = [[] for _ in range(SPOTS)]
    title_note = f"Commodity: {commodity_selected} | Facility: {facility_selected} | Add products to populate spots."

svg = render_spot_grid_svg(
    car_id=car_id,
    cols=COLS,
    rows=ROWS,
    spot_contents=spot_contents,
    title_note=title_note,
)
components.html(svg, height=460, scrolling=False)
