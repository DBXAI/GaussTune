# S5 Hot-Set SB Validation

## Purpose

This is a separate S5 action validation.  It does not reuse an observed TPS
value to choose `shared_buffers`.

The prior retained-AP run used a TP working set that both 4GB and 8GB SB could
not retain, while Linux page cache still provided a substantial compensation.
It therefore could not establish an S5 "raise SB" recommendation.

## Blinded replay input

`tpch_orders_hotset_tid.lua` reads uniformly from 786432 physical 8KiB pages
of SF85 `orders`, a 6GiB TP hot set.  The candidate capacities are:

| Candidate | Buffer pages | TP hot-set coverage |
| --- | ---: | ---: |
| 4GB | 524288 | 66.67% |
| 8GB | 1048576 | 100.00% |

The replay is written before opening either candidate TPS result.  It assumes
zero Linux-cache credit only after sustained AP scanning has been observed:
six Q3 sessions at 256MB begin in S3 and continue through S5.  The AP sessions
are never cancelled; the process waits for their natural completion after the
five scored windows.

The initial coverage-only replay was intentionally retained as a negative
control.  Its 8GB direction was falsified by the first 4GB run: the 4,000 TPS
protected stream still had sufficient terminal capacity despite 4GB's lower
buffer coverage.  The current replay additionally transforms each candidate's
physical miss probability into terminal capacity using an AP I/O service-time
anchor.  It selects the *smallest* candidate in the predicted TPS plateau.
Consequently it may correctly return 4GB/8GB as a tie; it is no longer allowed
to select 8GB from hit rate alone.

## Validation rule

Run the same S1--S5 schedule after a clean restart for 4GB and 8GB.  The
protected 4,000 TPS stream stays alive across all stages, and S5 adds an
independent 4,000 TPS surge stream.  Compare only the stable S5 window after
the surge settles.  Accept the prediction only when all of these hold:

1. both candidates retain the same number of running AP statements;
2. no AP statement is cancelled or fails;
3. 8GB has a material protected-TP TPS gain over 4GB, not merely noise; and
4. the observed I/O pressure is present during the scored S5 interval.

The calibration file for this microbenchmark records offered-rate stability,
not the five-stage CPU envelope.  It must not be cited as proof that the
original CPU-saturation acceptance workload is met.
