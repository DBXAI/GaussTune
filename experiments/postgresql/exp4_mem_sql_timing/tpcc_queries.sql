-- tpcc_queries.sql
-- TPC-C 代表性只读查询，用于测量不同 shared_buffers 下的 SQL 执行时间
-- 选取 StockLevel / OrderStatus / 聚合统计等读密集查询

-- Q_STOCK: StockLevel 核心查询（stock + order_line join，TP 热点）
\echo 'QUERY_START STOCK_LEVEL'
\timing on
SELECT count(DISTINCT s_i_id) AS low_stock
FROM stock, order_line
WHERE s_w_id = 1
  AND s_quantity < 15
  AND ol_w_id = 1
  AND ol_d_id = 1
  AND ol_o_id BETWEEN (
      SELECT max(d_next_o_id) - 20
      FROM district
      WHERE d_w_id = 1 AND d_id = 1
  ) AND (
      SELECT max(d_next_o_id)
      FROM district
      WHERE d_w_id = 1 AND d_id = 1
  )
  AND ol_i_id = s_i_id;
\timing off
\echo 'QUERY_END STOCK_LEVEL'

-- Q_ORDER_STATUS: 查询客户最近订单（customer + oorder + order_line join）
\echo 'QUERY_START ORDER_STATUS'
\timing on
SELECT c_id, c_first, c_last, c_balance,
       o_id, o_entry_d, o_carrier_id,
       ol_i_id, ol_supply_w_id, ol_quantity, ol_amount, ol_delivery_d
FROM customer
JOIN oorder ON o_w_id = c_w_id AND o_d_id = c_d_id AND o_c_id = c_id
JOIN order_line ON ol_w_id = o_w_id AND ol_d_id = o_d_id AND ol_o_id = o_id
WHERE c_w_id = 1 AND c_d_id = 1
  AND o_id = (
      SELECT max(o_id) FROM oorder
      WHERE o_w_id = 1 AND o_d_id = 1 AND o_c_id = c_id
  )
ORDER BY c_id
LIMIT 100;
\timing off
\echo 'QUERY_END ORDER_STATUS'

-- Q_WAREHOUSE_SUMMARY: 跨 warehouse 聚合（全表扫描 order_line，AP 风格）
\echo 'QUERY_START WAREHOUSE_SUMMARY'
\timing on
SELECT
    ol_w_id,
    ol_d_id,
    count(*)                    AS order_count,
    sum(ol_amount)              AS total_amount,
    avg(ol_amount)              AS avg_amount,
    min(ol_delivery_d)          AS earliest_delivery,
    max(ol_delivery_d)          AS latest_delivery
FROM order_line
GROUP BY ol_w_id, ol_d_id
ORDER BY ol_w_id, ol_d_id;
\timing off
\echo 'QUERY_END WAREHOUSE_SUMMARY'

-- Q_CUSTOMER_BALANCE: 高消费客户排名（customer 全扫 + 排序）
\echo 'QUERY_START CUSTOMER_BALANCE'
\timing on
SELECT
    c_w_id,
    c_d_id,
    c_id,
    c_first,
    c_last,
    c_balance
FROM customer
WHERE c_balance > 0
ORDER BY c_balance DESC
LIMIT 50;
\timing off
\echo 'QUERY_END CUSTOMER_BALANCE'

-- Q_STOCK_SCAN: stock 全表扫描（3.6GB 表，最能体现 buffer size 影响）
\echo 'QUERY_START STOCK_SCAN'
\timing on
SELECT
    s_w_id,
    count(*)            AS items,
    avg(s_quantity)     AS avg_qty,
    sum(s_ytd)          AS total_ytd,
    sum(s_order_cnt)    AS total_orders
FROM stock
GROUP BY s_w_id
ORDER BY s_w_id;
\timing off
\echo 'QUERY_END STOCK_SCAN'
