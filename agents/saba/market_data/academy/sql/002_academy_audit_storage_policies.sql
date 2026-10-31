-- READ-ONLY audit for the Academy bucket. Run in the Supabase SQL editor after 001. Expected results below.

-- A. Bucket must be private (public = false), JSON only.
select id, public, file_size_limit, allowed_mime_types from storage.buckets where id = 'academy-exports';

-- B. Every policy on storage.objects. Permissive policies are OR-ed, restrictive ones AND-ed.
--    Review each permissive row: if its qual does not restrict bucket_id (or includes academy-exports) it
--    may grant access to this bucket - the restrictive guards still cap it, but remove such policies anyway.
select policyname, permissive, roles, cmd, qual, with_check
from pg_policies where schemaname = 'storage' and tablename = 'objects' order by permissive, policyname;

-- C. Must be true: RLS enabled on storage.objects.
select relrowsecurity from pg_class where oid = 'storage.objects'::regclass;

-- D. Anyone/anything with BYPASSRLS (service_role etc.) and grants on the membership table.
select rolname from pg_roles where rolbypassrls order by 1;
select grantee, privilege_type from information_schema.role_table_grants
where table_schema = 'public' and table_name = 'academy_memberships' order by 1, 2;

-- Manual checks (client side), expect: own manifest = OK; every other case = denied / not found
--   1. anon key, no session:            download v1/simulation/orgs/<org>/users/<uid>/manifest.json   -> denied
--   2. a second signed-in user:         same path                                                       -> denied
--   3. you, path with another uid / org not in academy_memberships                                      -> denied
--   4. you, upload / update / remove via the client                                                    -> denied
--   5. you, createSignedUrl / public URL on the bucket                                                  -> denied / not public
