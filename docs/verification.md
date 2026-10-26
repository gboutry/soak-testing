# Verification boundaries

Run the project checks with `make test`, `make lint`, and
`uv run ruff format --check .`. Tests exercise planning, emitted guest code,
workload evidence, lifecycle coordination, ownership recovery, and cleanup
using local fixtures and mocked service boundaries. Image transfer tests use a
fake LXD command; they do not build or boot an image.

Exercise CLI argument handling with `uv run soak-tester --help` and the
subcommand help paths. Campaign dry runs validate manifests and profile loading;
they do not create cloud resources or validate live workload results.

Live acceptance requires an isolated OpenStack project and existing deployment
prerequisites. Check normal and direct networking, storage, native API and Rally,
interruption and repeated cleanup, guest tuning readbacks, workload evidence,
and live resource absence after cleanup. Local checks do not establish these
results or validate deployment-specific performance.

Keep campaign inputs and live evidence outside this source repository. Run
directories may contain credentials; preserve their private permissions.
