# Reversible Filename & Folder Encryptor Tools

A complete toolkit for zero-loss, reversible file and directory name encryption.

---

## 📂 Drop Folders (Automated Watchers)

Drop folders allow you to simply **copy or drag-and-drop** any folder or files into them for instant automatic processing:

### 1. `watch_encrypt_sparse/`
- **What it does**: Automatically encrypts all files and folder names (nested, everything) using each file and folder's **sparse content hash**.
- **Password**: **None needed**. 100% self-contained and reversible from the file contents alone.
- Renames files to `.encname` and folders to `.encdir`.

### 2. `watch_encrypt_password/`
- **What it does**: Automatically encrypts all files and folder names using a custom password.
- **Password file**: Stored inside this folder in `password.txt`. You can edit this file to change your password anytime!
- Renames files to `.encname` and folders to `.encdir`.

### 3. `watch_decrypt/`
- **What it does**: Automatically decrypts any encrypted folder or file dropped inside it.
- **Smart Detection**: Automatically tests whether sparse hash OR a password was used:
  - If sparse hash matches $\rightarrow$ Decrypts with sparse hash!
  - If not $\rightarrow$ Tests passwords from `password.txt` and decrypts!
- Restores original folder names, subfolder names, and filenames 100%.

---

## 🚀 How to Run the Drop Watcher

### Option A: Double-Click Launcher
Double-click [`run_drop_watcher.bat`](run_drop_watcher.bat) to start monitoring in real-time.

### Option B: Command Line (Continuous)
```powershell
python drop_watcher.py
```

### Option C: One-Shot Scan
Scan the drop folders once, process anything pending, and exit immediately:
```powershell
python drop_watcher.py --once
```

---

## 🛠 Manual CLI / Interactive Tool
You can also run the manual interactive wizard or CLI on any custom folder:
- **Interactive Wizard**: Double-click [`run_filename_encryptor.bat`](run_filename_encryptor.bat)
- **CLI**:
  ```powershell
  # Encrypt with sparse hash:
  python filename_encryptor.py -f "C:\path\to\folder" -m encrypt -y

  # Decrypt:
  python filename_encryptor.py -f "C:\path\to\folder" -m decrypt -y

  # Encrypt with custom password:
  python filename_encryptor.py -f "C:\path\to\folder" -m encrypt -k "MyPassword" -y
  ```

---

## 🧪 Testing & Verification
Run the automated test suites:
```powershell
# Test drop folder automation (sparse + password + decrypt):
python test_drop_watcher.py

# Stress test with complex/international Windows filenames:
python test_complex_windows_folder.py
```
