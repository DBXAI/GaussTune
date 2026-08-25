SELECT avg(projected_width), count(*)
FROM (
  SELECT pg_column_size(c_name) + pg_column_size(c_custkey)
       + pg_column_size(o_orderkey) + pg_column_size(o_orderdate)
       + pg_column_size(o_totalprice) + pg_column_size(l_quantity)
       AS projected_width
  FROM customer CROSS JOIN orders CROSS JOIN lineitem
  LIMIT 1000
) AS samples;
