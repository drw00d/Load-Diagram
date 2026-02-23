# streamlit_app.py
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
st.set_page_config(page_title="Load Diagram Optimizer — LoadXpert Route A", layout="wide")
st.title("Load Diagram Optimizer — LoadXpert Route A")


MASTER_PATH = "data/Ortec SP Product Master.xlsx"

COL_COMMODITY = "Product Type"
COL_FACILITY = "Facility Id"
COL_PRODUCT_ID = "Sales Product Id"
COL_DESC = "Short Descrip"
COL_ACTIVE = "Active"
COL_UNIT_H = "Unit Height (In)"
COL_UNIT_WT = "Unit Weight (lbs)"
COL_EDGE = "Edge Type"

COL_HALF_PACK = "Half Pack"
COL_THICK = "Panel Thickness"
COL_WIDTH = "Width"
COL_LENGTH = "Length"
COL_PIECECOUNT = "Piece Count"

BLOCK = "__BLOCK__"

# =============================
# LoadXpert conventions
# =============================
FLOOR_SPOTS_BOXCAR = 15
DOOR_START_SPOT = 6
DOOR_END_SPOT = 9

DOORFRAME_NO_ME = {6, 9}
DOORPOCKET_PINS = {7, 8}
AIRBAG_ALLOWED_GAPS = [(6, 7), (7, 8), (8, 9)]

LX_BLUE = "#0b2a7a"
LX_RED = "#c00000"
LX_RED2 = "#d00000"
LX_GRID = "#000000"

# These are “close to” the PDF colors:
# teal/aqua for A, green for C, and yellow for B in the example. :contentReference[oaicite:2]{index=2}
DEFAULT_CODE_COLORS = {
    "A": {"fill": "#79C7C7", "stroke": "#111111"},
    "B": {"fill": "#F4F48A", "stroke": "#111111"},
    "C": {"fill": "#2FB34B", "stroke": "#111111"},
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
# Data helpers
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
# Matrix helpers
# =============================
def make_empty_matrix(max_tiers: int, turn_spot: int) -> List[List[Optional[str]]]:
    m = [[None for _ in range(max_tiers)] for _ in range(FLOOR_SPOTS_BOXCAR)]
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
# Placement rules (hard + soft)
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
) -> Tuple[List[List[Optional[str]]], List[str]]:
    msgs: List[str] = []
    heavy, light = build_token_lists(products, requests)
    matrix = make_empty_matrix(max_tiers, turn_spot)

    # Turn usage first
    force_turn_tiers(matrix, products, heavy, light, max_tiers, turn_spot, required_turn_tiers, msgs)

    # Fill order: outside doorway, then doorway
    outside = [1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15]
    doorway = [7, 8, 6, 9]
    order = [s for s in outside + doorway if not is_blocked_spot(s, turn_spot)]

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
# A/B/C mapping (placeholder)
# =============================
def code_for_pid(pid: str, products: Dict[str, Product]) -> str:
    """
    Replace with your real LoadXpert SKU->A/B/C mapping.
    For now:
      - Half pack => B (matches example where HP line is B in the item table)
      - Else => A
    """
    p = products.get(pid)
    if p and p.is_half_pack:
        return "B"
    return "A"


# =============================
# Render (LoadXpert-style “Top + Side”)
# =============================
def render_loadxpert_top_side_pdf_style(
    *,
    view_title: str,
    created_by: str,
    created_at: str,
    order_number: str,
    vehicle_number: str,
    po_number: str,
    matrix: List[List[Optional[str]]],
    products: Dict[str, Product],
    turn_spot: int,
    airbag_gap_choice: Tuple[int, int],
    airbag_gap_in: float,
    cg_height_in: float,
    whole_unit_equiv: float,
    total_lisa_units: float,
    code_colors: Dict[str, Dict[str, str]],
    flip_side: bool,
    hatch_angle_deg: float,
    hatch_spacing_px: float,
    hatch_alpha: float,
    height_px: int,
) -> None:
    tiers = len(matrix[0]) if matrix else 4
    blocked = blocked_spot_for_turn(turn_spot)

    # Build “spot representative blocks” for TOP view:
    # Use first non-empty pid in each spot (ignoring BLOCK).
    rep = {}
    for s in range(1, FLOOR_SPOTS_BOXCAR + 1):
        col = matrix[s - 1]
        pid = next((x for x in col if x and x != BLOCK), None)
        rep[s] = pid

    # Build SIDE grid tokens
    cells = []
    for s in range(1, FLOOR_SPOTS_BOXCAR + 1):
        for t in range(tiers):
            pid = matrix[s - 1][t]
            if pid is None or pid == BLOCK:
                continue
            # don't double paint blocked span
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

    # Hatch behavior from PDF:
    # Doorframe loads (spots 6 and 9) are hatched to indicate securement requirements. :contentReference[oaicite:3]{index=3}
    hatched_spots = sorted(list(DOORFRAME_NO_ME))

    payload = {
        "meta": {
            "view_title": view_title,
            "created_by": created_by,
            "created_at": created_at,
            "order_number": order_number,
            "vehicle_number": vehicle_number,
            "po_number": po_number,
            "spots": FLOOR_SPOTS_BOXCAR,
            "tiers": tiers,
            "door_start": DOOR_START_SPOT,
            "door_end": DOOR_END_SPOT,
            "turn_spot": turn_spot,
            "blocked_spot": blocked,
            "airbag_a": airbag_gap_choice[0],
            "airbag_b": airbag_gap_choice[1],
            "airbag_in": airbag_gap_in,
            "cg_in": cg_height_in,
            "wue": whole_unit_equiv,
            "lisa": total_lisa_units,
            "flip_side": flip_side,
            "hatched_spots": hatched_spots,
        },
        "colors": code_colors,
        "hatch": {"angle": hatch_angle_deg, "spacing": hatch_spacing_px, "alpha": hatch_alpha},
        "rep": rep,
        "cells": cells,
    }
    payload_json = json.dumps(payload)

    HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<style>
  body { margin:0; padding:0; background:#fff; font-family: Helvetica, Arial, sans-serif; }
  canvas { background:#fff; }
</style>
</head>
<body>
<canvas id="c" width="1420" height="980"></canvas>

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

(function draw() {
  const canvas = document.getElementById("c");
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;

  ctx.clearRect(0,0,W,H);

  // Outer border
  ctx.strokeStyle = LX_GRID;
  ctx.lineWidth = 2;
  ctx.strokeRect(10, 10, W-20, H-20);

  // Title
  ctx.fillStyle = "#111";
  ctx.font = "700 22px Helvetica, Arial, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(DATA.meta.view_title, W/2, 38);

  // LoadXpert logo placeholder (top-left)
  ctx.font = "700 16px Helvetica, Arial, sans-serif";
  ctx.textAlign = "left";
  ctx.fillText("LOADXPERT", 36, 58);
  ctx.font = "400 11px Helvetica, Arial, sans-serif";
  ctx.fillText("SOFTWARE", 36, 72);

  // Header table
  const hx=30, hy=80, hw=W-60, hh=84;
  ctx.strokeStyle = LX_GRID; ctx.lineWidth = 2;
  ctx.strokeRect(hx,hy,hw,hh);
  const fr = [0.14,0.22,0.20,0.22,0.22];
  const xs=[hx];
  for (let i=0;i<fr.length-1;i++) xs.push(xs[xs.length-1] + hw*fr[i]);
  for (let i=1;i<xs.length;i++){ ctx.beginPath(); ctx.moveTo(xs[i],hy); ctx.lineTo(xs[i],hy+hh); ctx.stroke(); }
  const midY = hy + hh*0.55;
  ctx.beginPath(); ctx.moveTo(hx,midY); ctx.lineTo(hx+hw,midY); ctx.stroke();

  const headers=["Created By","Created At","Order Number","Vehicle Number","PO Number"];
  const vals=[DATA.meta.created_by, DATA.meta.created_at, DATA.meta.order_number, DATA.meta.vehicle_number, DATA.meta.po_number];

  for (let i=0;i<5;i++){
    ctx.fillStyle="#111";
    ctx.textAlign="left";
    ctx.font="700 14px Helvetica, Arial, sans-serif";
    ctx.fillText(headers[i], xs[i]+10, hy+24);
    ctx.font="400 16px Helvetica, Arial, sans-serif";
    ctx.fillText(vals[i], xs[i]+10, midY+28);
  }

  // Panels (match PDF proportions) :contentReference[oaicite:4]{index=4}
  const panelTopX = 360;
  const panelTopY = 195;
  const panelTopW = W - panelTopX - 40;
  const panelTopH = 160;

  const panelSideX = panelTopX;
  const panelSideY = panelTopY + panelTopH + 55;
  const panelSideW = panelTopW;
  const panelSideH = 250;

  // Divider line between top and side zones
  ctx.strokeStyle = "rgba(0,0,0,0.35)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(30, panelTopY-12);
  ctx.lineTo(W-30, panelTopY-12);
  ctx.stroke();

  // ---- TOP label ----
  ctx.font="700 16px Helvetica, Arial, sans-serif";
  ctx.fillStyle="#111";
  ctx.textAlign="center";
  ctx.fillText("Top", panelTopX + panelTopW/2, panelTopY - 10);

  // Top frame
  ctx.strokeStyle = LX_BLUE;
  ctx.lineWidth = 3;
  ctx.strokeRect(panelTopX, panelTopY, panelTopW, panelTopH);

  // Ruler (above top frame)
  const rulerY = panelTopY - 14;
  ctx.strokeStyle = LX_BLUE;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(panelTopX, rulerY);
  ctx.lineTo(panelTopX + panelTopW, rulerY);
  ctx.stroke();

  // Ticks
  const ticks=70;
  ctx.lineWidth=1;
  for (let i=0;i<=ticks;i++){
    const tx = panelTopX + (panelTopW*i/ticks);
    const th = (i%5===0)?10:6;
    ctx.beginPath();
    ctx.moveTo(tx, rulerY);
    ctx.lineTo(tx, rulerY+th);
    ctx.stroke();
  }

  // Top blocks with gutters (this is the big difference vs your output)
  const spots = DATA.meta.spots;
  const gutterFrac = 0.10;         // white gap between blocks like PDF
  const cellW = panelTopW / spots;
  const innerPadY = 12;
  const blockH = panelTopH - innerPadY*2;

  // doorway band bounds
  const doorLeft = panelTopX + (DATA.meta.door_start-1) * cellW;
  const doorRight = panelTopX + (DATA.meta.door_end) * cellW;

  // Draw doorway red band (over top blocks)
  ctx.strokeStyle = LX_RED;
  ctx.lineWidth = 3;
  ctx.strokeRect(doorLeft, panelTopY, doorRight-doorLeft, panelTopH);

  // Airbag red band at the boundary between a and b
  const airX = panelTopX + DATA.meta.airbag_a * cellW;
  ctx.fillStyle = LX_RED2;
  ctx.fillRect(airX-3, panelTopY, 6, panelTopH);

  // Spot order (Side1 vs Side2 flip matches PDF pages 3/4) :contentReference[oaicite:5]{index=5}
  const order = [];
  for (let s=1;s<=spots;s++) order.push(s);
  if (DATA.meta.flip_side) order.reverse();

  // Render blocks
  for (let i=0;i<order.length;i++){
    const s = order[i];
    const pid = DATA.rep[String(s)] || null;

    const x = panelTopX + i*cellW;
    const gx = x + cellW * gutterFrac * 0.5;
    const gw = cellW * (1 - gutterFrac);

    let fill = "#fff";
    if (pid){
      // pick code by first matching cell in that spot (approx)
      const c = DATA.cells.find(z => z.spot === s);
      const code = (c && c.code) ? c.code : "A";
      fill = (DATA.colors[code] && DATA.colors[code].fill) ? DATA.colors[code].fill : "#fff";
    }

    ctx.fillStyle = fill;
    ctx.fillRect(gx, panelTopY + innerPadY, gw, blockH);

    // thin inner outline
    ctx.strokeStyle = "#111";
    ctx.lineWidth = 1;
    ctx.strokeRect(gx, panelTopY + innerPadY, gw, blockH);

    // label rotated
    if (pid){
      const fs = fitRotated(ctx, pid, gw, blockH, 10, 22, "700");
      ctx.save();
      ctx.translate(gx + gw/2, panelTopY + innerPadY + blockH/2);
      ctx.rotate(-Math.PI/2);
      ctx.font = `700 ${fs}px Helvetica, Arial, sans-serif`;
      ctx.fillStyle = "#111";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(pid, 0, 0);
      ctx.restore();
    }
  }

  // Hatched securement blocks (doorframe spots 6 & 9) — matches PDF behavior :contentReference[oaicite:6]{index=6}
  for (const hs of DATA.meta.hatched_spots){
    const idx = order.indexOf(hs);
    if (idx < 0) continue;
    const x = panelTopX + idx*cellW;
    const gx = x + cellW * gutterFrac * 0.5;
    const gw = cellW * (1 - gutterFrac);
    drawHatch(ctx, gx, panelTopY + innerPadY, gw, blockH, DATA.hatch.angle, DATA.hatch.spacing, DATA.hatch.alpha, "#000");
  }

  // ---- SIDE label ----
  ctx.font="700 16px Helvetica, Arial, sans-serif";
  ctx.fillStyle="#111";
  ctx.textAlign="center";
  ctx.fillText(DATA.meta.flip_side ? "Side2" : "Side1", panelSideX + panelSideW/2, panelSideY - 10);

  // Side frame
  ctx.strokeStyle = LX_BLUE;
  ctx.lineWidth = 3;
  ctx.strokeRect(panelSideX, panelSideY, panelSideW, panelSideH);

  // Side interior
  const sidePad = 14;
  const sx = panelSideX + sidePad;
  const sy = panelSideY + sidePad;
  const sw = panelSideW - 2*sidePad;
  const sh = panelSideH - 2*sidePad - 36;  // room for spot numbers + wheels

  const cw = sw / spots;
  const ch = sh / DATA.meta.tiers;

  // Doorway red band + center airbag blank gap
  const sDoorLeft = sx + (DATA.meta.door_start-1)*cw;
  const sDoorRight = sx + (DATA.meta.door_end)*cw;
  ctx.strokeStyle = LX_RED;
  ctx.lineWidth = 3;
  ctx.strokeRect(sDoorLeft, sy, sDoorRight - sDoorLeft, sh);

  // Airbag blank gap column is the “blocked spot” (turn+1) shown empty in PDF side view
  // Render that as a white column with red edges
  const blankSpot = DATA.meta.blocked_spot;
  const blankIdx = order.indexOf(blankSpot);
  if (blankIdx >= 0){
    const bx = sx + blankIdx*cw;
    ctx.fillStyle = "#fff";
    ctx.fillRect(bx, sy, cw, sh);
    ctx.strokeStyle = LX_RED;
    ctx.lineWidth = 2;
    ctx.strokeRect(bx, sy, cw, sh);
  }

  // Airbag red band at boundary between a and b
  const sAirX = sx + DATA.meta.airbag_a * cw;
  ctx.fillStyle = LX_RED2;
  ctx.fillRect(sAirX-3, sy, 6, sh);

  // Grid + blocks
  ctx.strokeStyle = "#111";
  ctx.lineWidth = 1;

  // draw cells (skip blankSpot)
  for (let i=0;i<order.length;i++){
    const spot = order[i];
    const x = sx + i*cw;

    // outline columns
    ctx.strokeStyle = "rgba(17,17,17,0.55)";
    ctx.strokeRect(x, sy, cw, sh);

    if (spot === blankSpot) continue;

    for (let t=0;t<DATA.meta.tiers;t++){
      const y = sy + sh - (t+1)*ch;

      // find matching cell in payload
      const cell = DATA.cells.find(z => z.spot === spot && z.tier === t);
      if (!cell) continue;

      const fill = (DATA.colors[cell.code] && DATA.colors[cell.code].fill) ? DATA.colors[cell.code].fill : "#fff";

      ctx.fillStyle = fill;
      ctx.fillRect(x+1, y+1, cw-2, ch-2);
      ctx.strokeStyle = "#111";
      ctx.lineWidth = 1;
      ctx.strokeRect(x+1, y+1, cw-2, ch-2);

      // label
      const fs = fitNormal(ctx, cell.pid, cw-6, ch-6, 8, 14, "700");
      ctx.font = `700 ${fs}px Helvetica, Arial, sans-serif`;
      ctx.fillStyle = "#111";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(cell.pid, x+cw/2, y+ch/2);
    }
  }

  // Hatched securement overlay on side for doorframe spots (6 & 9)
  for (const hs of DATA.meta.hatched_spots){
    const idx = order.indexOf(hs);
    if (idx < 0) continue;
    const x = sx + idx*cw;
    drawHatch(ctx, x+1, sy+1, cw-2, sh-2, DATA.hatch.angle, DATA.hatch.spacing, DATA.hatch.alpha, "#000");
  }

  // Wheels (like PDF)
  const wheelY = panelSideY + panelSideH - 26;
  const wxs = [panelSideX + panelSideW*0.20, panelSideX + panelSideW*0.27, panelSideX + panelSideW*0.73, panelSideX + panelSideW*0.80];
  ctx.fillStyle = "rgba(80,80,80,0.85)";
  for (const wx of wxs){
    ctx.beginPath();
    ctx.arc(wx, wheelY, 14, 0, Math.PI*2);
    ctx.fill();
  }

  // Spot numbers under side (PDF shows 1..15 or reversed) :contentReference[oaicite:7]{index=7}
  ctx.fillStyle = "#111";
  ctx.font = "400 12px Helvetica, Arial, sans-serif";
  ctx.textAlign = "center";
  for (let i=0;i<order.length;i++){
    const x = sx + i*cw;
    ctx.fillText(String(order[i]), x+cw/2, sy+sh+22);
  }

  // Footer metrics row (matches PDF row) :contentReference[oaicite:8]{index=8}
  const fx = 30, fy = panelSideY + panelSideH + 20;
  const fw = W-60, fh = 70;
  ctx.strokeStyle = LX_GRID;
  ctx.lineWidth = 1.5;
  ctx.strokeRect(fx, fy, fw, fh);

  // 4 columns
  const fcols = [0.25,0.25,0.25,0.25];
  const fxs=[fx];
  for (let i=0;i<fcols.length-1;i++) fxs.push(fxs[fxs.length-1] + fw*fcols[i]);
  for (let i=1;i<fxs.length;i++){ ctx.beginPath(); ctx.moveTo(fxs[i],fy); ctx.lineTo(fxs[i],fy+fh); ctx.stroke(); }

  ctx.font = "700 12px Helvetica, Arial, sans-serif";
  ctx.fillStyle="#111";
  ctx.textAlign="left";

  ctx.fillText(`Floor spots = ${DATA.meta.spots}`, fxs[0]+10, fy+24);
  ctx.fillText(`C.G. height = ${DATA.meta.cg_in.toFixed(2)} (in)`, fxs[1]+10, fy+24);
  ctx.fillText(`Airbag Space = ${DATA.meta.airbag_in.toFixed(2)} (in)`, fxs[2]+10, fy+24);
  ctx.fillText(`Whole Unit Equivalent = ${DATA.meta.wue.toFixed(1)}`, fxs[3]+10, fy+24);

  ctx.fillText(`Total LISA Units = ${DATA.meta.lisa.toFixed(1)}`, fxs[3]+10, fy+48);

  // Hatch legend text like PDF (bottom) :contentReference[oaicite:9]{index=9}
  const ly = fy + fh + 20;
  ctx.font = "700 12px Helvetica, Arial, sans-serif";
  ctx.fillText("Secure Loads from:", 40, ly);

  // diagonal hatch swatch
  drawHatch(ctx, 170, ly-14, 34, 18, 45, 6, 0.35, "#000");
  ctx.strokeStyle="#111"; ctx.strokeRect(170, ly-14, 34, 18);
  ctx.font="400 12px Helvetica, Arial, sans-serif";
  ctx.fillText("sliding", 210, ly);

  // vertical hatch swatch
  drawHatch(ctx, 300, ly-14, 34, 18, 90, 6, 0.35, "#000");
  ctx.strokeStyle="#111"; ctx.strokeRect(300, ly-14, 34, 18);
  ctx.fillText("tipping & sliding", 340, ly);
})();
</script>
</body>
</html>
"""
    html = HTML.replace("__PAYLOAD__", payload_json)
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

with st.sidebar:
    st.header("Settings (PDF-matching layout)")

    view_title = st.text_input("View Title", value="Top + Side View (Route A)")
    created_by = st.text_input("Created By", value="307")
    created_at = st.text_input("Created At", value="Feb 11, 2026")
    order_number = st.text_input("Order Number", value="307305097")
    vehicle_number = st.text_input("Vehicle Number", value="UML_TBOX-0000644577")
    po_number = st.text_input("PO Number", value="WC2096056")

    st.divider()

    max_tiers = st.slider("Max tiers per spot", 1, 8, 4)
    turn_spot = int(st.selectbox("Turn spot (must be 7 or 8)", ["7", "8"], index=0))
    required_turn_tiers = st.slider("Turn tiers required (HARD)", 0, 8, int(max_tiers))
    required_turn_tiers = min(int(required_turn_tiers), int(max_tiers))

    st.divider()

    auto_airbag = st.checkbox('Auto airbag (prefer <= 9")', value=False)
    if auto_airbag:
        airbag_gap_choice, airbag_gap_in = (7, 8), 9.0
    else:
        gap_labels = [f"{a}-{b}" for a, b in AIRBAG_ALLOWED_GAPS]
        gap_choice_label = st.selectbox("Airbag location", gap_labels, index=1)
        airbag_gap_choice = AIRBAG_ALLOWED_GAPS[gap_labels.index(gap_choice_label)]
        airbag_gap_in = st.slider("Airbag space (in)", 6.0, 12.0, 9.0, 0.5)

    st.divider()

    st.subheader("Footer metrics (PDF row)")
    cg_height_in = st.number_input("C.G. height (in)", min_value=0.0, value=97.24, step=0.01)
    whole_unit_equiv = st.number_input("Whole Unit Equivalent", min_value=0.0, value=58.5, step=0.1)
    total_lisa_units = st.number_input("Total LISA Units", min_value=0.0, value=60.0, step=0.1)

    st.divider()

    st.subheader("A/B/C Colors (close to PDF)")
    colA = st.color_picker("A Fill", DEFAULT_CODE_COLORS["A"]["fill"])
    colB = st.color_picker("B Fill", DEFAULT_CODE_COLORS["B"]["fill"])
    colC = st.color_picker("C Fill", DEFAULT_CODE_COLORS["C"]["fill"])
    code_colors = {
        "A": {"fill": colA, "stroke": "#111111"},
        "B": {"fill": colB, "stroke": "#111111"},
        "C": {"fill": colC, "stroke": "#111111"},
    }

    st.divider()

    st.subheader("Hatch calibration")
    hatch_angle_deg = st.slider("Hatch angle (deg)", 0.0, 90.0, 45.0, 1.0)
    hatch_spacing_px = st.slider("Hatch spacing (px)", 4.0, 20.0, 8.0, 1.0)
    hatch_alpha = st.slider("Hatch opacity", 0.05, 0.6, 0.22, 0.01)

    st.divider()

    flip_side = st.checkbox("Side2 (flip like PDF page 4)", value=True)

    st.divider()

    optimize_btn = st.button("Optimize Layout")
    render_btn = st.button("Render PDF-style", type="primary")
    clear_btn = st.button("Clear All")


if clear_btn:
    st.session_state.requests = []
    st.session_state.matrix = make_empty_matrix(int(max_tiers), int(turn_spot))


st.success(f"Product Master loaded: {len(pm):,} rows")

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
    if COL_HALF_PACK in pm_cf.columns:
        hp = " HP" if _truthy(r.get(COL_HALF_PACK, "")) else ""
    else:
        hp = " HP" if desc.upper().rstrip().endswith("HP") else ""
    return f"{pid}{hp} | {desc}" if desc else f"{pid}{hp}"


labels = [option_label(r) for r in options]
selected_label = st.selectbox("Pick a Product", labels) if labels else None

c1, c2 = st.columns([2, 1], vertical_alignment="bottom")
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
        rows.append({"Sales Product Id": r.product_id, "Description": p.description if p else "", "Tiers": r.tiers})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, height=220)
else:
    st.info("Add one or more SKUs, then Optimize Layout, then Render.")

msgs: List[str] = []
if optimize_btn:
    if not st.session_state.requests:
        st.warning("No request lines to optimize.")
    else:
        matrix, msgs = optimize_layout(products, st.session_state.requests, int(max_tiers), int(turn_spot), int(required_turn_tiers))
        st.session_state.matrix = matrix

for m in msgs:
    st.warning(m)

if render_btn:
    if not st.session_state.requests:
        st.warning("No request lines to render.")
    else:
        render_loadxpert_top_side_pdf_style(
            view_title=view_title,
            created_by=created_by,
            created_at=created_at,
            order_number=order_number,
            vehicle_number=vehicle_number,
            po_number=po_number,
            matrix=st.session_state.matrix,
            products=products,
            turn_spot=int(turn_spot),
            airbag_gap_choice=airbag_gap_choice,
            airbag_gap_in=float(airbag_gap_in),
            cg_height_in=float(cg_height_in),
            whole_unit_equiv=float(whole_unit_equiv),
            total_lisa_units=float(total_lisa_units),
            code_colors=code_colors,
            flip_side=bool(flip_side),
            hatch_angle_deg=float(hatch_angle_deg),
            hatch_spacing_px=float(hatch_spacing_px),
            hatch_alpha=float(hatch_alpha),
            height_px=1000,
        )
else:
    st.caption("Click **Render PDF-style** to draw the LoadXpert-like Top + Side page.")
