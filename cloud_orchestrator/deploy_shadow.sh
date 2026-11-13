#!/usr/bin/env bash
# Each phase is explicit. Never upgrades billing, exports credentials or starts Kaggle jobs.
set -euo pipefail
PROJECT="${1:?project ID required}"
PHASE="${2:-preflight}"
[[ "$PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || exit 64
REGION=us-central1
SERVICE=kcp-cloud-shadow
REPOSITORY=kcp-cloud
RUNNER="kcp-cloud-runner@${PROJECT}.iam.gserviceaccount.com"
SCHEDULER="kcp-cloud-clock@${PROJECT}.iam.gserviceaccount.com"
BUILDER="kcp-cloud-builder@${PROJECT}.iam.gserviceaccount.com"
BUCKET="gs://${PROJECT}-kcp-build-src"
STATE="${PWD}/.deploy"
mkdir -p "$STATE"
GCLOUD=(gcloud --quiet --project="$PROJECT")

preflight() {
  "${GCLOUD[@]}" projects describe "$PROJECT" --format=json > "$STATE/project.json"
  "${GCLOUD[@]}" billing projects describe "$PROJECT" --format='json(projectId,billingEnabled)' > "$STATE/billing.json"
  python3 - "$PROJECT" "$STATE" <<'PY'
import json,sys,pathlib
project,folder=sys.argv[1],pathlib.Path(sys.argv[2])
p=json.loads((folder/'project.json').read_text())
b=json.loads((folder/'billing.json').read_text())
assert p['projectId']==project and p['lifecycleState']=='ACTIVE', 'Project identity/state mismatch'
assert b['projectId']==project and b.get('billingEnabled') is True, 'Billing not enabled'
print('PREFLIGHT_PASS: active exact project; billing linked; no upgrade performed')
PY
}

verify_source() {
  python3 - <<'PY'
import hashlib,json,pathlib
root=pathlib.Path('.').resolve()
manifest=json.loads((root/'SOURCE_MANIFEST.json').read_text())
assert manifest and len(manifest)<40
for name,expected in manifest.items():
    p=(root/name).resolve()
    assert p.parent==root and p.is_file(), 'Unsafe source name'
    assert hashlib.sha256(p.read_bytes()).hexdigest()==expected, 'Source SHA mismatch'
print('SOURCE_SHA_PASS')
PY
}

case "$PHASE" in
  verify-source)
    verify_source
    ;;
  preflight)
    preflight
    ;;
  resources)
    preflight
    verify_source
    "${GCLOUD[@]}" services enable run.googleapis.com firestore.googleapis.com cloudscheduler.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com iam.googleapis.com
    "${GCLOUD[@]}" iam service-accounts list --format='value(email)' > "$STATE/accounts.txt"
    for id in kcp-cloud-runner kcp-cloud-clock kcp-cloud-builder; do
      email="${id}@${PROJECT}.iam.gserviceaccount.com"
      if ! grep -Fxq "$email" "$STATE/accounts.txt"; then
        "${GCLOUD[@]}" iam service-accounts create "$id" --display-name="$id"
      fi
    done
    "${GCLOUD[@]}" firestore databases list --format=json > "$STATE/databases.json"
    db_count=$(python3 - "$STATE/databases.json" <<'PY'
import json,sys
rows=json.load(open(sys.argv[1])); print(len(rows))
PY
)
    if [[ "$db_count" == 0 ]]; then
      "${GCLOUD[@]}" firestore databases create --database='(default)' --location="$REGION" --type=firestore-native
    fi
    # Do not silently use a paid secondary database or a different existing location.
    "${GCLOUD[@]}" firestore databases describe --database='(default)' --format=json > "$STATE/database.json"
    python3 - "$STATE/database.json" "$REGION" <<'PY'
import json,sys
d=json.load(open(sys.argv[1])); assert d['locationId']==sys.argv[2] and d['type']=='FIRESTORE_NATIVE'
PY
    "${GCLOUD[@]}" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$RUNNER" --role=roles/datastore.user --condition="expression=resource.name==\"projects/${PROJECT}/databases/(default)\",title=kcp-default-db-only" --format=none
    "${GCLOUD[@]}" artifacts repositories list --location="$REGION" --format='value(name)' > "$STATE/repositories.txt"
    if ! grep -Fxq "projects/${PROJECT}/locations/${REGION}/repositories/${REPOSITORY}" "$STATE/repositories.txt"; then
      "${GCLOUD[@]}" artifacts repositories create "$REPOSITORY" --location="$REGION" --repository-format=docker
    fi
    "${GCLOUD[@]}" artifacts repositories add-iam-policy-binding "$REPOSITORY" --location="$REGION" --member="serviceAccount:$BUILDER" --role=roles/artifactregistry.writer --format=none
    "${GCLOUD[@]}" projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$BUILDER" --role=roles/logging.logWriter --condition=None --format=none
    "${GCLOUD[@]}" storage buckets list --format='value(name)' > "$STATE/buckets.txt"
    if ! grep -Fxq "${BUCKET#gs://}" "$STATE/buckets.txt" && ! grep -Fxq "$BUCKET" "$STATE/buckets.txt"; then
      "${GCLOUD[@]}" storage buckets create "$BUCKET" --location="$REGION" --uniform-bucket-level-access --public-access-prevention
    fi
    "${GCLOUD[@]}" storage buckets add-iam-policy-binding "$BUCKET" --member="serviceAccount:$BUILDER" --role=roles/storage.objectViewer --format=none
    printf '%s\n' 'RESOURCES_PASS: private resources and narrow service identities; no keys'
    ;;
  build)
    preflight
    verify_source
    SOURCE_SHA=$(sha256sum SOURCE_MANIFEST.json | cut -d ' ' -f1)
    TAG="sha-${SOURCE_SHA:0:16}"
    IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPOSITORY}/shadow:${TAG}"
    "${GCLOUD[@]}" builds list --region="$REGION" --filter="tags:${TAG}" --limit=20 --format=json > "$STATE/existing-builds.json"
    if [[ "$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))))' "$STATE/existing-builds.json")" != 0 ]]; then
      printf '%s\n' 'RECONCILE_EXISTING_BUILD: inspect existing-builds.json; do not replay build submission'
      exit 75
    fi
    printf '%s\n' "$IMAGE" > "$STATE/image.txt"
    printf '%s\n' "$SOURCE_SHA" > "$STATE/build-intent.txt"
    "${GCLOUD[@]}" builds submit . --region="$REGION" --async --config=cloudbuild.yaml --service-account="projects/${PROJECT}/serviceAccounts/${BUILDER}" --gcs-source-staging-dir="${BUCKET}/source" --substitutions="_IMAGE=${IMAGE},_SOURCE_SHA=${SOURCE_SHA},_TAG=${TAG}" --format=json > "$STATE/build-receipt.json"
    python3 - "$STATE/build-receipt.json" <<'PY'
import json,sys
b=json.load(open(sys.argv[1])); print('BUILD_SUBMITTED',b['id'],b.get('status'))
PY
    ;;
  service)
    preflight
    verify_source
    BUILD_ID=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["id"])' "$STATE/build-receipt.json")
    "${GCLOUD[@]}" builds describe "$BUILD_ID" --region="$REGION" --format=json > "$STATE/build-complete.json"
    IMAGE=$(python3 - "$STATE/build-complete.json" <<'PY'
import json,sys
b=json.load(open(sys.argv[1])); assert b['status']=='SUCCESS', 'Build not complete SUCCESS'
images=b['results']['images']; assert len(images)==1
print(images[0]['name'].split('@')[0].rsplit(':',1)[0]+'@'+images[0]['digest'])
PY
)
    "${GCLOUD[@]}" run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" --no-allow-unauthenticated --service-account="$RUNNER" --memory=512Mi --cpu=1 --concurrency=1 --min-instances=0 --max-instances=1 --timeout=60 --cpu-throttling --no-cpu-boost --labels=app=kcp-cloud,mode=shadow --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT},KCP_CLOUD_MODE=shadow" --format=json > "$STATE/service.json"
    "${GCLOUD[@]}" run services add-iam-policy-binding "$SERVICE" --region="$REGION" --member="serviceAccount:$SCHEDULER" --role=roles/run.invoker --format=none
    printf '%s\n' 'SERVICE_DEPLOYED_PRIVATE_SHADOW: verify tick before creating schedule'
    ;;
  schedule)
    preflight
    URL=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"]["url"])' "$STATE/service.json")
    [[ -f "$STATE/tick-pass.json" ]] || { printf '%s\n' 'STOP: actual private HTTP tick verification required'; exit 65; }
    python3 - "$STATE/tick-pass.json" <<'PY'
import json,sys
r=json.load(open(sys.argv[1])); assert r.get('mode')=='shadow' and r.get('status')=='RECORDED' and r.get('submission_enabled') is False
PY
    "${GCLOUD[@]}" scheduler jobs list --location="$REGION" --format=json > "$STATE/schedules.json"
    python3 - "$STATE/schedules.json" <<'PY'
import json,sys
assert not json.load(open(sys.argv[1])), 'Review existing schedules before consuming free job allowance'
PY
    "${GCLOUD[@]}" scheduler jobs create http kcp-cloud-tick --location="$REGION" --schedule='*/10 * * * *' --time-zone=Etc/UTC --uri="${URL}/tick" --http-method=POST --oidc-service-account-email="$SCHEDULER" --oidc-token-audience="$URL" --attempt-deadline=60s --max-retry-attempts=0 --format=json > "$STATE/schedule.json"
    printf '%s\n' 'SCHEDULE_CREATED_SHADOW_ONLY'
    ;;
  *) printf '%s\n' 'Use verify-source, preflight, resources, build, service or schedule'; exit 64 ;;
esac
