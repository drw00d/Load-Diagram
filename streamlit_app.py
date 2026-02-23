# streamlit_app.py
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

DOOR_START_SPOT = 6
DOOR_END_SPOT = 9

DOORFRAME_NO_ME = {6, 9}    # Machine Edge not allowed here
DOORPOCKET_PINS = {7, 8}    # Machine Edge allowed/preferred here

AIRBAG_ALLOWED_GAPS = [(6, 7), (7, 8), (8, 9)]

# CG_above_TOR thresholds (configurable)
CG_THRESHOLDS_IN = {
    "boxcar": {"preferred_lt": 105.0, "caution_le": 115.0},
    "centerbeam": {"preferred_lt": 105.0, "caution_le": 115.0},
}


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
# Turn rules
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
        penalty += 100  # soft
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
    """
    HARD RULE: ensure at least `required_turn_tiers` placements occur in the TURN spot.
    Each such placement consumes BOTH turn_spot and turn_spot+1 at that tier.
    """
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

    force_turn_tiers(matrix, products, heavy, light, max_tiers, turn_spot, required_turn_tiers, msgs)

    # Boxcar fill order mimics your preference
    if floor_spots == FLOOR_SPOTS_BOXCAR:
        outside = [1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15]
        doorway = [7, 8, 6, 9]
        order = [s for s in outside + doorway if not is_blocked_spot(s, turn_spot)]
    else:
        # Centerbeam: fill from ends inward for symmetry
        order = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18]
        order = [s for s in order if not is_blocked_spot(s, turn_spot)]

    def tier_pref(t: int) -> str:
        return "heavy" if t % 2 == 0 else "light"

    while (heavy or light):
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

            # PIN preference: if Machine Edge, try 7/8 at same tier (boxcar only)
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
def color_for_pid(pid: str) -> str:
    palette = ["#d9ecff", "#ffe3d9", "#e6ffd9", "#f2e6ff", "#fff5cc", "#d9fff7", "#ffd9f1", "#e0e0ff"]
    h = 0
    for ch in pid:
        h = (h * 31 + ord(ch)) % 10_000
    return palette[h % len(palette)]


def components_svg(svg: str, height: int) -> None:
    components.html(f"<div style='width:100%;overflow:visible'>{svg}</div>", height=height, scrolling=False)


def auto_airbag_choice() -> Tuple[Tuple[int, int], float]:
    # Default to the most common/clean look
    return (7, 8), 6.0


# =============================
# TOP VIEW (boxcar style)
# =============================
def render_top_svg(
    *,
    car_id: str,
    matrix: List[List[Optional[str]]],
    products: Dict[str, Product],
    note: str,
    turn_spot: int,
    airbag_gap_choice: Tuple[int, int],
    airbag_gap_in: float,
    unit_length_ref_in: float,
    floor_spots: int,
) -> str:
    W, H = 1400, 360
    margin = 26
    header_h = 85

    x0, y0 = margin, margin + header_h
    w = W - 2 * margin
    lane_h = H - y0 - margin
    cell_w = w / floor_spots

    tiers = len(matrix[0]) if matrix else 0
    blocked = blocked_spot_for_turn(turn_spot)

    door_left = x0 + (DOOR_START_SPOT - 1) * cell_w
    door_right = x0 + DOOR_END_SPOT * cell_w

    a, b = airbag_gap_choice
    frac = (airbag_gap_in / unit_length_ref_in) if unit_length_ref_in > 0 else 0.06
    band_w = max(8.0, min(cell_w * 0.9, cell_w * frac))
    airbag_x_center = x0 + a * cell_w
    band_x = airbag_x_center - band_w / 2

    box_h = lane_h * 0.72
    box_y = y0 + (lane_h - box_h) / 2

    spot_rect: Dict[int, Tuple[float, float, float, float]] = {}
    for s in range(1, floor_spots + 1):
        x = x0 + (s - 1) * cell_w + cell_w * 0.08
        bw = cell_w * 0.84
        spot_rect[s] = (x, box_y, bw, box_h)

    turn_col = matrix[turn_spot - 1]
    turn_tiers = [(t, pid) for t, pid in enumerate(turn_col) if pid and pid != BLOCK]

    svg = []
    svg.append(f'<svg width="{W}" height="{H}" xmlns="http://www.w3.org/2000/svg">')
    svg.append("""
    <defs>
      <pattern id="doorHatch" patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="8" stroke="#c00000" stroke-width="2" opacity="0.22"/>
      </pattern>
      <pattern id="turnHatch" patternUnits="userSpaceOnUse" width="10" height="10" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="10" stroke="#000" stroke-width="2" opacity="0.10"/>
      </pattern>
    </defs>
    """)
    svg.append(f'<rect x="{margin}" y="{margin}" width="{W-2*margin}" height="{H-2*margin}" fill="white" stroke="black" stroke-width="2"/>')
    svg.append(f'<text x="{margin+10}" y="{margin+30}" font-size="18" font-weight="700">Car: {car_id} — Top View</text>')
    svg.append(f'<text x="{margin+10}" y="{margin+58}" font-size="13">{note}</text>')

    # Doorway (only meaningful for boxcar)
    if floor_spots == FLOOR_SPOTS_BOXCAR:
        svg.append(f'<rect x="{door_left}" y="{y0}" width="{door_right-door_left}" height="{lane_h}" fill="url(#doorHatch)" stroke="#c00000" stroke-width="3"/>')
        svg.append(f'<text x="{door_left+6}" y="{y0-8}" font-size="12" fill="#c00000">Doorway (Spots {DOOR_START_SPOT}-{DOOR_END_SPOT})</text>')

        svg.append(f'<rect x="{band_x}" y="{y0}" width="{band_w}" height="{lane_h}" fill="none" stroke="#d00000" stroke-width="5"/>')
        svg.append(f'<text x="{band_x+4}" y="{y0+lane_h+16}" font-size="12" fill="#d00000">Airbag {airbag_gap_in:.1f}" between {a}-{b}</text>')

        # Turn zone hatch (spans two spots)
        tzx = x0 + (turn_spot - 1) * cell_w
        svg.append(f'<rect x="{tzx}" y="{y0}" width="{cell_w*2}" height="{lane_h}" fill="url(#turnHatch)" stroke="#111" stroke-width="2"/>')
        svg.append(f'<text x="{tzx + cell_w}" y="{y0+16}" font-size="12" text-anchor="middle">FORKLIFT TURN ({turn_spot}-{blocked})</text>')

    for s in range(1, floor_spots + 1):
        x, y, bw, bh = spot_rect[s]
        col = matrix[s - 1]
        rep = next((pid for pid in col if pid and pid != BLOCK), None)
        fill = "#ffffff" if rep is None else color_for_pid(rep)
        if s == blocked:
            fill = "#ffffff"

        svg.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" fill="{fill}" opacity="0.35" stroke="#333" stroke-width="1"/>')
        svg.append(f'<text x="{x+6}" y="{y+16}" font-size="12" fill="#333">{s}</text>')

        if floor_spots == FLOOR_SPOTS_BOXCAR and s in DOORFRAME_NO_ME:
            svg.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" fill="none" stroke="#7a0000" stroke-width="3"/>')
            svg.append(f'<text x="{x+6}" y="{y+bh-8}" font-size="11" fill="#7a0000">NO Machine Edge</text>')

        if s == blocked:
            svg.append(f'<text x="{x+6}" y="{y+36}" font-size="11" fill="#555">BLOCKED</text>')

    # TURN tiers render (merged span) for boxcar only
    if floor_spots == FLOOR_SPOTS_BOXCAR and tiers > 0 and turn_tiers:
        x1, y1, bw1, bh1 = spot_rect[turn_spot]
        x2, y2, bw2, bh2 = spot_rect[blocked]
        tier_h = bh1 / tiers

        span_x = x1
        span_w = (x2 + bw2) - x1

        for t, pid in turn_tiers:
            y_bar = y1 + bh1 - (t + 1) * tier_h
            fill = color_for_pid(pid)
            svg.append(f'<rect x="{span_x}" y="{y_bar}" width="{span_w}" height="{tier_h}" fill="{fill}" opacity="0.92" stroke="#111" stroke-width="1"/>')
            svg.append(f'<text x="{span_x + 10}" y="{y_bar + tier_h/2 + 5}" font-size="12" font-weight="700" fill="#111">TURN</text>')
            hp = " HP" if (pid in products and products[pid].is_half_pack) else ""
            svg.append(f'<text x="{span_x + span_w/2}" y="{y_bar + tier_h/2 + 5}" font-size="12" text-anchor="middle">{pid}{hp}</text>')

        svg.append(f'<rect x="{span_x}" y="{y1}" width="{span_w}" height="{bh1}" fill="none" stroke="#111" stroke-width="2"/>')

    svg.append("</svg>")
    return "\n".join(svg)


# =============================
# SIDE VIEW
# =============================
def render_side_svg(
    *,
    car_id: str,
    matrix: List[List[Optional[str]]],
    products: Dict[str, Product],
    title: str,
    turn_spot: int,
    airbag_gap_choice: Tuple[int, int],
    airbag_gap_in: float,
    unit_length_ref_in: float,
    floor_spots: int,
) -> str:
    tiers = len(matrix[0]) if matrix else 0
    blocked = blocked_spot_for_turn(turn_spot)

    W = 1850
    H = 600
    margin = 24

    x0 = margin
    y0 = margin + 74
    car_w = W - 2 * margin
    car_h = H - y0 - margin - 12

    pad = 22
    load_x = x0 + pad
    load_y = y0 + pad
    load_w = car_w - 2 * pad
    load_h = car_h - 2 * pad

    cell_w = load_w / floor_spots
    cell_h = load_h / max(1, tiers)

    a, b = airbag_gap_choice
    frac = (airbag_gap_in / unit_length_ref_in) if unit_length_ref_in > 0 else 0.06
    band_w = max(8.0, min(cell_w * 0.9, cell_w * frac))
    airbag_x_center = load_x + a * cell_w
    airbag_x = airbag_x_center - band_w / 2

    door_left = load_x + (DOOR_START_SPOT - 1) * cell_w
    door_right = load_x + DOOR_END_SPOT * cell_w

    subtitle = f"Car: {car_id} • Spots: {floor_spots}"
    if floor_spots == FLOOR_SPOTS_BOXCAR:
        subtitle += f" • Doorway {DOOR_START_SPOT}-{DOOR_END_SPOT} • Airbag {a}-{b} @ {airbag_gap_in:.1f}\" • Turn {turn_spot}-{blocked}"

    svg = []
    svg.append(f'<svg width="100%" height="{H}" viewBox="0 0 {W} {H}" preserveAspectRatio="xMinYMin meet" xmlns="http://www.w3.org/2000/svg">')
    svg.append("""
    <defs>
      <pattern id="doorHatch2" patternUnits="userSpaceOnUse" width="10" height="10" patternTransform="rotate(45)">
        <line x1="0" y1="0" x2="0" y2="10" stroke="#c00000" stroke-width="2" opacity="0.22"/>
      </pattern>
    </defs>
    """)
    svg.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="white"/>')
    svg.append(f'<text x="{margin}" y="{margin+26}" font-size="20" font-weight="700">{title}</text>')
    svg.append(f'<text x="{margin}" y="{margin+50}" font-size="14" fill="#333">{subtitle}</text>')

    svg.append(f'<rect x="{x0}" y="{y0}" width="{car_w}" height="{car_h}" fill="none" stroke="#0b2a7a" stroke-width="4"/>')

    wheel_y = y0 + car_h - 12
    for cx in [x0 + car_w * 0.22, x0 + car_w * 0.28, x0 + car_w * 0.72, x0 + car_w * 0.78]:
        svg.append(f'<circle cx="{cx}" cy="{wheel_y}" r="10" fill="#666" opacity="0.5"/>')

    if floor_spots == FLOOR_SPOTS_BOXCAR:
        svg.append(f'<rect x="{door_left}" y="{load_y}" width="{door_right-door_left}" height="{load_h}" fill="url(#doorHatch2)" stroke="#c00000" stroke-width="3"/>')
        svg.append(f'<rect x="{airbag_x}" y="{load_y}" width="{band_w}" height="{load_h}" fill="none" stroke="#d00000" stroke-width="5"/>')

    for spot in range(1, floor_spots + 1):
        x = load_x + (spot - 1) * cell_w
        svg.append(f'<rect x="{x}" y="{load_y}" width="{cell_w}" height="{load_h}" fill="none" stroke="#333" stroke-width="1" opacity="0.55"/>')
        svg.append(f'<text x="{x + cell_w/2}" y="{load_y + load_h + 18}" font-size="12" text-anchor="middle" fill="#333">{spot}</text>')

        if floor_spots == FLOOR_SPOTS_BOXCAR and spot in DOORFRAME_NO_ME:
            svg.append(f'<rect x="{x+2}" y="{load_y+2}" width="{cell_w-4}" height="{load_h-4}" fill="none" stroke="#7a0000" stroke-width="4"/>')
            svg.append(f'<text x="{x + cell_w/2}" y="{load_y + 16}" font-size="11" text-anchor="middle" fill="#7a0000">NO ME</text>')

    # Regular cells
    for spot in range(1, floor_spots + 1):
        col = matrix[spot - 1]
        x = load_x + (spot - 1) * cell_w

        for t in range(tiers):
            pid = col[t]
            if pid is None or pid == BLOCK:
                continue

            # Prevent double-render from blocked spot for TURN spans
            if floor_spots == FLOOR_SPOTS_BOXCAR and spot == blocked:
                pid_turn = matrix[turn_spot - 1][t]
                if pid_turn == pid:
                    continue

            y = load_y + load_h - (t + 1) * cell_h
            fill = color_for_pid(pid)
            svg.append(f'<rect x="{x+1}" y="{y+1}" width="{cell_w-2}" height="{cell_h-2}" fill="{fill}" stroke="#1a1a1a" stroke-width="1" opacity="0.95"/>')
            hp = " HP" if (pid in products and products[pid].is_half_pack) else ""
            svg.append(f'<text x="{x + cell_w/2}" y="{y + cell_h/2 + 5}" font-size="13" text-anchor="middle" fill="#0a0a0a">{pid}{hp}</text>')

    # TURN tiers merged (boxcar only)
    if floor_spots == FLOOR_SPOTS_BOXCAR:
        turn_col = matrix[turn_spot - 1]
        for t in range(tiers):
            pid = turn_col[t]
            if pid is None or pid == BLOCK:
                continue

            x_turn = load_x + (turn_spot - 1) * cell_w
            y = load_y + load_h - (t + 1) * cell_h
            span_w = cell_w * 2

            fill = color_for_pid(pid)
            svg.append(f'<rect x="{x_turn+1}" y="{y+1}" width="{span_w-2}" height="{cell_h-2}" fill="{fill}" stroke="#111" stroke-width="2" opacity="0.98"/>')
            svg.append(f'<text x="{x_turn + 10}" y="{y + cell_h/2 + 5}" font-size="12" font-weight="700" fill="#111">TURN</text>')
            hp = " HP" if (pid in products and products[pid].is_half_pack) else ""
            svg.append(f'<text x="{x_turn + span_w/2}" y="{y + cell_h/2 + 5}" font-size="13" text-anchor="middle" fill="#0a0a0a">{pid}{hp}</text>')

    svg.append("</svg>")
    return "\n".join(svg)


# =============================
# Placement iteration (dedup for TURN span)
# =============================
def iter_unique_placements(
    matrix: List[List[Optional[str]]],
    *,
    turn_spot: int,
    floor_spots: int,
) -> Iterable[Tuple[int, int, str]]:
    """
    Yields unique placements as (spot, tier_idx, pid).
    Prevents double-counting TURN span (turn_spot + blocked_spot) for the same tier.
    """
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
# CG_above_TOR
# =============================
def compute_payload_and_stack_stats(
    matrix: List[List[Optional[str]]],
    products: Dict[str, Product],
    *,
    turn_spot: int,
    floor_spots: int,
) -> Tuple[float, int, float, float, Dict[int, Dict[str, float]]]:
    """
    Returns:
      payload_lbs,
      placed_tiers_count,
      avg_stack_height_in (weighted by spot payload),
      avg_stack_height_simple_in,
      per_spot_stats: {spot: {"stack_h":..., "spot_wt":..., "tiers":...}}
    """
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

    # Weighted avg stack height (weight by spot payload)
    num = 0.0
    den = 0.0
    for s in range(1, floor_spots + 1):
        sh = per_spot[s]["stack_h"]
        sw = per_spot[s]["spot_wt"]
        if sh > 0 and sw > 0:
            num += sh * sw
            den += sw
    avg_stack_weighted = (num / den) if den > 0 else 0.0

    # Simple avg across non-empty spots (fallback/reference)
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
    """
    CG_above_TOR = ((B*E) + ((A+C)*F)) / (E+F)
      A = deck height above TOR minus spring deflection
      B = empty car CG above TOR
      C = load CG above deck/base
      E = tare weight
      F = load weight
    Returns (cg_above_tor_in, trace_dict)
    """
    # Validate inputs
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
    """
    Returns (tier_label, emoji_chip)
    """
    if cg_in is None:
        return "N/A", "⚪ N/A"
    th = CG_THRESHOLDS_IN.get(car_type_key, CG_THRESHOLDS_IN["boxcar"])
    preferred_lt = th["preferred_lt"]
    caution_le = th["caution_le"]
    if cg_in < preferred_lt:
        return "Preferred", "🟢 Preferred"
    if cg_in <= caution_le:
        return "Caution", "🟡 Caution"
    return "High Risk", "🔴 High Risk"


# =============================
# Validation panel
# =============================
def validate_airbag(airbag_gap_in: float) -> Tuple[str, str]:
    """
    Returns (status, message), where status in {"PASS","WARN","FAIL"}
    """
    if airbag_gap_in < 6.0:
        return "FAIL", f'Airbag Space {airbag_gap_in:.1f}" is below 6.0" (too tight).'
    if airbag_gap_in <= 9.0:
        return "PASS", f'Airbag Space {airbag_gap_in:.1f}" is within preferred 6.0–9.0".'
    if airbag_gap_in <= 10.0:
        return "WARN", f'Airbag Space {airbag_gap_in:.1f}" is above preferred (6.0–9.0") but ≤ 10.0".'
    return "FAIL", f'Airbag Space {airbag_gap_in:.1f}" exceeds 10.0" (not acceptable).'


def validate_centerbeam_symmetry(per_spot_stats: Dict[int, Dict[str, float]]) -> Tuple[str, str]:
    """
    Simple symmetry check: compare total weight left half vs right half.
    (You can refine to true left/right side-by-side later.)
    """
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
    st.session_state.matrix = make_empty_matrix(4, 7, FLOOR_SPOTS_BOXCAR)

# --- Sidebar: formal UI structure
with st.sidebar:
    st.header("Settings")

    # Context
    order_number = st.text_input("Order Number", value="")
    po_number = st.text_input("PO Number", value="")
    commodity_type = st.selectbox("Commodity Type", ["Plywood", "OSB", "Lumber"], index=0)

    # Car selection
    car_type = st.selectbox("Car Type", ["Boxcar", "Centerbeam"], index=0)
    car_type_key = "boxcar" if car_type.lower().startswith("box") else "centerbeam"
    floor_spots = FLOOR_SPOTS_BOXCAR if car_type_key == "boxcar" else FLOOR_SPOTS_CENTERBEAM

    car_spec_source = st.selectbox("Car Spec Source", ["Manual Override", "UMLER"], index=0)
    car_id = st.text_input("Vehicle Number / Car ID", value="TBOX632012")

    st.divider()

    # Layout controls
    max_tiers = st.slider("Max tiers per spot", 1, 8, 4)

    if car_type_key == "boxcar":
        turn_spot = int(st.selectbox("Turn spot (must be 7 or 8)", ["7", "8"], index=0))
        required_turn_tiers = st.slider("Turn tiers required (HARD)", 1, 8, int(max_tiers))
        required_turn_tiers = min(required_turn_tiers, int(max_tiers))

        unit_length_ref_in = st.number_input("Unit length ref (in) for gap drawing", min_value=1.0, value=96.0, step=1.0)

        auto_airbag = st.checkbox('Auto airbag (prefer <= 9")', value=True)
        if auto_airbag:
            airbag_gap_choice, airbag_gap_in = auto_airbag_choice()
        else:
            gap_labels = [f"{a}-{b}" for a, b in AIRBAG_ALLOWED_GAPS]
            gap_choice_label = st.selectbox("Airbag location", gap_labels, index=1)
            airbag_gap_choice = AIRBAG_ALLOWED_GAPS[gap_labels.index(gap_choice_label)]
            airbag_gap_in = st.slider("Airbag gap (in)", 6.0, 10.0, 6.0, 0.5)
    else:
        # Centerbeam: no doorway/airbag/turn in this UI version
        turn_spot = 7  # placeholder (unused)
        required_turn_tiers = 0
        unit_length_ref_in = 96.0
        airbag_gap_choice = (7, 8)
        airbag_gap_in = 0.0

    view_mode = st.radio("View", ["Top + Side", "Top only", "Side only"], index=0)

    st.divider()

    # Engineering Inputs expander (formal)
    with st.expander("Engineering Inputs (CG + Axle)", expanded=False):
        st.caption("CG_above_TOR requires deck height, empty car CG, tare weight, spring deflection, and payload.")
        # NOTE: UMLER integration is a placeholder here; keep Manual Override as source-of-truth until you wire UMLER.
        if car_spec_source == "UMLER":
            st.info("UMLER source selected. Wire lookup here (read-only fields) or switch to Manual Override for now.")

        deck_height_TOR_in = st.number_input("Deck height above TOR (in)", min_value=0.0, value=60.0, step=0.25)
        empty_car_CG_TOR_in = st.number_input("Empty car CG above TOR (in)", min_value=0.0, value=62.0, step=0.25)
        tare_weight_lbs = st.number_input("Tare weight (lb)", min_value=0.0, value=82000.0, step=100.0)
        spring_deflection_in = st.number_input("Spring deflection (in)", min_value=0.0, value=0.0, step=0.05)

        payload_override_on = st.checkbox("Override payload weight (lb)", value=False)
        payload_override_lbs = st.number_input("Payload override (lb)", min_value=0.0, value=0.0, step=100.0, disabled=(not payload_override_on))

    st.divider()

    # Run control
    optimize_btn = st.button("Generate Diagram", type="primary")
    clear_btn = st.button("Clear All")


st.success(f"Product Master loaded: {len(pm):,} rows")

# --- Commodity / Facility filters (data-driven)
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
    sort_cols.append(COL_THICK)
    ascending.append(False)
if COL_WIDTH in pm_cf.columns:
    sort_cols.append(COL_WIDTH)
    ascending.append(False)
if COL_LENGTH in pm_cf.columns:
    sort_cols.append(COL_LENGTH)
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
    thick = r.get(COL_THICK)
    width = r.get(COL_WIDTH)
    length = r.get(COL_LENGTH)
    pcs = r.get(COL_PIECECOUNT)
    wt = r.get(COL_UNIT_WT)

    if COL_HALF_PACK in pm_cf.columns:
        hp = " HP" if _truthy(r.get(COL_HALF_PACK, "")) else ""
    else:
        hp = " HP" if desc.upper().rstrip().endswith("HP") else ""

    parts = [f"{pid}{hp}"]
    dims = []
    if pd.notna(thick):
        dims.append(f'{float(thick):g}"')
    if pd.notna(width):
        dims.append(f"{float(width):g}")
    if pd.notna(length):
        dims.append(f"{float(length):g}")
    if dims:
        parts.append(" x ".join(dims))
    if pd.notna(pcs):
        parts.append(f"{int(pcs)} pcs")
    if pd.notna(wt):
        parts.append(f"{float(wt):,.0f} lbs")
    if edge:
        parts.append(edge)
    if desc:
        parts.append(desc)
    return " | ".join(parts)


labels = [option_label(r) for r in options]
selected_label = st.selectbox("Pick a Product", labels) if labels else None

c1, c2, c3 = st.columns([2, 1, 1], vertical_alignment="bottom")
with c1:
    tiers_to_add = st.number_input("Tiers to add", min_value=1, value=4, step=1)
with c2:
    add_line = st.button("Add Line", disabled=(selected_label is None))
with c3:
    # keep a local optimize button too (mirrors sidebar)
    optimize_btn2 = st.button("Optimize Layout")

if clear_btn:
    st.session_state.requests = []
    st.session_state.matrix = make_empty_matrix(int(max_tiers), turn_spot, floor_spots)

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

# --- Optimize
msgs: List[str] = []
if optimize_btn or optimize_btn2:
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

# --- Compute payload + stack stats (dedup TURN span)
payload_calc_lbs, placed, avg_stack_weighted_in, avg_stack_simple_in, per_spot_stats = compute_payload_and_stack_stats(
    matrix, products, turn_spot=int(turn_spot), floor_spots=int(floor_spots)
)

payload_used_lbs = float(payload_override_lbs) if payload_override_on else float(payload_calc_lbs)

# --- Compute CG_above_TOR
cg_in, cg_trace = cg_above_tor(
    deck_height_TOR_in=float(deck_height_TOR_in),
    empty_car_CG_TOR_in=float(empty_car_CG_TOR_in),
    tare_weight_lbs=float(tare_weight_lbs),
    spring_deflection_in=float(spring_deflection_in),
    payload_weight_lbs=float(payload_used_lbs),
    avg_stack_height_in=float(avg_stack_weighted_in),
)
tier_label, tier_chip = cg_tier(cg_in, car_type_key=car_type_key)

# Legacy-ish "C.G. height (in)" for display row:
# We'll show Load CG above TOR = (A + C). (This is NOT the combined car+load CG_above_TOR.)
cg_height_in = None
if cg_trace:
    cg_height_in = cg_trace.get("A_plus_C")

# =============================
# Summary + Engineering Row (formal)
# =============================
st.subheader("Engineering Summary")

colA, colB, colC = st.columns(3)
with colA:
    st.metric("Payload (lb)", f"{payload_used_lbs:,.0f}" + (" (override)" if payload_override_on else ""))
with colB:
    st.metric("Placed tiers", f"{placed:,}")
with colC:
    st.metric("Avg stack height (in)", f"{avg_stack_weighted_in:,.2f}")

# Engineering row (Load Xpert style)
eng = st.container()
with eng:
    c1, c2, c3, c4, c5, c6, c7 = st.columns([1.2, 1.6, 1.8, 1.4, 1.6, 1.6, 1.2])
    with c1:
        st.write("**Floor spots**")
        st.write(f"{floor_spots}")
    with c2:
        st.write("**C.G. height (in)**")
        st.write("N/A" if cg_height_in is None else f"{cg_height_in:.2f} in")
    with c3:
        st.write("**CG_above_TOR (in)**")
        st.write("N/A" if cg_in is None else f"{cg_in:.2f} in")
        st.caption(tier_chip)
    with c4:
        st.write("**Airbag Space (in)**")
        if car_type_key == "boxcar":
            st.write(f"{float(airbag_gap_in):.2f} in")
        else:
            st.write("N/A")
    with c5:
        st.write("**Whole Unit Eq.**")
        # Placeholder until you wire WUE logic
        st.write("—")
    with c6:
        st.write("**Total LISA Units**")
        # Placeholder until you wire LISA unit logic
        st.write("—")
    with c7:
        st.write("**Car Type**")
        st.write(car_type)

# =============================
# Validation / Warnings panel (formal)
# =============================
st.subheader("Validations")

vrows = []

# Airbag checks (boxcar only)
if car_type_key == "boxcar":
    status, msg = validate_airbag(float(airbag_gap_in))
    vrows.append((status, msg))

# CG tier check
if cg_in is None:
    vrows.append(("FAIL", "CG_above_TOR is N/A (missing/invalid engineering inputs or payload)."))
else:
    if tier_label == "Preferred":
        vrows.append(("PASS", f"CG_above_TOR {cg_in:.2f} in is in Preferred range."))
    elif tier_label == "Caution":
        vrows.append(("WARN", f"CG_above_TOR {cg_in:.2f} in is in Caution range (manual awareness)."))
    else:
        vrows.append(("FAIL", f"CG_above_TOR {cg_in:.2f} in is High Risk — manual approval required."))

# Centerbeam symmetry
if car_type_key == "centerbeam":
    s_status, s_msg = validate_centerbeam_symmetry(per_spot_stats)
    vrows.append((s_status, s_msg))

# Render panel
for status, msg in vrows:
    if status == "PASS":
        st.success(msg)
    elif status == "WARN":
        st.warning(msg)
    else:
        st.error(msg)

# Manual approval banner
if tier_label == "High Risk":
    st.error("Manual Approval Required: CG_above_TOR exceeds High Risk threshold.")

# Calculation trace (formal, hidden by default)
with st.expander("Calculation Trace (CG)", expanded=False):
    if not cg_trace:
        st.info("No CG trace available (missing required inputs or payload).")
    else:
        st.write("**Inputs / intermediates**")
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
        st.write("**Formula**")
        st.latex(r"CG_{above\_TOR} = \frac{(B \cdot E) + ((A + C)\cdot F)}{E + F}")

# =============================
# Diagram view
# =============================
blocked = blocked_spot_for_turn(int(turn_spot))
note = (
    f"Order: {order_number or '—'} | PO: {po_number or '—'} | Commodity: {commodity_type} | "
    f"Product Type: {commodity_selected} | Facility: {facility_selected} | "
    f"Spots: {floor_spots}"
)

if car_type_key == "boxcar":
    note += (
        f" | Doorway: {DOOR_START_SPOT}-{DOOR_END_SPOT} | "
        f'Airbag: {airbag_gap_choice[0]}-{airbag_gap_choice[1]} @ {float(airbag_gap_in):.1f}" | '
        f"Turn spans {turn_spot}-{blocked} (consumes 2 spots) | Turn tiers required: {required_turn_tiers}"
    )

top_svg = render_top_svg(
    car_id=car_id,
    matrix=matrix,
    products=products,
    note=note,
    turn_spot=int(turn_spot),
    airbag_gap_choice=airbag_gap_choice,
    airbag_gap_in=float(airbag_gap_in),
    unit_length_ref_in=float(unit_length_ref_in),
    floor_spots=int(floor_spots),
)

side_svg = render_side_svg(
    car_id=car_id,
    matrix=matrix,
    products=products,
    title="Side (Load Xpert style)",
    turn_spot=int(turn_spot),
    airbag_gap_choice=airbag_gap_choice,
    airbag_gap_in=float(airbag_gap_in),
    unit_length_ref_in=float(unit_length_ref_in),
    floor_spots=int(floor_spots),
)

st.subheader("Diagram View")
if view_mode == "Top only":
    components_svg(top_svg, height=390)
elif view_mode == "Side only":
    components_svg(side_svg, height=620)
else:
    components_svg(top_svg, height=390)
    st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
    components_svg(side_svg, height=620)
