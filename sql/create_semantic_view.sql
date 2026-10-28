-- Native CREATE SEMANTIC VIEW over SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.
-- Logical tables, keys, and relationships match the Ossie YAML.
--
-- NOTE: Do not replace this with SYSTEM$CREATE_SEMANTIC_VIEW_FROM_OSSIE_YAML.
-- ossie-tableau convert writes Ossie 0.2.0.dev0. The procedure currently
-- accepts 0.1.1 only.
-- https://docs.snowflake.com/en/sql-reference/sql/create-semantic-view
-- https://docs.snowflake.com/en/sql-reference/stored-procedures/system_create_semantic_view_from_ossie_yaml

-- Schemas first: the database comes from sql/setup_snowflake.sql, and a notebook cleanup may have dropped them.
-- OSSIE_QS_DBT is where dbt Cloud writes its models; IF NOT EXISTS keeps a schema the dbt role already owns.
CREATE SCHEMA IF NOT EXISTS OSSIE_TABLEAU.OSSIE_QS;
CREATE SCHEMA IF NOT EXISTS OSSIE_TABLEAU.OSSIE_QS_DBT;

CREATE OR REPLACE SEMANTIC VIEW OSSIE_TABLEAU.OSSIE_QS.SV_01_TPCH

  TABLES (
    LINEITEM AS SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.LINEITEM
      PRIMARY KEY (L_ORDERKEY, L_LINENUMBER),
    ORDERS AS SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS
      PRIMARY KEY (O_ORDERKEY),
    CUSTOMER AS SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER
      PRIMARY KEY (C_CUSTKEY),
    NATION AS SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.NATION
      PRIMARY KEY (N_NATIONKEY),
    PART AS SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.PART
      PRIMARY KEY (P_PARTKEY),
    SUPPLIER AS SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.SUPPLIER
      PRIMARY KEY (S_SUPPKEY)
  )

  RELATIONSHIPS (
    LINEITEM_ORDERS AS
      LINEITEM (L_ORDERKEY) REFERENCES ORDERS,
    ORDERS_CUSTOMER AS
      ORDERS (O_CUSTKEY) REFERENCES CUSTOMER,
    CUSTOMER_NATION AS
      CUSTOMER (C_NATIONKEY) REFERENCES NATION,
    LINEITEM_PART AS
      LINEITEM (L_PARTKEY) REFERENCES PART,
    LINEITEM_SUPPLIER AS
      LINEITEM (L_SUPPKEY) REFERENCES SUPPLIER
  )

  FACTS (
    LINEITEM.discounted_price AS L_EXTENDEDPRICE * (1 - L_DISCOUNT)
  )

  DIMENSIONS (
    CUSTOMER.C_CUSTKEY AS C_CUSTKEY,
    CUSTOMER.C_MKTSEGMENT AS C_MKTSEGMENT,
    CUSTOMER.C_NAME AS C_NAME,
    NATION.N_NAME AS N_NAME,
    LINEITEM.L_RETURNFLAG AS L_RETURNFLAG,
    LINEITEM.L_SHIPMODE AS L_SHIPMODE,
    ORDERS.O_ORDERDATE AS O_ORDERDATE,
    PART.P_BRAND AS P_BRAND
  )

  METRICS (
    LINEITEM.total_revenue AS SUM(LINEITEM.discounted_price),
    -- Distinct customers on the line-item join, not every CUSTOMER row.
    PRIVATE LINEITEM.customers_with_lines AS COUNT(DISTINCT CUSTOMER.C_CUSTKEY),
    revenue_per_customer AS LINEITEM.total_revenue / LINEITEM.customers_with_lines
  );
