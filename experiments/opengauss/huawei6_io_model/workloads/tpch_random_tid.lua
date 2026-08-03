-- Disk-resident random-page transaction workload for causal I/O/TPS tests.
-- The lineitem heap is larger than RAM; a TID scan issues one random page read
-- without requiring an index or changing the source dataset.

local drv
local con

function thread_init()
  drv = sysbench.sql.driver()
  con = drv:connect()
end

function thread_done()
  con:disconnect()
end

function event()
  local block = sysbench.rand.uniform(0, 10580000)
  con:query(string.format(
    "SELECT l_orderkey FROM lineitem WHERE ctid = '(%d,1)'::tid LIMIT 1",
    block
  ))
end
