# Human summary of a run_nuke.sh / cloudshell-oneshot.sh result (input = the result JSON). Output: text.
#   jq -r -f table.jq result.json

def pad($n): tostring | if length >= $n then .[0:$n] else . + ([range($n - length)] | map(" ") | join("")) end;
def lpad($n): tostring | if length >= $n then . else ([range($n - length)] | map(" ") | join("")) + . end;
def dash: if . == null or . == 0 then "-" else tostring end;
def bytype: to_entries | sort_by(-.value, .key) | map("\(.key) \(.value)") | join(", ");
# LIST-ERR = lister failures that may hide resources (unexpected + transient); EXP/SCP = expected / maybe-SCP types
def lferr: (.listing_failed_unexpected // []) | length;
def expscp: (.listing_failed_by_bucket // {}) as $b | [($b.expected // 0), ($b.maybe_scp // 0)]
            | if add == 0 then "-" else "\(.[0])/\(.[1])" end;
def bucket($b): (.listing_failed_buckets // {})[$b] // [];

. as $r
| ($r.applied) as $apply
| [
    "",
    "== aws-nuke \(if $apply then "APPLY" else "DRY-RUN" end)  account \($r.account_id) (\($r.account_alias // "" | if . == "" then "no alias" else . end))  aws-nuke \($r.aws_nuke_version // "?")  \($r.duration_s // 0)s ==",
    ("REGION" | pad(16)) + ("EXIT" | pad(6)) + ("TIME" | pad(7))
      + (if $apply then ("REMOVED" | lpad(8)) + ("FAILED" | lpad(8)) + ("LEFT" | lpad(6))
         else ("WOULD-DELETE" | lpad(13)) end)
      + ("KEPT" | lpad(6)) + ("LIST-ERR" | lpad(10)) + ("EXP/SCP" | lpad(9)),
    ( $r.regions | to_entries[] | .key as $o | .value as $v
      | ($o | pad(16)) + ($v.exit_code | tostring | pad(6)) + ("\($v.duration_s // 0)s" | pad(7))
        + (if $apply then ($v.counts.removed | dash | lpad(8)) + ($v.counts.failed | dash | lpad(8))
                          + ($v.counts.remaining | dash | lpad(6))
           else ($v.counts.would_remove | dash | lpad(13)) end)
        + ($v.counts.filtered | dash | lpad(6)) + ($v | lferr | dash | lpad(10)) + ($v | expscp | lpad(9)) ),
    "",
    ( $r.regions | to_entries[] | .key as $o | .value as $v
      | ( if $apply then
            (if ($v.removed_by_type | length) > 0 then "[\($o)] removed: \($v.removed_by_type | bytype)" else empty end),
            (if ($v.failed_by_type | length) > 0 then "[\($o)] FAILED: \($v.failed_by_type | bytype)" else empty end),
            ($v.failed_items[]? | "[\($o)]   x \(.type) \(.name)"),
            (if ($v.remaining_by_type | length) > 0 then "[\($o)] NOT FINISHED: \($v.remaining_by_type | bytype)" else empty end)
          else
            (if ($v.would_remove_by_type | length) > 0 then "[\($o)] would delete: \($v.would_remove_by_type | bytype)" else empty end)
          end ),
        ( $v | bucket("expected")
          | if length > 0 then "[\($o)] listing failed (expected - service not used/subscribed): \(join(", "))" else empty end ),
        ( $v | bucket("maybe_scp")
          | if length > 0 then "[\($o)] access denied (maybe SCP): \(join(", "))" else empty end ),
        ( $v | bucket("transient")
          | if length > 0 then "[\($o)] LISTING FAILED (5xx/throttling - re-run to confirm): \(join(", "))" else empty end ),
        ( $v | bucket("unexpected")[] as $k
          | "[\($o)] LISTING FAILED \($k): \($v.listing_failed_by_type[$k].error)" ),
        ( ($v.fatal // [])[] | "[\($o)] aws-nuke stopped: \(.)" ),
        ( if ($v.non_json_lines // 0) > 0 and ($v.exit_code != 0) then "[\($o)] aws-nuke crashed - see \($v.log) / .stderr" else empty end )
    ),
    "",
    ( if $apply then
        "RESULT: \($r.status) (exit \($r.exit_code))  removed \($r.totals.removed), failed \($r.totals.failed), not finished \($r.totals.remaining)"
      else
        "RESULT: \($r.status) (exit \($r.exit_code))  would delete \($r.totals.would_remove) item(s) - nothing was changed"
      end ),
    ( ($r.totals.listing_failed_unexpected // 0) as $n
      | if $n > 0 then "LIST-ERR: \($n) lister failure(s) - resources of those types were NOT scanned (see LISTING FAILED above)" else empty end ),
    ( if ($r.protected_users // [] | length) > 0 then "protected IAM user(s): \($r.protected_users | join(", "))" else empty end ),
    "logs + configs: \($r.log_dir)"
  ]
| .[]
