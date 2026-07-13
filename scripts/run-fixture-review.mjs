#!/usr/bin/env node

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

function fail(message) {
  console.error(`error: ${message}`);
  process.exit(2);
}

const fixturePath = process.argv[2];
if (!fixturePath || process.argv.length !== 3) fail("Usage: run-fixture-review.mjs fixtures/name.json");

const fixtureRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../fixtures");
const absolutePath = resolve(fixturePath);
const relativePath = relative(fixtureRoot, absolutePath);
if (relativePath.startsWith("..") || relativePath.includes("..")) fail("fixture must remain under fixtures/");

let raw;
let fixture;
try {
  raw = readFileSync(absolutePath, "utf8");
  fixture = JSON.parse(raw);
} catch (error) {
  fail(`fixture could not be read: ${error.message}`);
}

const expected = fixture.expected;
if (!fixture.fixture_id || !expected?.verdict || !fixture.target) fail("fixture lacks required expected review fields");

const result = {
  schema_version: "review-receipt.v1",
  packet_sha256: createHash("sha256").update(raw).digest("hex"),
  target: fixture.target,
  reviewer: {
    identity: "hummbl-agent-offline-runner",
    runtime: "synthetic-fixture-v1",
    generated_at: new Date().toISOString()
  },
  result: {
    verdict: expected.verdict,
    finding_count: 1,
    summary: expected.finding
  },
  authority: {
    mode: "fixture",
    can_approve: false,
    can_merge: false,
    can_resolve_threads: false,
    limitations: ["synthetic fixture only", "no live GitHub calls", "no write capability"]
  }
};

process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
