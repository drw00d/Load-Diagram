import streamlit as st

st.set_page_config(page_title="Load Diagram Optimizer", layout="wide")
st.title("Load Diagram Optimizer")

st.write("Now rendering a simple diagram (placeholder) so you can SEE output.")

car_id = st.text_input("Car ID", value="TBOX632012")
unit_height = st.number_input("Unit Height (in)", value=20.25)

# simple controls to visualize something
center_gap = st.slider("Center gap (in)", min_value=0, max_value=20, value=8)
cols_left = st.slider("Columns left of airbag", min_value=0, max_value=20, value=7)
cols_right = st.slider("Columns right of airbag", min_value=0, max_value=20, value=7)
tiers = st.slider("Tiers", min_value=1, max_value=10, value=7)

def render_top_view_svg(cols_left, cols_right, center_gap):
    # Simple SVG top view: car rectangle, center gap, blocks left and right
    W, H = 900, 220
    margin = 20
    car_x, car_y = margin, margin
    car_w, car_h = W - 2 * margin, H - 2 * margin

    # map columns to pixels
    total_cols = max(cols_left + cols_right, 1)
    gap_px = max(int((center_gap / 20) * 120), 10)  # scale 0-20 in -> 10-120 px
    usable_w = car_w - gap_px
    col_w = usable_w / total_cols

    left_w = cols_left * col_w
    right_w = cols_right * col_w

    gap_x = car_x + left_w
    right_x = gap_x + gap_px

    svg = f"""
    <svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">
      <rect x="{car_x}" y="{car_y}" width="{car_w}" height="{car_h}" fill="white" stroke="black" stroke-width="2"/>
      <text x="{car_x}" y="{car_y - 2}" font-size="14">Car: {car_id}</text>

      <!-- Left block area -->
      <rect x="{car_x}" y="{car_y}" width="{left_w}" height="{car_h}" fill="#d9ecff" stroke="#1f77b4" stroke-width="2"/>
      <text x="{car_x + 6}" y="{car_y + 20}" font-size="14">Left cols: {cols_left}</text>

      <!-- Center gap -->
      <rect x="{gap_x}" y="{car_y}" width="{gap_px}" height="{car_h}" fill="#ffffff" stroke="#000" stroke-dasharray="6,6" stroke-width="2"/>
      <text x="{gap_x + 6}" y="{car_y + 20}" font-size="14">Gap: {center_gap}"</text>

      <!-- Right block area -->
      <rect x="{right_x}" y="{car_y}" width="{right_w}" height="{car_h}" fill="#ffe3d9" stroke="#d62728" stroke-width="2"/>
      <text x="{right_x + 6}" y="{car_y + 20}" font-size="14">Right cols: {cols_right}</text>

      <!-- Summary -->
      <text x="{car_x + 6}" y="{car_y + car_h - 10}" font-size="14">
        Tiers: {tiers} | Unit height: {unit_height}" | Total units (rough): {(cols_left+cols_right)*tiers}
      </text>
    </svg>
    """
    return svg

if st.button("Run Test"):
    st.success(f"Car {car_id} processed.")

    st.subheader("Top View (placeholder)")
    svg = render_top_view_svg(cols_left, cols_right, center_gap)
    st.markdown(svg, unsafe_allow_html=True)

