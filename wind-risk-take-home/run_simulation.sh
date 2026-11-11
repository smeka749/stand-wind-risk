#!/usr/bin/env bash
set -o pipefail

# MPI rank count. Defaults to the SME setting; SIM_NP overrides it for
# experiments (the job's slot size stays separate).
NP="${SIM_NP:-16}"
MPIRUN_OPTS="--oversubscribe --allow-run-as-root"

if [ $# -ne 1 ]; then
    echo "usage: $0 CASE_DIR" >&2
    exit 2
fi

CASE_DIR="$1"
if [ ! -d "$CASE_DIR" ]; then
    echo "case dir not found: $CASE_DIR" >&2
    exit 2
fi

cd "$CASE_DIR"

# Keep the decomposition in step with the rank count.
sed -i -E "s/^(numberOfSubdomains[[:space:]]+)[0-9]+;/\1${NP};/" system/decomposeParDict

echo "=== blockMesh ==="
if ! blockMesh 2>&1 | tee log.blockMesh; then
    echo "blockMesh failed" >&2
    exit 3
fi

echo "=== surfaceFeatureExtract ==="
if ! surfaceFeatureExtract 2>&1 | tee log.surfaceFeatureExtract; then
    echo "surfaceFeatureExtract failed" >&2
    exit 3
fi

echo "=== decomposePar (background mesh) ==="
#rm -rf processor*
find . -maxdepth 1 -type d -name 'processor*' -exec rm -rf {} +

if ! decomposePar -force 2>&1 | tee log.decomposePar.pre; then
    echo "decomposePar failed" >&2
    exit 3
fi

echo "=== snappyHexMesh -parallel (NP=${NP}) ==="
if ! mpirun -np "$NP" $MPIRUN_OPTS snappyHexMesh -overwrite -parallel 2>&1 | tee log.snappyHexMesh; then
    echo "snappyHexMesh failed" >&2
    exit 3
fi

echo "=== reconstructParMesh ==="
if ! reconstructParMesh -constant 2>&1 | tee log.reconstructParMesh; then
    echo "reconstructParMesh failed" >&2
    exit 3
fi

echo "=== checkMesh -allGeometry ==="
checkMesh -allGeometry 2>&1 | tee log.checkMesh
if grep -qE "Number of negative volume cells: [1-9]|Zero or negative face area detected" log.checkMesh; then
    echo "checkMesh: mesh has degenerate cells (snappy collapsed geometry)" >&2
    exit 2
fi

echo "=== decomposePar (redecompose for solver) ==="
#rm -rf processor*
find . -maxdepth 1 -type d -name 'processor*' -exec rm -rf {} +

if ! decomposePar -force 2>&1 | tee log.decomposePar.solve; then
    echo "decomposePar (post-mesh) failed" >&2
    exit 3
fi

echo "=== simpleFoam -parallel (NP=${NP}) ==="
mpirun -np "$NP" $MPIRUN_OPTS simpleFoam -parallel 2>&1 | tee log.simpleFoam
SF_EXIT=${PIPESTATUS[0]}
if [ "$SF_EXIT" -ne 0 ]; then
    echo "simpleFoam exited $SF_EXIT" >&2
    exit "$SF_EXIT"
fi

echo "=== reconstructPar ==="
if ! reconstructPar 2>&1 | tee log.reconstructPar; then
    echo "reconstructPar failed" >&2
    exit 3
fi

if ! grep -q "solution converged" log.simpleFoam; then
    LAST_TIME=$(awk '/^Time = / {t=$3} END {print t}' log.simpleFoam)
    echo "solution did not converge after ${LAST_TIME} iterations" >&2
    exit 1
fi

touch case.foam
echo "=== DONE ==="
