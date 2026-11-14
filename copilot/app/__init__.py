"""Clinical Co-Pilot sidecar — a thin, read-only AI integration beside OpenEMR.

The LLM is an untrusted planner: it proposes tool calls only. Authorization is
deterministic (Authorization Gateway), data access is read-only and
patient-scoped, and every factual claim is bound to a real OpenEMR primary key
by the deterministic Verifier before any response reaches the user.
"""

__version__ = "0.1.0"
