SELECT avg(pg_column_size(s_name) + pg_column_size(s_suppkey)), count(*)
FROM (SELECT s_name, s_suppkey FROM supplier LIMIT 1000) AS samples;
