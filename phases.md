# Voxora — Implementation Phases and Learning Roadmap

**Scope:** Single fictional business (BrightSmile Dental Clinic), browser voice first, no RAG.  
**Working method:** Complete one phase at a time. Do not build ahead.

## How to use this roadmap

For every phase, the coding assistant must follow this workflow (in coordination with the persistent behavioral and commenting rules in `memory.md`):
1. Read `memory.md`, `PRD.md`, `Architecture.md`, and this roadmap, plus relevant existing code.
2. Understand the current phase's requirements, scope, assumptions, and files likely to change.
3. Implement the **entire** phase (not a partial skeleton), adding meaningful beginner-friendly comments and docstrings as defined in `memory.md`.
4. Add/update tests and run all relevant checks to verify the implementation.
5. Inspect all files created or modified during the phase.
6. Create `docs/phase-N-notes.md` following the mandatory 14-section learning-notes template and documentation quality rules at the bottom of this file.
7. Summarize what was implemented, report actual verification results, and tell the user where to begin studying in the notes and source code.
8. Stop and wait at the phase's learning gate before beginning the next phase.

Do not interrupt implementation to teach concepts mid-build; implement and test the full phase first, then deliver complete post-implementation learning notes. Do not claim a phase is complete merely because code was generated. Report actual commands and results. If a provider API or feature is uncertain, verify current official documentation and run a small spike before depending on it.

## Global constraints

- One fictional business: BrightSmile Dental Clinic.
- One calendar; one owner account.
- No multi-tenancy or tenant-related schema/abstractions.
- No RAG, LangChain, LangGraph, embeddings, vector DB, pgvector, document ingestion.
- No Redis, Celery, Kafka, or background queue in MVP.
- Neon PostgreSQL from the start; do not install/run PostgreSQL locally.
- Gemini API is the likely LLM provider; free-tier limits are not a production guarantee.
- Browser voice first; phone/SIP integration is post-MVP.
- English-only MVP.
- User manually reviews and commits changes.
- Never stage, commit, push, tag, amend, reset, rebase, clean, checkout, or mutate Git history/index/working tree. Read-only Git commands only.
- Keep dependencies minimal. Do not add a framework for resume keywords.
- No real patient data. Use fictional seed data.

## Phase overview

| Phase | Name | Estimate | Demoable milestone |
|---|---|---:|---|
| 0 | Project foundation and Neon connection | 1–2 days | Frontend/backend scaffold, CI, Neon health check |
| 1 | Voice transport spike | 2–3 days | Browser audio reaches a LiveKit agent and returns speech |
| 2 | Conversation behaviour and latency | 2–3 days | English conversation, interruption/silence handling, latency report |
| 3 | Clinic data model and configuration | 2–3 days | Owner can seed/read clinic profile, services, hours, FAQs (authenticated editing deferred to Phase 7) |
| 4 | Booking engine without voice | 3–4 days | Tested availability, proposals, confirmation, reschedule/cancel services |
| 5 | Voice tools and safe booking workflows | 3–4 days | Voice agent books/reschedules/cancels through validated tools |
| 6 | Evaluation harness and call tracing | 3–4 days | 15+ scripted scenarios and call-detail latency waterfall |
| 7 | Owner authentication and dashboard | 3–4 days | Secure owner login and operational dashboard |
| 8 | Security, privacy, and reliability hardening | 2–3 days | Verification, retention, concurrency, failure and abuse tests |
| 9 | Public demo and deployment | 2–3 days | Bounded public demo with kill switch and usage limits |
| 10 | Final polish and portfolio readiness | 1–2 days | Documentation, measured results, polished demo |
| Post-MVP | Telephony / SIP | separate | Phone calling after browser MVP is stable |

Estimates are planning estimates, not commitments. Adjust based on actual learning and integration friction.

---

## Phase 0 — Project foundation and Neon connection

### Goal
Create a clean, runnable monorepo foundation and connect to hosted Neon PostgreSQL without running a local PostgreSQL server.

### In scope
- Inspect repository and preserve useful existing work.
- Set up Python backend with FastAPI and dependency management (`uv`).
- Set up Next.js + TypeScript frontend.
- Add environment configuration and `.env.example`.
- Connect to Neon using SQLAlchemy async.
- Add a simple health endpoint that verifies application and database connectivity.
- Add Alembic migration foundation.
- Add formatting/lint/type/test commands.
- Add CI for backend checks and frontend lint/typecheck/build.
- Add basic README setup instructions.

### Out of scope
Voice integration, business schema, auth, booking, Redis, Dockerized PostgreSQL.

### Acceptance checks
- Frontend starts.
- FastAPI starts.
- API health endpoint works.
- Neon connection works using environment variables.
- A harmless initial migration can be applied.
- `.env` is ignored; no secret is committed.
- CI runs backend and frontend checks.

### Learning gate
Explain virtual environments/dependency management, FastAPI app lifecycle, Neon connection strings/pooling, SQLAlchemy async sessions, Alembic migrations, and CI basics.

### Notes file
Create `docs/phase-0-notes.md`.

---

## Phase 1 — Voice transport spike

### Goal
Prove the riskiest integration early: browser microphone → LiveKit → Python agent → speech output.

### In scope
- Create LiveKit Cloud project and secure credentials.
- Create a minimal FastAPI endpoint to issue short-lived room tokens.
- Build a minimal browser page to join a room and publish microphone audio.
- Build a separate Python LiveKit Agent worker process.
- Integrate Deepgram STT (`deepgram.STTv2`, `flux-general-en`, `turn_detection="stt"` with Silero VAD retained for interruption detection), Gemini LLM (`gemini-3.1-flash-lite`), and ElevenLabs TTS (`eleven_flash_v2_5`) using currently supported LiveKit plugins/APIs.
- Keep the agent prompt hardcoded and minimal for this spike.
- Measure connection time, STT/LLM/TTS timing, and first-audio latency where available.
- Record provider versions, model IDs, quota/plan limitations, and test conditions.

### Out of scope
Booking tools, business database, dashboard, auth, RAG, telephony.

### Acceptance checks
- Browser joins a room.
- Agent receives caller audio.
- Caller speech is transcribed.
- Gemini produces a response.
- ElevenLabs speech is played back.
- Errors/timeouts are surfaced safely.
- A short latency report is recorded.

### Decision checkpoint
If the selected provider combination is incompatible, too slow, unavailable on the intended plan, or operationally unsuitable, compare the smallest viable alternatives and document the choice before proceeding. Do not silently change architecture.

### Learning gate
Explain WebRTC at a high level, LiveKit rooms/tokens/agents, STT vs LLM vs TTS, worker process lifecycle, and the measured latency path.

### Notes file
Create `docs/phase-1-notes.md`.

---

## Phase 2 — Conversation behaviour and latency

### Goal
Turn the transport spike into a usable receptionist conversation loop.

### In scope
- Define agent instructions and turn-taking.
- AI disclosure at session start.
- English-only concise spoken responses.
- Barge-in/interruption handling if supported by selected stack.
- Silence and idle timeout behaviour.
- Tool-latency filler behaviour (neutral acknowledgment, never false success).
- Read-back patterns for names, phone numbers, dates, times.
- Clarification when transcription is uncertain.
- Provider timeouts and bounded retries.
- Current date/time and clinic timezone injection (initially `Asia/Kolkata`).
- Structured voice event logging with redaction.
- Test with a small set of manual conversation scripts.

### Out of scope
Actual booking tools and owner dashboard.

### Acceptance checks
- Agent handles a short multi-turn conversation.
- Interruption and silence behaviour is documented/tested to the extent supported.
- It does not claim tools/actions succeeded when no tool exists.
- Names/contact/date/time are read back before action.
- Voice timings are captured.

### Learning gate
Explain conversation state, turn detection, barge-in, prompt boundaries, transcription uncertainty, and why prompt rules are not security controls.

### Notes file
Create `docs/phase-2-notes.md`.

---

## Phase 3 — Clinic data model and configuration

### Goal
Create the structured source of truth for BrightSmile Dental Clinic.

### In scope
- Database migrations and models for:
  - single clinic profile,
  - weekly business hours and breaks,
  - closures/holidays,
  - services,
  - FAQs,
  - one owner account placeholder/seed strategy (authentication implementation is Phase 7).
- Seed fictional BrightSmile data.
- Store timezone as `Asia/Kolkata`.
- Add service fields: duration, price, buffer, minimum lead time, cancellation window.
- Build shared service/repository functions to read clinic context.
- Build read-only API endpoints to inspect configuration (authenticated edit/update endpoints are deferred to Phase 7).
- Assemble bounded prompt context from structured records.
- Do not hardcode business details into source code except seed data/default fixtures.

### Out of scope
RAG, document upload, embeddings, multi-business abstractions, full owner dashboard.

### Acceptance checks
- Seed command creates fictional clinic data.
- API returns clinic profile, hours, services, policies, FAQs.
- Context builder produces a bounded, typed context.
- Missing facts remain missing; agent must not invent them.

### Learning gate
Explain relational modeling, migrations, seed data, structured context injection, and why live availability should not be embedded in the prompt.

### Notes file
Create `docs/phase-3-notes.md`.

---

## Phase 4 — Booking engine without voice

### Goal
Implement and thoroughly test booking correctness independently of the LLM.

### In scope
- Availability calculation considering hours, breaks, closures, service duration, buffer, lead time, existing appointments.
- Appointment and booking proposal models.
- Server-side proposal expiry and state transitions.
- `propose_booking` service.
- `confirm_booking` service with transactional availability recheck.
- Idempotent confirmation with server-generated idempotency identity.
- Reschedule proposal/confirmation.
- Cancellation with explicit confirmation state.
- Caller verification service and attempt limit.
- Concurrency strategy (transaction locks and/or verified PostgreSQL exclusion constraint).
- Typed error outcomes: slot unavailable, proposal expired, verification failed, policy blocked, conflict.
- Unit/integration tests against isolated Neon test target.

### Critical rules
- Proposal is not a slot hold.
- The model cannot supply the authoritative idempotency key.
- Confirmation requires an explicit caller confirmation in a new caller turn after the proposal.
- Confirmation must be tied to the session and exact server-stored details.
- Recheck availability transactionally at confirmation.
- Repeated confirmation cannot create duplicate appointments.
- No appointment details before caller verification.

### Out of scope
Voice tool wiring, billing, payments, clinical records.

### Acceptance checks
- Booking happy path passes.
- Expired and mismatched proposals fail.
- Duplicate confirmation creates one appointment.
- Two concurrent requests cannot create overlapping confirmed appointments.
- Slot-taken-after-proposal returns alternatives path.
- Reschedule/cancel authorization and confirmation tests pass.

### Learning gate
Explain transactions, row/advisory locks or exclusion constraints, race conditions, idempotency, state machines, and verification boundaries.

### Notes file
Create `docs/phase-4-notes.md`.

---

## Phase 5 — Voice tools and safe booking workflows

### Goal
Connect the proven booking services to the LiveKit agent.

### In scope
- Typed tools for business info, services, availability, booking proposal/confirmation, appointment lookup, reschedule, cancellation, and handoff.
- Voice worker imports shared services directly; do not call FastAPI over internal HTTP for each tool.
- Tool schemas validate all model-supplied arguments.
- Confirmation turn is tracked by the application and cannot be fabricated by the model.
- Read back exact booking details before confirmation.
- Handle slot becoming unavailable between proposal and confirmation.
- Caller verification and attempt-limit handoff.
- Ensure tools return only authorized caller data.
- Test tool errors and provider timeouts.

### Out of scope
Call trace UI (Phase 6), public demo, phone calling.

### Acceptance checks
- Caller can complete a booking via voice.
- Agent reports success only after tool success.
- Agent offers alternatives on slot conflict.
- Caller can reschedule/cancel after verification.
- Caller can request a human.
- Prompt injection cannot bypass tool authorization.

### Learning gate
Explain tool calling, typed tool arguments, service-layer reuse, untrusted model inputs, and server-enforced confirmation.

### Notes file
Create `docs/phase-5-notes.md`.

---

## Phase 6 — Evaluation harness and call tracing

### Goal
Make behaviour measurable and debuggable.

### In scope
- Create at least 15 scripted scenarios covering FAQs, booking, conflicts, verification, cancellation, handoff, prompt injection, silence, and provider failures.
- Make the conversation/tool loop testable without depending on live provider quotas. Verify whether a supported LiveKit text-only test mode exists; otherwise expose a deterministic text-callable agent/tool loop.
- Add fake/mock provider adapters for CI.
- Persist call metadata and ordered trace events.
- Capture STT, LLM, tool, TTS, and end-to-end timing where available.
- Build call-detail page with:
  - call outcome,
  - turn timeline,
  - tool-call timeline,
  - latency waterfall,
  - error details,
  - booking result.
- Redact personal information; do not store hidden reasoning or raw audio by default.
- Run evals after each subsequent phase.

### Acceptance checks
- 15+ scenarios run repeatably.
- CI does not depend on live Gemini/Deepgram/ElevenLabs quotas.
- Call trace can explain a failed booking.
- Latency waterfall shows actual timings, not placeholders.
- Sensitive fields are redacted.

### Learning gate
Explain evaluation design, deterministic tests vs LLM evaluations, trace event schema, latency percentiles, and why hidden reasoning is not logged.

### Notes file
Create `docs/phase-6-notes.md`.

---

## Phase 7 — Owner authentication and dashboard

### Goal
Give the fictional clinic owner secure control over the receptionist.

### In scope
- One owner account; no staff roles or membership system.
- Password hashing.
- Short-lived access JWT and refresh session/token.
- HttpOnly Secure cookies in production, refresh rotation, server-side revocation.
- CSRF protections and origin validation.
- Same-origin Next.js rewrites/proxy if practical.
- Dashboard pages:
  - overview,
  - appointments,
  - business profile,
  - hours/closures,
  - services,
  - FAQs,
  - calls and call details,
  - receptionist enable/disable.
- Real API data and loading/empty/error states.
- Owner-only authorization for all protected endpoints.

### Acceptance checks
- Owner can log in and log out.
- Refresh rotation and revocation work.
- Protected APIs reject unauthenticated requests.
- Owner can edit business context and see changes reflected in new voice sessions.
- No secrets are sent to browser.

### Learning gate
Explain password hashing vs encryption, JWT/session tradeoffs, refresh rotation, HttpOnly cookies, CSRF, and same-origin proxying.

### Notes file
Create `docs/phase-7-notes.md`.

---

## Phase 8 — Security, privacy, and reliability hardening

### Goal
Close safety, privacy, concurrency, and failure-mode gaps before public exposure.

### In scope
- Threat model review: caller prompt injection, misrecognition, enumeration, replay, tool misuse.
- Verify caller before appointment disclosure.
- Enforce attempt limits and handoff.
- Confirm retention/deletion rules for phone/email, appointments, transcripts, and traces.
- Ensure raw audio is not retained by default.
- Redact phone/email in logs and traces.
- Verify every public and owner endpoint has appropriate rate limits/auth.
- Test provider failures and database disconnects.
- Verify Neon cold-start/pooling behaviour and session startup connection handling.
- Check database constraints and concurrency behaviour.
- Review dependency versions and secrets handling.
- Security tests for cross-caller appointment access and public demo boundaries.

### Acceptance checks
- No cross-caller appointment disclosure in tests.
- Retention policy is documented and implemented.
- Failure modes are bounded and visible.
- No sensitive values in logs.
- Booking invariants still pass under concurrency.

### Learning gate
Explain threat modeling, least privilege, privacy-by-design, data minimization, rate limiting, and failure containment.

### Notes file
Create `docs/phase-8-notes.md`.

---

## Phase 9 — Public demo and deployment

### Goal
Deploy a safe public demonstration using synthetic data.

### In scope
- Deploy Next.js, FastAPI, and LiveKit agent worker.
- Configure Neon production database and migrations.
- Configure secrets in deployment environment.
- Add clear AI disclosure.
- Add server-side kill switch.
- Ensure public demo can only create demo appointments.
- Ensure owner dashboard remains private.
- Add health/readiness checks and basic operational logs.
- Add deployment instructions and rollback notes.

### Acceptance checks
- Public demo works from a fresh browser.
- Usage limits cannot be bypassed by editing client state.
- Kill switch stops new sessions.
- Demo data is clearly synthetic.
- Owner APIs remain protected.
- Costs and quotas are monitored.

### Learning gate
Explain deployment topology, secret management, usage metering, abuse controls, and operational rollback.

### Notes file
Create `docs/phase-9-notes.md`.

---

## Phase 10 — Final polish and portfolio readiness

### Goal
Make Voxora understandable, demonstrable, and technically credible.

### In scope
- Polish responsive UI and voice states.
- Improve empty/error/loading states.
- Add architecture diagram and concise product walkthrough.
- Document setup, env vars, migrations, seed data, tests, deployment, limitations.
- Record measured metrics: sample size, p50/p95 latency, booking test outcomes, evaluation pass rate, test environment.
- Add screenshots or a short demo video if useful.
- Remove dead code and unused dependencies.
- Verify a clean setup from README.
- Update memory status and phase notes.

### Acceptance checks
- Another developer can understand and run the project from documentation.
- Metrics are real and reproducible.
- No unsupported reliability/latency claims.
- Git remains untouched by the coding agent.

### Learning gate
Explain the complete system end-to-end, key tradeoffs, failure modes, and what would be required to support a real clinic safely.

### Notes file
Create `docs/phase-10-notes.md`.

---

## Post-MVP — Phone/SIP integration (optional)

Do not begin until browser voice, booking correctness, traceability, and deployment are stable.

Investigate providers that can connect phone calls to LiveKit through supported SIP integration. Verify current India availability, KYC/regulatory requirements, pricing, number provisioning, and supported codecs with official provider documentation. Telephony is not required for MVP acceptance.

## Required phase-notes template

Every `docs/phase-N-notes.md` file must be a complete, beginner-friendly learning resource grounded in the **actual code implemented and tests executed** (never generic filler or assumed behavior). Assume zero prior knowledge of Python internals, Agentic AI, or Voxora's libraries, and use MERN analogies (MongoDB, Express, React, Node.js, JavaScript) wherever they make concepts clearer.

Each phase notes file **must include all 14 sections below** in order:

```markdown
# Phase N — [Name]

### 1. What We Built
- What functionality was implemented.
- What problem it solves.
- What is included and explicitly excluded from this phase (identifying any planned functionality not implemented).

### 2. Technologies and Dependencies Explained
For every new or significantly used technology/library:
- What it is.
- Why it exists.
- Why we chose it for Voxora.
- How it works conceptually.
- Comparison with MERN equivalents where useful.
- Where it appears in our implementation.
- Important configuration and dependencies.
- What alternatives exist and why they were not chosen.

### 3. Foundational Concepts
Explain all important concepts required to understand this phase (including Python-specific syntax/mechanisms such as `async`/`await`, type hints, decorators, context managers, dependency injection, generators/`yield`, Pydantic models, and Agentic AI concepts as they appear). Start from the basics and explain concepts before relying on them.

### 4. Architecture and Complete Execution Flow
Explain the complete execution pipeline end-to-end:
- What triggers the functionality.
- Which file is the entry point.
- Which functions/classes execute and how control moves between files.
- What data is passed between components and what each component does.
- What gets returned, what happens next, and where execution terminates.
Include sequence diagrams or flow diagrams wherever they genuinely improve understanding.

### 5. Complete Codebase Walkthrough
Cover every file created or significantly modified during the phase (without silently skipping important files). For each file, explain:
- Its responsibility and why it exists.
- Its important imports.
- Its classes and functions.
- Important variables and configuration.
- How its components work.
- Which other files call or depend on it, and how it fits into the overall architecture.

### 6. Important Code Explained in Detail
Include relevant code snippets from the actual implementation and explain important code blocks and unfamiliar syntax/patterns:
- What the syntax means and why it is needed.
- What happens when it executes.
- How it interacts with other parts of the system.
- What would happen if it were implemented differently.

### 7. Data Flow and State Management
Explain:
- Where data originates, how it is validated, and how it is transformed.
- Where it is stored, how it is retrieved, and how it moves between components.
- How failures are handled.
Clearly distinguish between in-memory state, database state, external service state, and client-side state wherever relevant.

### 8. Design Decisions and Alternatives
For each significant architectural or implementation decision:
- What we chose and why.
- What alternatives were considered and why they were not selected.
- Any trade-offs or limitations.

### 9. Error Handling and Failure Scenarios
Explain:
- Possible failure points.
- How errors propagate, and how they are caught and handled.
- What the user or system experiences.
- Important edge cases.

### 10. Testing and Verification
Document:
- What was tested and why each test matters.
- Exact commands used to run tests and checks.
- Expected results vs. actual observed results.
- What the tests prove, and what remains unverified.
Never claim a test passed unless it was actually executed and observed passing.

### 11. Complete Implementation Recap
Provide a cohesive end-to-end narrative explaining how all the pieces fit together so the reader can explain the full phase after studying this section and the source code.

### 12. Important Things to Remember
List the key concepts, architectural decisions, dependencies, constraints, and implementation details to retain going forward.

### 13. Self-Assessment Questions
Write thoughtful questions testing:
- Foundational concepts.
- Technologies and libraries.
- Code comprehension.
- Execution flow.
- Data flow.
- Debugging.
- Design decisions.
Do not provide answers immediately.

### 14. My Answers
<!-- Leave empty for the user to fill in -->
```

### Documentation quality requirements

- Ground every explanation in the actual files, symbols, behavior, and test runs from that phase — do not invent details or test outputs.
- Explain concepts before relying on them, use practical examples and MERN comparisons, and include diagrams, tables, and real code snippets where helpful.
- Show how files connect to one another rather than documenting files in isolation.
- Do not omit technologies or Python patterns just because they seem obvious to an experienced Python developer.
- Avoid unnecessary repetition, but never sacrifice clarity for brevity.
- Never fill in `### 14. My Answers`; the user completes it to pass/defer the learning gate. After presenting the notes and telling the user where to start studying, stop and wait for the learning gate.
