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
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<style>
  * { box-sizing: border-box; }
  html,body { margin:0; padding:0; background:#ffffff; font-family: Arial, Helvetica, sans-serif; color:#222; }
  .root { width:100%; padding:8px 10px 14px 10px; }
  .hdrstrip { display:flex; justify-content:space-between; align-items:center; gap:18px;
    font-size:13px; padding:6px 4px; border-bottom:1px solid #cfcfcf; margin-bottom:6px; }
  .hdrstrip .lab { color:#222; }
  .hdrstrip .val { color:#1f6fb2; font-weight:bold; }
  .layout { display:flex; gap:10px; align-items:stretch; }
  .leftcol { width:34%; min-width:300px; }
  .rightcol { flex:1; display:flex; flex-direction:column; gap:8px; }
  .panel { border:1px solid #d0d0d0; border-radius:4px; padding:8px 10px; margin-bottom:10px; }
  .panel h3 { margin:0 0 8px 0; font-size:15px; font-weight:bold; }
  table.settings { width:100%; border-collapse:collapse; font-size:12.5px; }
  table.settings td { border:1px solid #d8d8d8; padding:5px 8px; }
  table.settings td.k { background:#f3f3f3; width:40%; color:#333; }
  .viewbox { border:1px solid #e2e2e2; border-radius:4px; padding:4px; }
  .viewtitle { text-align:center; font-size:15px; font-weight:bold; margin:2px 0 4px 0; }
  canvas { display:block; }
  .footer { border-top:1px solid #c9c9c9; margin-top:8px; padding-top:6px; font-size:12.5px; }
  .footrow { display:flex; justify-content:space-between; gap:14px; padding:2px 4px; }
  .footrow .cell { white-space:nowrap; }
  .footrow b { font-weight:bold; }
  .legend { display:flex; gap:30px; font-size:11px; color:#1f7a1f; margin-top:6px; align-items:flex-start; }
  .legend .lg { display:flex; gap:6px; align-items:flex-start; max-width:30%; }
  table.items { width:100%; border-collapse:collapse; font-size:12px; margin-top:6px; }
  table.items td, table.items th { border:1px solid #cfcfcf; padding:3px 6px; text-align:left; }
  table.items td.k { background:#f3f3f3; font-weight:bold; width:18%; }
  .swatch { display:inline-block; width:34px; height:14px; vertical-align:middle; }
</style>
</head>
<body>
<div class="root">

  <div class="hdrstrip">
    <div><span class="lab">Payload : </span><span class="val" id="h_payload"></span></div>
    <div><span class="lab">MaxPayload : </span><span class="val" id="h_maxpay"></span></div>
    <div><span class="lab">Volume % : </span><span class="val" id="h_vol"></span></div>
    <div><span class="lab">Weight % : </span><span class="val" id="h_wt"></span></div>
  </div>

  <div class="layout">
    <div class="leftcol">
      <div class="panel">
        <h3>Optimization Settings :</h3>
        <table class="settings" id="tbl_settings"></table>
      </div>
      <div class="panel">
        <table class="items" id="tbl_lines"></table>
      </div>
    </div>

    <div class="rightcol">
      <div class="viewbox">
        <div class="viewtitle">3D</div>
        <div id="three" style="width:100%;height:300px;"></div>
      </div>
      <div class="viewbox">
        <div class="viewtitle">Top</div>
        <canvas id="cvTop" width="1000" height="150"></canvas>
      </div>
      <div class="viewbox">
        <div class="viewtitle">Side1</div>
        <canvas id="cvSide" width="1000" height="220"></canvas>
      </div>
    </div>
  </div>

  <div class="footer">
    <div class="footrow">
      <div class="cell">Floor spots&nbsp; = <b id="f_spots"></b></div>
      <div class="cell">C.G. height&nbsp; = <b id="f_cg"></b> (in)</div>
      <div class="cell">Airbag Space&nbsp; = <b id="f_airbag"></b> (in)</div>
      <div class="cell">Whole Unit Equivalent&nbsp; = <b id="f_wue"></b></div>
    </div>
    <div class="footrow">
      <div class="cell">Secure Loads from:&nbsp; <span class="swatch" id="sw_diag"></span> sliding&nbsp;&nbsp; <span class="swatch" id="sw_vert"></span> tipping &amp; sliding</div>
      <div class="cell">Total LISA Units&nbsp; = <b id="f_lisa"></b></div>
    </div>
    <table class="items" id="tbl_items"></table>
    <div class="legend" id="legend"></div>
  </div>
</div>

<script src="https://unpkg.com/three@0.160.0/build/three.min.js"></script>
<script>
(function(){

  var P = __PAYLOAD__;
  var meta = P.meta || {};
  var colors = P.colors || {};
  var cells = P.cells || [];
  var items = P.items || [];
  var SPOTS = meta.spots || 15;
  var TIERS = meta.tiers || 4;
  var turn = meta.turn_spot || 0;
  var blocked = meta.blocked_spot || 0;

  function fmt(n){ try { return Math.round(n).toLocaleString('en-US'); } catch(e){ return ''+n; } }
  function f2(n){ return (Math.round(n*100)/100).toFixed(2); }
  function f1(n){ return (Math.round(n*10)/10).toFixed(1); }
  function setTxt(id,v){ var el=document.getElementById(id); if(el) el.textContent=v; }
  function colFor(code){ var c=colors[code]||colors['A']||{}; return {fill:c.fill||'#7fd4cf', stroke:c.stroke||'#2b8e88', text:c.text||'#06403d'}; }

  setTxt('h_payload', fmt(meta.payload_lbs||0));
  setTxt('h_maxpay', fmt(meta.max_payload_lbs||0));
  setTxt('h_vol', Math.round(meta.volume_pct||0));
  setTxt('h_wt', Math.round(meta.weight_pct||0));

  var st = document.getElementById('tbl_settings');
  var ini = (meta.car_id||'').match(/^[A-Za-z]+/);
  var carno = (meta.car_id||'').replace(/^[A-Za-z]+/,'');
  var setRows = [
    ['Jurisdiction', '286 000'],
    ['Securement', 'Plywood'],
    ['Vehicle', '60 ft Plate F'],
    ['Umler Initial', ini? ini[0] : '\u2014'],
    ['Umler Car #', carno || '\u2014']
  ];
  setRows.forEach(function(r){
    var tr=document.createElement('tr');
    tr.innerHTML='<td class="k">'+r[0]+'</td><td>'+r[1]+'</td>';
    st.appendChild(tr);
  });

  setTxt('f_spots', meta.used_spots || SPOTS);
  setTxt('f_cg', f2(meta.cg_in||0));
  setTxt('f_airbag', f2(meta.airbag_in||0));
  setTxt('f_wue', f1(meta.total_units||0));
  setTxt('f_lisa', f1(meta.total_units||0));

  // ---- Load Lines table (left, like the request grid) ----
  var lt = document.getElementById('tbl_lines');
  var hdr=document.createElement('tr');
  hdr.innerHTML='<th>#</th><th>Description</th><th>Qty</th><th>Wt (Unit Wt.)</th>';
  lt.appendChild(hdr);
  items.forEach(function(it,i){
    var tr=document.createElement('tr');
    var tot = (it.wt? it.wt*it.qty : 0);
    tr.innerHTML='<td>'+(i+1)+'</td>'+
      '<td><b>Product: ['+it.id+']</b><br>'+(it.name||'')+'</td>'+
      '<td>'+it.qty+'</td>'+
      '<td><b>'+fmt(tot)+'</b><br>('+fmt(it.wt||0)+')</td>';
    lt.appendChild(tr);
  });

  // ---- Item detail block (bottom) ----
  var firstCode = (items[0]&&items[0].code)||'A';
  var cd = colFor(firstCode);
  var totalUnits = meta.total_units||0;
  var it0 = items[0]||{};
  var sz = (it0.L!=null&&it0.W!=null&&it0.H!=null) ? (f2(it0.L)+'x'+f2(it0.W)+'x'+f2(it0.H)) : '';
  var itbl=document.getElementById('tbl_items');
  function irow(k,v,swatch){
    var tr=document.createElement('tr');
    var vc = swatch ? '<td><span class="swatch" style="background:'+cd.fill+';border:1px solid '+cd.stroke+'"></span></td><td>'+v+'</td>' : '<td colspan="2">'+v+'</td>';
    tr.innerHTML='<td class="k">'+k+'</td>'+vc;
    itbl.appendChild(tr);
  }
  irow('ITEM ('+totalUnits+')', totalUnits, true);
  irow('Name/SKU', it0.name||'');
  irow('Product Id', it0.id||'');
  irow('Size (LxWxH) (in)', sz);
  irow('Weight (lb)', fmt(it0.wt||0));

  // ---- legend ----
  var lg=document.getElementById('legend');
  lg.innerHTML =
    '<div class="lg"><span class="swatch" style="background:repeating-linear-gradient(45deg,#1f7a1f 0 2px,#fff 2px 5px);border:1px solid #1f7a1f"></span> Diagonally hatched Loads must be restrained from sliding</div>'+
    '<div class="lg"><span class="swatch" style="background:repeating-linear-gradient(90deg,#1f7a1f 0 2px,#fff 2px 5px);border:1px solid #1f7a1f"></span> Vertically hatched Loads must be restrained from tipping and sliding</div>'+
    '<div class="lg">Any securement system used must prevent movement of all Loads blocked by the hatched Load</div>';
  var sd=document.getElementById('sw_diag'); if(sd){ sd.style.background='repeating-linear-gradient(45deg,#888 0 2px,#fff 2px 5px)'; sd.style.border='1px solid #888'; }
  var sv=document.getElementById('sw_vert'); if(sv){ sv.style.background='repeating-linear-gradient(90deg,#888 0 2px,#fff 2px 5px)'; sv.style.border='1px solid #888'; }

  // grid[spot][tier] = {pid,code} ; spot 1..SPOTS, tier 0..TIERS-1 (0=bottom)
  var grid = {};
  for (var sp=1; sp<=SPOTS; sp++){ grid[sp]=[]; for(var t=0;t<TIERS;t++) grid[sp][t]=null; }
  cells.forEach(function(c){ if(grid[c.spot]) grid[c.spot][c.tier]={pid:c.pid, code:c.code}; });

  // turn spots: the two doorway spots that hold rotated units (turn and turn-? ).
  // Reference shows rotated 'r' units occupying the turn column (spot==turn) and its neighbor blocked spot.
  function isTurnSpot(sp){ return sp===turn; }

  // Sequential numbering left->right. For each spot left->right, number tiers bottom->top.
  // Assign n=1.. across all occupied cells. Turn-spot units get 'r' suffix.
  var seq = 1;
  var labels = {}; // labels[sp][t] = {n, code, rot}
  for (var sp=1; sp<=SPOTS; sp++){
    labels[sp]=[];
    for (var t=0;t<TIERS;t++){
      var g=grid[sp][t];
      if(!g){ labels[sp][t]=null; continue; }
      var rot = isTurnSpot(sp);
      labels[sp][t]={ n:seq, code:g.code, rot:rot };
      seq++;
    }
  }

  function topLabel(sp,t){ var l=labels[sp]&&labels[sp][t]; if(!l) return null; return l.n + (l.rot?'r':'') + ' ' + l.code; }

  function drawTop(){
    var cv=document.getElementById('cvTop'); if(!cv) return;
    var W=cv.width, H=cv.height; var ctx=cv.getContext('2d');
    ctx.clearRect(0,0,W,H);
    var padL=10, padR=10, padT=20, padB=14;
    var innerW=W-padL-padR, innerH=H-padT-padB;
    var colW=innerW/SPOTS;
    // outline of car bed
    ctx.strokeStyle='#333'; ctx.lineWidth=1.5; ctx.strokeRect(padL,padT,innerW,innerH);
    for(var sp=1; sp<=SPOTS; sp++){
      var x=padL+(sp-1)*colW;
      // top view shows the top tier representative for normal spots
      var topT=TIERS-1;
      // find topmost occupied tier
      var occT=-1; for(var t=TIERS-1;t>=0;t--){ if(labels[sp]&&labels[sp][t]){ occT=t; break; } }
      if(occT<0){ continue; }
      if(isTurnSpot(sp)){
        // draw two stacked rotated cells (top tier and one below) within this column
        var rows=[]; for(var t=TIERS-1;t>=0;t--){ if(labels[sp][t]) rows.push(t); }
        var show = rows.slice(0,2); // top two for display
        var ch=innerH/2;
        show.forEach(function(t,ix){
          var y=padT+ix*ch;
          var c=colFor(labels[sp][t].code);
          ctx.fillStyle=c.fill; ctx.fillRect(x+1,y+1,colW-2,ch-2);
          ctx.strokeStyle='#cc2222'; ctx.lineWidth=1.4; ctx.strokeRect(x+1,y+1,colW-2,ch-2);
          ctx.fillStyle=c.text; ctx.font='bold 12px Arial'; ctx.textAlign='center'; ctx.textBaseline='middle';
          ctx.fillText(topLabel(sp,t), x+colW/2, y+ch/2);
        });
      } else {
        var c=colFor(labels[sp][occT].code);
        ctx.fillStyle=c.fill; ctx.fillRect(x+1,padT+1,colW-2,innerH-2);
        ctx.strokeStyle=c.stroke; ctx.lineWidth=1; ctx.strokeRect(x+1,padT+1,colW-2,innerH-2);
        // vertical text
        ctx.save();
        ctx.translate(x+colW/2, padT+innerH/2);
        ctx.rotate(-Math.PI/2);
        ctx.fillStyle=c.text; ctx.font='bold 13px Arial'; ctx.textAlign='center'; ctx.textBaseline='middle';
        ctx.fillText(topLabel(sp,occT), 0, 0);
        ctx.restore();
      }
    }
  }

  function sideLabel(sp,t){ var l=labels[sp]&&labels[sp][t]; if(!l) return null; return l.n + (l.rot?'r':'') + ' ' + l.code; }

  function drawSide(){
    var cv=document.getElementById('cvSide'); if(!cv) return;
    var W=cv.width, H=cv.height; var ctx=cv.getContext('2d');
    ctx.clearRect(0,0,W,H);
    var padL=10, padR=10, padT=8, padB=34;
    var innerW=W-padL-padR, innerH=H-padT-padB;
    var colW=innerW/SPOTS;
    var rowH=innerH/TIERS;
    for(var sp=1; sp<=SPOTS; sp++){
      var x=padL+(sp-1)*colW;
      if(isTurnSpot(sp)){
        // rotated turn block: one merged tall cell per visible row, red outline
        for(var t=0;t<TIERS;t++){
          var l=labels[sp][t]; if(!l) continue;
          var y=padT+(TIERS-1-t)*rowH;
          var c=colFor(l.code);
          ctx.fillStyle=c.fill; ctx.fillRect(x+1,y+1,colW-2,rowH-2);
          ctx.strokeStyle='#cc2222'; ctx.lineWidth=1.6; ctx.strokeRect(x+1,y+1,colW-2,rowH-2);
          ctx.fillStyle=c.text; ctx.font='bold 12px Arial'; ctx.textAlign='center'; ctx.textBaseline='middle';
          ctx.fillText(sideLabel(sp,t), x+colW/2, y+rowH/2);
        }
        continue;
      }
      for(var t=0;t<TIERS;t++){
        var l=labels[sp][t]; if(!l) continue;
        var y=padT+(TIERS-1-t)*rowH;
        var c=colFor(l.code);
        ctx.fillStyle=c.fill; ctx.fillRect(x+1,y+1,colW-2,rowH-2);
        ctx.strokeStyle=c.stroke; ctx.lineWidth=1; ctx.strokeRect(x+1,y+1,colW-2,rowH-2);
        ctx.fillStyle=c.text; ctx.font='11px Arial'; ctx.textAlign='center'; ctx.textBaseline='middle';
        ctx.fillText(sideLabel(sp,t), x+colW/2, y+rowH/2);
      }
    }
    // baseline + wheels
    ctx.strokeStyle='#333'; ctx.lineWidth=1.5;
    ctx.beginPath(); ctx.moveTo(padL,padT+innerH); ctx.lineTo(padL+innerW,padT+innerH); ctx.stroke();
    var wy=padT+innerH+14;
    [0.07,0.13,0.19, 0.81,0.87,0.93].forEach(function(fx){
      var wx=padL+innerW*fx;
      ctx.fillStyle='#555'; ctx.beginPath(); ctx.arc(wx,wy,9,0,Math.PI*2); ctx.fill();
      ctx.fillStyle='#999'; ctx.beginPath(); ctx.arc(wx,wy,3,0,Math.PI*2); ctx.fill();
    });
  }

  drawTop(); drawSide();

  function draw3D(){
    var host=document.getElementById('three'); if(!host || !window.THREE) return;
    var w=host.clientWidth||500, h=host.clientHeight||300;
    var scene=new THREE.Scene(); scene.background=new THREE.Color(0xffffff);
    var cam=new THREE.PerspectiveCamera(45, w/h, 0.1, 5000);
    var renderer=new THREE.WebGLRenderer({antialias:true});
    renderer.setSize(w,h); host.appendChild(renderer.domElement);
    scene.add(new THREE.AmbientLight(0xffffff,0.75));
    var dl=new THREE.DirectionalLight(0xffffff,0.7); dl.position.set(1,2,1); scene.add(dl);

    var uW=2.0, uH=1.0, uD=3.0, gap=0.12;
    var group=new THREE.Group();
    var col = colFor((items[0]&&items[0].code)||'A');
    var matFill=new THREE.MeshLambertMaterial({color:new THREE.Color(col.fill)});
    var totalLen = SPOTS*(uW+gap);
    for(var sp=1; sp<=SPOTS; sp++){
      var px = (sp-1)*(uW+gap) - totalLen/2;
      for(var t=0;t<TIERS;t++){
        if(!(labels[sp]&&labels[sp][t])) continue;
        var rot = labels[sp][t].rot;
        var geo = rot ? new THREE.BoxGeometry(uD*0.6,uH,uW) : new THREE.BoxGeometry(uW,uH,uD);
        var mesh=new THREE.Mesh(geo,matFill);
        mesh.position.set(px, t*(uH+0.05)+uH/2, 0);
        group.add(mesh);
        var edges=new THREE.LineSegments(new THREE.EdgesGeometry(geo), new THREE.LineBasicMaterial({color:0x2b8e88}));
        edges.position.copy(mesh.position); group.add(edges);
      }
    }
    scene.add(group);
    cam.position.set(totalLen*0.55, TIERS*1.6, totalLen*0.75);
    cam.lookAt(0, TIERS*0.5, 0);
    function animate(){ requestAnimationFrame(animate); renderer.render(scene,cam); }
    animate();
  }
  if(window.THREE){ draw3D(); } else { window.addEventListener('load', draw3D); }

})();
</script>

</body>
</html>
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
