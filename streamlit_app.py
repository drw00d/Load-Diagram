# streamlit_app.py
import streamlit as st
import pandas as pd
import streamlit.components.v1 as components

st.set_page_config(page_title="Load Diagram Optimizer", layout="wide")
st.title("Load Diagram Optimizer")

MASTER_PATH = "data/Ortec SP Product Master.xlsx"

# --- Product Master columns ---
COL_COMMODITY = "Product Type"
COL_FACILITY = "Facility Id"
COL_PRODUCT_ID = "Sales Product Id"
COL_DESC = "Short Descrip"
COL_ACTIVE = "Active"
COL_UNIT_H = "Unit Height (In)"
COL_UNIT_WT = "Unit Weight (lbs)"
COL_EDGE = "Edge Type"            # <-- required for Machine Edge rule

COL_THICK = "Panel Thickness"
COL_WIDTH = "Width"
COL_LENGTH = "Length"

# --- Car family rules (your standard) ---
FLOOR_SPOTS = 15
DOOR_START_SPOT = 6
DOOR_END_SPOT = 9
DOORFRAME_SPOTS_NO_MACHINE_EDGE = {6, 9}
DOORWAY_SPOTS_ALLOW_MACHINE_EDGE = {7, 8}
AIRBAG_ALLOWED_GAPS = [(6, 7), (7, 8), (8, 9)]


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

    required = [COL_PRODUCT_ID, COL_UNIT_H, COL_UNIT_WT, COL_COMMODITY, COL_EDGE]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Product Master missing required columns: {missing}")

    # basic cleanup
    df[COL_PRODUCT_ID] = df[COL_PRODUCT_ID].astype(str).str.strip()
    df[COL_COMMODITY] = df[COL_COMMODITY].astype(str).str.strip()
    df[COL_EDGE] = df[COL_EDGE].astype(str).str.strip()

    if COL_FACILITY in df.columns:
        df[COL_FACILITY] = df[COL_FACILITY].astype(str).str.strip()
    if COL_DESC in df.columns:
        df[COL_DESC] = df[COL_DESC].astype(str)

    for c in [COL_UNIT_H, COL_UNIT_WT, COL_THICK, COL_WIDTH, COL_LENGTH]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

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
        "commodity": r[COL_COMMODITY],
        "facility_id": r[COL_FACILITY] if COL_FACILITY in df.columns else "",
        "description": r[COL_DESC] if COL_DESC in df.columns else "",
        "edge_type": str(r[COL_EDGE]).strip(),
        "unit_height_in": float(r[COL_UNIT_H]),
        "unit_weight_lbs": float(r[COL_UNIT_WT]),
    }


# =============================
# Stagger pattern (Load Xpert style)
# Spot ownership alternates by side.
# =============================
def spot_side(spot_num: int) -> str:
    # Spot 1 on Side A, Spot 2 on Side B, Spot 3 on Side A, ...
    return "A" if (spot_num % 2 == 1) else "B"


# =============================
# Plan model: per spot (1..15) store layers [{product_id, tiers}]
# =============================
def init_plan() -> list[list[dict]]:
    return [[] for _ in range(FLOOR_SPOTS)]


def spot_tiers(spot_layers: list[dict]) -> int:
    return int(sum(int(x["tiers"]) for x in spot_layers))


def is_machine_edge(edge_type: str) -> bool:
    et = (edge_type or "").strip().lower()
    return "machine" in et  # "Machine Edge" -> True


def can_place_layer_in_spot(product_lookup: dict, pid: str, spot_num: int) -> tuple[bool, str]:
    """
    Rule: Machine Edge NOT in spot 6 or 9 (doorframe).
          Machine Edge allowed in 7 or 8.
    """
    edge = str(product_lookup.get(pid, {}).get("edge_type", "")).strip()
    if is_machine_edge(edge) and spot_num in DOORFRAME_SPOTS_NO_MACHINE_EDGE:
        return False, f"Machine Edge not allowed in Spot {spot_num} (doorframe). Use Spot 7 or 8 if in doorway."
    return True, ""


def add_layers_to_plan_staggered(
    plan: list[list[dict]],
    product_lookup: dict,
    pid: str,
    tiers_to_add: int,
    max_tiers_per_spot: int,
    preferred_side: str = "A",
) -> list[str]:
    """
    Fill tiers across spots with staggered pattern preference.
    - preferred_side chooses which side we try to fill first (A or B).
    - Allows mixing SKUs inside a spot by tiers.
    - Enforces Machine Edge rule for spots 6 and 9.
    Returns list of warnings/errors encountered.
    """
    remaining = int(tiers_to_add)
    messages: list[str] = []
    if remaining <= 0:
        return messages

    # spot order by side preference, but still left->right within that side
    spots_by_side = [s for s in range(1, FLOOR_SPOTS + 1) if spot_side(s) == preferred_side] + \
                    [s for s in range(1, FLOOR_SPOTS + 1) if spot_side(s) != preferred_side]

    for spot_num in spots_by_side:
        if remaining <= 0:
            break

        spot_idx = spot_num - 1
        used = spot_tiers(plan[spot_idx])
        cap = max_tiers_per_spot - used
        if cap <= 0:
            continue

        ok, reason = can_place_layer_in_spot(product_lookup, pid, spot_num)
        if not ok:
            continue  # skip this spot entirely

        take = min(remaining, cap)

        # update layer
        for layer in plan[spot_idx]:
            if layer["product_id"] == pid:
                layer["tiers"] += take
                break
        else:
            plan[spot_idx].append({"product_id": pid, "tiers": take})

        remaining -= take

    if remaining > 0:
        messages.append(f"Not enough capacity: {remaining} tiers could not be placed (max tiers/spot reached).")

    # If placement was blocked due to Machine Edge doorframe rule, warn if nothing placed
    placed_any = any(any(l["product_id"] == pid for l in spot) for spot in plan)
    if not placed_any:
        edge = product_lookup.get(pid, {}).get("edge_type", "")
        if is_machine_edge(edge):
            messages.append("Nothing placed. This SKU is Machine Edge and may be blocked by doorframe Spots 6/9 if those are the only remaining openings.")

    return messages


# =============================
# Rendering
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


def doorway_bounds_px(x0: float, cell_w: float) -> tuple[float, float]:
    left = x0 + (DOOR_START_SPOT - 1) * cell_w
    right = x0 + DOOR_END_SPOT * cell_w
    return left, right


def airbag_center_px(x0: float, cell_w: float, gap_choice: tuple[int, int]) -> float:
    a, b = gap_choice
    return x0 + a * cell_w


def render_top_loadxpert_style(
    *,
    car_id: str,
    plan: list[list[dict]],
    note: str,
    airbag_gap_in: float,
    airbag_gap_choice: tuple[int, int],
    unit_length_ref_in: float,
) -> str:
    """
    Top view like Load Xpert:
    - 15 spots shown in a single lane
    - Spots alternate left/right offset to show stagger pattern visually
    - Doorway zone hatched/red outline across spots 6-9
    - Airbag gap drawn as red band between allowed spots
    """
    cols = FLOOR_SPOTS
    W, H = 1200, 280
    margin = 30
    header_h = 70

    x0, y0 = margin, margin + header_h
    w = W - 2 * margin
    lane_h = H - y0 - margin
    cell_w = w / cols

    # stagger offset
    lane_y_center = y0 + lane_h / 2
    box_h = lane_h * 0.65
    offset = lane_h * 0.12  # offset up/down

    # airbag band width
    frac = 0.0 if unit_length_ref_in <= 0 else (float(airbag_gap_in) / float(unit_length_ref_in))
    band_w = max(8.0, min(cell_w * 0.9, cell_w * frac))

    svg = []
    svg.append(f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">')
    svg.append("""
    <defs>
      <pattern id="doorHatch" patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="8" stroke="#c00000" stroke-width="2" opacity="0.35"/>
      </pattern>
    </defs>
    """)
    svg.append(f'<rect x="{margin}" y="{margin}" width="{W-2*margin}" height="{H-2*margin}" fill="white" stroke="black" stroke-width="2"/>')
    svg.append(f'<text x="{margin+8}" y="{margin+26}" font-size="18" font-weight="600">Car: {car_id} — Top View (staggered)</text>')
    svg.append(f'<text x="{margin+8}" y="{margin+50}" font-size="13">{note}</text>')

    # doorway overlay region (full lane height)
    door_left, door_right = doorway_bounds_px(x0, cell_w)
    svg.append(f'<rect x="{door_left}" y="{y0}" width="{door_right-door_left}" height="{lane_h}" fill="url(#doorHatch)" stroke="#c00000" stroke-width="3" opacity="0.9"/>')
    svg.append(f'<text x="{door_left+6}" y="{y0-10}" font-size="12" fill="#c00000">Doorway zone (Spots {DOOR_START_SPOT}–{DOOR_END_SPOT})</text>')

    # airbag band
    center_x = airbag_center_px(x0, cell_w, airbag_gap_choice)
    band_x = center_x - band_w / 2
    svg.append(f'<rect x="{band_x}" y="{y0}" width="{band_w}" height="{lane_h}" fill="none" stroke="#d00000" stroke-width="5"/>')
    svg.append(f'<text x="{band_x+4}" y="{y0+lane_h+16}" font-size="12" fill="#d00000">Airbag {airbag_gap_in:.1f}" between {airbag_gap_choice[0]}–{airbag_gap_choice[1]}</text>')

    # draw staggered boxes
    for i in range(cols):
        spot_num = i + 1
        layers = plan[i]
        x = x0 + i * cell_w + cell_w * 0.08
        bw = cell_w * 0.84

        side = spot_side(spot_num)
        y = lane_y_center - (box_h / 2) - (offset if side == "A" else -offset)

        fill = "#ffffff" if not layers else color_for_pid(layers[0]["product_id"])
        svg.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{box_h}" fill="{fill}" opacity="0.75" stroke="#333" stroke-width="1"/>')

        # spot label
        svg.append(f'<text x="{x+6}" y="{y+16}" font-size="12" fill="#333">{spot_num}{side}</text>')

        # content label lines
        if layers:
            tooltip = " | ".join([f'{ly["product_id"]} x{ly["tiers"]}' for ly in layers])
            svg.append(f"<title>Spot {spot_num}: {tooltip}</title>")

            for li, ly in enumerate(layers[:2]):
                txt = f'{ly["product_id"]} x{ly["tiers"]}'
                svg.append(f'<text x="{x+6}" y="{y+44 + li*16}" font-size="12" fill="#000">{txt[:20]}</text>')
            if len(layers) > 2:
                svg.append(f'<text x="{x+6}" y="{y+44 + 2*16}" font-size="12" fill="#000">+{len(layers)-2} more</text>')

        # doorframe "NO Machine Edge" callout boxes on spots 6 and 9
        if spot_num in DOORFRAME_SPOTS_NO_MACHINE_EDGE:
            svg.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{box_h}" fill="none" stroke="#7a0000" stroke-width="3"/>')
            svg.append(f'<text x="{x+6}" y="{y+box_h-8}" font-size="11" fill="#7a0000">NO Machine Edge</text>')

    svg.append("</svg>")
    return "\n".join(svg)


def render_side_view(
    *,
    car_id: str,
    plan: list[list[dict]],
    product_lookup: dict,
    side_name: str,
    car_inside_height_in: float,
    side_filter: str,     # "A" or "B"
    mirror_layers: bool,
) -> str:
    """
    Side view shows all 15 columns, but only draws stacks for spots belonging to that side.
    This makes Side A and Side B “match” in the staggered sense.
    """
    cols = FLOOR_SPOTS
    W, H = 1200, 400
    margin = 30
    header_h = 45

    x0, y0 = margin, margin + header_h
    plot_w = W - 2 * margin
    plot_h = H - y0 - margin
    cell_w = plot_w / cols
    base_y = y0 + plot_h

    # max height across all stacks
    max_stack_in = 0.0
    for spot_num in range(1, FLOOR_SPOTS + 1):
        layers = plan[spot_num - 1]
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

    # base and ref lines
    svg.append(f'<line x1="{x0}" y1="{base_y}" x2="{x0+plot_w}" y2="{base_y}" stroke="#000" stroke-width="1"/>')
    top_ref_y = base_y - float(car_inside_height_in) * scale
    svg.append(f'<line x1="{x0}" y1="{top_ref_y}" x2="{x0+plot_w}" y2="{top_ref_y}" stroke="#999" stroke-width="1" />')
    svg.append(f'<text x="{x0+4}" y="{top_ref_y-6}" font-size="12" fill="#666">Inside height ref</text>')

    # doorway bracket
    door_left = x0 + (DOOR_START_SPOT - 1) * cell_w
    door_right = x0 + DOOR_END_SPOT * cell_w
    svg.append(f'<rect x="{door_left}" y="{y0}" width="{door_right-door_left}" height="{plot_h}" fill="none" stroke="#c00000" stroke-width="2" opacity="0.5"/>')

    for i in range(cols):
        spot_num = i + 1
        x = x0 + i * cell_w + 2
        w = cell_w - 4

        # spot label
        svg.append(f'<text x="{x+2}" y="{base_y+14}" font-size="11" fill="#333">{spot_num}</text>')

        # only render stacks belonging to the side
        if spot_side(spot_num) != side_filter:
            continue

        layers = plan[i]
        if not layers:
            continue

        segs = layers[::-1] if mirror_layers else layers[:]
        y_cursor = base_y

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

if "plan" not in st.session_state:
    st.session_state.plan = init_plan()
if "selected_commodity" not in st.session_state:
    st.session_state.selected_commodity = "(Select)"
if "selected_facility" not in st.session_state:
    st.session_state.selected_facility = "(All facilities)"

with st.sidebar:
    st.header("Settings")
    car_id = st.text_input("Car ID", value="TBOX632012")
    max_tiers = st.slider("Max tiers per spot", 1, 8, 4)
    car_inside_height_in = st.number_input("Inside height ref (in)", min_value=60.0, value=110.0, step=1.0)

    st.divider()
    st.header("Doorway / Airbag")
    gap_labels = [f"{a}–{b}" for a, b in AIRBAG_ALLOWED_GAPS]
    default_idx = gap_labels.index("7–8") if "7–8" in gap_labels else 1
    gap_choice_label = st.selectbox("Airbag location", gap_labels, index=default_idx)
    airbag_gap_in = st.slider("Airbag gap (in)", 6.0, 9.0, 9.0, 0.5)
    unit_length_ref_in = st.number_input("Unit length ref (in) for gap drawing", min_value=1.0, value=96.0, step=1.0)

    st.divider()
    st.header("Placement behavior")
    preferred_side = st.selectbox("Fill preference (stagger)", ["A", "B"], index=0)
    mirror_side_b = st.checkbox("Mirror Side B tier order (optional)", value=False)

    st.divider()
    view_mode = st.radio("View", ["Top + Both Sides", "Top only", "Sides only"], index=0)

st.success(f"Product Master loaded: {len(pm):,} rows")

# Commodity primary filter
commodities = sorted(pm[COL_COMMODITY].dropna().astype(str).unique().tolist())
commodity_selected = st.selectbox("Commodity / Product Type (required)", ["(Select)"] + commodities)

if commodity_selected != st.session_state.selected_commodity:
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

if facility_selected != st.session_state.selected_facility:
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
    edge = r.get(COL_EDGE, "")
    thick = r.get(COL_THICK, None)
    parts = [str(pid)]
    if pd.notna(thick):
        parts.append(f'{thick:g}"')
    if str(edge).strip():
        parts.append(str(edge).strip())
    if str(desc).strip():
        parts.append(str(desc).strip())
    return " | ".join(parts)

options = pm_cf.to_dict("records")
labels = [label_row(r) for r in options]
selected_label = st.selectbox("Pick a Product", labels) if labels else None

c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="bottom")
with c1:
    tiers_to_add = st.number_input("Tiers to add (packs)", min_value=1, value=4, step=1)
with c2:
    add_btn = st.button("Add (staggered)", disabled=(selected_label is None))
with c3:
    clear_btn = st.button("Clear Plan")

if clear_btn:
    st.session_state.plan = init_plan()

# Build lookup from master for any SKU we touch
# (we lookup lazily when adding)
airbag_gap_choice = AIRBAG_ALLOWED_GAPS[[f"{a}–{b}" for a, b in AIRBAG_ALLOWED_GAPS].index(gap_choice_label)]

messages = []
if add_btn and selected_label:
    idx = labels.index(selected_label)
    pid = str(options[idx][COL_PRODUCT_ID])

    prod = lookup_product(pm, pid)
    product_lookup_for_rules = {pid: prod}  # single lookup needed for edge rule

    # Enforce "Machine Edge not in 6 or 9"
    add_msgs = add_layers_to_plan_staggered(
        st.session_state.plan,
        product_lookup_for_rules,
        pid,
        int(tiers_to_add),
        int(max_tiers),
        preferred_side=str(preferred_side),
    )
    messages.extend(add_msgs)

# Gather all product ids in plan for rendering/payload
product_ids_in_plan = sorted({ly["product_id"] for spot in st.session_state.plan for ly in spot})
product_lookup = {pid: lookup_product(pm, pid) for pid in product_ids_in_plan} if product_ids_in_plan else {}

# Show any warnings
for m in messages:
    st.warning(m)

# Validate Machine Edge rule across existing plan
violations = []
for spot_num in range(1, FLOOR_SPOTS + 1):
    if spot_num in DOORFRAME_SPOTS_NO_MACHINE_EDGE:
        for ly in st.session_state.plan[spot_num - 1]:
            pid = ly["product_id"]
            edge = product_lookup.get(pid, {}).get("edge_type", "")
            if is_machine_edge(edge):
                violations.append(f"Spot {spot_num} has Machine Edge SKU {pid} (not allowed).")

if violations:
    for v in violations:
        st.error(v)

# Payload (stagger itself doesn’t change total, but matches AAR intent)
payload = sum(int(ly["tiers"]) * float(product_lookup.get(ly["product_id"], {}).get("unit_weight_lbs", 0.0))
              for spot in st.session_state.plan for ly in spot)

st.subheader("Summary")
st.metric("Payload (lbs)", f"{payload:,.0f}")
st.caption(f"Doorway zone: Spots {DOOR_START_SPOT}–{DOOR_END_SPOT}. Machine Edge prohibited in Spots 6 & 9 (doorframe).")

# Plan table
rows = []
for i, spot in enumerate(st.session_state.plan):
    spot_num = i + 1
    side = spot_side(spot_num)
    comp = " + ".join([f'{ly["product_id"]} x{ly["tiers"]}' for ly in spot]) if spot else ""
    rows.append({"Spot": spot_num, "Side": side, "Tiers": spot_tiers(spot), "Composition": comp})
st.dataframe(pd.DataFrame(rows), use_container_width=True, height=260)

# Render diagrams
note = (
    f"Commodity: {commodity_selected} | Facility: {facility_selected} | "
    f"Stagger: Spot1=A, Spot2=B... | Doorway: {DOOR_START_SPOT}–{DOOR_END_SPOT}"
)

top_svg = render_top_loadxpert_style(
    car_id=car_id,
    plan=st.session_state.plan,
    note=note,
    airbag_gap_in=float(airbag_gap_in),
    airbag_gap_choice=airbag_gap_choice,
    unit_length_ref_in=float(unit_length_ref_in),
)

side_a = render_side_view(
    car_id=car_id,
    plan=st.session_state.plan,
    product_lookup=product_lookup,
    side_name="Side A (staggered spots only)",
    car_inside_height_in=float(car_inside_height_in),
    side_filter="A",
    mirror_layers=False,
)

side_b = render_side_view(
    car_id=car_id,
    plan=st.session_state.plan,
    product_lookup=product_lookup,
    side_name="Side B (staggered spots only)",
    car_inside_height_in=float(car_inside_height_in),
    side_filter="B",
    mirror_layers=bool(mirror_side_b),
)

st.subheader("Diagram View")
if view_mode == "Top only":
    components.html(top_svg, height=300, scrolling=False)
elif view_mode == "Sides only":
    ca, cb = st.columns(2)
    with ca:
        components.html(side_a, height=420, scrolling=False)
    with cb:
        components.html(side_b, height=420, scrolling=False)
else:
    components.html(top_svg, height=300, scrolling=False)
    ca, cb = st.columns(2)
    with ca:
        components.html(side_a, height=420, scrolling=False)
    with cb:
        components.html(side_b, height=420, scrolling=False)
