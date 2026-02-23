# streamlit_app.py
# Load Diagram Optimizer — Load Xpert-style PDF layout + CG_above_TOR integration (UI Spec v1.1)
# Copy/paste into GitHub as-is.

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Iterable

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


# =============================
# Page
# =============================
st.set_page_config(page_title="Load Diagram Optimizer", layout="wide")
st.title("Load Diagram Optimizer")

# Update if your repo stores the master elsewhere
MASTER_PATH = "data/Ortec SP Product Master.xlsx"

# Product Master columns (must match your Excel)
COL_COMMODITY = "Product Type"
COL_FACILITY = "Facility Id"
COL_PRODUCT_ID = "Sales Product Id"
COL_DESC = "Short Descrip"
COL_ACTIVE = "Active"
COL_UNIT_H = "Unit Height (In)"
COL_UNIT_WT = "Unit Weight (lbs)"
COL_EDGE = "Edge Type"

# Optional
COL_HALF_PACK = "Half Pack"
COL_THICK = "Panel Thickness"
COL_WIDTH = "Width"
COL_LENGTH = "Length"
COL_PIECECOUNT = "Piece Count"

BLOCK = "__BLOCK__"  # internal marker


# =============================
# Load Xpert-style constants
# =============================
FLOOR_SPOTS_BOXCAR = 15
FLOOR_SPOTS_CENTERBEAM = 18

# Doorway conventions (boxcar)
DOOR_START_SPOT = 6
DOOR_END_SPOT = 9

DOORFRAME_NO_ME = {6, 9}     # Machine Edge not allowed here
DOORPOCKET_PINS = {7, 8}     # Machine Edge allowed/preferred here

AIRBAG_ALLOWED_GAPS = [(6, 7), (7, 8), (8, 9)]

# CG_above_TOR thresholds (configurable)
CG_THRESHOLDS_IN = {
    "boxcar": {"preferred_lt": 105.0, "caution_le": 115.0},
    "centerbeam": {"preferred_lt": 105.0, "caution_le": 115.0},
}

# Load Xpert-like colors
LX_BLUE = "#0b2a7a"
LX_RED = "#c00000"
LX_RED2 = "#d00000"
LX_GRID = "#000000"
LX_TEXT = "#111111"


# =============================
# Models
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

    thickness: Optional[float] = None
    width: Optional[float] = None
    length: Optional[float] = None
    piece_count: Optional[float] = None

    @property
    def is_machine_edge(self) -> bool:
        return "machine" in (self.edge_type or "").strip().lower()


@dataclass
class RequestLine:
    product_id: str
    tiers: int


# =============================
# Data
# =============================
def _truthy(v) -> bool:
    s = str(v).strip().upper()
    return s in ("Y", "YES", "TRUE", "1", "T")


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
        raise ValueError(f"Missing required columns: {missing}")

    df[COL_PRODUCT_ID] = df[COL_PRODUCT_ID].astype(str).str.strip()
    df[COL_COMMODITY] = df[COL_COMMODITY].astype(str).str.strip()
    df[COL_EDGE] = df[COL_EDGE].astype(str).str.strip()

    if COL_FACILITY in df.columns:
        df[COL_FACILITY] = df[COL_FACILITY].astype(str).str.strip()
    if COL_DESC in df.columns:
        df[COL_DESC] = df[COL_DESC].astype(str)

    for c in [COL_UNIT_H, COL_UNIT_WT, COL_THICK, COL_WIDTH, COL_LENGTH, COL_PIECECOUNT]:
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

    desc = str(r[COL_DESC]).strip() if COL_DESC in df.columns else ""
    if COL_HALF_PACK in df.columns:
        is_hp = _truthy(r.get(COL_HALF_PACK, ""))
    else:
        is_hp = desc.upper().rstrip().endswith("HP")

    def opt_num(col: str) -> Optional[float]:
        if col in df.columns:
            v = r.get(col)
            if pd.notna(v):
                return float(v)
        return None

    return Product(
        product_id=pid,
        commodity=str(r[COL_COMMODITY]).strip(),
        facility_id=str(r[COL_FACILITY]).strip() if COL_FACILITY in df.columns else "",
        description=desc,
        edge_type=str(r[COL_EDGE]).strip(),
        unit_height_in=float(r[COL_UNIT_H]),
        unit_weight_lbs=float(r[COL_UNIT_WT]),
        is_half_pack=bool(is_hp),
        thickness=opt_num(COL_THICK),
        width=opt_num(COL_WIDTH),
        length=opt_num(COL_LENGTH),
        piece_count=opt_num(COL_PIECECOUNT),
    )


# =============================
# Turn rules (boxcar)
# =============================
def blocked_spot_for_turn(turn_spot: int) -> int:
    return turn_spot + 1


def is_blocked_spot(spot: int, turn_spot: int) -> bool:
    return spot == blocked_spot_for_turn(turn_spot)


def occupied_spots_for_placement(spot: int, turn_spot: int) -> List[int]:
    if spot == turn_spot:
        return [spot, blocked_spot_for_turn(turn_spot)]
    return [spot]


# =============================
# Placement matrix
# =============================
def make_empty_matrix(max_tiers: int, turn_spot: int, floor_spots: int) -> List[List[Optional[str]]]:
    m = [[None for _ in range(max_tiers)] for _ in range(floor_spots)]
    b = blocked_spot_for_turn(turn_spot)
    if 1 <= b <= floor_spots:
        for t in range(max_tiers):
            m[b - 1][t] = BLOCK  # visually blocked
    return m


def next_empty_tier_index(matrix: List[List[Optional[str]]], spot: int, turn_spot: int) -> Optional[int]:
    if is_blocked_spot(spot, turn_spot):
        return None
    occ = occupied_spots_for_placement(spot, turn_spot)
    tiers = len(matrix[0])
    for t in range(tiers):
        if all(matrix[s - 1][t] is None for s in occ):
            return t
    return None


def place_pid(matrix: List[List[Optional[str]]], spot: int, tier_idx: int, pid: str, turn_spot: int) -> None:
    for s in occupied_spots_for_placement(spot, turn_spot):
        matrix[s - 1][tier_idx] = pid


# =============================
# Rules (hard + soft)
# =============================
def can_place_hard(products: Dict[str, Product], pid: str, spot: int, turn_spot: int) -> Tuple[bool, str]:
    p = products[pid]
    occ = occupied_spots_for_placement(spot, turn_spot)

    if p.is_machine_edge and any(s in DOORFRAME_NO_ME for s in occ):
        return False, "Machine Edge not allowed in doorframe spots 6/9 (or any placement that touches them)."

    return True, ""


def soft_penalty(products: Dict[str, Product], pid: str, tier_idx: int, max_tiers: int) -> int:
    p = products[pid]
    penalty = 0
    if p.is_half_pack and tier_idx == (max_tiers - 1):
        penalty += 100
    return penalty


# =============================
# Optimization helpers
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


def pop_best(
    tokens: List[str],
    products: Dict[str, Product],
    spot: int,
    tier_idx: int,
    max_tiers: int,
    turn_spot: int,
) -> Optional[str]:
    best_i = None
    best_score = None
    for i, pid in enumerate(tokens):
        ok, _ = can_place_hard(products, pid, spot, turn_spot)
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


def force_turn_tiers(
    matrix: List[List[Optional[str]]],
    products: Dict[str, Product],
    heavy: List[str],
    light: List[str],
    max_tiers: int,
    turn_spot: int,
    required_turn_tiers: int,
    msgs: List[str],
) -> None:
    required_turn_tiers = max(0, min(required_turn_tiers, max_tiers))
    for t in range(required_turn_tiers):
        if matrix[turn_spot - 1][t] not in (None, BLOCK):
            continue

        pref = "heavy" if (t % 2 == 0) else "light"
        pid = None
        if pref == "heavy":
            pid = pop_best(heavy, products, turn_spot, t, max_tiers, turn_spot) or pop_best(
                light, products, turn_spot, t, max_tiers, turn_spot
            )
        else:
            pid = pop_best(light, products, turn_spot, t, max_tiers, turn_spot) or pop_best(
                heavy, products, turn_spot, t, max_tiers, turn_spot
            )

        if pid is None:
            msgs.append(f"TURN HARD RULE: could not place a legal tier into TURN spot at Tier {t+1}.")
            return

        place_pid(matrix, turn_spot, t, pid, turn_spot)


def optimize_layout(
    products: Dict[str, Product],
    requests: List[RequestLine],
    max_tiers: int,
    turn_spot: int,
    required_turn_tiers: int,
    floor_spots: int,
) -> Tuple[List[List[Optional[str]]], List[str]]:
    msgs: List[str] = []
    heavy, light = build_token_lists(products, requests)
    matrix = make_empty_matrix(max_tiers, turn_spot, floor_spots)

    # Turn rules apply only on 15-spot boxcar logic
    if floor_spots == FLOOR_SPOTS_BOXCAR:
        force_turn_tiers(matrix, products, heavy, light, max_tiers, turn_spot, required_turn_tiers, msgs)

    # Fill order
    if floor_spots == FLOOR_SPOTS_BOXCAR:
        outside = [1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15]
        doorway = [7, 8, 6, 9]
        order = [s for s in outside + doorway if not is_blocked_spot(s, turn_spot)]
    else:
        order = [s for s in range(1, floor_spots + 1) if not is_blocked_spot(s, turn_spot)]

    def tier_pref(t: int) -> str:
        return "heavy" if t % 2 == 0 else "light"

    while heavy or light:
        placed_any = False

        for spot in order:
            t = next_empty_tier_index(matrix, spot, turn_spot)
            if t is None:
                continue

            pref = tier_pref(t)
            pid = None
            if pref == "heavy":
                pid = pop_best(heavy, products, spot, t, max_tiers, turn_spot) or pop_best(
                    light, products, spot, t, max_tiers, turn_spot
                )
            else:
                pid = pop_best(light, products, spot, t, max_tiers, turn_spot) or pop_best(
                    heavy, products, spot, t, max_tiers, turn_spot
                )

            if pid is None:
                continue

            # Pin preference for machine-edge in 7/8 (boxcar only)
            if floor_spots == FLOOR_SPOTS_BOXCAR and products[pid].is_machine_edge:
                for pin_spot in [7, 8]:
                    if is_blocked_spot(pin_spot, turn_spot):
                        continue
                    tpin = next_empty_tier_index(matrix, pin_spot, turn_spot)
                    if tpin == t:
                        ok, _ = can_place_hard(products, pid, pin_spot, turn_spot)
                        if ok:
                            spot = pin_spot
                            break

            ok, why = can_place_hard(products, pid, spot, turn_spot)
            if not ok:
                msgs.append(f"Skipped {pid} at spot {spot}, tier {t+1}: {why}")
                continue

            place_pid(matrix, spot, t, pid, turn_spot)
            placed_any = True

        if not placed_any:
            break

    remaining = len(heavy) + len(light)
    if remaining:
        msgs.append(f"{remaining} tiers could not be placed (capacity/rules).")

    return matrix, msgs


# =============================
# Rendering helpers
# =============================
def svg_escape(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def color_for_pid(pid: str) -> str:
    palette = ["#d9ecff", "#ffe3d9", "#e6ffd9", "#f2e6ff", "#fff5cc", "#d9fff7", "#ffd9f1", "#e0e0ff"]
    h = 0
    for ch in pid:
        h = (h * 31 + ord(ch)) % 10_000
    return palette[h % len(palette)]


def hatch_pattern_defs() -> str:
    return """
    <defs>
      <pattern id="hx_diag" patternUnits="userSpaceOnUse" width="10" height="10" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="10" stroke="#000" stroke-width="2" opacity="0.18"/>
      </pattern>
      <pattern id="hx_vert" patternUnits="userSpaceOnUse" width="10" height="10">
        <line x1="2" y1="0" x2="2" y2="10" stroke="#000" stroke-width="2" opacity="0.18"/>
      </pattern>
    </defs>
    """


def draw_ruler(svg: list[str], x: float, y: float, w: float, ticks: int = 70) -> None:
    svg.append(f'<line x1="{x}" y1="{y}" x2="{x+w}" y2="{y}" stroke="{LX_BLUE}" stroke-width="2"/>')
    for i in range(ticks + 1):
        tx = x + (w * i / ticks)
        h = 10 if i % 5 == 0 else 6
        svg.append(f'<line x1="{tx}" y1="{y}" x2="{tx}" y2="{y+h}" stroke="{LX_BLUE}" stroke-width="1"/>')


def components_svg(svg: str, height: int) -> None:
    components.html(f"<div style='width:100%;overflow:visible'>{svg}</div>", height=height, scrolling=False)


def auto_airbag_choice() -> Tuple[Tuple[int, int], float]:
    return (7, 8), 6.0


# =============================
# Unique placement iteration (dedupe TURN span)
# =============================
def iter_unique_placements(
    matrix: List[List[Optional[str]]],
    *,
    turn_spot: int,
    floor_spots: int,
) -> Iterable[Tuple[int, int, str]]:
    blocked = blocked_spot_for_turn(turn_spot)
    tiers = len(matrix[0]) if matrix else 0

    for spot in range(1, floor_spots + 1):
        col = matrix[spot - 1]
        for t in range(tiers):
            pid = col[t]
            if pid is None or pid == BLOCK:
                continue

            if floor_spots == FLOOR_SPOTS_BOXCAR and spot == blocked:
                pid_turn = matrix[turn_spot - 1][t]
                if pid_turn == pid:
                    continue

            yield spot, t, pid


# =============================
# CG_above_TOR + stack stats
# =============================
def compute_payload_and_stack_stats(
    matrix: List[List[Optional[str]]],
    products: Dict[str, Product],
    *,
    turn_spot: int,
    floor_spots: int,
) -> Tuple[float, int, float, float, Dict[int, Dict[str, float]]]:
    per_spot: Dict[int, Dict[str, float]] = {s: {"stack_h": 0.0, "spot_wt": 0.0, "tiers": 0.0} for s in range(1, floor_spots + 1)}
    payload = 0.0
    placed = 0

    for spot, _, pid in iter_unique_placements(matrix, turn_spot=turn_spot, floor_spots=floor_spots):
        p = products.get(pid)
        if not p:
            continue
        payload += p.unit_weight_lbs
        placed += 1
        per_spot[spot]["stack_h"] += p.unit_height_in
        per_spot[spot]["spot_wt"] += p.unit_weight_lbs
        per_spot[spot]["tiers"] += 1.0

    # Weighted average stack height by spot payload
    num = 0.0
    den = 0.0
    for s in range(1, floor_spots + 1):
        sh = per_spot[s]["stack_h"]
        sw = per_spot[s]["spot_wt"]
        if sh > 0 and sw > 0:
            num += sh * sw
            den += sw
    avg_stack_weighted = (num / den) if den > 0 else 0.0

    # Simple average across non-empty spots (fallback/reference)
    sh_list = [per_spot[s]["stack_h"] for s in range(1, floor_spots + 1) if per_spot[s]["stack_h"] > 0]
    avg_stack_simple = (sum(sh_list) / len(sh_list)) if sh_list else 0.0

    return payload, placed, avg_stack_weighted, avg_stack_simple, per_spot


def cg_above_tor(
    *,
    deck_height_TOR_in: float,
    empty_car_CG_TOR_in: float,
    tare_weight_lbs: float,
    spring_deflection_in: float,
    payload_weight_lbs: float,
    avg_stack_height_in: float,
) -> Tuple[Optional[float], Dict[str, float]]:
    needed = [deck_height_TOR_in, empty_car_CG_TOR_in, tare_weight_lbs, payload_weight_lbs]
    if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in needed):
        return None, {}
    if tare_weight_lbs <= 0 or payload_weight_lbs <= 0:
        return None, {}

    A = float(deck_height_TOR_in) - float(spring_deflection_in)
    C = float(avg_stack_height_in) / 2.0 if avg_stack_height_in > 0 else 0.0
    E = float(tare_weight_lbs)
    F = float(payload_weight_lbs)
    B = float(empty_car_CG_TOR_in)

    cg = ((B * E) + ((A + C) * F)) / (E + F)
    trace = {"A": A, "B": B, "C": C, "E": E, "F": F, "A_plus_C": (A + C)}
    return cg, trace


def cg_tier(cg_in: Optional[float], *, car_type_key: str) -> Tuple[str, str]:
    if cg_in is None:
        return "N/A", "⚪ N/A"
    th = CG_THRESHOLDS_IN.get(car_type_key, CG_THRESHOLDS_IN["boxcar"])
    if cg_in < th["preferred_lt"]:
        return "Preferred", "🟢 Preferred"
    if cg_in <= th["caution_le"]:
        return "Caution", "🟡 Caution"
    return "High Risk", "🔴 High Risk"


# =============================
# Validation
# =============================
def validate_airbag(airbag_gap_in: float) -> Tuple[str, str]:
    if airbag_gap_in < 6.0:
        return "FAIL", f'Airbag Space {airbag_gap_in:.1f}" is below 6.0" (too tight).'
    if airbag_gap_in <= 9.0:
        return "PASS", f'Airbag Space {airbag_gap_in:.1f}" is within preferred 6.0–9.0".'
    if airbag_gap_in <= 10.0:
        return "WARN", f'Airbag Space {airbag_gap_in:.1f}" is above preferred (6.0–9.0") but ≤ 10.0".'
    return "FAIL", f'Airbag Space {airbag_gap_in:.1f}" exceeds 10.0" (not acceptable).'


def validate_centerbeam_symmetry(per_spot_stats: Dict[int, Dict[str, float]]) -> Tuple[str, str]:
    spots = sorted(per_spot_stats.keys())
    mid = len(spots) // 2
    left = sum(per_spot_stats[s]["spot_wt"] for s in spots[:mid])
    right = sum(per_spot_stats[s]["spot_wt"] for s in spots[mid:])
    total = left + right
    if total <= 0:
        return "WARN", "Symmetry check: no payload detected."
    diff_pct = abs(left - right) / total * 100.0
    if diff_pct <= 5.0:
        return "PASS", f"Symmetry: left/right weight delta {diff_pct:.1f}% (≤ 5%)."
    if diff_pct <= 10.0:
        return "WARN", f"Symmetry: left/right weight delta {diff_pct:.1f}% (5–10%)."
    return "FAIL", f"Symmetry: left/right weight delta {diff_pct:.1f}% (> 10%)."


# =============================
# Load Xpert-like PAGE renderer (Top + Side 1/2 + engineering + item table + legend)
# =============================
def render_loadxpert_top_side_page_svg(
    *,
    page_title: str,              # "Top + Side 1 View" / "Top + Side 2 View"
    created_by: str,
    created_at: str,
    order_number: str,
    vehicle_number: str,
    po_number: str,
    car_id: str,
    matrix: List[List[Optional[str]]],
    products: Dict[str, Product],
    floor_spots: int,
    turn_spot: int,
    airbag_gap_choice: Tuple[int, int],
    airbag_gap_in: float,
    floor_spots_text: str,
    cg_height_text: str,
    cg_above_tor_text: str,
    airbag_space_text: str,
    wue_text: str,
    lisa_text: str,
    item_rows: List[dict],
    side_flip: bool = False,
) -> str:
    W, H = 1400, 980
    M = 20

    header_h = 125
    top_block_h = 175
    side_block_h = 265
    eng_h = 70
    item_h = 170
    legend_h = 85

    y = M

    svg: list[str] = []
    svg.append(f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">')
    svg.append(hatch_pattern_defs())

    svg.append(f'<rect x="{M}" y="{M}" width="{W-2*M}" height="{H-2*M}" fill="white" stroke="{LX_GRID}" stroke-width="2"/>')

    svg.append(f'<text x="{W/2}" y="{y+22}" font-size="20" font-weight="700" text-anchor="middle">{svg_escape(page_title)}</text>')
    y += 30

    # Header table
    hx = M + 10
    hy = y
    hw = W - 2*M - 20
    hh = header_h - 35

    cols = [0.14, 0.22, 0.20, 0.22, 0.22]
    cx = [hx]
    for frac in cols[:-1]:
        cx.append(cx[-1] + hw * frac)

    svg.append(f'<rect x="{hx}" y="{hy}" width="{hw}" height="{hh}" fill="white" stroke="{LX_GRID}" stroke-width="2"/>')
    for xline in cx[1:]:
        svg.append(f'<line x1="{xline}" y1="{hy}" x2="{xline}" y2="{hy+hh}" stroke="{LX_GRID}" stroke-width="2"/>')
    mid = hy + hh * 0.55
    svg.append(f'<line x1="{hx}" y1="{mid}" x2="{hx+hw}" y2="{mid}" stroke="{LX_GRID}" stroke-width="2"/>')

    headers = ["Created By", "Created At", "Order Number", "Vehicle Number", "PO Number"]
    values = [created_by, created_at, order_number, vehicle_number, po_number]
    for i in range(5):
        x0 = cx[i]
        svg.append(f'<text x="{x0+10}" y="{hy+24}" font-size="16" font-weight="700">{headers[i]}</text>')
        svg.append(f'<text x="{x0+10}" y="{mid+30}" font-size="18">{svg_escape(values[i])}</text>')

    y = hy + hh + 18

    # TOP strip
    top_x = M + 80
    top_y = y + 10
    top_w = W - 2*M - 160
    top_h = top_block_h - 30

    svg.append(f'<text x="{W/2}" y="{top_y-5}" font-size="18" font-weight="700" text-anchor="middle">Top</text>')
    svg.append(f'<rect x="{top_x}" y="{top_y}" width="{top_w}" height="{top_h}" fill="white" stroke="{LX_BLUE}" stroke-width="3"/>')
    draw_ruler(svg, top_x, top_y - 14, top_w, ticks=70)

    def rep_pid_for_spot(s: int) -> Optional[str]:
        col = matrix[s - 1]
        return next((pid for pid in col if pid and pid != BLOCK), None)

    spot_order = list(range(1, floor_spots + 1))
    if side_flip:
        spot_order = list(reversed(spot_order))

    cell_w = top_w / floor_spots

    for idx, spot in enumerate(spot_order):
        pid = rep_pid_for_spot(spot)
        x = top_x + idx * cell_w
        fill = "#ffffff" if pid is None else color_for_pid(pid)
        svg.append(f'<rect x="{x}" y="{top_y}" width="{cell_w}" height="{top_h}" fill="{fill}" stroke="{LX_BLUE}" stroke-width="1"/>')
        if pid:
            svg.append(
                f'<text x="{x + cell_w/2}" y="{top_y + top_h/2}" font-size="22" font-weight="700" text-anchor="middle" '
                f'transform="rotate(-90 {x + cell_w/2} {top_y + top_h/2})">{svg_escape(pid)}</text>'
            )

    # Doorway + airbag + turn hatch for boxcar
    if floor_spots == FLOOR_SPOTS_BOXCAR:
        door_left = top_x + (DOOR_START_SPOT - 1) * cell_w
        door_right = top_x + DOOR_END_SPOT * cell_w
        svg.append(f'<rect x="{door_left}" y="{top_y}" width="{door_right-door_left}" height="{top_h}" fill="white" opacity="0.55"/>')
        svg.append(f'<rect x="{door_left}" y="{top_y}" width="{door_right-door_left}" height="{top_h}" fill="none" stroke="{LX_RED}" stroke-width="3"/>')
        svg.append(f'<text x="{door_left+6}" y="{top_y-6}" font-size="12" fill="{LX_RED}">Doorway (Spots {DOOR_START_SPOT}-{DOOR_END_SPOT})</text>')

        a, _b = airbag_gap_choice
        air_x = top_x + a * cell_w
        svg.append(f'<rect x="{air_x-3}" y="{top_y}" width="6" height="{top_h}" fill="{LX_RED2}" opacity="0.95"/>')
        svg.append(f'<text x="{air_x}" y="{top_y+top_h+18}" font-size="12" fill="{LX_RED2}" text-anchor="middle">Airbag {airbag_gap_in:.1f}"</text>')

        # Turn hatch region
        blocked = blocked_spot_for_turn(turn_spot)
        tzx = top_x + (turn_spot - 1) * cell_w
        svg.append(f'<rect x="{tzx}" y="{top_y}" width="{cell_w*2}" height="{top_h}" fill="url(#hx_diag)" opacity="0.45" stroke="#111" stroke-width="1"/>')
        svg.append(f'<text x="{tzx + cell_w}" y="{top_y+16}" font-size="12" text-anchor="middle">FORKLIFT TURN ({turn_spot}-{blocked})</text>')

    y = top_y + top_h + 40

    # SIDE strip
    side_x = M + 80
    side_y = y
    side_w = W - 2*M - 160
    side_h = side_block_h

    side_label = "Side2" if side_flip else "Side1"
    svg.append(f'<text x="{W/2}" y="{side_y-10}" font-size="18" font-weight="700" text-anchor="middle">{side_label}</text>')
    svg.append(f'<rect x="{side_x}" y="{side_y}" width="{side_w}" height="{side_h}" fill="white" stroke="{LX_BLUE}" stroke-width="3"/>')

    wheel_y = side_y + side_h - 22
    for cxw in [side_x + side_w * 0.15, side_x + side_w * 0.22, side_x + side_w * 0.78, side_x + side_w * 0.85]:
        svg.append(f'<circle cx="{cxw}" cy="{wheel_y}" r="14" fill="#666" opacity="0.6"/>')

    inset = 10
    lx = side_x + inset
    ly = side_y + inset
    lw = side_w - 2 * inset
    lh = side_h - 2 * inset - 35

    tiers = len(matrix[0]) if matrix else 0
    tiers = max(1, tiers)

    cw = lw / floor_spots
    ch = lh / tiers

    if floor_spots == FLOOR_SPOTS_BOXCAR:
        door_left = lx + (DOOR_START_SPOT - 1) * cw
        door_right = lx + DOOR_END_SPOT * cw
        svg.append(f'<rect x="{door_left}" y="{ly}" width="{door_right-door_left}" height="{lh}" fill="white" opacity="0.55"/>')
        svg.append(f'<rect x="{door_left}" y="{ly}" width="{door_right-door_left}" height="{lh}" fill="none" stroke="{LX_RED}" stroke-width="3"/>')

        a, _b = airbag_gap_choice
        air_x = lx + a * cw
        svg.append(f'<rect x="{air_x-3}" y="{ly}" width="6" height="{lh}" fill="{LX_RED2}" opacity="0.95"/>')

    blocked = blocked_spot_for_turn(turn_spot)

    for idx, spot in enumerate(spot_order):
        x = lx + idx * cw
        svg.append(f'<rect x="{x}" y="{ly}" width="{cw}" height="{lh}" fill="none" stroke="#333" stroke-width="1" opacity="0.6"/>')
        svg.append(f'<text x="{x + cw/2}" y="{ly + lh + 24}" font-size="12" text-anchor="middle" fill="#333">{spot}</text>')

        col = matrix[spot - 1]
        for t in range(tiers):
            pid = col[t]
            if pid is None or pid == BLOCK:
                continue

            if floor_spots == FLOOR_SPOTS_BOXCAR and spot == blocked:
                pid_turn = matrix[turn_spot - 1][t]
                if pid_turn == pid:
                    continue

            ycell = ly + lh - (t + 1) * ch
            fill = color_for_pid(pid)
            svg.append(f'<rect x="{x+1}" y="{ycell+1}" width="{cw-2}" height="{ch-2}" fill="{fill}" stroke="#111" stroke-width="1"/>')
            svg.append(f'<text x="{x + cw/2}" y="{ycell + ch/2 + 5}" font-size="12" text-anchor="middle">{svg_escape(pid)}</text>')

    y = side_y + side_h + 16

    # Engineering row
    ex = M + 20
    ey = y
    ew = W - 2*M - 40
    eh = eng_h

    svg.append(f'<rect x="{ex}" y="{ey}" width="{ew}" height="{eh}" fill="white" stroke="{LX_GRID}" stroke-width="2"/>')
    ecols = [0.14, 0.16, 0.18, 0.16, 0.18, 0.18]  # add CG_above_TOR column
    exs = [ex]
    for frac in ecols[:-1]:
        exs.append(exs[-1] + ew * frac)
    for xline in exs[1:]:
        svg.append(f'<line x1="{xline}" y1="{ey}" x2="{xline}" y2="{ey+eh}" stroke="{LX_GRID}" stroke-width="1.5"/>')

    # Top line labels
    labels = ["Floor spots =", "C.G. height =", "CG_above_TOR =", "Airbag Space =", "Whole Unit Equivalent =", "Total LISA Units ="]
    values = [floor_spots_text, cg_height_text, cg_above_tor_text, airbag_space_text, wue_text, lisa_text]

    for i in range(6):
        x0 = exs[i]
        svg.append(f'<text x="{x0+10}" y="{ey+24}" font-size="14" font-weight="700">{labels[i]}</text>')
        svg.append(f'<text x="{x0+150}" y="{ey+24}" font-size="14">{svg_escape(values[i])}</text>')

    y = ey + eh + 10

    # Item table
    tx = M + 20
    ty = y
    tw = W - 2*M - 40
    th = item_h

    svg.append(f'<rect x="{tx}" y="{ty}" width="{tw}" height="{th}" fill="white" stroke="{LX_GRID}" stroke-width="2"/>')

    col_fracs = [0.10, 0.08, 0.62, 0.20]
    xs = [tx]
    for frac in col_fracs[:-1]:
        xs.append(xs[-1] + tw * frac)
    for xline in xs[1:]:
        svg.append(f'<line x1="{xline}" y1="{ty}" x2="{xline}" y2="{ty+th}" stroke="{LX_GRID}" stroke-width="1.5"/>')

    rows = max(1, len(item_rows))
    rh = th / (rows + 1)
    for r in range(1, rows + 1):
        yline = ty + r * rh
        svg.append(f'<line x1="{tx}" y1="{yline}" x2="{tx+tw}" y2="{yline}" stroke="{LX_GRID}" stroke-width="1.0"/>')

    svg.append(f'<text x="{tx+10}" y="{ty+rh*0.7}" font-size="14" font-weight="700">ITEM</text>')
    svg.append(f'<text x="{xs[1]+10}" y="{ty+rh*0.7}" font-size="14" font-weight="700">Code</text>')
    svg.append(f'<text x="{xs[2]+10}" y="{ty+rh*0.7}" font-size="14" font-weight="700">Description</text>')
    svg.append(f'<text x="{xs[3]+10}" y="{ty+rh*0.7}" font-size="14" font-weight="700">Product Id</text>')

    for i, r in enumerate(item_rows[:6]):
        yy = ty + (i + 1) * rh + rh * 0.7
        svg.append(f'<text x="{tx+10}" y="{yy}" font-size="13">{svg_escape(r.get("item",""))}</text>')
        svg.append(f'<text x="{xs[1]+10}" y="{yy}" font-size="13">{svg_escape(r.get("code",""))}</text>')
        svg.append(f'<text x="{xs[2]+10}" y="{yy}" font-size="13">{svg_escape(r.get("desc",""))}</text>')
        svg.append(f'<text x="{xs[3]+10}" y="{yy}" font-size="13">{svg_escape(r.get("pid",""))}</text>')

    y = ty + th + 12

    # Legend
    lxg = M + 20
    lyg = y
    lwg = W - 2*M - 40
    lhg = legend_h

    svg.append(f'<rect x="{lxg}" y="{lyg}" width="{lwg}" height="{lhg}" fill="white" stroke="{LX_GRID}" stroke-width="2"/>')

    pad = 12
    box = 34
    xcur = lxg + pad
    ymid = lyg + lhg / 2 + 6

    svg.append(f'<rect x="{xcur}" y="{lyg+18}" width="{box}" height="{box}" fill="url(#hx_diag)" stroke="#000" stroke-width="1"/>')
    svg.append(f'<text x="{xcur+box+10}" y="{ymid}" font-size="12">Diagonally hatched Loads must be restrained from sliding</text>')
    xcur += 520

    svg.append(f'<rect x="{xcur}" y="{lyg+18}" width="{box}" height="{box}" fill="url(#hx_vert)" stroke="#000" stroke-width="1"/>')
    svg.append(f'<text x="{xcur+box+10}" y="{ymid}" font-size="12">Vertically hatched Loads must be restrained from tipping and sliding</text>')
    xcur += 560

    svg.append(f'<text x="{xcur}" y="{ymid}" font-size="12">Any securement system used must prevent movement of all Loads blocked by the hatched Load</text>')

    svg.append("</svg>")
    return "\n".join(svg)


# =============================
# Item table rows (basic)
# =============================
def build_item_rows_from_requests(reqs: List[RequestLine], products: Dict[str, Product]) -> List[dict]:
    rows = []
    for i, r in enumerate(reqs, start=1):
        p = products.get(r.product_id)
        if not p:
            continue
        code = "A" if p.is_half_pack else "B"
        rows.append({"item": str(i), "code": code, "desc": f"(Qty: {r.tiers}) {p.description}", "pid": p.product_id})
    return rows[:6]


# =============================
# App start
# =============================
try:
    pm = load_product_master(MASTER_PATH)
except Exception as e:
    st.error(f"Could not load Product Master at '{MASTER_PATH}'. Error: {e}")
    st.stop()

if "requests" not in st.session_state:
    st.session_state.requests = []
if "matrix" not in st.session_state:
    st.session_state.matrix = make_empty_matrix(4, 7, FLOOR_SPOTS_BOXCAR)

# Sidebar — formal structure
with st.sidebar:
    st.header("Settings")

    order_number = st.text_input("Order Number", value="")
    po_number = st.text_input("PO Number", value="")
    commodity_type = st.selectbox("Commodity Type", ["Plywood", "OSB", "Lumber"], index=0)

    car_type = st.selectbox("Car Type", ["Boxcar", "Centerbeam"], index=0)
    car_type_key = "boxcar" if car_type.lower().startswith("box") else "centerbeam"
    floor_spots = FLOOR_SPOTS_BOXCAR if car_type_key == "boxcar" else FLOOR_SPOTS_CENTERBEAM

    car_spec_source = st.selectbox("Car Spec Source", ["Manual Override", "UMLER"], index=0)
    car_id = st.text_input("Vehicle Number / Car ID", value="TBOX632012")

    st.divider()

    max_tiers = st.slider("Max tiers per spot", 1, 8, 4)

    if car_type_key == "boxcar":
        turn_spot = int(st.selectbox("Turn spot (must be 7 or 8)", ["7", "8"], index=0))
        required_turn_tiers = st.slider("Turn tiers required (HARD)", 1, 8, int(max_tiers))
        required_turn_tiers = min(int(required_turn_tiers), int(max_tiers))

        auto_airbag = st.checkbox('Auto airbag (prefer <= 9")', value=True)
        if auto_airbag:
            airbag_gap_choice, airbag_gap_in = auto_airbag_choice()
        else:
            gap_labels = [f"{a}-{b}" for a, b in AIRBAG_ALLOWED_GAPS]
            gap_choice_label = st.selectbox("Airbag location", gap_labels, index=1)
            airbag_gap_choice = AIRBAG_ALLOWED_GAPS[gap_labels.index(gap_choice_label)]
            airbag_gap_in = st.slider("Airbag gap (in)", 6.0, 10.0, 6.0, 0.5)
    else:
        turn_spot = 7
        required_turn_tiers = 0
        airbag_gap_choice = (7, 8)
        airbag_gap_in = 0.0

    view_mode = st.radio("View", ["Top + Side pages (PDF)", "Side1 only", "Side2 only"], index=0)

    st.divider()

    with st.expander("Engineering Inputs (CG + Axle)", expanded=False):
        st.caption("CG_above_TOR requires deck height, empty car CG, tare weight, spring deflection, and payload.")
        if car_spec_source == "UMLER":
            st.info("UMLER source selected. Wire lookup here (read-only fields) or switch to Manual Override for now.")

        deck_height_TOR_in = st.number_input("Deck height above TOR (in)", min_value=0.0, value=60.0, step=0.25)
        empty_car_CG_TOR_in = st.number_input("Empty car CG above TOR (in)", min_value=0.0, value=62.0, step=0.25)
        tare_weight_lbs = st.number_input("Tare weight (lb)", min_value=0.0, value=82000.0, step=100.0)
        spring_deflection_in = st.number_input("Spring deflection (in)", min_value=0.0, value=0.0, step=0.05)

        payload_override_on = st.checkbox("Override payload weight (lb)", value=False)
        payload_override_lbs = st.number_input("Payload override (lb)", min_value=0.0, value=0.0, step=100.0, disabled=(not payload_override_on))

    st.divider()

    generate_btn = st.button("Generate Diagram", type="primary")
    clear_btn = st.button("Clear All")


st.success(f"Product Master loaded: {len(pm):,} rows")

# Filters
commodities = sorted(pm[COL_COMMODITY].dropna().astype(str).unique().tolist())
commodity_selected = st.selectbox("Commodity / Product Type (required)", ["(Select)"] + commodities)
if commodity_selected == "(Select)":
    st.info("Select a Commodity/Product Type to proceed.")
    st.stop()

pm_c = pm[pm[COL_COMMODITY].astype(str) == str(commodity_selected)].copy()
facilities = sorted(pm_c[COL_FACILITY].dropna().astype(str).unique().tolist()) if COL_FACILITY in pm_c.columns else []
facility_selected = st.selectbox("Facility Id (filtered by commodity)", ["(All facilities)"] + facilities)

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

options = pm_cf.to_dict("records")


def option_label(r: dict) -> str:
    pid = str(r.get(COL_PRODUCT_ID, "")).strip()
    desc = str(r.get(COL_DESC, "")).strip()
    edge = str(r.get(COL_EDGE, "")).strip()
    wt = r.get(COL_UNIT_WT)

    if COL_HALF_PACK in pm_cf.columns:
        hp = " HP" if _truthy(r.get(COL_HALF_PACK, "")) else ""
    else:
        hp = " HP" if desc.upper().rstrip().endswith("HP") else ""

    parts = [f"{pid}{hp}"]
    if pd.notna(wt):
        parts.append(f"{float(wt):,.0f} lbs")
    if edge:
        parts.append(edge)
    if desc:
        parts.append(desc)
    return " | ".join(parts)


labels = [option_label(r) for r in options]
selected_label = st.selectbox("Pick a Product", labels) if labels else None

c1, c2, c3, c4 = st.columns([2, 1, 1, 1], vertical_alignment="bottom")
with c1:
    tiers_to_add = st.number_input("Tiers to add", min_value=1, value=4, step=1)
with c2:
    add_line = st.button("Add Line", disabled=(selected_label is None))
with c3:
    optimize_btn = st.button("Optimize Layout")
with c4:
    st.write("")  # spacer

if clear_btn:
    st.session_state.requests = []
    st.session_state.matrix = make_empty_matrix(int(max_tiers), int(turn_spot), int(floor_spots))

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
    rows = []
    for r in st.session_state.requests:
        p = products.get(r.product_id)
        rows.append({"Sales Product Id": r.product_id, "Description": p.description if p else "", "Tiers": r.tiers})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=220)
else:
    st.info("Add one or more SKUs, then click Generate Diagram / Optimize Layout.")

msgs: List[str] = []
if generate_btn or optimize_btn:
    if not st.session_state.requests:
        st.warning("No request lines to optimize.")
    else:
        matrix, msgs = optimize_layout(
            products,
            st.session_state.requests,
            int(max_tiers),
            int(turn_spot),
            int(required_turn_tiers),
            int(floor_spots),
        )
        st.session_state.matrix = matrix

for m in msgs:
    st.warning(m)

matrix = st.session_state.matrix

payload_calc_lbs, placed, avg_stack_weighted_in, avg_stack_simple_in, per_spot_stats = compute_payload_and_stack_stats(
    matrix, products, turn_spot=int(turn_spot), floor_spots=int(floor_spots)
)

payload_used_lbs = float(payload_override_lbs) if payload_override_on else float(payload_calc_lbs)

cg_in, cg_trace = cg_above_tor(
    deck_height_TOR_in=float(deck_height_TOR_in),
    empty_car_CG_TOR_in=float(empty_car_CG_TOR_in),
    tare_weight_lbs=float(tare_weight_lbs),
    spring_deflection_in=float(spring_deflection_in),
    payload_weight_lbs=float(payload_used_lbs),
    avg_stack_height_in=float(avg_stack_weighted_in),
)
tier_label, tier_chip = cg_tier(cg_in, car_type_key=car_type_key)

cg_height_in = None
if cg_trace:
    cg_height_in = cg_trace.get("A_plus_C")

# =============================
# Summary + validations
# =============================
st.subheader("Engineering Summary")

colA, colB, colC = st.columns(3)
with colA:
    st.metric("Payload (lb)", f"{payload_used_lbs:,.0f}" + (" (override)" if payload_override_on else ""))
with colB:
    st.metric("Placed tiers", f"{placed:,}")
with colC:
    st.metric("Avg stack height (in)", f"{avg_stack_weighted_in:,.2f}")

st.write(f"**CG_above_TOR tier:** {tier_chip}")

st.subheader("Validations")
if car_type_key == "boxcar":
    s, msg = validate_airbag(float(airbag_gap_in))
    (st.success if s == "PASS" else st.warning if s == "WARN" else st.error)(msg)

if cg_in is None:
    st.error("CG_above_TOR is N/A (missing/invalid engineering inputs or payload).")
else:
    if tier_label == "Preferred":
        st.success(f"CG_above_TOR {cg_in:.2f} in is in Preferred range.")
    elif tier_label == "Caution":
        st.warning(f"CG_above_TOR {cg_in:.2f} in is in Caution range (manual awareness).")
    else:
        st.error(f"CG_above_TOR {cg_in:.2f} in is High Risk — manual approval required.")

if car_type_key == "centerbeam":
    s, msg = validate_centerbeam_symmetry(per_spot_stats)
    (st.success if s == "PASS" else st.warning if s == "WARN" else st.error)(msg)

with st.expander("Calculation Trace (CG)", expanded=False):
    if not cg_trace:
        st.info("No CG trace available (missing required inputs or payload).")
    else:
        st.write(
            {
                "A = deck_height_TOR - spring_deflection (in)": round(cg_trace["A"], 4),
                "B = empty_car_CG_TOR (in)": round(cg_trace["B"], 4),
                "C = load CG above deck (in)": round(cg_trace["C"], 4),
                "A + C (load CG above TOR) (in)": round(cg_trace["A_plus_C"], 4),
                "E = tare weight (lb)": round(cg_trace["E"], 1),
                "F = payload weight (lb)": round(cg_trace["F"], 1),
            }
        )
        st.latex(r"CG_{above\_TOR} = \frac{(B \cdot E) + ((A + C)\cdot F)}{E + F}")

# =============================
# Diagram View — Load Xpert-style pages
# =============================
item_rows = build_item_rows_from_requests(st.session_state.requests, products)

floor_spots_text = str(floor_spots)
cg_height_text = "N/A" if cg_height_in is None else f"{cg_height_in:.2f} (in)"
cg_above_tor_text = "N/A" if cg_in is None else f"{cg_in:.2f} (in)"
airbag_space_text = "N/A" if car_type_key != "boxcar" else f"{float(airbag_gap_in):.2f} (in)"
wue_text = "—"   # wire later
lisa_text = "—"  # wire later

created_by = facility_selected if facility_selected != "(All facilities)" else "—"
created_at = "—"  # wire later if desired
order_num = order_number or "—"
vehicle_num = "UML_TBOX" if car_type_key == "boxcar" else "UML_CENTERBEAM"
po_num = po_number or "—"

page_svg_side1 = render_loadxpert_top_side_page_svg(
    page_title="Top + Side 1 View",
    created_by=str(created_by),
    created_at=str(created_at),
    order_number=str(order_num),
    vehicle_number=str(vehicle_num),
    po_number=str(po_num),
    car_id=car_id,
    matrix=matrix,
    products=products,
    floor_spots=int(floor_spots),
    turn_spot=int(turn_spot),
    airbag_gap_choice=airbag_gap_choice,
    airbag_gap_in=float(airbag_gap_in),
    floor_spots_text=floor_spots_text,
    cg_height_text=cg_height_text,
    cg_above_tor_text=cg_above_tor_text,
    airbag_space_text=airbag_space_text,
    wue_text=wue_text,
    lisa_text=lisa_text,
    item_rows=item_rows,
    side_flip=False,
)

page_svg_side2 = render_loadxpert_top_side_page_svg(
    page_title="Top + Side 2 View",
    created_by=str(created_by),
    created_at=str(created_at),
    order_number=str(order_num),
    vehicle_number=str(vehicle_num),
    po_number=str(po_num),
    car_id=car_id,
    matrix=matrix,
    products=products,
    floor_spots=int(floor_spots),
    turn_spot=int(turn_spot),
    airbag_gap_choice=airbag_gap_choice,
    airbag_gap_in=float(airbag_gap_in),
    floor_spots_text=floor_spots_text,
    cg_height_text=cg_height_text,
    cg_above_tor_text=cg_above_tor_text,
    airbag_space_text=airbag_space_text,
    wue_text=wue_text,
    lisa_text=lisa_text,
    item_rows=item_rows,
    side_flip=True,
)

st.subheader("Diagram View (Load Xpert format)")

if view_mode == "Side1 only":
    components_svg(page_svg_side1, height=1000)
elif view_mode == "Side2 only":
    components_svg(page_svg_side2, height=1000)
else:
    components_svg(page_svg_side1, height=1000)
    st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
    components_svg(page_svg_side2, height=1000)
