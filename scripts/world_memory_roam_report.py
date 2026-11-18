#!/usr/bin/env python3
"""Summarize a world-memory roam run: trajectory extent + retained memory layers."""

from __future__ import annotations

import glob
import json
import math
import pathlib
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]


def main() -> int:
    paths = sorted(glob.glob(str(ROOT / "runs/world-memory-roam-*.json")))
    if len(sys.argv) > 1:
        path = pathlib.Path(sys.argv[1])
    else:
        path = pathlib.Path(paths[-1])
    data = json.loads(path.read_text(encoding="utf-8"))
    traj = data.get("trajectory") or []
    mem = data.get("memory") or {}
    start = data.get("start") or {}
    final = data.get("final_status") or {}
    p0 = (start.get("pos") or [0, 0, 0])[:3]
    p1 = (final.get("pos") or (traj[-1]["pos"] if traj else [0, 0, 0]))[:3]

    path_len = 0.0
    for a, b in zip(traj, traj[1:]):
        try:
            path_len += math.dist(a["pos"][:3], b["pos"][:3])
        except Exception:
            pass
    xs = [float(r["pos"][0]) for r in traj if r.get("pos")]
    zs = [float(r["pos"][2]) for r in traj if r.get("pos")]
    bbox = {
        "min_x": min(xs) if xs else None,
        "max_x": max(xs) if xs else None,
        "min_z": min(zs) if zs else None,
        "max_z": max(zs) if zs else None,
        "width_m": round(max(xs) - min(xs), 1) if xs else 0,
        "depth_m": round(max(zs) - min(zs), 1) if zs else 0,
    }
    displacement = round(math.dist(p0, p1), 1) if p0 and p1 else None
    chunks = mem.get("visited_chunks") or []
    unique_chunks = {(c[0], c[1], c[2]) for c in chunks if len(c) >= 3}
    poi_rows = mem.get("pois") or []
    poi_counts = Counter(p.get("name", "?") for p in poi_rows)
    habitats = mem.get("habitats") or {}
    lines = [
        "# WorldMemory 自由巡游摘要",
        "",
        f"- 报告：`{path.name}`",
        f"- Bot：`{data.get('bot')}`",
        f"- 状态：`{'ok' if data.get('ok') else 'failed'}`",
        f"- 步数：`{len(traj)}`",
        f"- 起点：`{p0}`",
        f"- 终点：`{p1}`",
        f"- 直线位移：`{displacement} m`",
        f"- 轨迹累计路程：`{round(path_len, 1)} m`",
        f"- 轨迹包围盒：`{bbox}`",
        f"- 独立区块数：`{len(unique_chunks)}`",
        "",
        "## 记忆留存",
        "",
        f"- 核心锚点：`{json.dumps({k: mem.get(k) for k in ('spawn_points','bed_spawn','home_bases','portals','last_death')}, ensure_ascii=False)}`",
        f"- 区块足迹：`{len(chunks)}` 条",
        f"- POI：`{len(poi_rows)}` 条",
        f"- POI 分类：`{dict(poi_counts)}`",
        f"- 容器快照：`{len(mem.get('containers') or [])}` 个",
        f"- 群系：`{mem.get('biomes')}`",
        f"- 栖息地：`{json.dumps(habitats, ensure_ascii=False)}`",
        "",
        "## 轨迹最后 10 步",
        "",
        "| step | t(s) | dir | pos | dist | chunks | pois | habitats |",
        "|---:|---:|---|---|---:|---:|---:|---:|",
    ]
    for r in traj[-10:]:
        lines.append(
            f"| {r.get('step')} | {r.get('t')} | {r.get('dir')} | {r.get('pos')} | "
            f"{r.get('dist')} | {r.get('chunks')} | {r.get('pois')} | {r.get('habitats')} |"
        )
    out = ROOT / "runs" / f"{path.stem}-summary.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"summary": str(out), "steps": len(traj), "chunks": len(unique_chunks), "pois": len(poi_rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
