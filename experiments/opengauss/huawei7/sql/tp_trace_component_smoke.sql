-- Bounded real-buffer activity for validating the trace plumbing only.
-- This is not a substitute for either final sysbench or BenchBase evidence.
SET application_name = 'sysbench_tp_trace_smoke';

DO $$
DECLARE
    i integer;
    key_id integer;
    payload text;
BEGIN
    FOR i IN 1..2400 LOOP
        -- A deliberately bounded hot set makes the trace's finite warmup able
        -- to reconstruct the real initial cache state.  It validates event
        -- completeness/state transitions only; it is not calibration input.
        key_id := 1 + ((i * 7919) % 64);
        SELECT c INTO payload FROM sbtest1 WHERE id = key_id;
        PERFORM pg_sleep(0.01);
    END LOOP;
END
$$;
