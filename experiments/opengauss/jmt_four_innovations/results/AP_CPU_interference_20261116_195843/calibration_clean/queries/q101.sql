SELECT count(*),sum(l_extendedprice*l_discount),sum(l_quantity),
            sum(length(md5(l_comment||l_partkey::text))),
            sum(length(md5(l_comment||l_orderkey::text))),
            sum(length(md5(l_comment||l_suppkey::text))) FROM lineitem
