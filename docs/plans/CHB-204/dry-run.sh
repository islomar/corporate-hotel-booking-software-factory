#!/usr/bin/env bash
# CHB-204 dry run: simulate every step of plan.md in order on an in-memory model, and check
# the invariants after each step. It changes nothing: it reads repository files and makes
# read-only `gh api` GET calls (no -X, -f, -F or --input) to seed the model.
#
# Usage: bash dry-run.sh [path-to-repository]
#   APP_ROUTE=public   (default) Apps made public and installed on the organization first.
#   APP_ROUTE=transfer Apps transferred after the repository; the outage window is declared.
# The script runs the planned order under both routes, then four wrong orders. It exits 0 only
# when the planned order passes every invariant and every wrong order breaks one.
set -uo pipefail

REPO_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)}" # default: the repository that holds this script
OLD_OWNER="islomar"
NEW_OWNER="islomar-software-garden"
NAME="corporate-hotel-booking-software-factory"
HUMAN_OWNER="islomar"
TRANSFER_DATE="2026-11-21" # after 2026-07-15: GitHub switches `sub` to the immutable format
SUBJECT_MAX=127

# ---------- paths the script reads (checked first, owner rule) ----------
PATHS=(
  "$REPO_DIR/infra/gcp/identities.tf" "$REPO_DIR/infra/gcp/planner.tf" "$REPO_DIR/infra/gcp/variables.tf"
  "$REPO_DIR/infra/github/ruleset.tf" "$REPO_DIR/infra/github/tests/guards.tftest.hcl"
  "$REPO_DIR/.github/workflows/ci.yml" "$REPO_DIR/.github/workflows/supply-chain.yml"
  "$REPO_DIR/.github/workflows/triage.yml" "$REPO_DIR/.github/workflows/pr-updates.yml"
  "$REPO_DIR/.github/workflows/dependabot-auto-merge.yml" "$REPO_DIR/.github/workflows/deploy.yml"
  "$REPO_DIR/factory/as_agent.py"
)
echo "== Paths used by this script"
for p in "${PATHS[@]}"; do
  if [ -e "$p" ]; then echo "  ok      $p"; else echo "  MISSING $p"; exit 2; fi
done
# The GitHub state is git-ignored: only the human's main checkout has it (step B0 backs it up).
if [ ! -e "$REPO_DIR/infra/github/terraform.tfstate" ]; then
  echo "  note    infra/github/terraform.tfstate is absent here (git-ignored); B0 needs the main checkout"
fi
if [ -e "$REPO_DIR/infra/github/versions.tf" ]; then
  echo "  note    infra/github/versions.tf exists: re-read the provider owner before the real run"
fi

# ---------- read-only seeding ----------
gh_get() { # GET only. Prints the jq result, or the fallback when gh is unavailable.
  local endpoint=$1 jq=$2 fallback=$3 out
  if command -v gh >/dev/null && out=$(gh api "$endpoint" --jq "$jq" 2>/dev/null) && [ -n "$out" ]; then
    echo "$out"
  else
    echo "  NOTE: fallback used for $endpoint ($jq): $fallback" >&2
    echo "$fallback"
  fi
}
echo
echo "== Seeding from files and read-only GitHub GETs"
SEED_REPO_ID=$(gh_get "repos/$OLD_OWNER/$NAME" .id 1447565814)
SEED_FULL=$(gh_get "repos/$OLD_OWNER/$NAME" .full_name "$OLD_OWNER/$NAME")
SEED_OWNER_TYPE=$(gh_get "repos/$OLD_OWNER/$NAME" .owner.type User)
ORG_ID=$(gh_get "orgs/$NEW_OWNER" .id 359214981)
SEED_ORG_INSTALLS=$(gh_get "orgs/$NEW_OWNER/installations" '[.installations[].app_slug]|join(" ")' "")
SEED_STRICT=$(gh_get "repos/$OLD_OWNER/$NAME/rules/branches/main" \
  '[.[]|select(.type=="required_status_checks")|.parameters.strict_required_status_checks_policy][0]' true)
SEED_REQUIRED=$(gh_get "repos/$OLD_OWNER/$NAME/rules/branches/main" \
  '[.[]|select(.type=="required_status_checks")|.parameters.required_status_checks[].context]|unique|join(" ")' \
  "backend e2e frontend supply-chain triage")
SEED_QUEUE=$(gh_get "repos/$OLD_OWNER/$NAME/rules/branches/main" '[.[]|select(.type=="merge_queue")]|length' 0)
agent_public=false; gh api apps/chb-factory-agent >/dev/null 2>&1 && agent_public=true
reviewer_public=false; gh api apps/chb-factory-reviewer >/dev/null 2>&1 && reviewer_public=true

if [ "$SEED_FULL" != "$OLD_OWNER/$NAME" ]; then
  echo "  The repository is already $SEED_FULL. This dry run models the move from $OLD_OWNER. Stop."
  exit 2
fi

triggers() { # top-level keys under `on:` of a workflow
  awk '/^on:/{f=1;next} f&&/^[^ #]/{f=0} f&&/^  [a-z_]+:/{gsub(/[ :]/,"");printf "%s ",$0}' "$1"
}
guard_of() { # the repository guard of a workflow, as `field==value`, or `none`
  local g
  g=$(grep -oE "github\.repository(_id)? == '?[A-Za-z0-9_./-]+'?" "$1" | head -1)
  if [ -z "$g" ]; then echo none; return; fi
  g=${g//github./}; g=${g// == /==}; g=${g//\'/}; echo "$g"
}
gcp_condition() { # attribute_condition of a provider file, with var.github_repository resolved
  local file=$1 var cond
  var=$(awk '/variable "github_repository"/{f=1} f&&/default/{gsub(/.*= *"|".*/,"");print;exit}' \
    "$REPO_DIR/infra/gcp/variables.tf")
  cond=$(grep -m1 'attribute_condition' "$file" | sed -E 's/.*= *"(.*)"$/\1/')
  cond=${cond//\$\{var.github_repository\}/$var}
  cond=${cond//\$\{var.deployer_ref\}/refs/heads/main}
  cond=${cond//\$\{var.planner_event_name\}/pull_request}
  echo "$cond"
}
gcp_binding() { # attribute/value of the principalSet bindings in a file
  local var
  var=$(awk '/variable "github_repository"/{f=1} f&&/default/{gsub(/.*= *"|".*/,"");print;exit}' \
    "$REPO_DIR/infra/gcp/variables.tf")
  grep -oE 'attribute\.repository(_id)?/[^"]+' "$1" | sed "s#\${var.github_repository}#$var#" | sed 's/^attribute\.//' | tr '\n' ' '
}
SEED_DEPLOY_COND=$(gcp_condition "$REPO_DIR/infra/gcp/identities.tf")
SEED_PLAN_COND=$(gcp_condition "$REPO_DIR/infra/gcp/planner.tf")
SEED_DEPLOY_BIND=$(gcp_binding "$REPO_DIR/infra/gcp/identities.tf")
SEED_PLAN_BIND=$(gcp_binding "$REPO_DIR/infra/gcp/planner.tf")
SEED_MAPPING=$(grep -ohE '"attribute\.[a-z_]+"' "$REPO_DIR/infra/gcp/identities.tf" | tr -d '"' | sed 's/attribute\.//' | sort -u | tr '\n' ' ')
SEED_SUPPLY_OWNER=$(grep -q 'OWNER: \${{ github.repository_owner }}' "$REPO_DIR/.github/workflows/supply-chain.yml" && echo repository_owner || echo constant)
SEED_AGREEMENT_OWNER=$(grep -qE '^HUMAN_LOGIN = "' "$REPO_DIR/factory/review/agreement.py" && echo constant || echo repository_owner)
# as_agent.py builds its names from REPOSITORY_NAME; A4 adds the organization name first (CHB-218).
if grep -qE '^REPOSITORY_CANDIDATES = .*islomar-software-garden' "$REPO_DIR/factory/as_agent.py"; then
  SEED_AS_AGENT="$NEW_OWNER/$NAME $OLD_OWNER/$NAME"
else
  SEED_AS_AGENT="$OLD_OWNER/$NAME"
fi
SEED_TF_OWNER=$(grep -rhoE 'owner *= *"[^"]+"' "$REPO_DIR"/infra/github/*.tf 2>/dev/null | head -1 | sed -E 's/.*"(.*)"/\1/')
[ -z "$SEED_TF_OWNER" ] && SEED_TF_OWNER="token-user:$HUMAN_OWNER"

cat <<EOF
  repository        $SEED_FULL id=$SEED_REPO_ID owner.type=$SEED_OWNER_TYPE
  organization      $NEW_OWNER id=$ORG_ID installations=[${SEED_ORG_INSTALLS}]
  main ruleset      strict=$SEED_STRICT required=[$SEED_REQUIRED] merge_queue_rules=$SEED_QUEUE
  Apps public       agent=$agent_public reviewer=$reviewer_public
  GCP deployer      condition: $SEED_DEPLOY_COND
                    bindings:  $SEED_DEPLOY_BIND   mapping: $SEED_MAPPING
  GCP planner       condition: $SEED_PLAN_COND
                    bindings:  $SEED_PLAN_BIND
  guards            pr-updates: $(guard_of "$REPO_DIR/.github/workflows/pr-updates.yml")
                    dependabot-auto-merge: $(guard_of "$REPO_DIR/.github/workflows/dependabot-auto-merge.yml")
  triggers          ci: $(triggers "$REPO_DIR/.github/workflows/ci.yml")
                    supply-chain: $(triggers "$REPO_DIR/.github/workflows/supply-chain.yml")
                    triage: $(triggers "$REPO_DIR/.github/workflows/triage.yml")
  supply-chain      --owner from: $SEED_SUPPLY_OWNER
  as_agent          REPOSITORY=$SEED_AS_AGENT
  infra/github      provider owner: $SEED_TF_OWNER (no provider block)
EOF

# ---------- model ----------
declare -A S
reset_state() {
  S=(
    [owner]=$OLD_OWNER [owner_type]=$SEED_OWNER_TYPE [repo_id]=$SEED_REPO_ID
    [deploy_cond]=$SEED_DEPLOY_COND [plan_cond]=$SEED_PLAN_COND
    [deploy_bind]=$SEED_DEPLOY_BIND [plan_bind]=$SEED_PLAN_BIND [mapping]=$SEED_MAPPING
    [guard_pr_updates]=$(guard_of "$REPO_DIR/.github/workflows/pr-updates.yml")
    [guard_dependabot]=$(guard_of "$REPO_DIR/.github/workflows/dependabot-auto-merge.yml")
    [trig_ci]=$(triggers "$REPO_DIR/.github/workflows/ci.yml")
    [trig_supply]=$(triggers "$REPO_DIR/.github/workflows/supply-chain.yml")
    [trig_triage]=$(triggers "$REPO_DIR/.github/workflows/triage.yml")
    [required]=$SEED_REQUIRED [strict]=$SEED_STRICT [queue]=$([ "$SEED_QUEUE" -gt 0 ] && echo yes || echo no)
    [supply_owner]=$SEED_SUPPLY_OWNER [agreement_owner]=$SEED_AGREEMENT_OWNER
    [as_agent]=$SEED_AS_AGENT
    [agent_app_owner]=$OLD_OWNER [agent_public]=$agent_public [agent_installs]="$OLD_OWNER $SEED_ORG_INSTALLS"
    [rev_app_owner]=$OLD_OWNER [rev_public]=$reviewer_public [rev_installs]="$OLD_OWNER $SEED_ORG_INSTALLS"
    [tf_owner]=$SEED_TF_OWNER [state_backup]=no [remote]="$OLD_OWNER/$NAME"
    [old_name_reused]=no [transferred]=no [rebase_logic]=yes [secrets_checked]=no
  )
  S[agent_installs]=$(echo "${S[agent_installs]}" | xargs -n1 | grep -x -e "$OLD_OWNER" -e chb-factory-agent | sed "s/^chb-factory-agent$/$NEW_OWNER/" | xargs)
  S[rev_installs]=$(echo "${S[rev_installs]}" | xargs -n1 | grep -x -e "$OLD_OWNER" -e chb-factory-reviewer | sed "s/^chb-factory-reviewer$/$NEW_OWNER/" | xargs)
}

# OIDC claims of a job, computed from the state
full_name() { echo "${S[owner]}/$NAME"; }
owner_id() { if [ "${S[owner]}" = "$NEW_OWNER" ]; then echo "$ORG_ID"; else echo 966013; fi; }
subject() { # $1: suffix such as environment:production or pull_request
  if [ "${S[transferred]}" = yes ] && [[ "$TRANSFER_DATE" > "2026-07-15" ]]; then
    echo "repo:${S[owner]}@$(owner_id)/$NAME@${S[repo_id]}:$1"
  else
    echo "repo:$(full_name):$1"
  fi
}
claim() { # $1 claim, $2 event
  case $1 in
    repository) full_name ;; repository_id) echo "${S[repo_id]}" ;; repository_owner) echo "${S[owner]}" ;;
    ref) [ "$2" = push ] && echo refs/heads/main || echo refs/pull/1/merge ;;
    event_name) echo "$2" ;;
  esac
}
eval_condition() { # $1 condition `a=='x' && b=='y'`, $2 event
  local cond=$1 ev=$2 term field value
  IFS='&' read -ra terms <<<"${cond//&&/&}"
  for term in "${terms[@]}"; do
    term=$(echo "$term" | xargs)
    [ -z "$term" ] && continue
    field=${term%%==*}; field=${field#assertion.}
    value=${term#*==}; value=${value//\'/}
    [ "$(claim "$field" "$ev")" = "$value" ] || return 1
  done
}
eval_binding() { # $1 bindings `attr/value ...`, $2 event: one binding must match a mapped claim
  local b attr value
  for b in $1; do
    attr=${b%%/*}; value=${b#*/}
    [[ " ${S[mapping]} " == *" $attr "* ]] || continue
    [ "$(claim "$attr" "$2")" = "$value" ] && return 0
  done
  return 1
}
eval_guard() { # $1 `repository==x` or `repository_id==n` or `none`
  [ "$1" = none ] && return 0
  local field=${1%%==*} value=${1#*==}
  [ "$(claim "$field" push)" = "$value" ]
}
installed() { # $1 install list: the App reaches the repository when installed on its owner
  [[ " $1 " == *" ${S[owner]} "* ]]
}

FAILS=()
fail() { FAILS+=("$1"); }
check_invariants() {
  FAILS=()
  local sub
  # I1 a push to main deploys through workload identity
  sub=$(subject environment:production)
  eval_condition "${S[deploy_cond]}" push || fail "I1 deploy: GCP deployer condition rejects $(full_name)"
  eval_binding "${S[deploy_bind]}" push || fail "I1 deploy: no deployer binding matches $(full_name)"
  [ ${#sub} -le $SUBJECT_MAX ] || fail "I1 deploy: google.subject is ${#sub} > $SUBJECT_MAX characters"
  # I2 pull requests can plan infra/gcp read-only
  sub=$(subject pull_request)
  eval_condition "${S[plan_cond]}" pull_request || fail "I2 planner: GCP planner condition rejects $(full_name)"
  eval_binding "${S[plan_bind]}" pull_request || fail "I2 planner: no planner binding matches $(full_name)"
  [ ${#sub} -le $SUBJECT_MAX ] || fail "I2 planner: google.subject is ${#sub} > $SUBJECT_MAX characters"
  # I3 every required check reports on the path PRs take to main
  local need=pull_request c t
  [ "${S[queue]}" = yes ] && need="pull_request merge_group"
  for c in ${S[required]}; do
    case $c in backend | frontend | e2e) t=${S[trig_ci]} ;; supply-chain) t=${S[trig_supply]} ;; triage) t=${S[trig_triage]} ;; *) t="" ;; esac
    for ev in $need; do [[ " $t " == *" $ev "* ]] || fail "I3 checks: '$c' has no $ev trigger; the queue would wait forever"; done
  done
  # I4 the agent can push and open PRs
  installed "${S[agent_installs]}" || fail "I4 agent: chb-factory-agent is not installed on ${S[owner]}"
  [[ " ${S[as_agent]} " == *" $(full_name) "* ]] || fail "I4 agent: as_agent.py does not name $(full_name) (relies on an undocumented API redirect)"
  # I5 automation workflows run and can mint the agent token
  eval_guard "${S[guard_pr_updates]}" || fail "I5 automation: pr-updates.yml guard ${S[guard_pr_updates]} skips the job"
  eval_guard "${S[guard_dependabot]}" || fail "I5 automation: dependabot-auto-merge.yml guard ${S[guard_dependabot]} skips the job"
  # I6 the reviewer can post
  installed "${S[rev_installs]}" || fail "I6 reviewer: chb-factory-reviewer is not installed on ${S[owner]}"
  # I7 the owner's approval still counts in supply-chain and the agreement report
  if [ "${S[supply_owner]}" = repository_owner ] && [ "${S[owner]}" != "$HUMAN_OWNER" ]; then
    fail "I7 owner: supply-chain --owner is ${S[owner]}, not $HUMAN_OWNER; owner-approved lockfile PRs can never pass"
  fi
  if [ "${S[agreement_owner]}" = repository_owner ] && [ "${S[owner]}" != "$HUMAN_OWNER" ]; then
    fail "I7 owner: review agreement counts reactions of ${S[owner]}, not $HUMAN_OWNER"
  fi
  # I8 the GitHub Terraform provider targets the owner of the repository (checked when applied)
  # I9 the old name stays free so redirects keep working
  [ "${S[old_name_reused]}" = no ] || fail "I9 redirects: a repository at $OLD_OWNER/$NAME deletes the redirects"
}
check_tf_owner() { # I8, at an infra-apply step
  local o=${S[tf_owner]#token-user:}
  [ "$o" = "${S[owner]}" ] || { fail "I8 terraform: infra/github provider owner '$o' is not the repository owner '${S[owner]}'"; return 1; }
}

show() {
  printf '    owner=%s guards=[%s | %s] gcp_deploy=[%s] bindings=[%s] mapping=[%s]\n' \
    "${S[owner]}" "${S[guard_pr_updates]}" "${S[guard_dependabot]}" "${S[deploy_cond]}" "${S[deploy_bind]}" "${S[mapping]}"
  printf '    apps: agent owner=%s public=%s installs=[%s]; reviewer owner=%s public=%s installs=[%s]; as_agent=[%s]\n' \
    "${S[agent_app_owner]}" "${S[agent_public]}" "${S[agent_installs]}" "${S[rev_app_owner]}" "${S[rev_public]}" "${S[rev_installs]}" "${S[as_agent]}"
  printf '    checks: queue=%s strict=%s ci=[%s] supply=[%s] triage=[%s]; supply_owner=%s tf_owner=%s remote=%s\n' \
    "${S[queue]}" "${S[strict]}" "${S[trig_ci]}" "${S[trig_supply]}" "${S[trig_triage]}" "${S[supply_owner]}" "${S[tf_owner]}" "${S[remote]}"
  printf '    subject(deploy)=%s (%d chars)\n' "$(subject environment:production)" "$(subject environment:production | wc -c | awk '{print $1-1}')"
}

# ---------- steps ----------
DEPLOY_ID_COND="assertion.repository_id=='$SEED_REPO_ID' && assertion.ref=='refs/heads/main'"
PLAN_ID_COND="assertion.repository_id=='$SEED_REPO_ID' && assertion.event_name=='pull_request'"
step() {
  local s=$1
  case $s in
    A1) S[supply_owner]=constant; S[agreement_owner]=constant ;;
    A2) S[guard_pr_updates]="repository_id==${S[repo_id]}"; S[guard_dependabot]="repository_id==${S[repo_id]}" ;;
    A3) S[trig_ci]="${S[trig_ci]}merge_group "; S[trig_supply]="${S[trig_supply]}merge_group "; S[trig_triage]="${S[trig_triage]}merge_group " ;;
    A4) S[as_agent]="$NEW_OWNER/$NAME $OLD_OWNER/$NAME" ;;
    A5) S[mapping]="${S[mapping]}repository_id "; S[deploy_cond]=$DEPLOY_ID_COND; S[plan_cond]=$PLAN_ID_COND
        S[deploy_bind]="${S[deploy_bind]}repository_id/${S[repo_id]} "; S[plan_bind]="${S[plan_bind]}repository_id/${S[repo_id]} " ;;
    B0) S[state_backup]=yes ;;
    B1) S[agent_public]=true; S[rev_public]=true ;;
    B2) if [ "${S[agent_public]}" = true ] || [ "${S[agent_app_owner]}" = "$NEW_OWNER" ]; then S[agent_installs]="${S[agent_installs]} $NEW_OWNER"; else fail "B2 refused: chb-factory-agent is private and owned by ${S[agent_app_owner]}"; fi
        if [ "${S[rev_public]}" = true ] || [ "${S[rev_app_owner]}" = "$NEW_OWNER" ]; then S[rev_installs]="${S[rev_installs]} $NEW_OWNER"; else fail "B2 refused: chb-factory-reviewer is private and owned by ${S[rev_app_owner]}"; fi ;;
    B3) S[owner]=$NEW_OWNER; S[owner_type]=Organization; S[transferred]=yes ;;
    D1) S[remote]="$NEW_OWNER/$NAME" ;;
    B5) S[secrets_checked]=yes ;;
    B7) # App transfer: the private App leaves islomar; installs on the new owner stay
        S[agent_app_owner]=$NEW_OWNER; S[rev_app_owner]=$NEW_OWNER
        S[agent_installs]=$(echo "${S[agent_installs]}" | xargs -n1 | grep -vx "$OLD_OWNER" | xargs)
        S[rev_installs]=$(echo "${S[rev_installs]}" | xargs -n1 | grep -vx "$OLD_OWNER" | xargs)
        S[agent_public]=false; S[rev_public]=false ;;
    C1) S[tf_owner]=$NEW_OWNER; S[as_agent]="$NEW_OWNER/$NAME $OLD_OWNER/$NAME"; check_tf_owner ;;
    C3) check_tf_owner && { S[queue]=yes; S[strict]=false; } ;;
    C4) [ "${S[queue]}" = yes ] || fail "C4 canary: no merge queue to test"
        [ "${S[secrets_checked]}" = yes ] || fail "C4 canary: secrets not checked after the transfer (B5)" ;;
    C5) S[rebase_logic]=no ;;
    C6) check_tf_owner
        S[deploy_bind]=$(echo "${S[deploy_bind]}" | xargs -n1 | grep -v '^repository/' | xargs)
        S[plan_bind]=$(echo "${S[plan_bind]}" | xargs -n1 | grep -v '^repository/' | xargs)
        S[as_agent]="$NEW_OWNER/$NAME" ;;
    INFRA_PLAN_NOW) check_tf_owner ;;
    REUSE_OLD_NAME) S[old_name_reused]=yes ;;
    UNDO_A1) S[supply_owner]=repository_owner; S[agreement_owner]=repository_owner ;;
    *) echo "unknown step $s"; exit 2 ;;
  esac
}

declare -A DESC=(
  [A1]="agent PR: owner login is a constant (supply-chain, agreement, sweep)"
  [A2]="agent PR: workflow guards on github.repository_id"
  [A3]="agent PR: merge_group trigger on ci, supply-chain, triage"
  [A4]="agent PR: as_agent.py tries the new name, then the old"
  [A5]="agent PR + owner apply: GCP trusts repository_id (expand, old bindings kept)"
  [B0]="owner: back up infra/github/terraform.tfstate"
  [B1]="owner: make both Apps public"
  [B2]="owner: install both Apps on $NEW_OWNER"
  [B3]="owner: transfer the repository to $NEW_OWNER"
  [D1]="owner: git remote set-url origin (one .git, all worktrees)"
  [B5]="agent reads, owner fixes: environments, secrets, variables, rulesets after transfer"
  [B7]="owner: transfer Apps to $NEW_OWNER, make them private"
  [C1]="agent PR + owner apply: new name, infra/github provider owner"
  [C3]="agent PR + owner apply: main ruleset requires a merge queue, strict off"
  [C4]="canary: Dependabot PR and agent PR merge through the queue"
  [C5]="agent PR: retire pr-updates rebase logic; ADRs"
  [C6]="agent PR + owner apply: GCP contract, drop name bindings"
  [INFRA_PLAN_NOW]="owner runs make infra-plan/apply now"
  [REUSE_OLD_NAME]="someone creates $OLD_OWNER/$NAME again"
  [UNDO_A1]="simulation only: the owner login reads github.repository_owner again (A1 not done)"
)

# run_order NAME "STEPS" "DECLARED" : DECLARED lists `step:invariant-prefix` failures allowed as a declared window
run_order() {
  local title=$1 steps=$2 declared=${3:-} s f broken=0 window=0
  reset_state
  echo
  echo "################ $title"
  echo "  start"
  show
  check_invariants
  for f in "${FAILS[@]}"; do echo "  FAIL at start: $f"; broken=1; done
  for s in $steps; do
    echo "  -- $s: ${DESC[$s]}"
    echo "    before:"; show
    FAILS=()
    step "$s"
    local step_fails=("${FAILS[@]}")
    check_invariants
    FAILS=("${step_fails[@]}" "${FAILS[@]}")
    echo "    after:"; show
    for f in "${FAILS[@]}"; do
      [ -z "$f" ] && continue
      if [ -n "$declared" ] && [[ " $declared " == *" $s:${f%% *} "* ]]; then
        echo "    DECLARED WINDOW: $f"; window=1
      else
        echo "    BROKEN: $f"; broken=1
      fi
    done
    [ ${#FAILS[@]} -eq 0 ] && echo "    invariants I1-I9 hold"
  done
  if [ $broken -eq 0 ]; then
    echo "  RESULT: $title passes$([ $window -eq 1 ] && echo ' (with a declared outage window)')"
    return 0
  fi
  echo "  RESULT: $title breaks an invariant"
  return 1
}

PLANNED_P="A1 A2 A3 A4 A5 B0 B1 B2 B3 D1 B5 C1 C3 C4 C5 C6 B7"
PLANNED_T="A1 A2 A3 A4 A5 B0 B3 D1 B7 B2 B5 C1 C3 C4 C5 C6"
# Route T: from B3 until B2, the Apps cannot reach the repository.
WINDOW_T="B3:I4 B3:I5 B3:I6 D1:I4 D1:I6 B7:I4 B7:I6"
STATUS=0

route=${APP_ROUTE:-public}
if [ "$route" = public ]; then
  run_order "PLANNED ORDER, Route P (Apps public first)" "$PLANNED_P" || STATUS=1
  run_order "PLANNED ORDER, Route T (Apps transferred after the repository)" "$PLANNED_T" "$WINDOW_T" || STATUS=1
else
  run_order "PLANNED ORDER, Route T (Apps transferred after the repository)" "$PLANNED_T" "$WINDOW_T" || STATUS=1
fi

echo
echo "================ Wrong orders: each must break an invariant"
expect_break() {
  if run_order "$1" "$2" >"$TMPOUT" 2>&1; then
    cat "$TMPOUT"; echo "  UNEXPECTED: $1 passed. The dry run does not catch this mistake."; STATUS=1
  else
    grep -E "^  -- |BROKEN|RESULT" "$TMPOUT" | grep -B1 -E "BROKEN|RESULT" | head -12
    echo "  expected: $1 is refused"
  fi
}
TMPOUT=$(mktemp)
expect_break "WRONG 1: transfer before GCP trusts the ID (A5 skipped)" "A1 A2 A3 A4 B0 B1 B2 B3"
expect_break "WRONG 2: merge queue before merge_group triggers (A3 skipped)" "A1 A2 A4 A5 B0 B1 B2 B3 D1 B5 C1 C3"
expect_break "WRONG 3: transfer before the owner login is a constant (A1 skipped)" "UNDO_A1 A2 A3 A4 A5 B0 B1 B2 B3"
expect_break "WRONG 4: infra-plan between the transfer and C1" "A1 A2 A3 A4 A5 B0 B1 B2 B3 INFRA_PLAN_NOW"
expect_break "WRONG 5: transfer with private Apps and no declared window" "A1 A2 A3 A4 A5 B0 B3"
expect_break "WRONG 6: old name reused after the transfer" "A1 A2 A3 A4 A5 B0 B1 B2 B3 REUSE_OLD_NAME"
rm -f "$TMPOUT"

echo
if [ $STATUS -eq 0 ]; then
  echo "DRY RUN PASSED: the planned order holds every invariant; every wrong order is refused."
else
  echo "DRY RUN FAILED: see BROKEN or UNEXPECTED above."
fi
exit $STATUS
