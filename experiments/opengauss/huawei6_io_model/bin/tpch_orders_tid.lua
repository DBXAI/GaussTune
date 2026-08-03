-- Random physical-page TP reads over the 18GB SF85 orders table.
-- Every event uses a Tid Scan, so the working set can exceed shared_buffers.
sysbench.cmdline.options = {
  tpch_orders_blocks = {"Number of orders heap blocks", 2359296},
  tpch_rows_per_block = {"Candidate tuple offsets per heap block", 64}
}

function thread_init()
  drv = sysbench.sql.driver()
  con = drv:connect()
end

function thread_done()
  con:disconnect()
end

function event()
  local block = sysbench.rand.uniform(0, sysbench.opt.tpch_orders_blocks - 1)
  local offset = sysbench.rand.uniform(1, sysbench.opt.tpch_rows_per_block)
  con:query(string.format(
    "SELECT o_totalprice FROM orders WHERE ctid = '(%d,%d)'", block, offset
  ))
end
