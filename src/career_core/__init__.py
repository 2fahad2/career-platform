"""career_core — pure, proven engine kernel.

Faithful port of the verified logic in docs/LEGACY_KNOWLEDGE.md (read-only
reference); approved deviations are recorded in docs/DEVIATIONS.md. Pure
functions only: no network, no LLM, no database. Tenant-specific values
(salary threshold, weights) are injected — never hardcoded (D4/D10).
"""

__version__ = "0.1.0"
