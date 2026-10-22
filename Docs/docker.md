# Docker and Unraid

The container is an alternative to the native installer, not a requirement.
It needs no GPIO, privileged mode, host networking or Docker socket. It must
be able to reach the configured Protect console, plug, DNS and SMTP service.
Run exactly one controller against a physical plug; stop the old service before
starting its replacement. Test migrations with the emulator first.

## Images and channels

Registry: `ghcr.io/holloway25/fan-controller`, Linux `amd64` and `arm64`.
Package visibility follows the repository's existing registry permissions;
this workflow does not make packages public. Private pulls require a GitHub
credential with read access to packages, configured on the Docker host.

- `candidate`: proposed code, published only after source CI, image checks and
  both native platform builds succeed. Do not use this for unattended production.
- `latest`: explicitly approved controller code. Maintenance builds automatically
  refresh this channel only after their checks pass.
- `candidate-<commit>-<run>-<attempt>`, `maintenance-<commit>-<run>-<attempt>`
  and `release-<commit>-<run>-<attempt>` identify rollback artifacts. Record the
  manifest digest for an immutable deployment; moving channel tags are not pins.

No `latest` image is created until the first candidate is approved. Publishing
an image does not pull, replace or restart any running controller. Do not add an
automatic updater until you have separately chosen that deployment policy.

## Start

Use the repository's `compose.yaml` with Docker Compose, or create an Unraid
container with the same settings:

| Setting | Value |
| --- | --- |
| Image | Approved registry tag or digest |
| Web port | Container `8096` mapped to an available host port |
| Persistent storage | Named volume or local directory mounted at `/data` |
| Container user | UID/GID `10001:10001` (directory must be writable by it) |
| Stop timeout | At least 60 seconds |
| Timezone | Your IANA timezone via `TZ`, default `UTC` |
| Restart policy | `unless-stopped` |
| Privileged / Docker socket | Neither |

For an Unraid bind mount, create a dedicated appdata directory owned by UID/GID
10001, restrict access, and mount it read-write at `/data`. Do not assume `PUID`
or `PGID` variables change ownership: this image does not implement those variables.
Prefer local persistent storage, not a network share with uncertain durability.

The entrypoint copies only the public example into an empty data directory;
it starts in Manual, with power monitoring off. Configure your own installation
through the dashboard. Credentials are never baked into the image.

The container binds HTTP on all container interfaces to allow port publishing.
It does not trust forwarded headers by default. Keep it on a trusted network;
use a properly configured HTTPS reverse proxy if needed. Set
`FAN_CONTROLLER_TRUST_PROXY=1` only behind one trusted proxy which overwrites
forwarded headers and is the only path into this port. Native installs retain
their existing loopback/nginx defaults. The container currently uses the same
threaded Flask server as the native service, with one controller process.

The health check tests the local API, not whether the plug/Protect is reachable.
A network outage must not cause a controller restart loop. Docker restart
policies restart exited containers; they do not by themselves restart an
unhealthy but running container.

## Data and stop/start recovery

Keep the whole `/data` directory. It includes `config.json` (credentials),
`fault.json`, `manual-off.pending`, history, events, daily summaries, optional
diagnostics and the one-use `resume.json` clean-stop checkpoint. Do not mount
only individual JSON files: durable writers replace files atomically.

A normal stop forwards SIGTERM to Python and saves latches, leases, elapsed
safety timers and retry spacing before exiting. It does not command the fan
OFF merely because the container is stopping. Faults and retry budgets stay
latched. Startup consumes the checkpoint once, checks configuration/fault
identity and obtains fresh device readings rather than trusting a cached relay
or wattage. Downtime counts toward the remaining lease. Clock rollback across
a host reboot or a checkpoint older than 24 hours uses cold-start recovery.

Unexpected power loss or a forced kill cannot save current transient state;
the existing cold-start logic applies. Durable faults and Manual-stop promises
still survive. Back up `/data` after stopping cleanly, and keep the previous
image digest for rollback. Do not share one writable volume between controllers.
Restoring an old backup does not prove the current physical relay is OFF.

## Build, maintenance and approval

GitHub's `container` workflow builds after successful source tests, or by manual
dispatch. It builds and tests the image on native x86-64 and ARM64 runners.
Runtime integration tests use an isolated network with HTTPS Protect fixtures
and the repository plug emulator: no production hardware or real email.
Tests replace containers while retaining a volume, exercise clean-stop latches
and leases, persist a draw fault, acknowledge it, and check actual Docker stop
signal handling. Image vulnerability scans reject fixable HIGH/CRITICAL issues.
These checks do not prove physical power isolation or eliminate all vulnerabilities.

The final runtime image removes pip, setuptools, wheel and ensurepip (including
its bundled installer wheel) after dependencies are installed. Their private
libraries are not controller runtime dependencies and must not be left behind
as unmaintained copies. Update dependencies by rebuilding the image, not by
installing packages inside a running container. The native installer is unchanged.

Each successful platform build saves its tested registry digest as a workflow
artifact. Publication downloads both records and validates the repository,
source, channel, architecture and workflow run before assembling by digest.
This supports **Re-run failed jobs**: a successful earlier AMD64 attempt can
be combined with a successful retried ARM64 attempt, without guessing tag names
from the publication attempt. Records are retained for 30 days; missing/expired
records fail closed and require rebuilding the missing platforms.

Maintenance builds run every second UTC day at 03:17 UTC (GitHub may delay
scheduled starts). A daily trigger checks UTC epoch-day parity, preserving the
two-day cadence across month/year boundaries; the intervening trigger skips
the build. Manual maintenance is available on any day.

Scheduled maintenance (or a manual maintenance dispatch) uses the exact commit
referenced by `container-approved`, never the newest code on `main`. It:

1. Refreshes `python:3.15-slim-bookworm` and stable-distribution OS packages.
2. Resolves stable runtime Python packages within their existing major version,
   accepting minor/patch updates. Certifi uses date-based releases, so newer
   calendar versions are allowed. Downgrades, prereleases and new dependency
   names are rejected. Candidate builds use this same resolver.
3. Runs the complete suite against those resolved dependencies, maintenance
   mutations, actual-image integration and vulnerability checks.
4. Publishes a dated/run-specific artifact and updates `latest` only if the
   approved source pointer has not changed during the build and the publication
   is newer than the last reserved publication. Maintenance and approvals share
   one publication lock and a durable `container-publication` metadata tag;
   older/repeated builds cannot move `latest` backwards. The order is reserved
   before moving the channel, so a failed publish requires a newer run/attempt.
   That metadata tag anchors to the workflow commit; its JSON records the actual
   approved controller revision and image digest separately.

Python stays within 3.15; OS stays within Bookworm. Python minor/major changes,
dependency major changes and controller code changes require a candidate
approval. Exact baseline pins in `requirements.txt` make native installs
reproducible; they are lower bounds for container updates, not permanently frozen
image dependencies. Each build resolves exact versions into
`container/requirements.lock` and tests that image without modifying the approved
source requirements. Updates are not assumed to be security-only or risk-free.
If any check fails, `latest` remains unchanged.

After reviewing a successful candidate, run **approve container** in GitHub
Actions, supply its full immutable candidate tag from the build summary, and
explicitly check the approval box. Only repository writers can dispatch it.
The workflow checks its successful supplying run and source labels, updates
the approved source pointer, and promotes the already-tested manifest to
`latest` without rebuilding. No production host is contacted.

Approval requires Actions read permission to verify the supplying run. GitHub
may restrict its built-in token from creating/moving `container-approved` to
an older commit containing different workflow files. In that case, an explicitly
authorised maintainer must set that source tag to the exact reviewed commit
using their own workflow-capable login, then retry approval. Never substitute
the newest unreviewed commit, bypass the image checks, or store a broad personal
token merely to avoid this step. An already matching source tag is left alone;
normal maintenance uses the existing approval without moving it.

For local development builds, keep `container/requirements.lock` aligned with
`requirements.txt`, then build with `docker build -t fan-controller:local .`.
Container integration is `python tools/test_container.py fan-controller:local`;
it requires Docker and OpenSSL on the test host and creates/removes its own
uniquely named volumes and containers. Do not run it on a live controller host.
