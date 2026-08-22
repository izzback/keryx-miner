#!/usr/bin/env python3
from pathlib import Path

P = Path("src/pom_gpu.rs")
s = P.read_text(encoding="utf-8")

MARKER = "POM_V4_TC_KERNEL_NAME"
if MARKER in s:
    print("pom_gpu.rs already wired for v4 tensor-core path; nothing to do")
    raise SystemExit(0)


def once(old: str, new: str, label: str) -> None:
    global s
    n = s.count(old)
    if n != 1:
        raise SystemExit(f"{label}: expected exactly 1 match, got {n}")
    s = s.replace(old, new, 1)
    print(f"patched: {label}")


def exact_count(old: str, new: str, count: int, label: str) -> None:
    global s
    n = s.count(old)
    if n != count:
        raise SystemExit(f"{label}: expected exactly {count} matches, got {n}")
    s = s.replace(old, new)
    print(f"patched: {label} ({count} matches)")


once(
'''const POM_V4_KERNEL_NAME: &str = "pom_mine_v4";
const POM_V4_SHARED_BYTES: u32 = 2048;''',
'''const POM_V4_KERNEL_NAME: &str = "pom_mine_v4";
const POM_V4_CHASE_KERNEL_NAME: &str = "pom_mine_v4_chase";
const POM_V4_TC_KERNEL_NAME: &str = "pom_mine_v4_tc";
const POM_V4_SHARED_BYTES: u32 = 2048;''',
"v4 kernel names",
)

once(
'''    function_v3: Option<sys::CUfunction>,
    function_v3_dump: Option<sys::CUfunction>,
    function_v4: Option<sys::CUfunction>,
}''',
'''    function_v3: Option<sys::CUfunction>,
    function_v3_dump: Option<sys::CUfunction>,
    function_v4: Option<sys::CUfunction>,
    function_v4_chase: Option<sys::CUfunction>,
    function_v4_tc: Option<sys::CUfunction>,
}''',
"LoadedPomKernel fields",
)

old_ctor = '''        let (function_v3, function_v3_dump) = load_v3_functions(module);
        let function_v4 = unsafe { result::module::get_function(module, CString::new(POM_V4_KERNEL_NAME).unwrap()) }.ok();
        Ok(Self { module, function, function_v3, function_v3_dump, function_v4 })'''
new_ctor = '''        let (function_v3, function_v3_dump) = load_v3_functions(module);
        let function_v4 = unsafe { result::module::get_function(module, CString::new(POM_V4_KERNEL_NAME).unwrap()) }.ok();
        let function_v4_chase = unsafe {
            result::module::get_function(module, CString::new(POM_V4_CHASE_KERNEL_NAME).unwrap())
        }
        .ok();
        let function_v4_tc = unsafe {
            result::module::get_function(module, CString::new(POM_V4_TC_KERNEL_NAME).unwrap())
        }
        .ok();
        Ok(Self {
            module,
            function,
            function_v3,
            function_v3_dump,
            function_v4,
            function_v4_chase,
            function_v4_tc,
        })'''
exact_count(old_ctor, new_ctor, 2, "load v4 TC symbols")

anchor = '''    /// v3 (H6) dump: re-walk ONE (winning) nonce and return (states S_0..=S_K concatenated,
'''
insert = r'''    fn has_v4_tc(&self) -> bool {
        self.function_v4_chase.is_some() && self.function_v4_tc.is_some()
    }

    /// PoM v4 two-phase tensor-core grind. Phase 1 resolves K tile offsets per nonce; phase 2
    /// walks those offsets with INT8 mma.sync and cp.async. The offsets allocation is cached by
    /// device outside this object. When `chase_stream` is Some, four sub-batches are event-ordered
    /// so chase(k+1) can overlap walk(k), matching the supr v0.11.6 dispatch strategy.
    #[allow(clippy::too_many_arguments)]
    fn launch_v4_tc(
        &self,
        stream: &Arc<CudaStream>,
        chase_stream: Option<&Arc<CudaStream>>,
        offsets: &CudaSlice<u32>,
        bases_dev: &CudaSlice<u64>,
        prefix_dev: &CudaSlice<u64>,
        t_count: u32,
        n_tiles: u64,
        p_words: &[u64; 4],
        s_words: &[u64; 4],
        timestamp: u64,
        target_le: &[u8; 32],
        start: u64,
        batch: u64,
    ) -> Result<Option<u64>> {
        if batch == 0 {
            return Ok(None);
        }
        let chase_fn = self
            .function_v4_chase
            .ok_or_else(|| anyhow!("PoM GPU: loaded kernel image has no pom_mine_v4_chase entry"))?;
        let walk_fn = self
            .function_v4_tc
            .ok_or_else(|| anyhow!("PoM GPU: loaded kernel image has no pom_mine_v4_tc entry"))?;
        let t = words4(target_le);
        let k = crate::pom_v4::POM_V4_K as u32;
        let winner = stream.clone_htod(&[u64::MAX])?;

        let (bases_ptr, _bases_guard) = bases_dev.device_ptr(stream);
        let (prefix_ptr, _prefix_guard) = prefix_dev.device_ptr(stream);
        let (offsets_ptr, _offsets_guard) = offsets.device_ptr(stream);
        let (winner_ptr, _winner_guard) = winner.device_ptr(stream);

        const TC_WARPS: u64 = 4;
        const SUBS: u64 = 4;
        let overlap = chase_stream.is_some() && batch >= SUBS * 4096;
        let n_sub = if overlap { SUBS } else { 1 };
        let sub = (batch + n_sub - 1) / n_sub;

        for i in 0..n_sub {
            let consumed = i * sub;
            if consumed >= batch {
                break;
            }
            let s_start = start.wrapping_add(consumed);
            let s_batch = sub.min(batch - consumed);
            // CUdeviceptr arithmetic is byte-addressed: each stored offset is one u32.
            let view_ptr = offsets_ptr + consumed * u64::from(k) * std::mem::size_of::<u32>() as u64;
            let chase_cfg = LaunchConfig {
                grid_dim: (((s_batch + 255) / 256) as u32, 1, 1),
                block_dim: (256, 1, 1),
                shared_mem_bytes: 0,
            };
            let mut chase_params: [*mut c_void; 13] = [
                (&bases_ptr as *const _ as *mut c_void),
                (&prefix_ptr as *const _ as *mut c_void),
                (&t_count as *const _ as *mut c_void),
                (&n_tiles as *const _ as *mut c_void),
                (&k as *const _ as *mut c_void),
                (&s_words[0] as *const _ as *mut c_void),
                (&s_words[1] as *const _ as *mut c_void),
                (&s_words[2] as *const _ as *mut c_void),
                (&s_words[3] as *const _ as *mut c_void),
                (&timestamp as *const _ as *mut c_void),
                (&s_start as *const _ as *mut c_void),
                (&s_batch as *const _ as *mut c_void),
                (&view_ptr as *const _ as *mut c_void),
            ];

            if let Some(cs) = chase_stream.filter(|_| overlap) {
                unsafe {
                    result::launch_kernel(
                        chase_fn,
                        chase_cfg.grid_dim,
                        chase_cfg.block_dim,
                        chase_cfg.shared_mem_bytes,
                        cs.cu_stream(),
                        &mut chase_params,
                    )
                }?;
                let event = cs.record_event(None)?;
                stream.wait(&event)?;
            } else {
                unsafe {
                    result::launch_kernel(
                        chase_fn,
                        chase_cfg.grid_dim,
                        chase_cfg.block_dim,
                        chase_cfg.shared_mem_bytes,
                        stream.cu_stream(),
                        &mut chase_params,
                    )
                }?;
            }

            let walk_cfg = LaunchConfig {
                grid_dim: (((s_batch + TC_WARPS - 1) / TC_WARPS) as u32, 1, 1),
                block_dim: ((TC_WARPS * 32) as u32, 1, 1),
                shared_mem_bytes: (TC_WARPS as u32) * 4096,
            };
            let mut walk_params: [*mut c_void; 21] = [
                (&bases_ptr as *const _ as *mut c_void),
                (&prefix_ptr as *const _ as *mut c_void),
                (&t_count as *const _ as *mut c_void),
                (&k as *const _ as *mut c_void),
                (&p_words[0] as *const _ as *mut c_void),
                (&p_words[1] as *const _ as *mut c_void),
                (&p_words[2] as *const _ as *mut c_void),
                (&p_words[3] as *const _ as *mut c_void),
                (&s_words[0] as *const _ as *mut c_void),
                (&s_words[1] as *const _ as *mut c_void),
                (&s_words[2] as *const _ as *mut c_void),
                (&s_words[3] as *const _ as *mut c_void),
                (&timestamp as *const _ as *mut c_void),
                (&t[0] as *const _ as *mut c_void),
                (&t[1] as *const _ as *mut c_void),
                (&t[2] as *const _ as *mut c_void),
                (&t[3] as *const _ as *mut c_void),
                (&s_start as *const _ as *mut c_void),
                (&s_batch as *const _ as *mut c_void),
                (&view_ptr as *const _ as *mut c_void),
                (&winner_ptr as *const _ as *mut c_void),
            ];
            unsafe {
                result::launch_kernel(
                    walk_fn,
                    walk_cfg.grid_dim,
                    walk_cfg.block_dim,
                    walk_cfg.shared_mem_bytes,
                    stream.cu_stream(),
                    &mut walk_params,
                )
            }?;
        }

        stream.synchronize()?;
        let w = stream.clone_dtoh(&winner)?[0];
        Ok(if w == u64::MAX { None } else { Some(w) })
    }

'''
once(anchor, insert + anchor, "tensor-core host launcher")

once(
'''    let is_nextgen_cc = is_nextgen_device(device_id);

    let fatbin_candidates:''',
'''    let is_nextgen_cc = is_nextgen_device(device_id);
    let cc = gpu_compute_capability(device_id);
    let tc_requested = std::env::var("KERYX_POM_V4_TC").ok().as_deref() != Some("0")
        && matches!(cc, Some((major, _)) if major >= 8);
    let mut classic_fallback: Option<(&str, &str, LoadedPomKernel)> = None;

    let fatbin_candidates:''',
"TC-aware kernel selection state",
)

once(
'''        match LoadedPomKernel::from_fatbin(label, fatbin) {
            Ok(kernel) => {
                let cc = gpu_compute_capability(device_id);''',
'''        match LoadedPomKernel::from_fatbin(label, fatbin) {
            Ok(kernel) => {
                if tc_requested && !kernel.has_v4_tc() {
                    warn!(
                        "PoM[gpu{}]: {} is compatible but has no v4 tensor-core symbols; trying PTX ladder",
                        device_id, label
                    );
                    if classic_fallback.is_none() {
                        classic_fallback = Some((module_name, label, kernel));
                    }
                    continue;
                }
                let cc = gpu_compute_capability(device_id);''',
"skip stale TC-less fatbin",
)

once(
'''    for (module_name, label, ptx) in POM_PTX_CANDIDATES {
        match LoadedPomKernel::from_ptx(label, ptx) {
            Ok(kernel) => {
                let cc = gpu_compute_capability(device_id);''',
'''    for (module_name, label, ptx) in POM_PTX_CANDIDATES {
        // A sub-sm80 PTX image contains the named TC stub but cannot execute mma.sync. Do not
        // mistake symbol presence for capability while looking for the accelerated path.
        if tc_requested && matches!(label, "sm_75" | "sm_70" | "sm_61") {
            continue;
        }
        match LoadedPomKernel::from_ptx(label, ptx) {
            Ok(kernel) => {
                if tc_requested && !kernel.has_v4_tc() {
                    warn!("PoM[gpu{}]: {} PTX has no v4 TC symbols; trying next image", device_id, label);
                    continue;
                }
                let cc = gpu_compute_capability(device_id);''',
"prefer TC-capable PTX",
)

once(
'''    Err(anyhow!("PoM GPU: no compatible PTX image for this device/driver"))
}''',
'''    if let Some((module_name, label, kernel)) = classic_fallback {
        warn!(
            "PoM[gpu{}]: tensor-core image unavailable; falling back to classic v4 via {}",
            device_id, label
        );
        set_gpu_kernel_info(device_id, cc, label, module_name);
        return Ok(kernel);
    }

    Err(anyhow!("PoM GPU: no compatible PTX image for this device/driver"))
}''',
"classic fallback after TC search",
)

cache_anchor = '''/// Total VRAM (MB) of every CUDA device, in **CUDA device order** — the same ordering
'''
cache_code = r'''/// Per-device cached v4 offset table. It is intentionally uninitialized: the chase kernel
/// overwrites every u32 before the TC walk consumes it. This removes cudaMalloc + memset + free
/// from every batch. The allocation grows monotonically and remains resident for the process.
static V4_OFFSETS: OnceLock<Mutex<HashMap<usize, Arc<CudaSlice<u32>>>>> = OnceLock::new();

fn v4_offsets_buf(stream: &Arc<CudaStream>, len: usize) -> Result<Arc<CudaSlice<u32>>> {
    let cache = V4_OFFSETS.get_or_init(|| Mutex::new(HashMap::new()));
    let ord = stream.context().ordinal();
    let mut guard = cache.lock().unwrap_or_else(|p| p.into_inner());
    if let Some(buf) = guard.get(&ord) {
        if buf.len() >= len {
            return Ok(Arc::clone(buf));
        }
    }
    let buf = Arc::new(unsafe { stream.alloc::<u32>(len)? });
    // stream-ordered alloc must be complete before a cached secondary stream can first use it.
    stream.synchronize()?;
    guard.insert(ord, Arc::clone(&buf));
    Ok(buf)
}

/// One reusable secondary stream per CUDA device for chase(k+1) / walk(k) overlap.
static V4_CHASE_STREAMS: OnceLock<Mutex<HashMap<usize, Arc<CudaStream>>>> = OnceLock::new();

fn v4_chase_stream(stream: &Arc<CudaStream>) -> Result<Arc<CudaStream>> {
    let cache = V4_CHASE_STREAMS.get_or_init(|| Mutex::new(HashMap::new()));
    let ord = stream.context().ordinal();
    let mut guard = cache.lock().unwrap_or_else(|p| p.into_inner());
    if let Some(s) = guard.get(&ord) {
        return Ok(Arc::clone(s));
    }
    let s = stream.context().new_stream()?;
    guard.insert(ord, Arc::clone(&s));
    Ok(s)
}

'''
once(cache_anchor, cache_code + cache_anchor, "v4 offset/stream caches")

mine_old = '''            return self.kernel.launch_v4(&self.stream, &self.bases_dev, &self.prefix_dev, self.t_count, n_tiles, &p_words, &s_words, timestamp, target_le, start, batch);
        }
        let p_words = crate::pom::pph_words_for_era(pre_pow_hash, h3);'''
mine_new = '''            if self.v4_tc_available() {
                let k = crate::pom_v4::POM_V4_K;
                let batch_usize = usize::try_from(batch).map_err(|_| anyhow!("PoM v4 batch does not fit usize"))?;
                let len = batch_usize
                    .checked_mul(k)
                    .ok_or_else(|| anyhow!("PoM v4 offsets allocation overflow"))?;
                let offsets = v4_offsets_buf(&self.stream, len)?;
                let overlap = std::env::var("KERYX_POM_V4_OVERLAP").ok().as_deref() != Some("0")
                    && batch >= 4 * 4096;
                let chase_stream = if overlap { Some(v4_chase_stream(&self.stream)?) } else { None };
                return self.kernel.launch_v4_tc(
                    &self.stream,
                    chase_stream.as_ref(),
                    offsets.as_ref(),
                    &self.bases_dev,
                    &self.prefix_dev,
                    self.t_count,
                    n_tiles,
                    &p_words,
                    &s_words,
                    timestamp,
                    target_le,
                    start,
                    batch,
                );
            }
            return self.kernel.launch_v4(&self.stream, &self.bases_dev, &self.prefix_dev, self.t_count, n_tiles, &p_words, &s_words, timestamp, target_le, start, batch);
        }
        let p_words = crate::pom::pph_words_for_era(pre_pow_hash, h3);'''
once(mine_old, mine_new, "v4 dispatch")

impl_anchor = '''    /// v3 dump for the winning nonce: (states S_0..=S_K, snippets, fold64(root_K)).
'''
avail = r'''    fn v4_tc_available(&self) -> bool {
        if std::env::var("KERYX_POM_V4_TC").ok().as_deref() == Some("0") {
            return false;
        }
        let ord = self.stream.context().ordinal();
        let cc = gpu_compute_capability(ord);
        let ok = matches!(cc, Some((major, _)) if major >= 8) && self.kernel.has_v4_tc();
        static LOGGED: OnceLock<Mutex<HashSet<usize>>> = OnceLock::new();
        let mut seen = LOGGED
            .get_or_init(|| Mutex::new(HashSet::new()))
            .lock()
            .unwrap_or_else(|p| p.into_inner());
        if seen.insert(ord) {
            match (ok, cc) {
                (true, Some((major, minor))) => info!(
                    "PoM v4[gpu{} cc{}.{}]: tensor-core solver active (chase + cp.async + mma.m16n8k32)",
                    ord, major, minor
                ),
                (_, Some((major, minor))) => info!(
                    "PoM v4[gpu{} cc{}.{}]: using classic dp4a solver",
                    ord, major, minor
                ),
                _ => info!("PoM v4[gpu{}]: using classic dp4a solver", ord),
            }
        }
        ok
    }

'''
once(impl_anchor, avail + impl_anchor, "TC capability dispatch")

P.write_text(s, encoding="utf-8")
print(f"wrote {P} ({len(s)} bytes)")
