SELECT avg(pg_column_size(o_custkey) + pg_column_size(o_orderkey)), count(*)
FROM (SELECT o_custkey, o_orderkey FROM orders LIMIT 1000) AS samples;
