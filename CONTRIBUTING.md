# Contributing

Thanks for taking a look at Football Content Agent. This repo should stay clear
as a public production proof: football-specific enough to be real, but sanitized
enough that no private deployment details leak.

## Local checks

```bash
python3 -m venv .venv
. .venv/bin/activate
make install
make check
```

`make check` runs the lightweight lint gate and the no-secret test suite.

## What makes a good change

- Keep every publish path human-approved.
- Keep live credentials, chat IDs, account IDs, hostnames, and private paths out
  of the repo.
- Update `README.md`, `ARCHITECTURE.md`, `RUNBOOK.md`, or `FAILURE_MODES.md` when
  setup, commands, or behavior changes.
- Add tests for season mode, news ranking, image rules, fallback behavior, and
  approval safety.

## Before opening a PR

1. Run `make check`.
2. Confirm `.env`, generated queues, local media, and credentials are not staged.
3. Explain the workflow changed and how it was verified.
