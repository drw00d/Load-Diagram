# streamlit_app.py
# Route A (Dynamic 2D Canvas + Dynamic 3D Three.js) — FULL FILE
#
# FIXED: No Python f-strings around JS template literals.
# The HTML is a raw template and we inject only JSON payload via .replace("__PAYLOAD__", payload_json).
#
# Requirements:
#   pip install streamlit pandas openpyxl
#
# Files:
#   data/Ortec SP Product Master.xlsx
#
# Notes:
# - PDF used Base14 Helvetica (not embedded). Browser uses Helvetica/Arial fallback.
# - Three.js is loaded from a CDN. If your environment blocks CDNs, vendor three.min.js in your repo
#   and change the script src accordingly.

from __future__ import annotations

import math
import json
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components


# =============================
# Page
# =============================
st.set_page_config(page_title="Load Diagram Optimizer — Route A", layout="wide")
st.title("Load Diagram Optimizer — Route A (Canvas 2D + Three.js 3D)")

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

BLOCK = "__BLOCK__"


# =============================
# Load Xpert-style constants
# =============================
FLOOR_SPOTS_BOXCAR = 15
FLOOR_SPOTS_CENTERBEAM = 18

# Doorway conventions (boxcar)
DOOR_START_SPOT = 6
DOOR_END_SPOT = 9

DOORFRAME_NO_ME = {6, 9}
DOORPOCKET_PINS = {7, 8}

AIRBAG_ALLOWED_GAPS = [(6, 7), (7, 8), (8, 9)]

# PDF-extracted palette candidates (from raster panels)
DEFAULT_CODE_COLORS = {
    "A": {"fill": "#2FB448", "stroke": "#1B5E20"},  # green candidate
    "B": {"fill": "#FAFD7C", "stroke": "#8A8D00"},  # yellow candidate
    "C": {"fill": "#76C3C0", "stroke": "#1B6F6A"},  # teal candidate
    "TURN": {"fill": "#FFFFFF", "stroke": "#111111"},
}

# Page style
LX_BLUE = "#0b2a7a"
LX_RED = "#c00000"
LX_RED2 = "#d00000"
LX_GRID = "#000000"


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
        if pref == "heavy":
            pid = pop_best(heavy, products, turn_spot, t, max_tiers, turn_spot) or pop_best(light, products, turn_spot, t, max_tiers, turn_spot)
        else:
            pid = pop_best(light, products, turn_spot, t, max_tiers, turn_spot) or pop_best(heavy, products, turn_spot, t, max_tiers, turn_spot)

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

    if floor_spots == FLOOR_SPOTS_BOXCAR:
        force_turn_tiers(matrix, products, heavy, light, max_tiers, turn_spot, required_turn_tiers, msgs)

        outside = [1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15]
        doorway = [7, 8, 6, 9]
        order = [s for s in outside + doorway if not is_blocked_spot(s, turn_spot)]
    else:
        order = [s for s in range(1, floor_spots + 1)]

    def tier_pref(t: int) -> str:
        return "heavy" if t % 2 == 0 else "light"

    while heavy or light:
        placed_any = False
        for spot in order:
            t = next_empty_tier_index(matrix, spot, turn_spot)
            if t is None:
                continue

            pref = tier_pref(t)
            if pref == "heavy":
                pid = pop_best(heavy, products, spot, t, max_tiers, turn_spot) or pop_best(light, products, spot, t, max_tiers, turn_spot)
            else:
                pid = pop_best(light, products, spot, t, max_tiers, turn_spot) or pop_best(heavy, products, spot, t, max_tiers, turn_spot)

            if pid is None:
                continue

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
# A/B/C code assignment (stub)
# =============================
def code_for_pid(pid: str, products: Dict[str, Product]) -> str:
    """
    Replace this with your real A/B/C mapping if you have it (table, SKU prefix, etc).
    Current fallback:
      - Half pack => A
      - Otherwise => B
    """
    p = products.get(pid)
    if p and p.is_half_pack:
        return "A"
    return "B"


# =============================
# Renderer Component (NO f-strings)
# =============================
def render_loadxpert_routeA_component(
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
    floor_spots: int,
    max_tiers: int,
    turn_spot: int,
    airbag_gap_choice: Tuple[int, int],
    airbag_gap_in: float,
    code_colors: Dict[str, Dict[str, str]],
    hatch_angle_deg: float,
    hatch_spacing_px: float,
    hatch_alpha: float,
    cam_fov: float,
    cam_x: float,
    cam_y: float,
    cam_z: float,
    light_intensity: float,
    ambient_intensity: float,
    show_edges: bool,
    flip_side: bool,
    height_px: int = 1020,
) -> None:
    tiers = len(matrix[0]) if matrix else max_tiers
    tiers = max(1, int(tiers))

    blocked = blocked_spot_for_turn(turn_spot) if floor_spots == FLOOR_SPOTS_BOXCAR else None

    cells = []
    for spot in range(1, floor_spots + 1):
        for t in range(tiers):
            pid = matrix[spot - 1][t]
            if pid is None or pid == BLOCK:
                continue
            # dedupe turn span in blocked spot (avoid double draw)
            if floor_spots == FLOOR_SPOTS_BOXCAR and blocked == spot:
                pid_turn = matrix[turn_spot - 1][t]
                if pid_turn == pid:
                    continue

            p = products.get(pid)
            cells.append(
                {
                    "spot": spot,
                    "tier": t,
                    "pid": pid,
                    "code": code_for_pid(pid, products),
                    "hp": bool(p.is_half_pack) if p else False,
                    "unit_h": float(p.unit_height_in) if p else 20.0,
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
            "floor_spots": floor_spots,
            "tiers": tiers,
            "turn_spot": turn_spot,
            "blocked_spot": blocked,
            "airbag_choice": airbag_gap_choice,
            "airbag_gap_in": airbag_gap_in,
            "door_start": DOOR_START_SPOT,
            "door_end": DOOR_END_SPOT,
            "is_boxcar": floor_spots == FLOOR_SPOTS_BOXCAR,
        },
        "colors": code_colors,
        "hatch": {"angle_deg": hatch_angle_deg, "spacing_px": hatch_spacing_px, "alpha": hatch_alpha},
        "three": {
            "cam_fov": cam_fov,
            "cam_pos": [cam_x, cam_y, cam_z],
            "light_intensity": light_intensity,
            "ambient_intensity": ambient_intensity,
            "show_edges": show_edges,
        },
        "flip_side": flip_side,
        "cells": cells,
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
</style>
</head>
<body>
<div class="wrap">
  <canvas id="page" width="1420" height="980"></canvas>
  <canvas id="three" width="520" height="980"></canvas>
</div>

<script>
const DATA = __PAYLOAD__;

// Style constants
const LX_GRID = "#000000";
const LX_BLUE = "#0b2a7a";
const LX_RED  = "#c00000";
const LX_RED2 = "#d00000";

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

function fitRotatedText(ctx, text, rectW, rectH, fontFamily, maxPx, minPx, weight="700") {
  let fs = maxPx;
  while (fs >= minPx) {
    ctx.font = `${weight} ${fs}px ${fontFamily}`;
    const tw = ctx.measureText(text).width;
    if (tw <= rectH * 0.92 && fs <= rectW * 0.92) return fs;
    fs -= 1;
  }
  return minPx;
}

function fitNormalText(ctx, text, rectW, rectH, fontFamily, maxPx, minPx, weight="400") {
  let fs = maxPx;
  while (fs >= minPx) {
    ctx.font = `${weight} ${fs}px ${fontFamily}`;
    const tw = ctx.measureText(text).width;
    if (tw <= rectW * 0.92 && fs <= rectH * 0.72) return fs;
    fs -= 1;
  }
  return minPx;
}

// --------- 2D draw ----------
(function drawPage() {
  const canvas = document.getElementById('page');
  const ctx = canvas.getContext('2d');
  const M = 18;
  const W = canvas.width, H = canvas.height;

  ctx.clearRect(0,0,W,H);

  // Outer border
  ctx.strokeStyle = LX_GRID;
  ctx.lineWidth = 2;
  ctx.strokeRect(M, M, W-2*M, H-2*M);

  // Title
  ctx.fillStyle = "#111";
  ctx.font = "700 20px Helvetica, Arial, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(DATA.meta.page_title, W/2, M+24);

  // Header table (basic)
  const hx = M+10, hy = M+40;
  const hw = W-2*M-20, hh = 88;
  ctx.lineWidth = 2;
  ctx.strokeStyle = LX_GRID;
  ctx.strokeRect(hx, hy, hw, hh);

  const colFracs = [0.14,0.22,0.20,0.22,0.22];
  const colX = [hx];
  for (let i=0;i<colFracs.length-1;i++) colX.push(colX[colX.length-1] + hw*colFracs[i]);
  for (let i=1;i<colX.length;i++) { ctx.beginPath(); ctx.moveTo(colX[i], hy); ctx.lineTo(colX[i], hy+hh); ctx.stroke(); }
  const midY = hy + hh*0.55;
  ctx.beginPath(); ctx.moveTo(hx, midY); ctx.lineTo(hx+hw, midY); ctx.stroke();

  const headers = ["Created By","Created At","Order Number","Vehicle Number","PO Number"];
  const vals = [DATA.meta.created_by, DATA.meta.created_at, DATA.meta.order_number, DATA.meta.vehicle_number, DATA.meta.po_number];

  ctx.textAlign = "left";
  for (let i=0;i<5;i++) {
    const x0 = colX[i];
    ctx.font = "700 16px Helvetica, Arial, sans-serif";
    ctx.fillStyle = "#111";
    ctx.fillText(headers[i], x0+10, hy+24);
    ctx.font = "400 18px Helvetica, Arial, sans-serif";
    ctx.fillText(vals[i], x0+10, midY+30);
  }

  // Top strip geometry
  const topX = M+80, topY = hy+hh+28;
  const topW = W-2*M-160, topH = 140;

  ctx.textAlign = "center";
  ctx.font = "700 18px Helvetica, Arial, sans-serif";
  ctx.fillStyle = "#111";
  ctx.fillText("Top", W/2, topY-8);

  ctx.strokeStyle = LX_BLUE;
  ctx.lineWidth = 3;
  ctx.strokeRect(topX, topY, topW, topH);

  // Ruler
  const rulerY = topY - 14;
  ctx.lineWidth = 2;
  ctx.beginPath(); ctx.moveTo(topX, rulerY); ctx.lineTo(topX+topW, rulerY); ctx.stroke();
  const ticks = 70;
  ctx.lineWidth = 1;
  for (let i=0;i<=ticks;i++) {
    const tx = topX + (topW * i / ticks);
    const hh2 = (i%5===0) ? 10 : 6;
    ctx.beginPath(); ctx.moveTo(tx, rulerY); ctx.lineTo(tx, rulerY+hh2); ctx.stroke();
  }

  const spots = DATA.meta.floor_spots;
  const cellW = topW / spots;

  // representative PID per spot
  const rep = new Map();
  for (const c of DATA.cells) { if (!rep.has(c.spot)) rep.set(c.spot, c.pid); }

  const order = [];
  for (let s=1;s<=spots;s++) order.push(s);
  if (DATA.flip_side) order.reverse();

  for (let i=0;i<order.length;i++) {
    const spot = order[i];
    const pid = rep.get(spot) || null;
    const x = topX + i*cellW;

    let fill = "#fff";
    if (pid) {
      const first = DATA.cells.find(z => z.spot === spot);
      const code = (first && first.code) ? first.code : "B";
      fill = (DATA.colors[code] && DATA.colors[code].fill) ? DATA.colors[code].fill : "#fff";
    }

    ctx.fillStyle = fill;
    ctx.fillRect(x, topY, cellW, topH);

    ctx.strokeStyle = LX_BLUE;
    ctx.lineWidth = 1;
    ctx.strokeRect(x, topY, cellW, topH);

    if (pid) {
      const fontFamily = "Helvetica, Arial, sans-serif";
      const fs = fitRotatedText(ctx, pid, cellW, topH, fontFamily, 26, 10, "700");
      ctx.save();
      ctx.translate(x + cellW/2, topY + topH/2);
      ctx.rotate(-Math.PI/2);
      ctx.font = `700 ${fs}px ${fontFamily}`;
      ctx.fillStyle = "#111";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(pid, 0, 0);
      ctx.restore();
    }
  }

  // Doorway + airbag + turn hatch (boxcar only)
  if (DATA.meta.is_boxcar) {
    const doorLeft = topX + (DATA.meta.door_start - 1)*cellW;
    const doorRight = topX + (DATA.meta.door_end)*cellW;

    ctx.fillStyle = "rgba(255,255,255,0.55)";
    ctx.fillRect(doorLeft, topY, doorRight-doorLeft, topH);

    ctx.strokeStyle = LX_RED;
    ctx.lineWidth = 3;
    ctx.strokeRect(doorLeft, topY, doorRight-doorLeft, topH);

    ctx.font = "400 12px Helvetica, Arial, sans-serif";
    ctx.fillStyle = LX_RED;
    ctx.textAlign = "left";
    ctx.fillText(`Doorway (Spots ${DATA.meta.door_start}-${DATA.meta.door_end})`, doorLeft+6, topY-4);

    const a = DATA.meta.airbag_choice[0];
    const airX = topX + a*cellW;
    ctx.fillStyle = LX_RED2;
    ctx.fillRect(airX-3, topY, 6, topH);

    const tz = topX + (DATA.meta.turn_spot - 1)*cellW;
    drawHatch(ctx, tz, topY, cellW*2, topH, DATA.hatch.angle_deg, DATA.hatch.spacing_px, DATA.hatch.alpha, "#000");
    ctx.strokeStyle = "#111";
    ctx.lineWidth = 1;
    ctx.strokeRect(tz, topY, cellW*2, topH);
  }

  // Side strip
  const sideX = M+80, sideY = topY+topH+60;
  const sideW = W-2*M-160, sideH = 260;

  ctx.textAlign = "center";
  ctx.font = "700 18px Helvetica, Arial, sans-serif";
  ctx.fillStyle = "#111";
  ctx.fillText(DATA.flip_side ? "Side2" : "Side1", W/2, sideY-10);

  ctx.strokeStyle = LX_BLUE;
  ctx.lineWidth = 3;
  ctx.strokeRect(sideX, sideY, sideW, sideH);

  // wheels
  const wy = sideY + sideH - 22;
  const wheelXs = [sideX+sideW*0.15, sideX+sideW*0.22, sideX+sideW*0.78, sideX+sideW*0.85];
  for (const wx of wheelXs) {
    ctx.beginPath();
    ctx.fillStyle = "rgba(102,102,102,0.6)";
    ctx.arc(wx, wy, 14, 0, Math.PI*2);
    ctx.fill();
  }

  const inset = 10;
  const lx = sideX+inset, ly = sideY+inset;
  const lw = sideW-2*inset, lh = sideH-2*inset-35;

  const cw = lw / spots;
  const tiers = DATA.meta.tiers;
  const ch = lh / tiers;

  if (DATA.meta.is_boxcar) {
    const dl = lx + (DATA.meta.door_start - 1)*cw;
    const dr = lx + (DATA.meta.door_end)*cw;
    ctx.fillStyle = "rgba(255,255,255,0.55)";
    ctx.fillRect(dl, ly, dr-dl, lh);
    ctx.strokeStyle = LX_RED;
    ctx.lineWidth = 3;
    ctx.strokeRect(dl, ly, dr-dl, lh);

    const a2 = DATA.meta.airbag_choice[0];
    const ax = lx + a2*cw;
    ctx.fillStyle = LX_RED2;
    ctx.fillRect(ax-3, ly, 6, lh);
  }

  // grid + numbers
  ctx.strokeStyle = "rgba(51,51,51,0.6)";
  ctx.lineWidth = 1;
  ctx.fillStyle = "#333";
  ctx.font = "400 12px Helvetica, Arial, sans-serif";
  ctx.textAlign = "center";
  for (let i=0;i<order.length;i++) {
    const spot = order[i];
    const x = lx + i*cw;
    ctx.strokeRect(x, ly, cw, lh);
    ctx.fillText(String(spot), x+cw/2, ly+lh+24);
  }

  // blocks
  for (const c of DATA.cells) {
    const idx = order.indexOf(c.spot);
    if (idx < 0) continue;
    const x = lx + idx*cw;
    const y = ly + lh - (c.tier+1)*ch;

    const fill = (DATA.colors[c.code] && DATA.colors[c.code].fill) ? DATA.colors[c.code].fill : "#fff";
    ctx.fillStyle = fill;
    ctx.strokeStyle = "#111";
    ctx.lineWidth = 1;
    ctx.fillRect(x+1, y+1, cw-2, ch-2);
    ctx.strokeRect(x+1, y+1, cw-2, ch-2);

    const fontFamily = "Helvetica, Arial, sans-serif";
    const fs = fitNormalText(ctx, c.pid, cw-6, ch-6, fontFamily, 14, 8, "400");
    ctx.font = `400 ${fs}px ${fontFamily}`;
    ctx.fillStyle = "#111";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(c.pid, x+cw/2, y+ch/2);
  }
})();
</script>

<script src="https://unpkg.com/three@0.160.0/build/three.min.js"></script>
<script>
(function drawThree() {
  const canvas = document.getElementById('three');
  const renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true });
  renderer.setSize(canvas.width, canvas.height, false);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xffffff);

  const camera = new THREE.PerspectiveCamera(DATA.three.cam_fov, canvas.width/canvas.height, 0.1, 1000);
  camera.position.set(DATA.three.cam_pos[0], DATA.three.cam_pos[1], DATA.three.cam_pos[2]);
  camera.lookAt(0, 0, 0);

  scene.add(new THREE.AmbientLight(0xffffff, DATA.three.ambient_intensity));

  const dir = new THREE.DirectionalLight(0xffffff, DATA.three.light_intensity);
  dir.position.set(8, 12, 10);
  scene.add(dir);

  const grid = new THREE.GridHelper(20, 20, 0xcccccc, 0xeeeeee);
  grid.position.y = -0.01;
  scene.add(grid);

  const spots = DATA.meta.floor_spots;
  const tiers = DATA.meta.tiers;

  const spotW = 0.9;
  const spotD = DATA.meta.is_boxcar ? 1.1 : 0.9;
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
    const spotIndex = (DATA.flip_side) ? (spots - c.spot) : (c.spot - 1);
    const x = x0 + spotIndex * spotW;
    const y = (c.tier + 0.5) * tierH;
    const z = 0;

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
    st.session_state.requests = []
if "matrix" not in st.session_state:
    st.session_state.matrix = make_empty_matrix(4, 7, FLOOR_SPOTS_BOXCAR)

# Sidebar
with st.sidebar:
    st.header("Route A Controls")

    car_type = st.selectbox("Car Type", ["Boxcar (15)", "Centerbeam (18)"], index=0)
    is_boxcar = car_type.startswith("Boxcar")
    floor_spots = FLOOR_SPOTS_BOXCAR if is_boxcar else FLOOR_SPOTS_CENTERBEAM

    st.divider()
    car_id = st.text_input("Vehicle Number / Car ID", value="TBOX632012")
    order_number = st.text_input("Order Number", value="—")
    po_number = st.text_input("PO Number", value="—")
    created_by = st.text_input("Created By", value="—")
    created_at = st.text_input("Created At", value="—")
    vehicle_number = st.text_input("Vehicle Number (label)", value="—")

    st.divider()
    max_tiers = st.slider("Max tiers per spot", 1, 10, 4)

    if is_boxcar:
        turn_spot = int(st.selectbox("Turn spot (must be 7 or 8)", ["7", "8"], index=0))
        required_turn_tiers = st.slider("Turn tiers required (HARD)", 0, int(max_tiers), int(max_tiers))
        required_turn_tiers = min(int(required_turn_tiers), int(max_tiers))

        auto_airbag = st.checkbox('Auto airbag (prefer <= 9")', value=True)
        if auto_airbag:
            airbag_gap_choice, airbag_gap_in = (7, 8), 6.0
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

    st.divider()
    st.subheader("A/B/C Colors")
    colA = st.color_picker("A Fill", DEFAULT_CODE_COLORS["A"]["fill"])
    colB = st.color_picker("B Fill", DEFAULT_CODE_COLORS["B"]["fill"])
    colC = st.color_picker("C Fill", DEFAULT_CODE_COLORS["C"]["fill"])
    code_colors = {
        "A": {"fill": colA, "stroke": DEFAULT_CODE_COLORS["A"]["stroke"]},
        "B": {"fill": colB, "stroke": DEFAULT_CODE_COLORS["B"]["stroke"]},
        "C": {"fill": colC, "stroke": DEFAULT_CODE_COLORS["C"]["stroke"]},
        "TURN": DEFAULT_CODE_COLORS["TURN"],
    }

    st.divider()
    st.subheader("Hatch Calibration")
    hatch_angle_deg = st.slider("Hatch angle (deg)", 0.0, 90.0, 45.0, 1.0)
    hatch_spacing_px = st.slider("Hatch spacing (px)", 4.0, 20.0, 10.0, 1.0)
    hatch_alpha = st.slider("Hatch opacity", 0.05, 0.6, 0.18, 0.01)

    st.divider()
    st.subheader("3D Calibration")
    cam_fov = st.slider("Camera FOV", 20.0, 75.0, 42.0, 1.0)
    cam_x = st.slider("Camera X", -30.0, 30.0, 10.0, 0.5)
    cam_y = st.slider("Camera Y", 0.0, 30.0, 10.0, 0.5)
    cam_z = st.slider("Camera Z", -40.0, 40.0, 18.0, 0.5)
    light_intensity = st.slider("Directional light", 0.2, 3.0, 1.2, 0.1)
    ambient_intensity = st.slider("Ambient light", 0.0, 2.0, 0.65, 0.05)
    show_edges = st.checkbox("3D edges/outline", value=True)

    st.divider()
    flip_side = st.checkbox("Render Side2 (flip)", value=False)

    st.divider()
    optimize_btn = st.button("Optimize Layout")
    render_btn = st.button("Generate Diagram", type="primary")
    clear_btn = st.button("Clear All")

if clear_btn:
    st.session_state.requests = []
    st.session_state.matrix = make_empty_matrix(int(max_tiers), int(turn_spot), int(floor_spots))

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

pm_cf = pm_cf.drop_duplicates(subset=[COL_PRODUCT_ID], keep="first").head(5000)
options = pm_cf.to_dict("records")


def option_label(r: dict) -> str:
    pid = str(r.get(COL_PRODUCT_ID, "")).strip()
    desc = str(r.get(COL_DESC, "")).strip()
    wt = r.get(COL_UNIT_WT)
    if COL_HALF_PACK in pm_cf.columns:
        hp = " HP" if _truthy(r.get(COL_HALF_PACK, "")) else ""
    else:
        hp = " HP" if desc.upper().rstrip().endswith("HP") else ""
    parts = [f"{pid}{hp}"]
    if pd.notna(wt):
        parts.append(f"{float(wt):,.0f} lbs")
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
        rows.append({"Sales Product Id": r.product_id, "Description": p.description if p else "", "Tiers": r.tiers})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=220)
else:
    st.info("Add one or more SKUs, then Optimize Layout, then Generate Diagram.")

msgs: List[str] = []
if optimize_btn:
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

if render_btn:
    if not st.session_state.requests:
        st.warning("No request lines to render.")
    else:
        if not st.session_state.matrix or len(st.session_state.matrix) != int(floor_spots):
            st.session_state.matrix = make_empty_matrix(int(max_tiers), int(turn_spot), int(floor_spots))

        render_loadxpert_routeA_component(
            page_title="Top + Side View (Route A)",
            created_by=str(created_by if created_by != "—" else facility_selected),
            created_at=str(created_at),
            order_number=str(order_number),
            vehicle_number=str(vehicle_number),
            po_number=str(po_number),
            car_id=str(car_id),
            matrix=st.session_state.matrix,
            products=products,
            floor_spots=int(floor_spots),
            max_tiers=int(max_tiers),
            turn_spot=int(turn_spot),
            airbag_gap_choice=airbag_gap_choice,
            airbag_gap_in=float(airbag_gap_in),
            code_colors=code_colors,
            hatch_angle_deg=float(hatch_angle_deg),
            hatch_spacing_px=float(hatch_spacing_px),
            hatch_alpha=float(hatch_alpha),
            cam_fov=float(cam_fov),
            cam_x=float(cam_x),
            cam_y=float(cam_y),
            cam_z=float(cam_z),
            light_intensity=float(light_intensity),
            ambient_intensity=float(ambient_intensity),
            show_edges=bool(show_edges),
            flip_side=bool(flip_side),
            height_px=1040,
        )
else:
    st.caption("Click **Generate Diagram** to render the Canvas 2D page + Three.js 3D panel.")
