"""Small-model 1.21.11 Minecraft + Agent API knowledge base."""

from .data_1_21_11 import (
    ADVANCEMENT_PLAYBOOKS,
    ITEM_ALIASES,
    MOBS_AND_MECHANICS,
    ORES_1_21,
    RECIPES_1_21,
)
from .ecology import WORLD_ECOLOGY_RULES, assess_world_target, normalize_dimension
from .lookup import build_jit_error_hint, lookup_kb, resolve_item_alias

__all__ = [
    "ADVANCEMENT_PLAYBOOKS",
    "ITEM_ALIASES",
    "MOBS_AND_MECHANICS",
    "ORES_1_21",
    "RECIPES_1_21",
    "WORLD_ECOLOGY_RULES",
    "assess_world_target",
    "build_jit_error_hint",
    "lookup_kb",
    "normalize_dimension",
    "resolve_item_alias",
]
