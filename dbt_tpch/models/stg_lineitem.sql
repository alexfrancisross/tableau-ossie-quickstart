{{ config(materialized="view") }}

select
    l_orderkey,
    l_linenumber,
    l_extendedprice,
    l_discount,
    l_shipdate
from {{ source("tpch_sf1", "LINEITEM") }}
