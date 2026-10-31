"""Saba's research memory: pre-registered hypotheses and evidence-linked lessons (persistent, searchable).

Lives next to the registry on the persistent data volume (`<root>/academy_memory.sqlite3`). Facts stay in the
registry and ledger; this store holds the *reasoning* that connects them, and refuses claims without proof:

* a hypothesis is registered BEFORE it is run, with its plan hashed (pre-registration) and a trial budget;
* a lesson must cite experiments that exist in the registry; their result hashes are stored, so later edits of
  the evidence are detected (`verify_lesson`);
* verdict `supported` additionally requires a positive, consumed final test; validation-only success can at most
  be a `candidate`; negative validation evidence is enough for `refuted` (dead ends are valuable).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

VERDICTS = ("candidate", "supported", "refuted", "inconclusive")
STATUSES = ("open", "testing", "supported", "refuted", "abandoned")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, allow_nan=False, default=str).encode()).hexdigest()


class ResearchMemory:
    def __init__(self, path, journal=None):
        """journal: optional memory.experience_store.ExperienceStore, mirrored as a generic event timeline."""
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.journal = journal
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS hypotheses (
          id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, statement TEXT NOT NULL, family TEXT NOT NULL,
          status TEXT NOT NULL, trial_budget INTEGER NOT NULL, trials_used INTEGER NOT NULL DEFAULT 0,
          parent_id INTEGER, plan_json TEXT NOT NULL, preregistered_sha256 TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS hypothesis_experiments (hypothesis_id INTEGER NOT NULL, experiment_id INTEGER NOT NULL,
          PRIMARY KEY (hypothesis_id, experiment_id));
        CREATE TABLE IF NOT EXISTS lessons (
          id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, hypothesis_id INTEGER, claim TEXT NOT NULL, verdict TEXT NOT NULL,
          confidence REAL NOT NULL, scope_json TEXT NOT NULL, tags TEXT NOT NULL, evidence_json TEXT NOT NULL,
          evidence_sha256 TEXT NOT NULL, superseded_by INTEGER);
        CREATE TABLE IF NOT EXISTS cycles (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
          experiments_run INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL);
        """)
        try:
            self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts USING fts5(claim, tags, scope_text)")
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False
        self.db.commit()

    def close(self):
        self.db.close()

    def _event(self, kind, payload):
        if self.journal is not None:
            self.journal.add(f"academy.{kind}", payload)

    # ---- hypotheses ------------------------------------------------------
    def register_hypothesis(self, statement: str, family: str, trial_budget: int, plan: dict, parent_id: int | None = None) -> int:
        if not statement.strip() or not family.strip():
            raise ValueError("statement and family are required")
        if not isinstance(trial_budget, int) or not 1 <= trial_budget <= 10_000:
            raise ValueError("trial_budget must be an integer in [1, 10000]")
        if not isinstance(plan, dict) or not plan.get("experiments") or not plan.get("instruments"):
            raise ValueError("plan needs 'experiments' and 'instruments'")
        if parent_id is not None and self.hypothesis(parent_id) is None:
            raise ValueError("unknown parent hypothesis")
        cur = self.db.execute("INSERT INTO hypotheses(created_at,statement,family,status,trial_budget,parent_id,plan_json,preregistered_sha256) "
                              "VALUES (?,?,?,?,?,?,?,?)",
                              (_now(), statement.strip(), family.strip(), "open", trial_budget, parent_id,
                               json.dumps(plan, sort_keys=True), _sha(plan)))
        self.db.commit()
        self._event("hypothesis", {"id": cur.lastrowid, "statement": statement.strip(), "family": family})
        return int(cur.lastrowid)

    def hypothesis(self, hid: int):
        row = self.db.execute("SELECT * FROM hypotheses WHERE id=?", (hid,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["plan"] = json.loads(d.pop("plan_json"))
        d["trials_remaining"] = d["trial_budget"] - d["trials_used"]
        d["plan_intact"] = _sha(d["plan"]) == d["preregistered_sha256"]
        return d

    def open_hypotheses(self) -> list[dict]:
        ids = [r[0] for r in self.db.execute("SELECT id FROM hypotheses WHERE status IN ('open','testing') ORDER BY id")]
        return [self.hypothesis(i) for i in ids]

    def spend_trials(self, hid: int, n: int):
        h = self.hypothesis(hid)
        if h is None:
            raise ValueError("unknown hypothesis")
        if n > h["trials_remaining"]:
            raise ValueError(f"trial budget exhausted for hypothesis {hid}: need {n}, {h['trials_remaining']} left")
        self.db.execute("UPDATE hypotheses SET trials_used = trials_used + ? WHERE id=?", (n, hid))
        self.db.commit()

    def set_status(self, hid: int, status: str):
        if status not in STATUSES:
            raise ValueError("invalid status")
        if self.hypothesis(hid) is None:
            raise ValueError("unknown hypothesis")
        self.db.execute("UPDATE hypotheses SET status=? WHERE id=?", (status, hid))
        self.db.commit()
        self._event("hypothesis_status", {"id": hid, "status": status})

    def link_experiments(self, hid: int, experiment_ids):
        self.db.executemany("INSERT OR IGNORE INTO hypothesis_experiments VALUES (?,?)", [(hid, int(e)) for e in experiment_ids])
        self.db.commit()

    # ---- lessons ---------------------------------------------------------
    def _evidence(self, registry, evidence: list[dict]) -> list[dict]:
        if not evidence:
            raise ValueError("a lesson needs evidence: cite at least one experiment id")
        out = []
        for e in evidence:
            eid = e.get("experiment_id")
            row = registry.db.execute("SELECT name, dataset_sha256, configuration_json, results_json FROM experiments WHERE id=?",
                                      (eid,)).fetchone() if isinstance(eid, int) else None
            if row is None or not row[0].startswith("academy:"):
                raise ValueError(f"evidence experiment {eid!r} does not exist in the registry")
            out.append({"experiment_id": eid, "name": row[0], "dataset_sha256": row[1], "results_sha256": _sha(json.loads(row[3]))})
        return out

    def add_lesson(self, registry, *, claim: str, verdict: str, confidence: float, evidence: list[dict], scope: dict,
                   hypothesis_id: int | None = None, tags: str = "") -> int:
        if not claim.strip():
            raise ValueError("claim is required")
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")
        if hypothesis_id is not None and self.hypothesis(hypothesis_id) is None:
            raise ValueError("unknown hypothesis")
        ev = self._evidence(registry, evidence)
        if verdict == "supported":
            tests = [e for e in ev if e["name"] == "academy:final_test"]
            ok = False
            for e in tests:
                res = json.loads(registry.db.execute("SELECT results_json FROM experiments WHERE id=?", (e["experiment_id"],)).fetchone()[0])
                m = res.get("final_test", {})
                ok |= (m.get("net_return_pct") or 0) > 0 and (m.get("trades") or 0) >= 5 and not m.get("shortfall")
            if not ok:
                raise ValueError("'supported' needs a consumed final test with positive net return, >= 5 trades and no shortfall; "
                                 "validation-only success can be recorded as 'candidate'")
        cur = self.db.execute("INSERT INTO lessons(created_at,hypothesis_id,claim,verdict,confidence,scope_json,tags,evidence_json,evidence_sha256) "
                              "VALUES (?,?,?,?,?,?,?,?,?)",
                              (_now(), hypothesis_id, claim.strip(), verdict, float(confidence), json.dumps(scope, sort_keys=True),
                               tags, json.dumps(ev, sort_keys=True), _sha(ev)))
        lid = int(cur.lastrowid)
        if self.fts:
            self.db.execute("INSERT INTO lessons_fts(rowid, claim, tags, scope_text) VALUES (?,?,?,?)",
                            (lid, claim.strip(), tags, json.dumps(scope, sort_keys=True)))
        self.db.commit()
        self.link_experiments(hypothesis_id, [e["experiment_id"] for e in ev]) if hypothesis_id else None
        self._event("lesson", {"id": lid, "verdict": verdict, "claim": claim.strip()[:200]})
        return lid

    def lesson(self, lid: int):
        row = self.db.execute("SELECT * FROM lessons WHERE id=?", (lid,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["scope"], d["evidence"] = json.loads(d.pop("scope_json")), json.loads(d.pop("evidence_json"))
        return d

    def verify_lesson(self, registry, lid: int) -> dict:
        """Re-hash the cited registry rows; `ok` is False if any evidence changed or disappeared."""
        les = self.lesson(lid)
        if les is None:
            raise ValueError("unknown lesson")
        problems = []
        for e in les["evidence"]:
            row = registry.db.execute("SELECT results_json FROM experiments WHERE id=?", (e["experiment_id"],)).fetchone()
            if row is None:
                problems.append({"experiment_id": e["experiment_id"], "problem": "missing"})
            elif _sha(json.loads(row[0])) != e["results_sha256"]:
                problems.append({"experiment_id": e["experiment_id"], "problem": "results changed"})
        return {"lesson_id": lid, "ok": not problems, "problems": problems}

    def supersede(self, old_id: int, new_id: int):
        if self.lesson(old_id) is None or self.lesson(new_id) is None or old_id == new_id:
            raise ValueError("both lessons must exist and differ")
        self.db.execute("UPDATE lessons SET superseded_by=? WHERE id=?", (new_id, old_id))
        self.db.commit()

    def search(self, text: str | None = None, *, verdict: str | None = None, market: str | None = None,
               include_superseded: bool = False, limit: int = 20) -> list[dict]:
        if not 1 <= limit <= 200:
            raise ValueError("limit out of range")
        clauses, args = [], []
        if text and text.strip():
            terms = [t for t in "".join(c if c.isalnum() else " " for c in text).split() if t]
            if self.fts and terms:
                clauses.append("id IN (SELECT rowid FROM lessons_fts WHERE lessons_fts MATCH ?)")
                args.append(" ".join(f'"{t}"' for t in terms))
            else:
                for t in terms:
                    clauses.append("(claim LIKE ? OR tags LIKE ? OR scope_json LIKE ?)")
                    args += [f"%{t}%"] * 3
        if verdict:
            clauses.append("verdict=?")
            args.append(verdict)
        if market:
            clauses.append("scope_json LIKE ?")
            args.append(f'%"{market}"%')
        if not include_superseded:
            clauses.append("superseded_by IS NULL")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        ids = [r[0] for r in self.db.execute(f"SELECT id FROM lessons{where} ORDER BY id DESC LIMIT ?", (*args, limit))]
        return [self.lesson(i) for i in ids]

    # ---- notebook ----------------------------------------------------------
    def notebook(self, *, status: dict | None = None, max_chars: int = 8000) -> str:
        """Compact digest for the agent's context: derived from the stores, never a second source of truth."""
        lines = ["# Saba research notebook (generated; sources: registry, ledger, academy_memory)", ""]
        if status:
            lines += ["## Status", *[f"- {k}: {v}" for k, v in status.items()], ""]
        lines.append("## Open hypotheses")
        for h in self.open_hypotheses():
            lines.append(f"- H{h['id']} [{h['family']}] {h['statement']} (trials left {h['trials_remaining']}/{h['trial_budget']}, "
                         f"{h['status']}{'' if h['plan_intact'] else ', PLAN TAMPERED'})")
        lines += ["", "## Supported / candidate findings"]
        for les in self.search(limit=60):
            if les["verdict"] in ("supported", "candidate"):
                lines.append(f"- L{les['id']} {les['verdict'].upper()} (conf {les['confidence']:.2f}) {les['claim']} "
                             f"| scope {json.dumps(les['scope'], sort_keys=True)} | evidence {[e['experiment_id'] for e in les['evidence']]}")
        lines += ["", "## Dead ends (refuted - do not repeat without a new reason)"]
        for les in self.search(verdict="refuted", limit=60):
            lines.append(f"- L{les['id']} {les['claim']} | scope {json.dumps(les['scope'], sort_keys=True)}")
        lines += ["", "## Inconclusive"]
        for les in self.search(verdict="inconclusive", limit=20):
            lines.append(f"- L{les['id']} {les['claim']}")
        text = "\n".join(lines) + "\n"
        return text if len(text) <= max_chars else text[: max_chars - 40].rstrip() + "\n... (truncated; use search_lessons)\n"

    def write_notebook(self, path, **kw) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(self.notebook(**kw), encoding="utf-8")
        tmp.replace(p)
        return p
