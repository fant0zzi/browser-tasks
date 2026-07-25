# CLAUDE.md

Read and follow `AGENTS.md`; it is the canonical workspace policy.

Do not create a task for a simple conversational answer. For durable work, use
one short, human-named workspace per user intent. Reuse it for continuations
and keep internal runs in `.task/state.sqlite`; publish only reusable material
under `deliverables/`.

Reasoning delegates may plan or review, but only the local agent may execute
authorized browser actions and verify their outcomes.
