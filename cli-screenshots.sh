#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BOOT_ART="$PROJECT_DIR/assets/boot-art.txt"
BOOT_ART_RENDERER="$PROJECT_DIR/assets/render-boot-art.pl"

RESET=$'\033[0m'; BOLD=$'\033[1m'
CYAN=$'\033[38;5;51m'; MAGENTA=$'\033[38;5;213m'
GREEN=$'\033[38;5;84m'; RED=$'\033[38;5;203m'
WHITE=$'\033[38;5;255m'; MUTED=$'\033[38;5;245m'; PANEL=$'\033[48;5;236m'

GOAL='Launch a niche market research report and maximize independently verified net profit.'
PAGE=1
PAGE_COUNT=21
KEY=''
SELECTION=0

PAGE_NAMES=(
  'first boot installation'
  'first-time API key'
  'model selection'
  'welcome'
  'saved credentials'
  'replace API key'
  'change model'
  'delete old runs'
  'confirm cleanup'
  'cleanup complete'
  'define objective'
  'epoch duration'
  'custom epoch duration'
  'population size'
  'custom population'
  'epoch count'
  'custom epoch count'
  'launching Architect'
  'Architect approval'
  'live evolution dashboard'
  'completed evolution dashboard'
)

cleanup() {
  printf '%s\033[?25h\n' "$RESET"
}

interrupted() {
  cleanup
  exit 130
}

trap cleanup EXIT
trap interrupted INT TERM

clear_screen() {
  printf '\033[2J\033[H'
}

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
  brand
  printf '  %s%s%s  %s%s%s\n' "$MAGENTA" "$BOLD" "$step" "$WHITE" "$BOLD" "$title"
  printf '  %s%s%s\n\n' "$MUTED" "$subtitle" "$RESET"
}

footer() {
  printf '\n  %s↑/↓ navigate   Enter select   Ctrl-C exit%s\n' "$MUTED" "$RESET"
}

option_line() {
  local selected="$1" text="$2"
  if [[ "$selected" == 1 ]]; then
    printf '  %s%s  ›  %-52s%s\n' "$PANEL" "$CYAN" "$text" "$RESET"
  else
    printf '       %s%-52s%s\n' "$WHITE" "$text" "$RESET"
  fi
}

option_at() {
  local index="$1" text="$2"
  if ((SELECTION == index)); then option_line 1 "$text"; else option_line 0 "$text"; fi
}

prompt_value() {
  local step="$1" title="$2" subtitle="$3" prompt="$4" value="$5" default_value="${6:-}"
  header "$step" "$title" "$subtitle"
  printf '  %s%s%s\n\n' "$WHITE" "$prompt" "$RESET"
  if [[ -n "$default_value" ]]; then
    printf '  %sDefault: %s%s\n\n' "$MUTED" "$default_value" "$RESET"
  fi
  printf '  %s› %s%s\n' "$CYAN" "$RESET" "$value"
}

prompt_value_with_back() {
  local step="$1" title="$2" subtitle="$3" prompt="$4" value="$5" default_value="${6:-}"
  header "$step" "$title" "$subtitle"
  printf '  %s%s%s\n\n' "$WHITE" "$prompt" "$RESET"
  if [[ -n "$default_value" ]]; then
    printf '  %sDefault: %s%s\n\n' "$MUTED" "$default_value" "$RESET"
  fi
  option_at 0 "$value"
  option_at 1 '← Back'
  footer
}

render_boot_art() {
  if command -v perl >/dev/null 2>&1 && [[ -f "$BOOT_ART_RENDERER" ]]; then
    LC_ALL=C perl -CSD "$BOOT_ART_RENDERER" "$BOOT_ART"
  elif [[ -f "$BOOT_ART" ]]; then
    printf '%s' "$CYAN"
    command cat "$BOOT_ART"
  else
    printf '%sKADATH\n' "$CYAN"
  fi
  printf '%s\n' "$RESET"
  printf '  %s████████████████████████████████████%s░░░░░░░░░░░░░░%s   72%%  Installing required packages...\n' "$GREEN" "$MUTED" "$RESET"
}

screen_api_key() {
  header '01 / 05' 'First-time setup required' 'Your API key is stored locally with owner-only permissions.'
  printf '  %sOpenAI API key%s\n\n' "$WHITE" "$RESET"
  printf '  %s› %s••••••••••••••••••••••••••••\n' "$CYAN" "$RESET"
}

screen_model() {
  prompt_value '01 / 05' 'Model selection' 'This model will drive the Architect, organisms, and specialists.' 'OpenAI model ID' 'gpt-5.2' 'gpt-5.2'
}

screen_welcome() {
  header '01 / 05' 'Welcome' 'Your saved credentials and local runtime are ready.'
  option_at 0 'Start a new run'
  option_at 1 'Edit saved credentials'
  option_at 2 'Delete old runs'
  option_at 3 'Exit KADATH'
  footer
}

screen_saved_credentials() {
  header '01 / 05' 'Saved credentials' 'Existing values stay in place unless you choose one to replace.'
  option_at 0 'Done editing'
  option_at 1 'Replace OpenAI API key'
  option_at 2 'Change selected model'
  footer
}

screen_replace_key() {
  header '01 / 05' 'Replace API key' 'The current key remains active until the replacement is saved.'
  printf '  %sNew OpenAI API key%s\n\n' "$WHITE" "$RESET"
  printf '  %s› %s••••••••••••••••••••••••••••\n' "$CYAN" "$RESET"
}

screen_change_model() {
  prompt_value '01 / 05' 'Change model' 'The API key and local credentials remain unchanged.' 'OpenAI model ID' 'gpt-5.2' 'gpt-5.2'
}

screen_delete_runs() {
  header '01 / 05' 'Delete old runs' 'Active runs are protected. Choose how much finished history to remove.'
  option_at 0 'Delete finished runs older than 30 days'
  option_at 1 'Delete all finished runs'
  option_at 2 'Back'
  footer
}

screen_confirm_cleanup() {
  header '01 / 05' 'Confirm cleanup' 'Delete finished runs older than 30 days. This also removes their stored artifacts; verified exports remain.'
  option_at 0 'Cancel'
  option_at 1 'Confirm deletion'
  footer
}

screen_cleanup_complete() {
  header '01 / 05' 'Cleanup complete' 'Finished run data matching the selected age was removed.'
  printf '  %s✓%s Delete finished runs older than 30 days\n\n' "$GREEN" "$RESET"
  printf '  %sActive runs and verified exports were preserved.%s\n\n' "$MUTED" "$RESET"
  printf '  Press any key to return to the main menu.\n'
}

screen_goal() {
  prompt_value_with_back '02 / 05' 'Define the objective' 'Describe the outcome the population should maximize.' 'Goal' "$GOAL"
}

screen_duration() {
  header '03 / 05' 'Epoch duration' 'How long each agent gets before grading and reflection.'
  option_at 0 '5 minutes'
  option_at 1 '15 minutes'
  option_at 2 '30 minutes'
  option_at 3 '1 hour'
  option_at 4 'Custom duration'
  option_at 5 '← Back'
  footer
}

screen_custom_duration() {
  prompt_value_with_back '03 / 05' 'Custom epoch duration' 'Use a value such as 90s, 45m, or 2h.' 'Duration' '45m' '30m'
}

screen_population() {
  header '03 / 05' 'Population size' 'Every agent receives an isolated runtime and its own evolving genome.'
  option_at 0 '10 agents'
  option_at 1 '30 agents'
  option_at 2 '50 agents'
  option_at 3 '100 agents'
  option_at 4 'Custom population'
  option_at 5 '← Back'
  footer
}

screen_custom_population() {
  prompt_value_with_back '03 / 05' 'Custom population' 'Use at least four agents.' 'Number of agents' '100' '100'
}

screen_epochs() {
  header '03 / 05' 'Epoch count' 'The final epoch is graded without another cull or reproduction cycle.'
  option_at 0 '1 epoch'
  option_at 1 '3 epochs'
  option_at 2 '5 epochs'
  option_at 3 '10 epochs'
  option_at 4 'Custom epoch count'
  option_at 5 '← Back'
  footer
}

screen_custom_epochs() {
  prompt_value_with_back '03 / 05' 'Custom epoch count' 'Choose one or more epochs.' 'Number of epochs' '4' '3'
}

screen_launching() {
  printf '%sLaunching the Architect for the real approval contract...%s\n\n' "$CYAN" "$RESET"
}

approval_label() {
  printf '%s%s%s' "$BOLD" "$CYAN" "$1"
}

approval_section() {
  printf '%s%s%s' "$BOLD" "$MAGENTA" "$1"
}

screen_approval() {
  printf '%s%sKADATH  ARCHITECT APPROVAL%s\n' "$BOLD" "$CYAN" "$RESET"
  approval_label 'Objective:'; printf '%s Create and sell a concise niche market research report during the epoch.\n' "$RESET"
  approval_label 'Metric:'; printf '%s Verified net profit in USD: attributed receipts minus documented direct costs.\n' "$RESET"
  approval_label 'Attribution:'; printf '%s Receipts must identify the run, epoch, agent, and genome.\n' "$RESET"
  approval_label 'Baseline:'; printf '%s $0 verified net profit\n' "$RESET"
  approval_label 'Score range:'; printf '%s 0 to 100\n' "$RESET"
  approval_section 'Rubric:'; printf '%s\n' "$RESET"
  printf '  - verified net profit (60%%): {"type": "numeric", "unit": "USD"}\n'
  printf '  - evidence quality (25%%): {"best": "independent", "type": "levels"}\n'
  printf '  - buyer usefulness (15%%): {"pass": "usable", "type": "binary"}\n'
  approval_label 'Required outputs:'; printf '%s candidate result [candidate]; finished report [file:artifacts/report.pdf]; attributed ledger [external:ledger]\n' "$RESET"
  approval_label 'Evidence requirements:'; printf '%s transaction receipt; cost ledger; report artifact\n' "$RESET"
  approval_label 'Automatic failures:'; printf '%s missing-attribution: Revenue cannot be attributed to this agent.; fabricated-evidence: Any receipt or buyer evidence is fabricated.\n' "$RESET"
  approval_label 'Tie breaks:'; printf '%s {"criterion": "higher verified profit", "priority": 1}\n' "$RESET"
  approval_label 'Tie-break policy:'; printf '%s Higher independently verified profit wins; then stronger evidence.\n' "$RESET"
  approval_label 'Anti-fraud:'; printf '%s cross-check receipt IDs; reject self-authored payment evidence\n' "$RESET"
  approval_label 'Grader rules:'; printf '%s Use only captured evidence; Record unsupported claims as zero\n' "$RESET"
  approval_label 'Enabled tools:'; printf '%s web_search, web_fetch, browser, workers\n' "$RESET"
  approval_label 'Configured services:'; printf '%s searxng, playwright, docker-worker-broker, ledger\n' "$RESET"
  approval_label 'Configured agent environment keys:'; printf '%s KADATH_RUN_ID, KADATH_AGENT_ID, KADATH_EPOCH\n' "$RESET"
  approval_label 'Kernel checks:'; printf '%s artifact hash, receipt attribution, cost reconciliation\n' "$RESET"
  approval_label 'Independent connectors:'; printf '%s ledger\n' "$RESET"
  approval_label 'Measurement limitations:'; printf '%s Cash not settled before the epoch cutoff receives no credit.\n' "$RESET"
  approval_section 'Specialist instructions:'; printf '%s\n' "$RESET"
  printf '  - grader: Verify evidence, calculate rubric fractions, and reject unsupported claims.\n'
  printf '  - tweaker: Summarize the behaviors and memories that distinguish the top cohort.\n'
  printf '  - birther: Create varied offspring that inherit the strongest verified characteristics.\n'
  printf 'Final scores are calculated by the kernel from verified criterion fractions; agent self-scores are ignored.\n\n'
  printf '%sProceed? [Y/n]%s y\n' "$CYAN" "$RESET"
}

dashboard_header() {
  local status="$1" epoch="$2" resolved="$3" complete="$4" active="$5" failed="$6"
  printf '%s%sKADATH  EVOLUTION DASHBOARD%s\n' "$BOLD" "$CYAN" "$RESET"
  printf '%sRUN%s  run-20260807-214337-a91f2c    %sSTATUS%s  %s%s%s%s\n' "$MUTED" "$RESET" "$MUTED" "$RESET" "$BOLD" "$GREEN" "$status" "$RESET"
  printf '%sGOAL%s %s\n\n' "$MUTED" "$RESET" "$GOAL"
  printf '%s%sEPOCH %s / 4%s\n' "$BOLD" "$MAGENTA" "$epoch" "$RESET"
  if [[ "$epoch" == 2 ]]; then
    printf '%s████████████████████%s░░░░░░░░░░░░%s  %s/100 agents resolved\n' "$GREEN" "$MUTED" "$RESET" "$resolved"
  else
    printf '%s████████████████████████████████%s%s  %s/100 agents resolved\n' "$GREEN" "$MUTED" "$RESET" "$resolved"
  fi
  printf '%s%s%s complete   %s%s%s active   %s%s%s failed\n\n' "$GREEN" "$complete" "$RESET" "$CYAN" "$active" "$RESET" "$RED" "$failed" "$RESET"
  printf '%s%sLEADERBOARD%s\n' "$BOLD" "$MAGENTA" "$RESET"
  printf '%s               #%s   AGENT                SCORE  STATE\n' "$MUTED" "$RESET"
  printf '%s1%s   agent-073           94.860  %ssuccess%s\n' $'\033[38;5;220m' "$RESET" "$GREEN" "$RESET"
  printf '               2   agent-019           91.425  %ssuccess%s\n' "$GREEN" "$RESET"
  printf '               3   agent-041           89.770  %ssuccess%s\n' "$GREEN" "$RESET"
  printf '               4   agent-088           87.315  %ssuccess%s\n' "$GREEN" "$RESET"
  printf '               5   agent-012           85.940  %ssuccess%s\n\n' "$GREEN" "$RESET"
}

screen_live_dashboard() {
  dashboard_header 'RUNNING' 2 64 61 36 3
  printf '%s%sOPERATIONS%s\n' "$BOLD" "$MAGENTA" "$RESET"
  printf 'model calls  486      workers  37       crashes  2\n\n'
  printf '%s%sRECENT AGENT ACTIVITY%s\n' "$BOLD" "$MAGENTA" "$RESET"
  printf '%sagent-073%s  verified the first attributed sale and reconciled direct costs\n' "$CYAN" "$RESET"
  printf '%sagent-019%s  launched a three-price buyer-intent test with tracked links\n' "$CYAN" "$RESET"
  printf '%sagent-041%s  assembled the report, charts, citations, and evidence appendix\n' "$CYAN" "$RESET"
  printf '%sagent-088%s  worker compared pricing across 27 competing research products\n' "$CYAN" "$RESET"
  printf '%sagent-012%s  validated buyer pain points against 18 independent sources\n' "$CYAN" "$RESET"
  printf '%sagent-056%s  identified an underserved compliance intelligence niche\n\n' "$CYAN" "$RESET"
  printf '%sCtrl-C stops at a durable boundary; the run can be resumed.%s\n' "$MUTED" "$RESET"
}

screen_complete_dashboard() {
  dashboard_header 'COMPLETE' 4 100 96 0 4
  printf '%s%sOPERATIONS%s\n' "$BOLD" "$MAGENTA" "$RESET"
  printf 'model calls  1284     workers  119      crashes  4\n\n'
  printf '%s%sRECENT AGENT ACTIVITY%s\n' "$BOLD" "$MAGENTA" "$RESET"
  printf '%sagent-073%s  finished epoch 4 with $418.60 independently verified net profit\n' "$CYAN" "$RESET"
  printf '%sgrader%s  final evidence audit complete; 96 candidates received verified scores\n' "$CYAN" "$RESET"
  printf '%skernel%s  final ranking frozen; no cull or reproduction follows the last epoch\n' "$CYAN" "$RESET"
  printf '%sagent-019%s  published report, cost ledger, receipt hashes, and final reflection\n' "$CYAN" "$RESET"
  printf '%skernel%s  run artifacts and database records finalized for export\n\n' "$CYAN" "$RESET"
  printf '%sCtrl-C stops at a durable boundary; the run can be resumed.%s\n' "$MUTED" "$RESET"
}

render_page() {
  clear_screen
  printf '\033[?25l'
  case "$PAGE" in
    1) render_boot_art ;;
    2) screen_api_key ;;
    3) screen_model ;;
    4) screen_welcome ;;
    5) screen_saved_credentials ;;
    6) screen_replace_key ;;
    7) screen_change_model ;;
    8) screen_delete_runs ;;
    9) screen_confirm_cleanup ;;
    10) screen_cleanup_complete ;;
    11) screen_goal ;;
    12) screen_duration ;;
    13) screen_custom_duration ;;
    14) screen_population ;;
    15) screen_custom_population ;;
    16) screen_epochs ;;
    17) screen_custom_epochs ;;
    18) screen_launching ;;
    19) screen_approval ;;
    20) screen_live_dashboard ;;
    21) screen_complete_dashboard ;;
  esac
}

read_key() {
  local tail='' next=''
  KEY=''
  IFS= read -r -s -n 1 KEY || true
  if [[ "$KEY" == $'\033' ]]; then
    while IFS= read -r -s -n 1 -t 1 next; do
      tail+="$next"
      [[ "$next" == A || "$next" == B || "$next" == C || "$next" == D ]] && break
    done
    KEY+="$tail"
  fi
}

choice_count() {
  case "$PAGE" in
    4) printf '4' ;;
    5|8) printf '3' ;;
    9|11|13|15|17) printf '2' ;;
    12|14|16) printf '6' ;;
    *) printf '0' ;;
  esac
}

set_page() {
  PAGE="$1"
  case "$PAGE" in
    5|9|16) SELECTION=1 ;;
    12) SELECTION=2 ;;
    14) SELECTION=3 ;;
    *) SELECTION=0 ;;
  esac
}

move_selection() {
  local direction="$1" count
  count="$(choice_count)"
  if ((count <= 0)); then return 0; fi
  if ((direction < 0)); then
    SELECTION=$((SELECTION == 0 ? count - 1 : SELECTION - 1))
  else
    SELECTION=$(((SELECTION + 1) % count))
  fi
}

activate_selection() {
  case "$PAGE" in
    1) set_page 2 ;;
    2) set_page 3 ;;
    3) set_page 4 ;;
    4)
      case "$SELECTION" in
        0) set_page 11 ;;
        1) set_page 5 ;;
        2) set_page 8 ;;
        3) exit 0 ;;
      esac
      ;;
    5)
      case "$SELECTION" in
        0) set_page 4 ;;
        1) set_page 6 ;;
        2) set_page 7 ;;
      esac
      ;;
    6|7) set_page 5 ;;
    8)
      if ((SELECTION == 2)); then set_page 4; else set_page 9; fi
      ;;
    9)
      if ((SELECTION == 0)); then set_page 8; else set_page 10; fi
      ;;
    10) set_page 4 ;;
    11)
      if ((SELECTION == 0)); then set_page 12; else set_page 4; fi
      ;;
    12)
      if ((SELECTION == 4)); then set_page 13
      elif ((SELECTION == 5)); then set_page 11
      else set_page 14
      fi
      ;;
    13)
      if ((SELECTION == 0)); then set_page 14; else set_page 12; fi
      ;;
    14)
      if ((SELECTION == 4)); then set_page 15
      elif ((SELECTION == 5)); then set_page 12
      else set_page 16
      fi
      ;;
    15)
      if ((SELECTION == 0)); then set_page 16; else set_page 14; fi
      ;;
    16)
      if ((SELECTION == 4)); then set_page 17
      elif ((SELECTION == 5)); then set_page 14
      else set_page 18
      fi
      ;;
    17)
      if ((SELECTION == 0)); then set_page 18; else set_page 16; fi
      ;;
    18) set_page 19 ;;
    19) set_page 20 ;;
    20) set_page 21 ;;
    21) set_page 4 ;;
  esac
}

list_pages() {
  local index
  for ((index=0; index<PAGE_COUNT; index++)); do
    printf '%2d  %s\n' "$((index + 1))" "${PAGE_NAMES[$index]}"
  done
}

while (($#)); do
  case "$1" in
    --list)
      list_pages
      exit 0
      ;;
    --page)
      if (($# < 2)) || [[ ! "$2" =~ ^[0-9]+$ ]] || ((10#$2 < 1 || 10#$2 > PAGE_COUNT)); then
        printf 'Page must be a number from 1 to %d.\n' "$PAGE_COUNT" >&2
        exit 2
      fi
      PAGE=$((10#$2))
      shift 2
      ;;
    *)
      printf 'Usage: %s [--page 1-%d] [--list]\n' "$0" "$PAGE_COUNT" >&2
      exit 2
      ;;
  esac
done

set_page "$PAGE"

if [[ ! -t 0 || ! -t 1 ]]; then
  printf 'KADATH needs an interactive terminal.\n' >&2
  exit 1
fi

columns="$(tput cols 2>/dev/null || printf '0')"
rows="$(tput lines 2>/dev/null || printf '0')"
if [[ "$columns" != 100 || "$rows" != 50 ]]; then
  printf 'Resize this terminal to 100 columns × 50 rows (currently %s × %s).\n' "$columns" "$rows"
  printf 'Press Enter to continue.'
  IFS= read -r _continue
fi

while true; do
  render_page
  read_key
  case "$KEY" in
    q|Q) exit 0 ;;
    $'\033'*A) move_selection -1 ;;
    $'\033'*B) move_selection 1 ;;
    '') activate_selection ;;
  esac
done
