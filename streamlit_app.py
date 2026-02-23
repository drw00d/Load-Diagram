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

// Load Xpert-ish constants
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

  // Top strip geometry
  const topX = M+80, topY = M+140;
  const topW = W-2*M-160, topH = 140;

  ctx.font = "700 18px Helvetica, Arial, sans-serif";
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
  for (const c of DATA.cells) {
    if (!rep.has(c.spot)) rep.set(c.spot, c.pid);
  }

  const order = [];
  for (let s=1;s<=spots;s++) order.push(s);
  if (DATA.flip_side) order.reverse();

  for (let i=0;i<order.length;i++) {
    const spot = order[i];
    const pid = rep.get(spot) || null;
    const x = topX + i*cellW;

    // Fill by A/B/C if any block exists in this spot
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

    // Turn hatch region
    const tz = topX + (DATA.meta.turn_spot - 1)*cellW;
    drawHatch(ctx, tz, topY, cellW*2, topH, DATA.hatch.angle_deg, DATA.hatch.spacing_px, DATA.hatch.alpha, "#000");
    ctx.strokeStyle = "#111";
    ctx.lineWidth = 1;
    ctx.strokeRect(tz, topY, cellW*2, topH);
  }

  // Side strip (simple)
  const sideX = M+80, sideY = topY+topH+60;
  const sideW = W-2*M-160, sideH = 260;

  ctx.textAlign = "center";
  ctx.font = "700 18px Helvetica, Arial, sans-serif";
  ctx.fillStyle = "#111";
  ctx.fillText(DATA.flip_side ? "Side2" : "Side1", W/2, sideY-10);

  ctx.strokeStyle = LX_BLUE;
  ctx.lineWidth = 3;
  ctx.strokeRect(sideX, sideY, sideW, sideH);

  const inset = 10;
  const lx = sideX+inset, ly = sideY+inset;
  const lw = sideW-2*inset, lh = sideH-2*inset-35;

  const cw = lw / spots;
  const tiers = DATA.meta.tiers;
  const ch = lh / tiers;

  // doorway cutout + airbag
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
