# Wind Risk Pipeline

Accepts an address and returns a wind risk score plus a video of the
simulation, by running the SME's three stages (preprocess, OpenFOAM
simulation, postprocess) as a pipeline.

```
Client -> API (FastAPI) -> Postgres (quote state: phase / status)
                              ^
        PreProcess -> Simulation -> PostProcess    (stage workers)
        each worker atomically claims the next eligible quote, runs its
        SME script, writes artifacts, and marks the stage Success/Fail
```

## Layout

| Path | Contents |
|---|---|
| `service/app/` | API (`main.py`), quote store and state machine (`store.py`), worker loop (`worker.py`), one runner per stage, S3 helpers |
| `service/db/schema.sql` | Postgres schema |
| `service/infra/` | Terraform for one AWS environment |
| `service/tests/` | Unit and integration tests |
| `service/scripts/check_reference_scores.py` | ±2 reference-score gate (runs against the API with `--api`) |
| `service/Dockerfile.*` | One image per role: `api`, `preprocess`, `openfoam` (simulation), `postprocess` |
| `wind-risk-take-home/` | SME code (one change, see below) |
| `.github/workflows/` | CI (tests, Terraform validate, API image build), push-to-deploy, manual OpenFOAM base build |

## API

| Method | Path | Returns |
|---|---|---|
| `POST` | `/quotes` `{"address": "..."}` | `202` and the quote. Idempotent: the same normalized address returns the existing quote. A repeat POST also retries a failed quote and restarts a quote waiting for its next stage. |
| `GET` | `/quotes/{id}` | `phase`, `status`, `score`, `video_url`, `error`, `retry_count`, `waiting_for`, `eta_seconds`, `history` |
| `GET` | `/health` | `{"ok": true}` |

`history` lists each stage attempt (outcome, duration, image tag that ran it).
`eta_seconds` is a rough estimate: the median of recent successful runs for
each remaining stage, minus time already spent in the running one.

`phase` moves `Requested -> PreProcess -> Simulation -> PostProcess -> Quoted`;
`status` is `Running`, `Success` or `Fail` for the current phase. A failed
stage shows its error; POSTing the same address again retries it. `video_url` is a presigned S3 link
created on each read (never stored); it is null when running without S3.

## Run locally (Docker)

From the repo root:

```bash
docker compose up --build -d db api
curl -X POST localhost:8000/quotes -H 'Content-Type: application/json' \
  -d '{"address": "1265 Lands End Rd, Lantana, FL 33462"}'
docker compose run --rm preprocess
docker compose run --rm simulation
docker compose run --rm postprocess
curl localhost:8000/quotes/<quote_id>
```

Each worker container processes one quote and exits. The simulation image is
`linux/amd64`; on Apple Silicon it runs under emulation, so timings and
Atherton's score are not representative. Use the AWS run for the reference check.

## Tests

Postgres is the only store, so the tests run against a real database (CI
uses a Postgres service container). From the repo root:

```bash
docker compose up -d db
cd service
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
TEST_DATABASE_URL=postgresql://windrisk:windrisk@localhost:5432/windrisk python -m pytest tests/ -q
```

CI runs Python 3.12; any 3.10+ works.

Without `TEST_DATABASE_URL`, tests that need the store fail with a message
saying how to start the database, rather than skipping.

## Deploy (AWS)

`service/infra/main.tf` defines one environment: the API on ECS Fargate behind an
ALB, RDS Postgres, S3, and AWS Batch (PreProcess and PostProcess on Fargate,
Simulation on a 16-vCPU on-demand EC2 `c6i.4xlarge`). A new AWS account may
need an EC2 vCPU quota increase for that instance.

**A. Use the running deployment.** The API URL is shared with the submission.
An address that already ran returns its finished quote; a new address takes
about 12 minutes.

**B. Your AWS account, via GitHub Actions.** Push the code to your own GitHub repo, then:

1. `terraform -chdir=bootstrap init && terraform -chdir=bootstrap apply -var 'github_repos=["<owner>/<repo>"]' -var 'budget_email=<email>'`
   (state bucket, lock table, budget, deploy role). If the deploy later fails with
   `Not authorized to perform sts:AssumeRoleWithWebIdentity`, also pass
   `github_repo_ids` (the subject in CloudTrail's `AssumeRoleWithWebIdentity` event).
2. In your GitHub repo, under Settings > Secrets and variables > Actions >
   Variables (or with `gh variable set`), set `AWS_ROLE_ARN` and
   `TF_STATE_BUCKET` (the bootstrap's `deploy_role_arn` and `state_bucket`
   outputs) and `AWS_REGION`. `SIMULATION_BASE_TAG` is set in step 4; optional
   overrides are listed in `deploy.yml`.
3. Create the ECR repositories:

   ```bash
   terraform -chdir=service/infra init \
     -backend-config=bucket=$TF_STATE_BUCKET -backend-config=region=$AWS_REGION
   terraform -chdir=service/infra apply \
     -target=aws_ecr_repository.api -target=aws_ecr_repository.preprocess \
     -target=aws_ecr_repository.simulation -target=aws_ecr_repository.postprocess
   ```

4. Run the `docker-openfoam` workflow (Actions tab), then, in the same place as
   step 2 (Settings > Secrets and variables > Actions > Variables, or with
   `gh variable set`), set `SIMULATION_BASE_TAG` to the `base-<sha>` tag it
   prints in its run summary. Every push to `main` then deploys.

**C. Your AWS account, from a laptop** (your own AWS credentials, no GitHub):

```bash
export AWS_REGION=us-west-2 TAG=local-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REG=$ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com

terraform -chdir=bootstrap init
terraform -chdir=bootstrap apply -var 'github_repos=["none/none"]' -var 'budget_email=<email>'
terraform -chdir=service/infra init \
  -backend-config=bucket=wind-risk-tfstate-$ACCOUNT -backend-config=region=$AWS_REGION
terraform -chdir=service/infra apply \
  -target=aws_ecr_repository.api -target=aws_ecr_repository.preprocess \
  -target=aws_ecr_repository.simulation -target=aws_ecr_repository.postprocess

aws ecr get-login-password | docker login --username AWS --password-stdin $REG
for img in api preprocess postprocess; do
  docker buildx build --platform linux/amd64 -f service/Dockerfile.$img \
    -t $REG/wind-risk-prod-$img:$TAG --push .
done
# Builds OpenFOAM: 10-15 min on x86, much longer on Apple Silicon.
docker buildx build --platform linux/amd64 -f service/Dockerfile.openfoam \
  -t $REG/wind-risk-prod-simulation:$TAG --push .

TF_VAR_image_tag=$TAG terraform -chdir=service/infra apply
```

**Request a quote** (any option):

```bash
API=<API URL>   # B and C: terraform -chdir=service/infra output -raw api_url
curl -X POST $API/quotes -H 'Content-Type: application/json' \
  -d '{"address": "1265 Lands End Rd, Lantana, FL 33462"}'
curl $API/quotes/<quote_id>
```

**Tear down:** empty the artifact bucket and ECR repositories, then
`terraform -chdir=service/infra destroy`, then the same in `bootstrap/`.

## Configuration

| Variable | Purpose |
|---|---|
| `DATABASE_URL`, or `DB_HOST` `DB_PORT` `DB_NAME` `DB_USER` `DB_PASSWORD` | Postgres connection |
| `S3_BUCKET` | Store artifacts in S3 and mint presigned video URLs. Unset: local paths |
| `SME_DIR` | Location of the SME scripts (`/opt/sme` in the images) |
| `PREPROCESS_WORK_ROOT` | Local working directory for case files |
| `BATCH_QUEUE_<STAGE>`, `BATCH_JOBDEF_<STAGE>` | Where to submit each stage's job (set by Terraform). Unset: nothing is submitted (local mode) |
| `PREPROCESS_TIMEOUT_SECONDS`, `SIMULATION_TIMEOUT_SECONDS`, `SCORE_TIMEOUT_SECONDS`, `RENDER_TIMEOUT_SECONDS` | Per-script timeouts (defaults 1500, 1500, 300, 1500) |
| `LEASE_SECONDS` | Claim lease (default 1800). Must exceed the stage timeout |
| `LOG_LEVEL` | Log level. Logs are JSON lines (`stage_started`, `stage_succeeded`, `stage_failed`) with per-stage durations |

## Reference scores

Scores must be within ±2 of the SME's `wind-risk-take-home/reference/*.json`. Against a running API
(local or deployed), this submits both reference addresses, plus any extras,
waits up to 30 minutes, and checks the scores:

```bash
cd service && python scripts/check_reference_scores.py --api http://localhost:8000 \
    --address "1600 Amphitheatre Pkwy, Mountain View, CA 94043"
```

Without `--api` it checks score files copied to `service/scores/<reference file name>`.

AWS results (production images on x86):

Reference check: Lantana 77.68 vs 77.69 (−0.01), Atherton 63.98 vs 64.74
(−0.76), both within ±2. Performance work and experiments: [PERFORMANCE.md](PERFORMANCE.md).

Twelve addresses from `addresses.txt` in AWS (Nov 7–10, 16 MPI ranks): 4 quoted,
8 failed fast in the stage that found the problem, with the reason recorded on
the quote and no score returned.

| Address | Outcome | Score or reason |
|---|---|---|
| 1265 Lands End Rd, Lantana, FL | Quoted | 77.68 |
| 169 Tuscaloosa Ave, Atherton, CA | Quoted | 63.98 |
| 432 S Loxahatchee Dr, Jupiter, FL | Quoted | 84.09 |
| 32 Royal Rd, Belmont, MA | Quoted | 88.67 |
| 17 Concord Ave, Cambridge, MA | Failed in Simulation | did not converge in 1,500 iterations |
| 34 Madison St, Somerville, MA | Failed in Simulation | did not converge in 1,500 iterations |
| 498 Bella Vista Ave, Los Gatos, CA | Failed in Simulation | did not converge in 1,500 iterations |
| 24216 Malibu Rd, Malibu, CA | Failed in PreProcess | no USGS 3DEP LiDAR coverage found |
| 15 Silverwood Ct, Orinda, CA | Failed in PreProcess | no USGS 3DEP LiDAR coverage found |
| 74 Ashbury Terrace, San Francisco, CA | Failed in PreProcess | 0 building points (needs 500) |
| 1143 Casey Key Rd, Nokomis, FL | Failed in PreProcess | 64 building points (needs 500) |
| 1333 Drury Rd, Berkeley, CA | Failed in PreProcess | no geocoding result |

The failures are model and data limits, not pipeline faults: non-converging
cases go to the CFD engineer and SME (solver logs are kept in S3), and the
coverage and building-label cases are the Q4 problem. 
Malibu and Orinda reporting no coverage is worth confirming with the SME against the full 3DEP catalog.

Local Docker on Apple Silicon (amd64 emulated):

| Address | Score | Reference | Difference |
|---|---|---|---|
| 1265 Lands End Rd, Lantana, FL | 77.88 | 77.69 | +0.19, pass |
| 169 Tuscaloosa Ave, Atherton, CA | 62.65 | 64.74 | −2.09, outside ±2 |

Atherton is sensitive to the platform. Under emulation it scored 62.65, and
natively on macOS 63.09: Poisson surface reconstruction and quadric
simplification in `preprocess.py` amplify small floating-point differences into
different geometry. On the production platform (x86 in AWS) both addresses
pass. Pinning the hardware family is part of the Q3 plan.

## Changes to the SME code

`run_simulation.sh`: the two `rm -rf processor*` lines are replaced with
`find . -maxdepth 1 -type d -name 'processor*' -exec rm -rf {} +`. The glob
crashed the bash bundled with OpenFOAM on macOS. This only cleans up old
processor folders; mesh, solver and scoring are unchanged.
