SELECT avg(projected_width), count(*)
FROM (
  SELECT pg_column_size(n_name)
       + pg_column_size(extract(year FROM o_orderdate))
       + pg_column_size(l_extendedprice * (1 - l_discount)
                        - ps_supplycost * l_quantity) AS projected_width
  FROM nation CROSS JOIN orders CROSS JOIN lineitem CROSS JOIN partsupp
  LIMIT 1000
) AS samples;
