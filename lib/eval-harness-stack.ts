import * as fs from "node:fs"
import * as path from "node:path"
import * as cdk from "aws-cdk-lib"
import * as bedrock from "aws-cdk-lib/aws-bedrock"
import * as iam from "aws-cdk-lib/aws-iam"
import * as s3 from "aws-cdk-lib/aws-s3"
import * as s3deploy from "aws-cdk-lib/aws-s3-deployment"
import type { Construct } from "constructs"
import { deployedPromptText, discoverPrompts, inputVariableNames, logicalId, validateNameSuffix } from "./prompts"

/**
 * Infra only. One Bedrock Prompt per prompts/ directory, two shared scratch buckets
 * (destroy-on-delete, 7-day expiry), and the role evaluation jobs assume.
 * Jobs are not a CloudFormation resource — see docs/runbook.md.
 */

function readPromptFile(dir: string, promptId: string, fileName: string): string {
  const filePath = path.join(dir, fileName)
  if (!fs.existsSync(filePath)) {
    throw new Error(`prompts/${promptId}/${fileName} is missing`)
  }
  return fs.readFileSync(filePath, "utf-8")
}

export class EvalHarnessStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props)

    const modelUnderTestId = this.node.tryGetContext("modelUnderTestId")
    if (typeof modelUnderTestId !== "string" || modelUnderTestId.trim() === "") {
      throw new Error("Pass -c modelUnderTestId=<the Bedrock model id you enabled>. See docs/setup.md.")
    }

    // Bucket and role names are global. A second region needs -c nameSuffix=...; changing a
    // suffix on a live stack replaces the buckets and deletes their objects.
    const rawSuffix = this.node.tryGetContext("nameSuffix") ?? ""
    if (typeof rawSuffix !== "string") {
      throw new Error("nameSuffix must be a string, for example -c nameSuffix=-use1")
    }
    const nameSuffix = validateNameSuffix(rawSuffix)

    // Deterministic names so IAM policies can pin exact ARNs.
    const expireScratch: s3.LifecycleRule[] = [
      {
        id: "expire-after-seven-days",
        expiration: cdk.Duration.days(7),
        abortIncompleteMultipartUploadAfter: cdk.Duration.days(7),
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
      description: "Assumed by Bedrock evaluation jobs to read the dataset, write results, and invoke models.",
    })
    datasetBucket.grantRead(evalJobRole)
    // The service role needs to read the output bucket back (GetObject, ListBucket, GetBucketLocation), not only write it.
    outputBucket.grantRead(evalJobRole)
    outputBucket.grantWrite(evalJobRole)
    evalJobRole.addToPolicy(
      new iam.PolicyStatement({
        sid: "InvokeModelsForEval",
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:GetInferenceProfile",
          "bedrock:ListInferenceProfiles",
        ],
        // CreateEvaluationJob rejects a policy scoped to model ARNs. See docs/setup.md.
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

      const systemPromptText = readPromptFile(definition.dir, definition.id, "system-prompt.txt")
      const userMessageTemplate = readPromptFile(definition.dir, definition.id, "user-message-template.txt")
      const inputVariables = inputVariableNames(userMessageTemplate, definition.id).map((name) => ({ name }))
      // Evaluation datasets have one prompt string. This user message is that string, with the
      // placeholder left in, so invoking the Prompt resource sends the same text as a job.
      const userMessage = deployedPromptText(systemPromptText, userMessageTemplate)

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
                messages: [{ role: "user", content: [{ text: userMessage }] }],
                inputVariables,
              },
            },
            inferenceConfiguration: {
              text: { temperature: definition.temperature, maxTokens: definition.maxTokens },
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
