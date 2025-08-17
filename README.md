# ShootFlow

ShootFlow is a serverless system that uploads ready-to-go photos to Flickr on a schedule, learns from engagement (likes/comments), and automatically submits top-ranked images to PhotoCrowd and Instagram. It can also track Instagram comments in real time and auto-reply using ChatGPT.

---

## Features

- **S3 upload ingestion** → on each upload to `ready/`, extract **EXIF/IPTC/XMP** metadata and create a DynamoDB record (`status=READY`)
- **Scheduled Flickr posting** using the DB as the source of truth
- **Engagement ingestion** (likes/comments) from Flickr
- **Ranking engine** with age-decay; flags top items for submission
- **Automated submissions** to PhotoCrowd (headless browser) and Instagram (Graph API)
- **Instagram auto-replies** via webhook → ChatGPT → comment replies edge
- **Observability**: structured logs, metrics, alarms

---

## Repository Model

- **shootflow-infra** (separate repo): CDK/Terraform stacks, IAM, S3/DDB/SQS/EventBridge/API GW, and **Lambda function resources** that all point to the same image but use different handlers.
- **shootflow-services** (this repo): all Lambda code lives here; built into **one container image** with **multiple handler entrypoints**. Infra selects the handler via the Lambda **image command** (or `_HANDLER` env) per function.

> Why one image?  
> - Single dependency graph (incl. headless Chromium for PhotoCrowd and EXIF tooling).  
> - Faster, simpler CI/CD—one build, many functions.  
> - Handlers share a common utilities layer (logging, storage, API clients).

---

## Services Repo Layout (`shootflow-services`)

```
shootflow-services/
├─ src/
│  ├─ handlers/
│  │  ├─ s3_ingest.py            # S3 → EXIF parse → DB (READY)
│  │  ├─ flickr_poster.py        # schedule → claim READY → upload to Flickr
│  │  ├─ feedback_poll.py        # poll Flickr metrics
│  │  ├─ ranker.py               # compute score; enqueue submissions
│  │  ├─ ig_poster.py            # Instagram Graph API publish
│  │  ├─ ig_webhook_receiver.py  # API GW webhook intake → SQS
│  │  └─ reply_generator.py      # dequeue comment → ChatGPT → reply
│  ├─ clients/                   # Flickr/IG/OpenAI wrappers
│  ├─ shared/                    # models (pydantic), storage, logging, exif
│  └─ runtime/                   # headless browser bootstrap, helpers
├─ Dockerfile                    # single image for all handlers (includes exiftool)
├─ pyproject.toml                # Poetry project
├─ README.md
└─ .github/workflows/
   └─ build-and-push.yml         # builds image, pushes to ECR
```

**Handler names (entrypoints)** — referenced by infra as image commands:
- `src.handlers.s3_ingest:handler`
- `src.handlers.flickr_poster:handler`
- `src.handlers.feedback_poll:handler`
- `src.handlers.ranker:handler`
- `src.handlers.ig_poster:handler`
- `src.handlers.ig_webhook_receiver:handler`
- `src.handlers.reply_generator:handler`

---

## EXIF-Driven Metadata (no sidecars)

**Ingestion behavior (`s3_ingest`)**
1. On `ObjectCreated:*` for `ready/` images, stream the object from S3.
2. Compute **SHA‑256** for dedupe (`PHOTO#<file_hash>` as primary key).
3. Extract metadata from the image file:   Preferred mapping (first present wins):
   - **Title**: `XMP-dc:title` → `IPTC:ObjectName` → `EXIF:ImageDescription`
   - **Caption/Description**: `XMP-dc:description` → `IPTC:Caption-Abstract` → `EXIF:UserComment`
   - **Tags/Keywords**: `XMP-dc:subject[]` → `IPTC:Keywords[]`
   - **Capture Time**: `EXIF:DateTimeOriginal`
   - **GPS**: `EXIF:GPSLatitude`, `EXIF:GPSLongitude` (+ `GPSAltitude` when available)
   - **Location names** (optional): `XMP:LocationShown`, `IPTC:City/State/Country`
4. Write a **Photo** item with `status=READY` (see Data Model below).   Store camera/lens/exposure internally but **do not** use them for public tags.

**Implementation notes**
- Container bundles a static **exiftool** binary for robust EXIF/IPTC/XMP coverage. Fallback to `exifread`/`Pillow` if needed.
- Normalize keywords to lowercase, trim whitespace, and **dedupe**.
- Enforce a **blocklist** for gear-specific tags (camera brand/model/lens) to match our tagging policy.

---

## Configuration

Store production secrets in **AWS Secrets Manager**; use a local `.env` for dev.

```ini
# Core
AWS_REGION=us-east-1
S3_BUCKET=shootflow-ready
DDB_TABLE=shootflow-db

# Flickr
FLICKR_API_KEY=
FLICKR_API_SECRET=
FLICKR_OAUTH_TOKEN=

# Instagram / Meta
FB_APP_ID=
FB_APP_SECRET=
IG_ACCESS_TOKEN=
IG_USER_ID=

# OpenAI
OPENAI_API_KEY=

# PhotoCrowd (no public API; headless automation)
PHOTOCROWD_USERNAME=
PHOTOCROWD_PASSWORD=

# App tuning
POST_INTERVAL_CRON=rate(3 hours)
RANKING_MIN_AGE_HOURS=12
TOP_N_PER_DAY=2
DEFAULT_HASHTAGS="#photography #landscape"
```

---

## S3 Ready Folder (no sidecars)

Upload JPEGs to `s3://shootflow-ready/ready/`. **No YAML sidecars**—all metadata is pulled from the image file's EXIF/IPTC/XMP. If fields are missing, the system will:
- Use file name (sans extension) as a provisional **title** (configurable), and
- Leave **tags/caption** empty unless provided by EXIF/XMP/IPTC.

---

## Local Development

```bash
pyenv install 3.10.14
pipx install poetry
poetry install
```

Run a handler locally (example module run with a `--dry-run` flag you implement):

```bash
poetry run python -m src.handlers.flickr_poster --dry-run --limit 1
```

Build & test the container image:

```bash
docker build -t shootflow:local .
# Run a specific handler by overriding CMD or using _HANDLER:
docker run --rm -e _HANDLER="src.handlers.s3_ingest:handler" shootflow:local
```

---

## Deployment

1. **Infra repo** (`shootflow-infra`)
   - Provisions: S3, DDB, SQS, API GW, EventBridge, Secrets, ECR, and **Lambda functions** that all reference **the same image**.
   - Each function sets its own **image command** (e.g., `["src.handlers.flickr_poster:handler"]`).

2. **Services repo** (`shootflow-services`)
   - CI builds the Docker image once, pushes to ECR with tag & digest.
   - CI (or infra pipeline) updates all Lambda functions to the **new image digest**.

---

## Data Model (DynamoDB, single table)

- **Photo**: `pk=PHOTO#<file_hash>`, `sk=METADATA` → `s3_key`, `filename`, `file_hash`, `ingested_at`, `status`, optional `title/tags/caption`, `exif` (JSON), `gps` (lat/lon)
- **Flickr link**: `pk=PHOTO#<file_hash>`, `sk=FLICKR#<photo_id>` → `upload_ts`, `views`, `faves`, `comments`
- **Score**: `pk=PHOTO#<file_hash>`, `sk=SCORE#<iso>` → `score`, `rank_reason`
- **Submission**: `pk=PHOTO#<file_hash>`, `sk=SUBMIT#<target>#<iso>` → `status`, `target_id`
- **IG Comment**: `pk=IGCOMMENT#<comment_id>`, `sk=DETAIL` → `username`, `text`, `replied`

**GSIs**
- `status-index`: READY | POSTING | POSTED | SUBMITTED
- `score-index`: retrieve top scores in a window

---

## Operations

- **Metrics**: `IngestSucceeded`, `IngestDuplicate`, `ClaimSucceeded`, `UploadsSucceeded`, `SubmissionsSucceeded`, `AutoRepliesPosted`
- **Alarms**: webhook 4xx/5xx, DLQ depth, Step Function timeouts
- **Cost**: serverless-only typically **< $10/mo**; add **$30–$70/mo** if you need persistent NAT or headless EC2/Fargate (most cases covered by Lambda container image)
