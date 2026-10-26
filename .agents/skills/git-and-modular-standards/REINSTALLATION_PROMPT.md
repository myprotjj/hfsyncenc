# Gemini / Antigravity Reinstallation Setup Guide

Use this guide and prompt if you reinstall Gemini / Antigravity or set up a new environment. This restores the mandatory Git discipline, atomic commit hygiene, and anti-monolith modular architecture rules across all projects and conversations.

---

## 1. Prompt for Gemini / Antigravity

Copy and paste the prompt below directly into any fresh Gemini / Antigravity session:

```markdown
Configure permanent global engineering rules and skills for all conversations and all projects on this machine:

1. Create the global configuration files:
   - Path 1: `~/.gemini/config/GEMINI.md`
   - Path 2: `~/.gemini/config/AGENTS.md`
   - Path 3: `~/GEMINI.md`

Write the following exact content into all three files:

# Universal Standards & Mandatory Engineering Rules

This configuration enforces strict industry engineering practices across all projects and conversations. The assistant and all contributors must adhere to these hard constraints on every task without exception.

---

## 1. Mandatory Git Discipline & Atomic Commits

### A. Pre-flight Git Check
* **First Action on Every Task**: Before initiating any task or modifying code, verify Git status.
* **If Git is not initialized**: Immediately halt file alterations and remind the user to initialize Git (`git init`) and commit the baseline before proceeding. If Git is uninitialized, keep reminding the user on every turn.
* **Accounting for Every File**:
  * Every newly created file or runtime artifact must be accounted for:
    * If it is a credential, private state, cache, key, or temporary file: **immediately add it to `.gitignore`**.
    * If it is source code, documentation, or configuration: **track it in Git**.

### B. Commit After Every Single Verified Change
* **Never leave uncommitted working changes across turns.**
* As soon as a file change compiles cleanly (e.g. `python -m py_compile`, `npm test`, linter pass) and passes validation:
  1. Stage the modified files (`git add <files>`).
  2. Commit immediately with a clear, concise commit message:
     ```bash
     git commit -m "feat/fix/refactor: descriptive summary of atomic change"
     ```
* Speculative edits or multi-step features must never accumulate in an uncommitted working tree.

### C. Absolute Ban on Blind Destructive Git Commands
* **NEVER execute `git reset --hard` or `git checkout -- .`** without:
  1. Inspecting `git status` and `git diff` first.
  2. Creating a safety branch or physical directory backup.
  3. Explicitly explaining the consequence to the user.

---

## 2. Modular Architecture & Ban on Monolithic Files

### A. Strict File Size & Scope Limits
* **NEVER create or permit monolithic multi-thousand-line files.**
* Cramming HTML, CSS, JavaScript, backend routing, database queries, and business logic into a single monolithic file is strictly prohibited.
* Files must have a single, well-defined responsibility and adhere to standard industry size limits:
  * Target size: **300 – 800 lines** per module.
  * Hard threshold: Any file approaching **1,000 lines** must be refactored into dedicated submodules.

### B. Clean Separation of Concerns
* **Server & Routing**: Backend server files must only handle request routing, dispatching, and process orchestration.
* **UI & Templates**: Frontend HTML, CSS stylesheets, and client-side JavaScript must reside in dedicated template or UI modules/files, completely separated from backend server logic.
* **Business Logic & Utilities**: Cryptography, manifests, database operations, background tasks, and helpers must live in focused, single-purpose modules.

---

## 3. Clean Workspace & Patch Discipline

* **No Stray Patch Scripts**: Never leave ad-hoc patch scripts (`patch_*.py`, `update_*.py`, `test_*.py`) lingering in the project root.
* All temporary verification or scratch scripts must be executed in a dedicated, git-ignored folder (`scratch/` or similar) and cleaned up immediately upon completion.
* Never use broad string find-and-replace across large sections of code. Always verify localized targets.

---

2. Create `~/.gemini/config/skills.json` containing:
{
  "entries": [
    {
      "path": "skills"
    }
  ]
}

3. Create `~/.gemini/config/skills/git-and-modular-standards/SKILL.md` containing:
---
name: git-and-modular-standards
description: >-
  Enforces mandatory Git commit hygiene, .gitignore tracking, and modular architecture standards (preventing monolithic files and ensuring atomic commits after every change across all projects).
---

# Universal Git & Modular Architecture Standards

## Purpose & Mandate
This skill defines non-negotiable engineering standards across all workspaces and conversations:
1. Every task must be backed by active Git version control.
2. Every file created must be categorized into `.gitignore` (secrets/caches) or tracked by Git.
3. Every working code change must be committed atomically immediately after verification.
4. Monolithic files (e.g., 1,000+ lines, or mixing UI HTML/JS inside server code) are strictly forbidden; code must follow modular architecture and separation of concerns.

## 1. Git First Workflow
- Step 1: Pre-flight Verification (`git status`). If uninitialized, halt and prompt user to `git init`.
- Step 2: File Accounting (`.gitignore` secrets/temp; track source in Git).
- Step 3: Atomic Commit after verification (`git commit -m "..."`). Never leave uncommitted changes across turns.
- Step 4: Ban blind destructive commands (`git reset --hard`).

## 2. Modular Architecture & Anti-Monolith Guidelines
- Max module size: 300 – 800 lines (hard ceiling: 1,000 lines).
- Clean separation: Server router, UI templates, and business logic must live in dedicated files.

## 3. Verification Checklist
- [ ] Syntax & compilation verified
- [ ] No temporary scripts left in root
- [ ] Secrets/caches added to `.gitignore`
- [ ] All changes committed cleanly to Git
```

---

## 2. One-Line Automated Setup (PowerShell)

You can also run this command in Windows PowerShell to set up all files instantly:

```powershell
$cfg = "$HOME\.gemini\config"
$skillDir = "$cfg\skills\git-and-modular-standards"
New-Item -ItemType Directory -Force -Path $skillDir | Out-Null

$rules = @'
# Universal Standards & Mandatory Engineering Rules

This configuration enforces strict industry engineering practices across all projects and conversations. The assistant and all contributors must adhere to these hard constraints on every task without exception.

---

## 1. Mandatory Git Discipline & Atomic Commits

### A. Pre-flight Git Check
* **First Action on Every Task**: Before initiating any task or modifying code, verify Git status.
* **If Git is not initialized**: Immediately halt file alterations and remind the user to initialize Git (`git init`) and commit the baseline before proceeding. If Git is uninitialized, keep reminding the user on every turn.
* **Accounting for Every File**:
  * Every newly created file or runtime artifact must be accounted for:
    * If it is a credential, private state, cache, key, or temporary file: **immediately add it to `.gitignore`**.
    * If it is source code, documentation, or configuration: **track it in Git**.

### B. Commit After Every Single Verified Change
* **Never leave uncommitted working changes across turns.**
* As soon as a file change compiles cleanly (e.g. `python -m py_compile`, `npm test`, linter pass) and passes validation:
  1. Stage the modified files (`git add <files>`).
  2. Commit immediately with a clear, concise commit message:
     ```bash
     git commit -m "feat/fix/refactor: descriptive summary of atomic change"
     ```
* Speculative edits or multi-step features must never accumulate in an uncommitted working tree.

### C. Absolute Ban on Blind Destructive Git Commands
* **NEVER execute `git reset --hard` or `git checkout -- .`** without:
  1. Inspecting `git status` and `git diff` first.
  2. Creating a safety branch or physical directory backup.
  3. Explicitly explaining the consequence to the user.

---

## 2. Modular Architecture & Ban on Monolithic Files

### A. Strict File Size & Scope Limits
* **NEVER create or permit monolithic multi-thousand-line files.**
* Cramming HTML, CSS, JavaScript, backend routing, database queries, and business logic into a single monolithic file is strictly prohibited.
* Files must have a single, well-defined responsibility and adhere to standard industry size limits:
  * Target size: **300 – 800 lines** per module.
  * Hard threshold: Any file approaching **1,000 lines** must be refactored into dedicated submodules.

### B. Clean Separation of Concerns
* **Server & Routing**: Backend server files must only handle request routing, dispatching, and process orchestration.
* **UI & Templates**: Frontend HTML, CSS stylesheets, and client-side JavaScript must reside in dedicated template or UI modules/files, completely separated from backend server logic.
* **Business Logic & Utilities**: Cryptography, manifests, database operations, background tasks, and helpers must live in focused, single-purpose modules.

---

## 3. Clean Workspace & Patch Discipline

* **No Stray Patch Scripts**: Never leave ad-hoc patch scripts (`patch_*.py`, `update_*.py`, `test_*.py`) lingering in the project root.
* All temporary verification or scratch scripts must be executed in a dedicated, git-ignored folder (`scratch/` or similar) and cleaned up immediately upon completion.
* Never use broad string find-and-replace across large sections of code. Always verify localized targets.
'@

Set-Content -Path "$cfg\GEMINI.md" -Value $rules -Encoding utf8
Set-Content -Path "$cfg\AGENTS.md" -Value $rules -Encoding utf8
Set-Content -Path "$HOME\GEMINI.md" -Value $rules -Encoding utf8
Set-Content -Path "$cfg\skills.json" -Value '{"entries":[{"path":"skills"}]}' -Encoding utf8

$skillContent = @'
---
name: git-and-modular-standards
description: >-
  Enforces mandatory Git commit hygiene, .gitignore tracking, and modular architecture standards (preventing monolithic files and ensuring atomic commits after every change across all projects).
---

# Universal Git & Modular Architecture Standards

## Purpose & Mandate
This skill defines non-negotiable engineering standards across all workspaces and conversations:
1. Every task must be backed by active Git version control.
2. Every file created must be categorized into `.gitignore` (secrets/caches) or tracked by Git.
3. Every working code change must be committed atomically immediately after verification.
4. Monolithic files (e.g., 1,000+ lines, or mixing UI HTML/JS inside server code) are strictly forbidden; code must follow modular architecture and separation of concerns.

## 1. Git First Workflow
- Step 1: Pre-flight Verification (`git status`). If uninitialized, halt and prompt user to `git init`.
- Step 2: File Accounting (`.gitignore` secrets/temp; track source in Git).
- Step 3: Atomic Commit after verification (`git commit -m "..."`). Never leave uncommitted changes across turns.
- Step 4: Ban blind destructive commands (`git reset --hard`).

## 2. Modular Architecture & Anti-Monolith Guidelines
- Max module size: 300 – 800 lines (hard ceiling: 1,000 lines).
- Clean separation: Server router, UI templates, and business logic must live in dedicated files.

## 3. Verification Checklist
- [ ] Syntax & compilation verified
- [ ] No temporary scripts left in root
- [ ] Secrets/caches added to `.gitignore`
- [ ] All changes committed cleanly to Git
'@
Set-Content -Path "$skillDir\SKILL.md" -Value $skillContent -Encoding utf8
Write-Host "All global Git & Modular Architecture rules configured successfully!" -ForegroundColor Green
```
