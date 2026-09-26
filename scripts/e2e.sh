#!/bin/sh
# End-to-end smoke against a disposable Compose stack: API, worker and web proxy.
# Uses its own project name, ports and volumes, and tears everything down on exit.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PROJECT=${E2E_PROJECT:-bag-e2e}
ENV_FILE=$(mktemp)
WORK=$(mktemp -d)
API_PORT=${E2E_API_PORT:-18000}
WEB_PORT=${E2E_WEB_PORT:-18080}
PG_PORT=${E2E_PG_PORT:-15432}
COMPOSE="docker compose -p $PROJECT --env-file $ENV_FILE -f $ROOT/deploy/compose/compose.yaml"
PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')
POSTGRES_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "--- e2e failed; recent logs:"
    $COMPOSE logs --no-log-prefix --tail 40 bag-api bag-worker bag-web 2>/dev/null || true
  fi
  $COMPOSE down -v --remove-orphans > /dev/null 2>&1 || true
  rm -rf "$ENV_FILE" "$WORK"
  exit "$status"
}
trap cleanup EXIT INT TERM

cat > "$ENV_FILE" <<EOF
POSTGRES_USER=bag
POSTGRES_PASSWORD=$POSTGRES_PASSWORD
POSTGRES_DB=bag
BAG_POSTGRES_PORT=$PG_PORT
BAG_API_PORT=$API_PORT
BAG_WEB_PORT=$WEB_PORT
BAG_COOKIE_SECURE=false
BAG_WORKER_POLL_SECONDS=0.2
BAG_JOB_RETRY_SECONDS=0.5
BAG_FETCH_TIMEOUT_SECONDS=2
EOF

json() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {"d": d}))' "$1"; }
web() { curl --fail-with-body -sS -b "$WORK/jar" -c "$WORK/jar" -H 'X-Bag-Csrf: 1' "$@"; }

echo "--- build and start"
$COMPOSE build > "$WORK/build.log" 2>&1 || { tail -30 "$WORK/build.log"; exit 1; }
$COMPOSE up -d --wait postgres > /dev/null 2>&1
$COMPOSE run --rm -T bag-api bag migrate > /dev/null 2>&1
TOKEN=$($COMPOSE run --rm -T bag-api bag init 2>/dev/null | sed -n 's/^API token (shown once): //p')
[ -n "$TOKEN" ] || { echo "init printed no token"; exit 1; }
printf '%s\n' "$PASSWORD" | $COMPOSE run --rm -T bag-api bag password set --username e2e --stdin > /dev/null
$COMPOSE up -d --wait bag-api bag-worker bag-web > /dev/null 2>&1
for _ in $(seq 1 30); do
  curl -sf "http://127.0.0.1:$WEB_PORT/health" > /dev/null && break
  sleep 1
done
WEB="http://127.0.0.1:$WEB_PORT"

echo "--- bearer token works against the API port"
curl --fail-with-body -sS "http://127.0.0.1:$API_PORT/api/v1/items" -H "Authorization: Bearer $TOKEN" > /dev/null

echo "--- login through the web proxy"
web -H 'Content-Type: application/json' "$WEB/api/v1/session" \
  --data "{\"username\":\"e2e\",\"password\":\"$PASSWORD\"}" | json 'd["via"]' | grep -qx session
grep -q bag_session "$WORK/jar"

echo "--- capture text and wait for processing"
ITEM=$(web -H 'Content-Type: application/json' "$WEB/api/v1/capture/text" \
  --data '{"content":"Die Taschen sind voll und wir haben noch nicht alle Bücher aus dem Keller geholt.","user_note":"E2E"}' | json 'd["id"]')
for _ in $(seq 1 60); do
  STATUS=$(web "$WEB/api/v1/items/$ITEM" | json 'd["processing_status"]')
  [ "$STATUS" = ready ] && break
  sleep 0.5
done
[ "$STATUS" = ready ] || { echo "item stuck in $STATUS"; web "$WEB/api/v1/items/$ITEM/processing"; exit 1; }
web "$WEB/api/v1/items/$ITEM" | json 'd["language"]' | grep -qx de
web -G "$WEB/api/v1/search" --data-urlencode 'q=Tasche' | json '[r["id"] for r in d["results"]]' | grep -q "$ITEM"

echo "--- private URLs are skipped, never fetched"
URL=$(web -H 'Content-Type: application/json' "$WEB/api/v1/capture/url" \
  --data '{"url":"http://127.0.0.1:9/private"}' | json 'd["id"]')
for _ in $(seq 1 60); do
  [ "$(web "$WEB/api/v1/items/$URL" | json 'd["processing_status"]')" = ready ] && break
  sleep 0.5
done
web "$WEB/api/v1/items/$URL/processing" | json '[r["status"] for r in d if r["processor"]=="url_fetch"][0]' | grep -qx skipped

echo "--- upload, download and compare a file"
head -c 200000 /dev/urandom > "$WORK/original.bin"
FILE=$(web "$WEB/api/v1/capture/file" -F "file=@$WORK/original.bin" -F 'metadata={"user_note":"binary"}' | json 'd["id"]')
web "$WEB/api/v1/items/$FILE/content" --output "$WORK/downloaded.bin"
cmp "$WORK/original.bin" "$WORK/downloaded.bin"

echo "--- edit, trash, restore"
web -X PATCH -H 'Content-Type: application/json' "$WEB/api/v1/items/$FILE" \
  --data '{"title":"Zufall","tags":["e2e"]}' | json 'd["tags"]' | grep -q e2e
web -X DELETE "$WEB/api/v1/items/$FILE" > /dev/null
web "$WEB/api/v1/items?trashed=true" | json '[r["id"] for r in d["items"]]' | grep -q "$FILE"
web -X POST "$WEB/api/v1/items/$FILE/restore" | json 'd["title"]' | grep -qx Zufall

echo "--- session-only token management and logout"
web "$WEB/api/v1/tokens" | json 'len(d)' | grep -qx 1
curl -sS -o /dev/null -w '%{http_code}' "$WEB/api/v1/tokens" -H "Authorization: Bearer $TOKEN" | grep -qx 403
web -X DELETE "$WEB/api/v1/session" > /dev/null
curl -sS -o /dev/null -w '%{http_code}' -b "$WORK/jar" "$WEB/api/v1/session" | grep -qx 401

echo "--- export round trip inside the container"
$COMPOSE run --rm -T bag-api bag export /tmp/e2e-export | json 'd["items"]' | grep -qx 3
echo "e2e passed"
