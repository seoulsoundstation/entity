/**
 * Record Round 0 intent contract for the entity deep-interview session
 * via the sanctioned GJC recorder API (not by hand-editing .gjc state).
 */
import { homedir } from "node:os";
import { join, resolve } from "node:path";

const recorderPath = join(
	homedir(),
	".bun/install/global/node_modules/@gajae-code/coding-agent/src/gjc-runtime/deep-interview-recorder.ts",
);
const { appendOrMergeDeepInterviewRound } = await import(recorderPath);

const cwd = resolve(import.meta.dirname, "..");
const sessionId = "entity";
const statePath = resolve(cwd, ".gjc/_session-entity/state/deep-interview-state.json");

const confirmationOption = "Approve workflow-only topology";
const items = [
	{
		id: "artifact:workflow-docs",
		category: "artifact" as const,
		statement: "Ship AGENTS.md, README workflow section, and GJC start script for dual Cursor+gjc use.",
	},
	{
		id: "artifact:interview-handoff",
		category: "artifact" as const,
		statement: "Persist deep-interview + ralplan handoff artifacts under .gjc/_session-entity/.",
	},
	{
		id: "surface:local-cli",
		category: "surface" as const,
		statement: "Operator launches gjc from PowerShell via scripts/start-gjc.ps1 without tmux.",
	},
	{
		id: "surface:cursor-cloud",
		category: "surface" as const,
		statement: "Implementation continues in Cursor local Agent or Cloud Agents on the same GitHub repo.",
	},
	{
		id: "integration:gajae-code",
		category: "integration" as const,
		statement: "Use globally installed gajae-code (gjc) beside Cursor; not as a Cursor plugin.",
	},
	{
		id: "constraint:no-product-invention",
		category: "constraint" as const,
		statement: "Do not invent the entity product; product discovery is a later deep-interview.",
	},
];

const result = await appendOrMergeDeepInterviewRound(
	cwd,
	statePath,
	{
		interviewId: sessionId,
		round: 0,
		round_id: "round0-topology",
		questionId: "topology-confirm",
		questionText:
			"Round 0: Confirm Phase-0 topology is workflow scaffolding only (docs, scripts, GJC session) with no product feature code.",
		component: "review-topology",
		dimension: "topology",
		ambiguity: 0.35,
		selectedOptions: [confirmationOption],
		intent_contract: {
			items,
			confirmation_options: [confirmationOption, "Reject / revise topology"],
		},
	},
	{ sessionId },
);

console.log(JSON.stringify({ ok: true, action: result.action, round_key: result.record.round_key }, null, 2));
