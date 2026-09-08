# Deploying `pet-care-mcp`

Requirement 7 of the assignment is explicit: the server must **run on a cloud
service** (Google Cloud, Cloudflare, or similar). Option B below is therefore the
one the requirement asks for; option A exists because it is the right way to
develop, and the right way to rehearse the packet capture before spending
anything.

> **Nothing in this guide is executed automatically.** No project is created, no
> billing is enabled, no image is pushed and no service is deployed without you
> running the commands yourself.

---

## Option A — local only (free, no account; development and rehearsal)

```bash
cd remote_server
uv sync
HOST=127.0.0.1 PORT=8080 uv run pet-care-mcp
```

Then, in `config/servers.toml`:

```toml
[[servers]]
name = "pet_care_remote"
transport = "streamable-http"
enabled = true
url = "http://127.0.0.1:8080/mcp"
```

What this **does** demonstrate: Streamable HTTP, a real TCP connection, HTTP
request/response framing, and JSON-RPC readable in Wireshark on the loopback
interface.

What it **does not** demonstrate: DNS resolution, routing beyond the host, TLS, or
a public endpoint — and it does **not** satisfy requirement 7, which asks for a
cloud deployment. Capture both if you can: the plaintext local capture explains
what the encrypted remote one contains.

To capture traffic across a real interface without a cloud account, an intermediate
step is to bind to `0.0.0.0` and connect from a second machine on the same LAN:

```bash
HOST=0.0.0.0 PORT=8080 uv run pet-care-mcp
```

This is still plaintext HTTP: only do it on a network you control, and stop the
server afterwards. It gives real Ethernet frames, real IP routing and a real MAC
pair for the link-layer section of the report.

---

## Option B — Google Cloud Run

### Cost and risk, before anything else

| Item | Reality |
| --- | --- |
| Alternative without a card | Render's free web-service tier and similar platforms accept the same container with no billing account. Option C covers that; the requirement only asks for *a* cloud service. |
| Cost at demo scale | Effectively zero. Cloud Run's free tier covers far more than a few hundred requests per month, and a scale-to-zero service costs nothing while idle. |
| What you must enable | A billing account on the Google Cloud project. Free tier still requires one. |
| Real risk | A public, unauthenticated endpoint. Anyone with the URL can call your tools. These two tools are harmless and stateless, but the habit is not. |
| Second risk | Forgetting to delete it. Set a calendar reminder, or run the teardown command below right after the demo. |
| Artifact Registry | Stored images cost a few cents per GB-month. Delete the repository when done. |

The assignment does require it, so this is the path to take. Deploy shortly before
the demo, and delete it afterwards with the teardown commands below.

### Prerequisites

```bash
gcloud --version          # install from https://cloud.google.com/sdk if missing
gcloud auth login
gcloud config set project <YOUR_PROJECT_ID>
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com
```

### Generate the lockfile the image needs

The `Dockerfile` uses `uv sync --frozen`, so `remote_server/uv.lock` must exist and
be committed:

```bash
cd remote_server
uv lock
```

### Build and deploy

```bash
cd remote_server

gcloud run deploy pet-care-mcp \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --port 8080 \
  --min-instances 0 \
  --max-instances 2 \
  --memory 512Mi \
  --timeout 300
```

`--source .` lets Cloud Build build the `Dockerfile` for you; no local Docker
needed. `--min-instances 0` is what makes it free while idle. `--max-instances 2`
is a cost guard rail.

Cloud Run injects `PORT`; `main()` in `server.py` reads it, and `HOST` defaults are
overridden by the `ENV HOST=0.0.0.0` in the `Dockerfile`.

### Verify

```bash
URL=$(gcloud run services describe pet-care-mcp --region us-central1 --format 'value(status.url)')
echo "$URL"
curl "$URL/healthz"
```

Expect `{"status":"ok","server":"pet-care","version":"1.0.0"}`.

### Point the chatbot at it

In `config/servers.toml` — **the URL belongs in configuration, never in the
source**:

```toml
[[servers]]
name = "pet_care_remote"
transport = "streamable-http"
enabled = true
url = "https://pet-care-mcp-XXXXXXXX-uc.a.run.app/mcp"
timeout_seconds = 60
connect_timeout_seconds = 60
```

Then:

```bash
uv run adoptforme-chatbot --offline --check
```

### Watch the logs

```bash
gcloud run services logs tail pet-care-mcp --region us-central1
```

### Tear it down when the demo is over

```bash
gcloud run services delete pet-care-mcp --region us-central1 --quiet
gcloud artifacts repositories list                       # find the cloud-run-source-deploy repo
gcloud artifacts repositories delete cloud-run-source-deploy --location us-central1 --quiet
```

---

## Option C — any other container platform

The image is a plain, non-root Python container that listens on `$PORT`. It runs
unchanged on Render, Fly.io, Railway, Azure Container Apps or a VM with Docker:

```bash
cd remote_server
docker build -t pet-care-mcp .
docker run --rm -p 8080:8080 -e PORT=8080 pet-care-mcp
curl http://127.0.0.1:8080/healthz
```

---

## Notes for the network analysis

- Over `http://` the JSON-RPC bodies are readable in Wireshark. Over `https://`
  they are not: TLS encrypts the whole HTTP layer, and only the TCP connection,
  the TLS handshake (including the SNI) and the sizes and timing of the encrypted
  records are visible.
- Capture **both** if you can. The plaintext local capture explains what the
  encrypted remote capture contains, and the host's JSONL log ties the two
  together.
- To decrypt your own HTTPS session legitimately, see
  [`wireshark-analysis.md`](wireshark-analysis.md) § 8 (`SSLKEYLOGFILE`).

## Security checklist before deploying

- [ ] The service exposes only `/mcp` and `/healthz`.
- [ ] Neither tool touches the filesystem, a database, or another network service.
- [ ] Neither tool gives medical advice; both return a disclaimer.
- [ ] Input is validated by the tool schemas plus explicit range checks.
- [ ] The container runs as a non-root user.
- [ ] No secret is baked into the image — the server needs none.
- [ ] `--max-instances` is set, so a runaway cannot scale indefinitely.
- [ ] A teardown date is written down.
