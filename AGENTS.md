# entity — agent instructions

## Dual workflow (Gajae-Code + Cursor)

```text
gjc deep-interview  →  gjc ralplan  →  Cursor (local or Cloud) implements
```

1. **Clarify** vague ideas with Gajae-Code (`gjc`), skill `deep-interview`.
2. **Plan** with `ralplan` before mutating product code.
3. **Implement** in Cursor Agent or [Cloud Agents](https://cursor.com/agents) on `seoulsoundstation/entity`.

### Launch GJC (Windows)

```powershell
cd c:\entity
.\scripts\start-gjc.ps1
```

Inside gjc: `/skill:deep-interview` then `/skill:ralplan`.

If `gjc` has no model API keys, run the same interview in Cursor:

> 나는 비개발자야. deep-interview처럼 질문 하나만 해서 요구사항을 구체화해줘. 코드는 아직 쓰지 마.

### Cursor Cloud specific instructions

- Repo remote: `https://github.com/seoulsoundstation/entity`
- Prefer committing interview/plan markdown under `.gjc/_session-entity/specs/` before Cloud handoff.
- **Move to Cloud** does not transfer uncommitted local files — commit/push first.
- Phase 0 scope: workflow docs/scripts only. Do not invent product features until a product deep-interview is approved.

## Active session artifacts

- Deep interview: `.gjc/_session-entity/specs/deep-interview-entity-workflow.md`
- Ralplan: `.gjc/_session-entity/specs/ralplan-entity-workflow.md`
- Handoff notes: `docs/cursor-handoff.md`
