# Deep Interview Spec — entity workflow (Phase 0)

**Session:** `entity`  
**Resolution:** quick (threshold 0.6)  
**Status:** approved for workflow scaffolding only  
**Language:** Korean-first user; specs bilingual where useful  

## Goal (one sentence)

비개발자 사용자가 Gajae-Code(`gjc`)로 요구를 구체화하고 Cursor(로컬/Cloud)로 구현하는 **병행 워크플로를 `c:\entity`에 바로 쓸 수 있게 만든다.**

## Locked topology

| Component | In scope now | Notes |
|-----------|--------------|-------|
| Tooling bootstrap | Yes | Bun, `gajae-code`/`gjc`, PATH, smoke-test |
| GJC session artifacts | Yes | `.gjc/_session-entity/`, deep-interview + ralplan handoff |
| Cursor dual-run docs | Yes | AGENTS.md, start scripts, Cloud handoff notes |
| Product feature code | **No** | Real product idea still unknown — next deep-interview |
| Secrets / production deploy | No | Deferred |

## Locked intent IDs (Round 0)

- `artifact:workflow-docs`
- `artifact:interview-handoff`
- `surface:local-cli`
- `surface:cursor-cloud`
- `integration:gajae-code`
- `constraint:no-product-invention`

## Established facts

1. Repo: `https://github.com/seoulsoundstation/entity` (public), local `c:\entity`.
2. Cursor Cloud Agents Environment already configured for this repo.
3. User has limited programming knowledge; wants interview-before-code.
4. Gajae-Code is external harness (not a Cursor plugin); run beside Cursor.
5. `gjc` installed: v0.12.8; smoke-test ok; default skills installed under `~/.gjc/agent`.
6. `gjc` has **no LLM API keys** on this machine yet → interactive LLM interview inside `gjc` TUI needs provider auth; Cursor Agent can run the same interview protocol until keys exist.

## Ambiguity scores (quick)

| Dimension | Score (0=clear, 1=vague) | Note |
|-----------|--------------------------|------|
| Workflow tooling | 0.15 | Clear from plan |
| Product domain / users | 0.95 | Unknown — do not invent |
| Stack / UI | 0.9 | Deferred until product interview |
| Acceptance for Phase 0 | 0.2 | Defined below |

**Weighted ambiguity ≈ 0.35** (below quick threshold 0.6 for *workflow-only* scope).

## Acceptance criteria (Phase 0)

- [x] Bun + `gajae-code` installed; `gjc --version` / `--smoke-test` pass
- [x] Default GJC skills installed (`deep-interview`, `ralplan`, `ultragoal`, `team`)
- [x] Session seeded: `gjc deep-interview --quick --session-id entity ...`
- [ ] Repo contains start script + AGENTS.md explaining dual workflow
- [ ] Ralplan artifact written and Cursor-ready handoff doc exists
- [ ] README updated for “how to continue interview / implement”

## Out of scope

- Inventing what the `entity` product is
- Shipping application features, auth, DB, UI
- Forcing `gjc --tmux` on native Windows

## Next interview (product)

After Phase 0 lands, run:

```powershell
cd c:\entity
$env:Path = "$env:USERPROFILE\.bun\bin;" + $env:Path
gjc
# then: /skill:deep-interview
```

Or in Cursor: “deep-interview처럼 질문 하나만 해서 제품 아이디어를 구체화해줘. 코드는 쓰지 마.”

**First product question (weakest dimension: users/outcome):**  
만들고 싶은 결과물을 한 문장으로 말하면, **누가** 쓰고 **어떤 문제**를 해결하나요?

## Explicit execution approval

Approve execution of **workflow scaffolding + docs only** (no product feature code). Handoff → `ralplan`.
