---------------------------- MODULE GardenerRun ----------------------------
(* One run of the pull request job in .github/workflows/gardener.yml: run the fence with --fix,
   then commit if a fixer ran, git status shows changes outside .cache/, and no check regressed.
   REPORT says where the fence's comment file lands. The factory writes it under .cache/ (git-ignored
   and excluded from the status test); the first version wrote it in the repository root, and this
   model found the commit that version made on a real pull request.                      *)
CONSTANT REPORT        \* where the fence writes the comment file: "repository root" or "cache"

VARIABLES fixed,       \* a fixer ran (report.json "fixed" is not empty)
          code,        \* the fixer changed a tracked file
          regressed,   \* a check that passed before now fails
          report,      \* the comment file is an untracked file git status sees
          commit,      \* "none", or what the gardener's commit holds: "code" or "report only"
          pc

vars == <<fixed, code, regressed, report, commit, pc>>

Init == fixed = FALSE /\ code = FALSE /\ regressed = FALSE /\ report = FALSE /\ commit = "none" /\ pc = "fence failed"

\* fence --fix: a fixer is chosen for a failing check. It may change nothing (for example the
\* guard:ratchet fixer, check.py --fix-lines, on a ratchet finding about new dependencies).
RunFixer == pc = "fence failed" /\ fixed' = TRUE /\ code' \in BOOLEAN /\ pc' = "fixed"
            /\ UNCHANGED <<regressed, report, commit>>
\* Every check runs again. Only a fixer that changed code can break a check.
Recheck == pc = "fixed" /\ regressed' \in (IF code THEN BOOLEAN ELSE {FALSE}) /\ pc' = "rechecked"
           /\ UNCHANGED <<fixed, code, report, commit>>
\* --md writes the comment file.
WriteComment == pc = "rechecked" /\ report' = (REPORT = "repository root") /\ pc' = "commented"
                /\ UNCHANGED <<fixed, code, regressed, commit>>
\* git add -A; git commit, when the job's three tests pass.
Commit == pc = "commented" /\ fixed /\ (code \/ report) /\ ~regressed
          /\ commit' = (IF code THEN "code" ELSE "report only") /\ pc' = "done"
          /\ UNCHANGED <<fixed, code, regressed, report>>
NoCommit == pc = "commented" /\ ~(fixed /\ (code \/ report) /\ ~regressed) /\ pc' = "done"
            /\ UNCHANGED <<fixed, code, regressed, report, commit>>
Finished == pc = "done" /\ UNCHANGED vars

Next == RunFixer \/ Recheck \/ WriteComment \/ Commit \/ NoCommit \/ Finished
Spec == Init /\ [][Next]_vars

\* Rules
CommitsOnlyFixes == commit # "report only"
NeverCommitsARegression == commit # "none" => ~regressed
=============================================================================
