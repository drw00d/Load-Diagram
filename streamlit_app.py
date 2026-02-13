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

# Optional/if present
COL_HALF_PACK = "Half Pack"
COL_THICK = "Panel Thickness"
COL_WIDTH = "Width"
COL_LENGTH = "Length"

# Car / diagram assumptions (Load Xpert style)
FLOOR_SPOTS = 15
DOOR_START_SPOT = 6
DOOR_END_SPOT = 9

DOORFRAME_SPOTS_NO_MACHINE_EDGE = {6, 9}  # doorframe
DOORPOCKET_SPOTS = {7, 8}                 # doorway pocket (PIN zone)
AIRBAG_ALLOWED_GAPS = [(6, 7), (7, 8), (8, 9)]

BLOCK = "__BLOCK__"  # internal marker for blocked tiers


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
    is_half_pack: bool

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


def _truthy(v) -> bool:
    s = str(v).strip().upper()
    return s in ("Y", "YES", "TRUE", "1", "T")


def lookup_product(df: pd.DataFrame, pid: str) -> Product:
    pid = str(pid).strip()
    row = df.loc[df[COL_PRODUCT_ID] == pid]
    if row.empty:
        raise KeyError(f"Sales Product Id not found: {pid}")
    r = row.iloc[0]

    desc = str(r[COL_DESC]).strip() if COL_DESC in df.columns else ""
    if COL_HALF_PACK in df.columns:
        is_hp = _truthy(r.get(COL_HALF_PACK, ""))
    else:
        is_hp = desc.upper().rstrip().endswith("HP")

    return Product(
        product_id=pid,
        commodity=str(r[COL_COMMODITY]).strip(),
        facility_id=str(r[COL_FACILITY]).strip() if COL_FACILITY in df.columns else "",
        description=desc,
        edge_type=str(r[COL_EDGE]).strip(),
        unit_height_in=float(r[COL_UNIT_H]),
        unit_weight_lbs=float(r[COL_UNIT_WT]),
        is_half_pack=bool(is_hp),
    )


# =============================
# Spot helpers (doorway, turn, etc.)
# =============================
def is_doorway_spot(spot: int) -> bool:
    return DOOR_START_SPOT <= spot <= DOOR_END_SPOT


def spot_side_outside_doorway(spot: int) -> str:
    return "A" if (spot % 2 == 1) else "B"


def outside_doorway_spots() -> List[int]:
    return [s for s in range(1, FLOOR_SPOTS + 1) if not is_doorway_spot(s)]


def center_out_order_outside() -> List[int]:
    left = [5, 4, 3, 2, 1]
    right = [10, 11, 12, 13, 14, 15]
    order: List[int] = []
    for i in range(max(len(left), len(right))):
        if i < len(left):
            order.append(left[i])
        if i < len(right):
            order.append(right[i])
    return [s for s in order if s in outside_doorway_spots()]


def doorway_fill_order() -> List[int]:
    # favor pockets first (7/8), then doorframe (6/9)
    return [7, 8, 6, 9]


def blocked_spot_for_turn(turn_spot: Optional[int]) -> Optional[int]:
    if turn_spot is None:
        return None
    b = turn_spot + 1
    if 1 <= b <= FLOOR_SPOTS:
        return b
    return None


def is_blocked_spot(spot: int, turn_spot: Optional[int]) -> bool:
    b = blocked_spot_for_turn(turn_spot)
    return (b is not None and spot == b)


def occupied_spots_for_placement(spot: int, turn_spot: Optional[int]) -> List[int]:
    """
    If this is the TURN spot, it consumes spot and spot+1.
    Otherwise, single spot. (Blocked spot never places.)
    """
    if turn_spot is not None and spot == turn_spot:
        b = blocked_spot_for_turn(turn_spot)
        return [spot, b] if b is not None else [spot]
    return [spot]


# =============================
# Matrix / placement primitives
# =============================
def make_empty_matrix(max_tiers: int, turn_spot: Optional[int]) -> List[List[Optional[str]]]:
    m = [[None for _ in range(max_tiers)] for _ in range(FLOOR_SPOTS)]
    b = blocked_spot_for_turn(turn_spot)
    if b is not None:
        # mark all tiers blocked in the blocked spot column
        for t in range(max_tiers):
            m[b - 1][t] = BLOCK
    return m


def spot_has_capacity(matrix: List[List[Optional[str]]], spot: int, turn_spot: Optional[int]) -> bool:
    # blocked spot never has capacity
    if is_blocked_spot(spot, turn_spot):
        return False

    # if this is the TURN spot, it needs capacity in BOTH spots for a tier
    occ = occupied_spots_for_placement(spot, turn_spot)
    # capacity means: exists at least one tier index where all occupied spots are empty (None)
    tiers = len(matrix[0]) if matrix else 0
    for t in range(tiers):
        ok = True
        for s in occ:
            v = matrix[s - 1][t]
            if v is not None:
                ok = False
                break
        if ok:
            return True
    return False


def next_empty_tier_index(matrix: List[List[Optional[str]]], spot: int, turn_spot: Optional[int]) -> Optional[int]:
    if is_blocked_spot(spot, turn_spot):
        return None

    occ = occupied_spots_for_placement(spot, turn_spot)
    tiers = len(matrix[0]) if matrix else 0
    for t in range(tiers):  # bottom -> top
        ok = True
        for s in occ:
            v = matrix[s - 1][t]
            if v is not None:
                ok = False
                break
        if ok:
            return t
    return None


def place_pid(matrix: List[List[Optional[str]]], spot: int, tier_idx: int, pid: str, turn_spot: Optional[int]) -> None:
    occ = occupied_spots_for_placement(spot, turn_spot)
    for s in occ:
        matrix[s - 1][tier_idx] = pid


# =============================
# Hard rules (AAR-ish + doorway specifics)
# =============================
def can_place_pid_hard(products: Dict[str, Product], pid: str, spot: int, turn_spot: Optional[int]) -> Tuple[bool, str]:
    p = products[pid]
    occ = occupied_spots_for_placement(spot, turn_spot)

    # Machine Edge: NOT allowed in doorframe spots 6/9
    if p.is_machine_edge and any(s in DOORFRAME_SPOTS_NO_MACHINE_EDGE for s in occ):
        return False, f"Machine Edge not allowed in doorframe spot(s) {sorted(set(occ) & DOORFRAME_SPOTS_NO_MACHINE_EDGE)}."

    return True, ""


def soft_penalty(products: Dict[str, Product], pid: str, tier_idx: int, max_tiers: int) -> int:
    p = products[pid]
    penalty = 0
    # Soft: avoid half pack on very top tier
    if p.is_half_pack and tier_idx == (max_tiers - 1):
        penalty += 100
    return penalty


# =============================
# Optimizer (balanced vertically, pins, soft repair)
# =============================
def build_token_lists(products: Dict[str, Product], requests: List[RequestLine]) -> Tuple[List[str], List[str]]:
    expanded: List[Tuple[str, float]] = []
    for r in requests:
        if r.tiers <= 0:
            continue
        p = products[r.product_id]
        expanded.extend([(p.product_id, p.unit_weight_lbs)] * int(r.tiers))

    if not expanded:
        return [], []

    expanded.sort(key=lambda x: x[1], reverse=True)
    mid = math.ceil(len(expanded) / 2)
    heavy = [pid for pid, _ in expanded[:mid]]
    light = [pid for pid, _ in expanded[mid:]]
    return heavy, light


def pop_best_placeable(
    tokens: List[str],
    products: Dict[str, Product],
    spot: int,
    tier_idx: int,
    max_tiers: int,
    turn_spot: Optional[int],
) -> Optional[str]:
    best_i = None
    best_score = None
    for i, pid in enumerate(tokens):
        ok, _ = can_place_pid_hard(products, pid, spot, turn_spot)
        if not ok:
            continue
        score = soft_penalty(products, pid, tier_idx, max_tiers)
        if best_score is None or score < best_score:
            best_score = score
            best_i = i
            if score == 0:
                break
    if best_i is None:
        return None
    return tokens.pop(best_i)


def find_spot_for_pid_with_pins(
    matrix: List[List[Optional[str]]],
    products: Dict[str, Product],
    pid: str,
    tier_idx: int,
    base_order: List[int],
    turn_spot: Optional[int],
) -> Optional[int]:
    p = products[pid]

    # PIN preference: Machine Edge wants 7/8 if possible
    if p.is_machine_edge:
        for s in [7, 8]:
            if is_blocked_spot(s, turn_spot):
                continue
            ti = next_empty_tier_index(matrix, s, turn_spot)
            if ti != tier_idx:
                continue
            ok, _ = can_place_pid_hard(products, pid, s, turn_spot)
            if ok:
                return s

    # otherwise choose first feasible
    for s in base_order:
        if is_blocked_spot(s, turn_spot):
            continue
        ti = next_empty_tier_index(matrix, s, turn_spot)
        if ti != tier_idx:
            continue
        ok, _ = can_place_pid_hard(products, pid, s, turn_spot)
        if ok:
            return s

    return None


def count_top_halfpacks(matrix: List[List[Optional[str]]], products: Dict[str, Product]) -> int:
    if not matrix:
        return 0
    top = len(matrix[0]) - 1
    c = 0
    for spot in range(1, FLOOR_SPOTS + 1):
        pid = matrix[spot - 1][top]
        if pid and pid != BLOCK and pid in products and products[pid].is_half_pack:
            c += 1
    return c


def repair_reduce_top_halfpacks(matrix: List[List[Optional[str]]], products: Dict[str, Product]) -> int:
    if not matrix:
        return 0
    top = len(matrix[0]) - 1
    swaps = 0

    def is_half(pid: Optional[str]) -> bool:
        return bool(pid and pid != BLOCK and pid in products and products[pid].is_half_pack)

    def is_full(pid: Optional[str]) -> bool:
        return bool(pid and pid != BLOCK and pid in products and (not products[pid].is_half_pack))

    targets = [s for s in range(1, FLOOR_SPOTS + 1) if is_half(matrix[s - 1][top])]

    for spot in targets:
        hp_pid = matrix[spot - 1][top]
        if not hp_pid or hp_pid == BLOCK:
            continue

        # Try swap within same spot
        for t in range(top - 1, -1, -1):
            below = matrix[spot - 1][t]
            if not is_full(below):
                continue
            matrix[spot - 1][top], matrix[spot - 1][t] = below, hp_pid
            swaps += 1
            hp_pid = None
            break

        if hp_pid is None:
            continue

        # Try swap with another spot below-top
        swapped = False
        for other_spot in range(1, FLOOR_SPOTS + 1):
            if other_spot == spot:
                continue
            for t in range(top - 1, -1, -1):
                cand = matrix[other_spot - 1][t]
                if not is_full(cand):
                    continue
                matrix[spot - 1][top] = cand
                matrix[other_spot - 1][t] = hp_pid
                swaps += 1
                swapped = True
                break
            if swapped:
                break

    return swaps


def optimize_layout(
    products: Dict[str, Product],
    requests: List[RequestLine],
    max_tiers_per_spot: int,
    preferred_side_outside: str,
    turn_spot: Optional[int],
) -> Tuple[List[List[Optional[str]]], List[str]]:
    msgs: List[str] = []

    heavy, light = build_token_lists(products, requests)
    if not heavy and not light:
        return make_empty_matrix(max_tiers_per_spot, turn_spot), ["No requested tiers to place."]

    outside_order = center_out_order_outside()
    if preferred_side_outside in ("A", "B"):
        pref = [s for s in outside_order if spot_side_outside_doorway(s) == preferred_side_outside]
        other = [s for s in outside_order if spot_side_outside_doorway(s) != preferred_side_outside]
        outside_order = pref + other

    base_order = outside_order + doorway_fill_order()

    # remove blocked spot from ordering if turn is enabled
    base_order = [s for s in base_order if not is_blocked_spot(s, turn_spot)]

    matrix = make_empty_matrix(max_tiers_per_spot, turn_spot)

    def tier_pref_group(tier_idx: int) -> str:
        # bottom tier prefers heavy, then alternate
        return "heavy" if (tier_idx % 2 == 0) else "light"

    # main placement loop
    while (heavy or light) and any(spot_has_capacity(matrix, s, turn_spot) for s in range(1, FLOOR_SPOTS + 1)):
        # choose next spot to fill (least filled, in base order)
        best_spot = None
        best_fill = None
        for s in base_order:
            if not spot_has_capacity(matrix, s, turn_spot):
                continue
            filled = sum(v is not None for v in matrix[s - 1] if v != BLOCK)
            if best_fill is None or filled < best_fill:
                best_fill = filled
                best_spot = s
        if best_spot is None:
            break

        tier_idx = next_empty_tier_index(matrix, best_spot, turn_spot)
        if tier_idx is None:
            continue

        pref = tier_pref_group(tier_idx)

        pid = None
        if pref == "heavy":
            pid = pop_best_placeable(heavy, products, best_spot, tier_idx, max_tiers_per_spot, turn_spot) \
                  or pop_best_placeable(light, products, best_spot, tier_idx, max_tiers_per_spot, turn_spot)
        else:
            pid = pop_best_placeable(light, products, best_spot, tier_idx, max_tiers_per_spot, turn_spot) \
                  or pop_best_placeable(heavy, products, best_spot, tier_idx, max_tiers_per_spot, turn_spot)

        if pid is None:
            msgs.append(f"Could not place any remaining tiers at Tier {tier_idx+1} due to constraints/capacity.")
            break

        # PIN preference can move it to 7/8
        pinned = find_spot_for_pid_with_pins(matrix, products, pid, tier_idx, base_order, turn_spot)
        if pinned is not None:
            best_spot = pinned

        ok, why = can_place_pid_hard(products, pid, best_spot, turn_spot)
        if not ok:
            msgs.append(f"Skipped {pid} at Spot {best_spot}, Tier {tier_idx+1}: {why}")
            break

        place_pid(matrix, best_spot, tier_idx, pid, turn_spot)

    remaining = len(heavy) + len(light)
    if remaining > 0:
        msgs.append(f"{remaining} tiers could not be placed (capacity/rules).")

    before = count_top_halfpacks(matrix, products)
    swaps = repair_reduce_top_halfpacks(matrix, products)
    after = count_top_halfpacks(matrix, products)

    if swaps > 0:
        msgs.append(f"Repair pass: {swaps} swap(s) to reduce Half Packs on top (before={before}, after={after}).")
    else:
        msgs.append(f"Repair pass: no swaps found (Half Packs on top={after}).")

    return matrix, msgs


# =============================
# Rendering helpers
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


def components_svg(svg: str, height: int) -> None:
    html = f"""
    <div style="width:100%; overflow: visible;">
      {svg}
    </div>
    """
    components.html(html, height=height, scrolling=False)


def auto_airbag_choice(_matrix: List[List[Optional[str]]]) -> Tuple[Tuple[int, int], float]:
    # prefer 7–8 (most common), else 8–9, else 6–7
    preferred = [(7, 8), (8, 9), (6, 7)]
    for g in preferred:
        if g in AIRBAG_ALLOWED_GAPS:
            return g, 6.0
    return AIRBAG_ALLOWED_GAPS[1], 6.0


# =============================
# Top view render (with merged turn column)
# =============================
def render_top_svg(
    *,
    car_id: str,
    matrix: List[List[Optional[str]]],
    products: Dict[str, Product],
    note: str,
    airbag_gap_in: float,
    airbag_gap_choice: Tuple[int, int],
    unit_length_ref_in: float,
    center_end: str,
    turn_spot: Optional[int],
) -> str:
    W, H = 1200, 310
    margin = 30
    header_h = 74

    x0, y0 = margin, margin + header_h
    w = W - 2 * margin
    lane_h = H - y0 - margin
    cell_w = w / FLOOR_SPOTS

    lane_y_center = y0 + lane_h / 2
    box_h = lane_h * 0.65
    offset = lane_h * 0.12

    frac = 0.0 if unit_length_ref_in <= 0 else (float(airbag_gap_in) / float(unit_length_ref_in))
    band_w = max(8.0, min(cell_w * 0.9, cell_w * frac))

    # Doorway bounds (still 6–9)
    door_left = x0 + (DOOR_START_SPOT - 1) * cell_w
    door_right = x0 + DOOR_END_SPOT * cell_w

    # Airbag band
    a, b = airbag_gap_choice
    center_x = x0 + a * cell_w
    band_x = center_x - band_w / 2

    center_end_spot = None
    if center_end == "Spot 1":
        center_end_spot = 1
    elif center_end == "Spot 15":
        center_end_spot = 15

    top_idx = len(matrix[0]) - 1 if matrix else 0
    blocked = blocked_spot_for_turn(turn_spot)

    svg = []
    svg.append(f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">')
    svg.append("""
    <defs>
      <pattern id="doorHatch" patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="8" stroke="#c00000" stroke-width="2" opacity="0.35"/>
      </pattern>
      <pattern id="turnHatch" patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="8" stroke="#111" stroke-width="2" opacity="0.16"/>
      </pattern>
    </defs>
    """)
    svg.append(f'<rect x="{margin}" y="{margin}" width="{W-2*margin}" height="{H-2*margin}" fill="white" stroke="black" stroke-width="2"/>')
    svg.append(f'<text x="{margin+8}" y="{margin+26}" font-size="18" font-weight="600">Car: {car_id} — Top View</text>')
    svg.append(f'<text x="{margin+8}" y="{margin+52}" font-size="13">{note}</text>')

    svg.append(f'<rect x="{door_left}" y="{y0}" width="{door_right-door_left}" height="{lane_h}" fill="url(#doorHatch)" stroke="#c00000" stroke-width="3" opacity="0.9"/>')
    svg.append(f'<text x="{door_left+6}" y="{y0-10}" font-size="12" fill="#c00000">Doorway (Spots {DOOR_START_SPOT}–{DOOR_END_SPOT})</text>')

    svg.append(f'<rect x="{band_x}" y="{y0}" width="{band_w}" height="{lane_h}" fill="none" stroke="#d00000" stroke-width="5"/>')
    svg.append(f'<text x="{band_x+4}" y="{y0+lane_h+16}" font-size="12" fill="#d00000">Airbag {airbag_gap_in:.1f}" between {a}–{b}</text>')

    i = 1
    while i <= FLOOR_SPOTS:
        if blocked is not None and i == blocked:
            i += 1
            continue

        spot = i
        is_turn = (turn_spot is not None and spot == turn_spot and blocked is not None)
        span = 2 if is_turn else 1

        col = matrix[spot - 1]
        x = x0 + (spot - 1) * cell_w + cell_w * 0.08
        bw = cell_w * span * 0.84 + cell_w * (span - 1) * 0.16  # keep visual spacing similar

        # vertical placement: stagger outside doorway only; doorway stays centered
        if is_doorway_spot(spot) or (is_turn and any(is_doorway_spot(s) for s in [spot, blocked])):
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

        rep = next((pid for pid in col if pid is not None and pid != BLOCK), None)
        fill = "#ffffff" if rep is None else color_for_pid(rep)

        # turn column visual hatch
        if is_turn:
            svg.append(f'<rect x="{x0 + (spot-1)*cell_w}" y="{y0}" width="{cell_w*2}" height="{lane_h}" fill="url(#turnHatch)" stroke="#111" stroke-width="2" opacity="0.9"/>')
            svg.append(f'<text x="{x0 + (spot-1)*cell_w + cell_w}" y="{y0+16}" font-size="12" text-anchor="middle" fill="#111">FORKLIFT TURN (spans {spot}–{blocked})</text>')

        svg.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{box_h}" fill="{fill}" opacity="0.75" stroke="#333" stroke-width="1"/>')

        label = f"{spot}{side_tag}" if side_tag else f"{spot}"
        if is_turn:
            label = f"{spot}-{blocked}"
        svg.append(f'<text x="{x+6}" y="{y+16}" font-size="12" fill="#333">{label}</text>')

        # counts by SKU in the spot (tier-mix)
        counts: Dict[str, int] = {}
        for pid in col:
            if pid is None or pid == BLOCK:
                continue
            counts[pid] = counts.get(pid, 0) + 1

        if counts:
            items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
            tooltip = " | ".join([f"{pid} x{cnt}" for pid, cnt in items])
            svg.append(f"<title>Spot {label}: {tooltip}</title>")

            for li, (pid, cnt) in enumerate(items[:2]):
                hp = " HP" if (pid in products and products[pid].is_half_pack) else ""
                svg.append(f'<text x="{x+6}" y="{y+44 + li*16}" font-size="12" fill="#000">{pid}{hp} x{cnt}</text>')
            if len(items) > 2:
                svg.append(f'<text x="{x+6}" y="{y+44 + 2*16}" font-size="12" fill="#000">+{len(items)-2} more</text>')

        # Doorframe "NO ME" overlay if this spot (or its turned span) includes 6/9
        occ = [spot] if not is_turn else [spot, blocked]
        if any(s in DOORFRAME_SPOTS_NO_MACHINE_EDGE for s in occ):
            svg.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{box_h}" fill="none" stroke="#7a0000" stroke-width="3"/>')
            svg.append(f'<text x="{x+6}" y="{y+box_h-8}" font-size="11" fill="#7a0000">NO Machine Edge (doorframe)</text>')

        # Soft top-tier half pack highlight (pink)
        top_pid = col[top_idx] if col and top_idx < len(col) else None
        if top_pid and top_pid != BLOCK and top_pid in products and products[top_pid].is_half_pack:
            svg.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{box_h}" fill="none" stroke="#ff00aa" stroke-width="4" opacity="0.8"/>')

        i += span

    svg.append("</svg>")
    return "\n".join(svg)


# =============================
# Side view render (Load-Xpert-ish) with merged turn column
# =============================
def render_side_loadxpert_svg(
    *,
    car_id: str,
    matrix: List[List[Optional[str]]],
    products: Dict[str, Product],
    title: str,
    airbag_gap_choice: Tuple[int, int],
    airbag_gap_in: float,
    unit_length_ref_in: float,
    turn_spot: Optional[int],
) -> str:
    tiers = len(matrix[0]) if matrix else 0

    W = 1700
    H = 560
    margin = 24

    x0 = margin
    y0 = margin + 74
    car_w = W - 2 * margin
    car_h = H - y0 - margin - 10

    pad = 22
    load_x = x0 + pad
    load_y = y0 + pad
    load_w = car_w - 2 * pad
    load_h = car_h - 2 * pad

    cell_w = load_w / FLOOR_SPOTS
    cell_h = load_h / max(1, tiers)

    frac = 0.0 if unit_length_ref_in <= 0 else (float(airbag_gap_in) / float(unit_length_ref_in))
    band_w = max(8.0, min(cell_w * 0.9, cell_w * frac))

    door_left = load_x + (DOOR_START_SPOT - 1) * cell_w
    door_right = load_x + DOOR_END_SPOT * cell_w

    a, b = airbag_gap_choice
    airbag_x_center = load_x + a * cell_w
    airbag_x = airbag_x_center - band_w / 2

    blocked = blocked_spot_for_turn(turn_spot)

    subtitle = f"Car: {car_id} • Doorway {DOOR_START_SPOT}–{DOOR_END_SPOT} • Airbag {a}–{b} @ {airbag_gap_in:.1f}\""
    if turn_spot is not None and blocked is not None:
        subtitle += f" • Turn spans {turn_spot}–{blocked}"

    svg = []
    svg.append(
        f'<svg width="100%" height="{H}" viewBox="0 0 {W} {H}" '
        f'preserveAspectRatio="xMinYMin meet" xmlns="http://www.w3.org/2000/svg">'
    )
    svg.append("""
    <defs>
      <pattern id="doorHatch2" patternUnits="userSpaceOnUse" width="10" height="10" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="10" stroke="#c00000" stroke-width="2" opacity="0.25"/>
      </pattern>
      <pattern id="turnHatch2" patternUnits="userSpaceOnUse" width="10" height="10" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="10" stroke="#111" stroke-width="2" opacity="0.14"/>
      </pattern>
    </defs>
    """)
    svg.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="white"/>')

    svg.append(f'<text x="{margin}" y="{margin+26}" font-size="20" font-weight="700">{title}</text>')
    svg.append(f'<text x="{margin}" y="{margin+52}" font-size="14" fill="#333">{subtitle}</text>')

    # outer car
    svg.append(f'<rect x="{x0}" y="{y0}" width="{car_w}" height="{car_h}" fill="none" stroke="#0b2a7a" stroke-width="4"/>')

    # wheels
    wheel_y = y0 + car_h - 12
    svg.append(f'<circle cx="{x0+car_w*0.22}" cy="{wheel_y}" r="10" fill="#666" opacity="0.5"/>')
    svg.append(f'<circle cx="{x0+car_w*0.28}" cy="{wheel_y}" r="10" fill="#666" opacity="0.5"/>')
    svg.append(f'<circle cx="{x0+car_w*0.72}" cy="{wheel_y}" r="10" fill="#666" opacity="0.5"/>')
    svg.append(f'<circle cx="{x0+car_w*0.78}" cy="{wheel_y}" r="10" fill="#666" opacity="0.5"/>')

    # doorway and airbag
    svg.append(f'<rect x="{door_left}" y="{load_y}" width="{door_right-door_left}" height="{load_h}" fill="url(#doorHatch2)" stroke="#c00000" stroke-width="3"/>')
    svg.append(f'<text x="{door_left+6}" y="{load_y-6}" font-size="12" fill="#c00000">Doorway</text>')
    svg.append(f'<rect x="{airbag_x}" y="{load_y}" width="{band_w}" height="{load_h}" fill="none" stroke="#d00000" stroke-width="5"/>')

    # Draw columns; if turn spot, merge width and skip blocked
    i = 1
    while i <= FLOOR_SPOTS:
        if blocked is not None and i == blocked:
            i += 1
            continue

        spot = i
        is_turn = (turn_spot is not None and blocked is not None and spot == turn_spot)
        span = 2 if is_turn else 1

        x = load_x + (spot - 1) * cell_w
        wcol = cell_w * span

        # column outline + label
        if is_turn:
            svg.append(f'<rect x="{x}" y="{load_y}" width="{wcol}" height="{load_h}" fill="url(#turnHatch2)" stroke="#111" stroke-width="2" opacity="0.9"/>')
            svg.append(f'<text x="{x + wcol/2}" y="{load_y + 16}" font-size="12" text-anchor="middle" fill="#111">TURN</text>')
        svg.append(f'<rect x="{x}" y="{load_y}" width="{wcol}" height="{load_h}" fill="none" stroke="#333" stroke-width="1" opacity="0.55"/>')

        label = f"{spot}" if not is_turn else f"{spot}-{blocked}"
        svg.append(f'<text x="{x + wcol/2}" y="{load_y + load_h + 18}" font-size="12" text-anchor="middle" fill="#333">{label}</text>')

        # tier blocks (use spot's matrix; for turn it represents both)
        col = matrix[spot - 1]
        for t in range(tiers):
            pid = col[t]
            if pid is None or pid == BLOCK:
                continue

            y = load_y + load_h - (t + 1) * cell_h
            fill = color_for_pid(pid)

            svg.append(
                f'<rect x="{x+1}" y="{y+1}" width="{wcol-2}" height="{cell_h-2}" '
                f'fill="{fill}" stroke="#1a1a1a" stroke-width="1" opacity="0.95"/>'
            )

            hp = " HP" if (pid in products and products[pid].is_half_pack) else ""
            me = " ME" if (pid in products and products[pid].is_machine_edge) else ""
            text = f"{pid}{hp}{me}"
            svg.append(f'<text x="{x + wcol/2}" y="{y + cell_h/2 + 5}" font-size="13" text-anchor="middle" fill="#0a0a0a">{text}</text>')

        # Doorframe NO ME markers for 6/9 (and for turn span if it touches 6/9)
        occ = [spot] if not is_turn else [spot, blocked]
        if any(s in DOORFRAME_SPOTS_NO_MACHINE_EDGE for s in occ):
            svg.append(f'<rect x="{x+2}" y="{load_y+2}" width="{wcol-4}" height="{load_h-4}" fill="none" stroke="#7a0000" stroke-width="4"/>')
            svg.append(f'<text x="{x + wcol/2}" y="{load_y + 16}" font-size="11" text-anchor="middle" fill="#7a0000">NO ME</text>')

        i += span

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
    st.session_state.matrix = make_empty_matrix(4, None)
if "selected_commodity" not in st.session_state:
    st.session_state.selected_commodity = "(Select)"
if "selected_facility" not in st.session_state:
    st.session_state.selected_facility = "(All facilities)"


# =============================
# Sidebar
# =============================
with st.sidebar:
    st.header("Settings")
    car_id = st.text_input("Car ID", value="TBOX632012")

    st.divider()
    st.header("Tiers")
    max_tiers = st.slider("Max tiers per spot", 1, 8, 4)

    st.divider()
    st.header("Doorway / Airbag")
    auto_airbag = st.checkbox("Auto airbag (prefer <= 9\")", value=True)

    gap_labels = [f"{a}–{b}" for a, b in AIRBAG_ALLOWED_GAPS]
    gap_choice_label = st.selectbox("Airbag location (manual)", gap_labels, index=1, disabled=auto_airbag)
    airbag_gap_in = st.slider("Airbag gap (in) (manual)", 6.0, 9.0, 9.0, 0.5, disabled=auto_airbag)

    unit_length_ref_in = st.number_input("Unit length ref (in) for gap drawing", min_value=1.0, value=96.0, step=1.0)

    st.divider()
    st.header("Forklift turn")
    # IMPORTANT: turn consumes 2 spots (N and N+1)
    turn_spot_label = st.selectbox("Turn (horizontal) spot (consumes 2 spots)", ["None", "7", "8"], index=1)
    turn_spot = None if turn_spot_label == "None" else int(turn_spot_label)

    if turn_spot is not None:
        b = blocked_spot_for_turn(turn_spot)
        st.caption(f"Turn spans spots {turn_spot}–{b}. Spot {b} will be blocked.")

    st.divider()
    st.header("Balancing preferences")
    preferred_side = st.selectbox("Outside-doorway side preference", ["A", "B"], index=0)
    center_end = st.selectbox("Center one end unit (Top view)", ["None", "Spot 1", "Spot 15"], index=2)

    st.divider()
    view_mode = st.radio("View", ["Top + Both Sides", "Top only", "Sides only"], index=0)


# =============================
# Filters
# =============================
st.success(f"Product Master loaded: {len(pm):,} rows")

commodities = sorted(pm[COL_COMMODITY].dropna().astype(str).unique().tolist())
commodity_selected = st.selectbox("Commodity / Product Type (required)", ["(Select)"] + commodities)

if commodity_selected != st.session_state.selected_commodity:
    st.session_state.selected_commodity = commodity_selected
    st.session_state.selected_facility = "(All facilities)"
    st.session_state.requests = []
    st.session_state.matrix = make_empty_matrix(int(max_tiers), turn_spot)

if commodity_selected == "(Select)":
    st.info("Select a Commodity/Product Type to proceed.")
    st.stop()

pm_c = pm[pm[COL_COMMODITY].astype(str) == str(commodity_selected)].copy()

facilities = sorted(pm_c[COL_FACILITY].dropna().astype(str).unique().tolist()) if COL_FACILITY in pm_c.columns else []
facility_selected = st.selectbox("Facility Id (filtered by commodity)", ["(All facilities)"] + facilities)

if facility_selected != st.session_state.selected_facility:
    st.session_state.selected_facility = facility_selected
    st.session_state.requests = []
    st.session_state.matrix = make_empty_matrix(int(max_tiers), turn_spot)

pm_cf = pm_c.copy()
if facility_selected != "(All facilities)" and COL_FACILITY in pm_cf.columns:
    pm_cf = pm_cf[pm_cf[COL_FACILITY].astype(str) == str(facility_selected)].copy()

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

    if COL_HALF_PACK in pm_cf.columns:
        hp = " HP" if _truthy(r.get(COL_HALF_PACK, "")) else ""
    else:
        hp = " HP" if desc.upper().rstrip().endswith("HP") else ""

    parts = [pid + hp]
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
    st.session_state.matrix = make_empty_matrix(int(max_tiers), turn_spot)

if add_line and selected_label:
    idx = labels.index(selected_label)
    pid = str(options[idx][COL_PRODUCT_ID]).strip()
    st.session_state.requests.append(RequestLine(product_id=pid, tiers=int(tiers_to_add)))

st.subheader("Requested SKUs (tiers)")

products: Dict[str, Product] = {}
for r in st.session_state.requests:
    try:
        products[r.product_id] = lookup_product(pm, r.product_id)
    except Exception as e:
        st.error(f"Could not lookup SKU {r.product_id}: {e}")

if st.session_state.requests:
    req_rows = []
    for r in st.session_state.requests:
        p = products.get(r.product_id)
        req_rows.append(
            {"Sales Product Id": r.product_id, "Description": (p.description if p else ""), "Tiers": r.tiers}
        )
    st.dataframe(pd.DataFrame(req_rows), use_container_width=True, height=200)
else:
    st.info("Add one or more SKU lines, then click **Optimize Layout**.")

messages: List[str] = []
if optimize_btn:
    st.session_state.matrix = make_empty_matrix(int(max_tiers), turn_spot)
    if not st.session_state.requests:
        st.warning("No request lines to optimize.")
    else:
        matrix, msgs = optimize_layout(
            products=products,
            requests=st.session_state.requests,
            max_tiers_per_spot=int(max_tiers),
            preferred_side_outside=str(preferred_side),
            turn_spot=turn_spot,
        )
        st.session_state.matrix = matrix
        messages.extend(msgs)

for m in messages:
    st.warning(m)

matrix = st.session_state.matrix

# Auto airbag after we have matrix (if enabled)
if auto_airbag:
    airbag_gap_choice, airbag_gap_in = auto_airbag_choice(matrix)
else:
    airbag_gap_choice = AIRBAG_ALLOWED_GAPS[[f"{a}–{b}" for a, b in AIRBAG_ALLOWED_GAPS].index(gap_choice_label)]

# Payload / placed tiers
payload = 0.0
placed = 0
for spot in range(1, FLOOR_SPOTS + 1):
    for pid in matrix[spot - 1]:
        if pid is None or pid == BLOCK:
            continue
        payload += float(products[pid].unit_weight_lbs) if pid in products else 0.0
        placed += 1

st.subheader("Summary")
st.metric("Payload (lbs)", f"{payload:,.0f}")
st.metric("Placed tiers", f"{placed:,} / {FLOOR_SPOTS*int(max_tiers):,}")

top_half = count_top_halfpacks(matrix, products)
if top_half > 0:
    st.warning(f"Soft rule: {top_half} Half Pack(s) ended up on the TOP tier (allowed).")

# Hard Machine Edge violations (doorframe 6/9, and turn span touching 9)
for spot in range(1, FLOOR_SPOTS + 1):
    if is_blocked_spot(spot, turn_spot):
        continue
    for pid in matrix[spot - 1]:
        if not pid or pid == BLOCK:
            continue
        if pid in products and products[pid].is_machine_edge:
            ok, why = can_place_pid_hard(products, pid, spot, turn_spot)
            if not ok:
                st.error(f"HARD violation: {pid} in Spot {spot}: {why}")

note = (
    f"Commodity: {commodity_selected} | Facility: {facility_selected} | "
    f"Doorway: {DOOR_START_SPOT}–{DOOR_END_SPOT} (no stagger) | "
    f"Airbag: {airbag_gap_choice[0]}–{airbag_gap_choice[1]} @ {float(airbag_gap_in):.1f}\" | "
    f"PIN: Machine Edge prefers 7/8 | Half Pack top = soft"
)
if turn_spot is not None and blocked_spot_for_turn(turn_spot) is not None:
    note += f" | Turn spans {turn_spot}–{blocked_spot_for_turn(turn_spot)} (consumes 2 spots)"

top_svg = render_top_svg(
    car_id=car_id,
    matrix=matrix,
    products=products,
    note=note,
    airbag_gap_in=float(airbag_gap_in),
    airbag_gap_choice=airbag_gap_choice,
    unit_length_ref_in=float(unit_length_ref_in),
    center_end=str(center_end),
    turn_spot=turn_spot,
)

side1 = render_side_loadxpert_svg(
    car_id=car_id,
    matrix=matrix,
    products=products,
    title="Side 1 (Load Xpert style)",
    airbag_gap_choice=airbag_gap_choice,
    airbag_gap_in=float(airbag_gap_in),
    unit_length_ref_in=float(unit_length_ref_in),
    turn_spot=turn_spot,
)

side2 = render_side_loadxpert_svg(
    car_id=car_id,
    matrix=matrix,
    products=products,
    title="Side 2 (Load Xpert style)",
    airbag_gap_choice=airbag_gap_choice,
    airbag_gap_in=float(airbag_gap_in),
    unit_length_ref_in=float(unit_length_ref_in),
    turn_spot=turn_spot,
)

st.subheader("Diagram View")

TOP_HEIGHT = 340
SIDE_HEIGHT = 585

if view_mode == "Top only":
    components_svg(top_svg, height=TOP_HEIGHT)
elif view_mode == "Sides only":
    components_svg(side1, height=SIDE_HEIGHT)
    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
    components_svg(side2, height=SIDE_HEIGHT)
else:
    components_svg(top_svg, height=TOP_HEIGHT)
    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
    components_svg(side1, height=SIDE_HEIGHT)
    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
    components_svg(side2, height=SIDE_HEIGHT)
