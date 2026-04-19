# e-Invoice Engine (7th Engine)

**Status**: 🚧 Bootstrap Phase (2026-04-19)

## Purpose

Orchestrates e-Invoice operations across multiple PDPs.
Used by `tools/einvoice/` Tools.

## Architecture

```
Tool Layer (tools/einvoice/)
    ↓
Engine Layer (here)
    ├── router.py        - PDP selection logic
    ├── factur_x/        - Richard's code + validation
    ├── adapters/        - Per-PDP adapters
    └── success_detector - Pay-for-outcome logic
```

## Note on Decision #3

Original decision #3 locked ClawShow at 6 engines.
e-Invoice is added as 7th engine due to strategic
time window (2026-09-01 French mandate).
See decision #30 for rationale.

## Richard Collaboration

Richard's Factur-X code from 2025 (FocusingPro)
is reused here. Revenue share: 25-30%
per decision #30.
