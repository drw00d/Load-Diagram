# streamlit_app.py
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


# =============================
# Config
# =============================
st.set_page_config(page_title="Load Diagram Optimizer", layout="wide")
st.title("Load Diagram Optimizer")

MASTER_PATH = "data/Ortec SP Product Master.xlsx"

# Product Master columns
COL_COMMODITY = "Product Type"
COL_FACILITY = "Facility Id"
COL_PRODUCT_ID = "Sales Product Id"
COL_DESC = "Short Descrip"
COL_ACTIVE = "Active"
COL_UNIT_H = "Unit Height (In)"
COL_UNIT_WT = "Unit Weight (lbs)"
COL_EDGE = "Edge Type"

COL_THICK = "Panel Thickness"
COL_WIDTH = "Width"
COL_LENGTH = "Length"

# Car / diagram assumptions
FLOOR_SPOTS = 15
DOOR_START_SPOT = 6
DOOR_END_SPOT = 9

DOORFRAME_SPOTS_NO_MACHINE_EDGE = {6, 9}   # doorframe
DOORPOCKET_SPOTS = {7, 8}                  # doorway pocket
AIRBAG_ALLOWED_GAPS = [(6, 7), (7, 8), (8, 9)]


# =============================
# Data model
# =============================
@dataclass(frozen=True)
class Product:
    product_id: str
    commodity: str
    facility_id: str
    description: str
    edge_type: str
    unit_height_in: float
    unit_weight_lbs: float

    @property
    def is_machine_edge(self) -> bool:
        return "machine" in (self.edge_type or "").strip().lower()


@dataclass
class RequestLine:
    product_id: str
    tiers: int


# =============================
# Load Product Master
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


def lookup_product(df: pd.DataFrame, pid: str) -> Product:
    pid = str(pid).strip()
    row = df.loc[df[COL_PRODUCT_ID] == pid]
    if row.empty:
        raise KeyError(f"Sales Product Id not found: {pid}")
    r = row.iloc[0]
    return Product(
        product_id=pid,
        commodity=str(r[COL_COMMODITY]).strip(),
        facility_id=str(r[COL_FACILITY]).strip() if COL_FACILITY in df.columns else "",
        description=str(r[COL_DESC]).strip() if COL_DESC in df.columns else "",
        edge_type=str(r[COL_EDGE]).strip(),
        unit_height_in=float(r[COL_UNIT_H]),
        unit_weight_lbs=float(r[COL_UNIT_WT]),
    )


# =============================
# Spot / doorway / side rules
# =============================
def is_doorway_spot(spot: int) -> bool:
    return DOOR_START_SPOT <= spot <= DOOR_END_SPOT


def spot_side_outside_doorway(spot: int) -> str:
    # Alternate A/B for non-doorway spots
    return "A" if (spot % 2 == 1) else "B"


def spot_belongs_to_side(spot: int, side: str) -> bool:
    # Doorway shows on both sides
    if is_doorway_spot(spot):
        return True
    return spot_side_outside_doorway(spot) == side


def outside_doorway_spots() -> List[int]:
    return [s for s in range(1, FLOOR_SPOTS + 1) if not is_doorway_spot(s)]


def doorway_spots() -> List[int]:
    return [s for s in range(1, FLOOR_SPOTS + 1) if is_doorway_spot(s)]


def center_out_order_outside() -> List[int]:
    """
    Center-out fill pattern outside doorway.
    Closest to doorway: 5 and 10, then 4/11, 3/12, ...
    """
    left = [5, 4, 3, 2, 1]
    right = [10, 11, 12, 13, 14, 15]
    order: List[int] = []
    for i in range(max(len(left), len(right))):
        if i < len(left):
            order.append(left[i])
        if i < len(right):
            order.append(right[i])
    # Ensure only non-doorway
    return [s for s in order if s in outside_doorway_spots()]


def doorway_fill_order() -> List[int]:
    """
    Prefer door pocket spots 7/8 first (good for airbag/door),
    then 6/9 last (doorframe restrictions).
    """
    return [7, 8, 6, 9]


# =============================
# Optimizer: tier-slot placement (Load-Xpert-ish)
# =============================
def build_token_lists(products: Dict[str, Product], requests: List[RequestLine]) -> Tuple[List[str], List[str]]:
    """
    Split requested tiers into two token lists:
      - heavy-ish group
      - light-ish group
    We alternate these groups vertically inside each spot.
    """
    # Expand counts
    expanded: List[Tuple[str, float]] = []
    for r in requests:
        if r.tiers <= 0:
            continue
        p = products[r.product_id]
        expanded.extend([(p.product_id, p.unit_weight_lbs)] * int(r.tiers))

    if not expanded:
        return [], []

    # Sort by weight
    expanded.sort(key=lambda x: x[1], reverse=True)

    # Split into heavy / light halves
    mid = math.ceil(len(expanded) / 2)
    heavy = [pid for pid, _ in expanded[:mid]]
    light = [pid for pid, _ in expanded[mid:]]

    return heavy, light


def make_empty_matrix(max_tiers: int) -> List[List[Optional[str]]]:
    """
    matrix[spot_index][tier_index] where tier_index 0 is bottom tier.
    """
    return [[None for _ in range(max_tiers)] for _ in range(FLOOR_SPOTS)]


def spot_has_capacity(matrix: List[List[Optional[str]]], spot: int) -> bool:
    idx = spot - 1
    return any(v is None for v in matrix[idx])


def next_empty_tier_index(matrix: List[List[Optional[str]]], spot: int) -> Optional[int]:
    idx = spot - 1
    for t in range(len(matrix[idx])):  # bottom -> top
        if matrix[idx][t] is None:
            return t
    return None


def can_place_pid_in_spot(products: Dict[str, Product], pid: str, spot: int) -> bool:
    p = products[pid]
    if p.is_machine_edge and spot in DOORFRAME_SPOTS_NO_MACHINE_EDGE:
        return False
    return True


def choose_spot_for_pid(
    matrix: List[List[Optional[str]]],
    products: Dict[str, Product],
    pid: str,
    desired_spot_order: List[int],
) -> Optional[int]:
    """
    Pick the first spot in order that has capacity and passes rules.
    Special-case: if pid is machine edge, skip doorframe 6/9.
    """
    for s in desired_spot_order:
        if not spot_has_capacity(matrix, s):
            continue
        if not can_place_pid_in_spot(products, pid, s):
            continue
        return s
    return None


def optimize_layout(
    products: Dict[str, Product],
    requests: List[RequestLine],
    max_tiers_per_spot: int,
    preferred_side_outside: str,
) -> Tuple[List[List[Optional[str]]], List[str]]:
    """
    Produces a 15 x max_tiers matrix of SKUs, mixing SKUs:
      - Vertically: alternate heavy-ish and light-ish tokens in each spot
      - Lengthwise: center-out placement outside doorway, then doorway pocket (7/8), then 6/9
      - Outside doorway: bias spot order by preferred side first (A or B), but still center-out
    """
    msgs: List[str] = []

    heavy, light = build_token_lists(products, requests)
    total_tokens = len(heavy) + len(light)
    if total_tokens == 0:
        return make_empty_matrix(max_tiers_per_spot), ["No requested tiers to place."]

    # Spot orders
    outside_order = center_out_order_outside()
    # Bias by preferred side while keeping center-out character
    if preferred_side_outside in ("A", "B"):
        pref = [s for s in outside_order if spot_side_outside_doorway(s) == preferred_side_outside]
        other = [s for s in outside_order if spot_side_outside_doorway(s) != preferred_side_outside]
        outside_order = pref + other

    door_order = doorway_fill_order()

    # Combined "try" order per placement:
    # - primarily outside doorway
    # - then doorway pocket / doorway
    base_order = outside_order + door_order

    matrix = make_empty_matrix(max_tiers_per_spot)

    # We place token-by-token into the matrix. To enforce vertical alternation,
    # we alternate heavy/light *per spot tier level*:
    # bottom tier prefers heavy, next prefers light, etc.
    # Implementation: whenever we place into a spot's tier t, prefer:
    #   heavy if t is even, light if t is odd.
    # If that group is empty, fall back to the other.
    def pop_from(group: str) -> Optional[str]:
        nonlocal heavy, light
        if group == "heavy":
            return heavy.pop(0) if heavy else None
        return light.pop(0) if light else None

    def pop_best_for_tier(tier_index: int) -> Optional[str]:
        prefer = "heavy" if (tier_index % 2 == 0) else "light"
        pid = pop_from(prefer)
        if pid is not None:
            return pid
        # fallback
        return pop_from("light" if prefer == "heavy" else "heavy")

    # Main fill loop: attempt to place until tokens exhausted or matrix full
    placed = 0
    # Continue while there is any capacity anywhere
    while (heavy or light) and any(spot_has_capacity(matrix, s) for s in range(1, FLOOR_SPOTS + 1)):
        # Choose next spot to fill: pick the spot with least filled tiers first (promotes evenness)
        # but follow base_order for stability.
        best_spot = None
        best_fill = None
        for s in base_order:
            if not spot_has_capacity(matrix, s):
                continue
            # fill count
            idx = s - 1
            filled = sum(v is not None for v in matrix[idx])
            if best_fill is None or filled < best_fill:
                best_fill = filled
                best_spot = s
        if best_spot is None:
            break

        tier_idx = next_empty_tier_index(matrix, best_spot)
        if tier_idx is None:
            continue

        pid = pop_best_for_tier(tier_idx)
        if pid is None:
            break

        # Enforce machine edge rule; if illegal for this spot, try alternate spots
        if not can_place_pid_in_spot(products, pid, best_spot):
            # Try to find another spot at same tier index that is legal
            # Prefer door pocket if we're in doorway spots, otherwise search base order
            alt_spot = None
            for s in base_order:
                if not spot_has_capacity(matrix, s):
                    continue
                ti = next_empty_tier_index(matrix, s)
                if ti != tier_idx:
                    continue  # keep vertical alternation consistent
                if can_place_pid_in_spot(products, pid, s):
                    alt_spot = s
                    break

            if alt_spot is None:
                # Can't place this pid at this tier level anywhere; put it back and stop
                msgs.append(f"Could not place SKU {pid} at tier level {tier_idx+1} due to rules/capacity.")
                # push back to front of its group roughly
                # (safe fallback; not perfect but keeps it from disappearing)
                if products[pid].unit_weight_lbs >= 0:
                    heavy.insert(0, pid)
                break

            best_spot = alt_spot
            tier_idx = next_empty_tier_index(matrix, best_spot)
            if tier_idx is None:
                continue

        matrix[best_spot - 1][tier_idx] = pid
        placed += 1

    # If unplaced remain, warn
    remaining = len(heavy) + len(light)
    if remaining > 0:
        msgs.append(f"{remaining} tiers could not be placed (capacity/rules).")

    # Quick doorway validation for machine edge
    for s in DOORFRAME_SPOTS_NO_MACHINE_EDGE:
        idx = s - 1
        for pid in matrix[idx]:
            if pid and products[pid].is_machine_edge:
                msgs.append(f"Rule violation: Machine Edge SKU {pid} placed in Spot {s}. (Should not happen)")

    return matrix, msgs


# =============================
# Rendering (Top + Sides) - tier-accurate
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


def doorway_bounds_px(x0: float, cell_w: float) -> Tuple[float, float]:
    left = x0 + (DOOR_START_SPOT - 1) * cell_w
    right = x0 + DOOR_END_SPOT * cell_w
    return left, right


def airbag_center_px(x0: float, cell_w: float, gap_choice: Tuple[int, int]) -> float:
    a, _ = gap_choice
    return x0 + a * cell_w


def render_top_svg(
    *,
    car_id: str,
    matrix: List[List[Optional[str]]],
    note: str,
    airbag_gap_in: float,
    airbag_gap_choice: Tuple[int, int],
    unit_length_ref_in: float,
    center_end: str,
) -> str:
    """
    Top view: show footprint boxes (1-wide).
    - Outside doorway: staggered A/B
    - Doorway 6-9: not staggered (centered)
    - Optional: center end (Spot 1 or Spot 15) for symmetry
    - Label shows a compact mix summary (top 2 + "+n")
    """
    W, H = 1200, 280
    margin = 30
    header_h = 70

    x0, y0 = margin, margin + header_h
    w = W - 2 * margin
    lane_h = H - y0 - margin
    cell_w = w / FLOOR_SPOTS

    lane_y_center = y0 + lane_h / 2
    box_h = lane_h * 0.65
    offset = lane_h * 0.12

    frac = 0.0 if unit_length_ref_in <= 0 else (float(airbag_gap_in) / float(unit_length_ref_in))
    band_w = max(8.0, min(cell_w * 0.9, cell_w * frac))

    center_end_spot = None
    if center_end == "Spot 1":
        center_end_spot = 1
    elif center_end == "Spot 15":
        center_end_spot = 15

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
    svg.append(f'<text x="{margin+8}" y="{margin+26}" font-size="18" font-weight="600">Car: {car_id} — Top View</text>')
    svg.append(f'<text x="{margin+8}" y="{margin+50}" font-size="13">{note}</text>')

    # doorway overlay
    door_left, door_right = doorway_bounds_px(x0, cell_w)
    svg.append(f'<rect x="{door_left}" y="{y0}" width="{door_right-door_left}" height="{lane_h}" fill="url(#doorHatch)" stroke="#c00000" stroke-width="3" opacity="0.9"/>')
    svg.append(f'<text x="{door_left+6}" y="{y0-10}" font-size="12" fill="#c00000">Doorway (Spots {DOOR_START_SPOT}–{DOOR_END_SPOT})</text>')

    # airbag band
    center_x = airbag_center_px(x0, cell_w, airbag_gap_choice)
    band_x = center_x - band_w / 2
    svg.append(f'<rect x="{band_x}" y="{y0}" width="{band_w}" height="{lane_h}" fill="none" stroke="#d00000" stroke-width="5"/>')
    svg.append(f'<text x="{band_x+4}" y="{y0+lane_h+16}" font-size="12" fill="#d00000">Airbag {airbag_gap_in:.1f}" between {airbag_gap_choice[0]}–{airbag_gap_choice[1]}</text>')

    # draw spots
    for i in range(FLOOR_SPOTS):
        spot = i + 1
        col = matrix[i]

        x = x0 + i * cell_w + cell_w * 0.08
        bw = cell_w * 0.84

        # y positioning
        if is_doorway_spot(spot):
            y = lane_y_center - box_h / 2
            side_tag = ""
        else:
            if center_end_spot is not None and spot == center_end_spot:
                y = lane_y_center - box_h / 2
                side_tag = spot_side_outside_doorway(spot)
            else:
                side = spot_side_outside_doorway(spot)
                y = lane_y_center - (box_h / 2) - (offset if side == "A" else -offset)
                side_tag = side

        # pick a representative color: bottom-most non-null
        rep = next((pid for pid in col if pid is not None), None)
        fill = "#ffffff" if rep is None else color_for_pid(rep)

        svg.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{box_h}" fill="{fill}" opacity="0.75" stroke="#333" stroke-width="1"/>')
        label = f"{spot}{side_tag}" if side_tag else f"{spot}"
        svg.append(f'<text x="{x+6}" y="{y+16}" font-size="12" fill="#333">{label}</text>')

        # compact mix summary (counts in this spot)
        counts: Dict[str, int] = {}
        for pid in col:
            if pid is None:
                continue
            counts[pid] = counts.get(pid, 0) + 1
        if counts:
            items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            tooltip = " | ".join([f"{pid} x{cnt}" for pid, cnt in items])
            svg.append(f"<title>Spot {spot}: {tooltip}</title>")

            # show top 2 lines
            for li, (pid, cnt) in enumerate(items[:2]):
                svg.append(f'<text x="{x+6}" y="{y+44 + li*16}" font-size="12" fill="#000">{pid} x{cnt}</text>')
            if len(items) > 2:
                svg.append(f'<text x="{x+6}" y="{y+44 + 2*16}" font-size="12" fill="#000">+{len(items)-2} more</text>')

        # doorframe warning
        if spot in DOORFRAME_SPOTS_NO_MACHINE_EDGE:
            svg.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{box_h}" fill="none" stroke="#7a0000" stroke-width="3"/>')
            svg.append(f'<text x="{x+6}" y="{y+box_h-8}" font-size="11" fill="#7a0000">NO Machine Edge</text>')

    svg.append("</svg>")
    return "\n".join(svg)


def render_side_svg(
    *,
    car_id: str,
    matrix: List[List[Optional[str]]],
    products: Dict[str, Product],
    side_name: str,
    side_filter: str,     # "A" or "B"
    inside_height_ref_in: float,
) -> str:
    """
    Side view: true tier-by-tier rectangles for each spot on that side.
    Doorway spots appear on BOTH sides.
    """
    W, H = 1200, 430
    margin = 30
    header_h = 45

    x0, y0 = margin, margin + header_h
    plot_w = W - 2 * margin
    plot_h = H - y0 - margin
    cell_w = plot_w / FLOOR_SPOTS
    base_y = y0 + plot_h

    max_tiers = len(matrix[0]) if matrix else 0
    # Determine max stack height (inches) for scale
    max_stack_in = 1.0
    for spot in range(1, FLOOR_SPOTS + 1):
        if not spot_belongs_to_side(spot, side_filter):
            continue
        col = matrix[spot - 1]
        stack_in = 0.0
        for pid in col:
            if pid is None:
                continue
            stack_in += float(products[pid].unit_height_in)
        max_stack_in = max(max_stack_in, stack_in)

    ref_h = max(float(inside_height_ref_in), max_stack_in, 1.0)
    scale = plot_h / ref_h

    svg = []
    svg.append(f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">')
    svg.append(f'<rect x="{margin}" y="{margin}" width="{W-2*margin}" height="{H-2*margin}" fill="white" stroke="black" stroke-width="2"/>')
    svg.append(f'<text x="{margin+8}" y="{margin+26}" font-size="16" font-weight="600">Car: {car_id} — {side_name}</text>')

    # base and ref
    svg.append(f'<line x1="{x0}" y1="{base_y}" x2="{x0+plot_w}" y2="{base_y}" stroke="#000" stroke-width="1"/>')
    top_ref_y = base_y - float(inside_height_ref_in) * scale
    svg.append(f'<line x1="{x0}" y1="{top_ref_y}" x2="{x0+plot_w}" y2="{top_ref_y}" stroke="#999" stroke-width="1" />')
    svg.append(f'<text x="{x0+4}" y="{top_ref_y-6}" font-size="12" fill="#666">Inside height ref</text>')

    # doorway bracket
    door_left = x0 + (DOOR_START_SPOT - 1) * cell_w
    door_right = x0 + DOOR_END_SPOT * cell_w
    svg.append(f'<rect x="{door_left}" y="{y0}" width="{door_right-door_left}" height="{plot_h}" fill="none" stroke="#c00000" stroke-width="2" opacity="0.5"/>')

    # Draw columns
    for i in range(FLOOR_SPOTS):
        spot = i + 1
        x = x0 + i * cell_w + 2
        w = cell_w - 4

        # spot labels
        svg.append(f'<text x="{x+2}" y="{base_y+16}" font-size="11" fill="#333">{spot}</text>')

        if not spot_belongs_to_side(spot, side_filter):
            continue

        col = matrix[i]
        if not any(pid is not None for pid in col):
            continue

        # Draw each tier block bottom->top as its own rectangle
        y_cursor = base_y
        for t, pid in enumerate(col):  # tier 0 bottom
            if pid is None:
                continue
            ph = float(products[pid].unit_height_in) * scale
            y_cursor -= ph
            svg.append(f'<rect x="{x}" y="{y_cursor}" width="{w}" height="{ph}" fill="{color_for_pid(pid)}" stroke="#333" stroke-width="1"/>')
            # Label smaller for readability
            svg.append(f'<text x="{x+3}" y="{y_cursor+13}" font-size="10" fill="#000">{pid}</text>')

    svg.append("</svg>")
    return "\n".join(svg)


# =============================
# App state
# =============================
try:
    pm = load_product_master(MASTER_PATH)
except Exception as e:
    st.error(f"Could not load Product Master at '{MASTER_PATH}'. Error: {e}")
    st.stop()

if "requests" not in st.session_state:
    st.session_state.requests: List[RequestLine] = []
if "matrix" not in st.session_state:
    st.session_state.matrix = make_empty_matrix(4)  # default
if "selected_commodity" not in st.session_state:
    st.session_state.selected_commodity = "(Select)"
if "selected_facility" not in st.session_state:
    st.session_state.selected_facility = "(All facilities)"


# =============================
# Sidebar controls
# =============================
with st.sidebar:
    st.header("Settings")
    car_id = st.text_input("Car ID", value="TBOX632012")

    st.divider()
    st.header("Tiers / Height")
    max_tiers = st.slider("Max tiers per spot", 1, 8, 4)
    inside_height_ref_in = st.number_input("Inside height ref (in)", min_value=60.0, value=110.0, step=1.0)

    st.divider()
    st.header("Doorway / Airbag")
    gap_labels = [f"{a}–{b}" for a, b in AIRBAG_ALLOWED_GAPS]
    gap_choice_label = st.selectbox("Airbag location (within doorway)", gap_labels, index=1)  # default 7–8
    airbag_gap_in = st.slider("Airbag gap (in)", 6.0, 9.0, 9.0, 0.5)
    unit_length_ref_in = st.number_input("Unit length ref (in) for gap drawing", min_value=1.0, value=96.0, step=1.0)

    st.divider()
    st.header("Balancing preferences")
    preferred_side = st.selectbox("Outside-doorway side preference", ["A", "B"], index=0)
    center_end = st.selectbox("Center one end unit (Top view)", ["None", "Spot 1", "Spot 15"], index=2)

    st.divider()
    view_mode = st.radio("View", ["Top + Both Sides", "Top only", "Sides only"], index=0)

airbag_gap_choice = AIRBAG_ALLOWED_GAPS[gap_labels.index(gap_choice_label)]


# =============================
# Filters: Commodity -> Facility -> Products
# =============================
st.success(f"Product Master loaded: {len(pm):,} rows")

commodities = sorted(pm[COL_COMMODITY].dropna().astype(str).unique().tolist())
commodity_selected = st.selectbox("Commodity / Product Type (required)", ["(Select)"] + commodities)

# Reset on commodity change
if commodity_selected != st.session_state.selected_commodity:
    st.session_state.selected_commodity = commodity_selected
    st.session_state.selected_facility = "(All facilities)"
    st.session_state.requests = []
    st.session_state.matrix = make_empty_matrix(max_tiers)

if commodity_selected == "(Select)":
    st.info("Select a Commodity/Product Type to proceed.")
    st.stop()

pm_c = pm[pm[COL_COMMODITY].astype(str) == str(commodity_selected)].copy()

facilities = sorted(pm_c[COL_FACILITY].dropna().astype(str).unique().tolist()) if COL_FACILITY in pm_c.columns else []
facility_selected = st.selectbox("Facility Id (filtered by commodity)", ["(All facilities)"] + facilities)

# Reset on facility change
if facility_selected != st.session_state.selected_facility:
    st.session_state.selected_facility = facility_selected
    st.session_state.requests = []
    st.session_state.matrix = make_empty_matrix(max_tiers)

pm_cf = pm_c.copy()
if facility_selected != "(All facilities)" and COL_FACILITY in pm_cf.columns:
    pm_cf = pm_cf[pm_cf[COL_FACILITY].astype(str) == str(facility_selected)].copy()

# Search + sort + dedupe for picker
search = st.text_input("Search (Product Id or Description)", value="")

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


def format_option(r: dict) -> str:
    pid = str(r.get(COL_PRODUCT_ID, "")).strip()
    edge = str(r.get(COL_EDGE, "")).strip()
    desc = str(r.get(COL_DESC, "")).strip()
    thick = r.get(COL_THICK, None)
    parts = [pid]
    if pd.notna(thick):
        parts.append(f'{float(thick):g}"')
    if edge:
        parts.append(edge)
    if desc:
        parts.append(desc)
    return " | ".join(parts)


options = pm_cf.to_dict("records")
labels = [format_option(r) for r in options]
selected_label = st.selectbox("Pick a Product", labels) if labels else None

# Add request lines (tiers)
c1, c2, c3, c4 = st.columns([2, 1, 1, 1], vertical_alignment="bottom")
with c1:
    tiers_to_add = st.number_input("Tiers to add (packs)", min_value=1, value=4, step=1)
with c2:
    add_line = st.button("Add Line", disabled=(selected_label is None))
with c3:
    optimize_btn = st.button("Optimize Layout")
with c4:
    clear_btn = st.button("Clear All")

if clear_btn:
    st.session_state.requests = []
    st.session_state.matrix = make_empty_matrix(max_tiers)

if add_line and selected_label:
    idx = labels.index(selected_label)
    pid = str(options[idx][COL_PRODUCT_ID]).strip()
    st.session_state.requests.append(RequestLine(product_id=pid, tiers=int(tiers_to_add)))

# Show requests table
st.subheader("Requested SKUs (tiers)")
if st.session_state.requests:
    req_df = pd.DataFrame([{"Sales Product Id": r.product_id, "Tiers": r.tiers} for r in st.session_state.requests])
    st.dataframe(req_df, use_container_width=True, height=200)
else:
    st.info("Add one or more SKU lines, then click **Optimize Layout**.")

# Build products dict needed by optimizer
# (use full PM lookup so weights/heights are consistent)
products: Dict[str, Product] = {}
for r in st.session_state.requests:
    try:
        products[r.product_id] = lookup_product(pm, r.product_id)
    except Exception as e:
        st.error(f"Could not lookup SKU {r.product_id}: {e}")

# Optimize
messages: List[str] = []
if optimize_btn:
    # Ensure matrix tier depth matches slider
    st.session_state.matrix = make_empty_matrix(max_tiers)

    if not st.session_state.requests:
        st.warning("No request lines to optimize.")
    else:
        matrix, msgs = optimize_layout(
            products=products,
            requests=st.session_state.requests,
            max_tiers_per_spot=int(max_tiers),
            preferred_side_outside=str(preferred_side),
        )
        st.session_state.matrix = matrix
        messages.extend(msgs)

for m in messages:
    st.warning(m)

# Compute payload (from optimized matrix)
matrix = st.session_state.matrix
payload = 0.0
tiers_used = 0
for spot in range(1, FLOOR_SPOTS + 1):
    for pid in matrix[spot - 1]:
        if pid is None:
            continue
        payload += float(products[pid].unit_weight_lbs) if pid in products else 0.0
        tiers_used += 1

st.subheader("Summary")
st.metric("Payload (lbs)", f"{payload:,.0f}")
st.metric("Placed tiers", f"{tiers_used:,} / {FLOOR_SPOTS*int(max_tiers):,}")
st.caption(
    "This optimizer mixes SKUs **by tiers** (not blocks) and alternates heavy/light vertically for better balance. "
    "Doorway (Spots 6–9) is non-staggered; Machine Edge is blocked from 6 & 9."
)

# Hard validation: machine edge in 6/9
violations = []
for spot in DOORFRAME_SPOTS_NO_MACHINE_EDGE:
    for pid in matrix[spot - 1]:
        if pid and pid in products and products[pid].is_machine_edge:
            violations.append(f"Machine Edge SKU {pid} is in Spot {spot} (not allowed).")
for v in violations:
    st.error(v)

# Notes header for diagrams
note = (
    f"Commodity: {commodity_selected} | Facility: {facility_selected} | "
    f"Doorway: {DOOR_START_SPOT}–{DOOR_END_SPOT} (no stagger) | "
    f"Airbag: {gap_choice_label} @ {airbag_gap_in:.1f}\" | "
    f"Vertical mix: heavy/light alternating"
)

# Render diagrams
top_svg = render_top_svg(
    car_id=car_id,
    matrix=matrix,
    note=note,
    airbag_gap_in=float(airbag_gap_in),
    airbag_gap_choice=airbag_gap_choice,
    unit_length_ref_in=float(unit_length_ref_in),
    center_end=str(center_end),
)

side_a = render_side_svg(
    car_id=car_id,
    matrix=matrix,
    products=products,
    side_name="Side A (tier-accurate)",
    side_filter="A",
    inside_height_ref_in=float(inside_height_ref_in),
)

side_b = render_side_svg(
    car_id=car_id,
    matrix=matrix,
    products=products,
    side_name="Side B (tier-accurate)",
    side_filter="B",
    inside_height_ref_in=float(inside_height_ref_in),
)

st.subheader("Diagram View")
if view_mode == "Top only":
    components.html(top_svg, height=300, scrolling=False)
elif view_mode == "Sides only":
    ca, cb = st.columns(2)
    with ca:
        components.html(side_a, height=450, scrolling=False)
    with cb:
        components.html(side_b, height=450, scrolling=False)
else:
    components.html(top_svg, height=300, scrolling=False)
    ca, cb = st.columns(2)
    with ca:
        components.html(side_a, height=450, scrolling=False)
    with cb:
        components.html(side_b, height=450, scrolling=False)
