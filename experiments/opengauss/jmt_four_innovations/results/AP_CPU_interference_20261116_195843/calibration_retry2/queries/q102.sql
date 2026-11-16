SELECT count(*),sum(o_totalprice)
            FROM orders WHERE o_orderdate>=DATE '1993-01-01'
