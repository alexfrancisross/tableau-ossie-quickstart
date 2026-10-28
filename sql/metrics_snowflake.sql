-- Same two TPC-H metrics as the Ossie YAML / MSI / Power BI measures.
-- Statement 1 (notebook step 1): live query against SNOWFLAKE_SAMPLE_DATA.TPCH_SF1, no local copy.
-- Statement 2 (notebook step 4): the same metrics through the semantic view, which must agree.
-- Tableau's AGG() is not Snowflake SQL; SEMANTIC_VIEW() is the equivalent.
-- https://docs.snowflake.com/en/sql-reference/constructs/semantic_view

SELECT
  SUM(L_EXTENDEDPRICE * (1 - L_DISCOUNT)) AS total_revenue,
  SUM(L_EXTENDEDPRICE * (1 - L_DISCOUNT)) / COUNT(DISTINCT C_CUSTKEY) AS revenue_per_customer
FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.LINEITEM
JOIN SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS
  ON L_ORDERKEY = O_ORDERKEY
JOIN SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER
  ON O_CUSTKEY = C_CUSTKEY;

SELECT *
FROM SEMANTIC_VIEW(
  OSSIE_TABLEAU.OSSIE_QS.SV_01_TPCH
  METRICS total_revenue, revenue_per_customer
);
