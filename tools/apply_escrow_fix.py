from pathlib import Path
import re

ESCROW = Path("src/escrow.rs")
CARGO = Path("Cargo.toml")
POM_GPU = Path("src/pom_gpu.rs")

text = ESCROW.read_text(encoding="utf-8")

pattern = re.compile(
    r"        let mut batch: Vec<usize> = Vec::new\(\);.*?"
    r"        if !selected \{\n            return None;\n        \}\n",
    re.S,
)

replacement = '''        let mut batch: Vec<usize> = Vec::new();
        let mut selected = false;

        for pass in 0..3u8 {
            let mut indices: Vec<usize> = (0..self.state.entries.len()).collect();
            if pass == 0 {
                // Inference escrows keep priority in the normal pass, as before.
                indices.sort_by_key(|&i| !self.state.entries[i].is_inference);
            }

            // One-time legacy coinbase catch-up. Pre-H6 coinbase escrows use the
            // 36k window and can never reach the 87-input nominal batch once H6
            // coinbases have moved to 792k. Claim them together first, then the
            // normal H6 792k cycle resumes on the next block.
            if pass == 0 {
                let legacy: Vec<usize> = indices
                    .iter()
                    .copied()
                    .filter(|&i| {
                        let e = &self.state.entries[i];
                        !e.claimed
                            && !e.slashed
                            && !e.is_inference
                            && e.csv_window == CHALLENGE_WINDOW_BLOCKS
                            && e.batch_cap == 0
                            && !e.orphan_slashed
                            && e.orphan_retries == 0
                            && daa_score >= e.confirm_daa + CHALLENGE_WINDOW_BLOCKS + 10
                            && e.orphan_retry_after_daa.map_or(true, |retry_daa| daa_score >= retry_daa)
                            && !self
                                .in_flight_outpoints
                                .contains(&format!("{}:{}", e.coinbase_txid, e.output_index))
                    })
                    .take(MAX_CLAIM_BATCH)
                    .collect();

                let legacy_total: u64 = legacy
                    .iter()
                    .map(|&i| self.state.entries[i].amount_sompi)
                    .sum();
                if !legacy.is_empty() && legacy_total > CLAIM_FEE_SOMPI {
                    batch = legacy;
                    selected = true;
                    break;
                }
            }

            // Select one CSV window at a time. A claim transaction cannot safely
            // mix 36k and 792k inputs because every input carries the same sequence
            // value. The old implementation let the first window encountered lock
            // the whole batch, so a small legacy 36k group starved the large 792k group.
            let mut windows: Vec<u64> = Vec::new();
            for &i in &indices {
                let e = &self.state.entries[i];
                let proven_dead = e.orphan_slashed || e.orphan_retries > 0;
                let in_pass = match pass {
                    0 => !proven_dead && e.batch_cap == 0,
                    1 => !proven_dead && e.batch_cap != 0,
                    _ => proven_dead,
                };
                let eligible = in_pass
                    && !e.claimed
                    && !e.slashed
                    && daa_score >= e.confirm_daa + CHALLENGE_WINDOW_BLOCKS + 10
                    && e.orphan_retry_after_daa.map_or(true, |retry_daa| daa_score >= retry_daa)
                    && !self
                        .in_flight_outpoints
                        .contains(&format!("{}:{}", e.coinbase_txid, e.output_index));
                if eligible && !windows.iter().any(|&w| w == e.csv_window) {
                    windows.push(e.csv_window);
                }
            }

            for window in windows {
                let mut candidate: Vec<usize> = Vec::new();
                let mut limit = MAX_CLAIM_BATCH;

                for &i in &indices {
                    let e = &self.state.entries[i];
                    if e.csv_window != window {
                        continue;
                    }
                    let proven_dead = e.orphan_slashed || e.orphan_retries > 0;
                    let in_pass = match pass {
                        0 => !proven_dead && e.batch_cap == 0,
                        1 => !proven_dead && e.batch_cap != 0,
                        _ => proven_dead,
                    };
                    let eligible = in_pass
                        && !e.claimed
                        && !e.slashed
                        && daa_score >= e.confirm_daa + CHALLENGE_WINDOW_BLOCKS + 10
                        && e.orphan_retry_after_daa.map_or(true, |retry_daa| daa_score >= retry_daa)
                        && !self
                            .in_flight_outpoints
                            .contains(&format!("{}:{}", e.coinbase_txid, e.output_index));
                    if !eligible {
                        continue;
                    }

                    let cap = if e.batch_cap == 0 {
                        MAX_CLAIM_BATCH
                    } else {
                        (e.batch_cap as usize).min(MAX_CLAIM_BATCH)
                    };
                    if cap.min(limit) < candidate.len() + 1 {
                        continue;
                    }
                    limit = limit.min(cap);
                    candidate.push(i);
                    if candidate.len() >= limit {
                        break;
                    }
                }

                let releasable = if pass == 0 {
                    candidate.len() >= MIN_CLAIM_BATCH
                } else {
                    !candidate.is_empty()
                };

                if releasable {
                    batch = candidate;
                    selected = true;
                    break;
                }
            }

            if selected {
                break;
            }
        }

        if !selected {
            return None;
        }
'''

if not pattern.search(text):
    raise SystemExit("escrow selection block not found; refusing to patch")
text = pattern.sub(replacement, text, count=1)

marker = "    fn csv_window_follows_the_gate()"
test = '''    #[test]
    fn legacy_escrows_are_claimed_before_h6_batch_and_h6_cycle_continues() {
        let dir = tempfile::tempdir().unwrap();
        let state_path = dir.path().join("escrow_state.json");
        let mut watcher = EscrowWatcher::new(
            &"11".repeat(32),
            TEST_PAYOUT_ADDRESS,
            state_path,
        )
        .unwrap();

        // Reproduce the observed backlog: 16 legacy coinbase escrows at 36k,
        // followed by a large H6 backlog at 792k.
        watcher.state.entries = (0..16)
            .map(|i| EscrowEntry {
                coinbase_txid: format!("{:064x}", i + 1),
                block_hash: format!("{:064x}", 10_000 + i),
                confirm_daa: 0,
                amount_sompi: 108_000_000,
                output_index: 1,
                claimed: false,
                slashed: false,
                orphan_slashed: false,
                orphan_retries: 0,
                orphan_retry_after_daa: None,
                submit_retries: 0,
                batch_cap: 0,
                cap_set_daa: 0,
                is_inference: false,
                csv_window: CHALLENGE_WINDOW_BLOCKS,
            })
            .chain((0..100).map(|i| EscrowEntry {
                coinbase_txid: format!("{:064x}", 20_000 + i),
                block_hash: format!("{:064x}", 30_000 + i),
                confirm_daa: 0,
                amount_sompi: 108_000_000,
                output_index: 1,
                claimed: false,
                slashed: false,
                orphan_slashed: false,
                orphan_retries: 0,
                orphan_retry_after_daa: None,
                submit_retries: 0,
                batch_cap: 0,
                cap_set_daa: 0,
                is_inference: false,
                csv_window: SERVICE_BOND_CSV_WINDOW_BLOCKS,
            }))
            .collect();
        watcher.rebuild_indexes();

        let legacy_claim = watcher.find_claim(1_000_000).expect("legacy claim must be built");
        assert_eq!(legacy_claim.inputs.len(), 16);
        assert!(legacy_claim.inputs.iter().all(|i| i.sequence == CHALLENGE_WINDOW_BLOCKS));

        let legacy_txid = watcher.in_flight.keys().next().cloned().unwrap();
        assert!(matches!(
            watcher.on_submit_response(&legacy_txid, None),
            SubmitResponseOutcome::Accepted { outputs: 16, .. }
        ));

        let h6_claim = watcher.find_claim(1_000_000).expect("H6 claim must follow legacy cleanup");
        assert_eq!(h6_claim.inputs.len(), MAX_CLAIM_BATCH);
        assert!(h6_claim.inputs.iter().all(|i| i.sequence == SERVICE_BOND_CSV_WINDOW_BLOCKS));
    }

'''
if marker not in text:
    raise SystemExit("test insertion marker not found; refusing to patch")
text = text.replace(marker, test + marker, 1)
ESCROW.write_text(text, encoding="utf-8", newline="\n")

cargo = CARGO.read_text(encoding="utf-8")
if 'version = "0.4.8"' not in cargo:
    raise SystemExit("Cargo version 0.4.8 not found")
CARGO.write_text(cargo.replace('version = "0.4.8"', 'version = "0.4.9-escrow-fix-test.1"', 1), encoding="utf-8", newline="\n")

# The hosted runner is used only to validate the escrow fix and produce a Windows
# test binary for the user's RTX 5080 (sm_89). Keep this CUDA narrowing out of the
# actual commit; the release source retains the complete 7-image PTX ladder.
pom = POM_GPU.read_text(encoding="utf-8")
old_ptx = '''const PTX_SM90: &str = include_str!(concat!(env!("OUT_DIR"), "/pom_mine_sm90.ptx"));
const PTX_SM89: &str = include_str!(concat!(env!("OUT_DIR"), "/pom_mine_sm89.ptx"));
const PTX_SM86: &str = include_str!(concat!(env!("OUT_DIR"), "/pom_mine_sm86.ptx"));
const PTX_SM80: &str = include_str!(concat!(env!("OUT_DIR"), "/pom_mine_sm80.ptx"));
const PTX_SM75: &str = include_str!(concat!(env!("OUT_DIR"), "/pom_mine_sm75.ptx"));
const PTX_SM70: &str = include_str!(concat!(env!("OUT_DIR"), "/pom_mine_sm70.ptx"));
const PTX_SM61: &str = include_str!(concat!(env!("OUT_DIR"), "/pom_mine_sm61.ptx"));
const FATBIN_LEGACY: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/pom_mine_legacy.fatbin"));'''
new_ptx = '''const PTX_SM89: &str = include_str!(concat!(env!("OUT_DIR"), "/pom_mine_sm89.ptx"));
const FATBIN_LEGACY: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/pom_mine_legacy.fatbin"));'''
if old_ptx not in pom:
    raise SystemExit("PoM PTX constant block not found; refusing to narrow CI build")
pom = pom.replace(old_ptx, new_ptx, 1)
old_candidates = '''const POM_PTX_CANDIDATES: [(&str, &str, &str); 7] = [
    ("pom_mine_mod_sm90", "sm_90", PTX_SM90),
    ("pom_mine_mod_sm89", "sm_89", PTX_SM89),
    ("pom_mine_mod_sm86", "sm_86", PTX_SM86),
    ("pom_mine_mod_sm80", "sm_80", PTX_SM80),
    ("pom_mine_mod_sm75", "sm_75", PTX_SM75),
    ("pom_mine_mod_sm70", "sm_70", PTX_SM70),
    ("pom_mine_mod_sm61", "sm_61", PTX_SM61),
];'''
new_candidates = '''const POM_PTX_CANDIDATES: [(&str, &str, &str); 1] = [
    ("pom_mine_mod_sm89", "sm_89", PTX_SM89),
];'''
if old_candidates not in pom:
    raise SystemExit("PoM PTX candidate block not found; refusing to narrow CI build")
pom = pom.replace(old_candidates, new_candidates, 1)
POM_GPU.write_text(pom, encoding="utf-8", newline="\n")

print("escrow fix patch applied")
