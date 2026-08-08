#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
BOOT_ART="$PROJECT_DIR/assets/boot-art.txt"
BOOT_ART_RENDERER="$PROJECT_DIR/assets/render-boot-art.pl"
BOOT_SHOWN=0

run_tui() {

  if [[ ! -t 0 || ! -t 1 ]]; then
    printf 'KADATH needs an interactive terminal.\n' >&2
    exit 1
  fi

  RESET=$'\033[0m'; BOLD=$'\033[1m'
  CYAN=$'\033[38;5;51m'; MAGENTA=$'\033[38;5;213m'
  GREEN=$'\033[38;5;84m'; RED=$'\033[38;5;203m'
  WHITE=$'\033[38;5;255m'; MUTED=$'\033[38;5;245m'; PANEL=$'\033[48;5;236m'
  CONFIG_FILE="${KADATH_ENV_FILE:-$PROJECT_DIR/.kadath/config.env}"
  RUNTIME_STATE="${KADATH_RUNTIME_STATE:-$(dirname "$CONFIG_FILE")/runtime-installed}"
  SELECTED=""; KEY=""; model="gpt-5.2"

  cleanup() {
    printf '%s\033[?25h' "$RESET"
  }
  interrupted() {
    cleanup
    printf '\nKADATH closed.\n'
    exit 130
  }
  trap cleanup EXIT
  trap interrupted INT TERM

  clear_screen() { printf '\033[2J\033[H'; }

  brand() {
    printf '%s%s' "$BOLD" "$CYAN"
    printf '  ██╗  ██╗ █████╗ ██████╗  █████╗ ████████╗██╗  ██╗\n'
    printf '  ██║ ██╔╝██╔══██╗██╔══██╗██╔══██╗╚══██╔══╝██║  ██║\n'
    printf '%s' "$MAGENTA"
    printf '  █████╔╝ ███████║██║  ██║███████║   ██║   ███████║\n'
    printf '  ██╔═██╗ ██╔══██║██║  ██║██╔══██║   ██║   ██╔══██║\n'
    printf '  ██║  ██╗██║  ██║██████╔╝██║  ██║   ██║   ██║  ██║\n'
    printf '  ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝\n'
    printf '%s' "$RESET"
    printf '  %sKernel for Agentic Darwinian Adaptation, Tooling, and Heredity%s\n' "$MUTED" "$RESET"
    printf '  %sMade by i3t4an%s  %s·%s  Credit to Hugging Face for smolagents\n' "$WHITE" "$RESET" "$MAGENTA" "$RESET"
    printf '\n  %s%s LIVE CONTROL %s  Isolated evolutionary runtime\n\n' "$PANEL" "$GREEN" "$RESET"
  }

  header() {
    local step="$1" title="$2" subtitle="$3"
    clear_screen; brand
    printf '  %s%s%s  %s%s%s\n' "$MAGENTA" "$BOLD" "$step" "$WHITE" "$BOLD" "$title"
    printf '  %s%s%s\n\n' "$MUTED" "$subtitle" "$RESET"
  }

  footer() {
    printf '\n  %s↑/↓ navigate   Enter select   Ctrl-C exit%s\n' "$MUTED" "$RESET"
  }

  read_key() {
    local tail=""
    KEY=""
    IFS= read -r -s -n 1 KEY
    if [[ "$KEY" == $'\033' ]]; then
      IFS= read -r -s -n 2 -t 1 tail || true
      KEY+="$tail"
    fi
  }

  select_option() {
    local step="$1" title="$2" subtitle="$3" index="$4"; shift 4
    local options=("$@") i
    if (( index < 0 || index >= ${#options[@]} )); then index=0; fi
    printf '\033[?25l'
    while true; do
      header "$step" "$title" "$subtitle"
      for ((i=0; i<${#options[@]}; i++)); do
        if (( i == index )); then
          printf '  %s%s  ›  %-52s%s\n' "$PANEL" "$CYAN" "${options[$i]}" "$RESET"
        else
          printf '       %s%-52s%s\n' "$WHITE" "${options[$i]}" "$RESET"
        fi
      done
      footer
      read_key
      case "$KEY" in
        $'\033[A'|$'\033OA') index=$(( (index - 1 + ${#options[@]}) % ${#options[@]} )) ;;
        $'\033[B'|$'\033OB') index=$(( (index + 1) % ${#options[@]} )) ;;
        '') SELECTED="${options[$index]}"; printf '\033[?25h'; return ;;
      esac
    done
  }

  prompt_value() {
    local step="$1" title="$2" subtitle="$3" prompt="$4" default_value="${5:-}" value
    while true; do
      header "$step" "$title" "$subtitle"
      printf '  %s%s%s\n\n' "$WHITE" "$prompt" "$RESET"
      if [[ -n "$default_value" ]]; then printf '  %sDefault: %s%s\n\n' "$MUTED" "$default_value" "$RESET"; fi
      printf '  %s› %s' "$CYAN" "$RESET"
      IFS= read -r value
      value="${value:-$default_value}"
      if [[ -n "${value//[[:space:]]/}" ]]; then SELECTED="$value"; return; fi
      printf '  %sA value is required. Press any key to retry.%s' "$RED" "$RESET"
      IFS= read -rsn1 _retry
    done
  }

  prompt_value_with_back() {
    local step="$1" title="$2" subtitle="$3" prompt="$4" default_value="${5:-}"
    local value="" index=0 shown i
    printf '\033[?25l'
    while true; do
      header "$step" "$title" "$subtitle"
      printf '  %s%s%s\n\n' "$WHITE" "$prompt" "$RESET"
      if [[ -n "$default_value" ]]; then printf '  %sDefault: %s%s\n\n' "$MUTED" "$default_value" "$RESET"; fi
      shown="${value:-$default_value}"
      if ((index == 0)); then
        printf '  %s%s  ›  %-52s%s\n' "$PANEL" "$CYAN" "$shown" "$RESET"
        printf '       %s%-52s%s\n' "$WHITE" "← Back" "$RESET"
      else
        printf '       %s%-52s%s\n' "$WHITE" "$shown" "$RESET"
        printf '  %s%s  ›  %-52s%s\n' "$PANEL" "$CYAN" "← Back" "$RESET"
      fi
      footer
      read_key
      case "$KEY" in
        $'\033[A'|$'\033OA'|$'\033[B'|$'\033OB') index=$((1 - index)) ;;
        $'\033') SELECTED="← Back"; printf '\033[?25h'; return ;;
        '')
          if ((index == 1)); then
            SELECTED="← Back"; printf '\033[?25h'; return
          fi
          value="${value:-$default_value}"
          if [[ -n "${value//[[:space:]]/}" ]]; then SELECTED="$value"; printf '\033[?25h'; return; fi
          ;;
        $'\177'|$'\b')
          if ((index == 0)) && [[ -n "$value" ]]; then value="${value%?}"; fi
          ;;
        *)
          if ((index == 0)) && ((${#KEY} == 1)); then value+="$KEY"; fi
          ;;
      esac
    done
  }

  prompt_secret() {
    local step="$1" title="$2" subtitle="$3" prompt="$4" value
    while true; do
      header "$step" "$title" "$subtitle"
      printf '  %s%s%s\n\n' "$WHITE" "$prompt" "$RESET"
      printf '  %s› %s' "$CYAN" "$RESET"
      IFS= read -r -s value
      printf '\n'
      if [[ -n "$value" ]]; then SELECTED="$value"; return; fi
      printf '  %sA value is required. Press any key to retry.%s' "$RED" "$RESET"
      IFS= read -r -s -n 1 _retry
    done
  }

  validation_error() {
    printf '\n  %s%s Press any key to retry.%s' "$RED" "$1" "$RESET"
    IFS= read -r -s -n 1 _retry
  }

  configured_value() {
    local key="$1"
    sed -n "s/^${key}=//p" "$CONFIG_FILE" 2>/dev/null | tail -n 1
  }

  configuration_ready() {
    [[ -f "$CONFIG_FILE" ]] || return 1
    local key
    for key in OPENAI_API_KEY KADATH_UPSTREAM_MODEL KADATH_POSTGRES_PASSWORD KADATH_MINIO_USER KADATH_MINIO_PASSWORD LITELLM_MASTER_KEY SEARXNG_SECRET_KEY; do
      [[ -n "$(configured_value "$key")" ]] || return 1
    done
  }

  safe_value() { [[ "$1" =~ ^[A-Za-z0-9_./:@+=,-]+$ ]]; }

  random_secret() {
    if command -v openssl >/dev/null 2>&1; then openssl rand -hex 24
    else uuidgen | tr -d '-'; fi
  }

  write_configuration() {
    mkdir -p "$(dirname "$CONFIG_FILE")"
    umask 077
    local temporary_config="${CONFIG_FILE}.tmp.$$"
    {
      printf 'OPENAI_API_KEY=%s\n' "$openai_key"
      printf 'KADATH_UPSTREAM_MODEL=%s\n' "$model"
      printf 'KADATH_MODEL=kadath-default\n'
      printf 'KADATH_POSTGRES_PASSWORD=%s\n' "$(random_secret)"
      printf 'KADATH_MINIO_USER=kadath\n'
      printf 'KADATH_MINIO_PASSWORD=%s\n' "$(random_secret)"
      printf 'LITELLM_MASTER_KEY=%s\n' "$(random_secret)"
      printf 'SEARXNG_SECRET_KEY=%s\n' "$(random_secret)"
      printf 'KADATH_MODEL_GLOBAL_CONCURRENCY=64\n'
      printf 'KADATH_GRADER_CHUNK_TOKENS=12000\n'
      printf 'KADATH_WORKER_GLOBAL_LIMIT=500\n'
      printf 'KADATH_BROWSER_FLEET=1\n'
      printf 'KADATH_DOCKER_SOCKET=/var/run/docker.sock\n'
    } > "$temporary_config"
    mv "$temporary_config" "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
  }

  update_configuration_value() {
    local key="$1" value="$2" temporary_config="${CONFIG_FILE}.tmp.$$"
    awk -v key="$key" -v value="$value" '
      BEGIN { found = 0 }
      index($0, key "=") == 1 { print key "=" value; found = 1; next }
      { print }
      END { if (!found) print key "=" value }
    ' "$CONFIG_FILE" > "$temporary_config"
    mv "$temporary_config" "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
  }

  check_live_requirements() {
    command -v docker >/dev/null 2>&1 || { printf 'KADATH needs Docker Desktop or Docker Engine.\n' >&2; exit 1; }
    docker info >/dev/null 2>&1 || { printf 'Docker is installed but is not running.\n' >&2; exit 1; }
    docker compose version >/dev/null 2>&1 || { printf 'KADATH needs the Docker Compose plugin.\n' >&2; exit 1; }
  }

  render_boot_art() {
    if command -v perl >/dev/null 2>&1 && [[ -f "$BOOT_ART_RENDERER" ]]; then
      LC_ALL=C perl -CSD "$BOOT_ART_RENDERER" "$BOOT_ART"
    else
      printf '%s' "$CYAN"
      command cat "$BOOT_ART"
    fi
  }

  show_boot_art() {
    clear_screen
    printf '\033[?25l'
    if [[ -f "$BOOT_ART" ]]; then
      render_boot_art
    else
      printf '%sKADATH\n' "$CYAN"
    fi
    printf '%s\n' "$RESET"
  }

  draw_boot_progress() {
    local percent="$1" label="$2" width=50 filled empty i
    filled=$((percent * width / 100)); empty=$((width - filled))
    printf '\r\033[2K  %s' "$GREEN"
    for ((i=0; i<filled; i++)); do printf '█'; done
    printf '%s' "$MUTED"
    for ((i=0; i<empty; i++)); do printf '░'; done
    printf '%s  %3d%%  %s' "$RESET" "$percent" "$label"
  }

  quick_boot() {
    local percent
    show_boot_art
    for ((percent=0; percent<=100; percent+=5)); do
      draw_boot_progress "$percent" "Starting KADATH"
      sleep 0.06
    done
    printf '\n'
    sleep 0.25
    printf '\033[?25h'
  }

  first_time_credentials() {
    prompt_secret "01 / 05" "First-time setup required" "Your API key is stored locally with owner-only permissions." "OpenAI API key"
    openai_key="$SELECTED"
    prompt_value "01 / 05" "Model selection" "This model will drive the Architect, organisms, and specialists." "OpenAI model ID" "$model"
    model="$SELECTED"
    if ! safe_value "$openai_key" || ! safe_value "$model"; then
      printf 'Invalid API key or model ID.\n' >&2
      exit 1
    fi
    model="${model#openai/}"
    write_configuration
    KADATH_FRONTEND_ACTIVE=1 KADATH_SKIP_BOOT=1 "$PROJECT_DIR/kadath.sh" --prepare-runtime
  }

  edit_credentials() {
    while true; do
      select_option "01 / 05" "Saved credentials" "Existing values stay in place unless you choose one to replace." 0 \
        "Done editing" "Replace OpenAI API key" "Change selected model"
      case "$SELECTED" in
        "Done editing") return ;;
        "Replace OpenAI API key")
          prompt_secret "01 / 05" "Replace API key" "The current key remains active until the replacement is saved." "New OpenAI API key"
          safe_value "$SELECTED" || { validation_error "Invalid OpenAI API key."; continue; }
          update_configuration_value OPENAI_API_KEY "$SELECTED"
          ;;
        "Change selected model")
          prompt_value "01 / 05" "Change model" "The API key and local credentials remain unchanged." "OpenAI model ID" "$model"
          model="${SELECTED#openai/}"
          safe_value "$model" || { validation_error "Invalid OpenAI model ID."; continue; }
          update_configuration_value KADATH_UPSTREAM_MODEL "$model"
          ;;
      esac
    done
  }

  delete_old_runs() {
    select_option "01 / 05" "Delete old runs" "Active runs are protected. Choose how much finished history to remove." 0 \
      "Delete finished runs older than 30 days" "Delete all finished runs" "Back"
    [[ "$SELECTED" == "Back" ]] && return
    local cleanup_scope="$SELECTED"
    select_option "01 / 05" "Confirm cleanup" "$cleanup_scope. This also removes their stored artifacts; verified exports remain." 0 \
      "Cancel" "Confirm deletion"
    if [[ "$SELECTED" == "Confirm deletion" ]]; then
      local cleanup_output cleanup_status=0
      if [[ "$cleanup_scope" == "Delete all finished runs" ]]; then
        cleanup_output="$(KADATH_FRONTEND_ACTIVE=1 KADATH_SKIP_BOOT=1 "$PROJECT_DIR/kadath.sh" cleanup --all 2>&1)" || cleanup_status=$?
      else
        cleanup_output="$(KADATH_FRONTEND_ACTIVE=1 KADATH_SKIP_BOOT=1 "$PROJECT_DIR/kadath.sh" cleanup --older-than-days 30 2>&1)" || cleanup_status=$?
      fi
      if ((cleanup_status != 0)); then
        header "01 / 05" "Cleanup failed" "No active run was intentionally selected for deletion."
        printf '  %s%s%s\n\n' "$RED" "$cleanup_output" "$RESET"
      else
        header "01 / 05" "Cleanup complete" "Finished run data matching the selected age was removed."
        printf '  %s✓%s %s\n\n' "$GREEN" "$RESET" "$cleanup_scope"
        printf '  %sActive runs and verified exports were preserved.%s\n\n' "$MUTED" "$RESET"
      fi
      printf '  Press any key to return to the main menu.'
      IFS= read -r -s -n 1 _continue
    fi
  }

  check_live_requirements
  if configuration_ready; then
    model="$(configured_value KADATH_UPSTREAM_MODEL)"
    if [[ -f "$RUNTIME_STATE" ]]; then
      quick_boot
    else
      KADATH_FRONTEND_ACTIVE=1 KADATH_SKIP_BOOT=1 "$PROJECT_DIR/kadath.sh" --prepare-runtime
    fi
  else
    first_time_credentials
  fi
  setup_stage="menu"
  while [[ "$setup_stage" != "done" ]]; do
    case "$setup_stage" in
      menu)
        select_option "01 / 05" "Welcome" "Your saved credentials and local runtime are ready." 0 \
          "Start a new run" "Edit saved credentials" "Delete old runs" "Exit KADATH"
        case "$SELECTED" in
          "Start a new run") setup_stage="goal" ;;
          "Edit saved credentials") edit_credentials ;;
          "Delete old runs") delete_old_runs ;;
          "Exit KADATH") clear_screen; printf 'KADATH closed.\n'; exit 0 ;;
        esac
        ;;
      goal)
        prompt_value_with_back "02 / 05" "Define the objective" "Describe the outcome the population should maximize." "Goal"
        if [[ "$SELECTED" == "← Back" ]]; then setup_stage="menu"
        else goal="$SELECTED"; setup_stage="duration"; fi
        ;;
      duration)
        select_option "03 / 05" "Epoch duration" "How long each agent gets before grading and reflection." 2 \
          "5 minutes" "15 minutes" "30 minutes" "1 hour" "Custom duration" "← Back"
        case "$SELECTED" in
          "← Back") setup_stage="goal" ;;
          "Custom duration") setup_stage="custom-duration" ;;
          *) duration="$SELECTED"; setup_stage="population" ;;
        esac
        ;;
      custom-duration)
        prompt_value_with_back "03 / 05" "Custom epoch duration" "Use a value such as 90s, 45m, or 2h." "Duration" "30m"
        if [[ "$SELECTED" == "← Back" ]]; then
          setup_stage="duration"
        elif [[ "$SELECTED" =~ ^[1-9][0-9]*[smh]$ ]]; then
          duration="$SELECTED"; setup_stage="population"
        else
          validation_error "Use a positive duration ending in s, m, or h."
        fi
        ;;
      population)
        select_option "03 / 05" "Population size" "Every agent receives an isolated runtime and its own evolving genome." 3 \
          "10 agents" "30 agents" "50 agents" "100 agents" "Custom population" "← Back"
        case "$SELECTED" in
          "← Back") setup_stage="duration" ;;
          "Custom population") setup_stage="custom-population" ;;
          *) population="$SELECTED"; setup_stage="epochs" ;;
        esac
        ;;
      custom-population)
        prompt_value_with_back "03 / 05" "Custom population" "Use at least four agents." "Number of agents" "100"
        if [[ "$SELECTED" == "← Back" ]]; then
          setup_stage="population"
        elif [[ "$SELECTED" =~ ^[0-9]+$ ]] && (( 10#$SELECTED >= 4 )); then
          population="$((10#$SELECTED)) agents"; setup_stage="epochs"
        else
          validation_error "Population must be an integer of at least four."
        fi
        ;;
      epochs)
        select_option "03 / 05" "Epoch count" "The final epoch is graded without another cull or reproduction cycle." 1 \
          "1 epoch" "3 epochs" "5 epochs" "10 epochs" "Custom epoch count" "← Back"
        case "$SELECTED" in
          "← Back") setup_stage="population" ;;
          "Custom epoch count") setup_stage="custom-epochs" ;;
          *) epochs="$SELECTED"; setup_stage="done" ;;
        esac
        ;;
      custom-epochs)
        prompt_value_with_back "03 / 05" "Custom epoch count" "Choose one or more epochs." "Number of epochs" "3"
        if [[ "$SELECTED" == "← Back" ]]; then
          setup_stage="epochs"
        elif [[ "$SELECTED" =~ ^[0-9]+$ ]] && (( 10#$SELECTED >= 1 )); then
          epochs="$((10#$SELECTED)) epochs"; setup_stage="done"
        else
          validation_error "Epoch count must be a positive integer."
        fi
        ;;
    esac
  done

  case "$duration" in
    "5 minutes") epoch_seconds=300 ;;
    "15 minutes") epoch_seconds=900 ;;
    "30 minutes") epoch_seconds=1800 ;;
    "1 hour") epoch_seconds=3600 ;;
    *)
      amount="${duration%[smh]}"; unit="${duration: -1}"
      case "$unit" in
        s) epoch_seconds="$amount" ;;
        m) epoch_seconds=$((amount * 60)) ;;
        h) epoch_seconds=$((amount * 3600)) ;;
      esac
      ;;
  esac
  population_count="${population%% *}"
  epoch_count="${epochs%% *}"
  clear_screen
  printf '%sLaunching the Architect for the real approval contract...%s\n\n' "$CYAN" "$RESET"
  exec env KADATH_FRONTEND_ACTIVE=1 KADATH_SKIP_BOOT=1 \
    "$PROJECT_DIR/kadath.sh" start \
      --goal "$goal" \
      --epochs "$epoch_count" \
      --population "$population_count" \
      --epoch-seconds "$epoch_seconds" \
      --dashboard
}

if [[ $# -eq 0 && -t 0 && -t 1 && "${KADATH_FRONTEND_ACTIVE:-0}" != 1 ]]; then
  run_tui
  exit 0
fi

render_boot_art() {
  if command -v perl >/dev/null 2>&1 && [[ -f "$BOOT_ART_RENDERER" ]]; then
    LC_ALL=C perl -CSD "$BOOT_ART_RENDERER" "$BOOT_ART"
  else
    printf '\033[38;5;51m'
    command cat "$BOOT_ART"
  fi
}

show_boot_art() {
  [[ -t 1 ]] || return 0
  printf '\033[2J\033[H\033[?25l'
  if [[ -f "$BOOT_ART" ]]; then render_boot_art; else printf '\033[38;5;51mKADATH\n'; fi
  printf '\033[0m\n'
}

draw_boot_progress() {
  local percent="$1" label="$2" width=50 filled empty i
  [[ -t 1 ]] || return 0
  filled=$((percent * width / 100)); empty=$((width - filled))
  printf '\r\033[2K  \033[38;5;84m'
  for ((i=0; i<filled; i++)); do printf '█'; done
  printf '\033[38;5;245m'
  for ((i=0; i<empty; i++)); do printf '░'; done
  printf '\033[0m  %3d%%  %s' "$percent" "$label"
}

quick_boot() {
  local percent
  [[ -t 1 ]] || return 0
  show_boot_art
  for ((percent=0; percent<=100; percent+=5)); do
    draw_boot_progress "$percent" "Starting KADATH"
    sleep 0.06
  done
  printf '\n'
  sleep 0.25
  printf '\033[?25h'
}

if [[ "${KADATH_FRONTEND_ACTIVE:-0}" != 1 ]]; then
  printf '\n'
  printf '  KADATH\n'
  printf '  Kernel for Agentic Darwinian Adaptation, Tooling, and Heredity\n'
  printf '  Made by i3t4an\n'
  printf '  Credit to Hugging Face for smolagents.\n'
  printf '\n'
fi

if ! command -v docker >/dev/null 2>&1; then
  printf 'KADATH needs Docker Desktop or Docker Engine.\n' >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  printf 'Docker is installed but is not running. Start Docker and run ./kadath.sh again.\n' >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  printf 'KADATH needs the Docker Compose plugin.\n' >&2
  exit 1
fi

CONFIG_FILE="${KADATH_ENV_FILE:-$PROJECT_DIR/.kadath/config.env}"
RUNTIME_STATE="${KADATH_RUNTIME_STATE:-$(dirname "$CONFIG_FILE")/runtime-installed}"
FIRST_BOOT=0

configured_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "$CONFIG_FILE" 2>/dev/null | tail -n 1
}

configuration_ready() {
  [[ -f "$CONFIG_FILE" ]] || return 1
  local key
  for key in OPENAI_API_KEY KADATH_UPSTREAM_MODEL KADATH_POSTGRES_PASSWORD KADATH_MINIO_USER KADATH_MINIO_PASSWORD LITELLM_MASTER_KEY SEARXNG_SECRET_KEY; do
    [[ -n "$(configured_value "$key")" ]] || return 1
  done
}

safe_value() {
  [[ "$1" =~ ^[A-Za-z0-9_./:@+=,-]+$ ]]
}

required_value() {
  local label="$1" value
  while true; do
    read -r -p "$label: " value
    if [[ -n "$value" ]] && safe_value "$value"; then
      printf '%s' "$value"
      return
    fi
    printf 'Use a non-empty value containing letters, numbers, or . _ / : @ + = , -\n' >&2
  done
}

secret_value() {
  local label="$1" allow_generate="$2" value
  while true; do
    if [[ -t 0 ]]; then
      read -r -s -p "$label" value
      printf '\n' >&2
    else
      read -r value
    fi
    if [[ -z "$value" && "$allow_generate" == "yes" ]]; then
      if command -v openssl >/dev/null 2>&1; then value="$(openssl rand -hex 24)"
      else value="$(uuidgen | tr -d '-')"; fi
    fi
    if [[ -n "$value" ]] && safe_value "$value"; then
      printf '%s' "$value"
      return
    fi
    printf 'Use a non-empty value containing letters, numbers, or . _ / : @ + = , -\n' >&2
  done
}

random_secret() {
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex 24
  else uuidgen | tr -d '-'; fi
}

write_configuration() {
  mkdir -p "$(dirname "$CONFIG_FILE")"
  umask 077
  local temporary_config="${CONFIG_FILE}.tmp.$$"
  {
    printf 'OPENAI_API_KEY=%s\n' "$openai_key"
    printf 'KADATH_UPSTREAM_MODEL=%s\n' "$model"
    printf 'KADATH_MODEL=kadath-default\n'
    printf 'KADATH_POSTGRES_PASSWORD=%s\n' "$postgres_password"
    printf 'KADATH_MINIO_USER=%s\n' "$minio_user"
    printf 'KADATH_MINIO_PASSWORD=%s\n' "$minio_password"
    printf 'LITELLM_MASTER_KEY=%s\n' "$litellm_key"
    printf 'SEARXNG_SECRET_KEY=%s\n' "$searxng_key"
    printf 'KADATH_MODEL_GLOBAL_CONCURRENCY=64\n'
    printf 'KADATH_GRADER_CHUNK_TOKENS=12000\n'
    printf 'KADATH_WORKER_GLOBAL_LIMIT=500\n'
    printf 'KADATH_BROWSER_FLEET=1\n'
    printf 'KADATH_DOCKER_SOCKET=/var/run/docker.sock\n'
  } > "$temporary_config"
  mv "$temporary_config" "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE"
}

update_configuration_value() {
  local key="$1" value="$2" temporary_config="${CONFIG_FILE}.tmp.$$"
  awk -v key="$key" -v value="$value" '
    BEGIN { found = 0 }
    index($0, key "=") == 1 { print key "=" value; found = 1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$CONFIG_FILE" > "$temporary_config"
  mv "$temporary_config" "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE"
}

if ! configuration_ready; then
  FIRST_BOOT=1
  printf 'First-boot setup\n'
  printf 'Secrets are stored locally in %s with owner-only permissions.\n\n' "$CONFIG_FILE"
  openai_key="$(secret_value 'OpenAI API key: ' no)"
  model="$(required_value 'OpenAI model ID (for example, gpt-4.1)')"
  model="${model#openai/}"
  if [[ -z "$model" ]]; then printf 'The OpenAI model ID cannot be blank.\n' >&2; exit 1; fi
  postgres_password="$(random_secret)"; minio_user="kadath"; minio_password="$(random_secret)"
  litellm_key="$(random_secret)"; searxng_key="$(random_secret)"
  write_configuration
  printf '\nLocal service credentials generated automatically. Setup complete.\n\n'
elif [[ $# -eq 0 ]]; then
  quick_boot
  BOOT_SHOWN=1
  printf 'Saved API key and model found.\n'
  read -r -p 'Edit them before starting? [y/N] ' edit_saved
  if [[ "$edit_saved" =~ ^[Yy]([Ee][Ss])?$ ]]; then
    openai_key="$(configured_value OPENAI_API_KEY)"
    if [[ -t 0 ]]; then
      read -r -s -p 'New OpenAI API key [Enter to keep current]: ' replacement_key
      printf '\n' >&2
    else
      read -r replacement_key
    fi
    if [[ -n "$replacement_key" ]]; then
      if ! safe_value "$replacement_key"; then printf 'Invalid OpenAI API key.\n' >&2; exit 1; fi
      openai_key="$replacement_key"
    fi
    current_model="$(configured_value KADATH_UPSTREAM_MODEL)"
    read -r -p "OpenAI model ID [$current_model]: " model
    model="${model:-$current_model}"; model="${model#openai/}"
    if [[ -z "$model" ]] || ! safe_value "$model"; then printf 'Invalid OpenAI model ID.\n' >&2; exit 1; fi
    update_configuration_value OPENAI_API_KEY "$openai_key"
    update_configuration_value KADATH_UPSTREAM_MODEL "$model"
    printf 'Saved API key and model updated.\n\n'
  fi
fi

if [[ "$FIRST_BOOT" == 0 && "$BOOT_SHOWN" == 0 && "${KADATH_SKIP_BOOT:-0}" != 1 ]]; then
  quick_boot
  BOOT_SHOWN=1
fi

compose() {
  docker compose --env-file "$CONFIG_FILE" "$@"
}

RUNTIME_PREPARED=0
prepare_runtime() {
  if [[ "$RUNTIME_PREPARED" == 1 ]]; then return; fi
  if [[ -f "$RUNTIME_STATE" ]]; then
    compose --profile browser up -d postgres minio searxng litellm playwright-mcp
    RUNTIME_PREPARED=1
    return
  fi

  if [[ -t 1 ]]; then
    local runtime_log runtime_pid percent=0 status frame=0
    local dots=("" "." ".." "...")
    runtime_log="$(mktemp "${TMPDIR:-/tmp}/kadath-install.XXXXXX")"
    show_boot_art
    (compose --profile browser build control organism-worker && \
      compose --profile browser up -d postgres minio searxng litellm playwright-mcp) >"$runtime_log" 2>&1 &
    runtime_pid=$!
    trap 'kill "$runtime_pid" 2>/dev/null || true; wait "$runtime_pid" 2>/dev/null || true; rm -f "$runtime_log"; printf "\033[0m\033[?25h\n"; exit 130' INT TERM
    while kill -0 "$runtime_pid" 2>/dev/null; do
      draw_boot_progress "$percent" "Installing required packages${dots[$((frame % 4))]}"
      if ((percent < 80)); then percent=$((percent + 2))
      elif ((percent < 94)); then percent=$((percent + 1)); fi
      frame=$((frame + 1))
      sleep 0.15
    done
    if wait "$runtime_pid"; then status=0; else status=$?; fi
    trap - INT TERM
    if ((status != 0)); then
      printf '\033[0m\033[?25h\n\nInstallation failed:\n' >&2
      command cat "$runtime_log" >&2
      rm -f "$runtime_log"
      return "$status"
    fi
    draw_boot_progress 100 "Runtime ready"
    printf '\n\033[?25h'
    sleep 0.4
    rm -f "$runtime_log"
  else
    printf '\nInstalling and validating the KADATH runtime...\n'
    compose --profile browser build control organism-worker
    compose --profile browser up -d postgres minio searxng litellm playwright-mcp
  fi
  mkdir -p "$(dirname "$RUNTIME_STATE")"
  printf 'installed\n' > "$RUNTIME_STATE"
  RUNTIME_PREPARED=1
  printf 'KADATH runtime ready. Later launches will reuse Docker\047s cached packages.\n\n'
}

if [[ "$FIRST_BOOT" == 1 ]]; then
  prepare_runtime
fi

if [[ $# -eq 1 && "$1" == "--prepare-runtime" ]]; then
  prepare_runtime
  exit 0
fi

if [[ $# -gt 0 ]]; then
  prepare_runtime
  compose --profile control run --rm --use-aliases control "$@"
  exit $?
fi

while true; do
  read -r -p 'Goal: ' goal
  [[ -n "${goal//[[:space:]]/}" ]] && break
  printf 'The goal cannot be blank.\n' >&2
done

while true; do
  read -r -p 'Epoch duration [30m] (s/m/h): ' duration
  duration="${duration:-30m}"
  if [[ "$duration" =~ ^([1-9][0-9]*)([smh])$ ]]; then
    amount="${BASH_REMATCH[1]}"; unit="${BASH_REMATCH[2]}"
    case "$unit" in s) epoch_seconds="$amount";; m) epoch_seconds=$((amount * 60));; h) epoch_seconds=$((amount * 3600));; esac
    break
  fi
  printf 'Use a positive duration such as 90s, 30m, or 2h.\n' >&2
done

while true; do
  read -r -p 'Number of agents [100]: ' population
  population="${population:-100}"
  [[ "$population" =~ ^[0-9]+$ ]] && (( population >= 4 )) && break
  printf 'Agent count must be an integer of at least 4.\n' >&2
done

while true; do
  read -r -p 'Number of epochs [3]: ' epochs
  epochs="${epochs:-3}"
  [[ "$epochs" =~ ^[0-9]+$ ]] && (( epochs >= 1 )) && break
  printf 'Epoch count must be a positive integer.\n' >&2
done

prepare_runtime

compose --profile control run --rm --use-aliases control start \
  --goal "$goal" \
  --epochs "$epochs" \
  --population "$population" \
  --epoch-seconds "$epoch_seconds" \
  --dashboard
