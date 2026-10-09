#!/usr/bin/env node
import * as cdk from "aws-cdk-lib"
import { EvalHarnessStack } from "../lib/eval-harness-stack"

const app = new cdk.App()

new EvalHarnessStack(app, "BedrockPromptEvals", {
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION,
  },
})
