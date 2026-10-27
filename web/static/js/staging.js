/**
 * Staging & Push Hub - Modern Minimalist Flat Frontend Script
 * Compact, high-density, flat card rendering with zero redundant info
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
     * Sleek confirmation modal
     */
    function showCustomModal(opts) {
        let modal = document.getElementById('stagingActionModal');
        if (!modal) {
            modal = document.createElement('div');
            modal.id = 'stagingActionModal';
            modal.className = 'staging-modal-overlay';
            modal.innerHTML = `
            <div class="staging-modal-box" style="max-width: 480px; width: 100%;">
                <div class="staging-modal-header">
                    <div id="stagingModalTitle" style="font-weight: 700; font-size: 0.95rem; color: #f8fafc;">Confirm</div>
                    <button type="button" onclick="document.getElementById('stagingActionModal').style.display='none'" class="btn-flat-dismiss" style="font-size: 1.1rem; line-height: 1;">&times;</button>
                </div>
                <div id="stagingModalMessage" style="padding: 16px 18px; font-size: 0.86rem; color: #cbd5e1; line-height: 1.5;"></div>
                <div class="staging-modal-footer" style="justify-content: flex-end; gap: 8px;">
                    <button type="button" id="stagingModalCancelBtn" class="btn-flat" style="padding: 5px 14px;">Cancel</button>
                    <button type="button" id="stagingModalConfirmBtn" class="btn-flat" style="padding: 5px 16px; font-weight: 600;">Confirm</button>
                </div>
            </div>`;
            document.body.appendChild(modal);
            modal.addEventListener('click', function (e) {
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
            confirmBtn.style.cssText = (opts.confirmStyle || 'background: #0284c7; color: white;') + '; padding: 5px 16px; border-radius: 4px; font-weight: 600; cursor: pointer; border: none;';
            confirmBtn.onclick = function () {
                modal.style.display = 'none';
                if (typeof opts.onConfirm === 'function') opts.onConfirm();
            };
        }
        if (cancelBtn) {
            cancelBtn.onclick = function () {
                modal.style.display = 'none';
            };
        }
        modal.style.display = 'flex';
    }

    /**
     * Compact Live Git Console box
     */
    function buildGitConsoleHtml(repoFolder, pushProgress, isCompleted) {
        const prog = pushProgress || {};
        const safeClass = 'push-log-box-' + repoFolder.replace(/[^a-zA-Z0-9_-]/g, '_');
        const isExpanded = Boolean(activePushLogExpanded[repoFolder]);
        const logLines = prog.log_lines || [];
        const logCount = logLines.length;

        if (!isExpanded) return `<div id="${safeClass}" style="display: none;"></div>`;

        const logsHtml = logCount > 0 ? logLines.map(line => {
            let color = '#94a3b8';
            if (line.includes('✓') || line.includes('SUCCESS') || line.includes('accepted')) color = '#4ade80';
            else if (line.includes('Uploading') || line.includes('Pushing')) color = '#38bdf8';
            else if (line.includes('Error') || line.includes('Failed')) color = '#f87171';
            return `<div style="color: ${color}; padding: 1px 0;">${escapeHtml(line)}</div>`;
        }).join('') : '<div style="color: #64748b;">Connecting to Git LFS stream...</div>';

        return `
        <div id="${safeClass}" class="staging-console-box">
            ${logsHtml}
        </div>
        `;
    }

    function togglePushLog(repoFolder) {
        activePushLogExpanded[repoFolder] = !activePushLogExpanded[repoFolder];
        if (lastStagingData) {
            renderStagingSection(lastStagingData);
            renderStagingOverlay(lastStagingData);
        }
    }

    /**
     * Builds the Realtime Clickable Commit Strip & Batch Pills
     */
    function buildCommitsStripHtml(r, prog, state) {
        const cardCommits = prog.commits || r.commits || [];
        const hfDatasetUrl = 'https://huggingface.co/datasets/' + encodeURIComponent(r.repo_id || 'Vault');
        const commitsUrl = hfDatasetUrl + '/commits/main';
        const totalBatches = prog.total_batches || r.total_batches || 1;
        const curBatch = prog.current_batch || 1;

        let pillsHtml = '';

        // Render all completed commits as clickable emerald links
        if (cardCommits.length > 0) {
            pillsHtml += cardCommits.map(c => {
                const batchNum = c.batch != null ? c.batch : 1;
                const sizeStr = c.size_formatted ? ` ${c.size_formatted}` : '';
                const cUrl = c.commit_url || (commitsUrl);
                const title = `Git Commit for Batch #${batchNum}${c.size_formatted ? ' (' + c.size_formatted + ')' : ''} - Click to open commit diff on Hugging Face`;
                return `<a href="${escapeHtml(cUrl)}" target="_blank" rel="noopener noreferrer" class="staging-batch-pill staging-batch-pill-done" title="${escapeHtml(title)}">#${batchNum}${escapeHtml(sizeStr)} ↗</a>`;
            }).join('');
        }

        // State-specific in-progress / paused / error pill
        if (state === 'pushing') {
            const alreadyDone = cardCommits.some(c => c.batch === curBatch);
            if (!alreadyDone && curBatch <= totalBatches) {
                pillsHtml += `<span class="staging-batch-pill staging-batch-pill-active" title="Currently uploading Batch #${curBatch} of ${totalBatches}">#${curBatch} Uploading ⚡</span>`;
            }
        } else if (state === 'paused') {
            const alreadyDone = cardCommits.some(c => c.batch === curBatch);
            if (!alreadyDone && curBatch <= totalBatches) {
                pillsHtml += `<span class="staging-batch-pill staging-batch-pill-paused" title="Push paused at Batch #${curBatch} of ${totalBatches}">#${curBatch} Paused ⏸</span>`;
            }
        } else if (state === 'error') {
            const alreadyDone = cardCommits.some(c => c.batch === curBatch);
            if (!alreadyDone && curBatch <= totalBatches) {
                pillsHtml += `<span class="staging-batch-pill staging-batch-pill-error" title="Push error at Batch #${curBatch} of ${totalBatches}">#${curBatch} Failed ⚠️</span>`;
            }
        } else if (state === 'ready') {
            if (cardCommits.length === 0 && totalBatches > 1) {
                pillsHtml += `<span class="staging-batch-pill staging-batch-pill-pending" title="Repository partitioned into ${totalBatches} commits (~10 GB each)">${totalBatches} Batches (~10 GB each)</span>`;
                pillsHtml += `<a href="${commitsUrl}" target="_blank" rel="noopener noreferrer" class="staging-batch-pill staging-batch-pill-pending" title="View commit history on Hugging Face">Commits ↗</a>`;
            } else if (cardCommits.length === 0) {
                pillsHtml += `<span class="staging-batch-pill staging-batch-pill-pending" title="Single atomic commit (< 10 GB)">1-Commit Atomic</span>`;
            }
        }

        if (!pillsHtml) return '';

        const labelText = cardCommits.length > 0
            ? `Commits (${cardCommits.length}/${totalBatches}):`
            : `Commits:`;

        return `
        <div class="staging-commits-strip">
            <span class="staging-commits-title">${labelText}</span>
            <div class="staging-commits-pills">
                ${pillsHtml}
            </div>
        </div>`;
    }

    /**
     * Compact Flat Card: Ready / Pushing / Pushed / Error Staged Repo
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

        const estBatches = r.total_batches || prog.total_batches || Math.max(1, Math.ceil(totalBytes / (10 * 1024 * 1024 * 1024)));
        const pushBtnLabel = estBatches > 1 ? (`Push (${estBatches} × ~10GB)`) : 'Push to HF';
        const gitText = estBatches > 1 ? (`${estBatches} Commits (~10GB)`) : '1-Commit Atomic';

        const isPaused = r.status === 'paused';
        const isQueued = r.status === 'queued';
        const isPushing = r.status === 'pushing' || (prog.percent != null && r.status !== 'ready_to_push' && !isPaused && !isQueued && r.status !== 'error' && r.status !== 'pushed');
        const isPushed = r.status === 'pushed';
        const isError = r.status === 'error';
        const isFailedAudit = r.status === 'failed_audit' || r.status === 'staging_audit_failed';

        const hfDatasetUrl = 'https://huggingface.co/datasets/' + encodeURIComponent(repoId);
        const commitsUrl = hfDatasetUrl + '/commits/main';
        const displayTitle = account ? (account + ' / ' + rawRepoName) : repoId;
        const cardCommits = prog.commits || r.commits || [];
        const avatarHtml = thumbs.length > 0
            ? `<img src="${escapeHtml(thumbs[0])}" class="staging-avatar-mini" alt="" onerror="this.style.display='none'">`
            : '';

        // 1. Ready to Push (Compact 2-Row Design)
        if (!isPushing && !isPaused && !isQueued && !isPushed && !isError && !isFailedAudit) {
            return `
            <div class="staging-card-flat">
                <div class="staging-row-main">
                    <div class="staging-title-cluster">
                        <span class="repo-icon">📦</span>
                        <a href="${hfDatasetUrl}" target="_blank" rel="noopener noreferrer" class="staging-repo-link" title="Open repository on Hugging Face">${escapeHtml(displayTitle)} ↗</a>
                        <span class="staging-folder-mono" title="${escapeHtml(rf)}">(${escapeHtml(rf)})</span>
                        <span class="staging-badge staging-badge-ready">Ready</span>
                    </div>
                    <div class="staging-actions-compact">
                        <a href="/discover?staged_repo=${encodeURIComponent(rf)}" class="btn-flat btn-flat-discover" title="Preview staged videos in Discover feed">👁 Discover ↗</a>
                        <button type="button" onclick="window.StagingManager.confirmRevertRepo('${escapeJsString(rf)}', ${totalVideos}, '${escapeJsString(sizeStr)}')" class="btn-flat btn-flat-revert" title="Unstage and decrypt back to source">⏪ Unstage</button>
                        <button type="button" onclick="window.StagingManager.confirmDiscardRepo('${escapeJsString(rf)}', ${totalVideos}, '${escapeJsString(sizeStr)}')" class="btn-flat btn-flat-discard" title="Permanently delete staged ciphertext">🗑 Discard</button>
                        <button type="button" data-repo="${escapeHtml(rf)}" onclick="window.StagingManager.pushStagedRepo(this.dataset.repo)" class="btn-flat btn-flat-push" title="Commit and upload to Hugging Face">🚀 ${pushBtnLabel}</button>
                    </div>
                </div>

                <div class="staging-track-slim">
                    <div class="staging-fill-slim staging-fill-emerald" style="width: 100%;"></div>
                </div>

                <div class="staging-row-meta">
                    <div class="staging-meta-items">
                        <span><strong>${totalVideos}</strong> videos (${totalAssets} assets)</span>
                        <span class="meta-sep">•</span>
                        <span><strong>${escapeHtml(sizeStr)}</strong></span>
                        <span class="meta-sep">•</span>
                        <span style="color: #10b981;">AES-256-GCM Verified ✓</span>
                        <span class="meta-sep">•</span>
                        <span>⚡ ${gitText}</span>
                    </div>
                    <div class="staging-meta-right">
                        ${account ? `<span style="display: inline-flex; align-items: center; gap: 4px; font-size: 0.72rem; color: #cbd5e1;">${avatarHtml}<span>${escapeHtml(account)}</span></span>` : ''}
                    </div>
                </div>
                ${buildCommitsStripHtml(r, prog, 'ready')}
            </div>`;
        }

        // 2. Pushing in Progress
        if (isPushing) {
            const pct = prog.percent != null ? prog.percent : 25;
            const batchBadge = prog.batch_info || (prog.total_batches > 1 ? (`Batch ${prog.current_batch || 1}/${prog.total_batches}`) : 'Committing');
            const logCount = (prog.log_lines || []).length;
            return `
            <div class="staging-card-flat state-pushing">
                <div class="staging-row-main">
                    <div class="staging-title-cluster">
                        <span class="status-spinner" style="display:inline-block;width:12px;height:12px;border:2px solid #38bdf8;border-top-color:transparent;border-radius:50%;animation:spin 1s linear infinite;"></span>
                        <a href="${hfDatasetUrl}" target="_blank" rel="noopener noreferrer" class="staging-repo-link">${escapeHtml(displayTitle)} ↗</a>
                        <span class="staging-folder-mono">(${escapeHtml(rf)})</span>
                        <span class="staging-badge staging-badge-pushing">${escapeHtml(batchBadge)} • ${pct}%</span>
                    </div>
                    <div class="staging-actions-compact">
                        <button type="button" onclick="window.StagingManager.togglePushLog('${escapeJsString(rf)}')" class="btn-flat" title="Toggle upload console logs">📋 Console (${logCount})</button>
                        <button type="button" onclick="window.StagingManager.pauseRepoPush('${escapeJsString(rf)}')" class="btn-flat btn-flat-revert" title="Pause sequential push">⏸ Pause</button>
                    </div>
                </div>

                <div class="staging-track-slim">
                    <div class="staging-fill-slim staging-fill-cyan" style="width: ${Math.max(4, pct)}%;"></div>
                </div>

                <div class="staging-row-meta">
                    <div class="staging-meta-items">
                        <span>${escapeHtml(prog.message || 'Pushing to Hugging Face...')}</span>
                        ${prog.current_file ? `<span class="staging-folder-mono">[${escapeHtml(prog.current_file)}]</span>` : ''}
                        <span class="meta-sep">•</span>
                        <span>${prog.uploaded_bytes_formatted || '0 MB'} / ${prog.total_bytes_formatted || sizeStr}</span>
                        ${prog.speed_str ? `<span class="meta-sep">•</span><span style="color: #4ade80;">⚡ ${escapeHtml(prog.speed_str)}</span>` : ''}
                    </div>
                    <div class="staging-meta-right">
                        ${account ? `<span style="display: inline-flex; align-items: center; gap: 4px; font-size: 0.72rem; color: #cbd5e1;">${avatarHtml}<span>${escapeHtml(account)}</span></span>` : ''}
                    </div>
                </div>
                ${buildCommitsStripHtml(r, prog, 'pushing')}
                ${buildGitConsoleHtml(rf, prog, false)}
            </div>`;
        }

        // 3. Paused Push State
        if (isPaused) {
            return `
            <div class="staging-card-flat state-paused">
                <div class="staging-row-main">
                    <div class="staging-title-cluster">
                        <span class="repo-icon">⏸</span>
                        <a href="${hfDatasetUrl}" target="_blank" class="staging-repo-link">${escapeHtml(displayTitle)} ↗</a>
                        <span class="staging-badge staging-badge-pre">Paused at Batch ${prog.current_batch || 1}/${prog.total_batches || estBatches}</span>
                    </div>
                    <div class="staging-actions-compact">
                        <button type="button" onclick="window.StagingManager.resumeRepoPush('${escapeJsString(rf)}')" class="btn-flat btn-flat-push">▶ Resume</button>
                        <button type="button" onclick="window.StagingManager.confirmDiscardRepo('${escapeJsString(rf)}', ${totalVideos}, '${escapeJsString(sizeStr)}')" class="btn-flat btn-flat-discard">🗑 Discard</button>
                    </div>
                </div>
                <div class="staging-row-meta">
                    <div class="staging-meta-items">
                        <span>${escapeHtml(prog.message || 'Push paused. Click Resume to continue.')}</span>
                    </div>
                    <div class="staging-meta-right">
                        ${account ? `<span style="display: inline-flex; align-items: center; gap: 4px; font-size: 0.72rem; color: #cbd5e1;">${avatarHtml}<span>${escapeHtml(account)}</span></span>` : ''}
                    </div>
                </div>
                ${buildCommitsStripHtml(r, prog, 'paused')}
                ${buildGitConsoleHtml(rf, prog, false)}
            </div>`;
        }

        // 4. Push Complete
        if (isPushed) {
            return `
            <div class="staging-card-flat state-pushed">
                <div class="staging-row-main">
                    <div class="staging-title-cluster">
                        <span style="color: #4ade80; font-weight: 700;">✓</span>
                        <a href="${hfDatasetUrl}" target="_blank" rel="noopener noreferrer" class="staging-repo-link">${escapeHtml(displayTitle)} ↗</a>
                        <span class="staging-folder-mono">(${escapeHtml(rf)})</span>
                        <span class="staging-badge staging-badge-pushed">Pushed</span>
                    </div>
                    <div class="staging-actions-compact">
                        <button type="button" onclick="window.StagingManager.togglePushLog('${escapeJsString(rf)}')" class="btn-flat" title="View push logs">📋 Logs</button>
                        <button type="button" onclick="window.StagingManager.dismissPushedRepo('${escapeJsString(rf)}')" class="btn-flat-dismiss" title="Dismiss this notification">✕ Dismiss</button>
                    </div>
                </div>
                <div class="staging-row-meta">
                    <div class="staging-meta-items">
                        <span>${prog.total_assets || totalAssets} assets (${sizeStr}) committed in ${estBatches} commit(s)</span>
                        ${prog.speed_str ? `<span class="meta-sep">•</span><span style="color: #10b981;">⚡ ${escapeHtml(prog.speed_str)}</span>` : ''}
                    </div>
                    <div class="staging-meta-right">
                        ${account ? `<span style="display: inline-flex; align-items: center; gap: 4px; font-size: 0.72rem; color: #cbd5e1;">${avatarHtml}<span>${escapeHtml(account)}</span></span>` : ''}
                    </div>
                </div>
                ${buildCommitsStripHtml(r, prog, 'pushed')}
                ${buildGitConsoleHtml(rf, prog, true)}
            </div>`;
        }

        // 5. Failed Audit Discrepancy
        if (isFailedAudit) {
            return `
            <div class="staging-card-flat state-error">
                <div class="staging-row-main">
                    <div class="staging-title-cluster">
                        <span style="color: #ef4444; font-weight: 700;">⚠️</span>
                        <span class="staging-repo-link" style="color: #f87171;">Audit Discrepancy: ${escapeHtml(rf)}</span>
                        <span class="staging-badge staging-badge-error">Push Blocked</span>
                    </div>
                    <div class="staging-actions-compact">
                        <button type="button" onclick="window.StagingManager.confirmRevertRepo('${escapeJsString(rf)}', ${totalVideos}, '${escapeJsString(sizeStr)}')" class="btn-flat btn-flat-revert">⏪ Unstage</button>
                        <button type="button" onclick="window.StagingManager.confirmDiscardRepo('${escapeJsString(rf)}', ${totalVideos}, '${escapeJsString(sizeStr)}')" class="btn-flat btn-flat-discard">🗑 Discard</button>
                    </div>
                </div>
                <div class="staging-row-meta">
                    <div class="staging-meta-items" style="color: #fca5a5;">
                        <span>Target: <code>${escapeHtml(repoId)}</code></span>
                        <span class="meta-sep">•</span>
                        <span>Scan: ${r.initial_scanned_count || totalVideos} files (${r.initial_scanned_formatted || sizeStr})</span>
                        <span class="meta-sep">•</span>
                        <span>Staged: ${totalVideos} files (${sizeStr})</span>
                    </div>
                </div>
                ${(r.discrepancies || []).length > 0 ? `
                <div style="margin-top: 4px; background: rgba(0,0,0,0.5); padding: 4px 8px; border-radius: 4px; font-family: ui-monospace, monospace; font-size: 0.68rem; color: #fca5a5;">
                    ${r.discrepancies.map(d => `<div>&bull; ${escapeHtml(d)}</div>`).join('')}
                </div>` : ''}
            </div>`;
        }

        // 6. Push Error
        if (isError) {
            const errMsg = prog.message || r.error || 'Push encountered an error';
            return `
            <div class="staging-card-flat state-error">
                <div class="staging-row-main">
                    <div class="staging-title-cluster">
                        <span style="color: #ef4444;">❌</span>
                        <span class="staging-repo-link" style="color: #f87171;">Push Error: ${escapeHtml(rf)}</span>
                        <span class="staging-badge staging-badge-error">Error</span>
                    </div>
                    <div class="staging-actions-compact">
                        <button type="button" data-repo="${escapeHtml(rf)}" onclick="window.StagingManager.pushStagedRepo(this.dataset.repo)" class="btn-flat" style="background: #ef4444; color: white; border: none;">🔄 Retry Push</button>
                        <button type="button" onclick="window.StagingManager.confirmDiscardRepo('${escapeJsString(rf)}', ${totalVideos}, '${escapeJsString(sizeStr)}')" class="btn-flat btn-flat-discard">🗑 Discard</button>
                    </div>
                </div>
                <div class="staging-row-meta">
                    <div class="staging-meta-items" style="color: #fca5a5;">
                        <span style="font-family: ui-monospace, monospace;">${escapeHtml(errMsg)}</span>
                    </div>
                </div>
                ${buildCommitsStripHtml(r, prog, 'error')}
                ${buildGitConsoleHtml(rf, prog, false)}
            </div>`;
        }

        return '';
    }

    /**
     * Compact Flat Card: Active Pre-encryption Folder
     */
    function renderStagingPreCard(r) {
        const rf = r.repo_folder || '';
        const totalExp = r.initial_scanned_count || r.file_count || 1;
        const fileCount = r.file_count || 0;
        const pct = totalExp > 0 ? Math.min(100, Math.round((fileCount / totalExp) * 100)) : 0;
        const sizeFormatted = r.total_size_formatted || r.initial_scanned_formatted || '0 MB';
        const isPaused = r.is_paused || false;

        return `
        <div class="staging-card-flat state-pre">
            <div class="staging-row-main">
                <div class="staging-title-cluster">
                    <span class="repo-icon">⚙</span>
                    <span class="staging-repo-link">Pre-encrypting: ${escapeHtml(rf)}</span>
                    <span class="staging-badge staging-badge-pre">${isPaused ? 'Paused' : 'Encrypting ' + pct + '%'}</span>
                </div>
                <div class="staging-actions-compact">
                    ${isPaused ? `
                    <button type="button" onclick="window.StagingManager.resumeStagingRepo('${escapeJsString(rf)}')" class="btn-flat" style="color: #4ade80;" title="Resume pre-encryption">▶ Resume</button>
                    ` : `
                    <button type="button" onclick="window.StagingManager.pauseStagingRepo('${escapeJsString(rf)}')" class="btn-flat" style="color: #fbbf24;" title="Pause pre-encryption">⏸ Pause</button>
                    `}
                </div>
            </div>

            <div class="staging-track-slim">
                <div class="staging-fill-slim staging-fill-gradient" style="width: ${Math.max(2, pct)}%;"></div>
            </div>

            <div class="staging-row-meta">
                <div class="staging-meta-items">
                    <span>${fileCount} of ${totalExp} files staged</span>
                    <span class="meta-sep">•</span>
                    <span>${escapeHtml(sizeFormatted)}</span>
                    <span class="meta-sep">•</span>
                    <span>${pct}% complete</span>
                </div>
            </div>
        </div>`;
    }

    /**
     * Dashboard Page Inline Section
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
                    <span class="queue-title">Staging Queue ${readyRepos.length > 0 ? `<span class="staging-badge staging-badge-ready">${readyRepos.length} Ready</span>` : ''}</span>
                    <span class="queue-subtitle">Pre-encrypted with flat disk usage. Commit to Hugging Face when ready.</span>
                </div>
                ${readyRepos.length > 1 ? `
                <button type="button" onclick="window.StagingManager.pushAllStagedRepos()" class="staging-btn-push-all">
                    🚀 Push All Ready (${readyRepos.length})
                </button>` : ''}
            </div>

            <div class="staging-queue-list">
        `;

        errorRepos.forEach(r => { html += renderStagingRepoCard(r, false); });
        failedAuditRepos.forEach(r => { html += renderStagingRepoCard(r, false); });
        pushingRepos.forEach(r => { html += renderStagingRepoCard(r, false); });
        readyRepos.forEach(r => { html += renderStagingRepoCard(r, false); });
        stagingRepos.forEach(r => { html += renderStagingPreCard(r); });
        pushedRepos.forEach(r => { html += renderStagingRepoCard(r, false); });

        html += `
            </div>
        </div>`;

        container.innerHTML = html;
    }

    /**
     * Staging Hub Overlay Modal Renderer
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
                countBadge.innerText = `${failedAuditRepos.length} Flaw`;
                countBadge.className = 'staging-badge staging-badge-error';
            } else if (pushingRepos.length > 0) {
                countBadge.innerText = 'Pushing...';
                countBadge.className = 'staging-badge staging-badge-pushing';
            } else if (readyRepos.length > 0) {
                countBadge.innerText = `${readyRepos.length} Ready`;
                countBadge.className = 'staging-badge staging-badge-ready';
            } else {
                countBadge.innerText = `${stagingRepos.length} Staging`;
                countBadge.className = 'staging-badge staging-badge-pre';
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
            <div style="text-align: center; padding: 36px 18px; color: #64748b;">
                <div style="font-size: 2.2rem; margin-bottom: 8px;">📁</div>
                <div style="font-size: 1rem; font-weight: 600; color: #e2e8f0;">No Active Staging Batch</div>
                <div style="font-size: 0.80rem; color: #64748b; margin-top: 4px;">
                    Drop folders with video files into <code>videos_to_sync/</code> to begin folder-wise pre-encryption.
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
            footerHtml += `<button type="button" onclick="window.StagingManager.closeStagingOverlay()" class="btn-flat" style="padding: 5px 16px;">Close</button>`;
            footerActions.innerHTML = footerHtml;
        }
    }

    /**
     * Floating Tab & Header Badge Synchronization
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
                    badge.innerText = readyCount > 0 ? readyCount + ' Ready' : total + ' Active';
                    badge.style.background = readyCount > 0 ? '#10b981' : (failedCount > 0 ? '#ef4444' : '#0284c7');
                }
            } else {
                trigger.style.display = 'none';
            }
        }
    }

    /**
     * Staging API Calls
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
            showToast(`Initiating Push for ${repoFolder}...`);
            const res = await fetch('/api/staging/push', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_folder: repoFolder })
            });
            const d = await res.json();
            if (d && d.success) {
                showToast(`Push started: ${repoFolder}`);
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
            message: `Unstage and decrypt <strong>${escapeHtml(repoFolder)}</strong> (${fileCount} files, ${sizeStr}) back to source directory?`,
            confirmText: 'Unstage',
            confirmStyle: 'background: #d97706; color: white;',
            onConfirm: function () {
                revertStagedRepo(repoFolder);
            }
        });
    }

    async function revertStagedRepo(repoFolder) {
        try {
            showToast(`⏪ Unstaging: ${repoFolder}...`);
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
            message: `Permanently delete staged ciphertext for <strong>${escapeHtml(repoFolder)}</strong> (${fileCount} files, ${sizeStr})?`,
            confirmText: 'Discard',
            confirmStyle: 'background: #dc2626; color: white;',
            onConfirm: function () {
                discardStagedRepo(repoFolder);
            }
        });
    }

    async function discardStagedRepo(repoFolder) {
        try {
            showToast(`🗑️ Discarding: ${repoFolder}...`);
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
            await fetch('/api/staging/pause-staging', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_folder: repoFolder })
            });
            showToast('Staging paused.');
            fetchStagingStatus();
        } catch (err) {
            showToast(`Error: ${err.message}`);
        }
    }

    async function resumeStagingRepo(repoFolder) {
        try {
            await fetch('/api/staging/resume-staging', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_folder: repoFolder })
            });
            showToast('Staging resumed.');
            fetchStagingStatus();
        } catch (err) {
            showToast(`Error: ${err.message}`);
        }
    }

    async function pauseRepoPush(repoFolder) {
        try {
            await fetch('/api/staging/pause', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_folder: repoFolder })
            });
            showToast('Push paused.');
            fetchStagingStatus();
        } catch (err) {
            showToast(`Error: ${err.message}`);
        }
    }

    async function resumeRepoPush(repoFolder) {
        try {
            await fetch('/api/staging/resume', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_folder: repoFolder })
            });
            showToast('Push resumed.');
            fetchStagingStatus();
        } catch (err) {
            showToast(`Error: ${err.message}`);
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

    // Backward-compatibility window hooks
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
    document.addEventListener('keydown', function (e) {
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
