"""Deterministic L0/L1 skill stack and 1.21.11 advancement frontier planner."""

from .advancements import compute_advancement_frontier, load_advancements_catalog
from .combat_env import CombatControllerSlot, CombatEnv, SubcorticalInstinctController
from .ensure import PhysicalPrerequisiteResolver, effective_item_count, highest_pickaxe_tier, total_owned_counts
from .inventory_manager import InventoryManager, ProtectionStack, is_important_item, is_safe_fuel, plan_garbage_drops
from .library import MacroSkills
from .mining_dfs import MiningDFSEngine, WorldLedger, normalize_dfs_strategy
from .movement import BreadcrumbTrail, TerrainNavigator
from .safety import SafetyEmergency, check_and_stabilize

__all__ = [
    "BreadcrumbTrail",
    "CombatControllerSlot",
    "CombatEnv",
    "InventoryManager",
    "MacroSkills",
    "MiningDFSEngine",
    "PhysicalPrerequisiteResolver",
    "ProtectionStack",
    "SafetyEmergency",
    "SubcorticalInstinctController",
    "TerrainNavigator",
    "WorldLedger",
    "check_and_stabilize",
    "compute_advancement_frontier",
    "effective_item_count",
    "highest_pickaxe_tier",
    "is_important_item",
    "is_safe_fuel",
    "load_advancements_catalog",
    "normalize_dfs_strategy",
    "plan_garbage_drops",
    "total_owned_counts",
]
