# KADATH

**Kernel for Agentic Darwinian Adaptation, Tooling, and Heredity**

KADATH takes a goal and spends an ungodly number of model tokens evolving agents that are progressively better at achieving it. Over multiple generations, a population of agents attempts the goal, gets independently graded against a measurable benchmark, learns from the strongest performers, mutates, reproduces, and weeds out weak approaches. Instead of betting everything on one prompt or one agent, KADATH uses repeated competition and selection to produce the best agent it can for the user's intended outcome.

The thing being evolved is not merely a response. Each genome contains a complete editable agent framework: its system prompt, Python implementation, tools, dependency declarations, and supporting files. KADATH keeps the evolutionary control plane outside that genome so an organism can improve itself without gaining the ability to rewrite its own scores, objective, scheduler, lineage, or isolation rules.

Made by **i3t4an**. Credit to **Hugging Face** for `smolagents`.

## Proof it works

In this ten-epoch KADATH run, the entire leading cohort moved upward under the same locked fitness benchmark. The best score rose from **18 to 91**, the top-five median rose from **8 to 77**, and the lowest score among the top five rose from **1 to 71**.

![KADATH top-five fitness scores rising across ten epochs](assets/images/kadath-top5-fitness-by-epoch.png)

| Top-five measurement | Epoch 1 | Epoch 10 | Improvement |
| --- | ---: | ---: | ---: |
| Best fitness | 18 | 91 | +73 |
| Median fitness | 8 | 77 | +69 |
| Lowest fitness | 1 | 71 | +70 |

Each point is a different top-performing agent from that epoch rather than one agent tracked over time. The upward shift therefore reflects improvement across the competitive population, not a cherry-picked individual trajectory.

## The system at a glance

![KADATH system architecture and evolutionary loop](assets/images/system-at-a-glance.svg)

There are two distinct layers:

- The **kernel** owns the run, benchmark, deadlines, containers, evidence, grading formulas, population selection, database, Git lineage, recovery, exports, and cleanup. It does not evolve.
- The **organisms** perform the goal. Their complete framework is evolvable, but their repository is read-only while an epoch is running. They can change only during the post-grade mutation phase.

## What happens when a run starts

Launching `./kadath.sh` opens the interactive terminal interface. Arrow keys move through menus and `Ctrl-C` exits cleanly.

![KADATH main menu](assets/images/cli-main-menu.png)

On the first launch, KADATH asks for:

- an OpenAI API key;
- the OpenAI model ID that will drive the Architect, organisms, Grader, Tweaker, and Birther.

![First-time OpenAI API key setup](assets/images/cli-first-time-api-key.png)

![Model selection](assets/images/cli-model-selection.png)

KADATH generates its PostgreSQL, MinIO, LiteLLM, and SearXNG secrets locally. The resulting configuration is stored in `.kadath/config.env` with owner-only permissions. The Docker images and supporting services are prepared during the first-boot progress screen. Later launches reuse the installed runtime and expose menu options to edit the saved model credentials or delete old completed runs.

For a new run, the interface collects:

- the goal;
- the duration of each epoch;
- the population size;
- the number of epochs.

![Goal entry](assets/images/cli-objective.png)

![Epoch duration selection](assets/images/cli-epoch-duration.png)

![Population size selection](assets/images/cli-population-size.png)

![Epoch count selection](assets/images/cli-epoch-count.png)

The Architect then converts the goal into a complete benchmark. Before the population runs, the approval screen shows the objective, metric, attribution method, baseline, score range, weighted rubric, required evidence, automatic failures, tie-break policy, anti-fraud rules, enabled tools, external measurements, known limitations, and instructions for the specialist agents. Declining approval leaves the proposed run inactive.

![Architect benchmark approval](assets/images/cli-architect-approval.png)

## Architect and benchmark locking

The Architect is a model-driven specialist, not a hard-coded benchmark generator. It receives the goal, timing, population size, available services, available external measurement connectors, resource limits, and the names of configured agent environment variables. It receives credential names, never their secret values.

Its benchmark must satisfy a strict machine-readable contract:

- the score range must be finite;
- rubric weights must be positive and total exactly 100%;
- every rubric item must use a supported measurement type;
- required outputs must name a concrete evidence source;
- automatic failure rules and anti-fraud checks must be explicit;
- tie-break inputs must have numeric ranges and directions;
- tools and external connectors must already exist in the runtime inventory.

Supported rubric measurements are binary facts, linear numeric measurements, and locked descriptive levels. If the requested real-world outcome cannot be verified with the available environment, the Architect must define an honest measurable proxy and disclose the limitation.

Approval performs a runtime preflight, resolves mutable Docker image tags to immutable image IDs, creates generation one, and locks hashes of the objective, Architect output, tool manifest, and runtime configuration. Those locks are verified again before execution. Editing any locked input after approval stops the run instead of silently changing the experiment.

## Generation one

Every generation-one organism begins from the same complete vendored `smolagents` framework and KADATH organism runtime. Framework code is identical at birth. The Birther creates a distinct system-prompt variation for every initial agent, in bounded batches, and the kernel adds a unique generation index so repeated model prose cannot collapse two initial genomes into one.

Each new genome is committed into a run-local bare Git repository. A genome is identified from:

- the complete Git tree;
- the effective system prompt;
- the locked runtime configuration.

Commit history alone cannot make two identical genomes distinct. KADATH rejects duplicate content signatures.

## Inside an epoch

At the start of an epoch, the kernel verifies that every active worktree is clean and exactly matches its registered commit, tree hash, prompt, and runtime signature. The same checked genome is then mounted read-only into its own parent container.

Each organism receives:

- the user goal and approved measurement criterion;
- its own system prompt and complete framework source;
- a persistent writable workspace;
- its own ranked memory view;
- artifact storage;
- its scoped model gateway token;
- any Architect-approved search, HTTP, browser, and worker capabilities;
- a hard epoch deadline and a finalization signal near that deadline.

The default organism is a `CodeAgent`. It works in fresh-context passes. After each pass it writes durable progress, candidate output, high-level activity, and a provisional result envelope. It may begin another pass while time remains. The provisional numeric value is never accepted as fitness; it exists only as part of the execution contract.

Agents can inspect their own framework while working, but they cannot edit the mounted genome. They may freely read and write their persistent workspace, run bounded workspace commands, save artifacts, search memory, and use the optional capabilities approved for that run.

### Agent activity and telemetry

KADATH retains both concise operational memory and detailed evidence:

- agents publish high-level records describing what they investigated, what they did, what happened, and what should happen next;
- low-level reasoning steps and tool-call identifiers stay in local trace files;
- parent and worker model requests and responses are captured by the kernel-owned broker with run, epoch, attempt, agent, worker, and genome attribution;
- specialist requests and responses are recorded separately;
- configured secrets are redacted from retained traces;
- crashes, worker results, evidence references, grading results, lineage, and mutations are logged with their owning identity.

The live dashboard shows epoch progress, resolved and failed agents, the current leaderboard, model-call count, worker activity, crash count, and recent high-level activity.

![Live evolution dashboard](assets/images/cli-live-dashboard.png)

## Research, browsing, and temporary workers

Web search is provided through the local SearXNG service. Direct HTTP retrieval accepts only public HTTP and HTTPS destinations, rejects private and reserved network addresses, follows the same validation after redirects, limits response size, and accepts text-like content only.

When browser access is approved, every parent receives its own persistent Chromium profile and its own Playwright MCP container. Browser artifacts are stored inside that agent's state. Contexts are not shared between live parents.

An organism can delegate bounded subproblems to temporary workers through the kernel broker. It cannot start containers itself. The kernel enforces:

- no more than five live workers per parent;
- an overall worker concurrency limit;
- an explicit subset of the parent's approved optional tools for each worker;
- a separate container, identity, deadline, filesystem, result file, and model token;
- read-only access to the parent's exact genome;
- bounded result size and returned files;
- automatic worker and browser-container cleanup.

If a worker needs browser access, KADATH creates a consistent snapshot of the parent's durable Chromium profile. SQLite browser databases are copied using SQLite backup rather than raw live-file copying. The worker receives its own browser instance from that snapshot, so it keeps relevant cookies and state without sharing the live parent context.

Workers return findings and files to their parent. They do not grade, reproduce, mutate the parent, or become independently heritable.

## Evidence freezing and grading

When execution ends, every parent and worker is stopped before selection begins. The kernel copies each attempt into a frozen evidence boundary containing:

- candidate output and the organism result envelope;
- workspace files and saved artifacts;
- browser artifacts;
- worker outputs;
- model-call traces;
- high-level activity and detailed tool traces;
- crash status and identity metadata;
- the exact objective and genome hashes.

Files are copied without following symbolic links, hashed while streaming, and recorded in a manifest. Unsafe files are rejected. The default frozen-attempt ceiling is 512 MiB per agent. After successful grading, the entire frozen tree receives a second integrity seal and is made read-only.

The Grader reviews this frozen boundary, not the agent's live workspace. Large evidence is divided into overlapping, checkpointed chunks. Text is streamed; images are attached directly; every PDF page is text-extracted and rendered for visual review; video is sampled at five points; and audio is divided into bounded segments. Every fragment must be acknowledged exactly once. If the accumulated notes are still too large, they are hierarchically consolidated while preserving a kernel-owned count and checksum of coverage.

The model Grader does not choose the score. It extracts:

- typed facts or locked level IDs for every rubric item;
- numeric facts for every tie-break input;
- a decision for every automatic failure;
- a decision for every anti-fraud rule;
- evidence references for every credited or negative claim;
- a high-level account of the agent's activity.

The kernel validates those references and applies the locked formulas itself. Agent self-scores are ignored. Ranking places verified successes first, then orders by kernel-computed score, normalized tie-break result, and stable agent ID.

Optional independent measurement connectors can supply external facts. A connector must already be configured before the Architect can select it, and every response must attribute itself to the exact run, epoch, agent, and genome. The first valid response is frozen and reused during any regrade.

## Selection and evolution

After every graded non-final epoch, KADATH divides the population by verified outcome and rank:

- **Top 30%:** preserved exactly as they performed. Their prompt, source, dependencies, and complete framework remain unchanged.
- **Middle cohort:** each surviving non-elite agent receives its own evidence, population outcomes, elite semantic records, elite framework snapshots, and ranked memory. It independently decides to mutate or explicitly remain unchanged.
- **Culled cohort:** failed agents are culled first, followed by the lowest-ranked successful agents needed to reach the normal 30% replacement target. If failures exceed that target, every failure is culled and the replacement count grows accordingly.

The final epoch is graded and then stops. It does not cull, reflect, mutate, or birth another generation.

![Completed evolution dashboard](assets/images/cli-completed-dashboard.png)

### Middle-agent reflection

The middle cohort does not receive instructions from the Tweaker. Each middle agent re-enters its own model-driven runtime after grading and inspects its own scored behavior, exact framework, useful population memories, and direct elite evidence. It then calls the mutation interface once with either:

- `mutate`, including a reason and any prompt, source, dependency, or file changes; or
- `unchanged`, preserving the exact genome that just performed.

Proposals do not alter the tested genome retroactively. The kernel applies a valid proposal to a fresh worktree only after the epoch, commits it, registers a new content-addressed genome, and activates it for the next epoch. Invalid, duplicate, or crashed adaptations retain the previously scored genome.

### Tweaker

The Tweaker receives a dossier for every ranked agent: outcome, evidence, activity, memory and worker behavior, failures, lineage, prompt, manifest, and a bounded view of the complete framework. Dossiers are chunked and reduced without dropping coverage.

Its job is to explain which high-level characteristics made the elite effective, which patterns failed, and how many children each eligible elite should produce. It creates an elite-specific reproduction brief for the Birther. It does not edit genomes, change scores, or guide the middle cohort.

### Birther

For each empty population slot, the Birther receives:

- one selected elite's exact scored genome;
- a view of that parent's framework;
- the Tweaker's reproduction brief for that parent;
- any duplicate mutation attempts already rejected for that child.

The child begins from the complete elite commit, including the evolved framework—not from the original seed. The Birther proposes a bounded mutation, the kernel commits it as a new genome, links its lineage, and copies the parent's durable state and inherited memory. Exact duplicates are discarded and retried up to five times.

The active population returns to its configured size before the next epoch. If every organism fails and no verified genome remains, the run ends cleanly as failed rather than reproducing from unverified work.

## Memory and heredity

KADATH stores activity and experience as canonical knowledge records. Exact duplicate records from the same owner and record type collapse into one bank entry. A child inherits references to its parent's records instead of recursively copying the same payload on every generation.

To an organism, memory is presented in three scopes:

- **own:** records created by the current agent;
- **inherited:** parent and ancestral records presented as the agent's continuing personal memory, with original provenance retained;
- **population:** records deliberately published for other organisms to inspect.

Agents can search up to 500 visible records and rate another agent's memory as useful or misleading. They cannot rate their own records. Retrieval combines query relevance, record type, evidence presence, peer ratings, recency, and the verified quality of the source in the exact epoch that produced the memory. Elite-produced memories receive the strongest credibility; middle agents receive an additional elite-memory boost during reflection. Peer influence is bounded and weighted by the rater's own verified performance.

PostgreSQL stores the production run state and enforces row-level security on agent knowledge buckets. Organisms never receive PostgreSQL credentials or direct access to scores, genomes, lineage, objectives, memory links, ratings, or scheduler tables. Their scoped broker token is the only path for publishing, retrieving, and rating memory. SQLite remains available for local proof executors and the no-model lifecycle suite.

Durable filesystem heredity is separate from database memory. A descendant receives the parent's persistent workspace, progress, browser profile, and other non-transient state. Result files, pending mutations, worker directories, active checkpoints, and crash-control files are deliberately excluded.

## Genome boundaries and evolved dependencies

The complete organism repository is evolvable. A mutation may change the system prompt, organism entry point, vendored framework modules, tool usage, dependencies, or add and remove ordinary files. It cannot write outside the repository, touch `.git`, or recursively delete directories.

Before each organism starts, the kernel-owned container entrypoint hashes `pyproject.toml` and `requirements.txt`. If an evolved genome changed its dependency declarations, those dependencies are installed into that agent's persistent state for the current epoch and placed ahead of the base environment on `PYTHONPATH`. The dependency environment is refreshed again at the beginning of a later epoch when the declared inputs change.

Default mutation safeguards allow up to 512 file operations, 32 MiB of replacement content, and a 2 MiB system prompt. These are integrity limits, not model-spending budgets.

## Isolation and credentials

Only the control container receives the Docker socket and infrastructure credentials. Parent and worker containers do not receive the socket, database credentials, MinIO credentials, or the LiteLLM master key.

Each parent runs with:

- a read-only root filesystem and read-only genome mount;
- a non-root user;
- all Linux capabilities dropped;
- `no-new-privileges`;
- a private writable state mount and bounded temporary filesystem;
- one CPU, 2 GiB of memory, and PID/file-size limits by default;
- a scoped broker token for model, memory, and worker operations.

Workers use the same security posture with half a CPU, 1 GiB of memory, and tighter PID, file, temporary-storage, and output limits.

Parent and worker model calls go through the kernel broker and then LiteLLM. The broker adds immutable identity metadata, applies per-agent and global concurrency limits, retries transient gateway failures, enforces the epoch deadline, and stores redacted request/response traces. The model key is held by LiteLLM rather than placed inside organism containers.

The Docker stack separates the internal control plane from the agent network and provider egress. PostgreSQL and MinIO remain on the control plane, and organism containers are not attached to that control-plane network; they use their scoped broker and approved agent-facing services instead.

## Failure handling and recovery

KADATH treats execution, grading, and evolutionary selection as separate durable boundaries.

- A parent that crashes without a valid result is restarted from the same persistent state and locked genome, up to the configured restart limit and only while epoch time remains.
- One failed parent becomes a failed graded outcome; it does not abort the rest of the population.
- An interrupted epoch restores the pre-epoch state snapshot and discards partial scores, activity, attempts, and measurements before rerunning.
- A Grader or provider outage pauses at `grading_interrupted`. Frozen organism attempts remain intact and are regraded without rerunning the population.
- Specialist chunk outputs are checkpointed against a hash of their complete input, allowing large Grader, Tweaker, Birther, and adaptation work to resume without blindly repeating finished calls.
- Selection takes a full snapshot of agents, states, genomes, memory links, ratings, lineage, Git branches, and genome tags. A failure during culling, reflection, or birth rolls the transition back completely.
- A per-run file lock prevents two control processes from operating on the same run simultaneously.
- Managed containers carry run and agent labels. Starting, resuming, resetting, or recovering a run can remove only the orphan containers belonging to that run.
- A pause request takes effect after the current durable epoch boundary. The run can then resume from that boundary.

## Data retained for a run

Host-visible run data lives under `.kadath/runs/<run-id>/`. PostgreSQL and MinIO hold the production relational state and artifact copies, while the run directory contains the recoverable filesystem state.

Important retained data includes:

- the locked objective, Architect output, runtime, environment inventory, and tool manifest;
- the bare Git lineage repository and every registered genome manifest;
- live and archived agent worktrees and persistent state;
- epoch and selection recovery snapshots;
- frozen attempts and their integrity seals;
- Grader, Tweaker, Birther, and adaptation checkpoints;
- specialist and organism model traces;
- leaderboards, evidence, memory, ratings, events, crashes, and lineage;
- local or MinIO-backed artifacts.

Run IDs include a timestamp and random suffix, so repeated launches do not overwrite prior runs.

## Final exports and continuation

Terminal runs can be exported to `.kadath/exports/<run-id>/`. An export contains the final and archived frameworks, agent states, epoch champions, top historical genomes, raw scores, leaderboards, lineage, memory, ratings, events, benchmark and runtime locks, frozen attempts, specialist reports, model traces, artifacts, the canonical bare Git repository, and framework diffs.

### Retrieving the best agents

After a run finishes, export it with:

```bash
./kadath.sh export RUN_ID
```

The winning agents are then available in `.kadath/exports/RUN_ID/final-population/`, with one complete runnable framework directory per agent. `epoch-champions/records.json` identifies the winner of each epoch, including the final winner, while `leaderboards/records.json` contains the full ranking. Historical high performers that did not survive into the final population are indexed in `top-historical-genomes/records.json` and remain recoverable from the exported `git-repository/` using the genome registry.

Every regular file is covered by an export manifest containing its byte size and SHA-256 checksum. Continuation from an export first verifies the complete manifest, rejects unlisted files and symbolic links, and then births a new population from the selected genome's exact commit, prompt, durable state, frozen history, and inherited memories.

A live completed run can also be continued directly from any genome registered to that run. The continuation is a new run with its own approval boundary and settings; it does not overwrite the source history.

## Cleanup and reset

The main menu can delete all finished history or only finished runs older than 30 days. Active, paused, ready, and awaiting-approval runs are protected. CLI cleanup supports the same behavior with a custom age.

![Delete completed runs](assets/images/cli-delete-runs.png)

Reset is scoped to one known run. It removes that run's labeled Docker containers, relational rows, artifact prefix, and run directory through a staged deletion. If infrastructure cleanup fails, the filesystem move is reversed rather than leaving a half-deleted run. Verified exports live outside the run directory and are intentionally preserved.

## Runtime services

| Component | Responsibility |
|---|---|
| `control` | Fixed kernel, CLI backend, scheduler, grading formulas, selection, recovery, exports |
| `organism-worker` image | Parent and temporary-worker runtime containing the base framework and kernel-owned dependency launcher |
| PostgreSQL | Runs, agents, genomes, scores, attempts, lineage, events, knowledge, memory links, and ratings |
| MinIO | S3-compatible artifact storage under run/epoch/agent prefixes |
| LiteLLM | OpenAI-compatible model gateway for specialists, parents, and workers |
| SearXNG | Local web-search service exposed as an approved organism tool |
| Playwright MCP | Browser automation used to create isolated parent and worker browser contexts |

## Repository map

| Path | What it contains |
|---|---|
| `kadath.sh` | Interactive frontend, first-boot configuration, runtime preparation, and Docker-backed command launcher |
| `kadath/engine.py` | Run state machine, epoch execution, evidence freezing, grading coordination, evolution, heredity, recovery, export, and reset |
| `kadath/specialists.py` | Architect, Grader, Tweaker, Birther, model gateway client, multimodal evidence review, and checkpoint contracts |
| `kadath/store.py` | SQLite/PostgreSQL schema, row-level security, canonical memory, ranking, and deletion |
| `kadath/containers.py` | Isolated parent execution, deadline handling, crash restart, and adaptation containers |
| `kadath/worker_broker.py` | Scoped model proxy, memory API, worker API, identity attribution, redaction, and telemetry |
| `kadath/workers.py` | Worker limits, worker containers, result collection, and worker browser isolation |
| `kadath/browsers.py` | Per-parent browser fleet and consistent profile snapshots |
| `kadath/gitstore.py` | Run-local bare Git repository, worktree materialization, genome commits, branches, and permanent genome tags |
| `kadath/mutations.py` | Mutation schema, path validation, size limits, and file application |
| `kadath/artifacts.py` | Streaming content-addressed local and S3-compatible artifact storage |
| `kadath/cli.py` | Backend commands, approval rendering, status output, and live dashboard |
| `seed/organism.py` | Default evolvable `CodeAgent` loop and tool assembly |
| `seed/kadath_runtime.py` | Organism tools for workspace, artifacts, memory, activity, web access, workers, and mutation proposals |
| `seed/container_entrypoint.py` | Kernel-owned activation of dependencies declared by an evolved genome |
| `seed/smolagents/` | Complete editable framework seed copied into every lineage |
| `deploy/` | PostgreSQL role bootstrap, LiteLLM model mapping, and SearXNG configuration |
| `assets/` | Boot artwork and its terminal color renderer |
| `tests/` | Lifecycle suite and live Docker, crash-recovery, evolved-dependency, and PostgreSQL isolation probes |

## Direct operational commands

The interactive frontend is the normal entry point:

```bash
./kadath.sh
```

Existing runs can also be operated directly through the same launcher:

```bash
./kadath.sh status RUN_ID
./kadath.sh dashboard RUN_ID --watch
./kadath.sh pause RUN_ID
./kadath.sh resume RUN_ID
./kadath.sh export RUN_ID
./kadath.sh continue RUN_ID --genome GENOME_HASH --epochs 3
./kadath.sh continue-export .kadath/exports/RUN_ID --genome GENOME_HASH --epochs 3
./kadath.sh reset RUN_ID --yes
./kadath.sh cleanup --older-than-days 30
./kadath.sh cleanup --all
```

These commands still prepare and validate the Docker runtime before entering the control kernel; they do not bypass the locked objective, recovery, isolation, or cleanup rules.
