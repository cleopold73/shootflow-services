# ShootFlow Architecture (Single Services Repo, Single Image, Multiple Entrypoints — EXIF-Driven)

This document describes the target architecture and how multiple Lambda functions share **one** container image while selecting distinct handlers at deploy time. **All metadata is sourced from EXIF/IPTC/XMP**, not from sidecar files.

---

## Goals

- Automate end-to-end publishing and cross-posting
- Learn from engagement and bias toward higher-performing content
- Minimize cost/ops via serverless
- **Single build** for all services; **separate infra repo**
- **EXIF-driven ingestion** (no sidecars)

---

## High-Level Diagram

```
           +-----------------+           +--------------------+
           |  S3: ready/     |           | Secrets Manager    |
           | images only     |           | (API keys, creds)  |
           +--------+--------+           +---------+----------+
                    | (ObjectCreated)               |
                    v                                v
          +---------+-----------+              +-----+-------------------+
          | Lambda: s3_ingest  |              | DynamoDB: shootflow_db |
          |  EXIF→DB (READY)   |-------------->  source of truth        |
          +---------+-----------+              +-----+-------------------+
                    |                                     ^       ^
     EventBridge -> | (schedule)                          |       |
                    v                                     |       |
               +----+------------------+                  |       |
               | Lambda: flickr_poster |------------------+       |
               +----+------------------+                          |
                    |    uploads Flickr                           |
                    v                                              |
               +----+-------------------+                          |
               | Lambda: feedback_poll  |--------------------------+
               +----+-------------------+
                    |
               +----+-------------------+         +----------------------+
               | Lambda: ranker         |----> SQS| submissions-queue    |
               +----+-------------------+         +----------+-----------+
                                                         +---+---+
                                                         |       |
                                                 +-------v-+   +-v--------+
                                                 |photocrowd|  | ig_poster|
                                                 |  _bot    |  | (Graph)  |
                                                 +----------+  +----------+
(Instagram webhook + ChatGPT path not drawn; unchanged.)
```

---

## Function Mapping (all share one image)

| Lambda Function        | Trigger                         | Image Command (handler)                   |
|------------------------|----------------------------------|-------------------------------------------|
| `s3_ingest`            | S3 `ObjectCreated:*` on `ready/` | `src.handlers.s3_ingest:handler`          |
| `flickr_poster`        | EventBridge schedule             | `src.handlers.flickr_poster:handler`      |
| `feedback_poll`        | EventBridge schedule             | `src.handlers.feedback_poll:handler`      |
| `ranker`               | EventBridge schedule             | `src.handlers.ranker:handler`             |
| `photocrowd_bot`       | SQS (`submissions-queue`)        | `src.handlers.photocrowd_bot:handler`     |
| `ig_poster`            | SQS (`submissions-queue`)        | `src.handlers.ig_poster:handler`          |
| `ig_webhook_receiver`  | API Gateway (Meta Webhook)       | `src.handlers.ig_webhook_receiver:handler`|
| `reply_generator`      | SQS (`ig-comments-queue`)        | `src.handlers.reply_generator:handler`    |

---

## EXIF Ingestion Details

**Libraries/Tools in the container**
- **exiftool** (static) for broad EXIF/IPTC/XMP coverage → JSON
- Optionally: `exifread`/`Pillow` for light-weight fallback

**Field mapping (first present wins)**
- **Title** → `XMP-dc:title` → `IPTC:ObjectName` → `EXIF:ImageDescription`
- **Caption** → `XMP-dc:description` → `IPTC:Caption-Abstract` → `EXIF:UserComment`
- **Keywords/Tags** → `XMP-dc:subject[]` → `IPTC:Keywords[]` (normalize: lower-case, trim, dedupe)  *Exclude camera/lens/brand fields from public tags by policy.*
- **CaptureDate** → `EXIF:DateTimeOriginal`
- **GPS** → `EXIF:GPSLatitude`, `EXIF:GPSLongitude` (+ Altitude)  (Optional reverse-geocoding happens outside ingest if desired.)

**DB write (Photo item)**
- `status=READY`, `file_hash`, `s3_key`, `filename`, `ingested_at`
- `title`, `caption`, `tags[]`, `gps`, `exif` (JSON — camera/lens/exposure kept for analytics but not auto-tagged)

**Idempotency**
- `pk=PHOTO#<file_hash>` with conditional Put (`attribute_not_exists(pk)`)

---

## Sequence Diagrams

**A) S3 Upload → EXIF → DB READY**
```
S3:ObjectCreated -> s3_ingest -> S3 (get image)
                               -> exiftool (parse) -> normalized fields
                               -> DynamoDB PutItem(Photo, status=READY)
```

**B) Scheduled Flickr Post**
```
EventBridge -> flickr_poster -> DynamoDB Query(status-index READY limit 1)
                               -> DynamoDB Update(status=POSTING if READY)
                               -> S3 get object
                               -> Flickr API upload (title/tags from DB)
                               -> DynamoDB Put(FLICKR#photo_id), Update(status=POSTED)
```

**C) Feedback → Rank → Submit**
```
feedback_poll -> Flickr API metrics -> DynamoDB upsert
ranker        -> query recent POSTED -> compute scores -> SQS(submissions)
submissions   -> photocrowd_bot (headless) & ig_poster (Graph API)
```

**D) Instagram Comments Autoreply**
```
Meta Webhook -> API GW -> ig_webhook_receiver -> SQS
SQS -> reply_generator -> ChatGPT -> IG Replies API -> DynamoDB mark replied
```

---

## Data Model (single-table)

- **Photo**: `pk=PHOTO#<file_hash>`, `sk=METADATA` → `s3_key`, `filename`, `file_hash`, `ingested_at`, `status`, `title`, `caption`, `tags[]`, `gps`, `exif`
- **Flickr link**: `pk=PHOTO#<file_hash>`, `sk=FLICKR#<photo_id>` → `upload_ts`, `views`, `faves`, `comments`
- **Score**: `pk=PHOTO#<file_hash>`, `sk=SCORE#<iso>` → `score`, `rank_reason`
- **Submission**: `pk=PHOTO#<file_hash>`, `sk=SUBMIT#<target>#<iso>` → `status`, `target_id`
- **IG Comment**: `pk=IGCOMMENT#<comment_id>`, `sk=DETAIL` → `username`, `text`, `replied`

**GSIs**
- `status-index` → READY | POSTING | POSTED | SUBMITTED
- `score-index` → top scores in window

---

## IAM (least privilege)

- `s3_ingest`: `s3:GetObject` (ready/*), `dynamodb:PutItem`, logs
- `flickr_poster`: `dynamodb:Query/UpdateItem`, `s3:GetObject`, `secretsmanager:GetSecretValue`
- `feedback_poll`: `secretsmanager:GetSecretValue`, `dynamodb:UpdateItem`
- `ranker`: `dynamodb:Query/Scan/UpdateItem`, `sqs:SendMessage`
- `photocrowd_bot`: `secretsmanager:GetSecretValue`, `s3:PutObject` (artifacts/)
- `ig_poster`: `secretsmanager:GetSecretValue`, `dynamodb:UpdateItem`
- `ig_webhook_receiver`: `execute-api:Invoke`, `sqs:SendMessage`
- `reply_generator`: `secretsmanager:GetSecretValue`, `dynamodb:UpdateItem`

---

## Security & Policy

- Secrets in Secrets Manager; no plaintext in env
- Verify Meta webhook signatures (`X-Hub-Signature`)
- **Tag policy**: exclude gear/brand/lens fields from public tags (store in `exif` JSON only)
- Prefer VPC endpoints for Secrets/DynamoDB to avoid NAT costs

---

## Observability & Cost

- Logs: JSON with `correlation_id`, `file_hash`, `photo_pk`
- Metrics: `IngestSucceeded`, `IngestDuplicate`, `ClaimSucceeded`, `UploadSucceeded`, `AutoRepliesPosted`
- Cost: serverless-only typically **< $10/mo**; +$30–$70/mo if NAT or headless EC2/Fargate is required (usually not)

---

## Appendix

**Dockerfile notes**
- Bundle `exiftool` binary in the image; call via subprocess and parse JSON output.
- Keep the image lean; cache Poetry deps; pin versions for repeatable builds.

**Ranking formula**
```python
score = (3*faves + 4*comments + 1*views) / max(1, hours_since_upload/24)
```
