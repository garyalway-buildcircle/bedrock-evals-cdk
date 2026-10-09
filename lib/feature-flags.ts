import { CURRENTLY_RECOMMENDED_FLAGS } from "aws-cdk-lib/cx-api"

/**
 * Recommended values for every flag whose default still differs from the recommendation.
 * Read from the installed aws-cdk-lib, so a CLI upgrade picks up new flags without a
 * checked-in snapshot. `-c` still overrides these.
 */
export function recommendedFeatureFlagContext(): Record<string, unknown> {
  return { ...CURRENTLY_RECOMMENDED_FLAGS }
}
