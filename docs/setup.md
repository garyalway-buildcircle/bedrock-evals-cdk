# First-time AWS setup

The sequence to get this stack running in a fresh AWS account, and the failure modes worth knowing
about before you hit them. The policy templates already account for all of them.

## 0. Use a dedicated account

Don't deploy this into an account that runs anything else. A throwaway, single-purpose account
makes teardown unambiguous.

## 1. Attach the bootstrap policy (one-time)

Copy `iam/bootstrap-policy.json` to `iam/bootstrap-policy.local.json` (gitignored; the tracked
template keeps its placeholders, only the local copy holds real values), replace every
`<ACCOUNT_ID>` and `<REGION>`, and attach it to the IAM user or role you'll deploy with:

```bash
aws iam put-user-policy --user-name <your-user> --policy-name cdk-bootstrap \
  --policy-document file://iam/bootstrap-policy.local.json
```

It is scoped to CDK's predictable bootstrap resource names (the `cdk-hnb659fds-*` roles, staging
bucket, staging ECR repo) rather than a blanket admin grant, and it is not guaranteed complete. If
`cdk bootstrap` fails partway with `AccessDenied`, add the single action the error names rather
than broadening the policy.

## 2. Bootstrap, and confirm it actually finished

```bash
AWS_PROFILE=<your-profile> AWS_REGION=<your-region> npx cdk bootstrap aws://<ACCOUNT_ID>/<REGION>
```

Wait for the literal line `✅ Environment aws://<ACCOUNT_ID>/<REGION> bootstrapped.` Don't treat
"the command ran" as "it succeeded" — confirm the SSM parameter exists:

```bash
aws ssm get-parameter --name /cdk-bootstrap/hnb659fds/version --region <REGION>
```

If this returns nothing, bootstrap did not complete, and `cdk deploy` will fail later with
`SSM parameter /cdk-bootstrap/hnb659fds/version not found`. Don't move on until it returns a value.

## 3. Swap to the steady-state policy

Copy `iam/steady-state-policy.json` to `iam/steady-state-policy.local.json` the same way. Replace
`<ACCOUNT_ID>`, `<REGION>`, and `<NAME_SUFFIX>`. `<NAME_SUFFIX>` is the same string as
`-c nameSuffix` at deploy, including the leading hyphen. The us-east-1 deploy in `docs/runbook.md`
uses `-use1`, so the role is `bedrock-prompt-evals-job-role-use1` and the output bucket is
`bedrock-prompt-evals-output-<ACCOUNT_ID>-use1`. An unsuffixed stack uses an empty `<NAME_SUFFIX>`.
This policy is what stays attached long-term. `cdk deploy` and `cdk destroy` only need `sts:AssumeRole` on four of
the bootstrap-created roles — the CLI does the CloudFormation work through the assumed
`deploy-role`, not the caller's own permissions — plus direct Bedrock permissions for submitting
and reading evaluation jobs.

**Attach it as a customer-managed policy, not inline.** It exceeds the 2048-character inline limit;
a managed policy's 6144-character ceiling leaves headroom. The bootstrap policy from step 1 is
small enough to stay inline.

```bash
aws iam create-policy --policy-name bedrock-prompt-evals-steady-state \
  --policy-document file://iam/steady-state-policy.local.json
aws iam attach-user-policy --user-name <your-user> --policy-arn <arn from above>
```

If you already attached an earlier version inline and now hit `Policy exceeding the 2048 characters
limit can't be saved`, switch to the managed-policy commands above and delete the inline one
(`aws iam delete-user-policy`) so you aren't carrying both.

Five of this policy's statements are non-obvious:

- **`InvokeModelsForPreflight`** — the playground check in step 4 invokes as your identity.
  `InvokeModel`, `InvokeModelWithResponseStream`, `Converse`, and `ConverseStream` need
  `Resource: "*"`. An inference-profile ID does not accept a policy scoped to one model ARN.

- **`DiscoverModelCatalog`** — `bedrock:ListFoundationModels` / `ListInferenceProfiles` are
  catalog-wide reads with no per-model ARN to scope to, so they need `Resource: "*"`. Without them,
  discovering which models are enabled fails with `AccessDeniedException`.
- **`ReadStackOutputs`** — `cdk deploy` gets `cloudformation:DescribeStacks` implicitly through the
  assumed `deploy-role`, but a plain `aws cloudformation describe-stacks` call (which
  `docs/runbook.md` needs, to fetch stack outputs) uses your own identity directly.
- **`CreateEvaluationJobOnModels`** — Bedrock checks `CreateEvaluationJob` against the model ARNs
  referenced inside the request, not just the `evaluation-job` ARN being created. Granting the
  model ARNs to the `EvalJobRole` is not enough; the caller needs them too. The denial names the
  foundation-model ARN, not the evaluation-job one. Its resource must be `"*"` — this action does
  not respect scoped model ARNs.
- **`ListEvaluationJobsAccountWide`** — `bedrock:ListEvaluationJobs` queries the account rather than
  a named job, so a scoped `evaluation-job/*` ARN never matches and the grant silently fails. It
  needs its own statement with `Resource: "*"`.

## 3b. Adding a second region

The stack can run in more than one region at once. Three things are global and need care:

1. **Bucket and role names.** They are globally unique and carry no region component, so a second
   deployment collides with the first. Pass `-c nameSuffix=<suffix>` to give it distinct names.
   Never add or change a suffix on an already-deployed stack: the buckets are `autoDeleteObjects`,
   so renaming replaces them and discards the results they hold.
2. **Bootstrap is per-region.** Repeat step 2 for the new region. A missing bootstrap presents as
   `sts:AssumeRole ... AccessDenied` on `cdk-hnb659fds-deploy-role-<ACCOUNT_ID>-<REGION>`, which
   reads like a permissions problem but means the role does not exist yet.
3. **The steady-state policy is per-region and per-suffix.** Its `AssumeCdkDeployRoles`,
   evaluation-job, prompt and CloudFormation ARNs all embed `<REGION>`. Add the second region's
   ARNs. `PassEvalJobRoleToBedrock` and `ReadEvaluationResults` embed `<NAME_SUFFIX>` once. A second
   suffix needs a second ARN in each of those statements, or the deploying identity cannot submit
   jobs or read results for that deployment.

## 4. Enable Bedrock model access, and pick models that actually work

Console → Bedrock → **Model access** → enable candidates for a model under test and a separate,
stronger judge model. Then list what's enabled:

```bash
aws bedrock list-foundation-models --region <REGION> \
  --by-provider anthropic --query "modelSummaries[].modelId" --output table
```

**Being listed is not the same as being usable.** In the order you'll hit them:

1. **The account may not be entitled to the model at all**, regardless of what the catalog shows.
   Both `list-foundation-models` and `list-inference-profiles` (which will report `ACTIVE`) can
   list a model the account cannot invoke; the invoke then fails with
   `<model-id> is not available for this account`. That is a hard account-tier gate, not an IAM or
   region problem, and nothing here fixes it. The cheapest reliable check is a single playground
   invoke before wiring anything up. If it fails, pick a different model.
2. **Some models can't be invoked directly**, only through an inference profile. A bare model ID
   fails with `on-demand throughput isn't supported for this model, retry with the ID or ARN of an
   inference profile`. Check the `inferenceTypesSupported` field — if `ON_DEMAND` is absent, use
   the `us.`/`eu.`/`global.` profile ID instead:
   ```bash
   aws bedrock list-foundation-models --by-provider anthropic \
     --query "modelSummaries[].[modelId,inferenceTypesSupported]" --output text
   ```
3. **Built-in cross-region profiles need model access in every region they fan out to**, not just
   your own. Check the fan-out first:
   ```bash
   aws bedrock get-inference-profile --inference-profile-identifier us.anthropic.claude-X \
     --query "models[].modelArn"
   ```
   These profiles span anywhere from 3 to 7 regions. The profile is unusable unless *all* of them
   are granted, and there is no way to force which one it routes to. Prefer a profile with a
   smaller fan-out.
4. **You can create your own single-region profile**, sidestepping the multi-region requirement, but
   only if the model genuinely supports on-demand throughput in that region:
   ```bash
   aws bedrock create-inference-profile --inference-profile-name my-profile \
     --model-source copyFrom="arn:aws:bedrock:<REGION>::foundation-model/anthropic.claude-X" \
     --region <REGION>
   ```
   If this fails with `The provided foundation model does not support On Demand inference`, that is
   point 2's real cause and a hard model-level limitation. Stop and pick a different model.
5. **The judge model field is stricter than the model-under-test field.** The generator
   (`inferenceConfig.models[].bedrockModel.modelIdentifier`) accepts a custom
   `application-inference-profile` ARN from point 4. The judge
   (`customMetricConfig.evaluatorModelConfig.bedrockEvaluatorModels[].modelIdentifier`) does not —
   only a bare foundation-model ID or a system-defined profile ID. You cannot paper over a judge
   model's on-demand limitation with a custom profile.
6. **An enabled, invokable model may still throttle**, with no self-service fix. Small jobs can
   fail with `Encountered throttling exception while serving the request for model ...`,
   proportional to row count rather than intermittent. **Service Quotas is a dead end here**: for
   Anthropic models on Bedrock the console exposes only a tokens-per-minute quota (millions per
   minute, nowhere near what a handful of rows uses), not the requests-per-minute quota Amazon's own
   Nova models get. The limit is an internal burst/concurrency cap, not a named quota you can
   request an increase for.

   **Never run two evaluation jobs at once.** Concurrency is the strongest lever here: two jobs
   submitted together against the same Anthropic model will throttle one of them, where the same
   two run clean back to back. Submit one job, wait for `Completed`, then submit the next.

   Region also matters, though less than concurrency: the same model and job size can throttle in
   a smaller Bedrock region and run clean in a larger one such as `us-east-1`. Standing up a second
   region is cheap (section 3b).

   If a single job still throttles with nothing else running, the remaining options are to open an
   AWS Support case quoting the exact error, or to switch the model under test to an Amazon-hosted
   model (Nova Pro, Nova Lite), which get a much larger requests-per-minute allowance. A Nova pass
   is not a Sonnet pass. This harness is written for Claude Sonnet 4.6.

Pass an inference-profile ID to this harness, for example `us.anthropic.claude-sonnet-4-6`. That is the ID in the README deploy command. A bare model ID fails for current Claude models with the on-demand error in point 2.

## 5. Deploy

```bash
AWS_PROFILE=<your-profile> AWS_REGION=us-east-1 npx cdk deploy \
  -c modelUnderTestId=<the model ID from step 4> \
  -c nameSuffix=-use1
```

`<NAME_SUFFIX>` in the steady-state policy has to be this same `-use1`. Omitting
`-c modelUnderTestId=...` fails at synth. The stack does not deploy a placeholder model id.
`npm run deploy` and `npm run destroy` are plain `cdk deploy` and `cdk destroy`. They still need
the context flags above, and destroy asks for confirmation. Recommended CDK feature flags live in
`cdk.flags.json` and are loaded by `bin/app.ts`, so `cdk synth` does not warn that they are
unconfigured. `-c` values still override them.

Note the `Outputs`: `DatasetBucketName`, `OutputBucketName`, `EvalJobRoleArn`, and one
`PromptArn<Name>` per directory under `prompts/`.

## 6. Run an evaluation

See `docs/runbook.md`.

## Tearing down

```bash
AWS_REGION=us-east-1 npx cdk destroy -c nameSuffix=-use1
```

That is the deployment from step 5. Destroying it needs the same region and the same
`-c nameSuffix`. A stack that was deployed without a suffix is destroyed without `-c nameSuffix`.

Everything in the stack is destroy-on-delete. The bootstrap resources (`CDKToolkit` stack, staging
bucket, ECR repo, the `cdk-hnb659fds-*` roles) are CDK's own scaffolding, not this project's, and
survive `cdk destroy` by design. Removing them is a manual CloudFormation delete of `CDKToolkit`;
only do that if you're decommissioning the account.
