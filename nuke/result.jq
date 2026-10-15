# Combine per-region summaries (summarize.jq output) into the module result (CONTRACTS.md).
#
#   jq -n -f result.jq --argjson regions '{"ap-northeast-2":{...},"global":{...}}' --arg account_id 123456789012 \
#      --arg applied true --arg generated_at ... [--arg alias ... --arg alias_created false --arg caller_arn ...
#      --arg aws_nuke_version ... --arg log_dir ... --arg duration_s 0 --argjson protected_users '[]']
#
# status / exit_code:
#   ERROR  2  any region ended with outcome "error" (aws-nuke could not run / crashed / refused the account)
#   ISSUES 1  apply only: failed or unfinished items remain, or aws-nuke gave up (failed / max wait retries)
#   OK     0  dry-run finished in every region, or apply removed everything it found
# Lister errors ("listing failed") never change the status; they are reported per type and per bucket
# (expected / maybe_scp / transient / unexpected - see listing_errors.json). totals.listing_failed_unexpected =
# transient + unexpected types summed over regions (= the LIST-ERR column).

def num($s): ($s // "0") | tonumber? // 0;

($regions | [.[]]) as $r
| ($applied == "true") as $is_apply
| ($r | map(.outcome == "error") | any) as $err
| ($r | map((.counts.failed // 0) + (.counts.remaining // 0)) | add // 0) as $left
| ($r | map(.outcome | IN("gave_up_failed", "gave_up_wait")) | any) as $gave_up
| (if $err then "ERROR"
   elif $is_apply and ($left > 0 or $gave_up) then "ISSUES"
   else "OK" end) as $status
| {
    module: "nuke",
    account_id: $account_id,
    generated_at: $generated_at,
    applied: $is_apply,
    status: $status,
    exit_code: {"OK": 0, "ISSUES": 1, "ERROR": 2}[$status],
    account_alias: ($ARGS.named.alias // ""),
    alias_created: (($ARGS.named.alias_created // "false") == "true"),
    caller_arn: ($ARGS.named.caller_arn // ""),
    aws_nuke_version: ($ARGS.named.aws_nuke_version // ""),
    protected_users: ($ARGS.named.protected_users // []),
    log_dir: ($ARGS.named.log_dir // ""),
    duration_s: num($ARGS.named.duration_s),
    totals: {
      would_remove: ($r | map(.counts.would_remove // 0) | add // 0),
      removed: ($r | map(.counts.removed // 0) | add // 0),
      failed: ($r | map(.counts.failed // 0) | add // 0),
      remaining: ($r | map(.counts.remaining // 0) | add // 0),
      listing_failed: ($r | map(.counts.listing_failed // 0) | add // 0),
      listing_failed_unexpected: ($r | map(.listing_failed_unexpected // [] | length) | add // 0),
      listing_failed_by_bucket: (["expected", "maybe_scp", "transient", "unexpected"]
        | map(. as $k | {key: $k, value: ($r | map(.listing_failed_by_bucket[$k]? // 0) | add // 0)}) | from_entries)
    },
    regions: $regions
  }
