-- Saba Academy: private, per-user/per-org access to SIMULATED exports in Supabase Storage.
-- REVIEW BEFORE APPLYING. Not applied automatically; nothing in the repo runs this.
-- Object path convention (see export.py):
--   v1/simulation/orgs/<org_id>/users/<owner_id>/...
-- storage.foldername(name) is 1-indexed: [1]='v1' [2]='simulation' [3]='orgs' [4]=org_id [5]='users' [6]=owner_id

-- 1. Private bucket: no public URLs, ever.
insert into storage.buckets (id, name, public)
values ('academy-exports', 'academy-exports', false)
on conflict (id) do update set public = false;

-- 2. Org membership (multi-tenant ready; v1 can have a single row for the operator).
create table if not exists public.academy_memberships (
  user_id uuid not null references auth.users(id) on delete cascade,
  org_id  text not null check (org_id ~ '^[A-Za-z0-9_-]{1,64}$'),
  role    text not null default 'member' check (role in ('owner','member','viewer')),
  primary key (user_id, org_id)
);
alter table public.academy_memberships enable row level security;
create policy "members read own memberships" on public.academy_memberships
  for select to authenticated using (user_id = auth.uid());

-- 3. Read-only access to the caller's own folder inside an org they belong to.
--    Only the 'simulation' namespace is readable; real holdings/bookkeeping must use another bucket.
create policy "academy own simulation exports readable" on storage.objects
  for select to authenticated using (
    bucket_id = 'academy-exports'
    and (storage.foldername(name))[1] = 'v1'
    and (storage.foldername(name))[2] = 'simulation'
    and (storage.foldername(name))[6] = auth.uid()::text
    and exists (select 1 from public.academy_memberships m
                where m.user_id = auth.uid() and m.org_id = (storage.foldername(name))[4])
  );
-- No insert/update/delete policies for 'authenticated': uploads are done by the operator pipeline
-- with a service-role key kept OUTSIDE the browser, Lovable and this repository.
