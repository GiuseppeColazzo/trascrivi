# Trascrivi

**A local, single-user web platform for transcribing and studying university lectures.** It wraps the
existing `faster-whisper` engine of this repository in a small FastAPI server: you create one course per
university subject, add a lecture either by dragging the file into the browser or by pointing at an
absolute path on disk, and a background worker transcribes it while the page shows progress, realtime
speed and an ETA. The finished transcript lands in a searchable registry where you can read it, edit it
line by line, export it as `.txt`/`.md`/`.json`/segments, and optionally hand it to an LLM **correction
agent** that proposes anchored, individually reviewable fixes instead of rewriting your text. Courses
also carry a deterministic glossary of `find → replace` rules, so a correction accepted once is applied
silently to every later lecture of that course. Everything runs offline on your machine — no account, no
telemetry, and no network call at all unless you explicitly configure a cloud LLM provider.

The stack is deliberately minimal: **FastAPI + uvicorn**, one process and one background worker thread,
**SQLite** (stdlib `sqlite3`, WAL mode, **FTS5** for full-text search) with no ORM, and a plain
HTML/CSS/JS frontend in `web/` served at `/` with no npm and no build step. There is no Celery and no
Redis.

---

## Requirements

| | |
|---|---|
| **Python** | 3.11 or newer |
| **GPU** | NVIDIA GPU is *optional*. With CUDA the default `distil-large-v3` model runs at several times realtime; on CPU the same code works but you should pick a smaller model — see [README_CPU.md](README_CPU.md) |
| **New runtime dependencies** | `fastapi`, `uvicorn`, `python-multipart` (plus `pytest` for development), listed in `requirements.txt` |
| **Already present** | `faster-whisper` 1.2.1, `torch` 2.5.1+cu121, `rich`, `cryptography`, `openai` — the project `.venv` is created with `--system-site-packages` so these are reused from the system installation |
| **Disk** | Each Whisper model is 0.1–1.5 GB on first download from Hugging Face, plus the audio copies and transcripts under `data/` |

`ffmpeg` is **not** required: audio and video are decoded directly through PyAV, and video containers
(`.mp4`, `.mkv`, `.mov`, `.webm`) are accepted alongside `.mp3`, `.wav`, `.m4a`, `.ogg`, `.flac`, `.aac`,
`.opus` and `.wma`.

---

## Quick start

### Option A — `run.bat` (Windows, recommended)

From the repository root:

```powershell
.\run.bat
```

On the **first run** the launcher creates `.venv` with `--system-site-packages` and installs
`requirements.txt`, then writes a marker file; once that marker exists it skips straight to starting the
app. Delete `.venv\.deps-ok` if you ever want to force a reinstall of the dependencies. Any arguments you
pass are forwarded to `trascrivi_app.py`, so `.\run.bat --port 8010 --no-browser` works. Your browser
opens automatically on the local URL.

The installer prefers **`uv`** when it is available (`python -m uv`): installing into a
`--system-site-packages` virtual environment is where pip is at its worst — it resolves every candidate
against the hundreds of packages already visible from the system installation, which can mean minutes of
backtracking for three pure-Python packages. `uv` does the same job in seconds. If `uv` is missing the
launcher falls back to plain `pip`, which still works, just slowly.

> **If the install seems stuck:** that is the pip resolver, not a hang. Let it finish once, or run
> `python -m pip install uv` in the system interpreter and re-run `run.bat` to get the fast path.

### Option B — manual

```powershell
python -m venv --system-site-packages .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe trascrivi_app.py
```

`trascrivi_app.py` accepts:

| Option | Default | What it does |
|---|---|---|
| `--port` | `8000` | Preferred port. If it is busy the launcher tries the next one, up to 10 attempts |
| `--host` | `127.0.0.1` | Listening interface. Keep the default: the app has no authentication |
| `--no-browser` | off | Do not open the browser at startup |
| `--dev` | off | uvicorn auto-reload for working on the server code |

Interactive OpenAPI documentation for every endpoint is available at **`/docs`** while the server runs.

---

## The existing CLI still works exactly as before

`trascrivi_audio.py` is the original command-line transcriber. It has been kept **100% backward
compatible** and is now also the transcription engine that the web app calls, so both frontends share
one code path. Nothing about the old workflow changed.

Transcribe everything in `lezioni/` (video `.mp4` included), write the `.txt` files into `trascrizioni/`
and skip the ones already done:

```powershell
.\.venv\Scripts\python.exe trascrivi_audio.py lezioni -d trascrizioni --skip-existing
```

Add `--timestamps` to put `[mm:ss]` at the start of every line — required if you want to use `--resume`:

```powershell
.\.venv\Scripts\python.exe trascrivi_audio.py lezioni -d trascrizioni --skip-existing --timestamps
```

The first execution downloads the chosen model (about 1.5 GB for the default) from Hugging Face.

### Main CLI options

| Option | Default | What it does |
|---|---|---|
| `-m`, `--model` | `distil-large-v3` | Whisper model. `distil-*` and `*.en` are English-only |
| `-l`, `--language` | `en` | Source language; `auto` detects it per file |
| `-d`, `--output-dir` | audio folder | Directory for the generated `.txt` files |
| `-o`, `--output` | — | Explicit output file (single input file only) |
| `--device` | `auto` | `auto`, `cuda` or `cpu` |
| `--compute-type` | `auto` | `auto`, `int8`, `int8_float16`, `float16`, `float32`, `bfloat16`. `auto` means float16 on GPU, int8 on CPU |
| `--batch-size` | automatic | Segments decoded in parallel, chosen from free VRAM. `1` disables batching |
| `--beam-size` | `5` | Beam search width. `1` is faster and less accurate |
| `-w`, `--workers` | `1` on GPU, N on CPU | Parallel processes over files |
| `--cpu-threads` | automatic | Threads per worker |
| `--timestamps` | off | Prefix each segment with `[mm:ss]` in the output file |
| `--word-timestamps` | off | Word-level timestamps (slower) |
| `--no-vad` | off | Disable the VAD filter that removes silence |
| **`--initial-prompt`** | — | **New flag.** Course vocabulary and context — proper names, acronyms, technical terms — passed to the model to improve recognition |
| `--skip-existing` | off | Skip files whose `.txt` already exists |
| `--resume` | off | Resume an interrupted transcription from the last `[mm:ss]` written. Requires `--timestamps` |
| `--benchmark SECONDS` | — | Process only the first N seconds of each file (quick test) |
| `-v`, `--verbose` | off | Debug output |

Full list: `trascrivi_audio.py --help`.

---

## How it works

### The job pipeline

```
source (uploaded copy in data/sources/, or a path referenced in place)
        │
        ▼
   jobs queue ──► worker thread ──► partial file (data/partial/<job>.txt)
                        │                        │
                        │  progress / speed / ETA│  flushed every N seconds of audio
                        ▼                        ▼
                    jobs table             transcript (data/transcripts/<id>.json)
```

1. **Queue.** Creating a transcription inserts a row in `jobs` with status `queued` and wakes the single
   worker thread. A progress bar, `x realtime` speed and an ETA are written back to the job row as the
   worker advances, which is what the page polls.
2. **Partial output.** While the job runs, the raw `[mm:ss] text` output is flushed to
   `data/partial/<job_id>.txt` every `flush_every` seconds of audio (default 10 s). This is the same raw
   format the CLI produces, and it is the only format `--resume` can restart from.
3. **Cancel.** Cancelling sets a stop flag that is checked between segments, so the worker breaks
   cleanly at a segment boundary and the partial file is kept. You lose at most the segment in progress.
4. **Resume.** Restarting reads the last `[mm:ss]` in the partial file and starts again from there minus
   a **2-second margin**, so no speech is dropped at the seam. Without timestamps in the partial file the
   job restarts from zero — better to redo than to leave a hole in the middle.
5. **Crash recovery.** If the server dies mid-job, on the next startup every `running`/`queued` job is
   marked **`interrupted`** rather than left showing a frozen progress bar, and the UI offers
   *Retry* / *Resume*.
6. **Materialisation.** On success the raw text is parsed into segments, the course glossary is applied,
   and the transcript (plus its FTS5 index entry and a first revision record) is written.

### The correction agent

The design point worth understanding: **the LLM never rewrites the transcript.** Instead, the model is
asked for a list of *anchored edits*:

```json
{ "find": "...", "replace": "...", "reason": "...", "kind": "...", "confidence": 0.0 }
```

plus an optional glossary. The app then applies the edits itself as a deterministic substitution.

Why it is built that way:

- **Cost scales with the number of corrections, not the transcript length.** A two-hour lecture may need
  twenty fixes; you pay for those, not for re-emitting the lecture.
- **The substitution is deterministic and verifiable.** For each proposal the engine knows the exact
  match, the number of occurrences, and whether it overlaps or conflicts with another edit. Nothing is
  "mostly" applied.
- **Every proposal is individually acceptable or rejectable.** You see find → replace with the reason
  and confidence, preview a line-by-line diff, accept all, reject all, or apply only the ones you select.

Prompting is **chunked at ~4000 characters** (`llm_chunk_chars` in Settings) with **one segment of
overlap** between consecutive chunks so a correction spanning a boundary is still visible, and
`temperature=0` because we want substitution, not creativity.

#### What the agent is allowed to change

The system prompt is deliberately restrictive, because a permissive one produces noise rather than
corrections. The model is told its **only** job is to fix words the recogniser heard wrong when the
intended word is unmistakable from the sentence, and it is told explicitly never to propose
rephrasing, style changes, spelling variants, capitalisation, or punctuation-only edits. Its test for
every candidate is: *"would a human typing this transcript while listening have written something
different?"*

Measured on the same 30-segment chunk, the restrictive prompt returned **7 edits in 3.2 s** against
**30 edits in 8.8 s** for a permissive one, and the 30 were mostly style preferences. The 7 are the
kind of thing you actually want:

| Recogniser output | Correction | Why it is decidable |
|---|---|---|
| `four people if the Aligno have access to Oveni` | `poor people …` | "four people if" is not a phrase |
| `the lawsuit between Mecca and other people` | `… Meta …` | a lawsuit about a tech company |
| `COGO changes decline` | `CO2 changes climate` | domain vocabulary |
| `gave back` | `give-back` | noun, not a verb, in this sentence |
| `environmental research of knowledge` | `environmental erosion of knowledge` | collocation |

The prompt also **caps the output at 5 items per chunk and requires `confidence >= 0.9`** (anything
lower is dropped before review). This matters more than it looks: given room, the model fills the quota
with guesses — with a cap of 15 the same chunk produced 14 edits whose confidence was 0.4–0.7, including
invented wording like *"It hasn't been the same as what has happened"*. With the low cap it returns 6,
all defensible, at **1/3 of the output tokens**. Volume is not value here: a wrong suggestion costs the
reader more than a missed correction.

#### Proposal flags

Every proposal is validated against the actual segments and tagged. You can only apply what the engine
was able to anchor.

| Flag | Meaning |
|---|---|
| `ok` | The `find` text matches exactly one segment. Ready to apply. |
| `ambiguous` | `find` occurs more than once. The app refuses to guess and asks you to pick the segment from a dropdown. |
| `unmatched` | The `find` text was not found verbatim in the transcript. |
| `conflict` | Applying this edit would overlap an edit already claimed by another proposal. |
| `noop` | `find` and `replace` are identical (the model does this: 217 of 563 proposals on one real lecture) — discarded before it reaches the review list. |
| `too_large` | The edit rewrites more than `max_delta_chars = 160` characters. This is what stops a model from rewriting whole sentences. |
| `duplicate` | The same `find → replace` pair was already proposed. |

If a chunk is still too big to fit in one answer (the JSON gets cut off mid-object), the agent
**halves that chunk and retries**, down to a floor of two segments, instead of losing the section.

#### Cost of a lecture

Measured on a real 88-minute lecture (203 segments, 65 756 characters → 18 chunks, 28 s of wall clock):

| | Before | Now (cap 5, confidence ≥ 0.9) |
|---|---|---|
| Proposals returned | 563 | 61 |
| Input tokens | 27 558 | 29 088 |
| Output tokens | 12 018 | **4 714** |
| Off-peak cost, 10 lectures | $0.078 | **$0.034** |

At the published `deepseek-flash` rates ([api-docs.deepseek.com](https://api-docs.deepseek.com/quick_start/pricing),
USD per 1M tokens; a cache hit is 50× cheaper than a miss):

| Rate | 1 lecture | 10 lectures |
|---|---|---|
| Off-peak ($0.003 hit / $0.15 miss / $0.60 output) | **$0.003** | **$0.03** |
| Peak ($0.006 hit / $0.30 miss / $1.20 output) | $0.007 | $0.07 |

Output dominates the bill (~78% before, ~50% now), which is why the low cap pays off twice: fewer
proposals mean less to read *and* less to pay for. The input side is nearly free because the system
prompt is served from DeepSeek's context cache across all 18 chunks.

The agent is **opt-in per job** and can be **re-run on any existing transcript** from the Transcript
screen.

### Course glossary

Each course has a list of deterministic `find → replace` rules. A rule marked **`auto`** is applied
**silently and instantly at transcript creation** — zero tokens and zero latency, because it is a plain
substitution and no model is involved. That is the mechanism by which correcting something once improves
every later lecture of that course. Suggestions coming back from the agent are added as **non-auto**
terms, so nothing the model proposes is ever applied without your say-so.

---

## Web UI tour

The frontend is hash-routed, so every screen has a shareable URL. A light/dark toggle in the header is
persisted in `localStorage`.

| Screen | Route | What lives there |
|---|---|---|
| **Library** | `#/` | All courses with their transcript counts. Create, rename and delete a course. The header search box runs a full-text query across every transcript and links each hit with `#/t/<id>?q=word` so the matching segments are highlighted. |
| **Course** | `#/p/<id>` | The lectures of one course, with *New lecture* and per-transcript rename/move/delete. This is also where the **Course glossary** is edited, including the `auto` checkbox. |
| **New lecture** | `#/p/<id>/new` | The two ways to add audio (drag & drop / file picker, or an absolute local path validated live through `/api/media/probe`) plus the full transcription form: model, language, device, compute type, beam size, VAD, word timestamps, initial prompt for course vocabulary, and the optional correction agent with its provider picker. |
| **Jobs** | `#/jobs` | Queue and history with progress bar, `x realtime` speed, ETA, and Cancel / Retry / Resume. The header badge shows the running count. |
| **Transcript** | `#/t/<id>` | The registry entry: editor (one line = one segment, timestamps re-attached on save), **Segments** tab, **Info** tab with revision history, exports, and the agent's proposal review with diff preview and accept/reject. |
| **Settings** | `#/settings` | Transcription defaults, agent chunk size, flush interval, backups to keep, default provider, disk usage (audio copies / data dir / free space), database backup, *Delete all audio copies*, *Unload models from RAM*, and the LLM provider configuration. |

### Transcription options

| Field | Default | Notes |
|---|---|---|
| Model | `distil-large-v3` | Any model the CLI supports |
| Language | `en` | `en` or `auto` |
| Device | `auto` | `auto`, `cuda`, `cpu` |
| Compute type | `auto` | `auto`, `int8`, `int8_float16`, `float16`, `float32`, `bfloat16` |
| Beam size | `5` | 1–20 |
| VAD | on | Disable to keep silence in the output |
| Word timestamps | off | Slower, adds per-word timing |
| Initial prompt | — | Course vocabulary: names, acronyms, technical terms |
| Per-segment timestamps | always on | Every segment is stored with its `[mm:ss]` offset |

English-only models (anything ending in `.en`, plus every `distil-*`) combined with a non-English
language are rejected with **HTTP 422** before the job is created, so you find out immediately instead of
after ten minutes of GPU time.

#### Beam size — what it actually buys you

Beam search keeps several candidate decodings alive and keeps the most likely one, so it trades time for
accuracy. Measured on this machine (RTX 4060 Laptop 8 GB, `small`, float16, 5 minutes of a real lecture):

| Beam | Time | Speed | Words |
|---|---|---|---|
| 1 (greedy) | 22.7 s | 13.2x realtime | 792 |
| **5** (default) | 25.7 s | 11.7x realtime | 793 |
| 10 | 30.4 s | 9.9x realtime | 807 |

Comparing the text of beam 1 against beam 5 on the same clip, about 10% of the words differ, and the
differences are mostly punctuation and disfluencies (`small, because` vs `small because`) rather than
misheard terminology. On clean lecture audio:

- **5** is the balanced default and what the form pre-fills.
- **1** is worth it while iterating (a quick draft before committing to a 90-minute run) or on CPU, where
  every extra candidate costs real minutes.
- **10+** is not worth it here: ~18% more time than the default for a text that is essentially the same. On
  noisy recordings or strong accents the curve is a little more favourable to wider beams, but the ceiling
  is low.

The field applies to **that job only**; it is not persisted, so every new lecture starts again at 5.

---

## REST API

Everything is mounted under `/api` and documented interactively at `/docs`.

| Resource | Endpoints |
|---|---|
| **Health & metadata** | `GET /api/health` (hardware, queue size, providers, defaults) · `GET /api/models` · `POST /api/models/unload` · `GET /api/formats` |
| **Search** | `GET /api/search?q=...&project_id=&limit=` — FTS5 with highlighted snippets |
| **Courses** | `GET /api/projects` · `POST /api/projects` · `GET /api/projects/{id}` · `PATCH /api/projects/{id}` · `DELETE /api/projects/{id}` |
| **Sources** | `GET /api/projects/{id}/sources` · `POST /api/projects/{id}/sources` (register an absolute path) · `POST /api/projects/{id}/upload` (streaming upload, 1 MB chunks) · `GET /api/sources/{id}` · `DELETE /api/sources/{id}` |
| **Path probe** | `GET /api/media/probe?path=...` — validates an absolute path server-side and reports existence, size, duration and whether an audio track is present |
| **Jobs** | `POST /api/jobs` · `GET /api/jobs?status=&limit=` · `GET /api/jobs/{id}` · `POST /api/jobs/{id}/cancel` · `POST /api/jobs/{id}/retry` |
| **Transcripts** | `GET /api/transcripts` · `GET /api/transcripts/{id}` · `PATCH /api/transcripts/{id}` (title, description, course, text, segments) · `DELETE /api/transcripts/{id}` |
| **Export** | `GET /api/transcripts/{id}/export?format=txt\|md\|json\|segments` |
| **Correction agent** | `POST /api/transcripts/{id}/fix` · `GET /api/transcripts/{id}/proposals` · `POST /api/transcripts/{id}/proposals/accept` · `POST /api/transcripts/{id}/proposals/reject` · `POST /api/transcripts/{id}/proposals/preview` · `POST /api/transcripts/{id}/proposals/undo` |
| **Course glossary** | `GET /api/terms?project_id=` · `POST /api/terms` · `DELETE /api/terms/{id}` |
| **LLM providers** | `GET /api/providers` · `PATCH /api/providers/{id}` · `POST /api/providers/{id}/test` · `GET /api/providers/{id}/models` (model listing, used for Ollama) |
| **Settings** | `GET /api/settings` · `PUT /api/settings` |
| **Maintenance** | `POST /api/maintenance/backup` · `GET /api/maintenance/backups` · `POST /api/maintenance/purge-sources` |

### LLM providers

Three providers, all spoken to through the OpenAI-compatible protocol, all configurable from the UI
(base URL, model, API key, enable/disable, plus a **Test** button and a **List models** button for
Ollama).

| Provider | Base URL | Default model | API key |
|---|---|---|---|
| Ollama (local) | `http://localhost:11434/v1` | `qwen2.5:7b` | not needed |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` | required |
| OpenRouter | `https://openrouter.ai/api/v1` | `deepseek/deepseek-chat-v3.1` | required |

API keys are **encrypted at rest with Fernet**. The master key lives in `data/secret.key` (created on
first run with `chmod 600` where the filesystem supports it) or can be supplied through the
`TRASCRIVI_SECRET` environment variable / the `.env` file. Keys are **never returned by the API**: the
response only ever exposes `has_key: true`.

---

## Data layout, backup and restore

All application state lives under `data/`, which is gitignored:

```
data/
├── trascrivi.db          SQLite database (WAL mode) — courses, sources, jobs, transcripts, terms, providers
├── sources/<course>/     copies of audio uploaded from the browser (named <sha256-12>-<filename>)
├── transcripts/          structured transcripts as JSON
├── partial/              partial output of running or interrupted jobs, used for resume
├── secret.key            Fernet key used to encrypt the LLM API keys
├── settings.json         preferences editable from the UI
├── logs/app.log          application log (rotated at 1 MB, 3 backups)
└── backup/               database backups created by the maintenance endpoint
```

### Backing up

Because the database runs in **SQLite WAL mode**, a plain file copy taken while the server is running is
not sufficient: recent commits can still be sitting in the `-wal` sidecar file, so the copy can be
inconsistent or silently miss the last transactions. That is exactly why the **Back up database**
button (and `POST /api/maintenance/backup`) exists — it produces a consistent snapshot via the SQLite
backup API. The most recent `backup_keep` snapshots (default 5) are retained in `data/backup/`.

To restore: stop the server, replace `data/trascrivi.db` with the snapshot you want, and start it again.
Copying the whole `data/` directory while the server is **stopped** is also a complete backup and is the
simplest way to move the installation to another machine — carry `data/secret.key` (or set the same
`TRASCRIVI_SECRET`) with it, otherwise the stored API keys cannot be decrypted and you will have to
re-enter them.

---

## Privacy and offline behaviour

- Everything runs **locally**. There are no network calls unless you explicitly configure and run a
  cloud LLM provider (DeepSeek or OpenRouter). Ollama stays on your machine.
- Audio you upload from the browser is **deleted after a successful transcription**: the setting
  `delete_sources_after_job` defaults to **on**, and can be turned off per job or in Settings.
- Files you reference by **absolute path** are read **in place** and are never copied, moved or deleted
  by the app. Only the path is recorded.
- Uploads are streamed to disk in 1 MB chunks, so a large file does not have to fit in memory.

---

## Limitations

Stated plainly, so you can decide before installing:

- **No speaker diarization.** The transcript is one continuous text; it does not say who spoke.
- **No translation.** The language you transcribe in is the language you get.
- **No SRT/VTT subtitle export.** Exports are `.txt`, `.md`, `.json` and a segments-with-timestamps
  file. There is no subtitle format.
- **No authentication and no multi-user support.** It is a single-user tool and binds to `127.0.0.1` by
  default. Exposing it on a network interface hands your data to anyone who can reach the port.
- **The LLM agent never rewrites the full text, by design.** There is no "full rewrite" mode. Edits are
  bounded to `max_delta_chars = 160` and anything larger is rejected as `too_large`.
- **Uploads are capped at 4 GB per file**, with a warning above 1 GB. A three-hour lecture is well under
  1 GB, so the cap guards against selecting the wrong file rather than being a real constraint.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| First transcription hangs for minutes with no progress | The model is being downloaded from Hugging Face. `distil-large-v3` is ~1.5 GB. Let it finish; later runs load from cache. |
| Model download fails on Windows | The Hugging Face "xet" backend uses symlinks, which need Developer Mode or admin rights, and it can also hit CUDA DLL load issues. Set `HF_HUB_DISABLE_XET=1` before starting (see `.env.example`) — the engine already handles the classic symlink and CUDA DLL problems itself. |
| Port already in use | The launcher tries the next free port automatically, up to 10 attempts from `--port`. The chosen URL is printed at startup. |
| A job is stuck in *running* after a hard kill | On restart the app marks every `running`/`queued` job as `interrupted`. Open **Jobs** and press **Retry** — the partial file is kept, so it resumes instead of starting over. |
| `No audio track found` | The file is video-only or its audio codec is unreadable. The message is raised before the job is created, so nothing is queued. |
| `No API key configured for provider X` | The agent needs a key for DeepSeek/OpenRouter. Add it in **Settings → LLM providers**, or switch the job to Ollama, which needs none. |
| CUDA out of memory | The model cache holds at most **two** loaded models because VRAM is shared. If you also run a local LLM (for example an Ollama model on the same GPU), use **Settings → Unload models from RAM** before running the agent, or force `--device cpu` / a smaller model. |
| Transcribing is very slow but the GPU looks idle | The job fell back to CPU. Check `/api/health` for detected CUDA devices and the free VRAM reported there. |

---

## Tests

The test suite is `pytest`-based and runs against the standard library plus the new dependencies:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

119 tests, a few seconds, no GPU, no network and no Whisper model: DB isolation is done by remapping the
`data/` paths to a temporary directory, and the LLM provider is faked. What the suite covers:

| File | What it locks down |
|---|---|
| `tests/test_textutil.py` | timestamp parsing (`mm:ss` and `hh:mm:ss`), text ⇄ segments round trip, monotone timestamps when lines are added, Markdown export |
| `tests/test_resume.py` | `last_transcribed_seconds`, the 2 s resume margin, the English-only model rule |
| `tests/test_corrections.py` | the edit-plan engine: exact and whitespace-tolerant matching, `ambiguous` / `unmatched` / `noop` / `too_large` / `duplicate` / `conflict`, per-segment targets, idempotency, glossary mode, chunking |
| `tests/test_db_api.py` | project/transcript/proposal/term CRUD, FTS5 staying in sync across updates and deletes, startup recovery of stale jobs, and the REST endpoints (including the 422 on an English-only model with a non-English language) |
| `tests/test_llm_parse.py` | JSON extraction from fenced or prefixed model replies, chunk budgets, API-key encryption round trip |
| `tests/test_engine_guards.py` | the three engine bugs found on real audio: `batch_size` on the plain `transcribe`, batched inference with a clip range, and the partial file that makes cancel/resume work |
| `tests/test_web_contract.py` | the frontend: JS syntax (`node --check`), every `byId` having a matching element, and every `/api/...` path the UI calls existing in the OpenAPI schema |

Synthetic fixtures are generated by `gen_testdata.py`, which writes "speech-like" audio with the same
spectral and temporal structure as real speech (formants, syllables, pauses). It is not meant to
evaluate transcription *accuracy* — it is not real speech — but it measures the real throughput of the
pipeline (VAD + batching + decoding) reproducibly and offline:

```powershell
.\.venv\Scripts\python.exe gen_testdata.py                     # 4 files of ~2 minutes in testdata/
.\.venv\Scripts\python.exe gen_testdata.py --files 8 --minutes 5 --out testdata
```

---

## Project layout

```
trascrivi/
├── trascrivi_app.py       entrypoint: picks a free port, opens the browser, runs uvicorn
├── trascrivi_audio.py     original CLI (100% backward compatible) + the transcription engine
├── run.bat                Windows launcher: creates .venv, installs requirements, starts the app
├── gen_testdata.py        synthetic audio fixtures for tests and benchmarks
├── requirements.txt       runtime and dev dependencies
├── README.md              this file
├── README_CPU.md          how to run fast on a CPU-only machine
├── .env.example           template for TRASCRIVI_SECRET and HF_HUB_DISABLE_XET
├── trascrivi/             application package
│   ├── config.py          paths, settings, logging, Fernet secret
│   ├── db.py              schema, queries, FTS5, backup
│   ├── models.py          LRU cache of up to 2 loaded Whisper models + hardware info
│   ├── jobs.py            queue worker, cancel, resume, crash recovery
│   ├── corrections.py     deterministic edit-plan engine
│   ├── llm.py             OpenAI-compatible provider adapter
│   ├── api.py             all REST routes under /api
│   └── textutil.py        text ⇄ segments, timestamp parsing, markdown export
├── web/                   plain HTML/CSS/JS frontend, served at /
│   ├── index.html
│   ├── app.js
│   └── style.css
├── tests/                 pytest suite
└── data/                  all runtime state (gitignored) — see "Data layout" above
```

---

## License and credits

No third-party code was copied into this repository. The project builds on the following open-source
dependencies, each under its own license:

- **faster-whisper** and **CTranslate2** — the transcription engine
- **PyAV** — direct audio/video decoding without an external `ffmpeg` binary
- **FastAPI** and **uvicorn** — the HTTP layer
- **rich** — terminal output of the CLI
- **cryptography** — Fernet encryption of the stored API keys
- **openai** — the OpenAI-compatible client used for Ollama, DeepSeek and OpenRouter

The underlying models (`distil-large-v3`, `large-v3`, …) are downloaded from Hugging Face at runtime and
remain subject to their own licenses.
