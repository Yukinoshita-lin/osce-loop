# OSCE·Loop

An AI-assisted system for the full Objective Structured Clinical Examination (OSCE) workflow. The goal is not to replace examiners with AI, but to place AI where it is **verifiable, reversible, and auditable**: AI extracts evidence and proposes scores; humans retain sign-off, adjudication, and stop authority.

The full design document is available at [design/AI-OSCE-全流程设计方案.html](design/AI-OSCE-全流程设计方案.html) (Chinese).

## Core Capabilities

- **Rubric as protocol**: the four-tuple of behavior anchor, evidence type, dialogue phase, and AI authority drives derivation of SP information slots, checkpoints, evidence probes, and feedback templates.
- **Evidence-anchored scoring**: every AI suggestion must carry timestamped evidence; no evidence means no score. Integrity gaps are escalated to full human review.
- **Risk-based routing**: L0–L3 authority levels; critical items, low confidence, borderline cases, and appeals are routed to humans with named reasons.
- **SP persona consistency loop**: a deterministic state machine decides "what to say"; the LLM only decides "how to say it". An independent verifier checks persona fidelity, information boundaries, and clinical plausibility, falling back to a safe template on failure.
- **Gated rollout**: G0 offline copilot → G1 practice sandbox → G2 simulated exam → G3 limited high-stakes. Gates block progression until metrics pass; version changes trigger automatic authority downgrade.
- **Governance and traceability**: hash-chained audit, external anchors, sign-off chain, annual data-flow review, content audit, local threshold calibration, public model and data cards.
- **Fairness and experience**: subgroup score gaps, reviewer edit-rate gaps, feedback accessibility, SUS, usefulness, realism, and appeal/change rates are part of acceptance.
- **S9 metrics dashboard**: 21 acceptance indicators judged automatically; missing samples are never reported as passing; CSV export supported.

## Quick Start

Requires Python 3.11+ (developed and verified on Python 3.14).

```bash
pip install -r requirements.txt
python run.py
```

Open http://127.0.0.1:8000 . On first start the database, demo bundles, and demo accounts are initialized automatically.

### Demo Accounts

| Username | Password | Role |
| --- | --- | --- |
| `admin` | `admin123` | System administrator |
| `exam` | `exam123` | Exam manager |
| `reviewer` | `review123` | Examiner / rater |
| `auditor` | `audit123` | Auditor |
| `student` | `student123` | Student |

> Demo accounts are for local evaluation only. Change or remove them before deployment.

## Configuration

Configuration is provided through environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `OSCELOOP_HOST` / `OSCELOOP_PORT` | `127.0.0.1` / `8000` | Listen address and port |
| `OSCELOOP_DB` | `data/osceloop.db` | SQLite database path |
| `OSCELOOP_RELEASE_STAGE` | `G0_offline` | Current gate stage |
| `OSCELOOP_AI_PROVIDER` | `mock` | Scoring provider: `mock` / `http` |
| `OSCELOOP_AI_BASE_URL` | empty | OpenAI-compatible endpoint |
| `OSCELOOP_AI_API_KEY` | empty | Model API key |
| `OSCELOOP_ASR_PROVIDER` | `off` | ASR provider: `off` / `http` |
| `OSCELOOP_ASR_BASE_URL` | empty | OpenAI-compatible `/audio/transcriptions` endpoint |
| `OSCELOOP_AI_ALLOW_EXTERNAL` | `0` | Allow the scoring endpoint to leave the intranet |
| `OSCELOOP_ASR_ALLOW_EXTERNAL` | `0` | Allow the ASR endpoint to leave the intranet |
| `OSCELOOP_TRUSTED_AI_HOSTS` | empty | Additional trusted hosts, comma-separated |
| `OSCELOOP_COOKIE_SECURE` | `0` | Set to `1` for HTTPS deployments |

For L2–L3 scenarios involving real candidate audio/video, deploy models and ASR inside the hospital/campus network and complete a data-flow review.

## Project Structure

```text
app/
  api.py            FastAPI routes and auth middleware
  db.py             SQLite schema and audit chain
  config.py         Environment variables and thresholds
  domain/           Scoring, gates, SP, scheduling, governance, audit
  ai/               Capability gateway, scoring/ASR providers, call logs
web/                Vanilla HTML/CSS/JS single-page workbench
design/             Full workflow design document
tests/              pytest suite
run.py              Local entry point
```

## Tests

```bash
python -m pytest tests -q
```

The full suite currently reports **418 passed**, covering scoring routing, evidence integrity, SP red-team and certification, gates, audit chain, fairness, cost, S9 metrics, and governance loops.

## Packaging and Release

Release packages are source archives that exclude `data/` and local caches:

```powershell
Compress-Archive -Path app,design,tests,web,run.py,requirements.txt,LICENSE,README.md,README.en.md,.gitignore -DestinationPath dist/osce-loop-v0.1.0.zip -Force
```

## Security Notes

- All governance writes produce audit events; sensitive actions require role permissions.
- External AI/ASR endpoints are denied by default and must be explicitly allowed and covered by a data-flow review.
- High-risk content-audit findings feed P0 handling, red-team regression cases, and dynamic forbidden terms.
- Please do not open a public issue for security problems; contact the maintainer first.

## License

[Apache License 2.0](LICENSE)
