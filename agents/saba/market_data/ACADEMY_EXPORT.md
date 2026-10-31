# Saba Academy – run logs and private JSON export (schema v1)

Everything here is **simulation data**. It is stored and exported in a namespace that is
strictly separate from any future real holdings or bookkeeping data.

## 1. Full logs per experiment

`train` and `final-test` write complete logs next to the registry:

```
<archive root>/academy_runs/<experiment_id>/meta.json        config, strategy, params, results, owner/org, domain=simulation
<archive root>/academy_runs/<experiment_id>/train.json.gz
<archive root>/academy_runs/<experiment_id>/validation.json.gz
<archive root>/academy_runs/<experiment_id>/final_test.json.gz   (only after the one-time final test)
<archive root>/academy_runs/summaries/<summary_id>.json      selection, random-control comparison
```

Each period file holds `decisions` (every bar, incl. abstentions and the reason), `orders`
(fills and rejections), `trades`, `positions`, `equity` (balance, equity, drawdown per bar), and
`risk`. They are written atomically and deterministically (same seed/data ⇒ same bytes). Training never
writes a `final_test` file. A ledger directory belongs to one `owner_id`/`org_id`; writing to another
owner's experiment id is refused.

`--owner-id` (a Supabase user UUID for exports) and `--org-id` (default `personal`) are accepted by
`train`, `final-test` and `export`; ids are restricted to `[A-Za-z0-9_-]{1,64}` (no path traversal).

## 2. Export

```bash
python -m market_data.academy_cli export --owner-id <supabase-user-uuid> [--org-id personal] [--out DIR] [--include-abstentions]
```

Default output: `<archive root>/academy_export` (outside the repository). The exporter **refuses to write
inside any git working tree** (override `--allow-in-repo` exists but should not be used) so data cannot be
committed by accident. It never uploads and holds no credentials. The tree is built in a temp directory and
swapped in atomically.

```
v1/simulation/orgs/<org_id>/users/<owner_id>/
  manifest.json                      experiments index, sha256 + size of every file, content_sha256, generated_at
  strategies.json                    strategy catalogue with default parameters
  training_runs.json                 selection summaries + random-control comparison
  experiments/<id>/summary.json      config, params, metrics per period, final_test_consumed
  experiments/<id>/<period>/trades.json | positions.json | equity.json | decisions.json | risk.json
```

Every file is an envelope:

```json
{"schema_version": "1.0", "domain": "simulation", "simulated": true, "kind": "trades",
 "owner_id": "...", "org_id": "...", "experiment_id": 42, "period": "validation",
 "disclaimer": "Simulated historical results. No real orders, ...", "data": [ ... ]}
```

* `equity.json` is downsampled to at most 2000 points (first and last kept); it states `downsampled` and `points_total`.
* `decisions.json` omits `ABSTAIN` rows by default and reports `abstentions_omitted` / `decisions_total`.
* `final_test` files appear only after the one-time final test has been run.
* Timestamps are ISO-8601 UTC; unavailable numbers are `null` (never NaN).
* Same ledgers ⇒ identical files and `content_sha256` (only `generated_at` differs).
* Breaking changes ⇒ new `schema_version` **and** a new path prefix (`v2/`); v1 files keep their shape.

### Separation from real data
* Top-level namespace `simulation/` in the path, `"domain": "simulation"` and `"simulated": true` in every file.
* Real holdings, accounts or bookkeeping must use a **different bucket and schema**; the storage policy only grants
  access to `simulation/` paths in `academy-exports`. No identifiers, instrument accounts or broker data exist in these files.

## 3. Access: private Supabase Storage (not public)

1. Review and run `market_data/academy/sql/001_academy_private_storage.sql` (idempotent; **not applied by any code**), then
   `002_academy_audit_storage_policies.sql` (read-only) and its manual checklist. Add yourself once:
   `insert into public.academy_memberships(user_id, org_id) values ('<your-auth-uid>', 'personal');` (single operator; no admin UI).
2. Upload as the operator with a service-role key that stays **outside** the browser, Lovable and this repo, e.g.
   `supabase storage cp -r <path>/v1 ss:///academy-exports/`. Clients cannot write.
3. Use your Supabase user UUID as `--owner-id` so the folder name equals `auth.uid()`.

### How the policy is hardened
* **Whole-path validation** in one function, `public.academy_export_path_ok(name)`:
  `^v1/simulation/orgs/<org: [A-Za-z0-9_-]{1,64}>/users/<auth.uid()>(/[A-Za-z0-9_-]+)+\.json$` **and** a membership row for that org.
  This fixes the version, namespace, `orgs`/`users` keywords, org format, owner and file type, and forbids `..`, `//`, extra dots,
  other extensions and uid-prefix matches. The previous policy only compared two folder names.
  `SECURITY DEFINER`, `search_path=''`, `coalesce(..., false)` so anonymous callers get `false`, never an error or NULL.
* **Other policies on `storage.objects`:** Postgres ORs permissive policies, so any broad policy (an old "authenticated can read all", an
  anon/public policy, or one allowing insert/update/delete) would silently widen access. 001 adds **RESTRICTIVE** guards (AND-ed with all
  permissive ones) for select/insert/update/delete on `anon, authenticated`: reads require the valid own path; client writes are always
  denied for this bucket; other buckets are unaffected (`bucket_id <> 'academy-exports' or …`). The service role bypasses RLS and keeps uploading.
* **Bucket settings:** `public=false` (a public bucket bypasses RLS for downloads), JSON only, 50 MB limit.
* **Audit:** `002_...sql` lists all storage policies, RLS state, BYPASSRLS roles and table grants, plus 5 manual denial checks
  (anon, second user, wrong uid/org, client writes, signed/public URL).
* Tests extract the actual regex from the SQL and check it against every path the exporter produces and a list of malicious paths.
  **They cannot execute Postgres RLS**; confirm with the audit script in your own project.

### Ledger ownership (code side)
`LedgerStore` now checks ownership (`owner_id`, `org_id`, `domain=simulation` in the experiment's `meta.json`) on **every** write and read:
`write_period` requires existing, matching metadata (so a period file can neither be created for an unknown experiment nor overwritten by another
owner/org, nor by a ledger lacking ownership fields); `read_period`, `read_meta`, `periods` and `experiments()` apply the same rule; summaries
cannot be overwritten across owners.

## 4. Lovable client (design)

The Lovable app signs in with Supabase Auth and reads with the user's session:
`supabase.storage.from('academy-exports').download(`v1/simulation/orgs/${org}/users/${user.id}/manifest.json`)`.
It needs only the project URL and anon key; Trinity, Saba and the SQLite registry are never reachable from the web.
Pages: experiment list (from `manifest`), detail (summary + risk), equity/drawdown chart, trades, positions, decision log with reasons.
Treat the data as untrusted text when rendering (decision reasons are escaped, never HTML).

## 5. Known limitations
* Static files: no filtering or pagination server-side; the client loads per-experiment files on demand.
* Export is a snapshot; run `export` and upload again after new experiments. There is no live push.
* Bucket policy correctness must be verified in your Supabase project (try reading as a second user and as `anon`).
* Multi-tenant fields exist, but the registry and run folder are still one local store; sharing it between
  mutually untrusting tenants would need separate archive roots or a database.
* Exports contain the strategy parameters and your simulated performance: treat as confidential.
