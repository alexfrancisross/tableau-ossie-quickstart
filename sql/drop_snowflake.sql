-- Drops the quickstart schema (semantic view) as OSSIE_QS_ROLE. Does not touch
-- SNOWFLAKE_SAMPLE_DATA. Step 8 of the notebook drops OSSIE_QS_DBT separately;
-- the database and role stay (full reset: sql/teardown_snowflake.sql as ACCOUNTADMIN).
-- https://docs.snowflake.com/en/sql-reference/sql/drop-schema

DROP SCHEMA IF EXISTS OSSIE_TABLEAU.OSSIE_QS CASCADE;
