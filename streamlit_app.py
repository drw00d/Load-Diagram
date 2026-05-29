# streamlit_app.py
# LoadXpert Route A - Presentation1 Rules Integrated
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
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


# =============================
# Page
# =============================
st.set_page_config(page_title="LoadXpert Building Products", layout="wide")

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
        return SecurementDecision(x, True, "Straps: Yes - Double strapping", "Diagonal hatch = cord strap required (step-down)")
    if x >= 10.0:
        return SecurementDecision(x, True, "Straps: Yes - 2-unit double strapping", "Diagonal hatch = cord strap required (step-down)")
    return SecurementDecision(x, True, "Straps: Yes - 4-unit double strapping", "Diagonal hatch = cord strap required (step-down)")


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
  var halfStep = 0;
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
    var y0 = bandTop + (bandH - boxH)/2;
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
  for(var i=0;i<leftN;i++){ var sp=leftSpots[i]; var d=leftN-1-i; var raise=0; stack3D(sp, x+UW/2, raise); x+=UW+gap; }
  if(TURN>0){ var tcx=x+UW; for(var t=0;t<TIERS;t++){ var cell=grid[TURN][t]; if(!cell) continue; var cy=t*(UH+0.1)+UH/2; box(tcx,cy,0,UD,UH,UW*2,colorFor(cell.code),true); }
    // red doorway wireframe
    var dg=new THREE.BoxGeometry(UW*2+gap,TIERS*(UH+0.1)+1,UD+1); var de=new THREE.LineSegments(new THREE.EdgesGeometry(dg), new THREE.LineBasicMaterial({color:0xdd0000})); de.position.set(tcx,TIERS*(UH+0.1)/2,0); scene.add(de); x+=UW*2+gap; }
  for(var j=0;j<rightN;j++){ var sp2=rightSpots[j]; var raise2=0; stack3D(sp2, x+UW/2, raise2); x+=UW+gap; }
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
# LoadXpert Building Products replica state + UI helpers
# =============================
BP_MODES = ["Centerbeam", "Van", "Flatbed", "Intermodal"]
BP_VEHICLES = ["Centerbeam_WithRiser", "60 ft Plate F", "53 ft Dry Van", "48 ft Flatbed"]
BP_SECUREMENTS = ["Standard BP Securement", "GrantLoading", "Airbag + Strap", "Dunnage/Blocker"]
BP_JURISDICTIONS = ["US Rail", "US Highway", "Canada Rail", "Southeast Region", "West Region"]
BP_DATA_SOURCES = ["MES Live", "Planning Sandbox", "Archived Loads"]
BP_HISTORY = ["Today", "Last 7 days", "Last 30 days", "All history"]


def apply_loadxpert_theme() -> None:
    st.markdown(
        """
        <style>
        .block-container {padding-top: 1.4rem; padding-bottom: 2.5rem;}
        [data-testid="stSidebar"] {background: #1f2933;}
        [data-testid="stSidebar"] * {color: #f4f7fb;}
        [data-testid="stSidebar"] .stSelectbox label,
        [data-testid="stSidebar"] .stSlider label,
        [data-testid="stSidebar"] .stNumberInput label,
        [data-testid="stSidebar"] .stCheckbox label {color: #f4f7fb;}
        .lx-topbar {
            background: #202a33;
            color: #fff;
            padding: 14px 18px;
            border-radius: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            margin-bottom: 16px;
        }
        .lx-brand {font-size: 21px; font-weight: 700; letter-spacing: 0;}
        .lx-subtle {font-size: 12px; color: #b8c2cc;}
        .lx-pill {
            display: inline-flex;
            align-items: center;
            padding: 4px 9px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 700;
            background: #e7f0ff;
            color: #174ea6;
            margin-right: 6px;
            white-space: nowrap;
        }
        .lx-pill.green {background: #dff5e6; color: #17643a;}
        .lx-pill.amber {background: #fff1cc; color: #855900;}
        .lx-pill.red {background: #ffe0de; color: #a12a1f;}
        .lx-panel {
            border: 1px solid #d9e2ec;
            border-radius: 8px;
            padding: 14px 16px;
            background: #fff;
            margin-bottom: 12px;
        }
        .lx-section-title {font-size: 18px; font-weight: 700; margin: 2px 0 10px;}
        .lx-meta-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(120px, 1fr));
            gap: 10px;
            margin-bottom: 12px;
        }
        .lx-meta {
            border: 1px solid #e4e9f0;
            border-radius: 8px;
            padding: 10px 12px;
            background: #f8fafc;
        }
        .lx-meta .label {font-size: 11px; color: #627386; text-transform: uppercase;}
        .lx-meta .value {font-size: 17px; color: #1f2933; font-weight: 700;}
        .lx-scenario {
            border: 1px solid #d9e2ec;
            border-radius: 8px;
            padding: 12px;
            background: #fbfdff;
            min-height: 130px;
        }
        .lx-scenario.accepted {border-color: #2f9e58; background: #f2fbf5;}
        @media (max-width: 900px) {
            .lx-topbar {display: block;}
            .lx-meta-grid {grid-template-columns: repeat(2, minmax(120px, 1fr));}
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_topbar(page_label: str) -> None:
    st.markdown(
        f"""
        <div class="lx-topbar">
          <div>
            <div class="lx-brand">LoadXpert Building Products</div>
            <div class="lx-subtle">Planning board, optimization, securement, load-plan imaging, and dunnage workflow</div>
          </div>
          <div>
            <span class="lx-pill green">GPBP</span>
            <span class="lx-pill">Replica</span>
            <span class="lx-pill amber">{page_label}</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _first_product_ids(pm: pd.DataFrame, commodity: str, limit: int, offset: int = 0) -> List[str]:
    df = pm.copy()
    if COL_COMMODITY in df.columns and commodity:
        scoped = df[df[COL_COMMODITY].astype(str).str.upper() == commodity.upper()].copy()
        if not scoped.empty:
            df = scoped
    df = df.drop_duplicates(subset=[COL_PRODUCT_ID], keep="first").sort_values(COL_PRODUCT_ID)
    ids = df[COL_PRODUCT_ID].astype(str).tolist()
    if not ids:
        return []
    out: List[str] = []
    for i in range(limit):
        out.append(ids[(offset + i) % len(ids)])
    return out


def _make_lines(pm: pd.DataFrame, commodity: str, tiers: List[int], offset: int = 0) -> List[Dict[str, Any]]:
    pids = _first_product_ids(pm, commodity, len(tiers), offset)
    return [{"product_id": pid, "tiers": int(tier)} for pid, tier in zip(pids, tiers)]


def build_demo_orders(pm: pd.DataFrame) -> List[Dict[str, Any]]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return [
        {
            "id": 24051701,
            "orderId": "BP-240517-001",
            "batchId": "MES-517-A",
            "destination": "Atlanta, GA",
            "shipTo": "Southeast DC",
            "weightRestriction": "286k GRL",
            "carType": "Centerbeam",
            "mode": "Centerbeam",
            "grant": "Yes",
            "vehicleName": "Centerbeam_WithRiser",
            "securement": "GrantLoading",
            "jurisdictionName": "US Rail",
            "umlerInitial": "TBOX",
            "umlerCarNumber": "632012",
            "recordStatus": "New",
            "accepted": False,
            "createdAt": now,
            "requestLines": _make_lines(pm, "PLY", [4, 4, 3, 2], 0),
        },
        {
            "id": 24051702,
            "orderId": "BP-240517-002",
            "batchId": "MES-517-B",
            "destination": "Dallas, TX",
            "shipTo": "Southwest Yard",
            "weightRestriction": "263k GRL",
            "carType": "Centerbeam",
            "mode": "Centerbeam",
            "grant": "No",
            "vehicleName": "60 ft Plate F",
            "securement": "Standard BP Securement",
            "jurisdictionName": "US Rail",
            "umlerInitial": "TTGX",
            "umlerCarNumber": "941220",
            "recordStatus": "New",
            "accepted": False,
            "createdAt": now,
            "requestLines": _make_lines(pm, "OSB", [5, 4, 4], 3),
        },
        {
            "id": 24051703,
            "orderId": "BP-240517-003",
            "batchId": "MES-517-C",
            "destination": "Charlotte, NC",
            "shipTo": "Retail Pool",
            "weightRestriction": "High cube van",
            "carType": "Van",
            "mode": "Van",
            "grant": "No",
            "vehicleName": "53 ft Dry Van",
            "securement": "Airbag + Strap",
            "jurisdictionName": "US Highway",
            "umlerInitial": "",
            "umlerCarNumber": "",
            "recordStatus": "Planning",
            "accepted": False,
            "createdAt": now,
            "requestLines": _make_lines(pm, "MDF", [3, 3, 2, 2], 7),
        },
        {
            "id": 24051704,
            "orderId": "BP-240517-004",
            "batchId": "MES-517-D",
            "destination": "Spokane, WA",
            "shipTo": "West Reload",
            "weightRestriction": "Mixed commodity",
            "carType": "Flatbed",
            "mode": "Flatbed",
            "grant": "No",
            "vehicleName": "48 ft Flatbed",
            "securement": "Dunnage/Blocker",
            "jurisdictionName": "West Region",
            "umlerInitial": "",
            "umlerCarNumber": "",
            "recordStatus": "New",
            "accepted": False,
            "createdAt": now,
            "requestLines": _make_lines(pm, "PB", [4, 3, 3], 2),
        },
    ]


def ensure_replica_state(pm: pd.DataFrame) -> None:
    if "bp_orders" not in st.session_state:
        st.session_state.bp_orders = build_demo_orders(pm)
    if "bp_workspace" not in st.session_state:
        st.session_state.bp_workspace = "Planning Board"
    if "bp_current_order_id" not in st.session_state and st.session_state.bp_orders:
        st.session_state.bp_current_order_id = st.session_state.bp_orders[0]["id"]
    if "bp_search" not in st.session_state:
        st.session_state.bp_search = ""
    if "bp_last_message" not in st.session_state:
        st.session_state.bp_last_message = ""
    if "bp_dunnage" not in st.session_state:
        st.session_state.bp_dunnage = [
            {"title": "9 in Airbag", "dunnageType": "airbag", "supplier": "LoadXpert", "category": "general"},
            {"title": "Double Cord Strap", "dunnageType": "strap", "supplier": "LoadXpert", "category": "securement"},
            {"title": '3" Honeycomb Void Fill', "dunnageType": "spacer", "supplier": "GP", "category": "void fill"},
            {"title": "Doorway Blocker", "dunnageType": "blocker", "supplier": "GP", "category": "railcar"},
        ]
    if "bp_annotations" not in st.session_state:
        st.session_state.bp_annotations = []


def get_order_by_id(order_id: Optional[int]) -> Optional[Dict[str, Any]]:
    for order in st.session_state.get("bp_orders", []):
        if int(order["id"]) == int(order_id or -1):
            return order
    return None


def current_order() -> Optional[Dict[str, Any]]:
    return get_order_by_id(st.session_state.get("bp_current_order_id"))


def products_for_lines(pm: pd.DataFrame, lines: List[Dict[str, Any]]) -> Tuple[Dict[str, Product], List[str]]:
    products: Dict[str, Product] = {}
    errors: List[str] = []
    for line in lines:
        pid = str(line.get("product_id", "")).strip()
        if not pid:
            continue
        try:
            products[pid] = lookup_product(pm, pid)
        except Exception as exc:
            errors.append(f"{pid}: {exc}")
    return products, errors


def request_lines_from_order(order: Dict[str, Any]) -> List[RequestLine]:
    return [
        RequestLine(product_id=str(line["product_id"]), tiers=int(line.get("tiers", 0)))
        for line in order.get("requestLines", [])
        if int(line.get("tiers", 0)) > 0
    ]


def run_order_optimization(
    order: Dict[str, Any],
    pm: pd.DataFrame,
    *,
    max_tiers: int,
    turn_spot: int,
    required_turn_tiers: int,
    close_top_weight: int,
    weight_balance_weight: int,
    tg_safety_weight: int,
    stagger_weight: int,
) -> List[str]:
    products, errors = products_for_lines(pm, order.get("requestLines", []))
    if errors:
        order["recordStatus"] = "Error"
        return errors
    requests = request_lines_from_order(order)
    if not requests:
        order["recordStatus"] = "Error"
        return ["No load lines are available for this order."]

    matrix, msgs = optimize_layout(
        products=products,
        requests=requests,
        max_tiers=int(max_tiers),
        turn_spot=int(turn_spot),
        required_turn_tiers=int(required_turn_tiers),
        close_top_weight=int(close_top_weight),
        weight_balance_weight=int(weight_balance_weight),
        tg_safety_weight=int(tg_safety_weight),
        stagger_weight=int(stagger_weight),
    )
    order["matrix"] = matrix
    order["turn_spot"] = int(turn_spot)
    order["max_tiers"] = int(max_tiers)
    order["recordStatus"] = "Optimized" if not msgs else "Optimized With Warnings"
    order["optimizedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    order["requestId"] = f"REQ-{order['id']}"
    st.session_state.bp_current_order_id = order["id"]
    return msgs


def analyze_order(
    order: Dict[str, Any],
    pm: pd.DataFrame,
    *,
    A_deck: float,
    B_empty_cg: float,
    E_tare: float,
    cg_limit_in: float,
    C_override_val: Optional[float],
) -> Tuple[Optional[AnalysisResult], Dict[str, Product], List[str]]:
    products, errors = products_for_lines(pm, order.get("requestLines", []))
    matrix = order.get("matrix")
    if not matrix:
        return None, products, errors
    analysis = analyze_layout(
        matrix=matrix,
        products=products,
        turn_spot=int(order.get("turn_spot", 7)),
        A_deck=float(A_deck),
        B_empty_cg=float(B_empty_cg),
        E_tare=float(E_tare),
        cg_limit_in=float(cg_limit_in),
        override_C=(float(C_override_val) if C_override_val is not None else None),
    )
    return analysis, products, errors


def order_summary(order: Dict[str, Any], pm: pd.DataFrame) -> Dict[str, Any]:
    products, _ = products_for_lines(pm, order.get("requestLines", []))
    tiers = sum(int(line.get("tiers", 0)) for line in order.get("requestLines", []))
    weight = 0.0
    for line in order.get("requestLines", []):
        p = products.get(str(line.get("product_id")))
        if p:
            weight += p.unit_weight_lbs * int(line.get("tiers", 0))
    return {"tiers": tiers, "weight": weight, "sku_count": len(order.get("requestLines", []))}


def queue_dataframe(pm: pd.DataFrame, orders: List[Dict[str, Any]], search: str = "") -> pd.DataFrame:
    rows = []
    s = search.strip().lower()
    for order in orders:
        if s and s not in " ".join(
            [
                str(order.get("orderId", "")),
                str(order.get("destination", "")),
                str(order.get("shipTo", "")),
                str(order.get("recordStatus", "")),
            ]
        ).lower():
            continue
        summary = order_summary(order, pm)
        rows.append(
            {
                "Select": False,
                "id": int(order["id"]),
                "Order No's": order["orderId"],
                "Destination": order["destination"],
                "Weight Restriction": order["weightRestriction"],
                "Mode": order["mode"],
                "Grant": order["grant"],
                "Vehicle": order["vehicleName"],
                "Securement": order["securement"],
                "Umler Initial": order["umlerInitial"],
                "Umler Car No": order["umlerCarNumber"],
                "Tiers": summary["tiers"],
                "Est Weight": round(summary["weight"], 0),
                "Status": order["recordStatus"],
            }
        )
    return pd.DataFrame(rows)


def sync_orders_from_editor(edited_df: pd.DataFrame) -> List[int]:
    selected: List[int] = []
    for row in edited_df.to_dict("records"):
        order = get_order_by_id(int(row["id"]))
        if not order:
            continue
        order["grant"] = str(row.get("Grant", order["grant"]))
        order["vehicleName"] = str(row.get("Vehicle", order["vehicleName"]))
        order["securement"] = str(row.get("Securement", order["securement"]))
        order["umlerInitial"] = str(row.get("Umler Initial", order["umlerInitial"]))
        order["umlerCarNumber"] = str(row.get("Umler Car No", order["umlerCarNumber"]))
        if bool(row.get("Select", False)):
            selected.append(int(order["id"]))
    return selected


def product_option_label(row: Dict[str, Any]) -> str:
    pid = str(row.get(COL_PRODUCT_ID, "")).strip()
    desc = str(row.get(COL_DESC, "")).strip()
    edge = str(row.get(COL_EDGE, "")).strip()
    wt = row.get(COL_UNIT_WT)
    bits = [pid]
    if pd.notna(wt):
        bits.append(f"{float(wt):,.0f} lbs")
    if edge:
        bits.append(edge)
    if desc:
        bits.append(desc)
    return " | ".join(bits)


def status_badge(status: str) -> str:
    normalized = str(status or "").lower()
    css = "green" if "optimized" in normalized or "accepted" in normalized else "amber"
    if "error" in normalized:
        css = "red"
    return f'<span class="lx-pill {css}">{status}</span>'


def clean_securement_text(text: str) -> str:
    value = str(text)
    for token in (
        "\u2014",
        "\u2013",
        "\u00e2\u20ac\u201d",
        "\u00c3\u00a2\u00e2\u201a\u00ac\u00e2\u20ac\u009d",
    ):
        value = value.replace(token, "-")
    return value


def render_meta_grid(items: List[Tuple[str, str]]) -> None:
    cells = "".join(
        f'<div class="lx-meta"><div class="label">{label}</div><div class="value">{value}</div></div>'
        for label, value in items
    )
    st.markdown(f'<div class="lx-meta-grid">{cells}</div>', unsafe_allow_html=True)


def render_rule_metrics(analysis: AnalysisResult) -> None:
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("% Blocked", f"{analysis.securement.percent_blocked:.1f}%")
    with c2:
        st.metric("Securement", clean_securement_text(analysis.securement.strap_text))
    with c3:
        st.metric("CG above TOR", f"{analysis.cg_above_tor_in:.2f} in", analysis.cg_status)
    with c4:
        st.metric("Weight balance", f"{analysis.weight_balance_ratio * 100:.1f}%")




# =============================
# LoadXpert Building Products replica app
# =============================
try:
    pm = load_product_master(MASTER_PATH)
except Exception as e:
    st.error(f"Could not load Product Master at '{MASTER_PATH}'. Error: {e}")
    st.stop()

ensure_replica_state(pm)
apply_loadxpert_theme()

if st.session_state.get("bp_workspace_pending"):
    st.session_state.bp_workspace = st.session_state.pop("bp_workspace_pending")

with st.sidebar:
    st.header("LoadXpert")
    workspace = st.radio(
        "Workspace",
        ["Planning Board", "Order Details", "Load Plan View", "Dunnage & Annotations"],
        key="bp_workspace",
        label_visibility="collapsed",
    )

    st.divider()
    st.subheader("Planning Controls")
    history_selected = st.selectbox("History", BP_HISTORY, index=1)
    data_source_selected = st.selectbox("Data Source", BP_DATA_SOURCES, index=0)
    mode_selected = st.selectbox("Mode", ["All Modes"] + BP_MODES, index=0)

    st.divider()
    st.subheader("Railcar Rules")
    max_tiers = st.slider("Max tiers per spot", 1, 10, 4)
    turn_spot = int(st.selectbox("Turn spot", ["7", "8"], index=0))
    required_turn_tiers = st.slider("Turn tiers required", 0, int(max_tiers), int(max_tiers))

    gap_labels = [f"{a}-{b}" for a, b in AIRBAG_ALLOWED_GAPS]
    gap_choice_label = st.selectbox("Airbag location", gap_labels, index=1)
    airbag_gap_choice = AIRBAG_ALLOWED_GAPS[gap_labels.index(gap_choice_label)]
    airbag_gap_in = st.slider("Airbag space (in)", 6.0, 12.0, 9.0, 0.5)

    st.divider()
    st.subheader("Optimization Weights")
    close_top_weight = st.slider("Close top", 0, 100, 20, 5)
    weight_balance_weight = st.slider("Weight balance", 0, 100, 10, 5)
    tg_safety_weight = st.slider("T&G safety", 0, 150, 50, 5)
    stagger_weight = st.slider("Adjacent stagger", 0, 100, 25, 5)

    st.divider()
    st.subheader("Diagram Colors")
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
    st.subheader("CG Inputs")
    A_deck = st.number_input("Deck height above TOR minus spring deflection (in)", min_value=0.0, value=48.0, step=0.1)
    B_empty_cg = st.number_input("Empty car CG above TOR (in)", min_value=0.0, value=56.0, step=0.1)
    E_tare = st.number_input("Tare weight (lbs)", min_value=1.0, value=75000.0, step=100.0)
    cg_limit_in = st.number_input("CG limit above TOR (in)", min_value=1.0, value=98.0, step=0.5)
    override_C = st.checkbox("Override load CG above deck", value=False)
    C_override_val: Optional[float] = None
    if override_C:
        C_override_val = st.number_input("Load CG above deck (in)", min_value=0.0, value=30.0, step=0.5)

show_3d = True
show_edges = True
cam_fov = 42.0
cam_x = 10.0
cam_y = 10.0
cam_z = 18.0
light_intensity = 1.2
ambient_intensity = 0.65
flip_side = False

render_topbar(workspace)

orders = st.session_state.bp_orders
filtered_orders = orders if mode_selected == "All Modes" else [o for o in orders if o["mode"] == mode_selected]

if st.session_state.bp_last_message:
    st.success(st.session_state.bp_last_message)

render_meta_grid(
    [
        ("Product Master", f"{len(pm):,} SKUs"),
        ("Source", data_source_selected),
        ("History", history_selected),
        ("Active Orders", str(len(filtered_orders))),
    ]
)

if workspace == "Planning Board":
    st.markdown('<div class="lx-section-title">Building Products Planning Tool</div>', unsafe_allow_html=True)
    top_a, top_b, top_c, top_d = st.columns([2.2, 1, 1, 1], vertical_alignment="bottom")
    with top_a:
        st.session_state.bp_search = st.text_input("Order search", value=st.session_state.bp_search)
    with top_b:
        soft_search = st.button("Soft Search", width="stretch")
    with top_c:
        hard_refresh = st.button("Hard Refresh", width="stretch")
    with top_d:
        reset_board = st.button("Reset", width="stretch")

    if hard_refresh:
        st.session_state.bp_orders = build_demo_orders(pm)
        st.session_state.bp_last_message = "Planning board refreshed from the local MES-style demo feed."
        st.rerun()
    if reset_board:
        st.session_state.bp_search = ""
        st.session_state.bp_last_message = "Filters cleared."
        st.rerun()
    if soft_search:
        st.session_state.bp_last_message = "Soft search applied."

    queue_df = queue_dataframe(pm, filtered_orders, st.session_state.bp_search)
    if queue_df.empty:
        st.info("No Building Products orders match the current filters.")
    else:
        edited_df = st.data_editor(
            queue_df,
            width="stretch",
            hide_index=True,
            height=360,
            column_config={
                "Select": st.column_config.CheckboxColumn("Select"),
                "Grant": st.column_config.SelectboxColumn("Grant", options=["Yes", "No"]),
                "Vehicle": st.column_config.SelectboxColumn("Vehicle", options=BP_VEHICLES),
                "Securement": st.column_config.SelectboxColumn("Securement", options=BP_SECUREMENTS),
                "id": st.column_config.NumberColumn("ID", disabled=True),
                "Est Weight": st.column_config.NumberColumn("Est Weight", format="%.0f"),
            },
            disabled=[
                "id",
                "Order No's",
                "Destination",
                "Weight Restriction",
                "Mode",
                "Tiers",
                "Est Weight",
                "Status",
            ],
            key="bp_queue_editor",
        )
        selected_ids = sync_orders_from_editor(edited_df)

        act_a, act_b, act_c, act_d = st.columns([1.1, 1.1, 1.1, 2], vertical_alignment="center")
        with act_a:
            if st.button("Optimize Selected", type="primary", width="stretch", disabled=not selected_ids):
                all_msgs: List[str] = []
                for oid in selected_ids:
                    order = get_order_by_id(oid)
                    if order:
                        all_msgs.extend(
                            run_order_optimization(
                                order,
                                pm,
                                max_tiers=int(max_tiers),
                                turn_spot=int(turn_spot),
                                required_turn_tiers=int(required_turn_tiers),
                                close_top_weight=int(close_top_weight),
                                weight_balance_weight=int(weight_balance_weight),
                                tg_safety_weight=int(tg_safety_weight),
                                stagger_weight=int(stagger_weight),
                            )
                        )
                st.session_state.bp_last_message = f"Optimized {len(selected_ids)} order(s)."
                if all_msgs:
                    st.session_state.bp_last_message += f" {len(all_msgs)} warning(s) recorded."
                st.rerun()
        with act_b:
            if st.button("Open Details", width="stretch", disabled=not selected_ids):
                st.session_state.bp_current_order_id = selected_ids[0]
                st.session_state.bp_workspace_pending = "Order Details"
                st.session_state.bp_last_message = "Selected order opened in details."
                st.rerun()
        with act_c:
            if st.button("View Load Plan", width="stretch", disabled=not selected_ids):
                st.session_state.bp_current_order_id = selected_ids[0]
                st.session_state.bp_workspace_pending = "Load Plan View"
                st.session_state.bp_last_message = "Selected order opened in load-plan view."
                st.rerun()
        with act_d:
            st.caption(f"{len(selected_ids)} selected" if selected_ids else "Select one or more orders in the grid.")

    st.markdown('<div class="lx-section-title">Status Board</div>', unsafe_allow_html=True)
    status_rows = []
    for order in filtered_orders:
        analysis, _, _ = analyze_order(
            order,
            pm,
            A_deck=A_deck,
            B_empty_cg=B_empty_cg,
            E_tare=E_tare,
            cg_limit_in=cg_limit_in,
            C_override_val=C_override_val,
        )
        status_rows.append(
            {
                "Order": order["orderId"],
                "Status": order["recordStatus"],
                "Payload": round(analysis.payload_lbs, 0) if analysis else None,
                "Blocked %": round(analysis.securement.percent_blocked, 1) if analysis else None,
                "CG": analysis.cg_status if analysis else "",
                "Accepted": "Yes" if order.get("accepted") else "No",
            }
        )
    st.dataframe(pd.DataFrame(status_rows), width="stretch", hide_index=True)

elif workspace == "Order Details":
    order = current_order()
    if not order:
        st.warning("Choose an order on the planning board first.")
        st.stop()

    st.markdown(
        f'<div class="lx-section-title">Order - {order["orderId"]} {status_badge(order["recordStatus"])}</div>',
        unsafe_allow_html=True,
    )

    detail_a, detail_b, detail_c, detail_d = st.columns(4)
    with detail_a:
        order["jurisdictionName"] = st.selectbox(
            "Jurisdiction",
            BP_JURISDICTIONS,
            index=BP_JURISDICTIONS.index(order["jurisdictionName"]) if order["jurisdictionName"] in BP_JURISDICTIONS else 0,
        )
    with detail_b:
        order["securement"] = st.selectbox(
            "Securement",
            BP_SECUREMENTS,
            index=BP_SECUREMENTS.index(order["securement"]) if order["securement"] in BP_SECUREMENTS else 0,
        )
    with detail_c:
        order["vehicleName"] = st.selectbox(
            "Vehicle",
            BP_VEHICLES,
            index=BP_VEHICLES.index(order["vehicleName"]) if order["vehicleName"] in BP_VEHICLES else 0,
        )
    with detail_d:
        order["grant"] = st.selectbox("Grant", ["Yes", "No"], index=0 if order["grant"] == "Yes" else 1)

    um_a, um_b, um_c = st.columns([1, 1, 2])
    with um_a:
        order["umlerInitial"] = st.text_input("Umler Initial", value=order.get("umlerInitial", ""))
    with um_b:
        order["umlerCarNumber"] = st.text_input("Umler Car Number", value=order.get("umlerCarNumber", ""))
    with um_c:
        st.caption(f"Destination: {order['destination']} | Ship To: {order['shipTo']} | Batch: {order['batchId']}")

    products, product_errors = products_for_lines(pm, order.get("requestLines", []))
    for err in product_errors:
        st.error(err)

    line_rows = []
    for line in order.get("requestLines", []):
        product = products.get(str(line["product_id"]))
        line_rows.append(
            {
                "Sales Product Id": line["product_id"],
                "Description": product.description if product else "",
                "Edge": product.edge_type if product else "",
                "Unit Weight": product.unit_weight_lbs if product else None,
                "Tiers": int(line.get("tiers", 0)),
                "T&G": bool(product.is_tg) if product else False,
            }
        )
    st.dataframe(pd.DataFrame(line_rows), width="stretch", hide_index=True, height=220)

    with st.expander("Add product line", expanded=False):
        commodities = sorted(pm[COL_COMMODITY].dropna().astype(str).unique().tolist())
        add_c1, add_c2, add_c3 = st.columns([1, 2.6, 0.8], vertical_alignment="bottom")
        with add_c1:
            add_commodity = st.selectbox("Commodity", commodities, key="detail_add_commodity")
        add_df = pm[pm[COL_COMMODITY].astype(str) == str(add_commodity)].copy()
        add_df = add_df.drop_duplicates(subset=[COL_PRODUCT_ID], keep="first").sort_values(COL_PRODUCT_ID).head(1000)
        add_options = add_df.to_dict("records")
        add_labels = [product_option_label(row) for row in add_options]
        with add_c2:
            add_label = st.selectbox("Product", add_labels, key="detail_add_product") if add_labels else None
        with add_c3:
            add_tiers = st.number_input("Tiers", min_value=1, max_value=10, value=2, key="detail_add_tiers")
        if st.button("Add Line", disabled=not add_label):
            selected = add_options[add_labels.index(add_label)]
            order["requestLines"].append({"product_id": str(selected[COL_PRODUCT_ID]), "tiers": int(add_tiers)})
            st.session_state.bp_last_message = "Product line added."
            st.rerun()

    det_a, det_b, det_c, det_d = st.columns([1, 1, 1, 2])
    with det_a:
        if st.button("Optimize", type="primary", width="stretch"):
            msgs = run_order_optimization(
                order,
                pm,
                max_tiers=int(max_tiers),
                turn_spot=int(turn_spot),
                required_turn_tiers=int(required_turn_tiers),
                close_top_weight=int(close_top_weight),
                weight_balance_weight=int(weight_balance_weight),
                tg_safety_weight=int(tg_safety_weight),
                stagger_weight=int(stagger_weight),
            )
            st.session_state.bp_last_message = "Order optimized." + (f" {len(msgs)} warning(s)." if msgs else "")
            st.rerun()
    with det_b:
        if st.button("Reset", width="stretch"):
            order.pop("matrix", None)
            order["recordStatus"] = "Planning"
            order["accepted"] = False
            st.session_state.bp_last_message = "Order reset to planning."
            st.rerun()
    with det_c:
        if st.button("Accept Scenario", width="stretch", disabled="matrix" not in order):
            order["accepted"] = True
            order["recordStatus"] = "Accepted"
            st.session_state.bp_last_message = "Scenario accepted."
            st.rerun()
    with det_d:
        if st.button("Open Load Plan View", width="stretch", disabled="matrix" not in order):
            st.session_state.bp_workspace_pending = "Load Plan View"
            st.session_state.bp_last_message = "Load plan ready."
            st.rerun()

    analysis, _, _ = analyze_order(
        order,
        pm,
        A_deck=A_deck,
        B_empty_cg=B_empty_cg,
        E_tare=E_tare,
        cg_limit_in=cg_limit_in,
        C_override_val=C_override_val,
    )
    if analysis:
        st.markdown('<div class="lx-section-title">Scenarios</div>', unsafe_allow_html=True)
        s1, s2, s3 = st.columns(3)
        with s1:
            st.markdown(
                f"""
                <div class="lx-scenario">
                  <b>Base Case</b><br>
                  Payload: {analysis.payload_lbs:,.0f} lbs<br>
                  Blocked: {analysis.securement.percent_blocked:.1f}%<br>
                  CG: {analysis.cg_status}
                </div>
                """,
                unsafe_allow_html=True,
            )
        with s2:
            st.markdown(
                f"""
                <div class="lx-scenario {'accepted' if order.get('accepted') else ''}">
                  <b>Advise Case</b><br>
                  Vehicle: {order['vehicleName']}<br>
                  Securement: {order['securement']}<br>
                  {clean_securement_text(analysis.securement.strap_text)}
                </div>
                """,
                unsafe_allow_html=True,
            )
        with s3:
            honeycomb = "Required" if analysis.honeycomb_required else "Not required"
            st.markdown(
                f"""
                <div class="lx-scenario">
                  <b>Securement Notes</b><br>
                  Honeycomb: {honeycomb}<br>
                  Hatched spots: {", ".join(map(str, analysis.hatched_spots)) or "None"}<br>
                  Balance: {analysis.weight_balance_ratio * 100:.1f}%
                </div>
                """,
                unsafe_allow_html=True,
            )
        render_rule_metrics(analysis)
    else:
        st.info("Optimize this order to create scenarios and load-plan outputs.")

elif workspace == "Load Plan View":
    order = current_order()
    if not order:
        st.warning("Choose an order on the planning board first.")
        st.stop()
    analysis, products, errors = analyze_order(
        order,
        pm,
        A_deck=A_deck,
        B_empty_cg=B_empty_cg,
        E_tare=E_tare,
        cg_limit_in=cg_limit_in,
        C_override_val=C_override_val,
    )
    st.markdown(
        f'<div class="lx-section-title">Load Plan - {order["orderId"]} {status_badge(order["recordStatus"])}</div>',
        unsafe_allow_html=True,
    )
    for err in errors:
        st.error(err)
    if not analysis or "matrix" not in order:
        st.info("Optimize the selected order before opening the load-plan image set.")
    else:
        render_meta_grid(
            [
                ("Vehicle", order["vehicleName"]),
                ("Reference", order["requestId"]),
                ("Payload", f"{analysis.payload_lbs:,.0f} lbs"),
                ("Securement", clean_securement_text(analysis.securement.strap_text)),
            ]
        )
        selected_views = st.multiselect(
            "Views",
            ["3D Top and Side", "3D View", "3D Left and Right", "2D View", "2D Side and Top", "2D Axle Load"],
            default=["3D Top and Side", "2D Side and Top", "2D Axle Load"],
        )
        render_rule_metrics(analysis)

        if analysis.honeycomb_required:
            st.warning(f'3" honeycomb dunnage required at spot(s): {", ".join(map(str, analysis.honeycomb_spots))}')
        if analysis.hatched_spots:
            st.info(f"Cord strap hatch marks at spot(s): {', '.join(map(str, analysis.hatched_spots))}")

        render_routeA_component(
            page_title="Top + Side View (Building Products)",
            created_by="LoadXpert Replica",
            created_at=order.get("optimizedAt", datetime.now().strftime("%Y-%m-%d %H:%M")),
            order_number=order["orderId"],
            vehicle_number=order["vehicleName"],
            po_number=order["batchId"],
            car_id=(order.get("umlerInitial", "") + order.get("umlerCarNumber", "")).strip() or "UNASSIGNED",
            matrix=order["matrix"],
            products=products,
            turn_spot=int(order.get("turn_spot", 7)),
            airbag_gap_choice=airbag_gap_choice,
            airbag_gap_in=float(airbag_gap_in),
            analysis=analysis,
            code_colors=code_colors,
            hatch_angle_deg=float(hatch_angle_deg),
            hatch_spacing_px=float(hatch_spacing_px),
            hatch_alpha=float(hatch_alpha),
            show_3d=bool(any(v.startswith("3D") for v in selected_views)),
            show_edges=bool(show_edges),
            cam_fov=float(cam_fov),
            cam_pos=(float(cam_x), float(cam_y), float(cam_z)),
            light_intensity=float(light_intensity),
            ambient_intensity=float(ambient_intensity),
            flip_side=bool(flip_side),
            height_px=1040,
        )

        cargo_rows = []
        for line in order.get("requestLines", []):
            product = products.get(str(line["product_id"]))
            cargo_rows.append(
                {
                    "Order": order["orderId"],
                    "Product": line["product_id"],
                    "Description": product.description if product else "",
                    "Tiers": int(line["tiers"]),
                    "Unit Weight": product.unit_weight_lbs if product else 0,
                    "Total Weight": (product.unit_weight_lbs * int(line["tiers"])) if product else 0,
                }
            )
        cargo_df = pd.DataFrame(cargo_rows)
        st.download_button(
            "Download Loaded Cargo CSV",
            cargo_df.to_csv(index=False).encode("utf-8"),
            file_name=f"{order['orderId']}_loaded_cargo.csv",
            mime="text/csv",
        )
        st.dataframe(cargo_df, width="stretch", hide_index=True)

else:
    st.markdown('<div class="lx-section-title">Dunnage Manager</div>', unsafe_allow_html=True)
    d1, d2, d3 = st.columns(3)
    with d1:
        filter_type = st.selectbox("Dunnage Type", ["All"] + sorted({d["dunnageType"] for d in st.session_state.bp_dunnage}))
    with d2:
        filter_category = st.selectbox("Category", ["All"] + sorted({d["category"] for d in st.session_state.bp_dunnage}))
    with d3:
        supplier = st.text_input("Supplier", value="")

    filtered_dunnage = []
    for item in st.session_state.bp_dunnage:
        if filter_type != "All" and item["dunnageType"] != filter_type:
            continue
        if filter_category != "All" and item["category"] != filter_category:
            continue
        if supplier and supplier.lower() not in item["supplier"].lower():
            continue
        filtered_dunnage.append(item)

    cols = st.columns(4)
    for idx, item in enumerate(filtered_dunnage):
        with cols[idx % 4]:
            st.markdown(
                f"""
                <div class="lx-panel">
                  <span class="lx-pill">{item['dunnageType']}</span>
                  <div style="font-size:32px; line-height:1.4;">AIR</div>
                  <b>{item['title']}</b><br>
                  <span class="lx-subtle">{item['supplier']} | {item['category']}</span>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown('<div class="lx-section-title">Annotations</div>', unsafe_allow_html=True)
    order = current_order()
    ann_a, ann_b, ann_c = st.columns([1.2, 1, 2])
    with ann_a:
        ann_category = st.selectbox("Category", ["general", "airbag", "issue", "load"])
    with ann_b:
        ann_view = st.selectbox("View", ["3d-split", "2d-axle-load", "2d-side-top"])
    with ann_c:
        ann_text = st.text_input("Description", value="")
    if st.button("Save Annotation", disabled=not ann_text):
        st.session_state.bp_annotations.append(
            {
                "order": order["orderId"] if order else "Unassigned",
                "category": ann_category,
                "view": ann_view,
                "text": ann_text,
                "createdAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
        )
        st.session_state.bp_last_message = "Annotation saved."
        st.rerun()

    if st.session_state.bp_annotations:
        st.dataframe(pd.DataFrame(st.session_state.bp_annotations), width="stretch", hide_index=True)
    else:
        st.info("No annotations saved yet.")

st.stop()


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
    created_by = st.text_input("Created By", value="-")
    created_at = st.text_input("Created At", value="-")
    order_number = st.text_input("Order Number", value="-")
    vehicle_number = st.text_input("Vehicle Number", value="-")
    po_number = st.text_input("PO Number", value="-")
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
    st.dataframe(pd.DataFrame(rows), width="stretch", height=240)
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
    st.metric("Securement", clean_securement_text(analysis.securement.strap_text))
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
