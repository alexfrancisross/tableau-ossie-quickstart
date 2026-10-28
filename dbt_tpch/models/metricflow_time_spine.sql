{{ config(materialized="table") }}

{# TPC-H SF1 ship/order dates are 1992-01-01 through 1998-12-31. #}
select dateadd(day, seq4(), '1992-01-01'::date) as date_day
from table(generator(rowcount => 2557))
