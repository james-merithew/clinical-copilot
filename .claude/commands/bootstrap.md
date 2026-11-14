---
description: Walk a new project (or a major new phase) through durable planning docs, one phase at a time with review gates.
---

You are bootstrapping durable planning memory for this repo. The goal is that
every future session and every sub-agent starts warm from on-disk docs instead
of re-explaining the project in chat.

Given the project idea, stack, and requirements the user provides below, walk
these phases **one at a time**. After each phase, write/update the matching doc
and **pause for the user's review before continuing** — do not run ahead.

1. **PRD** (`docs/PRD.md`) — what/why, target user, core capabilities,
   non-negotiable invariants, definition of done, explicit out-of-scope.
2. **Tech spec** (`docs/TECH_SPEC.md`) — stack, system shape, the typed
   contracts that are the source of truth, key modules, the eval gate that
   defines "landed," testing tiers.
3. **Skills & rules** — update `CLAUDE.md` with standing rules; note any custom
   skills/commands the workflow needs.
4. **Build loop** — decide the execution model (e.g. project-management agent →
   Plan agent per ticket → build agents in worktrees → serialized merge with a
   gate re-run after each). Record it in TECH_SPEC.
5. **Plan review** — read the three docs back, confirm they are internally
   consistent and that CLAUDE.md auto-loads them, and list the first tickets.

Rules for the whole run:
- Keep the docs concise and current — they are read every session, so bloat is
  a tax. Link between them rather than duplicating.
- Seed docs with the project's real content, not empty templates.
- Ensure `CLAUDE.md` ends with the instruction to read the three docs before any
  work and to append non-obvious decisions to `docs/DECISIONS.md`.
- If the docs already exist, treat this as a refresh: reconcile, don't clobber.

Project idea / stack / requirements:
$ARGUMENTS
