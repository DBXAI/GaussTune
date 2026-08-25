SELECT avg(projected_width), count(*)
FROM (
  SELECT pg_column_size(s_acctbal) + pg_column_size(s_name)
       + pg_column_size(n_name) + pg_column_size(p_partkey)
       + pg_column_size(p_mfgr) + pg_column_size(s_address)
       + pg_column_size(s_phone) + pg_column_size(s_comment) AS projected_width
  FROM supplier CROSS JOIN nation CROSS JOIN part
  LIMIT 1000
) AS samples;
