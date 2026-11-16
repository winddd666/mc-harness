# Longer underground trajectory — 16m09s, still no iron

Run `20261115-161125-277956`, NJU/default, source `1e67662e3ad7dfdd`, requested 1500 seconds/100 model requests. It terminated after 969.36 seconds/62 requests because the normal death-loop protection activated. No observer restart or human steer during the run.

Authoritative API events agree with the now-complete terminal notes: 5 deaths, 41 hurt events (13 drown, 1 player_explosion, 27 mob_attack). Completed model requests consumed 819.61s, 84.55% of wall time. No raw iron, iron ingot, furnace or smelting progress was achieved. The latest connected inventory retained the stone pickaxe and 85 cobblestone; the final disconnected telemetry's zero items means unavailable observation, not verified inventory loss.

The start was assisted: 27 air blocks carved inside a verified natural stone cube, teleport to the room, no item grant. Original terrain is saved. The agent left that room while attempting mining and wood recovery, so the whole run must not be described as a protected-room survival benchmark.

## Failure chain

- 44.89s placed the carried crafting table. It then attempted planks without any logs, despite the initial inventory. Fuel planning fixated on obtaining more wood instead of coal/dropped tools as alternatives.
- 97.40s completed five downward steps; 137.47s stopped at a cave/drop after eight more steps. Distant iron and coal were visible but there was no traversable path. The API forbids automatic pathfinder digging; the skill set still lacked a level tunnel macro to connect the bot to known underground targets.
- 256.62s an upward staircase reported reached_surface at Y62. Later scans found nearby trees around Y71. The stop condition accepted any nearby above-level surface clue, and intermediate control results could be captured at a jump apex. That false arrival fed a long loop of horizontal move success at the wrong height, unstick, occupied placements and hand-built steps.
- 654.94s a dig was interrupted by drowning damage. The next model wait continued while oxygen/health fell, ending in drowning death. This is a direct test case for generation-time floating and cancellation.
- After respawn, a creeper-like explosion and subsequent mobs caused more damage. The model's stabilize call returned ok:true with HP7.34/actions[], then it kept gathering/crafting rather than achieving a protected state.
- Standalone collector correctly reported no_drops_collected at 780.78s. After a different approach it verified birch_log +1 at 812.51s. This confirms both truthful failure and actual successful pickup; it does not prove every canopy geometry is solved.
- 862.63s attempted iron_pickaxe with no iron ingots, then died in another model wait. Later tried raising a shield it did not possess. Material dependencies remain largely model-managed and unreliable.
- Three fast mob deaths at the end triggered the unchanged five-minute protection. Unlike the earlier stuck place case, the patched API and harness surfaced death_loop_cooldown and ended the run; no lingering API action lock had to be cleared.

## Prepared follow-ups

81 Python tests now pass. An opt-in generation guard polls actual status/events while the model is in flight, performs bounded shield/float/eat or carefully preflighted bunker attempts, and cancels stale generation before accepting more tools. It never claims general safety and is not yet a guardian during tools or idle time. Controlled live validation is pending.

Staircase counters now use a settled status, including fallback movement; jump-apex results cannot count a step. Reached-surface needs actual surface ground or matching-level ground plus a clear vertical column. Partial stops require replanning. Three regressions reproduce false arrival and false up/down movement.

Hot spawn is an additional controller/backend issue: during maintenance recovery, attacks can start before API ready becomes true. Those recovery deaths are saved separately and excluded from the five task deaths. The next isolated test prepares its stone room with RCON while the bot is offline, then places the bot there immediately after cooldown, before waiting for API readiness.
