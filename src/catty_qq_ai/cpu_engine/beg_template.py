"""L0_beg 撒娇求充值模板池 (S3.3).

当用户进入强互动场景但积分不够时, 不试图回答, 改用米雪儿语气拒绝/撒娇.
模板按 (balance_range, affection_range) 双维度分桶, 多桶同时命中按 weight 加权随机选.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

try:
    import yaml  # type: ignore

    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False
    yaml = None


@dataclass(slots=True)
class BegTemplateGroup:
    name: str
    balance_min: int
    balance_max: int
    affection_min: int
    affection_max: int
    templates: list[str]
    weight: float = 1.0


class BegTemplatePool:
    """L0_beg 模板池."""

    def __init__(self, groups: list[BegTemplateGroup]) -> None:
        self._groups = list(groups)
        logger.info(
            f"[cpu_engine.L0_beg] loaded {len(self._groups)} groups, "
            f"total_templates={sum(len(g.templates) for g in self._groups)}"
        )

    @property
    def size(self) -> int:
        return len(self._groups)

    def pick(self, *, balance: int, affection_level: int) -> str | None:
        candidates: list[BegTemplateGroup] = []
        for group in self._groups:
            if not (group.balance_min <= balance <= group.balance_max):
                continue
            if not (group.affection_min <= affection_level <= group.affection_max):
                continue
            if not group.templates:
                continue
            candidates.append(group)
        if not candidates:
            return None
        weights = [max(g.weight, 0.0001) for g in candidates]
        winner = random.choices(candidates, weights=weights, k=1)[0]
        return random.choice(winner.templates)


def load_pool_from_dir(beg_dir: str | Path) -> BegTemplatePool:
    """加载 data/cpu_engine/beg/*.yaml. 不存在或解析失败返回空 pool."""
    beg_dir = Path(beg_dir)
    if not beg_dir.exists():
        logger.warning(f"[cpu_engine.L0_beg] beg_dir not found: {beg_dir}")
        return BegTemplatePool([])
    if not _HAS_YAML:
        logger.warning("[cpu_engine.L0_beg] PyYAML not installed")
        return BegTemplatePool([])

    groups: list[BegTemplateGroup] = []
    for yaml_path in sorted(beg_dir.glob("*.yaml")):
        try:
            with yaml_path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[cpu_engine.L0_beg] failed to load {yaml_path}: {exc}")
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            group = _parse_entry(entry, source=yaml_path.name)
            if group is not None:
                groups.append(group)

    return BegTemplatePool(groups)


def _parse_entry(entry: dict[str, Any], source: str) -> BegTemplateGroup | None:
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    templates = entry.get("templates") or []
    if not name or not isinstance(name, str) or not templates:
        return None
    try:
        return BegTemplateGroup(
            name=str(name),
            balance_min=int(entry.get("balance_min", 0)),
            balance_max=int(entry.get("balance_max", 99999)),
            affection_min=int(entry.get("affection_min", 0)),
            affection_max=int(entry.get("affection_max", 99)),
            templates=[str(t) for t in templates if str(t).strip()],
            weight=float(entry.get("weight", 1.0)),
        )
    except (TypeError, ValueError) as exc:
        logger.warning(f"[cpu_engine.L0_beg] {source}/{name}: parse error {exc}")
        return None
