# entity

Greenfield project developed with **Gajae-Code (`gjc`) + Cursor**.

> Phase 0 = workflow only. The product idea is still open — we interview before inventing features.

## Quick start

### Cursor

1. Open this folder in Cursor.
2. Read [AGENTS.md](AGENTS.md) and [docs/cursor-handoff.md](docs/cursor-handoff.md).
3. Use Agent (local) or [Cloud Agents](https://cursor.com/agents) on this GitHub repo.

### Gajae-Code (interview / plan)

```powershell
# once per machine
powershell -c "irm bun.sh/install.ps1|iex"
# restart terminal, then:
bun install -g gajae-code
gjc --version
gjc --smoke-test

cd c:\entity
.\scripts\start-gjc.ps1
```

In the gjc session:

```text
/skill:deep-interview
/skill:ralplan
```

Then implement the approved plan in Cursor.

## Workflow

```text
deep-interview → ralplan → Cursor implement (local or Cloud)
```

| Step | Tool | Purpose |
|------|------|---------|
| Clarify | `gjc` deep-interview | Turn vague ideas into a spec |
| Plan | `gjc` ralplan | Review before code changes |
| Build | Cursor / Cloud Agents | Edit code, PRs, cross-device sessions |

## Current session

- Spec: `.gjc/_session-entity/specs/deep-interview-entity-workflow.md`
- Plan: `.gjc/_session-entity/specs/ralplan-entity-workflow.md`

## Next question for you

만들고 싶은 결과물을 한 문장으로 말하면, **누가** 쓰고 **어떤 문제**를 해결하나요?

## Repo

https://github.com/seoulsoundstation/entity
