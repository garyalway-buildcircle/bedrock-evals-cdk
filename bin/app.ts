#!/usr/bin/env node
import * as fs from "node:fs"
import * as path from "node:path"
import * as cdk from "aws-cdk-lib"
import { EvalHarnessStack } from "../lib/eval-harness-stack"

const featureFlags = JSON.parse(
  fs.readFileSync(path.join(__dirname, "..", "cdk.flags.json"), "utf-8"),
) as Record<string, unknown>

const app = new cdk.App({ context: featureFlags })

new EvalHarnessStack(app, "BedrockPromptEvals", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
})
