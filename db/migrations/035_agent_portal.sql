-- Per-person portal. Authentication writes have their own narrowly granted role.
BEGIN;
SET LOCAL lock_timeout = '5s';
CREATE SCHEMA IF NOT EXISTS portal_auth;
REVOKE ALL ON SCHEMA portal_auth FROM PUBLIC;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='portal_auth_app') THEN
    CREATE ROLE portal_auth_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='portal_reader') THEN
    CREATE ROLE portal_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END $$;
CREATE TABLE IF NOT EXISTS portal_auth.users (
  user_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  username text NOT NULL UNIQUE CHECK(username=lower(trim(username))),
  password_hash text NOT NULL,
  role text NOT NULL CHECK(role IN ('agent','leader','manager')),
  agent_id uuid REFERENCES public.agents(agent_id),
  active boolean NOT NULL DEFAULT true,
  must_change_password boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK(role<>'agent' OR agent_id IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS portal_auth.sessions (
  token_hash text PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES portal_auth.users(user_id),
  csrf_token text NOT NULL,
  expires_at timestamptz NOT NULL,
  revoked boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS portal_sessions_user ON portal_auth.sessions(user_id);
CREATE TABLE IF NOT EXISTS portal_auth.login_attempts (
  attempt_id bigserial PRIMARY KEY,
  username text NOT NULL,
  ip_hash text NOT NULL,
  succeeded boolean NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS portal_attempt_time ON portal_auth.login_attempts(created_at);
CREATE TABLE IF NOT EXISTS portal_auth.audit (
  audit_id bigserial PRIMARY KEY,
  actor_id uuid REFERENCES portal_auth.users(user_id),
  action text NOT NULL,
  subject_id uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);
GRANT CONNECT ON DATABASE customer360 TO portal_auth_app,portal_reader;
GRANT USAGE ON SCHEMA portal_auth TO portal_auth_app;
GRANT SELECT,INSERT,UPDATE ON ALL TABLES IN SCHEMA portal_auth TO portal_auth_app;
GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA portal_auth TO portal_auth_app;
GRANT USAGE ON SCHEMA public TO portal_reader;
GRANT SELECT ON public.agents,public.interactions,public.qa_grades_v6,
  public.call_log,public.call_recording_jobs TO portal_reader;
ALTER ROLE portal_reader SET default_transaction_read_only=on;
-- Passwords are provisioned out-of-band, never stored in source/migrations.
COMMIT;
