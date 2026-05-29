# streamlit_app.py
# LoadXpert Route A — Presentation1 Rules Integrated
#
# Key behaviors:
# - NORMAL doorway: 6/7/8/9 are independent 1-spot columns (but TOP labels in doorway render horizontal like PDF).
# - Forklift TURN: consumes 2 spots at turn_spot and turn_spot+1 for required tiers (hard).
# - Presentation1 rules:
#   * %Blocked -> Strapping requirement text
#   * Diagonal hatch = cord strap required due to step-down (only if straps_required by %blocked table)
#   * Honeycomb dunnage (3") required if a void exists between tiers
#   * CG_above_TOR = ((B*E)+((A+C)*F))/(E+F) + PASS/WARN/FAIL
#
# Notes:
# - A/B/C mapping is still a placeholder. Replace code_for_pid() with your true mapping logic.
# - Exact pixel-perfect font metrics to LoadXpert PDFs requires their exact font files and their exact layout constants.
#   This file focuses on correct rule logic + consistent rendering structure (doorway bays, turn span, hatching meaning).

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


# =============================
# Page
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
FLOOR_SPOTS = 15

DOOR_START_SPOT = 6
DOOR_END_SPOT = 9
DOOR_SPOTS = [6, 7, 8, 9]

DOORFRAME_NO_ME = {6, 9}
DOORPOCKET_PINS = {7, 8}

AIRBAG_ALLOWED_GAPS = [(6, 7), (7, 8), (8, 9)]

# Default colors (tunable)
DEFAULT_CODE_COLORS = {
    "A": {"fill": "#79C7C7", "stroke": "#111111"},  # teal-ish
    "B": {"fill": "#F4F48A", "stroke": "#111111"},  # yellow-ish
    "C": {"fill": "#2FB34B", "stroke": "#111111"},  # green-ish
}
DEFAULT_HATCH = {"angle_deg": 45.0, "spacing_px": 8.0, "alpha": 0.22}


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

    @property
    def is_tg(self) -> bool:
        s = (self.description or "").upper()
        return ("T&G" in s) or ("TNG" in s) or ("TONGUE" in s and "GROOVE" in s)


@dataclass
class RequestLine:
    product_id: str
    tiers: int


@dataclass
class SecurementDecision:
    percent_blocked: float
    straps_required: bool
    strap_text: str
    hatch_legend: str


@dataclass
class AnalysisResult:
    heights_by_spot: Dict[int, int]
    step_down_boundaries: List[Tuple[int, int]]
    hatched_spots: List[int]
    honeycomb_required: bool
    honeycomb_spots: List[int]
    securement: SecurementDecision
    payload_lbs: float
    cg_above_tor_in: float
    cg_status: str
    weight_balance_ratio: float


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
# Turn + Occupancy rules
# =============================
def blocked_spot_for_turn(turn_spot: int) -> int:
    return turn_spot + 1


def is_blocked_spot(spot: int, turn_spot: int) -> bool:
    return spot == blocked_spot_for_turn(turn_spot)


def occupied_spots_for_placement(spot: int, turn_spot: int) -> List[int]:
    # Forklift turn consumes 2 spots always
    if spot == turn_spot:
        return [spot, blocked_spot_for_turn(turn_spot)]

    # Normal: 1 spot
    return [spot]


# =============================
# Placement matrix helpers
# =============================
def make_empty_matrix(max_tiers: int, turn_spot: int) -> List[List[Optional[str]]]:
    m = [[None for _ in range(max_tiers)] for _ in range(FLOOR_SPOTS)]
    b = blocked_spot_for_turn(turn_spot)
    for t in range(max_tiers):
        if 1 <= b <= FLOOR_SPOTS:
            m[b - 1][t] = BLOCK
    return m


def next_empty_tier_index(
    matrix: List[List[Optional[str]]], spot: int, turn_spot: int
) -> Optional[int]:
    if is_blocked_spot(spot, turn_spot):
        return None
    occ = occupied_spots_for_placement(spot, turn_spot)
    tiers = len(matrix[0])
    for t in range(tiers):
        if all(matrix[s - 1][t] is None for s in occ):
            return t
    return None


def place_pid(
    matrix: List[List[Optional[str]]], spot: int, tier_idx: int, pid: str, turn_spot: int
) -> None:
    for s in occupied_spots_for_placement(spot, turn_spot):
        matrix[s - 1][tier_idx] = pid


# =============================
# Hard + Soft rules
# =============================
def can_place_hard(products: Dict[str, Product], pid: str, spot: int, turn_spot: int) -> Tuple[bool, str]:
    p = products[pid]
    occ = occupied_spots_for_placement(spot, turn_spot)

    if p.is_machine_edge and any(s in DOORFRAME_NO_ME for s in occ):
        return False, "Machine Edge not allowed in doorframe spots 6/9 (or any placement that touches them)."

    return True, ""


def soft_penalty(
    *,
    products: Dict[str, Product],
    pid: str,
    tier_idx: int,
    max_tiers: int,
    spot: int,
    matrix: List[List[Optional[str]]],
    close_top_weight: int,
    weight_balance_weight: int,
    tg_safety_weight: int,
    stagger_weight: int,
) -> int:
    """
    Soft goals:
    - Close top (avoid big isolated towers)
    - Weight balance (penalize distance from center)
    - T&G tier safety (avoid extremes)
    - Stagger rule (avoid placing same SKU adjacent in same tier)
    """
    p = products[pid]
    penalty = 0

    # HP not on very top
    if p.is_half_pack and tier_idx == (max_tiers - 1):
        penalty += 120

    # T&G: penalize bottom/top extremes
    if p.is_tg and tier_idx in (0, max_tiers - 1):
        penalty += tg_safety_weight

    # Close-top: tower penalty
    if close_top_weight > 0:
        def height_at(s: int) -> int:
            if s < 1 or s > FLOOR_SPOTS:
                return 0
            col = matrix[s - 1]
            return sum(1 for x in col if x and x != BLOCK)

        left = height_at(spot - 1)
        right = height_at(spot + 1)
        cur = height_at(spot)
        new_h = max(cur, tier_idx + 1)
        if new_h - max(left, right) >= 2:
            penalty += close_top_weight

    # Weight balance: distance from center
    if weight_balance_weight > 0:
        center = 7.5
        dist = abs(spot - center)
        penalty += int(weight_balance_weight * dist)

    # Stagger: penalize same PID adjacent on same tier
    if stagger_weight > 0:
        left_pid = matrix[spot - 2][tier_idx] if spot - 1 >= 1 else None
        right_pid = matrix[spot][tier_idx] if spot + 1 <= FLOOR_SPOTS else None
        if left_pid == pid:
            penalty += stagger_weight
        if right_pid == pid:
            penalty += stagger_weight
        if left_pid == pid and right_pid == pid:
            penalty += stagger_weight

    return penalty


# =============================
# Optimization
# =============================
def build_token_list(products: Dict[str, Product], requests: List[RequestLine]) -> List[str]:
    expanded: List[Tuple[str, float]] = []
    for r in requests:
        if r.tiers <= 0:
            continue
        p = products[r.product_id]
        expanded.extend([(p.product_id, p.unit_weight_lbs)] * int(r.tiers))
    expanded.sort(key=lambda x: x[1], reverse=True)
    return [pid for pid, _ in expanded]


def pop_best(
    tokens: List[str],
    *,
    products: Dict[str, Product],
    spot: int,
    tier_idx: int,
    max_tiers: int,
    matrix: List[List[Optional[str]]],
    turn_spot: int,
    close_top_weight: int,
    weight_balance_weight: int,
    tg_safety_weight: int,
    stagger_weight: int,
) -> Optional[str]:
    best_i = None
    best_score = None
    for i, pid in enumerate(tokens):
        ok, _ = can_place_hard(products, pid, spot, turn_spot)
        if not ok:
            continue
        score = soft_penalty(
            products=products,
            pid=pid,
            tier_idx=tier_idx,
            max_tiers=max_tiers,
            spot=spot,
            matrix=matrix,
            close_top_weight=close_top_weight,
            weight_balance_weight=weight_balance_weight,
            tg_safety_weight=tg_safety_weight,
            stagger_weight=stagger_weight,
        )
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
    tokens: List[str],
    max_tiers: int,
    turn_spot: int,
    required_turn_tiers: int,
    msgs: List[str],
    close_top_weight: int,
    weight_balance_weight: int,
    tg_safety_weight: int,
    stagger_weight: int,
) -> None:
    required_turn_tiers = max(0, min(required_turn_tiers, max_tiers))
    for t in range(required_turn_tiers):
        if matrix[turn_spot - 1][t] not in (None, BLOCK):
            continue
        pid = pop_best(
            tokens,
            products=products,
            spot=turn_spot,
            tier_idx=t,
            max_tiers=max_tiers,
            matrix=matrix,
            turn_spot=turn_spot,
            close_top_weight=close_top_weight,
            weight_balance_weight=weight_balance_weight,
            tg_safety_weight=tg_safety_weight,
            stagger_weight=stagger_weight,
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
    close_top_weight: int,
    weight_balance_weight: int,
    tg_safety_weight: int,
    stagger_weight: int,
) -> Tuple[List[List[Optional[str]]], List[str]]:
    msgs: List[str] = []
    tokens = build_token_list(products, requests)
    matrix = make_empty_matrix(max_tiers, turn_spot)

    force_turn_tiers(
        matrix,
        products,
        tokens,
        max_tiers,
        turn_spot,
        required_turn_tiers,
        msgs,
        close_top_weight,
        weight_balance_weight,
        tg_safety_weight,
        stagger_weight,
    )

    outside = [1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15]
    doorway = [7, 8, 6, 9]

    order = [s for s in outside + doorway if not is_blocked_spot(s, turn_spot)]

    while tokens:
        placed_any = False
        for spot in order:
            t = next_empty_tier_index(matrix, spot, turn_spot)
            if t is None:
                continue

            pid = pop_best(
                tokens,
                products=products,
                spot=spot,
                tier_idx=t,
                max_tiers=max_tiers,
                matrix=matrix,
                turn_spot=turn_spot,
                close_top_weight=close_top_weight,
                weight_balance_weight=weight_balance_weight,
                tg_safety_weight=tg_safety_weight,
                stagger_weight=stagger_weight,
            )
            if pid is None:
                continue

            # PIN preference: if Machine Edge, try 7/8 at same tier (if available)
            if products[pid].is_machine_edge:
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

    if tokens:
        msgs.append(f"{len(tokens)} tiers could not be placed (capacity/rules).")

    return matrix, msgs


# =============================
# Presentation1 Rules: %Blocked -> Strapping
# =============================
def decide_strapping(percent_blocked: float) -> SecurementDecision:
    x = float(percent_blocked)
    if x > 90.0:
        return SecurementDecision(x, False, "Straps: No", "(No cord strap required)")
    if x >= 50.0:
        return SecurementDecision(x, True, "Straps: Yes — Double strapping", "Diagonal hatch = cord strap required (step-down)")
    if x >= 10.0:
        return SecurementDecision(x, True, "Straps: Yes — 2-unit double strapping", "Diagonal hatch = cord strap required (step-down)")
    return SecurementDecision(x, True, "Straps: Yes — 4-unit double strapping", "Diagonal hatch = cord strap required (step-down)")


# =============================
# Analysis: step-down, voids, %blocked, CG
# =============================
def compute_spot_heights(matrix: List[List[Optional[str]]], turn_spot: int) -> Dict[int, int]:
    heights: Dict[int, int] = {}
    blocked = blocked_spot_for_turn(turn_spot)

    for s in range(1, FLOOR_SPOTS + 1):
        col = matrix[s - 1]
        h = sum(1 for x in col if x and x != BLOCK)
        heights[s] = h

    # Turn span mirror
    if 1 <= blocked <= FLOOR_SPOTS:
        heights[blocked] = heights.get(turn_spot, 0)

    return heights


def detect_step_down_boundaries(heights: Dict[int, int]) -> List[Tuple[int, int]]:
    out: List[Tuple[int, int]] = []
    for s in range(1, FLOOR_SPOTS):
        if heights.get(s, 0) != heights.get(s + 1, 0):
            out.append((s, s + 1))
    return out


def determine_hatched_spots_from_step_down(boundaries: List[Tuple[int, int]], heights: Dict[int, int]) -> List[int]:
    hs = set()
    for a, b in boundaries:
        ha, hb = heights.get(a, 0), heights.get(b, 0)
        if ha == hb:
            continue
        hs.add(a if ha > hb else b)
    return sorted(hs)


def detect_honeycomb_voids(matrix: List[List[Optional[str]]], turn_spot: int) -> Tuple[bool, List[int]]:
    void_spots: List[int] = []
    blocked = blocked_spot_for_turn(turn_spot)
    for s in range(1, FLOOR_SPOTS + 1):
        if s == blocked:
            continue
        col = matrix[s - 1]
        seen_filled_above = False
        for v in reversed(col):
            if v is None:
                if seen_filled_above:
                    void_spots.append(s)
                    break
            elif v != BLOCK:
                seen_filled_above = True
    return (len(void_spots) > 0), sorted(void_spots)


def compute_percent_blocked(matrix: List[List[Optional[str]]], turn_spot: int) -> float:
    """
    Proxy: occupied floor spots / available floor spots (excluding the blocked turn spot+1)
    """
    blocked = blocked_spot_for_turn(turn_spot)
    avail = FLOOR_SPOTS - 1
    occ = 0
    for s in range(1, FLOOR_SPOTS + 1):
        if s == blocked:
            continue
        col = matrix[s - 1]
        if any(v and v != BLOCK for v in col):
            occ += 1
    return 0.0 if avail <= 0 else 100.0 * (occ / avail)


def compute_payload_lbs(matrix: List[List[Optional[str]]], products: Dict[str, Product]) -> float:
    payload = 0.0
    for s in range(1, FLOOR_SPOTS + 1):
        for pid in matrix[s - 1]:
            if pid and pid != BLOCK and pid in products:
                payload += products[pid].unit_weight_lbs
    return payload


def compute_weight_balance_ratio(matrix: List[List[Optional[str]]], products: Dict[str, Product]) -> float:
    center = 7.5
    left = 0.0
    right = 0.0
    for s in range(1, FLOOR_SPOTS + 1):
        w = 0.0
        for pid in matrix[s - 1]:
            if pid and pid != BLOCK and pid in products:
                w += products[pid].unit_weight_lbs
        if s < center:
            left += w
        else:
            right += w
    total = left + right
    return 0.0 if total <= 0 else abs(left - right) / total


def compute_cg_above_tor(A: float, B: float, E: float, F: float, C: float) -> float:
    if (E + F) <= 0:
        return 0.0
    return ((B * E) + ((A + C) * F)) / (E + F)


def estimate_load_cg_above_deck(matrix: List[List[Optional[str]]], products: Dict[str, Product]) -> float:
    heights = []
    for s in range(1, FLOOR_SPOTS + 1):
        for pid in matrix[s - 1]:
            if pid and pid != BLOCK and pid in products:
                heights.append(products[pid].unit_height_in)
    if not heights:
        return 0.0
    avg_unit_h = float(sum(heights) / len(heights))

    spot_tiers = []
    for s in range(1, FLOOR_SPOTS + 1):
        col = matrix[s - 1]
        t = sum(1 for x in col if x and x != BLOCK)
        if t > 0:
            spot_tiers.append(t)
    if not spot_tiers:
        return 0.0
    avg_tiers = float(sum(spot_tiers) / len(spot_tiers))
    stack_h = avg_tiers * avg_unit_h
    return stack_h / 2.0


def analyze_layout(
    *,
    matrix: List[List[Optional[str]]],
    products: Dict[str, Product],
    turn_spot: int,
    A_deck: float,
    B_empty_cg: float,
    E_tare: float,
    cg_limit_in: float,
    override_C: Optional[float],
) -> AnalysisResult:
    heights = compute_spot_heights(matrix, turn_spot)
    boundaries = detect_step_down_boundaries(heights)
    hatched = determine_hatched_spots_from_step_down(boundaries, heights)

    honeycomb_required, honeycomb_spots = detect_honeycomb_voids(matrix, turn_spot)

    pct = compute_percent_blocked(matrix, turn_spot)
    securement = decide_strapping(pct)

    payload = compute_payload_lbs(matrix, products)
    C_est = estimate_load_cg_above_deck(matrix, products)
    C = float(override_C) if override_C is not None else C_est
    cg = compute_cg_above_tor(float(A_deck), float(B_empty_cg), float(E_tare), float(payload), float(C))
    status = "PASS" if cg <= cg_limit_in else ("WARN" if cg <= (cg_limit_in * 1.03) else "FAIL")

    wb = compute_weight_balance_ratio(matrix, products)

    return AnalysisResult(
        heights_by_spot=heights,
        step_down_boundaries=boundaries,
        hatched_spots=hatched if securement.straps_required else [],
        honeycomb_required=honeycomb_required,
        honeycomb_spots=honeycomb_spots,
        securement=securement,
        payload_lbs=payload,
        cg_above_tor_in=cg,
        cg_status=status,
        weight_balance_ratio=wb,
    )


# =============================
# A/B/C mapping placeholder
# =============================
def code_for_pid(pid: str, products: Dict[str, Product]) -> str:
    p = products.get(pid)
    if not p:
        return "A"
    if p.is_half_pack:
        return "B"
    if p.is_tg:
        return "C"
    return "A"


# =============================
# Rendering (2D Canvas + optional Three.js 3D)
# =============================
def render_routeA_component(
    *,
    page_title: str,
    created_by: str,
    created_at: str,
    order_number: str,
    vehicle_number: str,
    po_number: str,
    car_id: str,
    matrix: List[List[Optional[str]]],
    products: Dict[str, Product],
    turn_spot: int,
    airbag_gap_choice: Tuple[int, int],
    airbag_gap_in: float,
    analysis: AnalysisResult,
    code_colors: Dict[str, Dict[str, str]],
    hatch_angle_deg: float,
    hatch_spacing_px: float,
    hatch_alpha: float,
    show_3d: bool,
    show_edges: bool,
    cam_fov: float,
    cam_pos: Tuple[float, float, float],
    light_intensity: float,
    ambient_intensity: float,
    flip_side: bool,
    height_px: int = 1040,
    max_payload_lbs: float = 201900.0,
) -> None:
    tiers = len(matrix[0]) if matrix else 4
    blocked = blocked_spot_for_turn(turn_spot)

    rep: Dict[int, Optional[str]] = {}
    for s in range(1, FLOOR_SPOTS + 1):
        col = matrix[s - 1]
        rep[s] = next((x for x in col if x and x != BLOCK), None)

    cells = []
    for s in range(1, FLOOR_SPOTS + 1):
        for t in range(tiers):
            pid = matrix[s - 1][t]
            if pid is None or pid == BLOCK:
                continue
            if s == blocked and matrix[turn_spot - 1][t] == pid:
                continue
            cells.append({"spot": s, "tier": t, "pid": pid, "code": code_for_pid(pid, products)})

    prod_map = {}
    for pid, p in products.items():
        prod_map[pid] = {
            "id": pid,
            "desc": p.description or "",
            "L": float(p.length) if p.length is not None else None,
            "W": float(p.width) if p.width is not None else None,
            "H": float(p.unit_height_in) if p.unit_height_in is not None else None,
            "wt": float(p.unit_weight_lbs) if p.unit_weight_lbs is not None else None,
            "pieces": float(p.piece_count) if p.piece_count is not None else None,
            "code": code_for_pid(pid, products),
        }

    qty_by_pid = {}
    for c in cells:
        qty_by_pid[c["pid"]] = qty_by_pid.get(c["pid"], 0) + 1

    total_units = sum(qty_by_pid.values())
    used_spots = sum(1 for s in range(1, FLOOR_SPOTS + 1) if rep.get(s))
    total_capacity = FLOOR_SPOTS * (tiers if tiers else 4)
    volume_pct = (100.0 * total_units / total_capacity) if total_capacity else 0.0
    payload_lbs = float(analysis.payload_lbs)
    weight_pct = (100.0 * payload_lbs / max_payload_lbs) if max_payload_lbs else 0.0

    items = []
    for pid, q in qty_by_pid.items():
        pm = prod_map.get(pid, {})
        items.append({
            "code": pm.get("code", "A"),
            "qty": q,
            "name": pm.get("desc", ""),
            "id": pid,
            "L": pm.get("L"), "W": pm.get("W"), "H": pm.get("H"),
            "wt": pm.get("wt"),
        })

    payload = {
        "meta": {
            "page_title": page_title,
            "created_by": created_by,
            "created_at": created_at,
            "order_number": order_number,
            "vehicle_number": vehicle_number,
            "po_number": po_number,
            "car_id": car_id,
            "spots": FLOOR_SPOTS,
            "tiers": tiers,
            "door_start": DOOR_START_SPOT,
            "door_end": DOOR_END_SPOT,
            "turn_spot": turn_spot,
            "blocked_spot": blocked,
            "flip_side": bool(flip_side),
            "airbag_a": airbag_gap_choice[0],
            "airbag_b": airbag_gap_choice[1],
            "airbag_in": float(airbag_gap_in),
            "securement_text": analysis.securement.strap_text,
            "pct_blocked": float(analysis.securement.percent_blocked),
            "hatch_legend": analysis.securement.hatch_legend,
            "hatched_spots": analysis.hatched_spots,
            "honeycomb_required": bool(analysis.honeycomb_required),
            "honeycomb_spots": analysis.honeycomb_spots,
            "payload_lbs": payload_lbs,
            "max_payload_lbs": float(max_payload_lbs),
            "volume_pct": float(volume_pct),
            "weight_pct": float(weight_pct),
            "total_units": total_units,
            "used_spots": used_spots,
            "cg_in": float(analysis.cg_above_tor_in),
            "cg_status": analysis.cg_status,
            "weight_balance_ratio": float(analysis.weight_balance_ratio),
        },
        "colors": code_colors,
        "hatch": {"angle": float(hatch_angle_deg), "spacing": float(hatch_spacing_px), "alpha": float(hatch_alpha)},
        "rep": rep,
        "cells": cells,
        "items": items,
        "three": {
            "enabled": bool(show_3d),
            "show_edges": bool(show_edges),
            "cam_fov": float(cam_fov),
            "cam_pos": list(cam_pos),
            "light_intensity": float(light_intensity),
            "ambient_intensity": float(ambient_intensity),
        },
    }
    payload_json = json.dumps(payload)

    HTML = r"""
<!DOCTYPE html><html><head><meta charset="utf-8">
<script src="https://unpkg.com/three@0.160.0/build/three.min.js"></script>
<style>
*{box-sizing:border-box;}
body{font-family:Arial,Helvetica,sans-serif;margin:0;padding:8px;color:#111;background:#fff;}
.hdr{display:flex;justify-content:space-between;align-items:center;border-bottom:2px solid #1f3b73;padding-bottom:4px;margin-bottom:6px;}
.hdr .title{font-size:18px;font-weight:bold;color:#1f3b73;}
.hdr .meta{font-size:11px;text-align:right;color:#333;}
.statbar{display:flex;gap:24px;font-size:12px;margin:4px 0 8px;color:#1f3b73;font-weight:bold;}
.statbar span b{color:#1f3b73;}
.layout{display:flex;gap:10px;}
.col-left{width:320px;flex:0 0 320px;}
.col-right{flex:1;}
table.grid{border-collapse:collapse;width:100%;font-size:11px;margin-bottom:8px;}
table.grid th,table.grid td{border:1px solid #888;padding:3px 5px;text-align:left;}
table.grid th{background:#eef1f7;}
.viewbox{border:1px solid #ccc;margin-bottom:8px;padding:4px;}
.viewtitle{text-align:center;font-weight:bold;font-size:13px;margin:2px 0;}
canvas{display:block;width:100%;}
#three{width:100%;height:300px;}
.footer{border-top:2px solid #1f3b73;margin-top:6px;padding-top:4px;font-size:12px;}
.footrow{display:flex;justify-content:space-between;margin:2px 0;}
.legend{font-size:10px;color:#444;margin-top:4px;}
</style></head><body>
<div class="hdr">
  <div class="title" id="h_title"></div>
  <div class="meta" id="h_meta"></div>
</div>
<div class="statbar">
  <span>Payload : <b id="h_payload"></b></span>
  <span>MaxPayload : <b id="h_maxpay"></b></span>
  <span>Volume % : <b id="h_vol"></b></span>
  <span>Weight % : <b id="h_wt"></b></span>
</div>
<div class="layout">
  <div class="col-left">
    <table class="grid" id="tbl_settings"></table>
    <table class="grid" id="tbl_lines"></table>
  </div>
  <div class="col-right">
    <div class="viewbox"><div class="viewtitle">3D</div><div id="three"></div></div>
    <div class="viewbox"><div class="viewtitle">Top</div><canvas id="cvTop" width="1100" height="170"></canvas></div>
    <div class="viewbox"><div class="viewtitle">Side1</div><canvas id="cvSide" width="1100" height="240"></canvas></div>
  </div>
</div>
<div class="footer">
  <div class="footrow"><span>Floor spots = <b id="f_spots"></b></span><span>C.G. height = <b id="f_cg"></b></span><span>Airbag Space = <b id="f_airbag"></b></span><span>Whole Unit Equivalent = <b id="f_wue"></b></span></div>
  <div class="footrow"><span>Secure Loads from: <span id="sw_diag"></span> sliding &nbsp; <span id="sw_vert"></span> tipping &amp; sliding</span><span>Total LISA Units = <b id="f_lisa"></b></span></div>
  <table class="grid" id="tbl_items"></table>
  <div class="legend" id="legend"></div>
</div>
<script>
(function(){
var P = __PAYLOAD__;
var M = P.meta||{};
var colors = P.colors||{};
var cells = P.cells||[];
var items = P.items||[];
var SPOTS = M.spots||15;
var TIERS = P.tiers||4;
var TURN = M.turn_spot||0;
var BLOCK = M.blocked_spot||0;
var DOOR_S = M.door_start||TURN;
var DOOR_E = M.door_end||BLOCK;
function num(x,d){return (x===null||x===undefined||isNaN(x))?(d||0):x;}
function fmt(x){return num(x,0).toLocaleString();}
function colorFor(code){var c=colors[code]; if(c&&c.fill) return c.fill; return "#7fc8c8";}
// header
document.getElementById("h_title").textContent = M.page_title||"Load Diagram";
document.getElementById("h_meta").innerHTML = "Order: "+(M.order_number||"")+" &nbsp; Vehicle: "+(M.vehicle_number||"")+"<br>By: "+(M.created_by||"")+" &nbsp; "+(M.created_at||"");
document.getElementById("h_payload").textContent = fmt(M.payload_lbs);
document.getElementById("h_maxpay").textContent = fmt(M.max_payload_lbs);
document.getElementById("h_vol").textContent = num(M.volume_pct,0).toFixed(0);
document.getElementById("h_wt").textContent = num(M.weight_pct,0).toFixed(0);
// settings table
var st = [["Jurisdiction","286 000"],["Securement","Plywood"],["Floor spots",SPOTS],["Turn spot",TURN],["Order #",M.order_number||""],["Car ID",M.car_id||""]];
var sh = "";
st.forEach(function(r){sh += "<tr><th>"+r[0]+"</th><td>"+r[1]+"</td></tr>";});
document.getElementById("tbl_settings").innerHTML = sh;
// footer
document.getElementById("f_spots").textContent = M.used_spots!=null?M.used_spots:SPOTS;
document.getElementById("f_cg").textContent = num(M.cg_in,0).toFixed(2)+" (in)";
document.getElementById("f_airbag").textContent = num(M.airbag_in,0).toFixed(2)+" (in)";
document.getElementById("f_wue").textContent = num(M.total_units,0).toFixed(1);
document.getElementById("f_lisa").textContent = num(M.total_units,0).toFixed(1);
// build grid[spot][tier] = {pid,code} ; turn cells live at spot==TURN
var grid = {};
for (var sp=1; sp<=SPOTS; sp++){ grid[sp]=[]; for(var t=0;t<TIERS;t++) grid[sp][t]=null; }
cells.forEach(function(c){ if(c.spot>=1&&c.spot<=SPOTS&&c.tier>=0&&c.tier<TIERS){ grid[c.spot][c.tier]={pid:c.pid, code:c.code}; } });
function isTurnSpot(sp){ return TURN>0 && sp===TURN; }
function spotFilled(sp){ if(sp===BLOCK) return false; for(var t=0;t<TIERS;t++){ if(grid[sp][t]) return true; } return false; }
// ordered list of drawable spots left->right (skip blocked, it is absorbed by turn)
var drawSpots=[]; for(var sp=1; sp<=SPOTS; sp++){ if(sp===BLOCK) continue; if(!spotFilled(sp)&&!isTurnSpot(sp)) continue; drawSpots.push(sp); }
// SNAKING numbers: number per unit, left half up, right half down around doorway
var numByCell = {}; // key spot+"_"+tier
var leftSpots = drawSpots.filter(function(s){return s < TURN;});
var rightSpots = drawSpots.filter(function(s){return s > BLOCK;});
var n=1;
leftSpots.forEach(function(sp){ for(var t=0;t<TIERS;t++){ if(grid[sp][t]){ numByCell[sp+"_"+t]=n++; } } });
// turn column numbers
if(TURN>0){ for(var t=0;t<TIERS;t++){ if(grid[TURN][t]){ numByCell[TURN+"_"+t]=n++; } } }
// right side continues
rightSpots.forEach(function(sp){ for(var t=0;t<TIERS;t++){ if(grid[sp][t]){ numByCell[sp+"_"+t]=n++; } } });
// lines table (products)
var lh = "<tr><th>#</th><th>Description</th><th>Qty</th><th>Wt</th></tr>";
items.forEach(function(it,i){ lh += "<tr><td>"+(i+1)+"</td><td>"+(it.id||"")+"<br>"+((it.name||"").slice(0,40))+"</td><td>"+(it.qty||"")+"</td><td>"+fmt(it.wt)+"</td></tr>"; });
document.getElementById("tbl_lines").innerHTML = lh;
// items table (footer)
var ih = "<tr><th>ITEM</th><th>Name/SKU</th><th>Product Id</th><th>Size (LxWxH)</th><th>Weight</th></tr>";
items.forEach(function(it){ var sz=(it.L!=null?it.L:"")+"x"+(it.W!=null?it.W:"")+"x"+(it.H!=null?it.H:""); ih += "<tr><td>"+(it.code||"")+"</td><td>"+((it.name||"")) +"</td><td>"+(it.id||"")+"</td><td>"+sz+"</td><td>"+fmt(it.wt)+"</td></tr>"; });
document.getElementById("tbl_items").innerHTML = ih;
document.getElementById("legend").textContent = M.securement_text||"";
// ===== SIDE1 VIEW =====
function drawSide(){
  var cv=document.getElementById("cvSide"); var ctx=cv.getContext("2d");
  var W=cv.width, H=cv.height; ctx.clearRect(0,0,W,H);
  var padX=20, padTop=20, padBot=40;
  var floorY=H-padBot;
  // column widths: normal=1 unit, turn=2 units. count total unit-widths
  var leftN=leftSpots.length, rightN=rightSpots.length;
  var turnW = TURN>0?2:0;
  var totalUnits = leftN + rightN + turnW;
  var colW = (W-2*padX)/Math.max(totalUnits,1);
  var tierH = (floorY-padTop)/TIERS;
  var halfStep = tierH*0.5;
  // x position cursor
  function drawStack(sp, x0, w, raise){
    for(var t=0;t<TIERS;t++){
      var cell=grid[sp][t]; if(!cell) continue;
      var y = floorY - (t+1)*tierH - raise;
      ctx.fillStyle=colorFor(cell.code);
      ctx.fillRect(x0, y, w-1, tierH-1);
      ctx.strokeStyle="#333"; ctx.lineWidth=1; ctx.strokeRect(x0, y, w-1, tierH-1);
      var lbl=(numByCell[sp+"_"+t]||"")+" "+(cell.code||"");
      ctx.fillStyle="#111"; ctx.font="10px Arial"; ctx.textAlign="center"; ctx.textBaseline="middle";
      ctx.fillText(lbl, x0+w/2, y+tierH/2);
    }
  }
  // center-out stagger: doorway centered. left half stepped outward, right half stepped outward.
  // raise pattern: nearest-to-door stack = 0, next out = halfStep, alternating.
  var x = padX;
  // LEFT side: spots far->near door. step outward means index from door.
  for(var i=0;i<leftN;i++){ var sp=leftSpots[i]; var distFromDoor = leftN-1-i; var raise = (distFromDoor%2===1)?halfStep:0; drawStack(sp, x, colW, raise); x+=colW; }
  // TURN column (wide, clean, no stagger)
  if(TURN>0){
    var tx=x, tw=colW*2;
    for(var t=0;t<TIERS;t++){ var cell=grid[TURN][t]; if(!cell) continue; var y=floorY-(t+1)*tierH; ctx.fillStyle=colorFor(cell.code); ctx.fillRect(tx,y,tw-1,tierH-1); ctx.strokeStyle="#333"; ctx.strokeRect(tx,y,tw-1,tierH-1); var lbl=(numByCell[TURN+"_"+t]||"")+"r "+(cell.code||""); ctx.fillStyle="#111"; ctx.font="bold 11px Arial"; ctx.textAlign="center"; ctx.textBaseline="middle"; ctx.fillText(lbl, tx+tw/2, y+tierH/2); }
    // red doorway frame
    ctx.strokeStyle="#d00"; ctx.lineWidth=2; ctx.strokeRect(tx-1, padTop-6, tw+2, floorY-padTop+6);
    x+=tw;
  }
  // RIGHT side: spots near->far door. distFromDoor = index
  for(var j=0;j<rightN;j++){ var sp2=rightSpots[j]; var raise2=(j%2===1)?halfStep:0; drawStack(sp2, x, colW, raise2); x+=colW; }
  // car outline
  ctx.strokeStyle="#1f3b73"; ctx.lineWidth=2; ctx.strokeRect(padX-2, padTop-8, W-2*padX+4, floorY-padTop+10);
  // wheels
  ctx.fillStyle="#555"; var wy=floorY+12; [padX+40,padX+80,W-padX-80,W-padX-40].forEach(function(wx){ ctx.beginPath(); ctx.arc(wx,wy,9,0,7); ctx.fill(); });
}
// ===== TOP VIEW =====
function drawTop(){
  var cv=document.getElementById("cvTop"); var ctx=cv.getContext("2d");
  var W=cv.width,H=cv.height; ctx.clearRect(0,0,W,H);
  var padX=20, padY=20; var bandTop=padY, bandBot=H-padY; var bandH=bandBot-bandTop;
  var leftN=leftSpots.length, rightN=rightSpots.length; var turnW=TURN>0?2:0;
  var totalUnits=leftN+rightN+turnW; var colW=(W-2*padX)/Math.max(totalUnits,1);
  var boxH=bandH*0.6; var shift=bandH*0.18;
  function topLabel(sp){ var rep=null; for(var t=0;t<TIERS;t++){ if(grid[sp][t]){ rep=grid[sp][t]; break; } } return rep; }
  function drawTopCol(sp, x0, w, up){
    var rep=topLabel(sp); if(!rep) return;
    var y0 = up ? bandTop : (bandBot-boxH);
    ctx.fillStyle=colorFor(rep.code); ctx.fillRect(x0,y0,w-1,boxH); ctx.strokeStyle="#333"; ctx.lineWidth=1; ctx.strokeRect(x0,y0,w-1,boxH);
    // vertical label
    ctx.save(); ctx.translate(x0+w/2, y0+boxH/2); ctx.rotate(-Math.PI/2); ctx.fillStyle="#111"; ctx.font="bold 11px Arial"; ctx.textAlign="center"; ctx.textBaseline="middle";
    var num0 = numByCell[sp+"_0"]||""; ctx.fillText(num0+" "+(rep.code||""), 0, 0); ctx.restore();
  }
  var x=padX;
  for(var i=0;i<leftN;i++){ var sp=leftSpots[i]; var distFromDoor=leftN-1-i; var up=(distFromDoor%2===1); drawTopCol(sp,x,colW,up); x+=colW; }
  // turn: 2 stacked half boxes in a 2-wide slot, centered, red frame
  if(TURN>0){ var tx=x, tw=colW*2; var filled=[]; for(var t=0;t<TIERS;t++){ if(grid[TURN][t]) filled.push(t); }
    var hh=bandH/2;
    // draw two representative rotated units stacked
    for(var k=0;k<2;k++){ var t2=filled[k!=null?k:0]; if(t2==null) t2=filled[0]; var cell=grid[TURN][t2!=null?t2:0]; if(!cell) cell=topLabel(TURN); if(!cell) continue; var yy=bandTop+k*hh; ctx.fillStyle=colorFor(cell.code); ctx.fillRect(tx,yy,tw-1,hh-1); ctx.strokeStyle="#333"; ctx.strokeRect(tx,yy,tw-1,hh-1); ctx.fillStyle="#111"; ctx.font="9px Arial"; ctx.textAlign="center"; ctx.textBaseline="middle"; var nlbl=(numByCell[TURN+"_"+(filled[k]||0)]||"")+"r "+(cell.code||""); ctx.fillText(nlbl, tx+tw/2, yy+hh/2); }
    ctx.strokeStyle="#d00"; ctx.lineWidth=2; ctx.strokeRect(tx-1,bandTop-4,tw+2,bandH+8); x+=tw; }
  for(var j=0;j<rightN;j++){ var sp2=rightSpots[j]; var up2=(j%2===1); drawTopCol(sp2,x,colW,up2); x+=colW; }
  ctx.strokeStyle="#1f3b73"; ctx.lineWidth=2; ctx.strokeRect(padX-2,bandTop-6,W-2*padX+4,bandH+12);
}
// ===== 3D VIEW =====
function draw3D(){
  var host=document.getElementById("three"); if(!host) return;
  if(typeof THREE==="undefined"){ host.innerHTML="<div style=\"padding:20px;color:#888\">3D unavailable</div>"; return; }
  var w=host.clientWidth||700, h=host.clientHeight||300;
  var scene=new THREE.Scene(); scene.background=new THREE.Color(0xffffff);
  var cam=new THREE.PerspectiveCamera(num(P.three&&P.three.cam_fov,40),w/h,0.1,5000);
  var cp=(P.three&&P.three.cam_pos)||[60,40,90]; cam.position.set(cp[0]*2,cp[1]*2+20,cp[2]*2);
  cam.lookAt(0,5,0);
  var rend=new THREE.WebGLRenderer({antialias:true}); rend.setSize(w,h); host.innerHTML=""; host.appendChild(rend.domElement);
  scene.add(new THREE.AmbientLight(0xffffff,num(P.three&&P.three.ambient_intensity,0.8)));
  var dl=new THREE.DirectionalLight(0xffffff,num(P.three&&P.three.light_intensity,0.7)); dl.position.set(1,2,1); scene.add(dl);
  var leftN=leftSpots.length,rightN=rightSpots.length,turnW=TURN>0?2:0; var totalUnits=leftN+rightN+turnW;
  var UW=4, UH=3, UD=8; var gap=0.2;
  var totalLen=totalUnits*(UW+gap);
  var x=-totalLen/2;
  function box(cx,cy,cz,sx,sy,sz,col,rot){ var g=new THREE.BoxGeometry(sx,sy,sz); var m=new THREE.MeshLambertMaterial({color:new THREE.Color(col)}); var mesh=new THREE.Mesh(g,m); mesh.position.set(cx,cy,cz); if(rot) mesh.rotation.y=Math.PI/2; scene.add(mesh); var eg=new THREE.LineSegments(new THREE.EdgesGeometry(g), new THREE.LineBasicMaterial({color:0x333333})); eg.position.copy(mesh.position); if(rot) eg.rotation.y=Math.PI/2; scene.add(eg); }
  function stack3D(sp,cx,raise){ for(var t=0;t<TIERS;t++){ var cell=grid[sp][t]; if(!cell) continue; var cy=t*(UH+0.1)+UH/2+(raise||0); box(cx,cy,0,UW,UH,UD,colorFor(cell.code),false); } }
  for(var i=0;i<leftN;i++){ var sp=leftSpots[i]; var d=leftN-1-i; var raise=(d%2===1)?UH*0.5:0; stack3D(sp, x+UW/2, raise); x+=UW+gap; }
  if(TURN>0){ var tcx=x+UW; for(var t=0;t<TIERS;t++){ var cell=grid[TURN][t]; if(!cell) continue; var cy=t*(UH+0.1)+UH/2; box(tcx,cy,0,UD,UH,UW*2,colorFor(cell.code),true); }
    // red doorway wireframe
    var dg=new THREE.BoxGeometry(UW*2+gap,TIERS*(UH+0.1)+1,UD+1); var de=new THREE.LineSegments(new THREE.EdgesGeometry(dg), new THREE.LineBasicMaterial({color:0xdd0000})); de.position.set(tcx,TIERS*(UH+0.1)/2,0); scene.add(de); x+=UW*2+gap; }
  for(var j=0;j<rightN;j++){ var sp2=rightSpots[j]; var raise2=(j%2===1)?UH*0.5:0; stack3D(sp2, x+UW/2, raise2); x+=UW+gap; }
  // blue car bounding box
  var cg=new THREE.BoxGeometry(totalLen+2,TIERS*(UH+0.1)+2,UD+2); var ce=new THREE.LineSegments(new THREE.EdgesGeometry(cg), new THREE.LineBasicMaterial({color:0x1f3b73})); ce.position.set(0,TIERS*(UH+0.1)/2,0); scene.add(ce);
  rend.render(scene,cam);
}
try{ drawSide(); }catch(e){ console.error("drawSide",e); }
try{ drawTop(); }catch(e){ console.error("drawTop",e); }
try{ if(P.three&&P.three.enabled) draw3D(); }catch(e){ console.error("draw3D",e); }
})();
</script></body></html>
"""

    html = HTML.replace("__PAYLOAD__", payload_json)
    components.html(html, height=height_px, scrolling=True)





# =============================
# App init
# =============================
try:
    pm = load_product_master(MASTER_PATH)
except Exception as e:
    st.error(f"Could not load Product Master at '{MASTER_PATH}'. Error: {e}")
    st.stop()

if "requests" not in st.session_state:
    st.session_state.requests: List[RequestLine] = []
if "matrix" not in st.session_state:
    st.session_state.matrix = make_empty_matrix(4, 7)

# =============================
# Sidebar
# =============================
with st.sidebar:
    st.header("Settings")

    page_title = st.text_input("Page title", value="Top + Side View (Route A)")
    created_by = st.text_input("Created By", value="—")
    created_at = st.text_input("Created At", value="—")
    order_number = st.text_input("Order Number", value="—")
    vehicle_number = st.text_input("Vehicle Number", value="—")
    po_number = st.text_input("PO Number", value="—")
    car_id = st.text_input("Car ID", value="TBOX632012")

    st.divider()

    max_tiers = st.slider("Max tiers per spot", 1, 10, 4)
    turn_spot = int(st.selectbox("Turn spot (must be 7 or 8)", ["7", "8"], index=0))
    required_turn_tiers = st.slider("Turn tiers required (HARD)", 0, int(max_tiers), int(max_tiers))

    gap_labels = [f"{a}-{b}" for a, b in AIRBAG_ALLOWED_GAPS]
    gap_choice_label = st.selectbox("Airbag location", gap_labels, index=1)
    airbag_gap_choice = AIRBAG_ALLOWED_GAPS[gap_labels.index(gap_choice_label)]
    airbag_gap_in = st.slider("Airbag space (in)", 6.0, 12.0, 9.0, 0.5)

    st.divider()
    st.subheader("Soft goals")
    close_top_weight = 0
    weight_balance_weight = 0
    tg_safety_weight = 0
    stagger_weight = 0

    st.divider()
    st.subheader("A/B/C Colors")
    colA = st.color_picker("A fill", DEFAULT_CODE_COLORS["A"]["fill"])
    colB = st.color_picker("B fill", DEFAULT_CODE_COLORS["B"]["fill"])
    colC = st.color_picker("C fill", DEFAULT_CODE_COLORS["C"]["fill"])
    code_colors = {
        "A": {"fill": colA, "stroke": "#111111"},
        "B": {"fill": colB, "stroke": "#111111"},
        "C": {"fill": colC, "stroke": "#111111"},
    }

    hatch_angle_deg = float(DEFAULT_HATCH["angle_deg"])
    hatch_spacing_px = float(DEFAULT_HATCH["spacing_px"])
    hatch_alpha = float(DEFAULT_HATCH["alpha"])

    st.divider()
    st.subheader("CG_above_TOR inputs")
    A_deck = st.number_input("A: Deck height above TOR minus spring deflection (in)", min_value=0.0, value=48.0, step=0.1)
    B_empty_cg = st.number_input("B: Empty car CG above TOR (in)", min_value=0.0, value=56.0, step=0.1)
    E_tare = st.number_input("E: Tare weight (lbs)", min_value=1.0, value=75000.0, step=100.0)
    cg_limit_in = st.number_input("CG limit above TOR (in)", min_value=1.0, value=98.0, step=0.5)
    override_C = st.checkbox("Override C (load CG above deck)", value=False)
    C_override_val: Optional[float] = None
    if override_C:
        C_override_val = st.number_input("C: Load CG above deck (in)", min_value=0.0, value=30.0, step=0.5)

    # 3D view: always shown with sensible defaults; multiple sides + top are always visible.
    show_3d = True
    show_edges = True
    cam_fov = 42.0
    cam_x = 10.0
    cam_y = 10.0
    cam_z = 18.0
    light_intensity = 1.2
    ambient_intensity = 0.65
    flip_side = False

    st.divider()
    optimize_btn = st.button("Optimize Layout")
    render_btn = st.button("Render Diagram", type="primary")
    clear_btn = st.button("Clear All")


if clear_btn:
    st.session_state.requests = []
    st.session_state.matrix = make_empty_matrix(int(max_tiers), int(turn_spot))

st.success(f"Product Master loaded: {len(pm):,} rows")

# =============================
# Product selection
# =============================
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

pm_cf = pm_cf.sort_values(by=[COL_PRODUCT_ID], ascending=[True], na_position="last")
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
        rows.append({"Sales Product Id": r.product_id, "Description": p.description if p else "", "Tiers": r.tiers, "T&G?": (p.is_tg if p else False)})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=240)
else:
    st.info("Add one or more SKUs, then click Optimize Layout and Render Diagram.")

msgs: List[str] = []
if optimize_btn:
    if not st.session_state.requests:
        st.warning("No request lines to optimize.")
    else:
        matrix, msgs = optimize_layout(
            products=products,
            requests=st.session_state.requests,
            max_tiers=int(max_tiers),
            turn_spot=int(turn_spot),
            required_turn_tiers=int(required_turn_tiers),
            close_top_weight=int(close_top_weight),
            weight_balance_weight=int(weight_balance_weight),
            tg_safety_weight=int(tg_safety_weight),
            stagger_weight=int(stagger_weight),
        )
        st.session_state.matrix = matrix

for m in msgs:
    st.warning(m)

matrix = st.session_state.matrix
analysis = analyze_layout(
    matrix=matrix,
    products=products,
    turn_spot=int(turn_spot),
    A_deck=float(A_deck),
    B_empty_cg=float(B_empty_cg),
    E_tare=float(E_tare),
    cg_limit_in=float(cg_limit_in),
    override_C=(float(C_override_val) if C_override_val is not None else None),
)

st.subheader("Rule Outputs (Presentation1)")

a1, a2, a3, a4 = st.columns(4)
with a1:
    st.metric("% Blocked (proxy)", f"{analysis.securement.percent_blocked:.1f}%")
with a2:
    st.metric("Securement", analysis.securement.strap_text)
with a3:
    st.metric("CG above TOR (in)", f"{analysis.cg_above_tor_in:.2f} ({analysis.cg_status})")
with a4:
    st.metric("Weight balance", f"{analysis.weight_balance_ratio*100:.1f}%")

if analysis.hatched_spots:
    st.info(f"Hatched spots (cord strap due to step-down): {', '.join(map(str, analysis.hatched_spots))}")
else:
    st.info("No hatched spots flagged (either no step-downs, or straps not required by %blocked table).")

if analysis.honeycomb_required:
    st.warning(f'3" honeycomb dunnage required due to void(s) at spots: {", ".join(map(str, analysis.honeycomb_spots))}')

st.subheader("Diagram View")
if render_btn:
    render_routeA_component(
        page_title=page_title,
        created_by=created_by,
        created_at=created_at,
        order_number=order_number,
        vehicle_number=vehicle_number,
        po_number=po_number,
        car_id=car_id,
        matrix=matrix,
        products=products,
        turn_spot=int(turn_spot),
        airbag_gap_choice=airbag_gap_choice,
        airbag_gap_in=float(airbag_gap_in),
        analysis=analysis,
        code_colors=code_colors,
        hatch_angle_deg=float(hatch_angle_deg),
        hatch_spacing_px=float(hatch_spacing_px),
        hatch_alpha=float(hatch_alpha),
        show_3d=bool(show_3d),
        show_edges=bool(show_edges),
        cam_fov=float(cam_fov),
        cam_pos=(float(cam_x), float(cam_y), float(cam_z)),
        light_intensity=float(light_intensity),
        ambient_intensity=float(ambient_intensity),
        flip_side=bool(flip_side),
        height_px=1040,
    )
else:
    st.caption("Click **Render Diagram** to draw the 2D page and optional 3D panel.")
