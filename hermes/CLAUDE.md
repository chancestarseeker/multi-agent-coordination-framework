# Coordination Foundations

When working within this project, load the coordination skill at `SKILL.md`
in this directory.

The coordination daemon runs at `http://localhost:8420`. Always read the ledger summary (`/summary`) at session start. Write to the ledger (`/append`) frequently — after meaningful progress, before risky operations.

Route participants by capability via `/select?capability=X`. Do not rely on static fallback chains.

The foundations documents in `../foundations/` (one level up — shared
with the cli/ implementation) define the architecture. Load them on the
conditions specified in the Module Index (fnd-preamble.md).
