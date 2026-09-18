-- POSTGRES_DB already creates `triage` (checkpoints + timers). `crm` is a
-- separate database for the CRM stub's patient records — different bounded
-- context, same server, so it's still all "one DB you can watch".
CREATE DATABASE crm;
