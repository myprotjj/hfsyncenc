# Project Standards & Mandatory Engineering Rules

This repository enforces strict industry engineering practices. The assistant and all contributors must adhere to these hard constraints on every task without exception.

---

## 1. Mandatory Git Discipline & Atomic Commits

### A. Pre-flight Git Check
* Before initiating any task or modifying code, verify Git status.
* If Git is not initialized or the repository is broken, immediately alert the user and halt work until Git version control is established.
* Every newly created file or runtime artifact must be accounted for:
  * If it is a credential, private state, cache, or temporary file: **immediately add it to `.gitignore`**.
  * If it is source code, documentation, or configuration: **track it in Git**.

### B. Commit After Every Single Verified Change
* **Never leave uncommitted working changes across turns.**
* As soon as a file change compiles cleanly (`python -m py_compile`) and passes validation:
  1. Stage the modified files (`git add <files>`).
  2. Commit immediately with a clear, concise commit message:
     ```bash
     git commit -m "feat/fix/refactor: descriptive summary of change"
     ```
* Multiple features or speculative edits must never be piled into an unversioned working tree.

### C. Absolute Ban on Blind Destructive Git Commands
* **NEVER execute `git reset --hard` or `git checkout -- .`** without:
  1. Inspecting `git status` and `git diff` first.
  2. Creating a safety branch or physical directory backup.
  3. Explicitly explaining the consequence to the user.

---

## 2. Modular Architecture & Ban on Monolithic Files

### A. Strict File Size & Scope Limits
* **NEVER create or permit monolithic multi-thousand-line files.**
* Cramming tens of thousands of lines of HTML, CSS, JavaScript, and backend HTTP server logic into a single Python file (e.g. `stream_proxy.py`) is strictly prohibited.
* Files must have a single, well-defined responsibility and adhere to standard industry size limits:
  * Target size: **300 – 800 lines** per module.
  * Hard threshold: Any file approaching **1,000 lines** must be refactored into dedicated submodules.

### B. Separation of Concerns
* **Server & Routing**: Backend Python files (`stream_proxy.py`, `watcher_service.py`) must only handle request routing, dispatching, and process orchestration.
* **UI & Templates**: Frontend HTML, CSS stylesheets, and client-side JavaScript must reside in dedicated template or UI modules (`*_template.py` or `.html` templates), completely separated from backend server logic.
* **Business Logic & Helpers**: Cryptography, manifests, cloud sync, metadata parsing, and rate limiting must live in focused modules (`crypto_engine.py`, `metadata_helper.py`, `staging_manager.py`, `account_manager.py`).

---

## 3. Clean Workspace & Patch Discipline

* **No Stray Patch Scripts**: Never leave ad-hoc patch scripts (`patch_*.py`, `update_*.py`, `test_*.py`) lingering in the project root.
* All temporary verification or scratch scripts must be executed in a dedicated, git-ignored folder (`scratch/`) and cleaned up immediately upon completion.
* Never use broad string find-and-replace across large sections of code. Always verify localized targets.
