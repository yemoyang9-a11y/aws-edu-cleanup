# Summarize ONE aws-nuke v3 `--log-format json` log (one region / `global`).
#
#   jq -R -s -f summarize.jq --arg applied true --argjson exit_code 0 --argjson rules "$(cat listing_errors.json)" x.jsonl
#      [--argjson known_noise '["SomeType"]']   (extra type names classified as "expected", added to the rules)
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
# BEGIN listing-error classification (cloudshell-oneshot.sh carries an identical copy - a test compares them)
# Rules: listing_errors.json. Input {type, error(raw)} -> "expected" | "maybe_scp" | "transient" | "unexpected"
def lf_bucket($rules):
  (.error // "" | tostring) as $e | (.type // "") as $t
  | if any(($rules.known_noise_types // [])[]; . == $t) then "expected"
    elif any(($rules.expected // [])[]; .re as $re | $e | test($re)) then "expected"
    elif any(($rules.maybe_scp // [])[]; .re as $re | $e | test($re)) then "maybe_scp"
    elif any(($rules.transient // [])[]; .re as $re | $e | test($re)) then "transient"
    else "unexpected" end;
def lf_rank: {"expected": 0, "maybe_scp": 1, "transient": 2, "unexpected": 3}[.] // 3;
# END listing-error classification
def short: tostring | split("\n") | map(gsub("^\\s+"; "") | select(length > 0))
           | (.[0:3] | join(" | "))[0:300];

( ($ARGS.named.rules // error("summarize.jq: pass --argjson rules \"$(cat listing_errors.json)\""))
  | (if type == "array" then .[0] else . end)
  | .known_noise_types = ((.known_noise_types // []) + ($ARGS.named.known_noise // [])) ) as $rules
| [ splits("\n") | select(length > 0) | (try fromjson catch null) ] as $parsed
| [ $parsed[] | select(type == "object") ] as $ev
| ($parsed | map(select(type != "object")) | length) as $non_json
| ( reduce ($ev[] | select((.type | type) == "string" and (.state | type) == "string")) as $e
      ({}; .[$e.type + "\u0000" + ($e.owner // "") + "\u0000" + ($e.name // "")] =
             {type: $e.type, name: ($e.name // ""), state: $e.state}) | [.[]] ) as $all
| [ $ev[] | select(.level == "error" and ((.msg // "") | startswith("listing failed")))
          | {type: (.resource_type // "?"), raw: ((.error // .msg) | tostring)}
          | {type, error: (.raw | short), bucket: ({type, error: .raw} | lf_bucket($rules))} ] as $lf
# per type: the worst bucket of its lines (and that line's error)
| ($lf | group_by(.type) | map((map(.bucket | lf_rank) | max) as $m | (map(select((.bucket | lf_rank) == $m)) | .[0]) as $w
        | {key: .[0].type, value: {count: length, error: $w.error, bucket: $w.bucket}}) | from_entries) as $lf_by
| (["expected", "maybe_scp", "transient", "unexpected"]
    | map(. as $b | {key: $b, value: [$lf_by | to_entries[] | select(.value.bucket == $b) | .key]}) | from_entries) as $buckets
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
# real lister failures (LIST-ERR): unexpected + transient; expected / maybe_scp are reported but not counted
| ($buckets.transient + $buckets.unexpected | sort) as $unexpected
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
    listing_failed_buckets: $buckets,
    listing_failed_by_bucket: ($buckets | map_values(length)),
    errors: ($errs | unique | .[0:20]),
    fatal: ($fatal | unique),
    non_json_lines: $non_json
  }
