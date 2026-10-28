{{ config(materialized="view") }}

select
    c_custkey
from {{ source("tpch_sf1", "CUSTOMER") }}
