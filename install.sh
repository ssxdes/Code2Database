#!/usr/bin/env bash
set -euo pipefail

# install.sh — One-click installer for Code2Database.
#
# Installs only the files needed for agent operation:
#   - SKILL.md (auto-loaded by agent)
#   - scripts/ (command execution)
#   - references/ (on-demand docs referenced by SKILL.md)
#   - skill.json, AGENTS.md, CLAUDE.md (tool integration)
#   - config/runtime.json (runtime defaults)
#
# Developer-only files (tests, evals, OVERVIEW.md, skill-self-scan/,
# code2db-out/, etc.) are NOT installed.
#
# Usage:
#   bash install.sh                          # Interactive: prompts for path and language
#   bash install.sh --dir /path              # Specify install directory
#   bash install.sh --lang en                # Choose English skill docs
#   bash install.sh --lang zh                # Choose Chinese skill docs
#   bash install.sh --target claudecode      # Install for Claude Code
#   bash install.sh --target cursor          # Install for Cursor
#   bash install.sh --target codex           # Install for Codex CLI
#   bash install.sh --target opencode        # Install for OpenCode
#   bash install.sh --target gemini          # Install for Gemini CLI
#   bash install.sh --target all             # Install for all supported tools
#   bash install.sh --uninstall              # Remove installation (3 sub-skills + agent configs)
#
# Installs 3 sub-skills under <install-parent>/:
#   Code2Database            (core: build + browse — always loaded)
#   Code2Database-analysis   (deep semantic analysis — on-demand)
#   Code2Database-ops        (graph editing + ops — on-demand)
# The core sub-skill owns scripts/; the other two symlink to it.
# Default install parent: ~/.claude/skills/ (for Claude Code discovery).
# Users may specify any parent (e.g. ~/.cac/skills/) — all three sub-skills
# land under that parent, and discovery symlinks are created in
# ~/.claude/skills/ so Claude Code can still find them.
#
# Environment:
#   C2D_INSTALL_DIR    Override install directory
#   C2D_LANG           Override language (en/zh)
#   C2D_LANGUAGES      Comma-separated language list (c,cpp,go,python,java,rust,all)
#                      Default: all. Set to e.g. "c,go" for a leaner install
#                      (only installs the tree-sitter grammars you need).

main() {

REPO_NAME="Code2Database"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="${C2D_INSTALL_DIR:-}"
LANG="${C2D_LANG:-}"
TARGET="all"
ACTION="install"

# --- Colors ---
if [ -t 1 ] && command -v tput &>/dev/null; then
    GREEN=$(tput setaf 2)
    RED=$(tput setaf 1)
    YELLOW=$(tput setaf 3)
    BOLD=$(tput bold)
    RESET=$(tput sgr0)
else
    GREEN="" RED="" YELLOW="" BOLD="" RESET=""
fi

ok()   { echo "${GREEN}✓${RESET} $*"; }
fail() { echo "${RED}✗${RESET} $*"; }
warn() { echo "${YELLOW}⚠${RESET} $*"; }
info() { echo "  $*"; }
die()  { fail "$@"; exit 1; }

# --- Argument parsing ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir=*)      INSTALL_DIR="${1#--dir=}" ;;
        --dir)        shift; INSTALL_DIR="$1" ;;
        --lang=*)     LANG="${1#--lang=}" ;;
        --lang)       shift; LANG="$1" ;;
        --target=*)   TARGET="${1#--target=}" ;;
        --target)     shift; TARGET="$1" ;;
        --uninstall)  ACTION="uninstall" ;;
        --help|-h)
            echo "Usage: install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --dir PATH     Install directory or parent directory (default: interactive prompt)"
            echo "                 If PATH does not end in Code2Database, the skill is installed"
            echo "                 as <PATH>/Code2Database with sub-skills as <PATH>/Code2Database-{analysis,ops}."
            echo "                 Examples: --dir ~/.cac/skills  →  ~/.cac/skills/Code2Database"
            echo "                           --dir ~/.claude/skills/Code2Database  (explicit)"
            echo "  --lang LANG    Language: en or zh (default: interactive prompt)"
            echo "  --target TOOL  Target tool: claudecode, cursor, codex, opencode, gemini, all (default: all)"
            echo "  --uninstall    Remove installation (3 sub-skills + agent configs)"
            echo "  --help         Show this help"
            echo ""
            echo "Environment:"
            echo "  C2D_INSTALL_DIR  Override install directory"
            echo "  C2D_LANG         Override language (en/zh)"
            echo "  C2D_LANGUAGES    Comma-separated language list (c,cpp,go,python,java,rust,all)"
            echo "                   Default: all. Set to e.g. 'c,go' for a leaner install"
            echo "                   (only installs the tree-sitter grammars you need)."
            echo ""
            echo "Installs 3 sub-skills:"
            echo "  Code2Database            (core: build + browse — always loaded)"
            echo "  Code2Database-analysis   (deep semantic analysis — on-demand)"
            echo "  Code2Database-ops        (graph editing + ops — on-demand)"
            echo ""
            echo "Only files needed for agent operation are installed."
            echo "Developer files (tests, evals, OVERVIEW.md, etc.) are excluded."
            exit 0
            ;;
        *) die "Unknown argument: $1. Use --help for usage." ;;
    esac
    shift
done

# --- Uninstall ---
if [ "$ACTION" = "uninstall" ]; then
    if [ -z "$INSTALL_DIR" ]; then
        # Try to find existing installation in known locations
        for candidate in \
            "$HOME/.claude/skills/Code2Database" \
            "$HOME/.local/share/Code2Database" \
            "$HOME/.cursor/extensions/Code2Database"; do
            if [ -d "$candidate" ] || [ -L "$candidate" ]; then
                INSTALL_DIR="$candidate"
                break
            fi
        done
        if [ -z "$INSTALL_DIR" ]; then
            die "Cannot find existing installation. Specify --dir PATH to uninstall."
        fi
    fi
    # Expand ~ and normalize (in case user passes ~/.cac/skills/)
    INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"
    # If user passed a parent dir without /Code2Database, append it
    _base=$(basename "$INSTALL_DIR")
    if [ "$_base" != "Code2Database" ]; then
        INSTALL_DIR="${INSTALL_DIR%/}/Code2Database"
    fi
    # Resolve symlink in case INSTALL_DIR is a symlink (e.g. ~/.claude/skills/Code2Database -> real install)
    if [ -L "$INSTALL_DIR" ]; then
        REAL_DIR=$(readlink -f "$INSTALL_DIR")
        echo "  Resolving symlink: $INSTALL_DIR → $REAL_DIR"
        UNINSTALL_PARENT="$(dirname "$REAL_DIR")"
    else
        UNINSTALL_PARENT="$(dirname "$INSTALL_DIR")"
    fi

    echo "Uninstalling from $INSTALL_DIR ..."
    rm -rf "$INSTALL_DIR"

    # Also remove the two sub-skills (Code2Database-analysis, Code2Database-ops)
    # from the same parent directory as the core skill.
    for sub in analysis ops; do
        SUB_DIR="$UNINSTALL_PARENT/Code2Database-$sub"
        if [ -d "$SUB_DIR" ] || [ -L "$SUB_DIR" ]; then
            echo "  Removing sub-skill: $SUB_DIR"
            rm -rf "$SUB_DIR"
        fi
    done

    # Clean up agent-specific links/configs in ~/.claude/skills/ (in case
    # the user installed elsewhere and we created discovery symlinks there).
    for link in Code2Database Code2Database-analysis Code2Database-ops; do
        [ -L "$HOME/.claude/skills/$link" ] && rm -f "$HOME/.claude/skills/$link"
    done
    [ -f "$HOME/.codex/instructions.md" ] && sed -i '/Code2Database/,+2d' "$HOME/.codex/instructions.md" 2>/dev/null
    [ -f "$HOME/.cursor/rules/Code2Database.mdc" ] && rm -f "$HOME/.cursor/rules/Code2Database.mdc" 2>/dev/null
    # Gemini CLI config (GEMINI.md in project root is not removed — it's user-maintained)

    # Remove Claude Code MCP server config
    if [ -f "$HOME/.claude/settings.json" ] && command -v jq &>/dev/null; then
        TMP=$(mktemp)
        jq 'del(.mcpServers["Code2Database"])' "$HOME/.claude/settings.json" > "$TMP" && mv "$TMP" "$HOME/.claude/settings.json"
    fi

    ok "Uninstalled (3 sub-skills + agent configs)."
    exit 0
fi

# --- Validate language ---
if [ -z "$LANG" ]; then
    echo ""
    echo "${BOLD}Code2Database installer${RESET}"
    echo ""
    echo "Select language / 选择语言:"
    echo "  1) English (en)"
    echo "  2) 中文 (zh)"
    echo ""
    read -rp "Enter choice [1/2]: " lang_choice
    case "$lang_choice" in
        1) LANG="en" ;;
        2) LANG="zh" ;;
        *) die "Invalid choice. Use --lang en or --lang zh." ;;
    esac
fi

case "$LANG" in
    en|zh) ;;
    *) die "Unsupported language: $LANG. Use 'en' or 'zh'." ;;
esac

ok "Language: $LANG"

# --- Validate/determine install directory ---
if [ -z "$INSTALL_DIR" ]; then
    echo ""
    echo "Select install path (or press Enter for default):"
    echo "  Default for Claude Code:  ~/.claude/skills/Code2Database"
    echo "  Default for Cursor:      ~/.cursor/extensions/Code2Database"
    echo "  Default for Codex:      ~/.local/share/Code2Database"
    echo "  Default for OpenCode:   ~/.local/share/Code2Database"
    echo "  Default for Gemini CLI:  ~/.local/share/Code2Database"
    echo ""
    echo "  Tip: enter a parent directory (e.g. ~/.cac/skills/) and the skill"
    echo "  will be installed as <parent>/Code2Database, with sub-skills as"
    echo "  <parent>/Code2Database-analysis and <parent>/Code2Database-ops."
    echo ""
    read -rp "Install directory: " install_input
    if [ -z "$install_input" ]; then
        # Default to Claude Code path
        INSTALL_DIR="$HOME/.claude/skills/Code2Database"
    else
        INSTALL_DIR="$install_input"
    fi
fi

# Expand ~ in path
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"

# Normalize: if the user gave a parent directory (not ending in Code2Database),
# append /Code2Database automatically. This lets users type ~/.cac/skills/
# and get ~/.cac/skills/Code2Database without typing the skill name.
_base=$(basename "$INSTALL_DIR")
if [ "$_base" != "Code2Database" ]; then
    INSTALL_DIR="${INSTALL_DIR%/}/Code2Database"
fi

# Parent directory holds all three sub-skills (core, analysis, ops).
INSTALL_PARENT="$(dirname "$INSTALL_DIR")"
ok "Install directory: $INSTALL_DIR"
info "Sub-skills will be installed under: $INSTALL_PARENT"

# --- Check Python ---
if ! command -v python3 &>/dev/null; then
    die "python3 not found. Please install Python 3.9+ first."
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
ok "Python $PY_VER"

# --- Copy only essential files ---
echo ""
echo "${BOLD}Installing Code2Database (slim mode, 3 sub-skills)...${RESET}"

# Create install directory
mkdir -p "$INSTALL_DIR"

# Helper: copy with mkdir
copy_to() {
    local src="$1" dest="$2"
    mkdir -p "$(dirname "$dest")"
    cp -r "$src" "$dest"
}

# =============================================================================
# Sub-skill 1 of 3: Code2Database (CORE — always loaded)
# Gets scripts/ (the shared CLI), skill.json, SKILL.md, references/, AGENTS.md,
# CLAUDE.md, config/runtime.json. The other two sub-skills reference this
# install dir for scripts/ via absolute path.
# =============================================================================

# 1. SKILL.md — the primary agent entry point
if [ -f "$SCRIPT_DIR/docs/$LANG/SKILL.md" ]; then
    copy_to "$SCRIPT_DIR/docs/$LANG/SKILL.md" "$INSTALL_DIR/SKILL.md"
    ok "SKILL.md ($LANG) [core]"
else
    die "SKILL.md for $LANG not found at docs/$LANG/SKILL.md"
fi

# 2. Reference docs — only the files SKILL.md explicitly references
#    SKILL.md references: usage_reference.md, label_rules.md, data_model.md,
#    semantic_enhancement.md, endpoint_pipeline.md, cross_skill_collaboration.md
#    NOT installed: json_schema.md, usage_examples.md (not referenced from SKILL.md)
REF_DIR="$INSTALL_DIR/references"
mkdir -p "$REF_DIR"
for ref_file in usage_reference.md label_rules.md data_model.md \
    semantic_enhancement.md endpoint_pipeline.md cross_skill_collaboration.md; do
    if [ -f "$SCRIPT_DIR/docs/$LANG/references/$ref_file" ]; then
        copy_to "$SCRIPT_DIR/docs/$LANG/references/$ref_file" "$REF_DIR/$ref_file"
    elif [ -f "$SCRIPT_DIR/docs/en/references/$ref_file" ]; then
        # Fallback to English if language-specific version doesn't exist
        copy_to "$SCRIPT_DIR/docs/en/references/$ref_file" "$REF_DIR/$ref_file"
    fi
done
ok "References (6 files) [core]"

# 3. Scripts — all needed for commands to work (only installed in core skill)
mkdir -p "$INSTALL_DIR/scripts"
# Copy scripts directory structure, excluding caches
if command -v rsync &>/dev/null; then
    rsync -a --exclude='__pycache__' --exclude='.pytest_cache' \
        "$SCRIPT_DIR/scripts/" "$INSTALL_DIR/scripts/"
else
    # Manual copy excluding caches
    for item in "$SCRIPT_DIR/scripts"/*; do
        base=$(basename "$item")
        case "$base" in
            __pycache__|.pytest_cache) continue ;;
            *) cp -r "$item" "$INSTALL_DIR/scripts/" ;;
        esac
    done
    # Copy hidden files in scripts (like .vendor if any)
    for item in "$SCRIPT_DIR/scripts"/.*; do
        base=$(basename "$item")
        case "$base" in
            .|..|.pytest_cache|.cac) continue ;;
            *) cp -r "$item" "$INSTALL_DIR/scripts/" ;;
        esac
    done
fi
ok "Scripts (command execution) [core]"

# 4. Skill metadata
copy_to "$SCRIPT_DIR/skill.json" "$INSTALL_DIR/skill.json"
ok "skill.json [core]"

# 5. Agent integration files
copy_to "$SCRIPT_DIR/AGENTS.md" "$INSTALL_DIR/AGENTS.md"
copy_to "$SCRIPT_DIR/CLAUDE.md" "$INSTALL_DIR/CLAUDE.md"
ok "AGENTS.md + CLAUDE.md [core]"

# 6. Runtime config (if exists)
if [ -f "$SCRIPT_DIR/config/runtime.json" ]; then
    copy_to "$SCRIPT_DIR/config/runtime.json" "$INSTALL_DIR/config/runtime.json"
    ok "config/runtime.json [core]"
fi

# =============================================================================
# Sub-skill 2 of 3: Code2Database-analysis (DEEP ANALYSIS — on-demand)
# Gets SKILL_analysis.md (as SKILL.md), skill_analysis.json (as skill.json),
# and references/analysis_commands.md. No scripts/ — uses core's scripts/.
# =============================================================================

ANALYSIS_DIR="$INSTALL_PARENT/Code2Database-analysis"
mkdir -p "$ANALYSIS_DIR"

# Install SKILL_analysis.md as SKILL.md in the analysis sub-skill dir
if [ -f "$SCRIPT_DIR/docs/$LANG/SKILL_analysis.md" ]; then
    copy_to "$SCRIPT_DIR/docs/$LANG/SKILL_analysis.md" "$ANALYSIS_DIR/SKILL.md"
    ok "SKILL.md ($LANG) [analysis]"
else
    warn "SKILL_analysis.md for $LANG not found — analysis sub-skill will be incomplete"
fi

# Install skill_analysis.json as skill.json in the analysis sub-skill dir
if [ -f "$SCRIPT_DIR/skill_analysis.json" ]; then
    copy_to "$SCRIPT_DIR/skill_analysis.json" "$ANALYSIS_DIR/skill.json"
    ok "skill.json [analysis]"
fi

# Install references/analysis_commands.md in the analysis sub-skill dir
mkdir -p "$ANALYSIS_DIR/references"
if [ -f "$SCRIPT_DIR/docs/$LANG/references/analysis_commands.md" ]; then
    copy_to "$SCRIPT_DIR/docs/$LANG/references/analysis_commands.md" "$ANALYSIS_DIR/references/analysis_commands.md"
elif [ -f "$SCRIPT_DIR/docs/en/references/analysis_commands.md" ]; then
    copy_to "$SCRIPT_DIR/docs/en/references/analysis_commands.md" "$ANALYSIS_DIR/references/analysis_commands.md"
fi
ok "References/analysis_commands.md [analysis]"

# Symlink scripts/ from core skill so analysis sub-skill can run commands
if [ -d "$INSTALL_DIR/scripts" ] && [ ! -e "$ANALYSIS_DIR/scripts" ]; then
    ln -sf "$INSTALL_DIR/scripts" "$ANALYSIS_DIR/scripts"
    ok "scripts/ symlinked [analysis → core]"
fi

# =============================================================================
# Sub-skill 3 of 3: Code2Database-ops (OPERATIONS — on-demand)
# Gets SKILL_ops.md (as SKILL.md), skill_ops.json (as skill.json),
# and references/ops_commands.md. No scripts/ — uses core's scripts/.
# =============================================================================

OPS_DIR="$INSTALL_PARENT/Code2Database-ops"
mkdir -p "$OPS_DIR"

# Install SKILL_ops.md as SKILL.md in the ops sub-skill dir
if [ -f "$SCRIPT_DIR/docs/$LANG/SKILL_ops.md" ]; then
    copy_to "$SCRIPT_DIR/docs/$LANG/SKILL_ops.md" "$OPS_DIR/SKILL.md"
    ok "SKILL.md ($LANG) [ops]"
else
    warn "SKILL_ops.md for $LANG not found — ops sub-skill will be incomplete"
fi

# Install skill_ops.json as skill.json in the ops sub-skill dir
if [ -f "$SCRIPT_DIR/skill_ops.json" ]; then
    copy_to "$SCRIPT_DIR/skill_ops.json" "$OPS_DIR/skill.json"
    ok "skill.json [ops]"
fi

# Install references/ops_commands.md in the ops sub-skill dir
mkdir -p "$OPS_DIR/references"
if [ -f "$SCRIPT_DIR/docs/$LANG/references/ops_commands.md" ]; then
    copy_to "$SCRIPT_DIR/docs/$LANG/references/ops_commands.md" "$OPS_DIR/references/ops_commands.md"
elif [ -f "$SCRIPT_DIR/docs/en/references/ops_commands.md" ]; then
    copy_to "$SCRIPT_DIR/docs/en/references/ops_commands.md" "$OPS_DIR/references/ops_commands.md"
fi
ok "References/ops_commands.md [ops]"

# Symlink scripts/ from core skill so ops sub-skill can run commands
if [ -d "$INSTALL_DIR/scripts" ] && [ ! -e "$OPS_DIR/scripts" ]; then
    ln -sf "$INSTALL_DIR/scripts" "$OPS_DIR/scripts"
    ok "scripts/ symlinked [ops → core]"
fi

# =============================================================================
# Optional: support partial-language install (per-user language selection)
# =============================================================================
# If C2D_LANGUAGES is set, the user wants only a subset of tree-sitter grammars.
# We re-run setup.sh with that filter after this install.
if [ -n "${C2D_LANGUAGES:-}" ]; then
    info "C2D_LANGUAGES=$C2D_LANGUAGES — will install only selected language grammars"
fi

# --- Install Python dependencies ---
echo ""
echo "${BOLD}Installing Python dependencies...${RESET}"

# Honor C2D_LANGUAGES for partial-language install (per-user language selection).
# Default is "all" — installs every tree-sitter grammar.
# Engineers focused on a single language can set C2D_LANGUAGES=c,go (etc.) to
# install only the grammars they need.
if [ -n "${C2D_LANGUAGES:-}" ] && [ "${C2D_LANGUAGES:-}" != "all" ]; then
    info "Partial language install requested: C2D_LANGUAGES=$C2D_LANGUAGES"
    if [ -f "$INSTALL_DIR/scripts/setup.sh" ]; then
        bash "$INSTALL_DIR/scripts/setup.sh" --languages "$C2D_LANGUAGES" || \
            warn "setup.sh --languages failed; falling back to full requirements install"
    else
        warn "setup.sh not found at $INSTALL_DIR/scripts/setup.sh; doing full requirements install"
        python3 -m pip install -r "$INSTALL_DIR/scripts/requirements.txt" -q 2>/dev/null || {
            warn "Full requirements install failed, installing core dependencies only..."
            python3 -m pip install networkx tree-sitter tree-sitter-c tree-sitter-cpp \
                tree-sitter-go tree-sitter-python tree-sitter-java tree-sitter-rust \
                z3-solver -q
        }
    fi
else
    python3 -m pip install -r "$INSTALL_DIR/scripts/requirements.txt" -q 2>/dev/null || {
        warn "Full requirements install failed, installing core dependencies only..."
        python3 -m pip install networkx tree-sitter tree-sitter-c tree-sitter-cpp \
            tree-sitter-go tree-sitter-python tree-sitter-java tree-sitter-rust \
            z3-solver -q
    }
fi
ok "Dependencies installed"

# Verify optional cgdb backend (libclang) and z3-solver
echo ""
echo "${BOLD}Verifying optional backends...${RESET}"
echo "${BOLD}libclang (cgdb clang backend — recommended, not required):${RESET}"
if python3 -c "import clang.cindex" 2>/dev/null; then
    ok "libclang available (cgdb clang backend enabled)"
else
    warn "libclang not installed — cgdb clang backend disabled (tree-sitter-only mode)"
    info "  Tree-sitter-only mode is fully functional. To additionally enable the cgdb layer:"
    info "    pip install libclang==17.0.6"
    info "  Or use the system package: yum install llvm-devel || apt install libclang-dev"
fi
echo "${BOLD}z3-solver (sound path feasibility — recommended, not required):${RESET}"
if python3 -c "import z3" 2>/dev/null; then
    ok "z3-solver available (sound path feasibility enabled)"
else
    warn "z3-solver not installed — path-feasible will use heuristic fallback"
    info "  To enable sound path feasibility, install: pip install z3-solver"
fi

# --- Configure Claude Code ---
if [ "$TARGET" = "claudecode" ] || [ "$TARGET" = "all" ]; then
    echo ""
    echo "${BOLD}Configuring for Claude Code...${RESET}"

    # Ensure skill directory exists in Claude's skills path
    CLAUDE_SKILLS_DIR="$HOME/.claude/skills"
    CLAUDE_SKILL_DIR="$CLAUDE_SKILLS_DIR/Code2Database"
    mkdir -p "$CLAUDE_SKILLS_DIR"

    # Create symlink to install directory so Claude Code can discover the
    # skill when installed outside ~/.claude/skills/. Also link the two
    # sub-skills (analysis, ops) so all three are discoverable.
    _link_sub() {
        local src="$1" name="$2"
        local link="$CLAUDE_SKILLS_DIR/$name"
        if [ "$link" = "$src" ]; then
            return 0
        fi
        if [ -L "$link" ]; then
            rm -f "$link"
        elif [ -d "$link" ]; then
            warn "Existing directory at $link — backing up to ${link}.bak"
            mv "$link" "${link}.bak"
        fi
        ln -sf "$src" "$link"
        ok "Linked $link → $src"
    }
    _link_sub "$INSTALL_DIR" "Code2Database"
    _link_sub "$ANALYSIS_DIR" "Code2Database-analysis"
    _link_sub "$OPS_DIR" "Code2Database-ops"

    # Update Claude Code settings for MCP server
    CLAUDE_SETTINGS="$HOME/.claude/settings.json"
    MCP_ENTRY=$(cat <<JSONEOF
{"type":"stdio","command":"python3","args":["$INSTALL_DIR/scripts/code2database_builder.py","serve","--graph","code2db-out/"]}
JSONEOF
)

    if command -v jq &>/dev/null; then
        if [ -f "$CLAUDE_SETTINGS" ]; then
            TMP=$(mktemp)
            jq --argjson entry "$MCP_ENTRY" '.mcpServers["Code2Database"] = $entry' "$CLAUDE_SETTINGS" > "$TMP"
            mv "$TMP" "$CLAUDE_SETTINGS"
        else
            echo '{}' | jq --argjson entry "$MCP_ENTRY" '.mcpServers["Code2Database"] = $entry' > "$CLAUDE_SETTINGS"
        fi
        ok "Updated $CLAUDE_SETTINGS with MCP server config"
    elif command -v python3 &>/dev/null; then
        python3 -c "
import json, os
path = os.path.expanduser('$CLAUDE_SETTINGS')
data = {}
if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
data.setdefault('mcpServers', {})['Code2Database'] = json.loads('$MCP_ENTRY')
with open(path, 'w') as f:
    json.dump(data, f, indent=2)
"
        ok "Updated $CLAUDE_SETTINGS with MCP server config"
    else
        warn "Neither jq nor python3 available — cannot auto-configure MCP."
        info "Add to $CLAUDE_SETTINGS manually:"
        echo '  "mcpServers": {'
        echo '    "Code2Database": {'
        echo '      "type": "stdio",'
        echo "      \"command\": \"python3\","
        echo "      \"args\": [\"$INSTALL_DIR/scripts/code2database_builder.py\", \"serve\", \"--graph\", \"code2db-out/\"]"
        echo '    }'
        echo '  }'
    fi
fi

# --- Configure Cursor ---
if [ "$TARGET" = "cursor" ] || [ "$TARGET" = "all" ]; then
    echo ""
    echo "${BOLD}Configuring for Cursor...${RESET}"

    # Cursor uses .cursor/rules/*.mdc for project rules and .cursor/mcp.json for MCP servers
    CURSOR_DIR="$HOME/.cursor"
    mkdir -p "$CURSOR_DIR/rules"

    # Create Cursor rule file
    CURSOR_RULE="$CURSOR_DIR/Code2Database.mdc"
    cat > "$CURSOR_RULE" <<'CURSOREOF'
---
alwaysApply: true
description: Code2Database coding assistant rules
---

# Code2Database — Cursor Rules

## Project Context

Code2Database is a multi-language code graph generator for C/C++/Go/Python/Java/Rust/ASM codebases. Three-stage pipeline: Profile → Scan → Build.

## Rules

- **Never pre-load** `scripts/` or `config/profiles/` into context — they are implementation details
- **Never load** `OVERVIEW.md` — it is internal architecture, not needed for usage
- **Global-to-local query mode**: start from micro/lite context packs, then drill down with describe-node
- **Use query commands** (explore-flow, describe-node, trace-chain) instead of reading raw JSON output files
- **Only 7 labels**: API_entry, thread_processor, callback_func, constructor, destructor, out_end, unknown_end
- **Always annotate** edge confidence (EXTRACTED/INFERRED/AMBIGUOUS) and invocation conditions
- **Do not propose fixes** before finding root cause
- **Verify** after sync/update operations
- **Keep diffs minimal** — don't refactor unrelated code
- Skill instructions are in `SKILL.md`; reference docs in `references/` are on-demand only
CURSOREOF

    ok "Cursor rule file created at $CURSOR_RULE"

    # Add MCP server config for Cursor
    CURSOR_MCP="$CURSOR_DIR/mcp.json"
    if [ -f "$CURSOR_MCP" ]; then
        if command -v jq &>/dev/null; then
            TMP=$(mktemp)
            jq --argjson entry '{"type":"stdio","command":"python3","args":["'"$INSTALL_DIR"'/scripts/code2database_builder.py","serve","--graph","code2db-out/"]' '.mcpServers["Code2Database"] = $entry' "$CURSOR_MCP" > "$TMP"
            mv "$TMP" "$CURSOR_MCP"
        fi
    else
        echo '{"mcpServers":{"Code2Database":{"type":"stdio","command":"python3","args":["'"$INSTALL_DIR"'/scripts/code2database_builder.py","serve","--graph","code2db-out/"]}}' > "$CURSOR_MCP"
    fi
    ok "Cursor MCP server configured at $CURSOR_MCP"
fi

# --- Configure Codex CLI ---
if [ "$TARGET" = "codex" ] || [ "$TARGET" = "all" ]; then
    echo ""
    echo "${BOLD}Configuring for Codex CLI...${RESET}"

    # Codex reads AGENTS.md from the project root
    # AGENTS.md is already in the install directory
    # Create codex-specific instructions file
    CODEX_DIR="$HOME/.codex"
    mkdir -p "$CODEX_DIR"

    # Add to codex instructions if the file exists
    CODEX_INSTRUCTIONS="$CODEX_DIR/instructions.md"
    if [ -f "$CODEX_INSTRUCTIONS" ]; then
        if ! grep -q "Code2Database" "$CODEX_INSTRUCTIONS" 2>/dev/null; then
            echo "" >> "$CODEX_INSTRUCTIONS"
            echo "## Code2Database" >> "$CODEX_INSTRUCTIONS"
            echo "Skill directory: $INSTALL_DIR" >> "$CODEX_INSTRUCTIONS"
            echo "Follow AGENTS.md at $INSTALL_DIR/AGENTS.md for instructions." >> "$CODEX_INSTRUCTIONS"
        fi
    else
        echo "## Code2Database" > "$CODEX_INSTRUCTIONS"
        echo "Skill directory: $INSTALL_DIR" >> "$CODEX_INSTRUCTIONS"
        echo "Follow AGENTS.md at $INSTALL_DIR/AGENTS.md for instructions." >> "$CODEX_INSTRUCTIONS"
    fi
    ok "Codex CLI configured"
fi

# --- Configure OpenCode ---
if [ "$TARGET" = "opencode" ] || [ "$TARGET" = "all" ]; then
    echo ""
    echo "${BOLD}Configuring for OpenCode...${RESET}"

    # OpenCode uses opencode.jsonc in the project root or ~/.config/opencode/
    OPENCODE_DIR="$HOME/.config/opencode"
    mkdir -p "$OPENCODE_DIR"

    # Create OpenCode config with MCP server
    OPENCODE_CONFIG="$OPENCODE_DIR/config.jsonc"
    if [ -f "$OPENCODE_CONFIG" ]; then
        if command -v jq &>/dev/null; then
            TMP=$(mktemp)
            jq --argjson entry '{"type":"stdio","command":"python3","args":["'"$INSTALL_DIR"'/scripts/code2database_builder.py","serve","--graph","code2db-out/"]' '.mcpServers["Code2Database"] = $entry' "$OPENCODE_CONFIG" > "$TMP"
            mv "$TMP" "$OPENCODE_CONFIG"
        fi
    else
        echo '{"mcpServers":{"Code2Database":{"type":"stdio","command":"python3","args":["'"$INSTALL_DIR"'/scripts/code2database_builder.py","serve","--graph","code2db-out/"]}}' > "$OPENCODE_CONFIG"
    fi
    ok "OpenCode configured at $OPENCODE_DIR"
fi

# --- Configure Gemini CLI ---
if [ "$TARGET" = "gemini" ] || [ "$TARGET" = "all" ]; then
    echo ""
    echo "${BOLD}Configuring for Gemini CLI...${RESET}"

    # Gemini CLI uses settings in ~/.gemini/
    GEMINI_DIR="$HOME/.gemini"
    mkdir -p "$GEMINI_DIR"

    # Create GEMINI.md instruction file
    GEMINI_INSTRUCTIONS="$GEMINI_DIR/GEMINI.md"
    if [ -f "$GEMINI_INSTRUCTIONS" ]; then
        if ! grep -q "Code2Database" "$GEMINI_INSTRUCTIONS" 2>/dev/null; then
            echo "" >> "$GEMINI_INSTRUCTIONS"
            echo "## Code2Database" >> "$GEMINI_INSTRUCTIONS"
            echo "Skill directory: $INSTALL_DIR" >> "$GEMINI_INSTRUCTIONS"
            echo "Follow AGENTS.md at $INSTALL_DIR/AGENTS.md for instructions." >> "$GEMINI_INSTRUCTIONS"
        fi
    else
        echo "## Code2Database" > "$GEMINI_INSTRUCTIONS"
        echo "Skill directory: $INSTALL_DIR" >> "$GEMINI_INSTRUCTIONS"
        echo "Follow AGENTS.md at $INSTALL_DIR/AGENTS.md for instructions." >> "$GEMINI_INSTRUCTIONS"
    fi
    ok "Gemini CLI configured at $GEMINI_DIR"
fi

# --- Verification ---
echo ""
echo "${BOLD}Verifying installation (3 sub-skills)...${RESET}"

if python3 "$INSTALL_DIR/scripts/code2database_builder.py" --help &>/dev/null; then
    ok "code2database_builder.py is functional"
else
    warn "code2database_builder.py failed — check Python dependencies"
fi

# Core sub-skill
if [ -f "$INSTALL_DIR/SKILL.md" ]; then
    ok "Core SKILL.md present ($LANG) at $INSTALL_DIR/SKILL.md"
else
    fail "Core SKILL.md missing"
fi

if [ -d "$INSTALL_DIR/references" ]; then
    ref_count=$(find "$INSTALL_DIR/references" -name "*.md" | wc -l)
    ok "Core references/ present ($ref_count files)"
else
    warn "Core references/ missing"
fi

# Analysis sub-skill
if [ -f "$ANALYSIS_DIR/SKILL.md" ]; then
    ok "Analysis SKILL.md present at $ANALYSIS_DIR/SKILL.md"
else
    warn "Analysis SKILL.md missing"
fi
if [ -f "$ANALYSIS_DIR/references/analysis_commands.md" ]; then
    ok "Analysis references/analysis_commands.md present"
else
    warn "Analysis references/analysis_commands.md missing"
fi

# Ops sub-skill
if [ -f "$OPS_DIR/SKILL.md" ]; then
    ok "Ops SKILL.md present at $OPS_DIR/SKILL.md"
else
    warn "Ops SKILL.md missing"
fi
if [ -f "$OPS_DIR/references/ops_commands.md" ]; then
    ok "Ops references/ops_commands.md present"
else
    warn "Ops references/ops_commands.md missing"
fi

# Sub-skill scripts/ symlinks
if [ -L "$ANALYSIS_DIR/scripts" ]; then
    ok "Analysis scripts/ symlinked to core"
else
    warn "Analysis scripts/ symlink missing"
fi
if [ -L "$OPS_DIR/scripts" ]; then
    ok "Ops scripts/ symlinked to core"
else
    warn "Ops scripts/ symlink missing"
fi

# --- Summary ---
echo ""
ok "Installation complete! (3 sub-skills)"
echo ""
info "Core install directory:    $INSTALL_DIR"
info "Analysis sub-skill:        $ANALYSIS_DIR"
info "Ops sub-skill:             $OPS_DIR"
info "Language: $LANG"
echo ""
info "Sub-skills:"
info "  /Code2Database            — core: build + browse (always loaded)"
info "  /Code2Database-analysis   — deep semantic analysis (on-demand)"
info "  /Code2Database-ops        — graph editing + ops (on-demand)"
echo ""
if [ "$TARGET" = "claudecode" ] || [ "$TARGET" = "all" ]; then
    info "Claude Code: Skills at ~/.claude/skills/Code2Database{,-analysis,-ops}"
    info "Restart Claude Code and use /Code2Database, /Code2Database-analysis,"
    info "  or /Code2Database-ops to activate each sub-skill"
fi
if [ "$TARGET" = "cursor" ] || [ "$TARGET" = "all" ]; then
    info "Cursor: Rule file at ~/.cursor/rules/Code2Database.mdc"
fi
if [ "$TARGET" = "codex" ] || [ "$TARGET" = "all" ]; then
    info "Codex: Instructions added to ~/.codex/instructions.md"
fi
if [ "$TARGET" = "opencode" ] || [ "$TARGET" = "all" ]; then
    info "OpenCode: Config at ~/.config/opencode/config.jsonc"
fi
if [ "$TARGET" = "gemini" ] || [ "$TARGET" = "all" ]; then
    info "Gemini CLI: Instructions at ~/.gemini/GEMINI.md"
fi
echo ""
if [ -n "${C2D_LANGUAGES:-}" ] && [ "${C2D_LANGUAGES:-}" != "all" ]; then
    info "Partial language install: $C2D_LANGUAGES (only those grammars were installed)"
    info "  To add more languages later: bash $INSTALL_DIR/scripts/setup.sh --languages c,cpp,go"
else
    info "All language grammars installed (default)."
    info "  For a leaner install, set C2D_LANGUAGES=c,go (etc.) before re-running install.sh"
fi
echo ""
info "MCP server: 81 tools (34 code2database_* + 19 cgdb_* + 28 design-report) — start with:"
info "  python3 $INSTALL_DIR/scripts/code2database_builder.py serve --graph code2db-out/"
echo ""
info "Knowledge base (kb-*): unified FTS5+BM25 across memory+knowledge+global — start with:"
info "Multi-project: build-multi for joint C2D from A→B→C dependencies — start with:"
info "  python3 $INSTALL_DIR/scripts/code2database_builder.py build-multi --manifest projects.json --outdir code2db-out/"
info "  python3 $INSTALL_DIR/scripts/code2database_builder.py kb-rebuild-index --graph code2db-out/"
info "  python3 $INSTALL_DIR/scripts/code2database_builder.py kb-query --graph code2db-out/ --query \"bdev register\""
echo ""
info "Optional backends (recommended, not required):"
info "  pip install libclang==17.0.6   # enables cgdb clang backend (typed vtable dispatch, CFG, data flow, sync primitives)"
info "  pip install z3-solver          # enables sound path feasibility (heuristic fallback without it)"
echo ""
info "Not installed (developer-only): tests/, evals/, OVERVIEW.md,"
info "  PROFILE_MANUAL.md, RUNTIME_CONFIG.md, CHANGELOG.md, README.md,"
info "  skill-self-scan/, code2db-out/, etc."
echo ""
info "To uninstall: bash install.sh --uninstall --dir $INSTALL_DIR"

} # end main()

main "$@"
