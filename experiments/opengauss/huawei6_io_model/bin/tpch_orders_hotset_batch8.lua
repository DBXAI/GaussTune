-- A TP transaction comprising eight independent hot-set page reads.
-- Keeping the 6GiB physical hot set but increasing reads per transaction makes
-- AP-induced miss latency observable at a practical TP offered rate.
sysbench.cmdline.options = {
  tpch_orders_hotset_base_block = {"First orders heap block in TP hot set", 0},
  tpch_orders_hotset_blocks = {"Number of orders heap blocks in TP hot set", 786432},
  tpch_rows_per_block = {"Candidate tuple offsets per heap block", 64},
  tpch_reads_per_event = {"Random Tid reads per TP transaction", 8}
}

function thread_init()
  drv = sysbench.sql.driver()
  con = drv:connect()
end

function thread_done()
  con:disconnect()
end

function event()
  for _ = 1, sysbench.opt.tpch_reads_per_event do
    local block = sysbench.opt.tpch_orders_hotset_base_block +
      sysbench.rand.uniform(0, sysbench.opt.tpch_orders_hotset_blocks - 1)
    local offset = sysbench.rand.uniform(1, sysbench.opt.tpch_rows_per_block)
    con:query(string.format(
      "SELECT o_totalprice FROM orders WHERE ctid = '(%d,%d)'", block, offset
    ))
  end
end
