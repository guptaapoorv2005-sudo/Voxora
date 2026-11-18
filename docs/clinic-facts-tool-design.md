# Clinic facts through tools

## Why this changed

The previous worker built up to 4,000 characters of clinic facts and then appended behavioral instructions, even though the *combined* prompt also had a 4,000-character limit. A valid clinic configuration could therefore crash a call at startup. The worker now supplies only current date/time and concise behavior, safety, handoff and tool-use instructions to the model. Clinic facts are no longer inserted into the system prompt. This keeps prompt length independent of the number and size of clinic records.

## Execution flow

1. `backend/worker.py` loads clinic context for session setup, takes its configured timezone, calls `build_session_context`, and passes only that text to `build_system_instructions`. The worker still loads the broader snapshot for other session behavior; it is not sent as a catalogue in the prompt.
2. The model calls `get_clinic_info` for a fact. `on_date` is an optional ISO date for dated opening hours or closures. The wrapper opens a fresh database session and calls `lookup_clinic_info` in `app/services/clinic_lookup.py`.
3. Address, contact, name, policies, hours and FAQ matching use the current database records. Hours include a closure check for the requested date, and the regular weekly schedule is labeled separately. A general upcoming-holidays question reports only the next 30 days. FAQ matching uses exact topic/question or matching topic words, not semantic search. Missing information is reported as unknown; multiple matches require clarification.
4. `list_services` obtains active services and returns names, IDs, durations and configured prices. It accepts an optional service name to return a matching description. Ambiguous names require clarification. Results are limited to 30 services and a character budget; the response explicitly says when additional services are omitted. A fact too long to return safely is not silently truncated.
5. `check_availability` reads current profile, active services, hours, closures and appointments for the requested date range. It remains an availability search, not a reservation. Final booking confirmation still checks state and availability in the existing transaction and locking protocol.
6. The three lookup wrappers use a five-second timeout. Database failures and timeouts produce a neutral unavailable message and an allowlisted failure trace, not internal SQL details. Cancellation is not swallowed. Calls to tools can add database and model-turn latency; no latency improvement is claimed without a live measurement.

## Files and design decisions

- `app/voice/prompts.py` contains the shorter behavior prompt and preserves the length guard for that prompt. Tool output is treated as data, not instructions. Prompt rules are not authorization controls.
- `app/voice/clinic_tools.py` exposes typed LiveKit functions and provides timeout/error boundaries. Each call obtains its own async database session; concurrent calls do not share a session.
- `app/services/clinic_lookup.py` contains the read-only lookup, deterministic matching, formatting, and response-size limits. It uses the existing repository functions and does not introduce a database, RAG, or embeddings.
- `app/api/routes_owner.py` commits validated owner edits without rebuilding an unrelated full prompt. The individual input size limits remain in place.
- The legacy `build_clinic_prompt_context` remains in `clinic_context.py` for existing callers and historical tests, but the worker and owner edit path no longer call it.
- `tests/unit/test_clinic_lookup.py`, `test_prompts.py`, `test_worker.py`, and the fake-provider eval fixtures test the new flow. The existing scripted evaluations verify tool dispatch, not real LLM decision quality.

The worker's hardcoded demo greeting remains a separate pre-existing limitation. A very long individual policy, FAQ answer or service description returns a safe contact-reception response instead of partially presenting potentially misleading content. Service matching uses an exact name or an unambiguous substring; FAQ matching may miss a paraphrase. The five-second limit is an application timeout, not a measured performance target.

## Verification performed

- `python -m pytest tests/unit/test_clinic_lookup.py tests/unit/test_prompts.py tests/unit/test_phase7_owner.py tests/unit/test_phase6_evals.py tests/unit/test_livekit_text_evals.py tests/unit/test_worker.py -q -p no:cacheprovider --tb=short`: **76 passed**.
- `python -m mypy app/`: passed for 61 source files. Ruff passed for the files changed by this task.
- Read-only checks against the configured development database loaded the real clinic context and built a **1,725-character** new system prompt. The legacy clinic-facts text alone was **2,200 characters** and is not part of that prompt. Address, dated-hours, cancellation-policy and active-services lookup functions each returned a nonempty response. The checks did not print clinic data or credentials.
- A full `pytest tests/unit` run produced **365 passed, 6 failed**. The failures are in separate existing Phase 8/booking route tests: the remote development token test expected 403 but received 503, and five booking route tests expected successful/typed outcomes but received 403. Two representative failures were rerun individually and reproduced. These paths are outside this prompt/tool change; in particular, the current development token route does not check the remote peer. Do not expose that endpoint publicly.
- Full `ruff check .` reported an existing import-order issue in `app/api/routes_appointments.py`; focused lint on this task's files passed.

A real browser microphone, LiveKit room and model-backed conversation have not been tested for this change. Restart the backend and worker to load the changes, then manually ask about a date with a closure, a service price, a policy, an unknown FAQ, and availability. Measure the extra tool-call latency separately from speaker playback. Before claiming booking concurrency or public readiness, run the outstanding isolated PostgreSQL and Phase 8 checks.
