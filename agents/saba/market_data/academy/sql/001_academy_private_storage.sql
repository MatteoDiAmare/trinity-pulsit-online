-- Saba Academy: private access to SIMULATED exports in Supabase Storage.  Hardened v2.
-- REVIEW BEFORE APPLYING. Not applied automatically; nothing in the repo runs this.
-- Safe to re-run (idempotent). Then run 002_audit_storage_policies.sql and follow its checklist.
--
-- Object path (see export.py), validated as a WHOLE by public.academy_export_path_ok():
--   v1/simulation/orgs/<org_id>/users/<auth.uid()>/<segment>/…/<file>.json
--   segments and org_id: [A-Za-z0-9_-] only (no dots => no '..', no spaces, no '//' , no other extensions)

-- 1. Private bucket, JSON only, size-limited. A public bucket would bypass RLS for downloads.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('academy-exports', 'academy-exports', false, 52428800, array['application/json'])
on conflict (id) do update
  set public = false, file_size_limit = 52428800, allowed_mime_types = array['application/json'];

-- 2. Single-operator access list (kept minimal: one row for you; no admin UI/management here).
create table if not exists public.academy_memberships (
  user_id uuid not null references auth.users(id) on delete cascade,
  org_id  text not null check (org_id ~ '^[A-Za-z0-9_-]{1,64}$'),
  role    text not null default 'owner' check (role in ('owner','member','viewer')),
  primary key (user_id, org_id)
);
alter table public.academy_memberships enable row level security;
revoke all on public.academy_memberships from anon, authenticated;
grant select on public.academy_memberships to authenticated;
drop policy if exists "members read own memberships" on public.academy_memberships;
create policy "members read own memberships" on public.academy_memberships
  for select to authenticated using (user_id = auth.uid());

-- 3. One function = the single definition of "this caller may read this object".
--    SECURITY DEFINER so anon/authenticated evaluation never needs table privileges; it only answers
--    about the caller's own auth.uid(). coalesce => NULL (anon) becomes false.
create or replace function public.academy_export_path_ok(object_name text)
returns boolean
language sql stable security definer
set search_path = ''
as $$
  select coalesce(
    object_name ~ ('^v1/simulation/orgs/[A-Za-z0-9_-]{1,64}/users/' || auth.uid()::text || '(/[A-Za-z0-9_-]+)+\.json$')
    and exists (select 1 from public.academy_memberships m
                where m.user_id = auth.uid() and m.org_id = split_part(object_name, '/', 4)),
    false)
$$;
revoke all on function public.academy_export_path_ok(text) from public;
grant execute on function public.academy_export_path_ok(text) to anon, authenticated;

-- 4. PERMISSIVE: the only grant. Read-only, authenticated, own folder, valid path.
drop policy if exists "academy own simulation exports readable" on storage.objects;
create policy "academy own simulation exports readable" on storage.objects
  as permissive for select to authenticated
  using (bucket_id = 'academy-exports' and public.academy_export_path_ok(name));

-- 5. RESTRICTIVE guard (AND-ed with every permissive policy in the project). Even if another
--    policy (now or later) is broad, e.g. "authenticated can read all" or an anon/public policy, or
--    grants insert/update/delete, access to this bucket still needs a valid own path, and users can
--    never write: with check is always false for this bucket. Other buckets are unaffected.
--    service_role bypasses RLS, so the operator upload pipeline is unaffected.
drop policy if exists "academy guard: read own path only" on storage.objects;
create policy "academy guard: read own path only" on storage.objects
  as restrictive for select to anon, authenticated
  using (bucket_id <> 'academy-exports' or public.academy_export_path_ok(name));

drop policy if exists "academy guard: no client writes" on storage.objects;
create policy "academy guard: no client writes" on storage.objects
  as restrictive for insert to anon, authenticated
  with check (bucket_id <> 'academy-exports');
drop policy if exists "academy guard: no client updates" on storage.objects;
create policy "academy guard: no client updates" on storage.objects
  as restrictive for update to anon, authenticated
  using (bucket_id <> 'academy-exports') with check (bucket_id <> 'academy-exports');
drop policy if exists "academy guard: no client deletes" on storage.objects;
create policy "academy guard: no client deletes" on storage.objects
  as restrictive for delete to anon, authenticated
  using (bucket_id <> 'academy-exports');
