# EthSyncAuditor

A cross-implementation auditor that extracts a **Logical Synchronization
Graph (LSG)** from each Ethereum consensus client.


## Quick start

### 1. Set up the environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Fetch client sources

```bash
./getcode.sh
```

### 3. Configure an LLM API key

Create a `.env` file:

```bash
# Pick one
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...

# Optional: custom base URL (proxy / private deployment)
ANTHROPIC_BASE_URL=
GEMINI_BASE_URL=
```

The CLI flags `--anthropic-base-url` / `--gemini-base-url` override these.

### 4. Run the auditor

```bash
# use gemini
python main.py --provider gemini
# or anthropic.
python main.py --provider anthropic

# Resume from the latest checkpoint
python main.py --resume

```


## Output artifacts

| Path | Contents |
|---|---|
| `output/Global_LSG_Spec_Enriched.yaml` | Vocabulary Enricher result: merged Guard / Action vocabulary |
| `output/iterations/LSG_<client>_iter<N>.yaml` | Instance Generator intermediate per-iteration LSG |
| `output/LSG_<client>_final.yaml` | Instance Generator final LSG (×5) |
| `output/Audit_Diff_Report.md` / `.json` | Diff Verifier output: every cross-client difference that passed verification, with workflow, deviating client, severity, and source-code evidence |
| `output/Audit_False_Positives.md` | Items rejected by the Diff Verifier |
| `output/checkpoints/checkpoint_phase<P>_iter<N>.json` | Per-iteration checkpoint, used by `--resume` |
| `output/audit_logs/*.json` | Per-LLM-call prompt / response audit trail |
| `output/preprocess/` | Symbol tables, call graphs, vector & BM25 indices (reused on rerun) |


