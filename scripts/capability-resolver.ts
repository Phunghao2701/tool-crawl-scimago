#!/usr/bin/env bun
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

const sourceRoot =
  process.env.REPO_HARNESS_SOURCE_ROOT ||
  process.env.AGENTIC_DEV_ROOT ||
  process.env.AGENTIC_DEV_SKILL_ROOT;
const command = sourceRoot && existsSync(join(sourceRoot, "src", "cli", "index.ts"))
  ? ["bun", join(sourceRoot, "src", "cli", "index.ts"), "run", "capability-resolver"]
  : ["repo-harness", "run", "capability-resolver"];

const result = spawnSync(command[0], [...command.slice(1), ...process.argv.slice(2)], {
  cwd: process.cwd(),
  env: process.env,
  stdio: "inherit",
});

if (result.error) {
  console.error(`Missing repo-harness CLI for helper capability-resolver: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status ?? 1);
