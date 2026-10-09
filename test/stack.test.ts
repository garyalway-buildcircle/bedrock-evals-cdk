import assert from "node:assert/strict"
import * as cdk from "aws-cdk-lib"
import { Template } from "aws-cdk-lib/assertions"
import { EvalHarnessStack } from "../lib/eval-harness-stack"

const app = new cdk.App({
  context: {
    modelUnderTestId: "us.anthropic.claude-sonnet-4-6",
    nameSuffix: "-use1",
  },
})
const stack = new EvalHarnessStack(app, "BedrockPromptEvals", {
  env: { account: "123456789012", region: "us-east-1" },
})
const template = Template.fromStack(stack)

template.hasResourceProperties("AWS::S3::Bucket", {
  BucketName: { "Fn::Join": ["", ["bedrock-prompt-evals-dataset-", { Ref: "AWS::AccountId" }, "-use1"]] },
  LifecycleConfiguration: {
    Rules: [
      {
        Id: "expire-after-seven-days",
        ExpirationInDays: 7,
        AbortIncompleteMultipartUpload: { DaysAfterInitiation: 7 },
        Status: "Enabled",
      },
    ],
  },
})

const policies = template.findResources("AWS::IAM::Policy")
const statements = Object.values(policies).flatMap((policy) => {
  const document = (policy as { Properties?: { PolicyDocument?: { Statement?: unknown } } }).Properties?.PolicyDocument
  const statement = document?.Statement
  return Array.isArray(statement) ? statement : []
})
const invoke = statements.find((statement) => (statement as { Sid?: string }).Sid === "InvokeModelsForEval") as
  | { Action?: string[]; Resource?: string }
  | undefined
assert.ok(invoke)
assert.ok(invoke.Action?.includes("bedrock:InvokeModel"))
assert.ok(invoke.Action?.includes("bedrock:GetInferenceProfile"))
assert.ok(invoke.Action?.includes("bedrock:CreateModelInvocationJob"))
assert.ok(invoke.Action?.includes("bedrock:StopModelInvocationJob"))
assert.equal(invoke.Resource, "*")

const prompts = template.findResources("AWS::Bedrock::Prompt")
const prompt = Object.values(prompts)[0] as {
  Properties: {
    Variants: Array<{
      TemplateConfiguration: {
        Chat: { System?: unknown; Messages: Array<{ Content: Array<{ Text: string }> }> }
      }
    }>
  }
}
const chat = prompt.Properties.Variants[0]?.TemplateConfiguration.Chat
assert.ok(chat)
assert.equal(chat.System, undefined)
const text = chat.Messages[0]?.Content[0]?.Text ?? ""
assert.match(text, /You label a short customer note/)
assert.match(text, /\{\{note\}\}/)

console.log("stack.test.ts ok")
