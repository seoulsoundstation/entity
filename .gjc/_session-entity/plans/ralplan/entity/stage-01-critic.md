# Ralplan — entity dual workflow (deliberate)

**Source:** deep-interview `entity-workflow`  
**Mode:** deliberate  
**Execution target:** Cursor Agent (this session) + optional Cloud Agents  

## Planner summary

Implement Phase-0 workflow scaffolding only:

1. Document dual Gajae-Code + Cursor workflow in `AGENTS.md` and `README.md`.
2. Provide `scripts/start-gjc.ps1` for native Windows launch (no tmux).
3. Keep deep-interview / ralplan artifacts under `.gjc/_session-entity/`.
4. Add `.gitignore` entries that preserve intentional GJC specs while ignoring volatile caches if needed.
5. Add `docs/cursor-handoff.md` so Cloud/local Cursor can continue without re-deriving context.
6. Do **not** invent product features (`constraint:no-product-invention`).

## Acceptance checks

- `gjc --version` and `gjc --smoke-test` succeed when Bun bin is on PATH.
- `.\scripts\start-gjc.ps1` documents / launches gjc (or prints install help).
- Spec paths exist:
  - `.gjc/_session-entity/specs/deep-interview-entity-workflow.md`
  - `.gjc/_session-entity/specs/ralplan-entity-workflow.md`
- README explains: interview in gjc → plan → implement in Cursor/Cloud.
- Next product interview question is written for the user.

## Critic notes

- Risk: user may expect product code — mitigate with explicit Phase-0 boundary in README.
- Risk: gjc has no API keys — document Cursor fallback for interview.
- Risk: committing large `.gjc` state — commit specs; ignore lock/tmp if present.

## Explicit approval for Cursor execution

Approved to mutate repo files for Phase-0 scaffolding listed above. No application runtime code.
