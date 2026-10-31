interface Vars {
  fixed: boolean;
  code: boolean;
  regressed: boolean;
  report: boolean;
  commit: string;
  pc: string;
}

export const title = "The gardener on a pull request: what it commits";

export const state = (v: Vars): string => {
  if (v.pc === "fence failed") return "Fence failed";
  if (v.pc === "fixed") return v.code ? "Fixer changed code" : "Fixer changed nothing";
  if (v.pc === "rechecked") return v.regressed ? "A check now fails" : v.code ? "Fixed, checks pass" : "Nothing fixed";
  if (v.pc === "commented") return v.report ? "Comment file in the repository" : "Comment file in the cache";
  if (v.commit === "code") return "Committed the fix";
  if (v.commit === "report only") return "Committed only its comment file";
  return "Committed nothing";
};

export const step = (action: string): string | null => {
  if (action === "RunFixer") return "the fixer runs";
  if (action === "Recheck") return "every check runs again";
  if (action === "WriteComment") return "the fence writes its comment";
  if (action === "Commit") return "the job commits";
  if (action === "NoCommit") return "the job commits nothing";
  return null;
};

export const group = (v: Vars): string => (v.pc === "done" ? "end" : "run");
export const colors = { run: "blue", end: "teal" };
