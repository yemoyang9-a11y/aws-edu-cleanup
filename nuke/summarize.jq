# Summarize ONE aws-nuke v3 `--log-format json` log (one region / `global`).
#
#   jq -R -s -f summarize.jq --arg applied true --argjson exit_code 0 --argjson known_noise '["FMSPolicy"]' x.jsonl
#
# Input is read RAW (-R -s): a Go panic / stack trace is not JSON, so lines are parsed one by
# one and non-JSON lines are only counted.
# Field names verified against libnuke v1.3.0 source and a real aws-nuke v3.68.3 run:
#   queue item   (queue/item.go Print)       type, owner, name, state, state_code, msg
#                  msg: "would remove" | "removed" | "failed" | "filtered: <reason>" | "triggered remove" |
#                       "waiting for removal" | "waiting for parent removal" | "waiting on dependencies (..)"
#                  The same item is printed on every pass -> the LAST line per item is its final state.
#   lister error (scanner/scanner.go list)   level "error", resource_type, owner, error, msg "listing failed:\n..."
#   scan end     (nuke/nuke.go Scan)         msg "Scan complete: ...", total, nukeable, filtered
#   apply end    (nuke/nuke.go Run)          msg "Nuke complete: ...", failed, skipped, finished
#                                            or msg "No resource to delete."
#   gave up      (nuke/nuke.go + main.go)    level "fatal", msg "failed" | "max wait retries of N exceeded"
#   anything else at level "fatal" (bad config, no alias, auth ...) = error

def counts: group_by(.) | map({key: .[0], value: length}) | from_entries;
def short: tostring | split("\n") | map(gsub("^\\s+"; "") | select(length > 0))
           | (.[0:3] | join(" | "))[0:300];

[ splits("\n") | select(length > 0) | (try fromjson catch null) ] as $parsed
| [ $parsed[] | select(type == "object") ] as $ev
| ($parsed | map(select(type != "object")) | length) as $non_json
| ( reduce ($ev[] | select((.type | type) == "string" and (.state | type) == "string")) as $e
      ({}; .[$e.type + "\u0000" + ($e.owner // "") + "\u0000" + ($e.name // "")] =
             {type: $e.type, name: ($e.name // ""), state: $e.state}) | [.[]] ) as $all
| [ $ev[] | select(.level == "error" and ((.msg // "") | startswith("listing failed")))
          | {type: (.resource_type // "?"), error: ((.error // .msg) | short)} ] as $lf
| ($lf | group_by(.type) | map({key: .[0].type, value: {count: length, error: .[0].error}}) | from_entries) as $lf_by
| [ $ev[] | select(.level == "fatal" or .level == "panic") | (.msg // "" | short) ] as $fatal
| [ $ev[] | select(.level == "error" and ((.msg // "") | startswith("listing failed") | not)) | (.msg | short) ] as $errs
| ([ $ev[] | select((.msg // "") | startswith("Scan complete")) ] | last) as $scan
| ([ $ev[] | select((.msg // "") | startswith("Nuke complete")) ] | last) as $done
| ([ $ev[] | select((.msg // "") | startswith("No resource to delete")) ] | length > 0) as $nothing
| ([ $ev[] | select((.msg // "") | startswith("The above resources would be deleted")) ] | length > 0) as $dry_done
| ($applied == "true") as $is_apply
| ($all | map(select(.state == "new" or .state == "new-dependency"))) as $would
| ($all | map(select(.state == "finished"))) as $removed
| ($all | map(select(.state == "failed"))) as $failed
| ($all | map(select(.state == "filtered"))) as $filtered
| ($all | map(select(.state | IN("pending", "waiting", "hold", "pending-dependency")))) as $pending
# "expected" lister errors: known-unusable types, or the regional endpoint does not exist (DNS)
| ($lf_by | to_entries
    | map(select((.key as $t | $known_noise | index($t) | not)
                 and (.value.error | test("no such host|DNS lookup failed") | not)))
    | map(.key)) as $unexpected
| ( if $exit_code == 0 and ($done != null or $nothing or $dry_done or ($scan != null and ($is_apply | not)))
      then "completed"
    elif ($fatal | index("failed")) != null then "gave_up_failed"
    elif ($fatal | map(test("^max wait retries")) | any) then "gave_up_wait"
    else "error" end ) as $outcome
| {
    exit_code: $exit_code,
    outcome: $outcome,
    scan: (if $scan then {total: $scan.total, nukeable: $scan.nukeable, filtered: $scan.filtered} else null end),
    result: (if $done then {failed: $done.failed, skipped: $done.skipped, finished: $done.finished} else null end),
    counts: {
      would_remove: ($would | length),
      removed: ($removed | length),
      failed: ($failed | length),
      remaining: (if $is_apply then ($would | length) + ($pending | length) else 0 end),
      filtered: ($filtered | length),
      listing_failed: ($lf | length)
    },
    would_remove_by_type: ($would | map(.type) | counts),
    removed_by_type: ($removed | map(.type) | counts),
    failed_by_type: ($failed | map(.type) | counts),
    remaining_by_type: (if $is_apply then ($would + $pending) | map(.type) | counts else {} end),
    filtered_by_type: ($filtered | map(.type) | counts),
    failed_items: ($failed | map({type, name}) | .[0:100]),
    listing_failed_by_type: $lf_by,
    listing_failed_unexpected: $unexpected,
    errors: ($errs | unique | .[0:20]),
    fatal: ($fatal | unique),
    non_json_lines: $non_json
  }
