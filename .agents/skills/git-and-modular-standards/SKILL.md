---
name: git-and-modular-standards
description: >-
  Enforces mandatory Git commit hygiene, .gitignore tracking, and modular architecture standards (preventing monolithic files and ensuring atomic commits after every change).
---

# Git & Modular Architecture Standards

## Purpose & Mandate
This skill defines non-negotiable engineering standards for all code alterations in this repository:
1. Every task must be backed by active Git version control.
2. Every file created must be categorized into `.gitignore` or tracked by Git.
3. Every working code change must be committed atomically immediately after verification.
4. Monolithic files (e.g., 2,000+ lines, or mixing UI HTML/JS inside Python servers) are strictly forbidden; code must follow modular architecture and separation of concerns.

---

## 1. Git First Workflow

### Step 1: Pre-flight Verification
Run:
```powershell
& "C:\Users\opc\tools\git\cmd\git.exe" status
```
* If Git is not initialized: Halt all file alterations immediately. Remind the user to initialize Git (`git init`) and create the initial commit before any work begins.
* If Git working tree is dirty: Note the current changes and do not mix unrelated work into an uncommitted tree.

### Step 2: Immediate File Accounting
* **Untracked Secret / Cache / Temp Files**: Add immediately to `.gitignore`.
* **Source / Config / Test Files**: Stage with `git add <file>`.

### Step 3: Atomic Commit After Verification
Whenever a code change is made and verified (compilation, unit test, or dry run):
```powershell
& "C:\Users\opc\tools\git\cmd\git.exe" add <modified_files>
& "C:\Users\opc\tools\git\cmd\git.exe" commit -m "feat/fix: concise summary of atomic change"
```
* **NEVER accumulate changes across turns.**
* **NEVER end a turn leaving working code uncommitted.**

### Step 4: Protection Against Destructive Commands
* Never run `git reset --hard` or `git checkout -- .` without an explicit safety branch:
```powershell
& "C:\Users\opc\tools\git\cmd\git.exe" branch safety_stash_$(Get-Date -Format "yyyyMMdd_HHmmss")
```

---

## 2. Modular Architecture & Anti-Monolith Guidelines

### Strict Size Limits
* **Maximum module size**: 500 – 800 lines.
* **Hard ceiling**: 1,000 lines. Any file approaching this size must be broken down into cohesive submodules.

### Clean Separation of Concerns
1. **Server / Proxy Routing (`stream_proxy.py`)**:
   * Responsible solely for HTTP request routing, header validation, and dispatching to helper modules.
   * **Never embed multi-thousand-line HTML, CSS, or JavaScript strings directly in Python router files.**
2. **UI & Templates (`*_template.py` or `.html`)**:
   * Dashboard, Discover feed, and Storyboard Lab UI components must reside in dedicated template files (`dashboard_template.py`, `discover_template.py`, `storyboard_test_template.py`).
3. **Core Engines & Services**:
   * Cryptography: `crypto_engine.py`
   * Background Watcher: `watcher_service.py`
   * Staging Pipeline: `staging_manager.py`
   * Account Management: `account_manager.py`
   * Cloud Mirroring: `cloud_mirror_manager.py`
   * Metadata / Index: `metadata_helper.py`

---

## 3. Verification Checklist Before Turn Completion
- [ ] Python syntax verified (`python -m py_compile <files>`)
- [ ] No temporary or scratch files left in root directory
- [ ] All new secrets or cache paths added to `.gitignore`
- [ ] All modifications committed cleanly to Git (`git status` clean or cleanly staged)
- [ ] Git commit hash noted in turn response

---

## 4. Reinstallation & Machine Setup
If setting up a new environment or reinstalling Gemini, see:
* [Reinstallation Prompt & Script](./REINSTALLATION_PROMPT.md)

