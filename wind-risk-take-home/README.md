# Stand Applied Science — Wind Risk Pipeline Take-Home

## Problem

A subject matter expert (SME) has handed you three components that together form a
wind risk scoring pipeline for a property:

1. **`preprocess.py`** — turns an address into an OpenFOAM case directory
   (building geometry from USGS LiDAR, mesh dicts, boundary conditions).
2. **`run_simulation.sh`** — runs the OpenFOAM incantation over that case dir to
   produce a solved pressure and velocity field.
3. **`postprocess/`** — consumes the solved field to produce
   - `score.py` → a risk score as JSON
   - `render.py` → a video of the simulation

Each component runs independently and has different resource needs.

**Your job: stitch them into a deployed, production-ready pipeline** that accepts
an API request with an address and returns a risk score and a video of the
simulation.

## Contract

Given an address, return the risk score and a link to the video of the simulation.

## Dependencies

The scripts need OpenFOAM v2412 and PDAL in addition to `requirements.txt`.

## Requirements

Set up the overarching pipeline so that it:

- Utilizes resources effectively and is performant
- Stores the appropriate outputs
- Is robust and observable
- For observability, a use case is that an underwriter might want to take a look at
  where a quote is in the pipeline (i.e., what phase it's in and when it might be done)
- Is deployed automatically
- Handles 1000 requests a day, with bursts of up to 50 at once, and returns each
  quote within 30 minutes of the request

## Scope and time

Aim for about 8 hours. AI tools are welcome.

- **Must actually run:** the API (submit an address, check status, get the score and
  video link), an automated deploy of one environment, and the real pipeline for a
  handful of addresses from `addresses.txt`.
- **Can be described instead of built:** scaling to the full load, further
  environments, and anything you would do with more time. Explain how you would show
  the system meets the load requirement; you do not need to run 1000 real
  simulations.

## Bounds

You are allowed to change the underlying scripts the SME has provided you, and to
change code to increase performance/efficiency. Scores must stay within ±2 points
of the outputs in `reference/` for the same address.

## Deliverables

1. Your code, infrastructure-as-code and CI configuration (repo access or archive).
2. How to call the deployed API and how deployment works (a short section in your
   repo's README is enough).
3. A slide deck of at most 12 slides, which you will present in the debrief in about
   20 minutes. Cover:
   - architecture and the main tradeoffs;
   - a cost estimate for the required load;
   - what you would do next;
   - your answers to the questions below;
   - how you used AI tools and how you checked their output.

## Questions for your slides

No implementation is expected for these.

1. **Plan.** You have two engineers for six weeks to take your prototype to
   production: a mid-level backend engineer and a junior engineer with a CFD
   background. What ships first, who owns what, what do you cut, and how will you
   know it is on track?
2. **Tradeoffs.** The Chief Science Officer wants every property simulated at 8–36
   wind angles instead of one. Underwriting wants a quote in under 10 minutes.
   Finance caps compute at $5,000 a month. What do you propose, and how do you
   explain the tradeoff to each of them?
3. **SME changes.** The SME changes `preprocess.py` and the case template every few
   weeks. How do they ship changes without silently shifting production scores?
4. **Unclassified LiDAR.** Some USGS 3DEP LiDAR projects ship without building
   classification (ASPRS class 6), so `preprocess.py` cannot find buildings anywhere
   those projects are the only source. How would you handle this in production?
   Consider what an underwriter sees for an affected quote, how you would detect
   affected areas before quotes arrive, and what you would need from the SME.

## Cloud Resources

You are allowed to spend $100 with a cloud provider of your choice. Please save
your receipts/invoices for reimbursement.
