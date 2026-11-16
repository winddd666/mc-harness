# Exploration session — Beijing deadline 19:00

Active goal: explore existing harness weaknesses through longer NJU trajectories, varied recovery scenarios, and evidence-backed skill improvements. Do not finish the goal before the deadline. Keep raw logs and label assisted fixtures separately from autonomous runs.

## Common setup

- Server: nju-beast Leaves 1.21.11; API source snapshot v1.3.9 saved locally for read-only analysis.
- `gamerule keep_inventory true` enabled and queried successfully (initial value false). Monsters, damage and day/night remain enabled.
- No fresh authentication: all clients reuse the cached BeastBot_01 token.
- Restart only idle user services through `scripts/mc_services.sh`; never occupy production ports with ad hoc servers.
- Version each run using `/api/state.runtime.source_revision` and `run.started.runtime`; the experiment driver refuses a service/source mismatch.
- Task timeout can now be overridden per request, capped at 3600 seconds. Deadline-aware driver never starts a task with less than one minute remaining.
- Tests before first deployment: 52 passed, including a real per-run timeout regression.

## Intended experiments (adapt using live evidence)

1. Long iron-pickaxe chain with keep-inventory, conservative threat hints, unchanged existing macro skills. 20 minutes / 70 requests. Verify retained inventory through at least one natural respawn if it occurs. Observe cold-start planning, safety, wood/stone acquisition, placement, furnace and ore selection.
2. Isolated bootstrap (logs → wooden pickaxe → 3 cobblestone → stone pickaxe), with explicit checkpoints. Diagnose reachability, automatic equip, dig confirmation and collection; implement a small macro only after a concrete recurring failure.
3. Craft/place/smelt in restricted space and recover after interruption. Label any RCON-supplied tools/materials or teleports as assisted fixtures.
4. Water/pit escape and navigation interruption, bounded and with keep-inventory confirmed.
5. Live steering, user-stop, task timeout, model wait while hurt; verify process/task liveness before restarting anything.
6. Longer composite survival/progression run with the tested fixes, comparing goal progress, repeated error signatures, death rate and latency against the baseline.

## Intervention and evidence

- Each case writes request, before/after state, version, five-second telemetry and tool summary under `cases/` and references authoritative `runs/*.events.jsonl` / companion report.
- Five identical consecutive tool failures cause the observer to request stop for its own active task only. Server death-loop cooldown stays enabled and is not bypassed.
- Observe the same live process handle after a polling timeout; a transient observation failure is not a task termination.
- Before changing any running service, confirm its task is idle or explicitly stop and wait for terminal state.
- At deadline, stop any experiment-owned task, verify termination, compile findings and pending weaknesses. Keep the requested inventory-preserving rule enabled and report its final value.
