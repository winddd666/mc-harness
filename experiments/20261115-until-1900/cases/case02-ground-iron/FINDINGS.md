# Ground-start comparison, terminal evidence

Run `20261115-154521-609789`, source revision `5408efbebec81ac7`, 305.55 seconds, 13 model requests. Requested 20 minutes but terminated early with a client ReadTimeout. No new run was started to conceal that failure.

- The fixture teleported an empty bot to inspected ground, without granting items. An idle death occurred after the teleport and before the task started; raw API event seq399/400 proves this. The task actually started at `[-161.3,71,-371.5]`. Nearby old-world drops (including dirt) were available, so this is not a clean-world bootstrap benchmark.
- Harvest completed at 62.75s, wooden pickaxe at 70.45s, cobblestone at 95.21s, stone pickaxe at 210.96s. The first raw craft failed because the old table was five blocks above the mining position. The model then spent several calls making and placing another table, despite the existing craft_gear macro handling that dependency.
- Mining reported 8 blocks dug but only 7 cobblestone gained. This illustrates that block count is not a material count, even after improving the standalone collector.
- Authoritative API events show **3 deaths and 19 hurt events** within the run. The normal tool-text summary saw only 2 deaths/11 hurt notifications because a fatal tool error skipped the last event drain. A terminal-event regression now covers that gap.
- After deaths, telemetry and later direct inventory retained the stone pickaxe, wooden pickaxe and materials: keep_inventory was verified in behavior, not only via gamerule query. The server still claimed `dropped_items_at` in its death event, an assumption that no longer holds.
- At API seq444/time07:49:42 UTC the third death in 60s triggered the existing five-minute protection; seq445 disconnected the bot. That cooldown was respected. A pending place operation never returned, so the Python request timed out at 07:50:27.
- After cooldown, one reconnect with the same cached token succeeded. Status still showed `busy: place`; stop waited 5015ms and returned `released:false`. Saved evidence: `connection-diagnosis/1794729372.json`. The old request was not cleaned up by connection loss.
- The reconnect diagnostic also observed continued attacks while waiting for world readiness/stop. The bot was then disconnected for maintenance. These idle/diagnostic deaths are separate from task deaths and must not be added to the task benchmark.

Follow-up repairs prepared: expiring temporary avoidance (only explicit protected_region is permanent), partial/hurt success stops subsequent actions, mixed mining replies list allowed and blocked types, and final event capture on failure. Full Python tests: 70 passed. API action lifetime overlay: 7 Node tests passed, including the patched dispatcher. Deployment is tracked separately in backend-maintenance.json.

Outstanding: autonomous canopy descent, continuous survival during generation and idle time, actual per-material goal completion in harvest/mining, consistent death/keep-inventory semantics, and a genuinely longer trajectory that survives across progression phases.
