# Cursor handoff — entity Phase 0

## What is done

- Bun + `gajae-code` (`gjc` 0.12.8) installed; smoke-test ok.
- GJC defaults skills installed.
- Deep-interview session `entity` seeded, Round 0 intent locked, final spec written.
- Ralplan deliberate planner/critic stage-01 written.
- Scripts: `scripts/start-gjc.ps1`, `scripts/seed-round0-intent.ts` (one-time Round 0 helper).

## What to do next (product)

Ask the user (one question):

> 만들고 싶은 결과물을 한 문장으로 말하면, **누가** 쓰고 **어떤 문제**를 해결하나요?

Then continue `/skill:deep-interview` (or Cursor interview) for the real product — do not invent features.

## How to continue on another machine

1. Same Cursor account → [cursor.com/agents](https://cursor.com/agents) for Cloud sessions.
2. `git clone https://github.com/seoulsoundstation/entity.git` (or pull).
3. Install Bun + `bun install -g gajae-code` if using gjc locally.
4. `.\scripts\start-gjc.ps1`

## Provider auth for gjc TUI

`gjc --list-models` currently needs an API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CURSOR_ACCESS_TOKEN`, etc.). Until set, use Cursor Agent for interview/planning and gjc CLI for state/specs.
