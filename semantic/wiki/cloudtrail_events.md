# cloudtrail_events — notes

- "How many AssumeRole events" is ONE ungrouped count:
  `count(*) ... WHERE event_name = 'AssumeRole'`. Do not add up a grouped
  top-N — the long tail of small principals is missing from it (an eval case
  answered 13,511 / 14,700 that way; the true count for 2026-09-02 was 13,858).
- The acting principal is `coalesce(session_issuer_arn, user_arn, invoked_by)`:
  service-invoked calls carry neither ARN.
- Always bound `date` (the partition column).
