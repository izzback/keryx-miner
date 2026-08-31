#!/usr/bin/env python3
from pathlib import Path


def replace_once(path: str, old: str, new: str, label: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match for {label}, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"patched {path}: {label}")


replace_once(
    "src/main.rs",
    '''    let listen_result = tokio::select! {\n        listen_res = client.listen(&mut miner_manager) => {\n            listen_res\n        }\n        _ = wait_for_shutdown(shutdown_requested) => {\n            info!("Shutdown requested, stopping client listen loop");\n            Ok(())\n        }\n    };\n    // Flush funds-critical client state before potentially blocking on worker shutdown.\n''',
    '''    let listen_result = tokio::select! {\n        listen_res = client.listen(&mut miner_manager) => {\n            listen_res\n        }\n        _ = wait_for_shutdown(shutdown_requested) => {\n            info!("Shutdown requested, stopping client listen loop");\n            Ok(())\n        }\n        _ = wait_for_fatal_gpu_fault() => {\n            error!("Fatal CUDA fault detected; stopping the client so the process can restart with fresh CUDA contexts");\n            Err("fatal CUDA fault — process restart required".into())\n        }\n    };\n    // Flush funds-critical client state before potentially blocking on worker shutdown.\n''',
    "fatal CUDA select branch",
)

replace_once(
    "src/main.rs",
    '''async fn wait_for_shutdown(shutdown_requested: Arc<AtomicBool>) {\n    while !shutdown_requested.load(Ordering::Acquire) {\n        tokio::time::sleep(Duration::from_millis(100)).await;\n    }\n}\n\n/// Tokio async worker count. The miner's async workload is tiny (one gRPC/stratum connection +\n''',
    '''async fn wait_for_shutdown(shutdown_requested: Arc<AtomicBool>) {\n    while !shutdown_requested.load(Ordering::Acquire) {\n        tokio::time::sleep(Duration::from_millis(100)).await;\n    }\n}\n\nasync fn wait_for_fatal_gpu_fault() {\n    while !crate::miner::fatal_gpu_fault() && !keryx_miner::pom_gpu::fatal_gpu_fault() {\n        tokio::time::sleep(Duration::from_millis(50)).await;\n    }\n}\n\n/// Tokio async worker count. The miner's async workload is tiny (one gRPC/stratum connection +\n''',
    "fatal CUDA waiter",
)

replace_once(
    "src/main.rs",
    '''        if shutdown_requested.load(Ordering::Acquire) {\n            info!("Shutdown requested, skipping reconnect");\n            break;\n        }\n        info!("Client closed, reconnecting");\n        tokio::time::sleep(Duration::from_millis(100)).await;\n''',
    '''        if shutdown_requested.load(Ordering::Acquire) {\n            info!("Shutdown requested, skipping reconnect");\n            break;\n        }\n        // CUDA sticky faults survive Context drop/recreation inside one process. With GPU workers\n        // active, leave recovery to a process supervisor so the next run gets fresh CUDA state.\n        // CPU-only mining keeps the lightweight in-process reconnect path.\n        if worker_count > 0 {\n            return Err("Client disconnected while CUDA workers are active — clean process restart required".into());\n        }\n        info!("Client closed, reconnecting");\n        tokio::time::sleep(Duration::from_millis(100)).await;\n''',
    "GPU reconnect process restart",
)

replace_once(
    "src/miner.rs",
    '''type MinerHandler = std::thread::JoinHandle<Result<(), Error>>;\n\n#[cfg(any(target_os = "linux", target_os = "macos"))]\n''',
    '''type MinerHandler = std::thread::JoinHandle<Result<(), Error>>;\n\n// CUDA faults such as illegal-address/device-assert are sticky at the context level. Once one is\n// observed, stop the client, flush funds-critical state, tear workers down, and restart the whole\n// process. Rebuilding CUDA workers in-process is not a valid recovery for these errors.\nstatic FATAL_GPU_FAULT: AtomicBool = AtomicBool::new(false);\n\npub fn fatal_gpu_fault() -> bool {\n    FATAL_GPU_FAULT.load(Ordering::Acquire)\n}\n\nfn is_fatal_cuda_error_message(message: &str) -> bool {\n    let s = message.to_ascii_lowercase();\n    s.contains("illegal memory access")\n        || s.contains("illegal address")\n        || s.contains("cuda_error_illegal_address")\n        || s.contains("device-side assert")\n        || s.contains("cuda_error_assert")\n        || s.contains("hardware stack error")\n        || s.contains("illegal instruction")\n        || s.contains("misaligned address")\n        || s.contains("invalid address space")\n        || s.contains("invalid pc")\n        || s.contains("invalid program counter")\n        || s.contains("launch failure")\n        || s.contains("launch failed")\n}\n\nfn mark_fatal_gpu_fault(device: &str, message: &str) {\n    if !FATAL_GPU_FAULT.swap(true, Ordering::AcqRel) {\n        error!("{}: fatal CUDA fault: {}. A full process restart is required.", device, message);\n    }\n}\n\n#[cfg(any(target_os = "linux", target_os = "macos"))]\n''',
    "fatal CUDA state",
)

replace_once(
    "src/miner.rs",
    '''#[cfg(any(target_os = "windows"))]\nstruct RawHandle(*mut std::ffi::c_void);\n\n#[cfg(any(target_os = "windows"))]\nunsafe impl Send for RawHandle {}\n\n#[cfg(any(target_os = "windows"))]\nfn register_freeze_handler() {}\n\n#[cfg(target_os = "windows")]\nfn trigger_freeze_handler(kill_switch: Arc<AtomicBool>, handle: &MinerHandler) -> std::thread::JoinHandle<()> {\n    use std::os::windows::io::AsRawHandle;\n    let raw_handle = RawHandle(handle.as_raw_handle());\n\n    std::thread::spawn(move || unsafe {\n        let ensure_full_move = raw_handle;\n        sleep(Duration::from_millis(1000));\n        if kill_switch.load(Ordering::SeqCst) {\n            kernel32::TerminateThread(ensure_full_move.0, 0);\n        }\n    })\n}\n''',
    '''#[cfg(any(target_os = "windows"))]\nfn register_freeze_handler() {}\n\n#[cfg(target_os = "windows")]\nfn trigger_freeze_handler(kill_switch: Arc<AtomicBool>, _handle: &MinerHandler) -> std::thread::JoinHandle<()> {\n    std::thread::spawn(move || {\n        // Never TerminateThread a Rust/CUDA worker: it bypasses destructors and can leave CUDA\n        // state poisoned inside a still-running process. Give cooperative shutdown the same grace\n        // as Unix; if it still hangs, terminate the whole process so a supervisor can restart it.\n        sleep(Duration::from_millis(30_000));\n        if kill_switch.load(Ordering::SeqCst) {\n            error!("GPU worker did not stop within 30s; terminating process instead of force-killing a CUDA thread");\n            std::process::exit(70);\n        }\n    })\n}\n''',
    "remove Windows TerminateThread",
)

replace_once(
    "src/miner.rs",
    '''                            keryx_miner::pom_gpu::ensure_installed(worker_device_id, daa);\n                        }\n                        let h3 = daa >= keryx_miner::pom::pom_level_activation_daa();\n''',
    '''                            keryx_miner::pom_gpu::ensure_installed(worker_device_id, daa);\n                            if keryx_miner::pom_gpu::fatal_gpu_fault() {\n                                mark_fatal_gpu_fault(&device_id, "PoM reported a sticky CUDA runtime fault while rebuilding");\n                                return Err("fatal CUDA fault during PoM rebuild".into());\n                            }\n                        }\n                        let h3 = daa >= keryx_miner::pom::pom_level_activation_daa();\n''',
    "propagate PoM rebuild fatal fault",
)

replace_once(
    "src/miner.rs",
    '''                        let batch = if v4 { pom_v4_batch } else if v3 { POM_V3_BATCH } else { POM_BATCH };\n                        let found = keryx_miner::pom_gpu::mine(worker_device_id, &pph, time, &target_le, pom_nonce, batch, h3, walk_v2, h5_1, h5_2, v3, v4, h10);\n                        pom_nonce = pom_nonce.wrapping_add(batch);\n''',
    '''                        let batch = if v4 { pom_v4_batch } else if v3 { POM_V3_BATCH } else { POM_BATCH };\n                        let found = keryx_miner::pom_gpu::mine(worker_device_id, &pph, time, &target_le, pom_nonce, batch, h3, walk_v2, h5_1, h5_2, v3, v4, h10);\n                        if keryx_miner::pom_gpu::fatal_gpu_fault() {\n                            mark_fatal_gpu_fault(&device_id, "PoM reported a sticky CUDA runtime fault while mining");\n                            return Err("fatal CUDA fault during PoM mining".into());\n                        }\n                        pom_nonce = pom_nonce.wrapping_add(batch);\n''',
    "propagate PoM mining fatal fault",
)

replace_once(
    "src/miner.rs",
    '''                    state_ref.pow_gpu(gpu_work);\n                    if let Err(e) = gpu_work.sync() {\n                        warn!("CUDA run ignored: {}", e);\n                        continue\n                    }\n\n                    gpu_work.copy_output_to(&mut nonces)?;\n''',
    '''                    state_ref.pow_gpu(gpu_work);\n                    if let Err(e) = gpu_work.sync() {\n                        let message = e.to_string();\n                        if is_fatal_cuda_error_message(&message) {\n                            mark_fatal_gpu_fault(&device_id, &message);\n                            return Err(e);\n                        }\n                        warn!("CUDA run ignored: {}", e);\n                        continue\n                    }\n\n                    if let Err(e) = gpu_work.copy_output_to(&mut nonces) {\n                        let message = e.to_string();\n                        if is_fatal_cuda_error_message(&message) {\n                            mark_fatal_gpu_fault(&device_id, &message);\n                        }\n                        return Err(e);\n                    }\n''',
    "do not ignore sticky legacy CUDA faults",
)

replace_once(
    "src/pom_gpu.rs",
    '''use anyhow::{anyhow, Result};\nuse log::{info, warn};\n''',
    '''use anyhow::{anyhow, Result};\nuse log::{error, info, warn};\n''',
    "import error logger",
)

replace_once(
    "src/pom_gpu.rs",
    '''pub fn inference_paused() -> bool {\n    INFERENCE_PAUSED.load(Ordering::Acquire)\n}\n\n/// True while the GPU miner is being (re)built — a heavy one-time model load that blocks the\n''',
    '''pub fn inference_paused() -> bool {\n    INFERENCE_PAUSED.load(Ordering::Acquire)\n}\n\n// Sticky CUDA exceptions cannot be recovered by dropping/rebuilding CudaContext objects in the\n// same process. The binary watches this flag, flushes client/escrow state, and exits so the next\n// process gets genuinely fresh driver state.\nstatic FATAL_GPU_FAULT: AtomicBool = AtomicBool::new(false);\n\npub fn fatal_gpu_fault() -> bool {\n    FATAL_GPU_FAULT.load(Ordering::Acquire)\n}\n\nfn mark_fatal_gpu_fault(device_id: u32, message: &str) {\n    if !FATAL_GPU_FAULT.swap(true, Ordering::AcqRel) {\n        error!("PoM[gpu{}]: fatal sticky CUDA fault: {}. Full process restart required.", device_id, message);\n    }\n}\n\n/// True while the GPU miner is being (re)built — a heavy one-time model load that blocks the\n''',
    "PoM fatal CUDA state",
)

replace_once(
    "src/pom_gpu.rs",
    '''    let miner = {\n        let g = miners().lock().ok()?;\n        g.get(&device_id)?.clone()\n    };\n    miner.mine(pre_pow_hash, timestamp, target_le, start, batch, h3, walk_v2, h5_1, h5_2, v3, v4, seed_h10).ok().flatten()\n}\n''',
    '''    let miner = {\n        let g = miners().lock().ok()?;\n        g.get(&device_id)?.clone()\n    };\n    match miner.mine(pre_pow_hash, timestamp, target_le, start, batch, h3, walk_v2, h5_1, h5_2, v3, v4, seed_h10) {\n        Ok(found) => found,\n        Err(e) => {\n            let message = e.to_string();\n            if is_sticky_gpu_runtime_fault(&message) {\n                mark_fatal_gpu_fault(device_id, &message);\n            } else {\n                warn!("PoM[gpu{}]: mining call failed: {}", device_id, message);\n            }\n            None\n        }\n    }\n}\n''',
    "stop swallowing PoM mining errors",
)

replace_once(
    "src/pom_gpu.rs",
    '''fn is_transient_gpu_runtime_fault(err: &str) -> bool {\n    let s = err.to_ascii_lowercase();\n    s.contains("illegal address")\n        || s.contains("illegal memory")\n        || s.contains("cuda_error_illegal_address")\n        || s.contains("invalid device pointer")\n        || s.contains("misaligned address")\n}\n\nfn reset_stale_gpu_state(device_id: u32, use_llama: bool) {\n    // Order matters: the miner walks llama's resident tensors, so it must be released — and any\n    // in-flight walk drained — before those tensors are freed.\n    uninstall(device_id);\n    if use_llama {\n        crate::llama_engine::unload_for_gpu(device_id as usize);\n    }\n}\n''',
    '''fn is_sticky_gpu_runtime_fault(err: &str) -> bool {\n    let s = err.to_ascii_lowercase();\n    s.contains("illegal address")\n        || s.contains("illegal memory")\n        || s.contains("cuda_error_illegal_address")\n        || s.contains("device-side assert")\n        || s.contains("cuda_error_assert")\n        || s.contains("hardware stack error")\n        || s.contains("illegal instruction")\n        || s.contains("misaligned address")\n        || s.contains("invalid address space")\n        || s.contains("invalid pc")\n        || s.contains("launch failure")\n}\n''',
    "classify sticky PoM CUDA faults",
)

replace_once(
    "src/pom_gpu.rs",
    '''        Ok(Err(e)) => {\n            let e_msg = e.to_string();\n            if is_transient_gpu_runtime_fault(&e_msg) {\n                log::warn!(\n                    "PoM[gpu{}]: transient GPU runtime fault while loading miner ({}); dropping stale miner state and forcing a rebuild on the next cycle.",\n                    device_id,\n                    e_msg\n                );\n                reset_stale_gpu_state(device_id, use_llama);\n                return false;\n            }\n            match classify_miner_load_error(&e_msg) {\n''',
    '''        Ok(Err(e)) => {\n            let e_msg = e.to_string();\n            if is_sticky_gpu_runtime_fault(&e_msg) {\n                // The llama ownership gate above documents why this is fatal:\n                // CUDA_ERROR_ILLEGAL_ADDRESS poisons the primary context until process restart.\n                mark_fatal_gpu_fault(device_id, &e_msg);\n                return false;\n            }\n            match classify_miner_load_error(&e_msg) {\n''',
    "treat PoM load illegal-address as fatal",
)

replace_once(
    "plugins/cuda/src/worker.rs",
    '''                error!("Failed to load {} PTX (driver too old?): {}", label, e);\n''',
    '''                error!("Failed to load {} PTX: {}", label, e);\n''',
    "remove misleading PTX driver diagnosis",
)

print("CUDA reconnect/context poisoning fix applied successfully")
