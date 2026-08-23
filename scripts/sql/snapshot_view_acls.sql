-- snapshot_view_acls.sql — the production evidence that 014 did not change who
-- can read what.  READ-ONLY.  It creates nothing, drops nothing and writes
-- nothing; run it as often as you like.
--
-- WHY THIS FILE EXISTS (round-5 review finding F12). 014 DROPs and CREATEs
-- v_agent_scorecard and v_quality_by_input, and a DROP takes the object's ACL
-- with it. Section 5b of the migration snapshots owner and ACL into a temp
-- table before the DROP, clears whatever the CREATE inherits, replays the
-- snapshot and asserts the result matches -- all inside the one transaction.
-- That machinery was never exercised against real grants: the dump used to
-- build the staging copy was taken with `pg_dump --no-acl`, so every view
-- arrived with relacl NULL and the replay carried an empty set. On staging the
-- mechanism is now exercised with synthetic grants (acceptance section 9). On
-- PRODUCTION we cannot manufacture grants to test with, so the evidence is
-- this: take the picture before, take it again after, and diff the two.
--
-- HOW TO USE IT
--
--   BEFORE applying 014:
--     psql "$DATABASE_URL" -X -A -F $'\t' --pset=footer=off \
--          -f scripts/sql/snapshot_view_acls.sql > acl_before.tsv
--
--   apply the migration:
--     psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
--          -f db/migrations/014_evaluation_status.sql
--
--   AFTER:
--     psql "$DATABASE_URL" -X -A -F $'\t' --pset=footer=off \
--          -f scripts/sql/snapshot_view_acls.sql > acl_after.tsv
--
--   diff:
--     diff -u acl_before.tsv acl_after.tsv && echo "ACLs unchanged"
--
--   -X so no .psqlrc changes the formatting between the two runs, -A -F tab so
--   the output is diffable rather than box-drawn, footer off so the row count
--   is not part of the diff. Both files are evidence: keep them with the
--   rollout record, not in /tmp.
--
-- WHAT A NON-EMPTY DIFF MEANS
--
--   * A line only in acl_before  -> a privilege the DROP destroyed and the
--     replay failed to restore. Somebody has lost access. Do not proceed;
--     re-grant from acl_before.tsv and work out why 5b's own assertion did not
--     fire (it should have rolled the migration back).
--
--   * A line only in acl_after   -> a privilege that arrived from somewhere,
--     almost certainly ALTER DEFAULT PRIVILEGES stamping the newly created
--     views. This is the failure F5 is about, and 5b's clear step exists to
--     stop it. Revoke it and say so in the rollout record.
--
--   * grantor changing to the migration role on an otherwise identical line ->
--     expected and harmless where 5b could not assume the original grantor;
--     the migration RAISEs a NOTICE naming each one. Privileges are intact.
--
--   * The two views flipping between "(no explicit ACL)" and
--     `{owner=arwdDxt/owner}` -> the same privileges written a different way.
--     relacl is NULL only until the first GRANT or REVOKE touches the object
--     and there is no SQL that puts it back; compare the exploded grant rows
--     (the `grant` lines below), not the raw acl text.
--
-- SCOPE. Every view and every function in the public schema, not just the two
-- 014 touches: 014 also CREATE OR REPLACEs v_usable_evaluations and seven
-- eval_* functions, two of which are SECURITY DEFINER after F4, and a change
-- in the owner of a definer function is a privilege change even though no
-- GRANT moved. Sorted deterministically so the diff is about content.

-- ---------------------------------------------------------------------------
-- one row per object, plus one row per explicit grant on it
-- ---------------------------------------------------------------------------
WITH views AS (
  SELECT c.oid,
         -- ::text, not the bare name. This is the FIRST branch of a UNION ALL
         -- and PostgreSQL resolves the column type from it: left as `name`,
         -- every function signature in the other branch is silently truncated
         -- to 63 characters and two overloads can collapse into one line.
         c.relname::text                             AS object_name,
         'view'::text                                AS object_kind,
         c.relowner                                  AS owner_oid,
         pg_get_userbyid(c.relowner)                 AS owner_name,
         c.relacl                                    AS acl,
         NULL::text                                  AS extra
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
   WHERE n.nspname = current_schema()
     AND c.relkind IN ('v', 'm')
), funcs AS (
  SELECT p.oid,
         p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')'
                                                     AS object_name,
         'function'::text                            AS object_kind,
         p.proowner                                  AS owner_oid,
         pg_get_userbyid(p.proowner)                 AS owner_name,
         p.proacl                                    AS acl,
         -- security and search_path are part of "who can do what" for a
         -- definer function, so they travel with the snapshot.
         CASE WHEN p.prosecdef THEN 'SECURITY DEFINER' ELSE 'SECURITY INVOKER' END
           || coalesce(' | ' || array_to_string(p.proconfig, ' | '), '')
                                                     AS extra
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE n.nspname = current_schema()
     -- Extension-owned functions are excluded. pgcrypto, dblink and friends
     -- install several hundred functions into public; they are not ours, they
     -- do not change when 014 runs, and they would bury the twelve lines this
     -- snapshot is actually about. CREATE EXTENSION / ALTER EXTENSION UPDATE
     -- is the thing that moves them, and that is not this rollout.
     AND NOT EXISTS (SELECT 1 FROM pg_depend dep
                      WHERE dep.classid    = 'pg_proc'::regclass
                        AND dep.objid      = p.oid
                        AND dep.deptype    = 'e')
), objs AS (
  SELECT * FROM views
  UNION ALL
  SELECT * FROM funcs
)
SELECT line_kind, object_kind, object_name, detail
  FROM (
    -- the object line: owner, and the raw acl text for the human record
    SELECT 1                                                    AS ord,
           'object'::text                                       AS line_kind,
           o.object_kind,
           o.object_name,
           'owner=' || o.owner_name
             || coalesce(' | ' || o.extra, '')
             || ' | acl='
             || coalesce(array_to_string(o.acl, ' '), '(no explicit ACL)')
                                                                AS detail,
           o.object_name                                        AS sort_name,
           ''::text                                             AS sort_detail
      FROM objs o

    UNION ALL

    -- one line per explicit grant, exploded, so a diff points at the grant
    -- rather than at a reshuffled aclitem[]. The owner's own entry is included
    -- deliberately: a change of owner shows up as a pair of lines here.
    SELECT 2,
           'grant',
           o.object_kind,
           o.object_name,
           'grantee=' || CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                              ELSE pg_get_userbyid(a.grantee) END
             || ' | priv=' || a.privilege_type
             || ' | grantable=' || a.is_grantable
             || ' | grantor=' || pg_get_userbyid(a.grantor),
           o.object_name,
           CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                ELSE pg_get_userbyid(a.grantee) END || '/' || a.privilege_type
      FROM objs o
      -- aclexplode() is STRICT, so a NULL acl yields no rows and the object
      -- simply has no grant lines. Do NOT coalesce to '{}'::aclitem[]:
      -- aclexplode rejects a zero-dimensional array outright.
      CROSS JOIN LATERAL aclexplode(o.acl) a
  ) q
 ORDER BY object_kind, sort_name, ord, sort_detail;

-- ---------------------------------------------------------------------------
-- default privileges, because they are what silently re-grant the new objects
-- ---------------------------------------------------------------------------
-- If this returns anything for schema public and object type 'r' (tables and
-- views), 014's CREATE VIEW will inherit those grants and 5b's clear step is
-- doing real work. Take this before AND after too: it should not change, and
-- if it does, somebody altered default privileges during the rollout window.
SELECT 'default_acl'::text                       AS line_kind,
       coalesce(n.nspname, '(all schemas)')      AS schema,
       d.defaclobjtype                           AS object_type,
       pg_get_userbyid(d.defaclrole)             AS granting_role,
       array_to_string(d.defaclacl, ' ')         AS acl
  FROM pg_default_acl d
  LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace
 ORDER BY schema, object_type, granting_role;
-- Zero rows is the ordinary answer and means nothing is being stamped onto new
-- objects. defaclobjtype: r = table/view, S = sequence, f = function,
-- T = type, n = schema.
