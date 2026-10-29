# ossie_qs_tpch

Minimal dbt project for the Tableau Ossie quickstart Semantic Layer step.

Staging views read `SNOWFLAKE_SAMPLE_DATA.TPCH_SF1` (`LINEITEM`, `ORDERS`, `CUSTOMER`). MetricFlow YAML defines `total_revenue` (sum of discounted extended price) and `revenue_per_customer` (that sum over distinct customers).

Connect a dbt Cloud project with subdirectory `dbt_tpch`. This repo names that project `ossie-qs`. Some dbt plans allow only one active project; in that case point the existing project at this subdirectory (or add these models to that project) instead of creating a second one. The Cloud Snowflake role needs `SELECT` on the sample tables and `CREATE` in the environment schema (for example `OSSIE_TABLEAU.OSSIE_QS_DBT`; `sql/setup_snowflake.sql` grants it). Use a **deployment** environment, then a job that runs:

```
dbt run --select stg_* metricflow_time_spine
dbt parse
```

`OssieToMSIConverter` JSON in `out/semantic_manifest.json` is not this project. It is an illustration of what the converter emitted; Cloud parses the YAML here.
