import * as fs from "node:fs"
import * as path from "node:path"
import * as cdk from "aws-cdk-lib"
import * as bedrock from "aws-cdk-lib/aws-bedrock"
import * as iam from "aws-cdk-lib/aws-iam"
import * as s3 from "aws-cdk-lib/aws-s3"
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment"
import type { Construct } from "constructs"

/**
 * Infra only. One Bedrock Prompt per prompts/ directory, two shared scratch buckets
 * (destroy-on-delete, 1-day expiry), and the role evaluation jobs assume.
 * Jobs are not a CloudFormation resource — see docs/runbook.md.
 */

interface PromptDefinition {
  /** Directory name under prompts/, and the S3 key prefix its datasets deploy under. */
  id: string
  /** Absolute path to that directory. */
  dir: string
  /** Bedrock Prompt resource name. Defaults to the directory name. */
  promptName?: string
  description: string
}

function discoverPrompts(promptsRoot: string): PromptDefinition[] {
  return fs
    .readdirSync(promptsRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => {
      const dir = path.join(promptsRoot, entry.name)
      const manifest = path.join(dir, "prompt.json")
      if (!fs.existsSync(manifest)) {
        throw new Error(`prompts/${entry.name} has no prompt.json`)
      }
      // Spread first: the directory on disk decides the identity, never a key in the manifest.
      return { ...JSON.parse(fs.readFileSync(manifest, "utf-8")), id: entry.name, dir } as PromptDefinition
    })
    .sort((a, b) => a.id.localeCompare(b.id))
}

/** One {{name}} in the user template is that prompt's untrusted input. Keep in sync with render-datasets.py. */
const INPUT_VARIABLE = /\{\{([A-Za-z][A-Za-z0-9_]*)\}\}/g

function inputVariableNames(template: string, promptId: string): string[] {
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

/** CloudFormation logical IDs are alphanumeric only. */
function logicalId(id: string): string {
  return id
    .split(/[^a-zA-Z0-9]+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join("")
}

export class EvalHarnessStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props)

    const modelUnderTestId = this.node.tryGetContext("modelUnderTestId") ?? "REPLACE_WITH_ENABLED_MODEL_ID"

    // Bucket and role names are global. A second region needs -c nameSuffix=...; changing a
    // suffix on a live stack replaces the buckets and deletes their objects.
    const nameSuffix = this.node.tryGetContext("nameSuffix") ?? ""

    // Deterministic names so IAM policies can pin exact ARNs.
    const expireScratch: s3.LifecycleRule[] = [
      {
        id: "expire-after-one-day",
        expiration: cdk.Duration.days(1),
        abortIncompleteMultipartUploadAfter: cdk.Duration.days(1),
      },
    ]

    const datasetBucket = new s3.Bucket(this, "DatasetBucket", {
      bucketName: `bedrock-prompt-evals-dataset-${cdk.Aws.ACCOUNT_ID}${nameSuffix}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      enforceSSL: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      lifecycleRules: expireScratch,
    })

    const outputBucket = new s3.Bucket(this, "OutputBucket", {
      bucketName: `bedrock-prompt-evals-output-${cdk.Aws.ACCOUNT_ID}${nameSuffix}`,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      enforceSSL: true,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      lifecycleRules: expireScratch,
    })

    const evalJobRole = new iam.Role(this, "EvalJobRole", {
      roleName: `bedrock-prompt-evals-job-role${nameSuffix}`,
      assumedBy: new iam.ServicePrincipal("bedrock.amazonaws.com", {
        conditions: {
          StringEquals: { "aws:SourceAccount": cdk.Aws.ACCOUNT_ID },
          ArnLike: { "aws:SourceArn": `arn:aws:bedrock:${cdk.Aws.REGION}:${cdk.Aws.ACCOUNT_ID}:evaluation-job/*` },
        },
      }),
      description: "Assumed by Bedrock Evaluation Jobs to read the dataset and write results for this project only.",
    })
    datasetBucket.grantRead(evalJobRole)
    outputBucket.grantWrite(evalJobRole)
    evalJobRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "InvokeClaudeModelsForEval",
        actions: ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        // Scoped model ARNs fail CreateEvaluationJob validation. See docs/setup.md.
        resources: ["*"],
      }),
    )

    const promptsRoot = path.join(__dirname, "..", "prompts")
    for (const definition of discoverPrompts(promptsRoot)) {
      const suffix = logicalId(definition.id)

      // Per-prompt prefix so prune cannot touch another prompt's objects. retainOnDelete is
      // false: a rename would otherwise leave a stale key a job can still be pointed at.
      new s3deploy.BucketDeployment(this, `DeployDatasets${suffix}`, {
        sources: [s3deploy.Source.asset(path.join(definition.dir, "datasets"))],
        destinationBucket: datasetBucket,
        destinationKeyPrefix: `datasets/${definition.id}`,
        prune: true,
        retainOnDelete: false,
      })

      const systemPromptText = fs.readFileSync(path.join(definition.dir, "system-prompt.txt"), "utf-8")
      const userMessageTemplate = fs.readFileSync(path.join(definition.dir, "user-message-template.txt"), "utf-8")
      const inputVariables = inputVariableNames(userMessageTemplate, definition.id).map((name) => ({ name }))

      const prompt = new bedrock.CfnPrompt(this, `Prompt${suffix}`, {
        name: definition.promptName ?? definition.id,
        description: definition.description,
        defaultVariant: "default",
        variants: [
          {
            name: "default",
            templateType: "CHAT",
            modelId: modelUnderTestId,
            templateConfiguration: {
              chat: {
                system: [{ text: systemPromptText }],
                messages: [{ role: "user", content: [{ text: userMessageTemplate }] }],
                inputVariables,
              },
            },
            inferenceConfiguration: {
              text: { temperature: 0, maxTokens: 4000 },
            },
          },
        ],
      })

      new cdk.CfnOutput(this, `PromptArn${suffix}`, { value: prompt.promptRef.promptArn })
    }

    new cdk.CfnOutput(this, "DatasetBucketName", { value: datasetBucket.bucketName })
    new cdk.CfnOutput(this, "OutputBucketName", { value: outputBucket.bucketName })
    new cdk.CfnOutput(this, "EvalJobRoleArn", { value: evalJobRole.roleArn })
  }
}
