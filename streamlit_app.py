# streamlit_app.py
import streamlit as st
import pandas as pd
import streamlit.components.v1 as components

st.set_page_config(page_title="Load Diagram Optimizer", layout="wide")
st.title("Load Diagram Optimizer")

MASTER_PATH = "data/Ortec SP Product Master.xlsx"

# --- expected columns in your master ---
COL_PRODUCT_ID = "Sales Product Id"
COL_DESC = "Short Descrip"          # or "Descrip"
COL_ACTIVE = "Active"
COL_UNIT_H = "Unit Height (In)"
COL_UNIT_WT = "Unit Weight (lbs)"
COL_HALF_PACK = "Half Pack"


@st.cache_data(show_spinner=False)
def load_product_master(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, engine="openpyxl")
    df.columns = [c.strip() for c in df.columns]

    # Keep only columns we care about (but don’t break if missing)
    keep = [c for c in [COL_PRODUCT_ID, COL_DESC, COL_ACTIVE, COL_UNIT_H, COL_UNIT_WT, COL_HALF_PACK] if c in df.columns]
    if keep:
        df = df[keep].copy()

    # Clean types
    df[COL_PRODUCT_ID] = df[COL_PRODUCT_ID].astype(str).str.strip()
    if COL_DESC in df.columns:
        df[COL_DESC] = df[COL_DESC].astype(str)

    if COL_UNIT_H in df.columns:
        df[COL_UNIT_H] = pd.to_numeric(df[COL_UNIT_H], errors="coerce")
    if COL_UNIT_WT in df.columns:
        df[COL_UNIT_WT] = pd.to_numeric(df[COL_UNIT_WT], errors="coerce")

    # Normalize Half Pack to bool-ish
    if COL_HALF_PACK in df.columns:
        hp = df[COL_HALF_PACK].astype(str).str.strip().str.upper()
        df[COL_HALF_PACK] = hp.isin(["Y", "YES", "TRUE", "1"])

    # Filter active if possible
    if COL_ACTIVE in df.columns:
        act = df[COL_ACTIVE].astype(str).str.strip().str.upper()
        df = df[act.isin(["Y", "YES", "TRUE", "1", "ACTIVE"])].copy()

    # Drop rows missing critical fields
    missing_cols = [c for c in [COL_PRODUCT_ID, COL_UNIT_H, COL_UNIT_WT] if c not in df.columns]
    if missing_cols:
        raise ValueError(f"Product Master missing required columns: {missing_cols}")

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


def render_top_view_svg(car_id: str, cols_left: int, cols_right: int, center_gap_in: float, notes: str) -> str:
    """
    Simple SVG placeholder: car rectangle + left/right blocks + center gap.
    """
    W, H = 1000, 260
    margin = 25
    car_x, car_y = margin, margin + 10
    car_w, car_h = W - 2 * margin, H - 2 * margin - 10

    total_cols = max(cols_left + cols_right, 1)
    gap_px = max(int((center_gap_in / 20.0) * 160), 10)
    usable_w = car_w - gap_px
    col_w = usable_w / total_cols

    left_w = cols_left * col_w
    right_w = cols_right * col_w
    gap_x = car_x + left_w
    right_x = gap_x + gap_px

    svg = f"""
    <svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">
      <rect x="{car_x}" y="{car_y}" width="{car_w}" height="{car_h}" fill="white" stroke="black" stroke-width="2"/>
      <text x="{car_x}" y="{car_y - 6}" font-size="16" font-weight="600">Car: {car_id}</text>

      <rect x="{car_x}" y="{car_y}" width="{left_w}" height="{car_h}" fill="#d9ecff" stroke="#1f77b4" stroke-width="2"/>
      <text x="{car_x + 8}" y="{car_y + 26}" font-size="14">Left cols: {cols_left}</text>

      <rect x="{gap_x}" y="{car_y}" width="{gap_px}" height="{car_h}" fill="#ffffff" stroke="#000" stroke-dasharray="7,7" stroke-width="2"/>
      <text x="{gap_x + 8}" y="{car_y + 26}" font-size="14">Gap: {center_gap_in:.1f}"</text>

      <rect x="{right_x}" y="{car_y}" width="{right_w}" height="{car_h}" fill="#ffe3d9" stroke="#d62728" stroke-width="2"/>
      <text x="{right_x + 8}" y="{car_y + 26}" font-size="14">Right cols: {cols_right}</text>

      <text x="{car_x + 8}" y="{car_y + car_h - 10}" font-size="13">{notes}</text>
    </svg>
    """
    return svg


# -----------------------------
# Load Product Master
# -----------------------------
try:
    pm = load_product_master(MASTER_PATH)
except Exception as e:
    st.error(f"Could not load Product Master at '{MASTER_PATH}'. Error: {e}")
    st.stop()

st.success(f"Product Master loaded: {len(pm):,} active rows")

# -----------------------------
# Sidebar Inputs
# -----------------------------
with st.sidebar:
    st.header("Car / Scenario")

    car_id = st.text_input("Car ID", value="TBOX632012")
    max_load_lbs = st.number_input("Car max load (lbs)", min_value=0, value=180000, step=1000)

    scenario = st.selectbox("Scenario", ["RTD_SHTG", "BC", "SIDING"], index=0)

    needs_airbag = st.checkbox("Needs airbag", value=True)
    airbag_height_in = st.number_input("Airbag height (in)", min_value=0.0, value=60.0, step=1.0)

    st.divider()
    st.header("Diagram controls (placeholder)")
    center_gap_in = st.slider("Center gap (in)", min_value=0.0, max_value=20.0, value=8.0, step=0.5)
    cols_left = st.slider("Columns left of airbag", min_value=0, max_value=25, value=7)
    cols_right = st.slider("Columns right of airbag", min_value=0, max_value=25, value=7)

# -----------------------------
# Product Mix Builder
# -----------------------------
st.subheader("Product Mix (multiple products per car)")

# searchable selectbox via text filter + select
left, right = st.columns([2, 1], vertical_alignment="bottom")
with left:
    search = st.text_input("Search (by Product Id or Short Descrip)", value="")
with right:
    show_preview = st.checkbox("Show Product Master preview", value=False)

pm_view = pm.copy()
if search.strip():
    s = search.strip().lower()
    pm_view = pm_view[
        pm_view[COL_PRODUCT_ID].astype(str).str.lower().str.contains(s)
        | (pm_view[COL_DESC].astype(str).str.lower().str.contains(s) if COL_DESC in pm_view.columns else False)
    ].copy()

# build display label
def label_row(r):
    desc = r[COL_DESC] if COL_DESC in pm_view.columns else ""
    return f"{r[COL_PRODUCT_ID]} — {desc}" if desc else str(r[COL_PRODUCT_ID])

options = pm_view.head(5000).to_dict("records")  # keep UI fast
labels = [label_row(r) for r in options]
selected_label = st.selectbox("Pick a Product", labels) if labels else None

add_col1, add_col2, add_col3 = st.columns([2, 1, 1], vertical_alignment="bottom")
with add_col1:
    units_to_add = st.number_input("Units to add", min_value=1, value=10, step=1)
with add_col2:
    add_btn = st.button("Add to Mix")
with add_col3:
    clear_btn = st.button("Clear Mix")

if "mix" not in st.session_state:
    st.session_state.mix = []  # list of dicts

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

# show mix
if not st.session_state.mix:
    st.info("Add at least one product to the mix.")
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

    # Simple parity rule check (preview)
    if scenario == "RTD_SHTG" and (half_units % 2 != 0):
        st.warning(f"RTD_SHTG preview rule: Half-Pack units must be EVEN. Current half-pack units = {half_units} (ODD).")

    if max_load_lbs > 0 and total_weight > max_load_lbs:
        st.error(f"Total weight exceeds car max load: {total_weight:,.0f} > {max_load_lbs:,.0f} lbs")

# optional preview
if show_preview:
    st.subheader("Product Master Preview")
    st.dataframe(pm.head(50), use_container_width=True)

# -----------------------------
# Diagram (placeholder)
# -----------------------------
st.subheader("Top View (placeholder)")

notes = ""
if st.session_state.mix:
    notes = f"Mix items: {len(st.session_state.mix)} | Total units: {total_units:,} | Total weight: {total_weight:,.0f} lbs"
else:
    notes = "Add products to the mix to calculate totals."

svg = render_top_view_svg(
    car_id=car_id,
    cols_left=int(cols_left),
    cols_right=int(cols_right),
    center_gap_in=float(center_gap_in),
    notes=notes,
)
components.html(svg, height=300, scrolling=False)
