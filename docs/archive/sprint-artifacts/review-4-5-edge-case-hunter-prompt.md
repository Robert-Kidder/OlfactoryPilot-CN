# Edge Case Hunter — Story 4.5

Invoke `bmad-review-edge-case-hunter` on the complete workspace diff from baseline
`49fd99ecd0b4247555d071e6fd7e0fff8b3205b9`.

Inspect tracked changes with:

`git diff 49fd99ecd0b4247555d071e6fd7e0fff8b3205b9 --`

Also inspect every path returned by:

`git ls-files --others --exclude-standard`

Trace all branches reachable from selector configuration, receipt identity/staleness,
timeouts, concurrent stop/command ownership, A-zero failure, selector uncertainty,
odor close failure, restart, and cleaning recovery. Run the deletion check for replaced
legacy master-valve and rollback behavior. Treat unrelated Story 4.1 changes as
pre-existing dirty worktree evidence.
