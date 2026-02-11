# streamlit_app.py
import math
import streamlit as st
import pandas as pd
import streamlit.components.v1 as components

st.set_page_config(page_title="Load Diagram Optimizer", layout="wide")
st.title("Load Diagram Optimizer")

MASTER_PATH = "data/Ortec SP Product Master.xlsx"

# --- expected columns in your master ---
COL_PRODUCT_ID = "Sales Product Id"
COL_DESC = "Short Descrip"          # fallback to "Descrip" if needed
COL_ACTIVE = "Active"
COL_UNIT_H = "Unit Height (In)"
COL_UNIT_WT = "Unit Weight (lbs)"
COL_HALF_PACK = "Half Pack"


# =============================
# Product Master
# =============================
@st.cache_data(show_spinner=False)
def load_product_master(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, engine="openpyxl")
    df.columns = [c.strip() for c in df.columns]

    # Fallback: if Short Descrip doesn't exist, use Descrip
    global COL_DESC
    if COL_DESC not in df.columns and "Descrip" in df.columns:
        COL_DESC = "Descrip"

    required = [COL_PRODUCT_ID, COL_UNIT_H, COL_UNIT_WT]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Product Master missing required columns: {missing}")

    # Clean
    df[COL_PRODUCT_ID] = df[COL_PRODUCT_ID].astype(str).str.strip()
    df[COL_UNIT_H] = pd.to_numeric(df[COL_UNIT_H], errors="coerce")
    df[COL_UNIT_WT] = pd.to_numeric(df[COL_UNIT_WT], errors="coerce")

    if COL_DESC in df.columns:
        df[COL_DESC] = df[COL_DESC].astype(str)

    # Normalize Half Pack -> bool
    if COL_HALF_PACK in df.columns:
        hp = df[COL_HALF_PACK].astype(str).str.strip().str.upper()
        df[COL_HALF_PACK] = hp.isin(["Y", "YES", "TRUE", "1"])

    # Filter Active if present
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
        "description": r[COL_DESC] if COL_DESC in df.columns else "",
        "unit_height_in": float(r[COL_UNIT_H]),
        "unit_weight_lbs": float(r[COL_UNIT_WT]),
        "half_pack": bool(r[COL_HALF_PACK]) if COL_HALF_PACK in df.columns else False,
    }


# =============================
# Spot Allocation (45 spots)
# =============================
def allocate_to_spots(mix: list[dict], spots: int, capacity_per_spot: int) -> list[list[dict]]:
    """
    Allocate product units into 'spots' with a simple capacity model.

    Each spot can hold up to `capacity_per_spot` "units" (placeholder for tiers).
    Returns: spot_contents[spot_index] = list of {"product_id", "qty"} placed in that spot.
    """
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
            # move to next spot if this spot is "full" (we treat single SKU fill as full)
            # if you want mixing in a spot, remove this line.
            spot_i += 1

        if qty > 0:
            # ran out of spots
            break

    return spot_contents


def color_for_pid(pid: str) -> str:
    """
    Deterministic color from Product ID, so the same SKU always gets same fill color.
    """
    palette = [
        "#d9ecff", "#ffe3d9", "#e6ffd9", "#f2e6ff", "#fff5cc",
        "#d9fff7", "#ffd9f1", "#e0e0ff", "#ffe0b2", "#d7ffd9",
    ]
    h = 0
    for ch in pid:
        h = (h * 31 + ord(ch)) % 10_000
    return palette[h % len(palette)]


# =============================
# SVG Renderer (15 x 3 = 45)
# =============================
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

    # outer frame
    svg_parts = []
    svg_parts.append(f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">')
    svg_parts.append(f'<rect x="{margin}" y="{margin}" width="{W-2*margin}" height="{H-2*margin}" fill="white" stroke="black" stroke-width="2"/>')
    svg_parts.append(f'<text x="{margin+8}" y="{margin+22}" font-size="18" font-weight="600">Car: {car_id} — Top View (Spots 1–45)</text>')
    svg_parts.append(f'<text x="{margin+8}" y="{margin+40}" font-size="13">{title_note}</text>')

    # draw cells
    for r in range(rows):
        for c in range(cols):
            idx = r * cols + c  # 0..44
            spot_num = idx + 1  # 1..45
            x = grid_x + c * cell_w
            y = grid_y + r * cell_h

            contents = spot_contents[idx] if idx < len(spot_contents) else []

            # Fill logic
            if len(contents) == 0:
                fill = "#ffffff"
                label = ""
            elif len(contents) == 1:
                fill = color_for_pid(contents[0]["product_id"])
                label = f'{contents[0]["product_id"]} x{contents[0]["qty"]}'
            else:
                fill = "#dddddd"
                label = "MIX"

            # tooltip
            tooltip = ""
            if contents:
                tooltip_lines = [f'{x["product_id"]} x{x["qty"]}' for x in contents]
                tooltip = " | ".join(tooltip_lines)

            svg_parts.append(f'<rect x="{x}" y="{y}" width="{cell_w}" height="{cell_h}" fill="{fill}" stroke="#333" stroke-width="1"/>')
            if tooltip:
                svg_parts.append(f'<title>Spot {spot_num}: {tooltip}</title>')

            # spot number
            svg_parts.append(f'<text x="{x+6}" y="{y+16}" font-size="12" fill="#333">{spot_num}</text>')

            # label (product)
            if label:
                # wrap label into 2 lines if too long
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
# App Start
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

    st.caption("For now, each spot holds up to `tiers` units. We'll later tie this to unit height and car inside height.")


# =============================
# Product Mix Builder
# =============================
st.subheader("Product Mix (multiple products per car)")

search = st.text_input("Search (by Product Id or Short Descrip)", value="")

pm_view = pm.copy()
if search.strip():
    s = search.strip().lower()
    pm_view = pm_view[
        pm_view[COL_PRODUCT_ID].astype(str).str.lower().str.contains(s)
        | (pm_view[COL_DESC].astype(str).str.lower().str.contains(s) if COL_DESC in pm_view.columns else False)
    ].copy()

# limit options for speed
pm_view = pm_view.head(5000)

def label_row(r):
    desc = r[COL_DESC] if COL_DESC in pm_view.columns else ""
    return f"{r[COL_PRODUCT_ID]} — {desc}" if desc else str(r[COL_PRODUCT_ID])

options = pm_view.to_dict("records")
labels = [label_row(r) for r in options]
selected_label = st.selectbox("Pick a Product", labels) if labels else None

c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="bottom")
with c1:
    units_to_add = st.number_input("Units to add", min_value=1, value=10, step=1)
with c2:
    add_btn = st.button("Add to Mix")
with c3:
    clear_btn = st.button("Clear Mix")

if "mix" not in st.session_state:
    st.session_state.mix = []

if clear_btn:
    st.session_state.mix = []

if add_btn and selected_label:
    idx = labels.index(selected_label)
    pid = options[idx][COL_PRODUCT_ID]
    prod = lookup_product(pm, pid)
    prod["units"] = int(units_to_add)

    # If already in mix, increment units
    found = False
    for m in st.session_state.mix:
        if m["product_id"] == prod["product_id"]:
            m["units"] += prod["units"]
            found = True
            break
    if not found:
        st.session_state.mix.append(prod)


# =============================
# Mix Summary + Checks
# =============================
if not st.session_state.mix:
    st.info("Add at least one product to the mix.")
    mix_df = None
else:
    mix_df = pd.DataFrame(st.session_state.mix)
    mix_df = mix_df[["product_id", "description", "half_pack", "unit_height_in", "unit_weight_lbs", "units"]]
    st.dataframe(mix_df, use_container_width=True)

    total_units = int(mix_df["units"].sum())
    total_weight = float((mix_df["units"] * mix_df["unit_weight_lbs"]).sum())
    half_units = int(mix_df.loc[mix_df["half_pack"] == True, "units"].sum())

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Total Units", f"{total_units:,}")
    k2.metric("Total Weight (lbs)", f"{total_weight:,.0f}")
    k3.metric("Half-Pack Units", f"{half_units:,}")
    k4.metric("Scenario", scenario)

    if scenario == "RTD_SHTG" and (half_units % 2 != 0):
        st.warning(f"RTD_SHTG preview rule: Half-Pack units must be EVEN. Half-pack units = {half_units} (ODD).")

    if max_load_lbs > 0 and total_weight > max_load_lbs:
        st.error(f"Total weight exceeds car max load: {total_weight:,.0f} > {max_load_lbs:,.0f} lbs")


# =============================
# Top View: 45 Spot Grid + Labels
# =============================
st.subheader("Top View — Spot Grid (1–45)")

COLS = 15
ROWS = 3
SPOTS = COLS * ROWS  # 45

if st.session_state.mix:
    spot_contents = allocate_to_spots(st.session_state.mix, spots=SPOTS, capacity_per_spot=int(tiers_capacity))
    title_note = f"Capacity per spot: {tiers_capacity} | Spots: {SPOTS} (15x3)"
else:
    spot_contents = [[] for _ in range(SPOTS)]
    title_note = f"Capacity per spot: {tiers_capacity} | Add products to populate spots."

svg = render_spot_grid_svg(
    car_id=car_id,
    cols=COLS,
    rows=ROWS,
    spot_contents=spot_contents,
    title_note=title_note,
)

components.html(svg, height=460, scrolling=False)
