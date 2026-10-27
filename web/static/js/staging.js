/**
 * Staging & 1-Commit / Multi-Part Push Hub - Modular Frontend Script
 * Complete UI and control center matching verified screenshot and multi-part commit engine
 */

(function (window) {
    'use strict';

    let lastStagingData = null;
    let stagingPollTimer = null;
    const activePushLogExpanded = {};

    function escapeHtml(str) {
        if (!str) return '';
        return String(str)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function escapeJsString(str) {
        if (!str) return '';
        return String(str).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    }

    function showToast(msg) {
        if (typeof window.showToastNotification === 'function') {
            window.showToastNotification(msg);
        } else if (typeof window.showToast === 'function' && window.showToast !== showToast) {
            window.showToast(msg);
        } else {
            console.log('[Staging Toast]', msg);
        }
    }

    /**
     * Built-in dark glass confirmation modal
     */
    function showCustomModal(opts) {
        let modal = document.getElementById('stagingActionModal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'stagingActionModal';
            modal.style.cssText = 'position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(5, 9, 20, 0.88); backdrop-filter: blur(14px); display: none; align-items: center; justify-content: center; z-index: 10005; padding: 20px;';
            modal.innerHTML = `
            <div style="background: #0f172a; border: 1px solid rgba(56, 189, 248, 0.45); border-radius: 16px; max-width: 540px; width: 100%; box-shadow: 0 25px 60px rgba(0,0,0,0.9), 0 0 25px rgba(56, 189, 248, 0.15); overflow: hidden;">
                <div style="padding: 16px 22px; border-bottom: 1px solid rgba(255,255,255,0.08); display: flex; justify-content: space-between; align-items: center; background: rgba(30,41,59,0.5);">
                    <div id="stagingModalTitle" style="font-weight: 800; font-size: 1.1rem; color: #f8fafc; display: flex; align-items: center; gap: 8px;">Action</div>
                    <button type="button" onclick="document.getElementById('stagingActionModal').style.display='none'" class="tool-btn btn-xs" style="font-size: 1.3rem; padding: 2px 8px; line-height: 1; border: none; background: transparent; color: #94a3b8; cursor: pointer;">&times;</button>
                </div>
                <div id="stagingModalMessage" style="padding: 22px; font-size: 0.92rem; color: #cbd5e1; line-height: 1.55;"></div>
                <div style="padding: 14px 22px; border-top: 1px solid rgba(255,255,255,0.08); display: flex; justify-content: flex-end; gap: 12px; background: rgba(30,41,59,0.3);">
                    <button type="button" id="stagingModalCancelBtn" class="tool-btn" style="padding: 7px 18px; border-radius: 8px; font-weight: 600; cursor: pointer;">Cancel</button>
                    <button type="button" id="stagingModalConfirmBtn" class="tool-btn" style="padding: 7px 22px; border-radius: 8px; font-weight: 700; cursor: pointer; border: none;">Confirm</button>
                </div>
            </div>`;
            document.body.appendChild(modal);
            modal.addEventListener('click', function(e) {
                if (e.target === modal) modal.style.display = 'none';
            });
        }

        const titleEl = document.getElementById('stagingModalTitle');
        const msgEl = document.getElementById('stagingModalMessage');
        const confirmBtn = document.getElementById('stagingModalConfirmBtn');
        const cancelBtn = document.getElementById('stagingModalCancelBtn');

        if (titleEl) titleEl.innerHTML = opts.title || 'Confirm';
        if (msgEl) msgEl.innerHTML = opts.message || '';
        if (confirmBtn) {
            confirmBtn.textContent = opts.confirmText || 'Confirm';
            confirmBtn.style.cssText = (opts.confirmStyle || 'background: #0284c7; color: white;') + '; padding: 7px 22px; border-radius: 8px; font-weight: 700; cursor: pointer; border: none;';
            confirmBtn.onclick = function() {
                modal.style.display = 'none';
                if (typeof opts.onConfirm === 'function') opts.onConfirm();
            };
        }
        if (cancelBtn) {
            cancelBtn.onclick = function() {
                modal.style.display = 'none';
            };
        }
        modal.style.display = 'flex';
    }

    /**
     * Live Git Console & details box
     */
    function buildGitConsoleHtml(repoFolder, pushProgress, totalSizeFormatted, totalFileCount, isCompleted) {
        const prog = pushProgress || {};
        const safeClass = 'push-log-box-' + repoFolder.replace(/[^a-zA-Z0-9_-]/g, '_');
        const safeArrowClass = 'log-arrow-' + repoFolder.replace(/[^a-zA-Z0-9_-]/g, '_');
        const isExpanded = Boolean(activePushLogExpanded[repoFolder]);
        const logLines = prog.log_lines || [];
        const logCount = logLines.length;

        const logsHtml = logCount > 0 ? logLines.map(line => {
            let color = '#cbd5e1';
            if (line.includes('✓') || line.includes('SUCCESS') || line.includes('accepted')) color = '#4ade80';
            else if (line.includes('Uploading') || line.includes('Pushing') || line.includes('Git Push:')) color = '#38bdf8';
            else if (line.includes('Error') || line.includes('Failed')) color = '#f87171';
            else if (line.includes('Target') || line.includes('Assembled') || line.includes('Transferred')) color = '#c084fc';
            else if (line.includes('Pre-encrypting') || line.includes('Check')) color = '#facc15';
            return `<div style="color: ${color}; padding: 1px 0;">${escapeHtml(line)}</div>`;
        }).join('') : '<div style="color: #64748b;">Connecting to Git LFS stream...</div>';

        return `
        <div style="margin-top: 10px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;">
            <button type="button" onclick="window.StagingManager.togglePushLog('${escapeJsString(repoFolder)}')" class="tool-btn" style="background: rgba(0,0,0,0.5); border: 1px solid rgba(56, 189, 248, 0.4); color: #38bdf8; font-size: 0.74rem; font-weight: 600; padding: 4px 12px; border-radius: 6px; cursor: pointer; display: inline-flex; align-items: center; gap: 6px;">
                <span>📋 Live Git Console / Details</span>
                <span class="${safeArrowClass}">${isExpanded ? '▲' : '▼'}</span>
                <span style="font-size: 0.68rem; color: #94a3b8; background: rgba(255,255,255,0.08); padding: 1px 6px; border-radius: 4px;">${logCount} lines</span>
            </button>
            <div style="font-size: 0.75rem; color: #94a3b8;">
                ${prog.speed_str ? `<span style="color: #4ade80; font-weight: 600;">⚡ Speed: ${escapeHtml(prog.speed_str)}</span>` : ''}
            </div>
        </div>
        <div id="${safeClass}" style="display: ${isExpanded ? 'block' : 'none'}; margin-top: 8px; background: rgba(0,0,0,0.6); border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 8px; padding: 10px 14px; font-family: ui-monospace, SFMono-Regular, monospace; font-size: 0.74rem; max-height: 180px; overflow-y: auto;">
            ${logsHtml}
        </div>
        `;
    }

    function togglePushLog(repoFolder) {
        activePushLogExpanded[repoFolder] = !activePushLogExpanded[repoFolder];
        const safeClass = 'push-log-box-' + repoFolder.replace(/[^a-zA-Z0-9_-]/g, '_');
        const safeArrowClass = 'log-arrow-' + repoFolder.replace(/[^a-zA-Z0-9_-]/g, '_');
        const el = document.getElementById(safeClass);
        if (el) el.style.display = activePushLogExpanded[repoFolder] ? 'block' : 'none';
        const arrow = document.querySelector('.' + safeArrowClass);
        if (arrow) arrow.innerText = activePushLogExpanded[repoFolder] ? '▲' : '▼';
    }

    /**
     * Card 1: Ready / Active / Pushed / Error Staged Repo Card
     * Supports both 1-Commit Atomic and Multi-Part (~10GB) batch commits
     */
    function renderStagingRepoCard(r, isModal) {
        const rf = r.repo_folder || '';
        const totalVideos = r.total_videos || r.video_count || r.file_count || 0;
        const totalAssets = r.total_assets || (totalVideos * 4);
        const totalBytes = r.total_enc_bytes || r.total_enc_size || r.total_orig_bytes || r.total_size_bytes || 0;
        const sizeStr = r.total_size_formatted || r.size_formatted || r.initial_scanned_formatted || '0 MB';
        const repoId = r.repo_id || 'Vault';
        const account = r.account || (repoId.includes('/') ? repoId.split('/')[0] : '');
        const rawRepoName = repoId.includes('/') ? repoId.split('/')[1] : repoId;
        const thumbs = r.preview_thumbs || [];
        const prog = r.push_progress || {};

        // Calculate estimated or actual ~10GB batch count
        const estBatches = r.total_batches || prog.total_batches || Math.max(1, Math.ceil(totalBytes / (10 * 1024 * 1024 * 1024)));
        const pushBtnLabel = estBatches > 1 ? (`🚀 Push (${estBatches} Commits ~10GB)`) : '🚀 Push to HF (1 Commit)';
        const gitChipText = estBatches > 1 ? (`⚡ Git: ${estBatches} Batches (~10GB)`) : '⚡ Git: 1-Commit Atomic';

        const isPaused = r.status === 'paused';
        const isQueued = r.status === 'queued';
        const isPushing = r.status === 'pushing' || (prog.percent != null && r.status !== 'ready_to_push' && !isPaused && !isQueued && r.status !== 'error' && r.status !== 'pushed');
        const isPushed = r.status === 'pushed';
        const isError = r.status === 'error';
        const isFailedAudit = r.status === 'failed_audit' || r.status === 'staging_audit_failed';

        // 1. Ready State
        if (!isPushing && !isPaused && !isQueued && !isPushed && !isError && !isFailedAudit) {
            const hfDatasetUrl = 'https://huggingface.co/datasets/' + encodeURIComponent(repoId);
            const commitsUrl = hfDatasetUrl + '/commits/main';
            const displayTitle = account ? (account + ' / ' + rawRepoName) : repoId;
            const avatarHtml = thumbs.length > 0
                ? `<img src="${escapeHtml(thumbs[0])}" class="staging-account-avatar" alt="Preview" onerror="this.style.display='none'">`
                : '';

            const cardCommits = prog.commits || r.commits || [];

            return `
            <div class="staging-card-ready">
                <!-- Header row: Title, encdir, ready badge, action buttons -->
                <div class="staging-card-top">
                    <div class="staging-card-title-group">
                        <span class="repo-icon">📦</span>
                        <span class="repo-name">${escapeHtml(displayTitle)}</span>
                        <span class="repo-encdir">(${escapeHtml(rf)})</span>
                        <span class="staging-pill-ready">✓ Ready to Push</span>
                    </div>
                    <div class="staging-actions">
                        <a href="/discover?staged_repo=${encodeURIComponent(rf)}" class="staging-btn-discover" title="View staged videos in Discover feed">
                            👁 Discover ↗
                        </a>
                        <button type="button" onclick="window.StagingManager.confirmRevertRepo('${escapeJsString(rf)}', ${totalVideos}, '${escapeJsString(sizeStr)}')" class="staging-btn-revert" title="Revert staged copies back to source directory and restore ignored status">
                            ⏪ Unstage & Decrypt
                        </button>
                        <button type="button" onclick="window.StagingManager.confirmDiscardRepo('${escapeJsString(rf)}', ${totalVideos}, '${escapeJsString(sizeStr)}')" class="staging-btn-discard" title="Permanently discard staged files without restoring">
                            🗑 Discard
                        </button>
                        <button type="button" data-repo="${escapeHtml(rf)}" onclick="window.StagingManager.pushStagedRepo(this.dataset.repo)" class="staging-btn-push" title="Atomic multi-commit upload to Hugging Face">
                            ${pushBtnLabel}
                        </button>
                    </div>
                </div>

                <!-- Full width solid green progress bar -->
                <div class="staging-bar-ready-track"></div>

                <!-- Target info line -->
                <div class="staging-target-row">
                    <div>
                        Target: <a href="${hfDatasetUrl}" target="_blank" rel="noopener noreferrer" class="staging-target-link">${escapeHtml(repoId)} ↗</a> • Authenticated AES-256-GCM chunks verified on disk
                    </div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        ${account ? `<span class="staging-account-pill"><span>👤</span> <b>${escapeHtml(account)}</b></span>` : ''}
                        ${avatarHtml}
                    </div>
                </div>

                <!-- Metric chips row -->
                <div class="staging-chips-strip">
                    <span class="staging-chip staging-chip-prestaged" title="Total Videos & Cryptographic Assets">
                        📦 Pre-Staged: ${totalVideos} Videos (${totalAssets} Assets)
                    </span>
                    <span class="staging-chip staging-chip-size" title="Total Pre-encrypted Payload Size">
                        💾 Size: ${escapeHtml(sizeStr)}
                    </span>
                    <span class="staging-chip staging-chip-integrity" title="Local Chunk Integrity Check">
                        🛡 Integrity: 100% Passed ✔
                    </span>
                    <a href="${hfDatasetUrl}" target="_blank" rel="noopener noreferrer" class="staging-chip staging-chip-repo" title="Target Repository on Hugging Face">
                        ☁ Repo: ${escapeHtml(repoId)} ↗
                    </a>
                    <span class="staging-chip staging-chip-git" title="Multi-part commit partitioning">
                        ${gitChipText}
                    </span>
                </div>

                <!-- Commits line with batch commit pills -->
                <div class="staging-commits-row">
                    <span>Commits:</span>
                    ${cardCommits.length > 0 ? cardCommits.map(c => `
                        <a href="${escapeHtml(c.commit_url)}" target="_blank" rel="noopener noreferrer" class="staging-commit-link" title="View Batch ${c.batch} Commit on Hugging Face">
                            <span>🔗 #${c.batch} (${c.size_formatted || ''})</span> ↗
                        </a>
                    `).join('') : ''}
                    <a href="${commitsUrl}" target="_blank" rel="noopener noreferrer" class="staging-commit-link" title="View repository commits on Hugging Face">
                        📜 View All Commits ↗
                    </a>
                </div>
            </div>`;
        }

        // 2. Pushing State (Multi-batch progress tracking)
        if (isPushing) {
            const pct = prog.percent != null ? prog.percent : 25;
            const msg = prog.message || 'Pushing assets to Hugging Face...';
            const batchBadge = prog.batch_info || (prog.total_batches > 1 ? ('Batch ' + (prog.current_batch || 1) + '/' + prog.total_batches) : 'Committing');
            return `
            <div class="staging-card-ready" style="border-color: rgba(56, 189, 248, 0.6); background: rgba(15, 23, 42, 0.95);">
                <div class="staging-card-top">
                    <div class="staging-card-title-group">
                        <span class="status-spinner" style="display:inline-block;width:14px;height:14px;border:2px solid #38bdf8;border-top-color:transparent;border-radius:50%;animation:spin 1s linear infinite;"></span>
                        <span class="repo-name" style="color: #38bdf8;">Pushing: ${escapeHtml(repoId)}</span>
                        <span class="repo-encdir">(${escapeHtml(rf)})</span>
                        <span class="staging-pill-ready" style="background: rgba(56, 189, 248, 0.2); color: #38bdf8; border-color: rgba(56, 189, 248, 0.4);">
                            ⚡ ${escapeHtml(batchBadge)} (${pct}%)
                        </span>
                    </div>
                    <div class="staging-actions">
                        <button type="button" onclick="window.StagingManager.pauseRepoPush('${escapeJsString(rf)}')" class="staging-btn-revert" style="background: rgba(239, 68, 68, 0.2); color: #f87171; border-color: rgba(239, 68, 68, 0.4);">
                            ⏸ Pause Push
                        </button>
                    </div>
                </div>
                <div style="width: 100%; height: 6px; background: rgba(255, 255, 255, 0.1); border-radius: 3px; overflow: hidden; margin: 4px 0;">
                    <div style="width: ${Math.max(4, pct)}%; height: 100%; background: linear-gradient(90deg, #0ea5e9, #10b981); transition: width 0.3s ease;"></div>
                </div>
                <div class="staging-target-row">
                    <div>${escapeHtml(msg)} ${prog.current_file ? `<code style="color: #38bdf8;">[${escapeHtml(prog.current_file)}]</code>` : ''}</div>
                    <div style="font-weight: 600; color: #cbd5e1;">${prog.uploaded_bytes_formatted || '0 MB'} / ${prog.total_bytes_formatted || sizeStr}</div>
                </div>
                ${buildGitConsoleHtml(rf, prog, sizeStr, totalVideos, false)}
            </div>`;
        }

        // 3. Paused Push State
        if (isPaused) {
            return `
            <div class="staging-card-ready" style="border-color: rgba(245, 158, 11, 0.5); background: rgba(30, 41, 59, 0.95);">
                <div class="staging-card-top">
                    <div class="staging-card-title-group">
                        <span style="font-size: 1.15rem;">⏸</span>
                        <span class="repo-name" style="color: #fbbf24;">Push Paused: ${escapeHtml(repoId)}</span>
                        <span class="repo-encdir">(${escapeHtml(rf)})</span>
                        <span class="staging-pill-ready" style="background: rgba(245, 158, 11, 0.2); color: #fbbf24; border-color: rgba(245, 158, 11, 0.5);">
                            ⏸ Paused on Batch ${prog.current_batch || 1}/${prog.total_batches || estBatches}
                        </span>
                    </div>
                    <div class="staging-actions">
                        <button type="button" onclick="window.StagingManager.resumeRepoPush('${escapeJsString(rf)}')" class="staging-btn-push" style="background: #10b981;">
                            ▶ Resume Push
                        </button>
                        <button type="button" onclick="window.StagingManager.confirmDiscardRepo('${escapeJsString(rf)}', ${totalVideos}, '${escapeJsString(sizeStr)}')" class="staging-btn-discard">
                            🗑 Discard
                        </button>
                    </div>
                </div>
                <div class="staging-target-row" style="margin-top: 6px;">
                    <div>${escapeHtml(prog.message || 'Push paused by user. Click Resume Push to continue.')}</div>
                </div>
                ${buildGitConsoleHtml(rf, prog, sizeStr, totalVideos, false)}
            </div>`;
        }

        // 4. Pushed Completed State
        if (isPushed) {
            const cardCommits = prog.commits || r.commits || [];
            return `
            <div class="staging-card-ready" style="border-color: rgba(16, 185, 129, 0.5); background: rgba(16, 185, 129, 0.12);">
                <div class="staging-card-top">
                    <div class="staging-card-title-group">
                        <span style="font-size: 1.15rem;">✓</span>
                        <span class="repo-name" style="color: #4ade80;">Push Complete: ${escapeHtml(repoId)}</span>
                        <span class="repo-encdir">(${escapeHtml(rf)})</span>
                        <span class="staging-pill-ready">✓ 100% Pushed</span>
                    </div>
                    <div class="staging-actions">
                        <button type="button" onclick="window.StagingManager.dismissPushedRepo('${escapeJsString(rf)}')" class="staging-btn-discard" style="background: transparent; border: none; color: #94a3b8;">
                            ✕ Dismiss
                        </button>
                    </div>
                </div>
                <div class="staging-target-row" style="margin-top: 6px; color: #cbd5e1;">
                    <div>Successfully committed <strong>${prog.total_assets || totalAssets}</strong> assets (${sizeStr}) across ${estBatches} Git commit(s) to Hugging Face!</div>
                    ${prog.speed_str ? `<span style="color: #10b981; font-weight: 600;">⚡ Speed: ${escapeHtml(prog.speed_str)}</span>` : ''}
                </div>
                ${cardCommits.length > 0 ? `
                <div class="staging-commits-row" style="margin-top: 8px;">
                    <span>Commits:</span>
                    ${cardCommits.map(c => `
                        <a href="${escapeHtml(c.commit_url)}" target="_blank" rel="noopener noreferrer" class="staging-commit-link" title="View Batch ${c.batch} Commit on Hugging Face">
                            <span>🔗 #${c.batch} (${c.size_formatted || ''})</span> ↗
                        </a>
                    `).join('')}
                </div>` : ''}
                ${buildGitConsoleHtml(rf, prog, sizeStr, totalVideos, true)}
            </div>`;
        }

        // 5. Failed Audit Discrepancy State
        if (isFailedAudit) {
            return `
            <div class="staging-card-ready" style="border-color: rgba(239, 68, 68, 0.5); background: rgba(239, 68, 68, 0.12);">
                <div class="staging-card-top">
                    <div class="staging-card-title-group">
                        <span style="font-size: 1.15rem;">⚠️</span>
                        <span class="repo-name" style="color: #f87171;">Flaw Detected: Staging Audit Failed (${escapeHtml(rf)})</span>
                        <span class="staging-pill-ready" style="background: #ef4444; color: #fff; border: none;">
                            PUSH STRICTLY BLOCKED
                        </span>
                    </div>
                    <div class="staging-actions">
                        <button type="button" onclick="window.StagingManager.confirmRevertRepo('${escapeJsString(rf)}', ${totalVideos}, '${escapeJsString(sizeStr)}')" class="staging-btn-revert">
                            ⏪ Unstage & Decrypt
                        </button>
                        <button type="button" onclick="window.StagingManager.confirmDiscardRepo('${escapeJsString(rf)}', ${totalVideos}, '${escapeJsString(sizeStr)}')" class="staging-btn-discard">
                            🗑 Discard
                        </button>
                    </div>
                </div>
                <div style="font-size: 0.82rem; color: #fca5a5; margin-top: 6px;">
                    Target: <code>${escapeHtml(repoId)}</code> &bull; Initial Root Scan: <strong>${r.initial_scanned_count || totalVideos}</strong> file(s) (${r.initial_scanned_formatted || sizeStr}) &bull; Staged on disk: <strong>${totalVideos}</strong> file(s) (${sizeStr}). Root scan and staged contents do not match!
                </div>
                ${(r.discrepancies || []).length > 0 ? `
                <div style="margin-top: 8px; background: rgba(0, 0, 0, 0.4); border: 1px solid rgba(239, 68, 68, 0.25); border-radius: 6px; padding: 8px 12px; font-size: 0.74rem; color: #fca5a5; font-family: ui-monospace, monospace; max-height: 120px; overflow-y: auto;">
                    ${r.discrepancies.map(d => `<div style="padding: 2px 0;">&bull; ${escapeHtml(d)}</div>`).join('')}
                </div>` : ''}
            </div>`;
        }

        // 6. Push Error State
        if (isError) {
            const errMsg = prog.message || r.error || 'Push encountered an error';
            return `
            <div class="staging-card-ready" style="border-color: rgba(239, 68, 68, 0.5); background: rgba(239, 68, 68, 0.15);">
                <div class="staging-card-top">
                    <div class="staging-card-title-group">
                        <span style="font-size: 1.15rem;">❌</span>
                        <span class="repo-name" style="color: #f87171;">Push Error: ${escapeHtml(rf)}</span>
                        <span class="staging-pill-ready" style="background: rgba(239, 68, 68, 0.3); color: #fca5a5; border-color: rgba(239, 68, 68, 0.5);">
                            Push Interrupted
                        </span>
                    </div>
                    <div class="staging-actions">
                        <button type="button" data-repo="${escapeHtml(rf)}" onclick="window.StagingManager.pushStagedRepo(this.dataset.repo)" class="staging-btn-push" style="background: #ef4444;" title="Retry Push from current batch">
                            🔄 Retry Push
                        </button>
                        <button type="button" onclick="window.StagingManager.confirmDiscardRepo('${escapeJsString(rf)}', ${totalVideos}, '${escapeJsString(sizeStr)}')" class="staging-btn-discard">
                            🗑 Discard
                        </button>
                    </div>
                </div>
                <div style="font-size: 0.80rem; color: #fca5a5; margin-top: 6px; font-family: ui-monospace, monospace; word-break: break-all; background: rgba(0,0,0,0.4); padding: 8px 12px; border-radius: 6px;">
                    ${escapeHtml(errMsg)}
                </div>
                ${buildGitConsoleHtml(rf, prog, sizeStr, totalVideos, false)}
            </div>`;
        }

        return '';
    }

    /**
     * Card 2: Active Pre-encryption / Staging Queue Card
     */
    function renderStagingPreCard(r) {
        const rf = r.repo_folder || '';
        const totalExp = r.initial_scanned_count || r.file_count || 1;
        const fileCount = r.file_count || 0;
        const pct = totalExp > 0 ? Math.min(100, Math.round((fileCount / totalExp) * 100)) : 0;
        const sizeFormatted = r.total_size_formatted || r.initial_scanned_formatted || '0 MB';
        const isPaused = r.is_paused || false;

        return `
        <div class="staging-card-pre">
            <div class="staging-card-pre-top">
                <div>
                    <div class="staging-pre-title">
                        <span>⚙</span>
                        <span>Pre-encrypting Folder: ${escapeHtml(rf)}</span>
                    </div>
                    <div class="staging-pre-subtext">
                        ${fileCount} file(s) staged (${escapeHtml(sizeFormatted)}) • ${pct}% completed
                    </div>
                </div>
                <div class="staging-pre-actions">
                    ${isPaused ? `
                    <button type="button" onclick="window.StagingManager.resumeStagingRepo('${escapeJsString(rf)}')" class="staging-btn-pause" style="color: #4ade80; border-color: rgba(74, 222, 128, 0.4); background: rgba(74, 222, 128, 0.15);" title="Resume pre-encryption for this folder">
                        ▶ Resume Staging
                    </button>
                    <span class="staging-pill-encrypting" style="color: #94a3b8; border-color: rgba(148, 163, 184, 0.3); background: rgba(148, 163, 184, 0.1);">
                        ⏸ Paused
                    </span>
                    ` : `
                    <button type="button" onclick="window.StagingManager.pauseStagingRepo('${escapeJsString(rf)}')" class="staging-btn-pause" title="Pause pre-encryption for this folder">
                        ⏸ Pause Staging
                    </button>
                    <span class="staging-pill-encrypting">
                        Encrypting ${pct}%
                    </span>
                    `}
                </div>
            </div>
            <div class="staging-bar-pre-track">
                <div class="staging-bar-pre-fill" style="width: ${Math.max(2, pct)}%;"></div>
            </div>
        </div>`;
    }

    /**
     * Render the Staging Section on the dashboard page
     */
    function renderStagingSection(data) {
        lastStagingData = data;
        const container = document.getElementById('stagingPushSection');
        if (!container) return;

        const readyRepos = data.ready_repos || [];
        const stagingRepos = data.staging_repos || [];
        const pushingRepos = data.pushing_repos || [];
        const pushedRepos = data.pushed_repos || [];
        const failedAuditRepos = data.failed_audit_repos || [];
        const errorRepos = data.error_repos || [];

        const totalActive = readyRepos.length + stagingRepos.length + pushingRepos.length + pushedRepos.length + failedAuditRepos.length + errorRepos.length;

        if (totalActive === 0) {
            container.style.display = 'none';
            container.innerHTML = '';
            return;
        }

        container.style.display = 'block';

        let html = `
        <div class="staging-queue-wrapper">
            <div class="staging-queue-header">
                <div class="staging-queue-header-left">
                    <span class="queue-icon">⚡</span>
                    <div>
                        <div class="queue-title">
                            Staging & 1-Commit Push Queue
                            ${readyRepos.length > 0 ? `<span style="background: #0284c7; color: #fff; font-size: 0.72rem; font-weight: 700; padding: 2px 8px; border-radius: 9999px;">${readyRepos.length} Ready</span>` : ''}
                        </div>
                        <div class="queue-subtitle">
                            Videos are pre-encrypted folder-wise with flat disk usage. Approve to commit all assets to Hugging Face in one go.
                        </div>
                    </div>
                </div>
                ${readyRepos.length > 1 ? `
                <button type="button" onclick="window.StagingManager.pushAllStagedRepos()" class="staging-btn-push-all">
                    🚀 Push All Ready (${readyRepos.length})
                </button>` : ''}
            </div>

            <div class="staging-queue-list">
        `;

        // 1. Error Repos
        errorRepos.forEach(r => { html += renderStagingRepoCard(r, false); });

        // 2. Failed Audit Discrepancies
        failedAuditRepos.forEach(r => { html += renderStagingRepoCard(r, false); });

        // 3. Pushing Repos
        pushingRepos.forEach(r => { html += renderStagingRepoCard(r, false); });

        // 4. Ready Repos (Card 1)
        readyRepos.forEach(r => { html += renderStagingRepoCard(r, false); });

        // 5. Active Pre-encryption folders (Card 2)
        stagingRepos.forEach(r => { html += renderStagingPreCard(r); });

        // 6. Pushed Repos
        pushedRepos.forEach(r => { html += renderStagingRepoCard(r, false); });

        html += `
            </div>
        </div>`;

        container.innerHTML = html;
    }

    /**
     * Staging Overlay Modal Renderer (Staging Hub)
     */
    function renderStagingOverlay(data) {
        lastStagingData = data;
        const content = document.getElementById('stagingOverlayContent');
        const countBadge = document.getElementById('stagingOverlayCountBadge');
        const footerActions = document.getElementById('stagingOverlayFooterActions');
        if (!content) return;

        const readyRepos = data.ready_repos || [];
        const stagingRepos = data.staging_repos || [];
        const pushingRepos = data.pushing_repos || [];
        const pushedRepos = data.pushed_repos || [];
        const failedAuditRepos = data.failed_audit_repos || [];
        const errorRepos = data.error_repos || [];

        if (countBadge) {
            if (failedAuditRepos.length > 0) {
                countBadge.innerText = `${failedAuditRepos.length} Flaw Detected`;
                countBadge.className = 'status-pill status-offline';
                countBadge.style.background = '#ef4444';
            } else if (pushingRepos.length > 0) {
                countBadge.innerText = 'Committing to HF...';
                countBadge.className = 'status-pill status-active';
                countBadge.style.background = '#0284c7';
            } else if (readyRepos.length > 0) {
                countBadge.innerText = `${readyRepos.length} Ready`;
                countBadge.className = 'status-pill status-active';
                countBadge.style.background = '#10b981';
            } else {
                countBadge.innerText = `${stagingRepos.length} Staging`;
                countBadge.className = 'status-pill status-idle';
                countBadge.style.background = '#f59e0b';
            }
        }

        let html = '';
        errorRepos.forEach(r => { html += renderStagingRepoCard(r, true); });
        failedAuditRepos.forEach(r => { html += renderStagingRepoCard(r, true); });
        pushingRepos.forEach(r => { html += renderStagingRepoCard(r, true); });
        readyRepos.forEach(r => { html += renderStagingRepoCard(r, true); });
        stagingRepos.forEach(r => { html += renderStagingPreCard(r); });
        pushedRepos.forEach(r => { html += renderStagingRepoCard(r, true); });

        if (!html) {
            html = `
            <div style="text-align: center; padding: 48px 20px; color: #94a3b8;">
                <div style="font-size: 2.8rem; margin-bottom: 14px;">📁</div>
                <div style="font-size: 1.15rem; font-weight: 700; color: #e2e8f0;">No Active Staging Batch</div>
                <div style="font-size: 0.88rem; color: #64748b; margin-top: 6px;">
                    Drop folders with video files into <code>videos_to_sync/</code> to automatically begin folder-wise pre-encryption.
                </div>
            </div>`;
        }

        content.innerHTML = html;

        if (footerActions) {
            let footerHtml = '';
            if (readyRepos.length > 1) {
                footerHtml += `
                <button type="button" onclick="window.StagingManager.pushAllStagedRepos()" class="staging-btn-push-all">
                    🚀 Push All Ready (${readyRepos.length})
                </button>`;
            }
            footerHtml += `<button type="button" onclick="window.StagingManager.closeStagingOverlay()" class="tool-btn" style="padding: 7px 20px; border-radius: 8px;">Close</button>`;
            footerActions.innerHTML = footerHtml;
        }
    }

    /**
     * Floating Trigger and Header Badge updates
     */
    function updateStagingFloatingTrigger(data) {
        const trigger = document.getElementById('stagingFloatingTrigger');
        const badge = document.getElementById('stagingFloatingBadge');
        const headerBadge = document.getElementById('headerStagingCountBadge');

        const readyCount = (data.ready_repos || []).length;
        const stagingCount = (data.staging_repos || []).length;
        const pushingCount = (data.pushing_repos || []).length;
        const failedCount = (data.failed_audit_repos || []).length;
        const errorCount = (data.error_repos || []).length;
        const total = readyCount + stagingCount + pushingCount + failedCount + errorCount;

        if (headerBadge) {
            if (total > 0) {
                headerBadge.innerText = readyCount > 0 ? readyCount : total;
                headerBadge.style.display = 'inline-block';
                headerBadge.style.background = readyCount > 0 ? '#10b981' : (failedCount > 0 ? '#ef4444' : '#0284c7');
            } else {
                headerBadge.style.display = 'none';
            }
        }

        if (trigger) {
            if (total > 0) {
                trigger.style.display = 'flex';
                if (badge) {
                    if (failedCount > 0) {
                        badge.innerText = failedCount + ' Flaw';
                        badge.style.background = '#ef4444';
                    } else if (pushingCount > 0) {
                        badge.innerText = 'Pushing...';
                        badge.style.background = '#0284c7';
                    } else if (readyCount > 0) {
                        badge.innerText = readyCount + ' Ready';
                        badge.style.background = '#10b981';
                    } else {
                        badge.innerText = stagingCount + ' Staging';
                        badge.style.background = '#f59e0b';
                    }
                    badge.style.display = 'inline-block';
                }
            } else {
                trigger.style.display = 'none';
            }
        }
    }

    /**
     * Staging API Operations
     */
    async function fetchStagingStatus() {
        try {
            const res = await fetch('/api/staging/status');
            if (!res.ok) return;
            const data = await res.json();
            renderStagingSection(data);
            renderStagingOverlay(data);
            updateStagingFloatingTrigger(data);
        } catch (e) {
            console.debug('[Staging] fetch error:', e);
        }
    }

    async function pushStagedRepo(repoFolder) {
        try {
            showToast(`Initiating 1-Commit Push for ${repoFolder}...`);
            const res = await fetch('/api/staging/push', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_folder: repoFolder })
            });
            const d = await res.json();
            if (d && d.success) {
                showToast(`Push initiated for ${repoFolder}!`);
            } else {
                showToast(`Push failed: ${d.error || 'unknown error'}`);
            }
            fetchStagingStatus();
        } catch (e) {
            showToast(`Push error: ${e.message}`);
        }
    }

    async function pushAllStagedRepos() {
        try {
            showToast('Initiating Push for all ready repositories...');
            const res = await fetch('/api/staging/push-all', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            });
            const d = await res.json();
            if (d && d.success) {
                showToast('All ready repositories pushed!');
            } else {
                showToast(`Push all failed: ${d.error || 'unknown'}`);
            }
            fetchStagingStatus();
        } catch (e) {
            showToast(`Push all error: ${e.message}`);
        }
    }

    function confirmRevertRepo(repoFolder, fileCount, sizeStr) {
        showCustomModal({
            title: '⏪ Unstage & Decrypt',
            message: `Are you sure you want to <strong>unstage and decrypt</strong> <code style="color: #fbbf24;">${escapeHtml(repoFolder)}</code> (${fileCount} files, ${sizeStr})?<br><br>The pre-encrypted files will be safely decrypted back to the source folder and marked as ignored from auto-staging.`,
            confirmText: 'Unstage & Decrypt',
            confirmStyle: 'background: #d97706; color: white;',
            onConfirm: function () {
                revertStagedRepo(repoFolder);
            }
        });
    }

    async function revertStagedRepo(repoFolder) {
        try {
            showToast(`⏪ Unstaging & decrypting repo: ${repoFolder}...`);
            const res = await fetch('/api/staging/revert', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_folder: repoFolder })
            });
            const d = await res.json();
            if (d && d.success) {
                showToast(`Unstaged & decrypted ${repoFolder}`);
            } else {
                showToast(`Unstage failed: ${d.message || d.error || 'unknown'}`);
            }
            fetchStagingStatus();
        } catch (e) {
            showToast(`Unstage error: ${e.message}`);
        }
    }

    function confirmDiscardRepo(repoFolder, fileCount, sizeStr) {
        showCustomModal({
            title: '🗑️ Discard Staged Files',
            message: `Are you sure you want to <strong>permanently discard</strong> staged files for <code style="color: #f87171;">${escapeHtml(repoFolder)}</code> (${fileCount} files, ${sizeStr})?<br><br>The staged encrypted copies will be permanently deleted from disk.`,
            confirmText: 'Discard Staging',
            confirmStyle: 'background: #dc2626; color: white;',
            onConfirm: function () {
                discardStagedRepo(repoFolder);
            }
        });
    }

    async function discardStagedRepo(repoFolder) {
        try {
            showToast(`🗑️ Discarding staged repo: ${repoFolder}...`);
            const res = await fetch('/api/staging/discard', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_folder: repoFolder })
            });
            const d = await res.json();
            if (d && d.success) {
                showToast(`Discarded ${repoFolder}`);
            } else {
                showToast(`Discard failed: ${d.error || 'unknown'}`);
            }
            fetchStagingStatus();
        } catch (e) {
            showToast(`Discard error: ${e.message}`);
        }
    }

    async function pauseStagingRepo(repoFolder) {
        try {
            showToast(`⏸ Pausing staging for ${repoFolder}...`);
            const res = await fetch('/api/staging/pause-staging', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_folder: repoFolder })
            });
            await res.json();
            showToast('Staging paused.');
            fetchStagingStatus();
        } catch (err) {
            showToast(`Error pausing: ${err.message}`);
        }
    }

    async function resumeStagingRepo(repoFolder) {
        try {
            showToast(`▶ Resuming staging for ${repoFolder}...`);
            const res = await fetch('/api/staging/resume-staging', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_folder: repoFolder })
            });
            await res.json();
            showToast('Staging resumed.');
            fetchStagingStatus();
        } catch (err) {
            showToast(`Error resuming: ${err.message}`);
        }
    }

    async function pauseRepoPush(repoFolder) {
        try {
            showToast(`⏸ Pausing push for ${repoFolder}...`);
            const res = await fetch('/api/staging/pause', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_folder: repoFolder })
            });
            await res.json();
            showToast('Push paused.');
            fetchStagingStatus();
        } catch (err) {
            showToast(`Error pausing push: ${err.message}`);
        }
    }

    async function resumeRepoPush(repoFolder) {
        try {
            showToast(`▶ Resuming push for ${repoFolder}...`);
            const res = await fetch('/api/staging/resume', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_folder: repoFolder })
            });
            await res.json();
            showToast('Push resumed.');
            fetchStagingStatus();
        } catch (err) {
            showToast(`Error resuming push: ${err.message}`);
        }
    }

    async function dismissPushedRepo(repoFolder) {
        try {
            const res = await fetch('/api/staging/dismiss', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_folder: repoFolder })
            });
            const d = await res.json();
            if (d && d.success) {
                showToast(`Dismissed ${repoFolder}`);
            }
            fetchStagingStatus();
        } catch (e) {
            showToast(`Dismiss error: ${e.message}`);
        }
    }

    function openStagingOverlay() {
        const modal = document.getElementById('stagingOverlayModal');
        if (modal) {
            modal.style.display = 'flex';
            fetchStagingStatus();
        }
    }

    function closeStagingOverlay() {
        const modal = document.getElementById('stagingOverlayModal');
        if (modal) modal.style.display = 'none';
    }

    // Public Module API
    window.StagingManager = {
        fetchStagingStatus: fetchStagingStatus,
        renderStagingRepoCard: renderStagingRepoCard,
        renderStagingPreCard: renderStagingPreCard,
        renderStagingSection: renderStagingSection,
        renderStagingOverlay: renderStagingOverlay,
        updateStagingFloatingTrigger: updateStagingFloatingTrigger,
        pushStagedRepo: pushStagedRepo,
        pushAllStagedRepos: pushAllStagedRepos,
        confirmRevertRepo: confirmRevertRepo,
        revertStagedRepo: revertStagedRepo,
        confirmDiscardRepo: confirmDiscardRepo,
        discardStagedRepo: discardStagedRepo,
        pauseStagingRepo: pauseStagingRepo,
        resumeStagingRepo: resumeStagingRepo,
        pauseRepoPush: pauseRepoPush,
        resumeRepoPush: resumeRepoPush,
        dismissPushedRepo: dismissPushedRepo,
        togglePushLog: togglePushLog,
        openStagingOverlay: openStagingOverlay,
        closeStagingOverlay: closeStagingOverlay
    };

    // Backward-compatibility global window hooks
    window.renderStagingSection = renderStagingSection;
    window.renderStagingOverlay = renderStagingOverlay;
    window.renderStagingRepoCard = renderStagingRepoCard;
    window.pushStagedRepo = pushStagedRepo;
    window.pushAllStagedRepos = pushAllStagedRepos;
    window.confirmRevertRepo = confirmRevertRepo;
    window.revertStagedRepo = revertStagedRepo;
    window.confirmDiscardRepo = confirmDiscardRepo;
    window.discardStagedRepo = discardStagedRepo;
    window.pauseStagingRepo = pauseStagingRepo;
    window.resumeStagingRepo = resumeStagingRepo;
    window.pauseRepoPush = pauseRepoPush;
    window.resumeRepoPush = resumeRepoPush;
    window.dismissPushedRepo = dismissPushedRepo;
    window.togglePushLog = togglePushLog;
    window.openStagingOverlay = openStagingOverlay;
    window.closeStagingOverlay = closeStagingOverlay;
    window.updateStagingFloatingTrigger = updateStagingFloatingTrigger;

    // Close modals on Escape key
    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            closeStagingOverlay();
            const actionModal = document.getElementById('stagingActionModal');
            if (actionModal) actionModal.style.display = 'none';
        }
    });

    // Start periodic polling on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            fetchStagingStatus();
            if (!stagingPollTimer) {
                stagingPollTimer = setInterval(fetchStagingStatus, 2500);
            }
        });
    } else {
        fetchStagingStatus();
        if (!stagingPollTimer) {
            stagingPollTimer = setInterval(fetchStagingStatus, 2500);
        }
    }
})(window);
