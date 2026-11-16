"""虚空岛圆形电子围栏：防止 Bot 偏离竞赛岛屿或踩空坠落虚空（依据 RULES.md 禁止离岛）。"""

from __future__ import annotations

import dataclasses
import math
import os
import pathlib
from typing import Any, Mapping

from ..config import parse_dotenv

_DIRS_XZ: dict[str, tuple[int, int]] = {
    "north": (0, -1),
    "south": (0, 1),
    "east": (1, 0),
    "west": (-1, 0),
}


@dataclasses.dataclass(frozen=True, slots=True)
class VoidGeofence:
    """可配置的虚空岛水平圆形安全边界（radius <= 0 表示未启用）。"""

    center_x: float = 0.0
    center_z: float = 0.0
    radius: float = 0.0
    margin: float = 5.0

    @property
    def enabled(self) -> bool:
        return math.isfinite(self.radius) and self.radius > 0.0

    @property
    def safe_radius(self) -> float:
        if not self.enabled:
            return 0.0
        return max(1.0, float(self.radius) - max(0.0, float(self.margin)))

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        dotenv_path: str | pathlib.Path | None = None,
    ) -> VoidGeofence:
        merged: dict[str, str] = {}
        default_env = pathlib.Path(".env")
        if default_env.is_file():
            merged.update(parse_dotenv(default_env))
        if dotenv_path:
            dp = pathlib.Path(dotenv_path)
            if dp.is_file():
                merged.update(parse_dotenv(dp))
        source_env = os.environ if environ is None else environ
        for k, v in source_env.items():
            if v != "":
                merged[k] = v

        def _num(name: str, default: float) -> float:
            raw = str(merged.get(name, "")).strip()
            if not raw:
                return default
            try:
                val = float(raw)
                return val if math.isfinite(val) else default
            except ValueError:
                return default

        return cls(
            center_x=_num("MC_ISLAND_CENTER_X", 0.0),
            center_z=_num("MC_ISLAND_CENTER_Z", 0.0),
            radius=_num("MC_ISLAND_RADIUS", 0.0),
            margin=_num("MC_ISLAND_MARGIN", 5.0),
        )

    def distance_from_center(self, x: float, z: float) -> float:
        return math.hypot(float(x) - float(self.center_x), float(z) - float(self.center_z))

    def margin_remaining(self, x: float, z: float) -> float:
        if not self.enabled:
            return float("inf")
        return self.safe_radius - self.distance_from_center(x, z)

    def check_point(self, x: float, z: float, *, action: str = "action") -> dict[str, Any] | None:
        """若启用且坐标越出 safe_radius，返回结构化错误字典；否则返回 None。"""
        if not self.enabled:
            return None
        dist = self.distance_from_center(x, z)
        safe = self.safe_radius
        if dist <= safe:
            return None
        return {
            "ok": False,
            "error": {
                "code": "void_geofence_blocked",
                "action": action,
                "target_xz": [round(float(x), 1), round(float(z), 1)],
                "distance": round(dist, 1),
                "safe_radius": round(safe, 1),
                "island_radius": round(float(self.radius), 1),
                "center": [self.center_x, self.center_z],
                "message": (
                    f"目标坐标({round(float(x), 1)}, {round(float(z), 1)})距岛中心 {round(dist, 1)}m，"
                    f"逼近或超出虚空岛安全边界 {round(safe, 1)}m（RULES.md 禁止离岛且跌落虚空即死），请向岛中心方向行动。"
                ),
            },
        }

    def check_ray(
        self,
        start_x: float,
        start_z: float,
        direction: str,
        steps: int,
        *,
        action: str = "tunnel",
    ) -> dict[str, Any] | None:
        """预检沿 cardinal 方向推进 steps 格后的终点是否越界。"""
        if not self.enabled:
            return None
        d_key = str(direction).strip().lower()
        dx, dz = _DIRS_XZ.get(d_key, (0, 0))
        step_n = max(1, int(steps))
        end_x = math.floor(float(start_x)) + 0.5 + dx * step_n
        end_z = math.floor(float(start_z)) + 0.5 + dz * step_n
        return self.check_point(end_x, end_z, action=action)
