import { execFileSync } from "node:child_process";
import { readdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import assert from "node:assert/strict";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const fixtures = readdirSync(resolve(root, "fixtures"))
  .filter((name) => name.endsWith(".json"))
  .sort();

test("every synthetic fixture produces a non-authoritative receipt", () => {
  assert.equal(fixtures.length, 5);
  for (const fixture of fixtures) {
    const output = execFileSync(process.execPath, [
      resolve(root, "scripts/run-fixture-review.mjs"),
      resolve(root, "fixtures", fixture)
    ], { encoding: "utf8" });
    const receipt = JSON.parse(output);
    assert.equal(receipt.schema_version, "review-receipt.v1");
    assert.match(receipt.packet_sha256, /^[0-9a-f]{64}$/);
    assert.equal(receipt.authority.can_approve, false);
    assert.equal(receipt.authority.can_merge, false);
    assert.equal(receipt.authority.can_resolve_threads, false);
    assert.doesNotMatch(output, /SERVICE_TOKEN=|hidden instructions|REDACTED_SYNTHETIC_VALUE/);
  }
});

test("fixture runner rejects paths outside the fixture directory", () => {
  assert.throws(() => execFileSync(process.execPath, [
    resolve(root, "scripts/run-fixture-review.mjs"),
    resolve(root, "README.md")
  ], { encoding: "utf8", stdio: "pipe" }));
});
