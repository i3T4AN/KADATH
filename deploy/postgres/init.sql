-- Runtime tables and their RLS policies are installed idempotently by
-- kadath.store so upgrades also apply to existing PostgreSQL volumes. These
-- group roles are created here so the first kernel connection can grant and
-- enforce access immediately.
DO $$ BEGIN
  CREATE ROLE kadath_kernel NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
  CREATE ROLE kadath_agent NOLOGIN;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
GRANT kadath_kernel TO kadath;
