#!/usr/bin/env bash
set -Eeuo pipefail

STEP="all"
MODE="safe"
CONFIRM_RESET=""
WITH_BOT=0
REF=""
BACKUP_ROOT="/opt/tutor-assistant-backups"
BACKUP_DIR=""
ENV_FILE=".env"
PROJECT_DIR="$(pwd)"
PROJECT_NAME="${COMPOSE_PROJECT_NAME:-$(basename "$PROJECT_DIR")}"
SKIP_BUILD=0
SKIP_MIGRATIONS=0

ts_now() {
  date +"%Y-%m-%d %H:%M:%S"
}

log() {
  printf "[%s] %s\n" "$(ts_now)" "$*"
}

warn() {
  printf "[%s] WARN: %s\n" "$(ts_now)" "$*" >&2
}

die() {
  printf "[%s] ERROR: %s\n" "$(ts_now)" "$*" >&2
  exit 1
}

usage() {
  cat <<'USAGE'
Usage:
  ./scripts/deploy_prod.sh [options]

Options:
  --step <preflight|backup|deploy|verify|all|rollback-help>
  --mode <safe|reset>                  # default: safe
  --confirm-reset YES                  # required with --mode reset
  --ref <git_ref>                      # tag/sha/branch to deploy
  --with-bot                           # also start bot (polling profile)
  --backup-root <path>                 # default: /opt/tutor-assistant-backups
  --backup-dir <path>                  # explicit backup dir for this run
  --env-file <path>                    # default: .env
  --project-name <name>                # default: basename(cwd)
  --skip-build                         # skip docker compose build
  --skip-migrations                    # skip alembic upgrade head
  -h, --help

Examples:
  ./scripts/deploy_prod.sh --step preflight
  ./scripts/deploy_prod.sh --step all --mode safe --ref main
  ./scripts/deploy_prod.sh --step all --mode reset --confirm-reset YES --ref v0.1.0
  ./scripts/deploy_prod.sh --step rollback-help --ref v0.1.0 --backup-dir /opt/tutor-assistant-backups/tutor-assistant_20260306_120000
USAGE
}

need_value() {
  local arg_name="$1"
  [[ $# -ge 2 ]] || die "Missing value for ${arg_name}"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

require_file() {
  [[ -f "$1" ]] || die "Required file not found: $1"
}

run_compose() {
  docker compose --env-file "$ENV_FILE" "$@"
}

get_env_value() {
  local key="$1"
  local line=""
  line="$(grep -E "^${key}=" "$ENV_FILE" | tail -n1 || true)"
  if [[ -z "$line" ]]; then
    printf ""
    return
  fi
  printf "%s" "${line#*=}"
}

require_env_key() {
  local key="$1"
  local value=""
  value="$(get_env_value "$key")"
  [[ -n "$value" ]] || die "Missing required env key in ${ENV_FILE}: ${key}"
}

env_or_default() {
  local key="$1"
  local fallback="$2"
  local value=""
  value="$(get_env_value "$key")"
  if [[ -n "$value" ]]; then
    printf "%s" "$value"
  else
    printf "%s" "$fallback"
  fi
}

validate_options() {
  case "$STEP" in
    preflight|backup|deploy|verify|all|rollback-help) ;;
    *) die "Unsupported --step: $STEP" ;;
  esac

  case "$MODE" in
    safe|reset) ;;
    *) die "Unsupported --mode: $MODE" ;;
  esac

  if [[ "$MODE" == "reset" && "$CONFIRM_RESET" != "YES" && "$STEP" != "rollback-help" ]]; then
    die "Reset mode requires explicit confirmation: --confirm-reset YES"
  fi
}

show_context() {
  log "Project dir: $PROJECT_DIR"
  log "Project name: $PROJECT_NAME"
  log "Step: $STEP | Mode: $MODE | Ref: ${REF:-<current>}"
  log "Env file: $ENV_FILE"
  log "Git SHA: $(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
}

ensure_clean_worktree_if_checkout() {
  if [[ -n "$REF" ]]; then
    if [[ -n "$(git status --porcelain)" ]]; then
      die "Working tree is not clean. Commit/stash changes before using --ref."
    fi
  fi
}

checkout_ref_if_needed() {
  if [[ -z "$REF" ]]; then
    return
  fi

  ensure_clean_worktree_if_checkout
  log "Fetching git refs..."
  git fetch --all --prune
  log "Checking out ref: $REF"
  git checkout "$REF"
}

preflight() {
  require_cmd git
  require_cmd docker
  require_cmd curl
  require_cmd grep
  require_cmd tar
  require_cmd date

  require_file "$ENV_FILE"

  run_compose config --services >/dev/null

  require_env_key BOT_TOKEN
  require_env_key BASE_URL
  require_env_key POSTGRES_DB
  require_env_key POSTGRES_USER
  require_env_key POSTGRES_PASSWORD
  require_env_key DATABASE_URL
  require_env_key REDIS_URL

  show_context
  log "Compose services:"
  run_compose config --services
  log "Current compose status:"
  run_compose ps || true
}

init_backup_dir() {
  local ts
  ts="$(date +%Y%m%d_%H%M%S)"
  if [[ -n "$BACKUP_DIR" ]]; then
    mkdir -p "$BACKUP_DIR"
    return
  fi
  mkdir -p "$BACKUP_ROOT"
  BACKUP_DIR="${BACKUP_ROOT}/${PROJECT_NAME}_${ts}"
  mkdir -p "$BACKUP_DIR"
}

backup_now() {
  init_backup_dir

  local pg_user pg_db sql_dump
  pg_user="$(get_env_value POSTGRES_USER)"
  pg_db="$(get_env_value POSTGRES_DB)"

  log "Ensuring postgres/redis are running for backup..."
  run_compose up -d postgres redis

  sql_dump="${BACKUP_DIR}/db_$(date +%Y%m%d_%H%M%S).sql"
  log "Creating SQL dump: $sql_dump"
  run_compose exec -T postgres pg_dump -U "$pg_user" -d "$pg_db" > "$sql_dump"

  for logical_volume in postgres_data tutor_data; do
    local actual_volume archive_path
    actual_volume="${PROJECT_NAME}_${logical_volume}"
    archive_path="${BACKUP_DIR}/${logical_volume}_$(date +%Y%m%d_%H%M%S).tar.gz"

    if ! docker volume inspect "$actual_volume" >/dev/null 2>&1; then
      warn "Volume not found, skipping: $actual_volume"
      continue
    fi

    log "Backing up volume $actual_volume -> $archive_path"
    docker run --rm \
      -v "${actual_volume}:/src:ro" \
      -v "${BACKUP_DIR}:/dst" \
      alpine sh -c "tar -czf /dst/$(basename "$archive_path") -C /src ."
  done

  {
    echo "timestamp=$(date -Iseconds)"
    echo "project_dir=${PROJECT_DIR}"
    echo "project_name=${PROJECT_NAME}"
    echo "git_sha=$(git rev-parse HEAD 2>/dev/null || true)"
    echo "env_file=${ENV_FILE}"
    echo "mode=${MODE}"
    echo "sql_dump=${sql_dump}"
  } > "${BACKUP_DIR}/manifest.txt"

  log "Backup completed: $BACKUP_DIR"
}

deploy_stack() {
  checkout_ref_if_needed

  if [[ "$SKIP_BUILD" -eq 0 ]]; then
    log "Building images..."
    run_compose build backend worker
    if [[ "$WITH_BOT" -eq 1 ]]; then
      run_compose build bot
    fi
  else
    warn "Skipping image build (--skip-build)."
  fi

  if [[ "$MODE" == "reset" ]]; then
    log "RESET mode: dropping compose volumes (down -v)."
    run_compose down -v
  fi

  log "Starting core infra (postgres, redis)..."
  run_compose up -d postgres redis

  if [[ "$SKIP_MIGRATIONS" -eq 0 ]]; then
    log "Applying migrations (alembic upgrade head)..."
    run_compose run --rm backend alembic -c /app/alembic.ini upgrade head
  else
    warn "Skipping migrations (--skip-migrations)."
  fi

  log "Starting backend and worker..."
  run_compose up -d backend worker

  if [[ "$WITH_BOT" -eq 1 ]]; then
    log "Starting bot (polling profile)..."
    run_compose --profile polling up -d bot
  fi

  run_compose ps
}

verify_stack() {
  local host_port pg_user pg_db ops_token
  host_port="$(env_or_default HOST_PORT 8000)"
  pg_user="$(get_env_value POSTGRES_USER)"
  pg_db="$(get_env_value POSTGRES_DB)"
  ops_token="$(get_env_value OPS_API_TOKEN)"

  log "Running verify checks..."
  run_compose ps
  run_compose run --rm backend alembic -c /app/alembic.ini current
  run_compose run --rm backend alembic -c /app/alembic.ini check
  run_compose exec postgres psql -U "$pg_user" -d "$pg_db" -c "select version_num from alembic_version;"

  log "Checking health endpoint..."
  curl -fsS "http://localhost:${host_port}/health"
  printf "\n"

  log "Checking worker metrics endpoint..."
  if [[ -n "$ops_token" ]]; then
    curl -fsS -H "X-Ops-Token: ${ops_token}" "http://localhost:${host_port}/metrics/worker" >/dev/null
  else
    curl -fsS "http://localhost:${host_port}/metrics/worker" >/dev/null
  fi

  log "Verify completed successfully."
}

rollback_help() {
  local target_ref
  local pg_user
  local pg_db
  target_ref="${REF:-<previous_ref>}"
  pg_user="$(get_env_value POSTGRES_USER)"
  pg_db="$(get_env_value POSTGRES_DB)"
  if [[ -z "$pg_user" ]]; then
    pg_user="<postgres_user>"
  fi
  if [[ -z "$pg_db" ]]; then
    pg_db="<postgres_db>"
  fi

  cat <<EOF_HELP
Rollback helper (manual):

1) Switch to previous known-good ref:
   git fetch --all --prune
   git checkout ${target_ref}

2) Recreate stack:
   docker compose --env-file ${ENV_FILE} down
   docker compose --env-file ${ENV_FILE} up -d postgres redis
   docker compose --env-file ${ENV_FILE} run --rm backend alembic -c /app/alembic.ini upgrade head
   docker compose --env-file ${ENV_FILE} up -d backend worker

3) Optional DB restore from SQL backup:
   cat <backup.sql> | docker compose --env-file ${ENV_FILE} exec -T postgres psql -U ${pg_user} -d ${pg_db}

4) Optional volume restore:
   docker compose --env-file ${ENV_FILE} down -v
   docker volume create ${PROJECT_NAME}_postgres_data
   docker run --rm -v ${PROJECT_NAME}_postgres_data:/dst -v <backup_dir>:/src alpine \
     sh -c "cd /dst && tar xzf /src/postgres_data_<timestamp>.tar.gz"

EOF_HELP

  if [[ -n "$BACKUP_DIR" ]]; then
    log "Selected backup directory: $BACKUP_DIR"
  else
    log "Tip: pass --backup-dir <path> to keep rollback context in command output."
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --step)
        need_value "$1" "$@"
        STEP="$2"
        shift 2
        ;;
      --mode)
        need_value "$1" "$@"
        MODE="$2"
        shift 2
        ;;
      --confirm-reset)
        need_value "$1" "$@"
        CONFIRM_RESET="$2"
        shift 2
        ;;
      --ref)
        need_value "$1" "$@"
        REF="$2"
        shift 2
        ;;
      --with-bot)
        WITH_BOT=1
        shift
        ;;
      --backup-root)
        need_value "$1" "$@"
        BACKUP_ROOT="$2"
        shift 2
        ;;
      --backup-dir)
        need_value "$1" "$@"
        BACKUP_DIR="$2"
        shift 2
        ;;
      --env-file)
        need_value "$1" "$@"
        ENV_FILE="$2"
        shift 2
        ;;
      --project-name)
        need_value "$1" "$@"
        PROJECT_NAME="$2"
        shift 2
        ;;
      --skip-build)
        SKIP_BUILD=1
        shift
        ;;
      --skip-migrations)
        SKIP_MIGRATIONS=1
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Unknown argument: $1"
        ;;
    esac
  done
}

main() {
  parse_args "$@"
  validate_options

  case "$STEP" in
    preflight)
      preflight
      ;;
    backup)
      preflight
      backup_now
      ;;
    deploy)
      preflight
      deploy_stack
      ;;
    verify)
      preflight
      verify_stack
      ;;
    all)
      preflight
      backup_now
      deploy_stack
      verify_stack
      ;;
    rollback-help)
      rollback_help
      ;;
    *)
      die "Unhandled step: $STEP"
      ;;
  esac
}

main "$@"
