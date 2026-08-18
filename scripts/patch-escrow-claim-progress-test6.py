from pathlib import Path

cargo = Path("Cargo.toml")
escrow = Path("src/escrow.rs")
ui = Path("src/ui.rs")

cargo_text = cargo.read_text(encoding="utf-8")
old_version = 'version = "0.4.9-escrow-fix-test.4"'
new_version = 'version = "0.4.9-escrow-claim-progress-test.6"'
if old_version not in cargo_text and new_version not in cargo_text:
    raise SystemExit("expected test.4 Cargo version not found")
cargo_text = cargo_text.replace(old_version, new_version, 1)
cargo.write_text(cargo_text, encoding="utf-8")

escrow_text = escrow.read_text(encoding="utf-8")
ui_text = ui.read_text(encoding="utf-8")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


escrow_text = replace_once(
    escrow_text,
    "use std::path::{Path, PathBuf};\nuse std::time::{Duration, Instant};\n",
    "use std::path::{Path, PathBuf};\n"
    "use std::sync::atomic::{AtomicU64, Ordering};\n"
    "use std::time::{Duration, Instant};\n",
    "escrow atomic imports",
)

escrow_text = replace_once(
    escrow_text,
    "const MIN_CLAIM_BATCH: usize = MAX_CLAIM_BATCH;\n"
    "/// A repair verdict (bisection cap, orphan-death flag) not re-confirmed by a fresh\n",
    "const MIN_CLAIM_BATCH: usize = MAX_CLAIM_BATCH;\n"
    "\n"
    "/// UI-only estimate of the next nominal claim batch. Keryx currently targets ~10 BPS;\n"
    "/// the DAA countdown is authoritative and wall-clock time is deliberately shown as an estimate.\n"
    "const CLAIM_PROGRESS_ESTIMATED_BPS: u64 = 10;\n"
    "const CLAIM_PROGRESS_UNAVAILABLE: u64 = u64::MAX;\n"
    "static CLAIM_PROGRESS_VALID: AtomicU64 = AtomicU64::new(0);\n"
    "static CLAIM_PROGRESS_ETA_DAA: AtomicU64 = AtomicU64::new(CLAIM_PROGRESS_UNAVAILABLE);\n"
    "\n"
    "#[derive(Debug, Clone, Copy, PartialEq, Eq)]\n"
    "pub struct ClaimProgressSnapshot {\n"
    "    pub valid_outputs: u64,\n"
    "    pub target_outputs: u64,\n"
    "    pub eta_daa: Option<u64>,\n"
    "    pub eta_seconds: Option<u64>,\n"
    "}\n"
    "\n"
    "pub fn claim_progress_snapshot() -> ClaimProgressSnapshot {\n"
    "    let eta_raw = CLAIM_PROGRESS_ETA_DAA.load(Ordering::Acquire);\n"
    "    let eta_daa = (eta_raw != CLAIM_PROGRESS_UNAVAILABLE).then_some(eta_raw);\n"
    "    let eta_seconds = eta_daa.map(|daa| {\n"
    "        daa.saturating_add(CLAIM_PROGRESS_ESTIMATED_BPS - 1) / CLAIM_PROGRESS_ESTIMATED_BPS\n"
    "    });\n"
    "    ClaimProgressSnapshot {\n"
    "        valid_outputs: CLAIM_PROGRESS_VALID.load(Ordering::Acquire),\n"
    "        target_outputs: MAX_CLAIM_BATCH as u64,\n"
    "        eta_daa,\n"
    "        eta_seconds,\n"
    "    }\n"
    "}\n"
    "\n"
    "/// A repair verdict (bisection cap, orphan-death flag) not re-confirmed by a fresh\n",
    "claim progress globals",
)

pending_fn = '''    /// Count and sum the escrow outputs still awaiting claim: tracked, not claimed, and
    /// not proven dead (entries solo-rejected as orphans are excluded — including them
    /// would inflate the figure with outpoints that will never pay).
    pub fn pending_escrow(&self) -> (u64, u64) {
        let mut outputs = 0u64;
        let mut sompi = 0u64;
        for e in &self.state.entries {
            if !e.claimed && !e.slashed && !e.orphan_slashed && e.orphan_retries == 0 {
                outputs += 1;
                sompi += e.amount_sompi;
            }
        }
        (outputs, sompi)
    }
'''
progress_fn = pending_fn + '''
    /// Publish the next nominal claim batch progress for the TUI.
    ///
    /// This mirrors the normal-pass eligibility rules used by `find_claim`: dead/repair
    /// entries, cooldowns and in-flight outpoints are excluded, and each CSV window is
    /// evaluated independently. The displayed cohort is the one that can reach 87 inputs
    /// first. This is especially important across the 36k -> 792k transition: a small
    /// legacy/inference cohort must never hide a nearly-ready 792k batch.
    fn update_claim_progress(&self, daa_score: u64) {
        // Mirror the one-time pre-H6 coinbase catch-up. It is allowed to ship a partial
        // 36k batch, so report it as ready even when it is below the normal 87 target.
        let mut legacy_ready = 0usize;
        let mut legacy_total = 0u64;
        for e in &self.state.entries {
            let in_flight = self
                .in_flight_outpoints
                .contains(&format!("{}:{}", e.coinbase_txid, e.output_index));
            if !e.claimed
                && !e.slashed
                && !e.is_inference
                && e.csv_window == CHALLENGE_WINDOW_BLOCKS
                && e.batch_cap == 0
                && !e.orphan_slashed
                && e.orphan_retries == 0
                && daa_score >= e.confirm_daa.saturating_add(CHALLENGE_WINDOW_BLOCKS).saturating_add(10)
                && e.orphan_retry_after_daa.map_or(true, |retry_daa| daa_score >= retry_daa)
                && !in_flight
            {
                legacy_ready += 1;
                legacy_total = legacy_total.saturating_add(e.amount_sompi);
                if legacy_ready >= MAX_CLAIM_BATCH {
                    break;
                }
            }
        }
        if legacy_ready > 0 && legacy_total > CLAIM_FEE_SOMPI {
            CLAIM_PROGRESS_VALID.store(legacy_ready as u64, Ordering::Release);
            CLAIM_PROGRESS_ETA_DAA.store(0, Ordering::Release);
            return;
        }

        let mut groups: HashMap<u64, Vec<u64>> = HashMap::new();
        for e in &self.state.entries {
            let proven_dead = e.orphan_slashed || e.orphan_retries > 0;
            if e.claimed || e.slashed || proven_dead || e.batch_cap != 0 {
                continue;
            }
            if self
                .in_flight_outpoints
                .contains(&format!("{}:{}", e.coinbase_txid, e.output_index))
            {
                continue;
            }

            let maturity_daa = e.confirm_daa.saturating_add(e.csv_window).saturating_add(10);
            let ready_daa = e
                .orphan_retry_after_daa
                .map_or(maturity_daa, |retry_daa| maturity_daa.max(retry_daa));
            groups.entry(e.csv_window).or_default().push(ready_daa);
        }

        let mut fallback_valid = 0usize;
        let mut best: Option<(u64, usize)> = None;
        for ready_scores in groups.values_mut() {
            ready_scores.sort_unstable();
            let valid_now = ready_scores
                .iter()
                .filter(|&&ready_daa| daa_score >= ready_daa)
                .count()
                .min(MAX_CLAIM_BATCH);
            fallback_valid = fallback_valid.max(valid_now);

            if ready_scores.len() < MIN_CLAIM_BATCH {
                continue;
            }
            let full_batch_daa = ready_scores[MIN_CLAIM_BATCH - 1];
            let eta_daa = full_batch_daa.saturating_sub(daa_score);
            match best {
                None => best = Some((eta_daa, valid_now)),
                Some((best_eta, best_valid))
                    if eta_daa < best_eta || (eta_daa == best_eta && valid_now > best_valid) =>
                {
                    best = Some((eta_daa, valid_now));
                }
                _ => {}
            }
        }

        let (valid, eta) = best
            .map(|(eta, valid)| (valid, eta))
            .unwrap_or((fallback_valid, CLAIM_PROGRESS_UNAVAILABLE));
        CLAIM_PROGRESS_VALID.store(valid as u64, Ordering::Release);
        CLAIM_PROGRESS_ETA_DAA.store(eta, Ordering::Release);
    }
'''
escrow_text = replace_once(escrow_text, pending_fn, progress_fn, "claim progress method")

escrow_text = replace_once(
    escrow_text,
    "        let claim = self.find_claim(daa_score);\n"
    "        self.maybe_flush();\n",
    "        // Publish progress before `find_claim` moves a ready batch into in-flight state,\n"
    "        // so the operator can actually see the transition to 87/87.\n"
    "        self.update_claim_progress(daa_score);\n"
    "        let claim = self.find_claim(daa_score);\n"
    "        self.maybe_flush();\n",
    "handle_block progress update",
)

escrow_text = replace_once(
    escrow_text,
    "        self.mark_dirty();\n"
    "        self.maybe_flush();\n"
    "        outcome\n"
    "    }\n\n"
    "    /// Mark every entry matching the given outpoints as claimed. Returns the total amount\n",
    "        self.update_claim_progress(self.last_daa_score);\n"
    "        self.mark_dirty();\n"
    "        self.maybe_flush();\n"
    "        outcome\n"
    "    }\n\n"
    "    /// Mark every entry matching the given outpoints as claimed. Returns the total amount\n",
    "submit response progress update",
)

ui_text = replace_once(
    ui_text,
    "    let escrow_pending_value = format!(\n"
    "        \"{} ({:.2} KRX)\",\n"
    "        snapshot.escrow_pending_outputs,\n"
    "        snapshot.escrow_pending_sompi as f64 / 1e8\n"
    "    );\n"
    "    let uptime_value = format_duration(snapshot.uptime_s);\n",
    "    let escrow_pending_value = format!(\n"
    "        \"{} ({:.2} KRX)\",\n"
    "        snapshot.escrow_pending_outputs,\n"
    "        snapshot.escrow_pending_sompi as f64 / 1e8\n"
    "    );\n"
    "    let claim_progress = crate::escrow::claim_progress_snapshot();\n"
    "    let claim_progress_value = match claim_progress.eta_seconds {\n"
    "        Some(0) => format!(\"{}/{} · ready\", claim_progress.valid_outputs, claim_progress.target_outputs),\n"
    "        Some(seconds) => format!(\n"
    "            \"{}/{} · ~{}\",\n"
    "            claim_progress.valid_outputs,\n"
    "            claim_progress.target_outputs,\n"
    "            format_duration(seconds)\n"
    "        ),\n"
    "        None => format!(\"{}/{} · waiting\", claim_progress.valid_outputs, claim_progress.target_outputs),\n"
    "    };\n"
    "    let uptime_value = format_duration(snapshot.uptime_s);\n",
    "ui claim progress value",
)

ui_text = replace_once(
    ui_text,
    "        metric_row(\n"
    "            \"Escrow Pending\",\n"
    "            escrow_pending_value.clone(),\n"
    "            if snapshot.escrow_pending_outputs > 0 { palette().text } else { palette().dim },\n"
    "        ),\n"
    "        metric_row(\n"
    "            \"Stats Updated\",\n",
    "        metric_row(\n"
    "            \"Escrow Pending\",\n"
    "            escrow_pending_value.clone(),\n"
    "            if snapshot.escrow_pending_outputs > 0 { palette().text } else { palette().dim },\n"
    "        ),\n"
    "        metric_row(\n"
    "            \"Next Claim\",\n"
    "            claim_progress_value,\n"
    "            if claim_progress.valid_outputs >= claim_progress.target_outputs {\n"
    "                palette().bright\n"
    "            } else if claim_progress.valid_outputs > 0 {\n"
    "                palette().text\n"
    "            } else {\n"
    "                palette().dim\n"
    "            },\n"
    "        ),\n"
    "        metric_row(\n"
    "            \"Stats Updated\",\n",
    "ui next claim row",
)

escrow.write_text(escrow_text, encoding="utf-8")
ui.write_text(ui_text, encoding="utf-8")
print("Applied escrow claim progress test.6 source patch")
