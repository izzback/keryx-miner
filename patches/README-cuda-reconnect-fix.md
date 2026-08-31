# CUDA reconnect/context poisoning fix for v0.5.4

Base: `d1b5cbcc5a80a797473058e6c19baad533a1e7b4` (Keryx miner v0.5.4)

## Why

The miner currently reconnects Stratum in-process after dropping `MinerManager`. On GPU rigs this recreates CUDA workers inside the same process. If CUDA has raised a sticky exception such as `CUDA_ERROR_ILLEGAL_ADDRESS`, subsequent module loads can return the same error even when the fatbin/PTX is valid.

The v0.5.4 source already documents this in `src/pom_gpu.rs`: a bad device pointer / wrong-device llama tensor can poison the primary context until process restart. However, the current error path classifies illegal-address faults as transient and retries in-process.

The Windows shutdown path also uses `TerminateThread` after only one second, which can bypass Rust/CUDA destructors and leave driver state inconsistent.

## Patch

Apply from the repository root:

```bash
git apply patches/keryx-v0.5.4-cuda-reconnect-fix.patch
cargo fmt --all
```

Then build normally.

## Behaviour after the patch

- Sticky CUDA faults are recorded as fatal instead of swallowed.
- `pom_gpu::mine()` no longer converts every CUDA error to `None` silently.
- `client_main()` detects a fatal GPU fault, flushes escrow state, shuts workers down, and returns.
- With CUDA workers active, any client/Stratum disconnect exits the process instead of recreating CUDA workers in-process.
- CPU-only mining keeps the existing in-process reconnect.
- Windows no longer calls `TerminateThread` on a CUDA worker. It gives cooperative shutdown 30 seconds and terminates the whole process if the worker remains stuck.
- The misleading `PTX (driver too old?)` log text is removed.

## Supervisor

HiveOS/systemd/PM2 should restart the exited miner process automatically. A raw Windows console launch should be wrapped in a restart loop.

Example Windows wrapper:

```bat
@echo off
:again
keryx-miner.exe YOUR_ARGUMENTS_HERE
timeout /t 2 /nobreak >nul
goto again
```

## Validation

1. Start all RTX 3080 GPUs and verify normal `sm_86` startup.
2. Record baseline hashrate/shares.
3. Force a Stratum disconnect.
4. Confirm the old process does not create a second generation of CUDA workers.
5. Confirm the supervisor relaunches a fresh process.
6. Confirm every RTX 3080 reloads the legacy fatbin and returns to normal hashrate.
7. Repeat at least 10 disconnect/reconnect cycles.
8. Run a soak test and confirm no post-reconnect `illegal memory access` errors.
