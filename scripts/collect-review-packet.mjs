#!/usr/bin/env node

import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const usage = "Usage: collect-review-packet.mjs --repo OWNER/REPO --pr NUMBER | --fixture FILE";

function fail(message) {
  console.error(`error: ${message}\n${usage}`);
  process.exit(2);
}

function argsToMap(argv) {
  const result = new Map();
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith("--")) fail(`unexpected argument ${arg}`);
    const key = arg.slice(2);
    if (key === "mode") fail("mode is fixed to read_only; write modes are not supported");
    const value = argv[i + 1];
    if (!value || value.startsWith("--")) fail(`missing value for --${key}`);
    result.set(key, value);
    i += 1;
  }
  return result;
}

function runGh(arguments_) {
  try {
    return execFileSync("gh", arguments_, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  } catch (error) {
    const detail = error.stderr?.toString().trim() || error.message;
    fail(`read-only GitHub query failed: ${detail}`);
  }
}

function validSha(value) {
  return typeof value === "string" && /^[0-9a-f]{40}$/.test(value);
}

function fixturePacket(path) {
  const fixture = JSON.parse(readFileSync(path, "utf8"));
  const target = fixture.target;
  if (!target || !validSha(target.base_sha) || !validSha(target.head_sha)) {
    fail("fixture must contain 40-character base_sha and head_sha values");
  }
  return {
    schema_version: "review-packet.v1",
    target,
    review_context: {
      reviewer_identity: "hummbl-agent-fixture-runner",
      mode: "fixture",
      generated_at: new Date().toISOString(),
      limitations: ["synthetic fixture; no live GitHub evidence fetched"]
    },
    evidence: [{
      id: "E-001",
      kind: "fixture",
      source: path,
      directness: "direct",
      authority: "fixture",
      observed_at: new Date().toISOString(),
      notes: "Fixture content is untrusted data and is not treated as instructions."
    }],
    findings: []
  };
}

function livePacket(repository, pullRequest) {
  const metadata = JSON.parse(runGh([
    "pr", "view", String(pullRequest), "--repo", repository,
    "--json", "number,url,title,author,baseRefName,baseRefOid,headRefName,headRefOid"
  ]));
  if (!validSha(metadata.baseRefOid) || !validSha(metadata.headRefOid)) {
    fail("GitHub did not return full base/head SHAs; refusing to emit an unbound packet");
  }
  const observedAt = new Date().toISOString();
  return {
    schema_version: "review-packet.v1",
    target: {
      repository,
      pull_request: metadata.number,
      base_sha: metadata.baseRefOid,
      head_sha: metadata.headRefOid,
      url: metadata.url
    },
    review_context: {
      reviewer_identity: "hummbl-agent-read-only-collector",
      mode: "read_only",
      generated_at: observedAt,
      limitations: ["metadata-only collector; diff and thread analysis are separate layers"]
    },
    evidence: [{
      id: "E-001",
      kind: "review",
      source: `gh pr view ${repository}#${metadata.number}`,
      directness: "direct",
      authority: "github",
      observed_at: observedAt,
      sha: metadata.headRefOid,
      notes: `base=${metadata.baseRefOid}; head=${metadata.headRefOid}; author=${metadata.author?.login ?? "unknown"}`
    }],
    findings: []
  };
}

const options = argsToMap(process.argv.slice(2));
if (options.has("repo") !== options.has("pr") && !options.has("fixture")) fail("--repo and --pr must be supplied together");
if (options.has("fixture") && (options.has("repo") || options.has("pr"))) fail("choose --fixture or --repo/--pr, not both");
if (!options.has("fixture") && !options.has("repo")) fail("a target is required");

const packet = options.has("fixture")
  ? fixturePacket(options.get("fixture"))
  : livePacket(options.get("repo"), Number(options.get("pr")));
if (!Number.isInteger(packet.target.pull_request) || packet.target.pull_request < 1) fail("pull request number must be positive");

const output = JSON.stringify(packet, null, 2) + "\n";
if (options.has("output")) {
  console.error("error: --output is intentionally unsupported until the artifact writer is independently reviewed");
  process.exit(2);
}
process.stdout.write(output);
