# Voxora — AI-Powered Voice Receptionist

Voxora is a real-time, browser-based AI voice receptionist built for a single dental clinic: **BrightSmile Dental Clinic** (fictional demonstration).

A caller speaks naturally through the browser. Voxora answers questions about clinic hours, services, and policies, checks live appointment availability, safely proposes and confirms bookings, handles rescheduling and cancellations, and transfers callers to human staff when necessary.

---

## Architecture & Monorepo Structure

```text
voxora/
├── backend/                  # FastAPI REST API + Shared Python Services Layer
│   ├── app/
│   │   ├── api/              # HTTP Route handlers (/health, etc.)
│   │   ├── core/             # Settings (pydantic-settings), logging
│   │   ├── db/               # SQLAlchemy async engine, session, Alembic migrations
│   │   └── services/         # Business logic & tool services (Phase 3+)
│   ├── tests/
│   │   ├── unit/             # Unit tests (run offline without database)
│   │   └── integration/      # Integration tests (gated on live Neon database)
│   ├── pyproject.toml        # uv project configuration
│   ├── uv.lock               # Deterministic Python dependency lockfile
│   └── worker.py             # LiveKit Agent Worker process (Phase 1+)
├── web/                      # Next.js + TypeScript + Tailwind CSS Frontend
│   ├── app/                  # App Router pages and layouts
│   ├── components/           # UI components
│   ├── package.json          # Node dependencies
│   └── pnpm-lock.yaml        # Deterministic pnpm lockfile
├── docs/                     # Phase documentation and learning notes
├── .github/workflows/        # Independent CI workflows for backend and frontend
├── Architecture.md           # Technical architecture specifications
├── PRD.md                    # Product requirements document
├── phases.md                 # Implementation roadmap
└── memory.md                 # Agent memory and decision log
```

> **Independent Dependency Management:**
> - Python dependencies are managed strictly inside `backend/` using `uv`.
> - Node dependencies are managed strictly inside `web/` using `pnpm`.
> - No root-level Python or Node dependencies are used.

---

## Prerequisites

- **Python 3.12+**
- **uv** (`>= 0.5.0` recommended)
- **Node.js 20+**
- **pnpm** (`>= 9.0.0` or `12.x`)
- **Neon PostgreSQL** account & connection string (uses `postgresql+asyncpg://...`)

---

## Getting Started

### 1. Environment Configuration

Copy the example environment file inside `backend/`:

```bash
cp backend/.env.example backend/.env
```

Edit `backend/.env` and supply your Neon connection string:

```ini
# Asyncpg format for SQLAlchemy async engine
DATABASE_URL=postgresql+asyncpg://<username>:<password>@<ep-xyz>.neon.tech/neondb?ssl=require

# Optional: isolated test database for integration testing
TEST_DATABASE_URL=postgresql+asyncpg://<username>:<password>@<ep-xyz>.neon.tech/neondb_test?ssl=require

# Set to "true" only when debugging SQL queries locally
DB_ECHO=false
```

---

### 2. Backend Setup & Run

From the root directory:

```bash
cd backend

# Install dependencies into isolated virtual environment
uv sync

# Run database migrations (asyncpg via Alembic)
uv run alembic upgrade head

# Start the FastAPI development server
uv run uvicorn app.main:app --reload --port 8000
```

Verify application and database connectivity:
```bash
curl http://localhost:8000/health
# Returns: {"status":"ok","db":"ok","environment":"development"}
```

---

### 3. Frontend Setup & Run

In a separate terminal:

```bash
cd web

# Install frontend dependencies
pnpm install

# Start the Next.js development server
pnpm dev
```

Visit [http://localhost:3000](http://localhost:3000) in your browser.

---

## Testing & Quality Checks

### Backend Checks

```bash
cd backend

# 1. Linting with Ruff
uv run ruff check .

# 2. Strict Type Checking with Mypy
uv run mypy app/

# 3. Unit Tests (Always pass offline, no database required)
uv run pytest tests/unit/

# 4. Integration Tests (Requires live Neon database in TEST_DATABASE_URL)
uv run pytest -m integration tests/integration/
```

### Frontend Checks

```bash
cd web

# 1. ESLint
pnpm lint

# 2. TypeScript validation
pnpm type-check

# 3. Production Build
pnpm build
```

---

## Project Status

See [phases.md](phases.md) for the active phase and learning roadmap.
Phase notes and conceptual explanations are stored in [docs/](docs/).

