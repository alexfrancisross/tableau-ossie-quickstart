{{ config(materialized="view") }}

select
    o_orderkey,
    o_custkey,
    o_orderdate
from {{ source("tpch_sf1", "ORDERS") }}
