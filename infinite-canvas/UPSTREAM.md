# Upstream source

This repository imports the `web/` frontend snapshot from
<https://github.com/basketikun/infinite-canvas> at commit
`9bccd0ff1a7057a835708a731644ab05371fea3b`.

The snapshot covers the upstream `web/` directory. The accompanying
`LICENSE`, `CHANGELOG.md`, and `VERSION` files are copied from the same pinned
source revision. The upstream project is licensed under AGPL-3.0; its license
and copyright notices are retained in this repository.

The import is reproducible with `scripts/import-upstream.sh`, which verifies
the source checkout's exact HEAD before replacing the snapshot.

This snapshot is a provenance-only source baseline. It is **not deployable or
runnable**: the unmodified upstream source contains browser key paths, remote
plugin facilities, and dynamically loaded scripts that conflict with this
repository's security requirements. Do not serve or release this snapshot.
Task 2 is the first runnable version and removes those facilities, enforces the
same-origin API boundary, regenerates a compatible dependency lockfile, and
adds its security scan as a required gate.
