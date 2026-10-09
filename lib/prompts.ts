import * as fs from "node:fs"
import * as path from "node:path"

export interface PromptDefinition {
  /** Directory name under prompts/, and the S3 key prefix its datasets deploy under. */
  id: string
  /** Absolute path to that directory. */
  dir: string
  /** Bedrock Prompt resource name. Defaults to the directory name. */
  promptName?: string
  description: string
  temperature: number
  maxTokens: number
}

const DEFAULT_TEMPERATURE = 0
const DEFAULT_MAX_TOKENS = 4000

/** One {{name}} in the user template is that prompt's untrusted input. Keep in sync with render-datasets.py. */
const INPUT_VARIABLE = /\{\{([A-Za-z][A-Za-z0-9_]*)\}\}/g

export function inputVariableNames(template: string, promptId: string): string[] {
  const names = [...template.matchAll(INPUT_VARIABLE)].flatMap((match) => (match[1] ? [match[1]] : []))
  const unique = [...new Set(names)]
  if (unique.length === 0) {
    throw new Error(`prompts/${promptId}/user-message-template.txt has no {{variable}} placeholder`)
  }
  if (unique.length > 1) {
    throw new Error(
      `prompts/${promptId}/user-message-template.txt has multiple placeholders (${unique.join(", ")}); one untrusted input only`,
    )
  }
  return unique
}

/**
 * CloudFormation logical IDs are alphanumeric and must start with a letter.
 * `foo-bar` and `fooBar` both become FooBar; callers must reject that collision
 * rather than let the second prompt overwrite the first.
 */
export function logicalId(id: string): string {
  const parts = id.split(/[^a-zA-Z0-9]+/).filter((part) => part !== "")
  let body = parts.map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join("")
  if (body === "") body = "Prompt"
  if (/^[0-9]/.test(body)) body = `P${body}`
  return body
}

export function assertUniqueLogicalIds(ids: string[]): void {
  const seen = new Map<string, string>()
  for (const id of ids) {
    const logical = logicalId(id)
    const previous = seen.get(logical)
    if (previous !== undefined) {
      throw new Error(
        `prompts/${previous} and prompts/${id} both produce CloudFormation logical id ${logical}. Rename one of them.`,
      )
    }
    seen.set(logical, id)
  }
}

const DATASET_BUCKET_PREFIX = "bedrock-prompt-evals-dataset-"
const ROLE_NAME_PREFIX = "bedrock-prompt-evals-job-role"
const ACCOUNT_ID_LENGTH = 12

/** Empty, or `-use1`: a leading hyphen, then lowercase letters, digits, and hyphens, ending with a letter or digit. */
export function validateNameSuffix(suffix: string): string {
  if (suffix !== "" && !/^-[a-z0-9-]*[a-z0-9]$/.test(suffix)) {
    throw new Error(
      `nameSuffix ${JSON.stringify(suffix)} must be empty or start with "-" followed by lowercase letters, digits, and hyphens, and end with a letter or digit (for example -use1)`,
    )
  }
  const bucketName = `${DATASET_BUCKET_PREFIX}${"0".repeat(ACCOUNT_ID_LENGTH)}${suffix}`
  if (bucketName.length > 63) {
    throw new Error(
      `nameSuffix ${JSON.stringify(suffix)} is too long: the dataset bucket name would be ${bucketName.length} characters (max 63)`,
    )
  }
  const roleName = `${ROLE_NAME_PREFIX}${suffix}`
  if (roleName.length > 64) {
    throw new Error(
      `nameSuffix ${JSON.stringify(suffix)} is too long: the job role name would be ${roleName.length} characters (max 64)`,
    )
  }
  return suffix
}

function readManifest(promptId: string, manifestPath: string): Record<string, unknown> {
  let parsed: unknown
  try {
    parsed = JSON.parse(fs.readFileSync(manifestPath, "utf-8"))
  } catch (err) {
    const detail = err instanceof Error ? err.message : String(err)
    throw new Error(`prompts/${promptId}/prompt.json is not valid JSON: ${detail}`)
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error(`prompts/${promptId}/prompt.json must be a JSON object`)
  }
  return parsed as Record<string, unknown>
}

function requiredDescription(promptId: string, manifest: Record<string, unknown>): string {
  const description = manifest.description
  if (typeof description !== "string" || description.trim() === "") {
    throw new Error(`prompts/${promptId}/prompt.json description must be a non-empty string`)
  }
  return description
}

function optionalPromptName(promptId: string, manifest: Record<string, unknown>): string | undefined {
  if (manifest.promptName === undefined) return undefined
  if (typeof manifest.promptName !== "string" || manifest.promptName.trim() === "") {
    throw new Error(`prompts/${promptId}/prompt.json promptName must be a non-empty string when set`)
  }
  return manifest.promptName
}

function temperatureOf(promptId: string, manifest: Record<string, unknown>): number {
  if (manifest.temperature === undefined) return DEFAULT_TEMPERATURE
  const temperature = manifest.temperature
  if (typeof temperature !== "number" || !Number.isFinite(temperature) || temperature < 0 || temperature > 1) {
    throw new Error(`prompts/${promptId}/prompt.json temperature must be a number from 0 to 1`)
  }
  return temperature
}

function maxTokensOf(promptId: string, manifest: Record<string, unknown>): number {
  if (manifest.maxTokens === undefined) return DEFAULT_MAX_TOKENS
  const maxTokens = manifest.maxTokens
  if (typeof maxTokens !== "number" || !Number.isInteger(maxTokens) || maxTokens < 1) {
    throw new Error(`prompts/${promptId}/prompt.json maxTokens must be a positive integer`)
  }
  return maxTokens
}

export function discoverPrompts(promptsRoot: string): PromptDefinition[] {
  const definitions = fs
    .readdirSync(promptsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => {
      const dir = path.join(promptsRoot, entry.name)
      const manifestPath = path.join(dir, "prompt.json")
      if (!fs.existsSync(manifestPath)) {
        throw new Error(`prompts/${entry.name} has no prompt.json`)
      }
      const manifest = readManifest(entry.name, manifestPath)
      const promptName = optionalPromptName(entry.name, manifest)
      const definition: PromptDefinition = {
        id: entry.name,
        dir,
        description: requiredDescription(entry.name, manifest),
        temperature: temperatureOf(entry.name, manifest),
        maxTokens: maxTokensOf(entry.name, manifest),
      }
      if (promptName !== undefined) definition.promptName = promptName
      return definition
    })
    .sort((a, b) => a.id.localeCompare(b.id))

  assertUniqueLogicalIds(definitions.map((definition) => definition.id))
  return definitions
}
