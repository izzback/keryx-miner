from pathlib import Path

cargo = Path("Cargo.toml")
escrow = Path("src/escrow.rs")

cargo_text = cargo.read_text(encoding="utf-8")
old_version = 'version = "0.4.9-escrow-fix-test.4"'
new_version = 'version = "0.4.9-escrow-diagnostic-test.5"'
if old_version not in cargo_text and new_version not in cargo_text:
    raise SystemExit("expected test.4 Cargo version not found")
cargo_text = cargo_text.replace(old_version, new_version, 1)
cargo.write_text(cargo_text, encoding="utf-8")

text = escrow.read_text(encoding="utf-8")

def replace_once(old: str, new: str, label: str) -> None:
    global text
    if new in text:
        return
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, got {count}")
    text = text.replace(old, new, 1)

replace_once(
    "const MAX_IN_FLIGHT_CLAIMS: usize = 4;\n",
    "const MAX_IN_FLIGHT_CLAIMS: usize = 4;\n"
    "/// Diagnostic prerelease safety switch: submit at most one escrow claim per process,\n"
    "/// print the node's full rejection, and never persist watcher state to disk.\n"
    "const ESCROW_DIAGNOSTIC_ONE_SHOT: bool = true;\n",
    "diagnostic const",
)

replace_once(
    "    /// Entries purged by boot-time validation, for the completion log line.\n"
    "    validation_purged: u64,\n",
    "    /// Entries purged by boot-time validation, for the completion log line.\n"
    "    validation_purged: u64,\n"
    "    /// test.5 diagnostic guard: once a claim has been submitted, never build another.\n"
    "    diagnostic_claim_attempted: bool,\n",
    "diagnostic field",
)

replace_once(
    "            validation_pending: HashSet::new(),\n"
    "            validation_purged: 0,\n",
    "            validation_pending: HashSet::new(),\n"
    "            validation_purged: 0,\n"
    "            diagnostic_claim_attempted: false,\n",
    "diagnostic init",
)

replace_once(
    "    fn mark_dirty(&mut self) {\n"
    "        self.dirty = true;\n"
    "    }\n",
    "    fn mark_dirty(&mut self) {\n"
    "        // test.5 is intentionally read-only on disk: validation may quarantine ghosts\n"
    "        // in memory so the one diagnostic claim is well-formed, but no watcher mutation\n"
    "        // (slash/claim/retry/new tracking) is persisted to escrow_state.json.\n"
    "        if !ESCROW_DIAGNOSTIC_ONE_SHOT {\n"
    "            self.dirty = true;\n"
    "        }\n"
    "    }\n",
    "read-only mark_dirty",
)

replace_once(
    "    fn find_claim(&mut self, daa_score: u64) -> Option<RpcTransaction> {\n"
    "        if self.in_flight.len() >= MAX_IN_FLIGHT_CLAIMS {\n",
    "    fn find_claim(&mut self, daa_score: u64) -> Option<RpcTransaction> {\n"
    "        if ESCROW_DIAGNOSTIC_ONE_SHOT && self.diagnostic_claim_attempted {\n"
    "            return None;\n"
    "        }\n"
    "        if self.in_flight.len() >= MAX_IN_FLIGHT_CLAIMS {\n",
    "one-shot find_claim guard",
)

replace_once(
    "                let outpoints: Vec<(String, u32)> =\n"
    "                    entries.iter().map(|e| (e.coinbase_txid.clone(), e.output_index)).collect();\n"
    "                for (t, i) in &outpoints {\n",
    "                let outpoints: Vec<(String, u32)> =\n"
    "                    entries.iter().map(|e| (e.coinbase_txid.clone(), e.output_index)).collect();\n"
    "                if ESCROW_DIAGNOSTIC_ONE_SHOT {\n"
    "                    self.diagnostic_claim_attempted = true;\n"
    "                    warn!(\n"
    "                        \"ESCROW DIAGNOSTIC test.5: submitting ONE claim only: txid={}, outputs={}, csv_window={}, total={:.8} KRX. Further claims are disabled until restart; escrow_state.json is read-only.\",\n"
    "                        claim_txid,\n"
    "                        entries.len(),\n"
    "                        entries.first().map(|e| e.csv_window).unwrap_or(0),\n"
    "                        total as f64 / 1e8\n"
    "                    );\n"
    "                }\n"
    "                for (t, i) in &outpoints {\n",
    "one-shot submission log",
)

needle = "                if !burned_set.is_empty() {\n                    let mut burned_outpoints: Vec<String> =\n                        burned_set.iter().map(|(txid, index)| format!(\"{}:{}\", txid, index)).collect();\n                    burned_outpoints.sort();\n                    debug!(\n                        \"EscrowWatcher: node reported {} burned escrow outpoint(s) for claim {}: {:?}\",\n                        burned_outpoints.len(),\n                        claim_txid,\n                        burned_outpoints\n                    );\n                }\n"
insert = needle + "                if ESCROW_DIAGNOSTIC_ONE_SHOT {\n                    let mut submitted: Vec<String> = claim\n                        .outpoints\n                        .iter()\n                        .map(|(txid, index)| format!(\"{}:{}\", txid, index))\n                        .collect();\n                    submitted.sort();\n                    let mut burned: Vec<String> = burned_set\n                        .iter()\n                        .map(|(txid, index)| format!(\"{}:{}\", txid, index))\n                        .collect();\n                    burned.sort();\n                    warn!(\"ESCROW DIAGNOSTIC test.5 RAW NODE REJECTION for claim {}: {}\", claim_txid, msg);\n                    warn!(\"ESCROW DIAGNOSTIC test.5 submitted {} outpoint(s): {:?}\", submitted.len(), submitted);\n                    warn!(\"ESCROW DIAGNOSTIC test.5 parsed {} burned outpoint(s): {:?}\", burned.len(), burned);\n                    warn!(\"ESCROW DIAGNOSTIC test.5 COMPLETE: rejection caused NO escrow entry mutation and NO state-file write; all further claims are disabled until restart.\");\n                    return SubmitResponseOutcome::Handled;\n                }\n"
replace_once(needle, insert, "diagnostic rejection early-return")

replace_once(
    "            None => {\n"
    "                info!(\"EscrowWatcher: claim accepted ({} output(s), txid={})\", n_outputs, claim_txid);\n",
    "            None => {\n"
    "                info!(\"EscrowWatcher: claim accepted ({} output(s), txid={})\", n_outputs, claim_txid);\n"
    "                if ESCROW_DIAGNOSTIC_ONE_SHOT {\n"
    "                    warn!(\"ESCROW DIAGNOSTIC test.5: one-shot claim was ACCEPTED. In-memory state is updated for this process, but escrow_state.json remains unchanged on disk. Discard this diagnostic state copy after the test.\");\n"
    "                }\n",
    "diagnostic accepted log",
)

escrow.write_text(text, encoding="utf-8")
print("Applied escrow diagnostic test.5 source patch")
