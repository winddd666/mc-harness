# Beast API action lifetime repair

Observed on v1.3.9 in `20261115-154521-609789`: death-loop protection disconnected the bot while `place` was pending. Its HTTP request never completed, the Python client timed out after 45 seconds, and after reconnect the session still reported `busy: place`. `stop` waited five seconds and returned `released:false`.

The overlay gives each HTTP action an owned lease. Connection end rejects a pending request and releases the old lease. An old request's finally block cannot clear a newer lock; late business-code access through its scoped session fails instead of reaching the replacement connection. Stale end callbacks are ignored. A 30-second default action deadline returns a structured failure; it quarantines the connection until disconnect because an uncooperative library promise cannot safely share the socket with a new action.

Build with `python3 server_patches/action-lifecycle/build.py`. The original snapshot is immutable; generated files and before/after hashes are in `experiments/20261115-until-1900/backend_patched/`. Run `node --test server_patches/action-lifecycle/*.test.cjs`. The integration test executes the actual patched HTTP dispatcher with a fake Mineflayer session, including a never-resolving action, connection replacement and timeout quarantine.

Deployment must verify original remote hashes, back up source, validate syntax and restart the API only while all cached-token consumers are stopped. Since API sessions are in memory, a restart requires one controlled login and synchronized token caches before viewers/controllers resume. No `mc-bot login` is permitted while mc-live or mc-console is running. Preserve 0600 on both caches and credential files; never print the login response. Honor the saved death-loop cooldown before maintenance recovery. Do not restart Leaves, disable mobs/damage or weaken death-loop protection.

The lease cannot cancel code inside third-party promises. Quarantine and socket teardown are therefore required before reusing a timed-out connection. This patch does not implement a survival guardian.
