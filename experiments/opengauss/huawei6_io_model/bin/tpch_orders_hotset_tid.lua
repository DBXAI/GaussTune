-- Random physical-page TP reads from a fixed hot set in the SF85 orders table.
-- The default 6GiB set is deliberately larger than a 4GiB shared_buffers
-- pool (524288 8KiB pages) and smaller than an 8GiB pool (1048576 pages).
-- Sustained AP scans can evict the Linux page-cache copy; only the larger
-- database buffer can then protect the repeatedly-read TP pages.
sysbench.cmdline.options = {
  tpch_orders_hotset_base_block = {"First orders heap block in TP hot set", 0},
  tpch_orders_hotset_blocks = {"Number of orders heap blocks in TP hot set", 786432},
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
  local block = sysbench.opt.tpch_orders_hotset_base_block +
    sysbench.rand.uniform(0, sysbench.opt.tpch_orders_hotset_blocks - 1)
  local offset = sysbench.rand.uniform(1, sysbench.opt.tpch_rows_per_block)
  con:query(string.format(
    "SELECT o_totalprice FROM orders WHERE ctid = '(%d,%d)'", block, offset
  ))
end
