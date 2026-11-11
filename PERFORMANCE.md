# Performance log

## 1. Parallel S3 transfers; skip `processor*` 

**Finding:** meshing (7.6–10.8 s) and the solver (6–11 s) were a small part of
the 87–110 s simulation stage. Each case was 1,646 S3 objects, copied one at a
time, and 1,504 of them were `processor*` folders: the per-rank pieces left
after `reconstructPar`, which neither `score.py` nor `render.py` reads.

**Change:** `upload_dir` / `download_dir` use a 32-thread pool (boto3
connection pool sized to match); the simulation uploads the case without
`processor*`.

| Measurement | Before | After |
|---|---|---|
| Objects per case | 1,646 | 142–159 |
| Case size | 156 MiB | 78–138 MB |
| Simulation: download + upload | most of 87–110 s | 0.4 s + 1.1–1.3 s |
| PostProcess: download | sequential, ~1,646 files | 0.9–1.1 s |
| S3 requests per month (1,000 quotes a day) | $270 | $27 |

Scores unchanged: Lantana 77.68, Atherton 63.98.

**What it revealed:** PostProcess is now download 1 s, score 0.5 s,
**render 180–278 s**, upload 0.3 s. Rendering the 300-frame video 
is 99% of the stage, and the score waits for it.

## Experiment: 8 vs 16 solver processes (not deployed)

**Why:** the reference meshes are 81k–123k cells, 5–8k cells per core at 16
cores, below the ~10k where OpenFOAM usually stops scaling.

**How:** `SIMULATION_NP=8` (MPI ranks for snappyHexMesh and simpleFoam), same
c6i.4xlarge and 16-vCPU slot, so only the process count changed. Both reference
addresses quoted again with new spellings; production stays at 16.

| | Lantana 16 | Lantana 8 | Atherton 16 | Atherton 8 |
|---|---|---|---|---|
| Mesh cells | 80,966 | 80,965 | 122,601 | 122,653 |
| Cells per core | 5.1k | 10.1k | 7.7k | 15.3k |
| Meshing | 7.6 s | 6.3 s | 10.8 s | 9.0 s |
| Solver iterations | 195 | 191 | 238 | 237 |
| Solver | 6.0 s | 6.2 s | 10.8 s | 11.6 s |
| Meshing + solve | 13.5 s | 12.5 s | 21.6 s | 20.6 s |
| Score | 77.68 | 77.67 | 63.98 | 63.75 |

**Result:** 8 processes are as fast as 16 (meshing ~17% faster, solver within
1 s), and scores moved by at most 0.23 (Atherton miss vs reference 0.99, within
±2). An 8-vCPU slot would fit two simulations per instance at the same speed,
roughly halving simulation EC2 cost.

