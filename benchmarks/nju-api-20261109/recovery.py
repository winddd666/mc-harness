#!/usr/bin/env python3
"""Re-run the two episodes interrupted by the original server rate limit."""
import benchmark as b
b.run_agent("impossible")
b.run_agent("untrusted_sign")

