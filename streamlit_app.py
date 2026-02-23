# streamlit_app.py
# LoadXpert Route A — Presentation1 Rules Integrated
#
# Includes (from Presentation1.pdf):
# - % Blocked -> Strapping requirement (No / Double / 2-unit double / 4-unit double)
# - Diagonal hatch = cord strap required due to step-down
# - Honeycomb dunnage (3") required if void exists between tiers
# - CG_above_TOR calculation and PASS/WARN/FAIL gate
# - Optional "Grant Loading" doorway method (horizontal stacks at doorway)
# - Weight-balance metric + soft penalty in optimizer (basic)
# - Close-top / stagger soft penalty (basic)
#
# NOTE:
# - This implements the rules deterministically, but exact pixel-match to LoadXpert’s PDF
#   requires their embedded fonts + their proprietary A/B/C mapping + their exact “r-label”
#   scheme. This file focuses on correct rule-driven behavior + visuals aligned to those rules.

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


# =============================
# Page
# =============================
st.set_page_config(page_title="Load Diagram Optimizer — Route A (Rules Integrated)", layout="wide")
st.title("Load Diagram Optimizer — Route A (Presentation1 rules integrated)")

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

DOORFRAME_NO_ME = {6, 9}
DOORPOCKET_PINS = {7, 8}

AIRBAG_ALLOWED_GAPS = [(6, 7), (7, 8), (8, 9)]

# Visual constants
LX_BLUE = "#0b2a7a"
LX_RED = "#c00000"
LX_RED2 = "#d00000"
LX_GRID = "#000000"

# Close-to-PDF fills (tune via sidebar)
DEFAULT_CODE_COLORS = {
    "A": {"fill": "#79C7C7", "stroke": "#111111"},  # teal-ish
    "B": {"fill": "#F4F48A", "stroke": "#111111"},  # yellow-ish
    "C": {"fill": "#2FB34B", "stroke": "#111111"},  # green-ish
}

# Hatch defaults (tune via sidebar)
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
        # Basic inference: tune as needed (Presentation shows T&G handling)
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
    step_down_boundaries: List[Tuple[int, int]]  # boundary between spot a and b (adjacent)
    hatched_spots: List[int]  # spots to hatch (derived from step-down)
    honeycomb_required: bool
    honeycomb_spots: List[int]
    securement: SecurementDecision
    payload_lbs: float
    cg_above_tor_in: float
    cg_status: str
    weight_balance_ratio: float  # 0=perfect, 1=terrible (approx)


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
# Turn rules
# =============================
def blocked_spot_for_turn(turn_spot: int) -> int:
    return turn_spot + 1


def is_blocked_spot(spot: int, turn_spot: int) -> bool:
    return spot == blocked_spot_for_turn(turn_spot)


def occupied_spots_for_placement(spot: int, turn_spot: int) -> List[int]:
    # Turn consumes 2 spots at a tier
    if spot == turn_spot:
        return [spot, blocked_spot_for_turn(turn_spot)]
    return [spot]


# =============================
# Placement matrix
# =============================
def make_empty_matrix(max_tiers: int, turn_spot: int) -> List[List[Optional[str]]]:
    m = [[None for _ in range(max_tiers)] for _ in range(FLOOR_SPOTS)]
    b = blocked_spot_for_turn(turn_spot)
    for t in range(max_tiers):
        if 1 <= b <= FLOOR_SPOTS:
            m[b - 1][t] = BLOCK
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

    # Machine edge restriction in doorway frame spots
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
    turn_spot: int,
    close_top_weight: int,
    weight_balance_weight: int,
    tg_safety_weight: int,
) -> int:
    """
    Soft goals from Presentation:
    - Close top: avoid isolated peaks
    - Weight balance: keep left-right roughly even
    - T&G: prefer "safer" tiers (simple heuristic)
    """
    p = products[pid]
    penalty = 0

    # HP not on very top
    if p.is_half_pack and tier_idx == (max_tiers - 1):
        penalty += 120

    # T&G tier preference (simple):
    # prefer mid tiers vs bottom/top extremes
    if p.is_tg:
        # penalize lowest and highest tier
        if tier_idx in (0, max_tiers - 1):
            penalty += tg_safety_weight

    # Close-top: penalize creating a tower above neighbors
    # compare neighbor heights if placed here
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
        # if new height exceeds both neighbors by >=2, penalize
        if new_h - max(left, right) >= 2:
            penalty += close_top_weight

    # Weight balance: penalize adding weight far from center
    if weight_balance_weight > 0:
        # center between spots 7 and 8. distance based on spot index
        center = 7.5
        dist = abs(spot - center)
        penalty += int(weight_balance_weight * dist)

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
            turn_spot=turn_spot,
            close_top_weight=close_top_weight,
            weight_balance_weight=weight_balance_weight,
            tg_safety_weight=tg_safety_weight,
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
) -> Tuple[List[List[Optional[str]]], List[str]]:
    msgs: List[str] = []
    tokens = build_token_list(products, requests)
    matrix = make_empty_matrix(max_tiers, turn_spot)

    # Force TURN usage first
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
            )
            if pid is None:
                continue

            # PIN preference: if Machine Edge, try 7/8 at same tier
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
    """
    Presentation1 page 3 rule table:

    x > 90% blocked -> straps: No
    50% < x < 90%  -> straps: Yes (Double strapping)
    x < 50%        -> straps: Yes (2-unit double strapping)
    x < 10%        -> straps: Yes (4-unit double strapping)

    Interpreting boundaries deterministically:
    - if x > 90: No straps
    - elif x >= 50: Double strapping
    - elif x >= 10: 2-unit double strapping
    - else: 4-unit double strapping
    """
    x = float(percent_blocked)
    if x > 90.0:
        return SecurementDecision(
            percent_blocked=x,
            straps_required=False,
            strap_text="Straps: No",
            hatch_legend="(No cord strap required)",
        )
    if x >= 50.0:
        return SecurementDecision(
            percent_blocked=x,
            straps_required=True,
            strap_text="Straps: Yes — Double strapping",
            hatch_legend="Diagonal hatch = cord strap required (step-down)",
        )
    if x >= 10.0:
        return SecurementDecision(
            percent_blocked=x,
            straps_required=True,
            strap_text="Straps: Yes — 2-unit double strapping",
            hatch_legend="Diagonal hatch = cord strap required (step-down)",
        )
    return SecurementDecision(
        percent_blocked=x,
        straps_required=True,
        strap_text="Straps: Yes — 4-unit double strapping",
        hatch_legend="Diagonal hatch = cord strap required (step-down)",
    )


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
    # Make blocked spot height mirror the TURN spot to reflect the “span”
    if 1 <= blocked <= FLOOR_SPOTS:
        heights[blocked] = heights.get(turn_spot, 0)
    return heights


def detect_step_down_boundaries(heights: Dict[int, int], *, ignore_spots: Optional[set] = None) -> List[Tuple[int, int]]:
    """
    A step-down boundary exists where adjacent stack heights differ.
    """
    ignore_spots = ignore_spots or set()
    out: List[Tuple[int, int]] = []
    for s in range(1, FLOOR_SPOTS):
        a, b = s, s + 1
        if a in ignore_spots or b in ignore_spots:
            continue
        if heights.get(a, 0) != heights.get(b, 0):
            out.append((a, b))
    return out


def determine_hatched_spots_from_step_down(boundaries: List[Tuple[int, int]], heights: Dict[int, int]) -> List[int]:
    """
    Presentation1: diagonal hatch indicates cord strap required due to step-down.
    We hatch the *higher side* of each step-down boundary (more conservative).
    """
    hs = set()
    for a, b in boundaries:
        ha, hb = heights.get(a, 0), heights.get(b, 0)
        if ha == hb:
            continue
        hs.add(a if ha > hb else b)
    return sorted(hs)


def detect_honeycomb_voids(matrix: List[List[Optional[str]]], turn_spot: int) -> Tuple[bool, List[int]]:
    """
    Presentation1: use 3" honeycomb dunnage when there is a void in between tiers.
    Void definition: in a given spot, a None exists below a filled tier (i.e., gap within stack).
    """
    void_spots: List[int] = []
    blocked = blocked_spot_for_turn(turn_spot)
    for s in range(1, FLOOR_SPOTS + 1):
        if s == blocked:
            continue
        col = matrix[s - 1]
        seen_filled_above = False
        # scan from top to bottom
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
    Practical %blocked proxy (needs your official definition later):
    %blocked = occupied floor spots / available floor spots
    - available excludes the blocked spot (turn_spot+1)
    - occupied means spot has at least one tier loaded (ignoring BLOCK)
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
    if avail <= 0:
        return 0.0
    return 100.0 * (occ / avail)


def compute_payload_lbs(matrix: List[List[Optional[str]]], products: Dict[str, Product]) -> float:
    payload = 0.0
    for s in range(1, FLOOR_SPOTS + 1):
        for pid in matrix[s - 1]:
            if pid and pid != BLOCK and pid in products:
                payload += products[pid].unit_weight_lbs
    return payload


def compute_weight_balance_ratio(matrix: List[List[Optional[str]]], products: Dict[str, Product]) -> float:
    """
    Simple weight-balance metric: compare total weight left vs right of center (7.5).
    ratio = abs(L-R) / (L+R)
    """
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
    if total <= 0:
        return 0.0
    return abs(left - right) / total


def compute_cg_above_tor(
    *,
    A_deck_above_tor_minus_deflection: float,
    B_empty_car_cg_above_tor: float,
    E_tare_weight: float,
    F_load_weight: float,
    C_load_cg_above_deck: float,
) -> float:
    """
    Stored formula:
    CG_above_TOR = ((B*E)+((A+C)*F))/(E+F)
    """
    E = float(E_tare_weight)
    F = float(F_load_weight)
    if (E + F) <= 0:
        return 0.0
    A = float(A_deck_above_tor_minus_deflection)
    B = float(B_empty_car_cg_above_tor)
    C = float(C_load_cg_above_deck)
    return ((B * E) + ((A + C) * F)) / (E + F)


def estimate_load_cg_above_deck(matrix: List[List[Optional[str]]], products: Dict[str, Product]) -> float:
    """
    Estimate C (load CG above deck):
    - compute weighted average unit height across placed tiers, then
    - assume CG at half of the average stack height.

    This is a fallback; you can override via sidebar.
    """
    # avg unit height
    heights = []
    for s in range(1, FLOOR_SPOTS + 1):
        for pid in matrix[s - 1]:
            if pid and pid != BLOCK and pid in products:
                heights.append(products[pid].unit_height_in)
    if not heights:
        return 0.0
    avg_unit_h = float(sum(heights) / len(heights))

    # estimate average stack tiers among occupied spots
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
    A_deck_above_tor_minus_deflection: float,
    B_empty_car_cg_above_tor: float,
    E_tare_weight: float,
    cg_limit_in: float,
    override_C_load_cg_above_deck: Optional[float],
) -> AnalysisResult:
    heights = compute_spot_heights(matrix, turn_spot)
    boundaries = detect_step_down_boundaries(heights)
    hatched_spots = determine_hatched_spots_from_step_down(boundaries, heights)

    honeycomb_required, honeycomb_spots = detect_honeycomb_voids(matrix, turn_spot)

    pct = compute_percent_blocked(matrix, turn_spot)
    securement = decide_strapping(pct)

    payload = compute_payload_lbs(matrix, products)
    C_est = estimate_load_cg_above_deck(matrix, products)
    C = float(override_C_load_cg_above_deck) if override_C_load_cg_above_deck is not None else C_est
    cg = compute_cg_above_tor(
        A_deck_above_tor_minus_deflection=A_deck_above_tor_minus_deflection,
        B_empty_car_cg_above_tor=B_empty_car_cg_above_tor,
        E_tare_weight=E_tare_weight,
        F_load_weight=payload,
        C_load_cg_above_deck=C,
    )
    status = "PASS" if cg <= cg_limit_in else ("WARN" if cg <= (cg_limit_in * 1.03) else "FAIL")

    wb = compute_weight_balance_ratio(matrix, products)

    return AnalysisResult(
        heights_by_spot=heights,
        step_down_boundaries=boundaries,
        hatched_spots=hatched_spots if securement.straps_required else [],
        honeycomb_required=honeycomb_required,
        honeycomb_spots=honeycomb_spots,
        securement=securement,
        payload_lbs=payload,
        cg_above_tor_in=cg,
        cg_status=status,
        weight_balance_ratio=wb,
    )


# =============================
# A/B/C mapping (placeholder)
# =============================
def code_for_pid(pid: str, products: Dict[str, Product]) -> str:
    """
    Replace with your real LoadXpert SKU->A/B/C mapping.
    Temporary:
      - Half pack -> B
      - T&G -> C
      - else -> A
    """
    p = products.get(pid)
    if not p:
        return "A"
    if p.is_half_pack:
        return "B"
    if p.is_tg:
        return "C"
    return "A"


# =============================
# Rendering (Canvas 2D + Three.js 3D)
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
    grant_mode: bool,
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
) -> None:
    tiers = len(matrix[0]) if matrix else 4
    blocked = blocked_spot_for_turn(turn_spot)

    # Representative pid per spot (top view)
    rep: Dict[int, Optional[str]] = {}
    for s in range(1, FLOOR_SPOTS + 1):
        col = matrix[s - 1]
        rep[s] = next((v for v in col if v and v != BLOCK), None)

    # Cell list for side + 3D
    cells = []
    for s in range(1, FLOOR_SPOTS + 1):
        for t in range(tiers):
            pid = matrix[s - 1][t]
            if pid is None or pid == BLOCK:
                continue
            # avoid double-paint on blocked spot for turn-span (mirror)
            if s == blocked and matrix[turn_spot - 1][t] == pid:
                continue
            cells.append(
                {
                    "spot": s,
                    "tier": t,
                    "pid": pid,
                    "code": code_for_pid(pid, products),
                }
            )

    data = {
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
            "airbag_a": airbag_gap_choice[0],
            "airbag_b": airbag_gap_choice[1],
            "airbag_in": float(airbag_gap_in),
            "grant_mode": bool(grant_mode),
            "flip_side": bool(flip_side),
            "securement_text": analysis.securement.strap_text,
            "pct_blocked": float(analysis.securement.percent_blocked),
            "hatch_legend": analysis.securement.hatch_legend,
            "hatched_spots": analysis.hatched_spots,
            "honeycomb_required": bool(analysis.honeycomb_required),
            "honeycomb_spots": analysis.honeycomb_spots,
            "payload_lbs": float(analysis.payload_lbs),
            "cg_in": float(analysis.cg_above_tor_in),
            "cg_status": analysis.cg_status,
            "weight_balance_ratio": float(analysis.weight_balance_ratio),
        },
        "colors": code_colors,
        "hatch": {"angle": float(hatch_angle_deg), "spacing": float(hatch_spacing_px), "alpha": float(hatch_alpha)},
        "rep": rep,
        "cells": cells,
        "three": {
            "enabled": bool(show_3d),
            "show_edges": bool(show_edges),
            "cam_fov": float(cam_fov),
            "cam_pos": list(cam_pos),
            "light_intensity": float(light_intensity),
            "ambient_intensity": float(ambient_intensity),
        },
    }
    payload_json = json.dumps(data)

    HTML_TEMPLATE = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<style>
  body { margin:0; padding:0; background:#fff; font-family: Helvetica, Arial, sans-serif; }
  .wrap { display:flex; flex-direction:row; gap:18px; padding:14px; }
  canvas { background:#fff; border:1px solid #ddd; }
  #page { width: 1420px; height: 980px; }
  #three { width: 520px; height: 980px; }
  .hidden { display:none; }
</style>
</head>
<body>
<div class="wrap">
  <canvas id="page" width="1420" height="980"></canvas>
  <canvas id="three" width="520" height="980"></canvas>
</div>

<script>
const DATA = __PAYLOAD__;

const LX_BLUE = "#0b2a7a";
const LX_RED  = "#c00000";
const LX_RED2 = "#d00000";
const LX_GRID = "#000000";

function drawHatch(ctx, x, y, w, h, angleDeg, spacing, alpha, color="#000") {
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.beginPath();
  ctx.rect(x, y, w, h);
  ctx.clip();

  const angle = angleDeg * Math.PI / 180;
  const diag = Math.sqrt(w*w + h*h);
  const cx = x + w/2, cy = y + h/2;

  ctx.translate(cx, cy);
  ctx.rotate(angle);
  ctx.translate(-cx, -cy);

  ctx.strokeStyle = color;
  ctx.lineWidth = 2;

  const start = -diag;
  const end = diag*2;
  for (let i = start; i < end; i += spacing) {
    ctx.beginPath();
    ctx.moveTo(x + i, y - diag);
    ctx.lineTo(x + i, y + h + diag);
    ctx.stroke();
  }
  ctx.restore();
}

function fitRotated(ctx, text, maxW, maxH, minPx, maxPx, weight="700") {
  let fs = maxPx;
  while (fs >= minPx) {
    ctx.font = `${weight} ${fs}px Helvetica, Arial, sans-serif`;
    const w = ctx.measureText(text).width;
    if (w <= maxH * 0.92 && fs <= maxW * 0.92) return fs;
    fs -= 1;
  }
  return minPx;
}

function fitNormal(ctx, text, maxW, maxH, minPx, maxPx, weight="700") {
  let fs = maxPx;
  while (fs >= minPx) {
    ctx.font = `${weight} ${fs}px Helvetica, Arial, sans-serif`;
    const w = ctx.measureText(text).width;
    if (w <= maxW * 0.92 && fs <= maxH * 0.72) return fs;
    fs -= 1;
  }
  return minPx;
}

(function draw2D() {
  const canvas = document.getElementById("page");
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0,0,W,H);

  // Outer frame
  ctx.strokeStyle = LX_GRID;
  ctx.lineWidth = 2;
  ctx.strokeRect(10, 10, W-20, H-20);

  // Title
  ctx.fillStyle = "#111";
  ctx.font = "700 22px Helvetica, Arial, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(DATA.meta.page_title, W/2, 38);

  // Header table
  const hx=30, hy=60, hw=W-60, hh=86;
  ctx.strokeStyle = LX_GRID; ctx.lineWidth=2;
  ctx.strokeRect(hx,hy,hw,hh);
  const fr=[0.14,0.22,0.20,0.22,0.22];
  const xs=[hx];
  for (let i=0;i<fr.length-1;i++) xs.push(xs[xs.length-1] + hw*fr[i]);
  for (let i=1;i<xs.length;i++){ ctx.beginPath(); ctx.moveTo(xs[i],hy); ctx.lineTo(xs[i],hy+hh); ctx.stroke(); }
  const midY = hy + hh*0.55;
  ctx.beginPath(); ctx.moveTo(hx,midY); ctx.lineTo(hx+hw,midY); ctx.stroke();

  const headers=["Created By","Created At","Order Number","Vehicle Number","PO Number"];
  const vals=[DATA.meta.created_by, DATA.meta.created_at, DATA.meta.order_number, DATA.meta.vehicle_number, DATA.meta.po_number];
  ctx.textAlign="left";
  for (let i=0;i<5;i++){
    ctx.fillStyle="#111";
    ctx.font="700 14px Helvetica, Arial, sans-serif";
    ctx.fillText(headers[i], xs[i]+10, hy+24);
    ctx.font="400 16px Helvetica, Arial, sans-serif";
    ctx.fillText(vals[i], xs[i]+10, midY+28);
  }

  // Panels
  const topX = 360, topY = 182, topW = W - topX - 40, topH = 160;
  const sideX = topX, sideY = topY + topH + 55, sideW = topW, sideH = 260;

  // ---- TOP ----
  ctx.fillStyle="#111";
  ctx.font="700 16px Helvetica, Arial, sans-serif";
  ctx.textAlign="center";
  ctx.fillText("Top", topX + topW/2, topY - 10);

  ctx.strokeStyle=LX_BLUE; ctx.lineWidth=3;
  ctx.strokeRect(topX, topY, topW, topH);

  // Ruler
  const rulerY = topY - 14;
  ctx.strokeStyle = LX_BLUE;
  ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(topX, rulerY); ctx.lineTo(topX+topW, rulerY); ctx.stroke();
  ctx.lineWidth = 1;
  for (let i=0;i<=70;i++){
    const tx = topX + topW*i/70;
    const th = (i%5===0)?10:6;
    ctx.beginPath(); ctx.moveTo(tx, rulerY); ctx.lineTo(tx, rulerY+th); ctx.stroke();
  }

  const spots = DATA.meta.spots;
  const cellW = topW / spots;
  const gutterFrac = 0.10; // white gutters between blocks
  const padY = 12;
  const blockH = topH - padY*2;

  const order = [];
  for (let s=1;s<=spots;s++) order.push(s);
  if (DATA.meta.flip_side) order.reverse();

  // Doorway band
  const doorLeft = topX + (DATA.meta.door_start-1)*cellW;
  const doorRight = topX + (DATA.meta.door_end)*cellW;
  ctx.strokeStyle = LX_RED; ctx.lineWidth=3;
  ctx.strokeRect(doorLeft, topY, doorRight-doorLeft, topH);

  ctx.font="400 12px Helvetica, Arial, sans-serif";
  ctx.fillStyle = LX_RED;
  ctx.textAlign="left";
  ctx.fillText(`Doorway (Spots ${DATA.meta.door_start}-${DATA.meta.door_end})`, doorLeft+6, topY-4);

  // Airbag band
  const airX = topX + DATA.meta.airbag_a * cellW;
  ctx.fillStyle = LX_RED2;
  ctx.fillRect(airX-3, topY, 6, topH);

  // Turn hatch region
  const tz = topX + (DATA.meta.turn_spot-1)*cellW;
  drawHatch(ctx, tz, topY, cellW*2, topH, 45, 10, 0.10, "#000");
  ctx.strokeStyle="#111"; ctx.lineWidth=1;
  ctx.strokeRect(tz, topY, cellW*2, topH);
  ctx.fillStyle="#111";
  ctx.font="700 12px Helvetica, Arial, sans-serif";
  ctx.textAlign="center";
  ctx.fillText(`FORKLIFT TURN (${DATA.meta.turn_spot}-${DATA.meta.blocked_spot})`, tz + cellW, topY+16);

  // Draw top blocks
  for (let i=0;i<order.length;i++){
    const s = order[i];
    const pid = DATA.rep[String(s)] || null;
    const x = topX + i*cellW;
    const gx = x + cellW*gutterFrac*0.5;
    const gw = cellW*(1 - gutterFrac);

    let fill = "#fff";
    if (pid){
      const cell = DATA.cells.find(z => z.spot === s);
      const code = (cell && cell.code) ? cell.code : "A";
      fill = (DATA.colors[code] && DATA.colors[code].fill) ? DATA.colors[code].fill : "#fff";
    }

    ctx.fillStyle = fill;
    ctx.fillRect(gx, topY + padY, gw, blockH);
    ctx.strokeStyle="#111"; ctx.lineWidth=1;
    ctx.strokeRect(gx, topY + padY, gw, blockH);

    if (pid){
      // Grant mode: doorway stacks are horizontal labels (Presentation Grant method)
      const isDoor = (s >= DATA.meta.door_start && s <= DATA.meta.door_end);
      if (DATA.meta.grant_mode && isDoor){
        const fs = fitNormal(ctx, pid, gw-8, blockH-8, 10, 20, "700");
        ctx.font = `700 ${fs}px Helvetica, Arial, sans-serif`;
        ctx.fillStyle="#111";
        ctx.textAlign="center";
        ctx.textBaseline="middle";
        ctx.fillText(pid, gx+gw/2, topY+padY+blockH/2);
      } else {
        const fs = fitRotated(ctx, pid, gw, blockH, 10, 22, "700");
        ctx.save();
        ctx.translate(gx+gw/2, topY+padY+blockH/2);
        ctx.rotate(-Math.PI/2);
        ctx.font = `700 ${fs}px Helvetica, Arial, sans-serif`;
        ctx.fillStyle="#111";
        ctx.textAlign="center";
        ctx.textBaseline="middle";
        ctx.fillText(pid, 0, 0);
        ctx.restore();
      }
    }

    // Hatch only where step-down requires cord strap (Presentation1 meaning)
    if (DATA.meta.hatched_spots.includes(s)){
      drawHatch(ctx, gx, topY + padY, gw, blockH, DATA.hatch.angle, DATA.hatch.spacing, DATA.hatch.alpha, "#000");
    }
  }

  // ---- SIDE ----
  ctx.fillStyle="#111";
  ctx.font="700 16px Helvetica, Arial, sans-serif";
  ctx.textAlign="center";
  ctx.fillText(DATA.meta.flip_side ? "Side2" : "Side1", sideX + sideW/2, sideY - 10);

  ctx.strokeStyle=LX_BLUE; ctx.lineWidth=3;
  ctx.strokeRect(sideX, sideY, sideW, sideH);

  const sidePad = 14;
  const sx = sideX + sidePad;
  const sy = sideY + sidePad;
  const sw = sideW - 2*sidePad;
  const sh = sideH - 2*sidePad - 36; // room for numbers + wheels

  const cw = sw / spots;
  const ch = sh / DATA.meta.tiers;

  // Doorway band
  const sDoorLeft = sx + (DATA.meta.door_start-1)*cw;
  const sDoorRight = sx + (DATA.meta.door_end)*cw;
  ctx.strokeStyle = LX_RED; ctx.lineWidth=3;
  ctx.strokeRect(sDoorLeft, sy, sDoorRight-sDoorLeft, sh);

  // Airbag band
  const sAirX = sx + DATA.meta.airbag_a * cw;
  ctx.fillStyle = LX_RED2;
  ctx.fillRect(sAirX-3, sy, 6, sh);

  // Blank blocked spot (gap)
  const blankSpot = DATA.meta.blocked_spot;
  const blankIdx = order.indexOf(blankSpot);
  if (blankIdx >= 0){
    const bx = sx + blankIdx*cw;
    ctx.fillStyle="#fff";
    ctx.fillRect(bx, sy, cw, sh);
    ctx.strokeStyle = LX_RED; ctx.lineWidth=2;
    ctx.strokeRect(bx, sy, cw, sh);
  }

  // Grid lines + blocks
  for (let i=0;i<order.length;i++){
    const spot = order[i];
    const x = sx + i*cw;

    ctx.strokeStyle="rgba(17,17,17,0.55)";
    ctx.lineWidth=1;
    ctx.strokeRect(x, sy, cw, sh);

    if (spot === blankSpot) continue;

    for (let t=0;t<DATA.meta.tiers;t++){
      const y = sy + sh - (t+1)*ch;
      const cell = DATA.cells.find(z => z.spot === spot && z.tier === t);
      if (!cell) continue;

      const fill = (DATA.colors[cell.code] && DATA.colors[cell.code].fill) ? DATA.colors[cell.code].fill : "#fff";
      ctx.fillStyle=fill;
      ctx.fillRect(x+1, y+1, cw-2, ch-2);
      ctx.strokeStyle="#111";
      ctx.strokeRect(x+1, y+1, cw-2, ch-2);

      const fs = fitNormal(ctx, cell.pid, cw-6, ch-6, 8, 14, "700");
      ctx.font = `700 ${fs}px Helvetica, Arial, sans-serif`;
      ctx.fillStyle="#111";
      ctx.textAlign="center";
      ctx.textBaseline="middle";
      ctx.fillText(cell.pid, x+cw/2, y+ch/2);
    }

    // Hatch on entire column if that spot is flagged (cord strap for step-down)
    if (DATA.meta.hatched_spots.includes(spot)){
      drawHatch(ctx, x+1, sy+1, cw-2, sh-2, DATA.hatch.angle, DATA.hatch.spacing, DATA.hatch.alpha, "#000");
    }
  }

  // Spot numbers
  ctx.fillStyle="#111";
  ctx.font="400 12px Helvetica, Arial, sans-serif";
  ctx.textAlign="center";
  for (let i=0;i<order.length;i++){
    const x = sx + i*cw;
    ctx.fillText(String(order[i]), x+cw/2, sy+sh+22);
  }

  // Wheels
  const wheelY = sideY + sideH - 26;
  const wxs = [sideX+sideW*0.20, sideX+sideW*0.27, sideX+sideW*0.73, sideX+sideW*0.80];
  ctx.fillStyle="rgba(80,80,80,0.85)";
  for (const wx of wxs){
    ctx.beginPath();
    ctx.arc(wx, wheelY, 14, 0, Math.PI*2);
    ctx.fill();
  }

  // Footer metrics + rule text
  const fx=30, fy=sideY + sideH + 18, fw=W-60, fh=88;
  ctx.strokeStyle=LX_GRID; ctx.lineWidth=1.5;
  ctx.strokeRect(fx,fy,fw,fh);
  const cols=[0.25,0.25,0.25,0.25];
  const fxs=[fx];
  for (let i=0;i<cols.length-1;i++) fxs.push(fxs[fxs.length-1] + fw*cols[i]);
  for (let i=1;i<fxs.length;i++){ ctx.beginPath(); ctx.moveTo(fxs[i],fy); ctx.lineTo(fxs[i],fy+fh); ctx.stroke(); }

  ctx.fillStyle="#111";
  ctx.textAlign="left";
  ctx.font="700 12px Helvetica, Arial, sans-serif";
  ctx.fillText(`% Blocked = ${DATA.meta.pct_blocked.toFixed(1)}%`, fxs[0]+10, fy+22);
  ctx.fillText(DATA.meta.securement_text, fxs[0]+10, fy+42);

  ctx.fillText(`Payload (lbs) = ${DATA.meta.payload_lbs.toFixed(0)}`, fxs[1]+10, fy+22);
  ctx.fillText(`Weight balance ratio = ${(DATA.meta.weight_balance_ratio*100).toFixed(1)}%`, fxs[1]+10, fy+42);

  ctx.fillText(`CG above TOR (in) = ${DATA.meta.cg_in.toFixed(2)}`, fxs[2]+10, fy+22);
  ctx.fillText(`CG status = ${DATA.meta.cg_status}`, fxs[2]+10, fy+42);

  ctx.fillText(`Hatch legend:`, fxs[3]+10, fy+22);
  ctx.font="400 12px Helvetica, Arial, sans-serif";
  ctx.fillText(DATA.meta.hatch_legend, fxs[3]+10, fy+42);

  // Honeycomb callout
  if (DATA.meta.honeycomb_required){
    ctx.font="700 12px Helvetica, Arial, sans-serif";
    ctx.fillStyle="#111";
    ctx.fillText(`3" honeycomb dunnage required (void between tiers) at spots: ${DATA.meta.honeycomb_spots.join(", ")}`, 40, fy+72);
  }
})();
</script>

<script>
(function init3D() {
  const threeCanvas = document.getElementById("three");
  if (!DATA.three.enabled) {
    threeCanvas.classList.add("hidden");
    return;
  }
})();
</script>

<script src="https://unpkg.com/three@0.160.0/build/three.min.js"></script>
<script>
(function drawThree() {
  if (!DATA.three.enabled) return;

  const canvas = document.getElementById('three');
  const renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true });
  renderer.setSize(canvas.width, canvas.height, false);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xffffff);

  const camera = new THREE.PerspectiveCamera(DATA.three.cam_fov, canvas.width/canvas.height, 0.1, 2000);
  camera.position.set(DATA.three.cam_pos[0], DATA.three.cam_pos[1], DATA.three.cam_pos[2]);
  camera.lookAt(0, 0, 0);

  scene.add(new THREE.AmbientLight(0xffffff, DATA.three.ambient_intensity));
  const dir = new THREE.DirectionalLight(0xffffff, DATA.three.light_intensity);
  dir.position.set(8, 12, 10);
  scene.add(dir);

  const grid = new THREE.GridHelper(24, 24, 0xcccccc, 0xeeeeee);
  grid.position.y = -0.01;
  scene.add(grid);

  const spots = DATA.meta.spots;
  const tiers = DATA.meta.tiers;

  const spotW = 0.9;
  const spotD = 1.05;
  const tierH = 0.22;

  const x0 = -(spots * spotW) / 2 + spotW/2;

  const mats = new Map();
  function matForCode(code) {
    if (mats.has(code)) return mats.get(code);
    const fill = (DATA.colors[code] && DATA.colors[code].fill) ? DATA.colors[code].fill : "#ffffff";
    const m = new THREE.MeshLambertMaterial({ color: new THREE.Color(fill) });
    mats.set(code, m);
    return m;
  }

  const edgeMat = new THREE.LineBasicMaterial({ color: 0x111111 });
  const group = new THREE.Group();
  scene.add(group);

  for (const c of DATA.cells) {
    const spotIndex = (DATA.meta.flip_side) ? (spots - c.spot) : (c.spot - 1);
    const x = x0 + spotIndex * spotW;
    const y = (c.tier + 0.5) * tierH;
    const z = 0;

    // Gap column remains empty via missing cells
    const geo = new THREE.BoxGeometry(spotW*0.96, tierH*0.92, spotD*0.92);
    const mesh = new THREE.Mesh(geo, matForCode(c.code));
    mesh.position.set(x, y, z);
    group.add(mesh);

    if (DATA.three.show_edges) {
      const edges = new THREE.EdgesGeometry(geo);
      const line = new THREE.LineSegments(edges, edgeMat);
      line.position.copy(mesh.position);
      group.add(line);
    }
  }

  // Frame outline
  const frameGeo = new THREE.BoxGeometry(spots*spotW*1.02, tiers*tierH*1.02 + 0.1, spotD*1.05);
  const frameEdges = new THREE.EdgesGeometry(frameGeo);
  const frame = new THREE.LineSegments(frameEdges, new THREE.LineBasicMaterial({ color: 0x0b2a7a }));
  frame.position.set(0, (tiers*tierH)/2, 0);
  group.add(frame);

  function render() {
    renderer.render(scene, camera);
    requestAnimationFrame(render);
  }
  render();
})();
</script>

</body>
</html>
"""
    html = HTML_TEMPLATE.replace("__PAYLOAD__", payload_json)
    components.html(html, height=height_px, scrolling=True)


# =============================
# App start
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

    # Header fields
    page_title = st.text_input("Page title", value="Top + Side View (Route A)")
    created_by = st.text_input("Created By", value="—")
    created_at = st.text_input("Created At", value="—")
    order_number = st.text_input("Order Number", value="—")
    vehicle_number = st.text_input("Vehicle Number", value="—")
    po_number = st.text_input("PO Number", value="—")
    car_id = st.text_input("Car ID", value="TBOX632012")

    st.divider()

    # Layout controls
    max_tiers = st.slider("Max tiers per spot", 1, 10, 4)
    turn_spot = int(st.selectbox("Turn spot (must be 7 or 8)", ["7", "8"], index=0))
    required_turn_tiers = st.slider("Turn tiers required (HARD)", 0, int(max_tiers), int(max_tiers))

    # Airbag controls
    grant_mode = st.checkbox("Grant Loading Method (doorway horizontal stacks)", value=False)
    auto_airbag = st.checkbox('Auto airbag (prefer <= 9")', value=False)
    if auto_airbag:
        airbag_gap_choice, airbag_gap_in = (7, 8), 9.0
    else:
        gap_labels = [f"{a}-{b}" for a, b in AIRBAG_ALLOWED_GAPS]
        gap_choice_label = st.selectbox("Airbag location", gap_labels, index=1)
        airbag_gap_choice = AIRBAG_ALLOWED_GAPS[gap_labels.index(gap_choice_label)]
        airbag_gap_in = st.slider("Airbag space (in)", 6.0, 12.0, 9.0, 0.5)

    st.divider()

    # Optimization weights (soft goals)
    st.subheader("Soft goals (Presentation)")
    close_top_weight = st.slider("Close-top penalty", 0, 300, 120, 10)
    weight_balance_weight = st.slider("Weight balance penalty", 0, 40, 10, 1)
    tg_safety_weight = st.slider("T&G tier safety penalty", 0, 300, 140, 10)

    st.divider()

    # Colors A/B/C
    st.subheader("A/B/C Colors")
    colA = st.color_picker("A fill", DEFAULT_CODE_COLORS["A"]["fill"])
    colB = st.color_picker("B fill", DEFAULT_CODE_COLORS["B"]["fill"])
    colC = st.color_picker("C fill", DEFAULT_CODE_COLORS["C"]["fill"])
    code_colors = {
        "A": {"fill": colA, "stroke": "#111111"},
        "B": {"fill": colB, "stroke": "#111111"},
        "C": {"fill": colC, "stroke": "#111111"},
    }

    # Hatch settings (cord-strap / step-down hatch)
    st.subheader("Hatch (cord strap / step-down)")
    hatch_angle_deg = st.slider("Hatch angle (deg)", 0.0, 90.0, float(DEFAULT_HATCH["angle_deg"]), 1.0)
    hatch_spacing_px = st.slider("Hatch spacing (px)", 4.0, 20.0, float(DEFAULT_HATCH["spacing_px"]), 1.0)
    hatch_alpha = st.slider("Hatch opacity", 0.05, 0.6, float(DEFAULT_HATCH["alpha"]), 0.01)

    st.divider()

    # CG inputs (Presentation requires CG calc for high loading)
    st.subheader("CG_above_TOR inputs")
    A_deck = st.number_input("A: Deck height above TOR minus spring deflection (in)", min_value=0.0, value=48.0, step=0.1)
    B_empty_cg = st.number_input("B: Empty car CG above TOR (in)", min_value=0.0, value=56.0, step=0.1)
    E_tare = st.number_input("E: Tare weight (lbs)", min_value=1.0, value=75000.0, step=100.0)
    cg_limit_in = st.number_input("CG limit above TOR (in) (warning line)", min_value=1.0, value=98.0, step=0.5)
    override_C = st.checkbox("Override C (load CG above deck)", value=False)
    C_override_val = None
    if override_C:
        C_override_val = st.number_input("C: Load CG above deck (in)", min_value=0.0, value=30.0, step=0.5)

    st.divider()

    # 3D
    st.subheader("3D view")
    show_3d = st.checkbox("Show 3D panel", value=True)
    show_edges = st.checkbox("3D edges", value=True)
    cam_fov = st.slider("Camera FOV", 20.0, 75.0, 42.0, 1.0)
    cam_x = st.slider("Cam X", -40.0, 40.0, 10.0, 0.5)
    cam_y = st.slider("Cam Y", 0.0, 40.0, 10.0, 0.5)
    cam_z = st.slider("Cam Z", -60.0, 60.0, 18.0, 0.5)
    light_intensity = st.slider("Directional light", 0.2, 3.0, 1.2, 0.1)
    ambient_intensity = st.slider("Ambient light", 0.0, 2.0, 0.65, 0.05)

    st.divider()

    flip_side = st.checkbox("Side2 (flip)", value=False)

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

# stable sort
sort_cols, ascending = [], []
if COL_THICK in pm_cf.columns:
    sort_cols.append(COL_THICK)
    ascending.append(False)
sort_cols.append(COL_PRODUCT_ID)
ascending.append(True)

pm_cf = pm_cf.sort_values(by=sort_cols, ascending=ascending, na_position="last")
pm_cf = pm_cf.drop_duplicates(subset=[COL_PRODUCT_ID], keep="first").head(5000)

options = pm_cf.to_dict("records")


def option_label(r: dict) -> str:
    pid = str(r.get(COL_PRODUCT_ID, "")).strip()
    desc = str(r.get(COL_DESC, "")).strip()
    edge = str(r.get(COL_EDGE, "")).strip()
    wt = r.get(COL_UNIT_WT)
    thick = r.get(COL_THICK)
    w = r.get(COL_WIDTH)
    l = r.get(COL_LENGTH)

    if COL_HALF_PACK in pm_cf.columns:
        hp = " HP" if _truthy(r.get(COL_HALF_PACK, "")) else ""
    else:
        hp = " HP" if desc.upper().rstrip().endswith("HP") else ""

    parts = [f"{pid}{hp}"]
    dims = []
    if pd.notna(thick):
        dims.append(f'{float(thick):g}"')
    if pd.notna(w):
        dims.append(f"{float(w):g}")
    if pd.notna(l):
        dims.append(f"{float(l):g}")
    if dims:
        parts.append(" x ".join(dims))
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
    st.write("")
with c4:
    st.write("")

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
        rows.append(
            {
                "Sales Product Id": r.product_id,
                "Description": p.description if p else "",
                "Tiers": r.tiers,
                "T&G?": (p.is_tg if p else False),
            }
        )
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
        )
        st.session_state.matrix = matrix

for m in msgs:
    st.warning(m)

# Always compute analysis from current matrix (so user sees rule outputs even before rendering)
matrix = st.session_state.matrix
analysis = analyze_layout(
    matrix=matrix,
    products=products,
    turn_spot=int(turn_spot),
    A_deck_above_tor_minus_deflection=float(A_deck),
    B_empty_car_cg_above_tor=float(B_empty_cg),
    E_tare_weight=float(E_tare),
    cg_limit_in=float(cg_limit_in),
    override_C_load_cg_above_deck=(float(C_override_val) if C_override_val is not None else None),
)

st.subheader("Rule Outputs (Presentation1)")

cA, cB, cC, cD = st.columns(4)
with cA:
    st.metric("% Blocked (proxy)", f"{analysis.securement.percent_blocked:.1f}%")
with cB:
    st.metric("Securement", analysis.securement.strap_text)
with cC:
    st.metric("CG above TOR (in)", f"{analysis.cg_above_tor_in:.2f} ({analysis.cg_status})")
with cD:
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
        grant_mode=bool(grant_mode),
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
