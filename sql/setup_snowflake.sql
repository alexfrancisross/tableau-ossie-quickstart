-- One-time Snowflake setup for the quickstart. Run as ACCOUNTADMIN (Snowsight
-- worksheet, or: snow sql -c <connection> --role ACCOUNTADMIN -f sql/setup_snowflake.sql).
-- Safe to rerun. The notebook then runs as OSSIE_QS_ROLE and needs no other privilege.
--
-- Edit the three SET lines first.
--   qs_user  user whose connections.toml entry runs the notebook (default: you)
--   wh       warehouse the notebook uses
--   dbt_role default role of the dbt Cloud service user (DESC USER <user>), or '' to skip dbt grants
--
-- Why ACCOUNTADMIN: granting IMPORTED PRIVILEGES on SNOWFLAKE_SAMPLE_DATA needs it.
-- OSSIE_TABLEAU is created here and OWNERSHIP moves to OSSIE_QS_ROLE
-- (COPY CURRENT GRANTS keeps existing grants), so nothing is left owned by ACCOUNTADMIN.
-- No warehouse or user is created; an existing warehouse is only granted USAGE.
-- OSSIE_QS_ROLE is granted to SYSADMIN so admins can manage its objects.
-- https://docs.snowflake.com/en/sql-reference/sql/grant-ownership

SET qs_user = CURRENT_USER();
SET wh = 'COMPUTE_WH';
SET dbt_role = 'DBT_ROLE';

USE ROLE ACCOUNTADMIN;

CREATE ROLE IF NOT EXISTS OSSIE_QS_ROLE;
GRANT ROLE OSSIE_QS_ROLE TO ROLE SYSADMIN;
GRANT ROLE OSSIE_QS_ROLE TO USER IDENTIFIER($qs_user);
GRANT USAGE ON WAREHOUSE IDENTIFIER($wh) TO ROLE OSSIE_QS_ROLE;
GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA TO ROLE OSSIE_QS_ROLE;

CREATE DATABASE IF NOT EXISTS OSSIE_TABLEAU;
CREATE SCHEMA IF NOT EXISTS OSSIE_TABLEAU.OSSIE_QS;
CREATE SCHEMA IF NOT EXISTS OSSIE_TABLEAU.OSSIE_QS_DBT;
GRANT OWNERSHIP ON ALL SCHEMAS IN DATABASE OSSIE_TABLEAU TO ROLE OSSIE_QS_ROLE COPY CURRENT GRANTS;
GRANT OWNERSHIP ON DATABASE OSSIE_TABLEAU TO ROLE OSSIE_QS_ROLE COPY CURRENT GRANTS;

-- dbt Cloud service role: write to OSSIE_QS_DBT only. FUTURE grants keep access
-- when the notebook cleanup drops and recreates the schema.
-- (SELECT on SNOWFLAKE_SAMPLE_DATA must also be held by this role.)
GRANT USAGE ON DATABASE OSSIE_TABLEAU TO ROLE IDENTIFIER($dbt_role);
GRANT USAGE, CREATE TABLE, CREATE VIEW ON SCHEMA OSSIE_TABLEAU.OSSIE_QS_DBT TO ROLE IDENTIFIER($dbt_role);
GRANT USAGE, CREATE TABLE, CREATE VIEW ON FUTURE SCHEMAS IN DATABASE OSSIE_TABLEAU TO ROLE IDENTIFIER($dbt_role);

SHOW GRANTS TO ROLE OSSIE_QS_ROLE;
