import assert from "node:assert/strict"
import * as fs from "node:fs"
import * as os from "node:os"
import * as path from "node:path"
import {
  assertLocalJobsMatchModel,
  assertNoSystemPlaceholders,
  assertUniqueLogicalIds,
  deployedPromptText,
  discoverPrompts,
  inputVariableNames,
  logicalId,
  validateNameSuffix,
} from "../lib/prompts"

function withPrompt(root: string, id: string, manifest: unknown): void {
  const dir = path.join(root, id)
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(path.join(dir, "prompt.json"), JSON.stringify(manifest))
}

const tempRoots: string[] = []

function tempPrompts(): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "prompts-"))
  tempRoots.push(root)
  return root
}

assert.equal(logicalId("foo-bar"), "FooBar")
assert.equal(logicalId("fooBar"), "FooBar")
assert.equal(logicalId("1-foo"), "P1Foo")
assert.equal(logicalId("---"), "Prompt")
assert.throws(() => assertUniqueLogicalIds(["foo-bar", "fooBar"]), /CloudFormation logical id FooBar/)
assert.doesNotThrow(() => assertUniqueLogicalIds(["foo-bar", "foobar"]))

assert.equal(validateNameSuffix(""), "")
assert.equal(validateNameSuffix("-use1"), "-use1")
assert.equal(validateNameSuffix("-" + "a".repeat(21)), "-" + "a".repeat(21))
assert.throws(() => validateNameSuffix("use1"), /must be empty or start with/)
assert.throws(() => validateNameSuffix("-Use1"), /must be empty or start with/)
assert.throws(() => validateNameSuffix("-"), /must be empty or start with/)
assert.throws(() => validateNameSuffix("-" + "a".repeat(22)), /too long/)

assert.equal(
  deployedPromptText("Be brief.\n", "<<<UNTRUSTED_CONTENT>>>\n{{note}}\n<<<END_UNTRUSTED_CONTENT>>>\n"),
  "Be brief.\n\n<<<UNTRUSTED_CONTENT>>>\n{{note}}\n<<<END_UNTRUSTED_CONTENT>>>",
)

assert.doesNotThrow(() => assertNoSystemPlaceholders("Be brief.", "example"))
assert.throws(() => assertNoSystemPlaceholders("Hello {{name}}", "example"), /system-prompt.txt contains a placeholder/)

assert.deepEqual(inputVariableNames("{{note}}", "example"), ["note"])
assert.throws(() => inputVariableNames("{{note}}\n{{note}}", "example"), /exactly one placeholder/)
assert.throws(() => inputVariableNames("{{note}} {{email}}", "example"), /exactly one placeholder/)

{
  const root = tempPrompts()
  withPrompt(root, "bare", { description: "A prompt" })
  const [bare] = discoverPrompts(root)
  assert.ok(bare)
  assert.equal(bare.id, "bare")
  assert.equal(bare.description, "A prompt")
  assert.equal(bare.promptName, undefined)
  assert.equal(bare.temperature, 0)
  assert.equal(bare.maxTokens, 4000)
}

{
  const root = tempPrompts()
  withPrompt(root, "tuned", { description: "Tuned", promptName: "tuned-name", temperature: 0.2, maxTokens: 800 })
  const [tuned] = discoverPrompts(root)
  assert.ok(tuned)
  assert.equal(tuned.promptName, "tuned-name")
  assert.equal(tuned.temperature, 0.2)
  assert.equal(tuned.maxTokens, 800)
}

{
  const root = tempPrompts()
  withPrompt(root, "empty", {})
  assert.throws(() => discoverPrompts(root), /description must be a non-empty string/)
}

{
  const root = tempPrompts()
  withPrompt(root, "long", { description: "x".repeat(201) })
  assert.throws(() => discoverPrompts(root), /201 characters/)
}

{
  const root = tempPrompts()
  const dir = path.join(root, "local")
  fs.mkdirSync(path.join(dir, "eval-jobs"), { recursive: true })
  const model = "us.anthropic.claude-sonnet-4-6"
  fs.writeFileSync(
    path.join(dir, "eval-jobs", "golden-job.local.json"),
    JSON.stringify({ inferenceConfig: { models: [{ bedrockModel: { modelIdentifier: model } }] } }),
  )
  assert.doesNotThrow(() => assertLocalJobsMatchModel(dir, "local", model))
  assert.throws(() => assertLocalJobsMatchModel(dir, "local", "other-model"), /modelIdentifier must be/)
}

{
  const root = tempPrompts()
  withPrompt(root, "bad-temp", { description: "x", temperature: 2 })
  assert.throws(() => discoverPrompts(root), /temperature must be a number from 0 to 1/)
}

{
  const root = tempPrompts()
  withPrompt(root, "foo-bar", { description: "a" })
  withPrompt(root, "fooBar", { description: "b" })
  assert.throws(() => discoverPrompts(root), /Rename one of them/)
}

{
  const root = tempPrompts()
  withPrompt(root, "huge", { description: "x", maxTokens: 200001 })
  assert.throws(() => discoverPrompts(root), /maxTokens must be an integer from 1 to 200000/)
}

{
  const root = tempPrompts()
  withPrompt(root, "one", { description: "a", promptName: "shared" })
  withPrompt(root, "two", { description: "b", promptName: "shared" })
  assert.throws(() => discoverPrompts(root), /both use Bedrock prompt name/)
}

{
  const root = tempPrompts()
  withPrompt(root, "bad.name", { description: "a" })
  assert.throws(() => discoverPrompts(root), /Bedrock prompt name/)
}

{
  const example = discoverPrompts(path.join(__dirname, "..", "prompts"))
  assert.equal(example.length, 1)
  assert.equal(example[0]?.id, "example")
  assert.equal(example[0]?.temperature, 0)
  assert.equal(example[0]?.maxTokens, 4000)
  assert.ok(example[0]?.description)
}

for (const root of tempRoots) fs.rmSync(root, { recursive: true, force: true })
console.log("prompts.test.ts ok")
