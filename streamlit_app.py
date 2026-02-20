# streamlit_app.py
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

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
FLOOR_SPOTS = 15
DOOR_START_SPOT = 6
DOOR_END_SPOT = 9

DOORFRAME_NO_ME = {6, 9}   # Machine Edge not allowed here
DOORPOCKET_PINS = {7, 8}  # Machine Edge allowed/preferred here

AIRBAG_ALLOWED_GAPS = [(6, 7), (7, 8), (8, 9)]


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

    # If Short Descrip isn't present, fall back to Descrip
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
    # Turn consumes 2 spots: [turn_spot, turn_spot+1]
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

    # Machine Edge ban in doorframe 6/9, including TURN placements that touch them
    if p.is_machine_edge and any(s in DOORFRAME_NO_ME for s in occ):
        return False, "Machine Edge not allowed in doorframe spots 6/9 (or any placement that touches them)."

    return True, ""


def soft_penalty(products: Dict[str, Product], pid: str, tier_idx: int, max_tiers: int) -> int:
    # Half-pack on TOP is soft penalty
    p = products[pid]
    penalty = 0
    if p.is_half_pack and tier_idx == (max_tiers - 1):
        penalty += 100
    return penalty


# =============================
# Optimization (simple but stable)
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


def pop_best(tokens: List[str], products: Dict[str, Product], spot: int, tier_idx: int, max_tiers: int, turn_spot: int) -> Optional[str]:
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


def optimize_layout(products: Dict[str, Product], requests: List[RequestLine], max_tiers: int, turn_spot: int) -> Tuple[List[List[Optional[str]]], List[str]]:
    msgs: List[str] = []
    heavy, light = build_token_lists(products, requests)
    matrix = make_empty_matrix(max_tiers, turn_spot)

    # Fill order: outside doorway, then 7/8, then 6/9
    outside = [1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15]
    doorway = [7, 8, 6, 9]
    order = [s for s in outside + doorway if not is_blocked_spot(s, turn_spot)]

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
                pid = pop_best(heavy, products, spot, t, max_tiers, turn_spot) or pop_best(light, products, spot, t, max_tiers, turn_spot)
            else:
                pid = pop_best(light, products, spot, t, max_tiers, turn_spot) or pop_best(heavy, products, spot, t, max_tiers, turn_spot)

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
    return (7, 8), 6.0


# =============================
# TOP VIEW
# - Normal spots: one tall "column" block (just visual)
# - TURN spot (7 or 8): each tier draws ONE wide horizontal block spanning spots 7-8
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
) -> str:
    W, H = 1400, 360
    margin = 26
    header_h = 85

    x0, y0 = margin, margin + header_h
    w = W - 2 * margin
    lane_h = H - y0 - margin
    cell_w = w / FLOOR_SPOTS

    tiers = len(matrix[0]) if matrix else 0
    blocked = blocked_spot_for_turn(turn_spot)

    door_left = x0 + (DOOR_START_SPOT - 1) * cell_w
    door_right = x0 + DOOR_END_SPOT * cell_w

    a, b = airbag_gap_choice
    frac = (airbag_gap_in / unit_length_ref_in) if unit_length_ref_in > 0 else 0.06
    band_w = max(8.0, min(cell_w * 0.9, cell_w * frac))
    airbag_x_center = x0 + a * cell_w
    band_x = airbag_x_center - band_w / 2

    # Base spot box
    box_h = lane_h * 0.72
    box_y = y0 + (lane_h - box_h) / 2

    spot_rect: Dict[int, Tuple[float, float, float, float]] = {}
    for s in range(1, FLOOR_SPOTS + 1):
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

    # doorway hatch band
    svg.append(f'<rect x="{door_left}" y="{y0}" width="{door_right-door_left}" height="{lane_h}" fill="url(#doorHatch)" stroke="#c00000" stroke-width="3"/>')
    svg.append(f'<text x="{door_left+6}" y="{y0-8}" font-size="12" fill="#c00000">Doorway (Spots {DOOR_START_SPOT}-{DOOR_END_SPOT})</text>')

    # airbag band
    svg.append(f'<rect x="{band_x}" y="{y0}" width="{band_w}" height="{lane_h}" fill="none" stroke="#d00000" stroke-width="5"/>')
    svg.append(f'<text x="{band_x+4}" y="{y0+lane_h+16}" font-size="12" fill="#d00000">Airbag {airbag_gap_in:.1f}" between {a}-{b}</text>')

    # turn zone hatch
    tzx = x0 + (turn_spot - 1) * cell_w
    svg.append(f'<rect x="{tzx}" y="{y0}" width="{cell_w*2}" height="{lane_h}" fill="url(#turnHatch)" stroke="#111" stroke-width="2"/>')
    svg.append(f'<text x="{tzx + cell_w}" y="{y0+16}" font-size="12" text-anchor="middle">FORKLIFT TURN ({turn_spot}-{blocked})</text>')

    # draw base spot boxes (skip blocked label fill)
    for s in range(1, FLOOR_SPOTS + 1):
        x, y, bw, bh = spot_rect[s]
        col = matrix[s - 1]
        rep = next((pid for pid in col if pid and pid != BLOCK), None)
        fill = "#ffffff" if rep is None else color_for_pid(rep)
        if s == blocked:
            fill = "#ffffff"

        svg.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" fill="{fill}" opacity="0.35" stroke="#333" stroke-width="1"/>')
        svg.append(f'<text x="{x+6}" y="{y+16}" font-size="12" fill="#333">{s}</text>')

        if s in DOORFRAME_NO_ME:
            svg.append(f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" fill="none" stroke="#7a0000" stroke-width="3"/>')
            svg.append(f'<text x="{x+6}" y="{y+bh-8}" font-size="11" fill="#7a0000">NO Machine Edge</text>')

        if s == blocked:
            svg.append(f'<text x="{x+6}" y="{y+36}" font-size="11" fill="#555">BLOCKED</text>')

    # TURN tiers render: ONE wide "horizontal" block per tier spanning both spots
    if tiers > 0 and turn_tiers:
        x1, y1, bw1, bh1 = spot_rect[turn_spot]
        x2, y2, bw2, bh2 = spot_rect[blocked]
        tier_h = bh1 / tiers

        span_x = x1
        span_w = (x2 + bw2) - x1

        for t, pid in turn_tiers:
            y_bar = y1 + bh1 - (t + 1) * tier_h

            fill = color_for_pid(pid)
            svg.append(f'<rect x="{span_x}" y="{y_bar}" width="{span_w}" height="{tier_h}" fill="{fill}" opacity="0.92" stroke="#111" stroke-width="1"/>')
            # Add "TURN" label to make it obvious this row is rotated
            svg.append(f'<text x="{span_x + 10}" y="{y_bar + tier_h/2 + 5}" font-size="12" font-weight="700" fill="#111">TURN</text>')

            hp = " HP" if (pid in products and products[pid].is_half_pack) else ""
            svg.append(f'<text x="{span_x + span_w/2}" y="{y_bar + tier_h/2 + 5}" font-size="12" text-anchor="middle">{pid}{hp}</text>')

        svg.append(f'<rect x="{span_x}" y="{y1}" width="{span_w}" height="{bh1}" fill="none" stroke="#111" stroke-width="2"/>')

    svg.append("</svg>")
    return "\n".join(svg)


# =============================
# SIDE VIEW (Load Xpert style)
# - Draw grid 1..15 and tiers
# - TURN tiers render as ONE merged wide rectangle across turn_spot and blocked spot
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

    cell_w = load_w / FLOOR_SPOTS
    cell_h = load_h / max(1, tiers)

    a, b = airbag_gap_choice
    frac = (airbag_gap_in / unit_length_ref_in) if unit_length_ref_in > 0 else 0.06
    band_w = max(8.0, min(cell_w * 0.9, cell_w * frac))
    airbag_x_center = load_x + a * cell_w
    airbag_x = airbag_x_center - band_w / 2

    door_left = load_x + (DOOR_START_SPOT - 1) * cell_w
    door_right = load_x + DOOR_END_SPOT * cell_w

    subtitle = f"Car: {car_id} • Doorway {DOOR_START_SPOT}-{DOOR_END_SPOT} • Airbag {a}-{b} @ {airbag_gap_in:.1f}\" • Turn {turn_spot}-{blocked}"

    svg = []
    svg.append(
        f'<svg width="100%" height="{H}" viewBox="0 0 {W} {H}" preserveAspectRatio="xMinYMin meet" xmlns="http://www.w3.org/2000/svg">'
    )
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

    # wheels
    wheel_y = y0 + car_h - 12
    for cx in [x0+car_w*0.22, x0+car_w*0.28, x0+car_w*0.72, x0+car_w*0.78]:
        svg.append(f'<circle cx="{cx}" cy="{wheel_y}" r="10" fill="#666" opacity="0.5"/>')

    # doorway + airbag
    svg.append(f'<rect x="{door_left}" y="{load_y}" width="{door_right-door_left}" height="{load_h}" fill="url(#doorHatch2)" stroke="#c00000" stroke-width="3"/>')
    svg.append(f'<rect x="{airbag_x}" y="{load_y}" width="{band_w}" height="{load_h}" fill="none" stroke="#d00000" stroke-width="5"/>')

    # spot vertical grid and bottom numbers
    for spot in range(1, FLOOR_SPOTS + 1):
        x = load_x + (spot - 1) * cell_w
        svg.append(f'<rect x="{x}" y="{load_y}" width="{cell_w}" height="{load_h}" fill="none" stroke="#333" stroke-width="1" opacity="0.55"/>')
        svg.append(f'<text x="{x + cell_w/2}" y="{load_y + load_h + 18}" font-size="12" text-anchor="middle" fill="#333">{spot}</text>')

        if spot in DOORFRAME_NO_ME:
            svg.append(f'<rect x="{x+2}" y="{load_y+2}" width="{cell_w-4}" height="{load_h-4}" fill="none" stroke="#7a0000" stroke-width="4"/>')
            svg.append(f'<text x="{x + cell_w/2}" y="{load_y + 16}" font-size="11" text-anchor="middle" fill="#7a0000">NO ME</text>')

    # Draw normal cells, but SKIP blocked spot when it is part of a TURN tier (we'll draw merged wide block instead)
    for spot in range(1, FLOOR_SPOTS + 1):
        col = matrix[spot - 1]
        x = load_x + (spot - 1) * cell_w

        for t in range(tiers):
            pid = col[t]
            if pid is None or pid == BLOCK:
                continue

            # If this is the blocked spot, and the TURN spot at same tier has same pid,
            # skip drawing here (merged block will be drawn from TURN spot).
            if spot == blocked:
                pid_turn = matrix[turn_spot - 1][t]
                if pid_turn == pid:
                    continue

            y = load_y + load_h - (t + 1) * cell_h
            fill = color_for_pid(pid)
            svg.append(f'<rect x="{x+1}" y="{y+1}" width="{cell_w-2}" height="{cell_h-2}" fill="{fill}" stroke="#1a1a1a" stroke-width="1" opacity="0.95"/>')
            hp = " HP" if (pid in products and products[pid].is_half_pack) else ""
            svg.append(f'<text x="{x + cell_w/2}" y="{y + cell_h/2 + 5}" font-size="13" text-anchor="middle" fill="#0a0a0a">{pid}{hp}</text>')

    # TURN tiers render: merged wide rectangles across turn_spot + blocked
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

with st.sidebar:
    st.header("Settings")
    car_id = st.text_input("Car ID", value="TBOX632012")
    max_tiers = st.slider("Max tiers per spot", 1, 8, 4)

    # Turn spot MUST be 7 or 8
    turn_spot = int(st.selectbox("Turn spot (must be 7 or 8)", ["7", "8"], index=0))

    unit_length_ref_in = st.number_input("Unit length ref (in) for gap drawing", min_value=1.0, value=96.0, step=1.0)

    auto_airbag = st.checkbox('Auto airbag (prefer <= 9")', value=True)
    if auto_airbag:
        airbag_gap_choice, airbag_gap_in = auto_airbag_choice()
    else:
        gap_labels = [f"{a}-{b}" for a, b in AIRBAG_ALLOWED_GAPS]
        gap_choice_label = st.selectbox("Airbag location", gap_labels, index=1)
        airbag_gap_choice = AIRBAG_ALLOWED_GAPS[gap_labels.index(gap_choice_label)]
        airbag_gap_in = st.slider("Airbag gap (in)", 6.0, 9.0, 6.0, 0.5)

    view_mode = st.radio("View", ["Top + Side", "Top only", "Side only"], index=0)

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

# Sort and dedupe IDs
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
    thick = r.get(COL_THICK)
    width = r.get(COL_WIDTH)
    length = r.get(COL_LENGTH)
    pcs = r.get(COL_PIECECOUNT)
    wt = r.get(COL_UNIT_WT)

    hp = ""
    if COL_HALF_PACK in pm_cf.columns:
        hp = " HP" if _truthy(r.get(COL_HALF_PACK, "")) else ""
    else:
        hp = " HP" if desc.upper().rstrip().endswith("HP") else ""

    parts = [f"{pid}{hp}"]
    dims = []
    if pd.notna(thick): dims.append(f'{float(thick):g}"')
    if pd.notna(width): dims.append(f'{float(width):g}')
    if pd.notna(length): dims.append(f'{float(length):g}')
    if dims:
        parts.append(" x ".join(dims))  # ASCII x only
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

c1, c2, c3, c4 = st.columns([2, 1, 1, 1], vertical_alignment="bottom")
with c1:
    tiers_to_add = st.number_input("Tiers to add", min_value=1, value=4, step=1)
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

# Requested table w/ description
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
            }
        )
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=220)
else:
    st.info("Add one or more SKUs, then click Optimize Layout.")

msgs: List[str] = []
if optimize_btn:
    if not st.session_state.requests:
        st.warning("No request lines to optimize.")
    else:
        matrix, msgs = optimize_layout(products, st.session_state.requests, int(max_tiers), turn_spot)
        st.session_state.matrix = matrix

for m in msgs:
    st.warning(m)

matrix = st.session_state.matrix

# Summary
payload = 0.0
placed = 0
for spot in range(1, FLOOR_SPOTS + 1):
    for pid in matrix[spot - 1]:
        if pid and pid != BLOCK and pid in products:
            payload += products[pid].unit_weight_lbs
            placed += 1

st.subheader("Summary")
st.metric("Payload (lbs)", f"{payload:,.0f}")
st.metric("Placed tiers", f"{placed:,}")

blocked = blocked_spot_for_turn(turn_spot)
note = (
    f"Commodity: {commodity_selected} | Facility: {facility_selected} | "
    f"Doorway: {DOOR_START_SPOT}-{DOOR_END_SPOT} | "
    f"Airbag: {airbag_gap_choice[0]}-{airbag_gap_choice[1]} @ {airbag_gap_in:.1f}\" | "
    f"Turn spans {turn_spot}-{blocked} (consumes 2 spots)"
)

top_svg = render_top_svg(
    car_id=car_id,
    matrix=matrix,
    products=products,
    note=note,
    turn_spot=turn_spot,
    airbag_gap_choice=airbag_gap_choice,
    airbag_gap_in=float(airbag_gap_in),
    unit_length_ref_in=float(unit_length_ref_in),
)

side_svg = render_side_svg(
    car_id=car_id,
    matrix=matrix,
    products=products,
    title="Side (Load Xpert style)",
    turn_spot=turn_spot,
    airbag_gap_choice=airbag_gap_choice,
    airbag_gap_in=float(airbag_gap_in),
    unit_length_ref_in=float(unit_length_ref_in),
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
