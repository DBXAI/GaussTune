"""Static feature/FK scopes, not learned models or query-specific corrections."""

MODEL_SCOPES = {
    "lineitem_oc": {
        "features": [
            ["customer.c_mktsegment", "category"], ["orders.o_orderdate", "date"],
            ["lineitem.l_shipdate", "date"], ["lineitem.l_returnflag", "category"],
            ["customer.c_nationkey", "category"],
        ],
        "required": ["lineitem"],
        "allowed": ["lineitem", "orders", "customer", "nation"],
    },
    "lineitem_ps": {
        "features": [
            ["lineitem.l_shipdate", "date"], ["lineitem.l_quantity", "number"],
            ["lineitem.l_returnflag", "category"], ["part.p_brand", "category"],
            ["part.p_size", "number"], ["supplier.s_nationkey", "category"],
        ],
        "required": ["lineitem"],
        "allowed": ["lineitem", "part", "supplier", "nation"],
    },
    "orders_c": {
        "features": [
            ["customer.c_mktsegment", "category"], ["orders.o_orderdate", "date"],
            ["customer.c_nationkey", "category"], ["orders.o_orderstatus", "category"],
        ],
        "required": ["orders"],
        "allowed": ["orders", "customer", "nation"],
    },
    "customer_n": {
        "features": [
            ["customer.c_mktsegment", "category"], ["customer.c_nationkey", "category"],
        ],
        "required": ["customer"],
        "allowed": ["customer", "nation"],
    },
}
