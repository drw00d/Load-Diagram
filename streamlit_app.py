# streamlit_app.py
import streamlit as st
import pandas as pd
import streamlit.components.v1 as components

st.set_page_config(page_title="Load Diagram Optimizer", layout="wide")
st.title("Load Diagram Optimizer")

# -----------------------------
# CONFIG
# -----------------------------
MASTER_PATH = "data/Ortec SP Product Master.xlsx"


# -----------------------------
# LOAD PRODUCT MASTER
# -----------------------------
@st.cache_data(show_spinner=False)
def load_product_master(path: str) -> pd.DataFrame:
    df = pd.read_excel(path, engine="openpyxl")
    df.columns = [c.strip() for c in df.columns]
    return df


def render_top_view_svg(
    car_id: str,
    unit_height_in: float,
    cols_left: int,
    cols_right: int,
    center_gap_in: float,
    tiers: int,
) -> str:
    """
    Simple SVG top view placeholder (car rectangle + left/right blocks + center gap).
    """
    W, H = 900, 220
    margin = 20
    car_x, car_y = margin, margin + 10
    car_w, car_h = W - 2 * margin, H - 2 * margin - 10

    total_cols = max(cols_left + cols_right, 1)

    # scale 0–20 in -> ~10–140 px (keeps it visible)
    gap_px = max(int((center_gap_in / 20.0) * 140), 10)
    usable_w = car_w - gap_px
    col_w = usable_w / total_cols

    left_w = cols_left * col_w
    right_w = cols_right * col_w

    gap_x = car_x + left_w
    right_x = gap_x + gap_px

    total_units = (cols_left + cols_right) * tiers

    svg = f"""
    <svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">
      <rect x="{car_x}" y="{car_y}" width="{car_w}" height="{car_h}" fill="white" stroke="black" stroke-width="2"/>
      <text x="{car_x}" y="{car_y - 6}" font-size="14">Car: {car_id}</text>

      <!-- Left block area -->
      <rect x="{car_x}" y="{car_y}" width="{left_w}" height="{car_h}" fill="#d9ecff" stroke="#1f77b4" stroke-width="2"/>
      <text x="{car_x + 6}" y="{car_y + 20}" font-size="14">Left cols: {cols_left}</text>

      <!-- Center gap -->
      <rect x="{gap_x}" y="{car_y}" width="{gap_px}" height="{car_h}" fill="#ffffff" stroke="#000" stroke-dasharray="6,6" stroke-width="2"/>
      <text x="{gap_x + 6}" y="{car_y + 20}" font-size="14">Gap: {center_gap_in:.1f}"</text>

      <!-- Right block area -->
      <rect x="{right_x}" y="{car_y}" width="{right_w}" height="{car_h}" fill="#ffe3d9" stroke="#d62728" stroke-width="2"/>
      <text x="{right_x + 6}" y="{car_y + 20}" font-size="14">Right cols: {cols_right}</text>

      <!-- Summary -->
      <text x="{car_x + 6}" y="{car_y + car_h - 10}" font-size="14">
        Tiers: {tiers} | Unit height: {unit_height_in:.2f}" | Total units (rough): {total_units}
      </text>
    </svg>
    """
    return svg


# Try to load product master
try:
    pm = load_product_master(MASTER_PATH)
    st.success("Product Master loaded successfully.")
except Exception as e:
    st.error(f"Could not load Product Master at '{MASTER_PATH}'. Error: {e}")
    st.stop()

# -----------------------------
# SIDEBAR INPUTS
# -----------------------------
with st.sidebar:
    st.header("Car Inputs")
    car_id = st.text_input("Car ID", value="TBOX632012")

    st.header("Diagram Controls (placeholder)")
    center_gap_in = st.slider("Center gap (in)", min_value=0.0, max_value=20.0, value=8.0, step=0.5)
    cols_left = st.slider("Columns left of airbag", min_value=0, max_value=20, value=7)
    cols_right = st.slider("Columns right of airbag", min_value=0, max_value=20, value=7)
    tiers = st.slider("Tiers", min_value=1, max_value=12, value=7)

st.subheader("Product Master Preview")
st.caption("This is your Excel file loaded from GitHub. We'll use it to pull unit height and weight per Product_ID.")
st.dataframe(pm.head(20), use_container_width=True)

st.subheader("Select a Product (temporary)")
st.caption("Next we’ll switch this to a multi-product mix per car.")
# Pick first column as the selector if we don't know your schema yet
id_col_guess = pm.columns[0]
product_id = st.selectbox(f"Pick Product ID (using column: {id_col_guess})", pm[id_col_guess].astype(str).unique())

# Try to auto-detect height/weight columns by fuzzy matches
cols_lower = {c.lower(): c for c in pm.columns}
height_candidates = [c for c in pm.columns if "height" in c.lower()]
weight_candidates = [c for c in pm.columns if "weight" in c.lower()]

height_col = height_candidates[0] if height_candidates else None
weight_col = weight_candidates[0] if weight_candidates else None

row = pm.loc[pm[id_col_guess].astype(str) == str(product_id)]
unit_height_in = None
unit_weight_lbs = None

if not row.empty:
    r0 = row.iloc[0]
    if height_col is not None:
        unit_height_in = r0[height_col]
    if weight_col is not None:
        unit_weight_lbs = r0[weight_col]

st.markdown("### Selected Product Details (auto-detected)")
st.write(
    {
        "Product_ID": str(product_id),
        "Height column detected": height_col,
        "Weight column detected": weight_col,
        "Unit_Height_in": unit_height_in,
        "Unit_Weight_lbs": unit_weight_lbs,
    }
)

run = st.button("Run Test")

if run:
    st.success(f"Car {car_id} processed.")

    st.subheader("Top View (placeholder)")
    svg = render_top_view_svg(
        car_id=car_id,
        unit_height_in=float(unit_height_in) if unit_height_in is not None else 0.0,
        cols_left=int(cols_left),
        cols_right=int(cols_right),
        center_gap_in=float(center_gap_in),
        tiers=int(tiers),
    )
    # Render SVG safely
    components.html(svg, height=260, scrolling=False)
