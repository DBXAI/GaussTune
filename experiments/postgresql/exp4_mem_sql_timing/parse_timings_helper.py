#!/usr/bin/env python3
import sys, re
raw = open(sys.argv[1]).read()
starts = list(re.finditer(r'QUERY_START (\S+)', raw))
times  = list(re.finditer(r'Time:\s+([\d.]+)\s+ms', raw))
for s in starts:
    name = s.group(1)
    t = next((m for m in times if m.start() > s.end()), None)
    if t:
        print(f"{name}\t{float(t.group(1)):.3f}")
