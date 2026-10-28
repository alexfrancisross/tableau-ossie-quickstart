-- Full reset: removes everything sql/setup_snowflake.sql created. Run as ACCOUNTADMIN.
-- Drops the OSSIE_TABLEAU database (all schemas, the semantic view, dbt tables)
-- and OSSIE_QS_ROLE. SNOWFLAKE_SAMPLE_DATA is not touched. Rerun setup_snowflake.sql afterwards.
-- https://docs.snowflake.com/en/sql-reference/sql/drop-database

USE ROLE ACCOUNTADMIN;
DROP DATABASE IF EXISTS OSSIE_TABLEAU;
DROP ROLE IF EXISTS OSSIE_QS_ROLE;
