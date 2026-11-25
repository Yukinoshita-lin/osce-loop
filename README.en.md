# OSCE-Loop Examination System

OSCE-Loop is a full-process management system for Objective Structured Clinical Examinations (OSCE), supporting core functions such as exam scheduling, real-time execution, AI scoring, audit arbitration, and data visualization.

## Functional Features

### Exam Management
- **Exam Scheduling**: Intelligently generates candidate rotation schedules, supports special needs approval, equipment conflict detection
- **Real-time Execution**: On-site exam management, real-time monitoring of station status, supports exception marking and hint control
- **Data Collection**: Supports audio/video recording transcription, manual entry import, Automatic Speech Recognition (ASR)

### AI Scoring
- **Automatic Scoring**: LLM-based scoring engine, supports multi-dimensional evidence extraction and confidence assessment
- **Batch Re-scoring**: Supports batch re-scoring of historical exams
- **Scoring Cache**: Idempotent design to avoid repeated calls and save costs

### Audit & Quality
- **Double-blind Review**: Supports double-blind review mechanism, arbitrating disputes
- **Critical Miss Tracking**: Automatically identifies key items missed by AI
- **Indicator Monitoring**: Multi-dimensional quality indicators (G1-G3), automatic anomaly alerts

### Transparency & Compliance
- **Audit Trail**: Blockchain-style audit trail, supports tamper detection
- **AI Disclosure**: Disclose AI participation level and human confirmation status to candidates
- **Data Retention**: Automatically manage data lifecycle based on risk levels

## Technical Architecture

- **Backend**: Python 3.11+ / FastAPI
- **Database**: SQLite (including audit trail)
- **AI**: Supports multiple scoring providers (Http / Mock)
- **Frontend**: Native JavaScript + CSS

## Quick Start

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Initialize Database

```bash
python -c "from app.db import connect, init_db; conn = connect(); init_db(conn)"
```

### Start Service

```bash
python run.py
```

The service defaults to starting at `http://localhost:8000`.

### Initialize Demo Data

After the system starts, it will automatically check and initialize demo data (users, stations, grading criteria, etc.).

## Project Structure

```
.
├── app/
│   ├── api.py          # FastAPI route definitions
│   ├── config.py       # Configuration management
│   ├── db.py           # Database operations
│   ├── seed.py         # Demo data generation
│   ├── ai/             # AI scoring related
│   │   ├── asr.py      # Speech recognition
│   │   ├── base.py     # Scoring base class
│   │   ├── factory.py  # Provider factory
│   │   ├── http_provider.py  # HTTP scoring client
│   │   ├── mock.py     # Mock scoring implementation
│   │   └── pipeline.py # Scoring pipeline
│   └── domain/         # Domain logic
│       ├── scheduling.py    # Scheduling algorithm
│       ├── execution.py     # Exam execution
│       ├── scoring.py       # Scoring logic
│       ├── review.py        # Review process
│       └── ...
├── web/
│   ├── app.js     # Frontend interaction logic
│   ├── index.html # Page template
│   └── styles.css # Styles
├── tests/         # Test cases
└── run.py         # Entry script
```

## Configuration Instructions

Configured mainly via environment variables:

| Variable | Description | Default Value |
|------|------|--------|
| `DATABASE_URL` | Database Path | `data/osce.db` |
| `SCORING_PROVIDER_URL` | Scoring Service Address | - |
| `SCORING_API_KEY` | Scoring Service Key | - |
| `ASR_PROVIDER_URL` | ASR Service Address | - |
| `MEDIA_DIR` | Media File Directory | `data/media` |

When AI services are not configured, the system uses the built-in Mock Provider, facilitating development and testing.

## Core API

- `POST /api/auth/login` - User Login
- `GET /api/bundles` - Get Exam Station List
- `POST /api/bundles/draft` - Create Exam Plan
- `POST /api/attempts/ingest` - Ingest Exam Results
- `GET /api/review/queue` - Get Pending Review Queue
- `POST /api/execution/start` - Start Exam Session
- `GET /api/quality` - Quality Report
- `GET /api/governance/status` - Governance Status

## License

MIT License