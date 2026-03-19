/* ============================================
   AI Job Autopilot — Dashboard Logic
   ============================================
   Reads jobs.json (with live status from auto_apply.py)
   and application_log.json for detailed tracking.
   ============================================ */

const STORAGE_KEY = 'job_autopilot_data';
const PAGE_SIZE = 50;
const JOBS_PATH_CANDIDATES = ['jobs.json', './jobs.json', '../jobs.json', '/jobs.json'];
const LOG_PATH_CANDIDATES = ['application_log.json', './application_log.json', '../application_log.json', '/application_log.json'];
const RUNS_ENDPOINT = '/api/runs';
const JOB_STATUS_ENDPOINT = '/api/job-statuses';
const APPLY_ENDPOINT = '/api/apply';
const STOP_ENDPOINT = '/api/stop';

let allJobs = [];
let applicationLog = null;
let filteredJobs = [];
let currentPage = 1;
let sortField = 'age_days';
let sortAsc = true;
let selectedJobIds = new Set();
let applyRuns = [];
let dbStatusRows = [];

// Filters state
let filters = {
    search: '',
    category: 'all',
    type: 'all',
    region: 'all',
    status: 'all',
    maxAge: null,
};

// ============ INIT ============

document.addEventListener('DOMContentLoaded', () => {
    loadData();
    setupEventListeners();
    refreshRuns();
    refreshDbStatuses();
    setInterval(() => {
        refreshRuns();
        refreshDbStatuses();
    }, 3000);
});

async function fetchFirstJson(paths) {
    for (const p of paths) {
        try {
            const res = await fetch(`${p}?t=${Date.now()}`, { cache: 'no-store' });
            if (!res.ok) continue;
            const json = await res.json();
            return { data: json, path: p };
        } catch (_) { }
    }
    return { data: null, path: null };
}

async function loadData() {
    // Try multiple path variants so dashboard works whether served from repo root
    // (e.g. /dashboard/index.html) or from inside ./dashboard directly.
    const [jobsResult, logResult] = await Promise.all([
        fetchFirstJson(JOBS_PATH_CANDIDATES),
        fetchFirstJson(LOG_PATH_CANDIDATES),
    ]);

    const jobs = jobsResult.data;
    const log = logResult.data;

    if (jobs) {
        allJobs = jobs;
        applicationLog = log;

        // Merge log data into jobs for richer status info
        if (log) {
            mergeLogIntoJobs(log);
        }

        // Save to localStorage as backup
        localStorage.setItem(STORAGE_KEY, JSON.stringify(allJobs));
        applyFilters();
        return;
    }

    // Fallback to localStorage
    const cached = localStorage.getItem(STORAGE_KEY);
    if (cached) {
        try {
            allJobs = JSON.parse(cached);
            applyFilters();
            return;
        } catch (e) { }
    }

    // Show load modal as last resort
    document.getElementById('loadModal').style.display = 'flex';
}

function mergeLogIntoJobs(log) {
    // Build lookup maps from log
    const appliedMap = new Map();
    const failedMap = new Map();

    (log.applied || []).forEach(entry => {
        appliedMap.set(entry.job_id, entry);
    });
    (log.failed || []).forEach(entry => {
        failedMap.set(entry.job_id, entry);
    });
    (log.skipped || []).forEach(entry => {
        // skipped entries go into a "skipped" status
        failedMap.set(entry.job_id, { ...entry, status: 'skipped' });
    });

    // Update job statuses from log
    allJobs.forEach(job => {
        if (appliedMap.has(job.id)) {
            job.status = 'applied';
            job._logEntry = appliedMap.get(job.id);
        } else if (failedMap.has(job.id)) {
            const entry = failedMap.get(job.id);
            job.status = entry.status || 'failed';
            job._logEntry = entry;
        }
        // Keep existing status from jobs.json if not in log
        if (!job.status) job.status = 'pending';
    });
}

function normalizeUrl(url) {
    if (!url) return '';
    const trimmed = String(url).trim();
    if (!trimmed) return '';
    const noHash = trimmed.split('#')[0];
    const noQuery = noHash.split('?')[0];
    return noQuery.replace(/\/+$/, '').toLowerCase();
}

function backendStatusToUi(status) {
    const s = String(status || '').toLowerCase();
    if (s === 'applied') return 'applied';
    if (s === 'in_progress') return 'pending';
    if (s === 'failed' || s === 'manual' || s === 'captcha' || s === 'login_issue' || s === 'expired') return 'failed';
    return '';
}

function mergeRunStatusesIntoJobs() {
    if (!Array.isArray(allJobs) || allJobs.length === 0) return;
    const runMap = new Map();
    applyRuns.forEach(run => {
        const key = normalizeUrl(run.url);
        if (key) runMap.set(key, run);
    });

    allJobs.forEach(job => {
        const key = normalizeUrl(job.apply_url || job.url);
        if (!key || !runMap.has(key)) return;
        const run = runMap.get(key);
        const dbUiStatus = backendStatusToUi(run.db_apply_status);
        if (dbUiStatus) {
            job.status = dbUiStatus;
            if (run.db_apply_error) {
                job._logEntry = { reason: run.db_apply_error };
            }
            return;
        }
        if (run.state === 'running') {
            job.status = 'pending';
            return;
        }
        if (run.state === 'stopped') {
            if (!job.status || job.status === 'pending') {
                job.status = 'pending';
            }
            job._logEntry = { reason: 'Stopped by user' };
            return;
        }
        if (run.state === 'finished' && Number(run.returncode) !== 0) {
            job.status = 'failed';
            job._logEntry = { reason: `Pipeline error (exit ${run.returncode})` };
        }
    });
}

function mergeDbStatusesIntoJobs() {
    if (!Array.isArray(allJobs) || allJobs.length === 0 || !Array.isArray(dbStatusRows)) return;
    const statusMap = new Map();
    dbStatusRows.forEach(row => {
        const urlKey = normalizeUrl(row.url);
        const appKey = normalizeUrl(row.application_url);
        if (urlKey) statusMap.set(urlKey, row);
        if (appKey) statusMap.set(appKey, row);
    });

    allJobs.forEach(job => {
        const key = normalizeUrl(job.apply_url || job.url);
        if (!key || !statusMap.has(key)) return;
        const row = statusMap.get(key);
        const uiStatus = backendStatusToUi(row.apply_status);
        if (!uiStatus) return;
        job.status = uiStatus;
        if (row.apply_error) {
            job._logEntry = { reason: row.apply_error };
        }
    });
}

function saveData() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(allJobs));
}

// Auto-refresh every 10 seconds to pick up changes from auto_apply.py
setInterval(async () => {
    const jobsResult = await fetchFirstJson(JOBS_PATH_CANDIDATES);
    if (!jobsResult.data) return;

    allJobs = jobsResult.data;
    const logResult = await fetchFirstJson(LOG_PATH_CANDIDATES);
    if (logResult.data) mergeLogIntoJobs(logResult.data);
    applyFilters();
}, 10000);

// ============ EVENT LISTENERS ============

function setupEventListeners() {
    // Search
    document.getElementById('searchInput').addEventListener('input', (e) => {
        filters.search = e.target.value.toLowerCase();
        currentPage = 1;
        applyFilters();
    });

    // Chip filters
    document.querySelectorAll('.filter-chips').forEach(group => {
        group.addEventListener('click', (e) => {
            if (!e.target.classList.contains('chip')) return;
            const groupId = group.id;
            const value = e.target.dataset.value;

            // Update active chip
            group.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
            e.target.classList.add('active');

            // Update filter
            if (groupId === 'categoryFilters') filters.category = value;
            if (groupId === 'typeFilters') filters.type = value;
            if (groupId === 'regionFilters') filters.region = value;
            if (groupId === 'statusFilters') filters.status = value;

            currentPage = 1;
            applyFilters();
        });
    });

    // Age filter
    document.getElementById('ageFilter').addEventListener('change', (e) => {
        filters.maxAge = e.target.value === 'all' ? null : parseInt(e.target.value);
        currentPage = 1;
        applyFilters();
    });

    // Sort
    document.querySelectorAll('.sortable').forEach(th => {
        th.addEventListener('click', () => {
            const field = th.dataset.sort;
            if (sortField === field) {
                sortAsc = !sortAsc;
            } else {
                sortField = field;
                sortAsc = true;
            }
            applyFilters();
        });
    });

    // Select all
    document.getElementById('selectAll').addEventListener('change', (e) => {
        const start = (currentPage - 1) * PAGE_SIZE;
        const end = start + PAGE_SIZE;
        const pageJobs = filteredJobs.slice(start, end);
        pageJobs.forEach(j => {
            if (e.target.checked) selectedJobIds.add(j.id);
            else selectedJobIds.delete(j.id);
        });
        renderTable();
    });

    // Batch open
    document.getElementById('batchOpenBtn').addEventListener('click', batchOpen);

    // File input
    document.getElementById('fileInput').addEventListener('change', handleFileLoad);
    document.getElementById('cancelLoad').addEventListener('click', () => {
        document.getElementById('loadModal').style.display = 'none';
    });

    // Job View modal
    const closeBtn = document.getElementById('closeViewModal');
    if (closeBtn) {
        closeBtn.addEventListener('click', closeJobViewModal);
    }
    const viewModal = document.getElementById('jobViewModal');
    if (viewModal) {
        viewModal.addEventListener('click', (e) => {
            if (e.target === viewModal) closeJobViewModal();
        });
    }
}

// ============ FILTERING & SORTING ============

function applyFilters() {
    filteredJobs = allJobs.filter(job => {
        if (filters.search && !(
            job.company.toLowerCase().includes(filters.search) ||
            job.position.toLowerCase().includes(filters.search) ||
            job.location.toLowerCase().includes(filters.search)
        )) return false;

        if (filters.category !== 'all' && job.category !== filters.category) return false;
        if (filters.type !== 'all' && job.type !== filters.type) return false;
        if (filters.region !== 'all' && job.region !== filters.region) return false;
        if (filters.status !== 'all') {
            const jobStatus = job.status || 'pending';
            if (filters.status === 'pending' && jobStatus !== 'pending') return false;
            if (filters.status === 'applied' && jobStatus !== 'applied') return false;
            if (filters.status === 'skipped' && jobStatus !== 'skipped') return false;
            if (filters.status === 'failed' && jobStatus !== 'failed' && jobStatus !== 'uncertain' && jobStatus !== 'manual' && jobStatus !== 'captcha' && jobStatus !== 'login_issue' && jobStatus !== 'expired') return false;
        }
        if (filters.maxAge && job.age_days && job.age_days > filters.maxAge) return false;

        return true;
    });

    // Sort
    filteredJobs.sort((a, b) => {
        let va = a[sortField];
        let vb = b[sortField];
        if (va == null) va = sortAsc ? Infinity : -Infinity;
        if (vb == null) vb = sortAsc ? Infinity : -Infinity;
        if (typeof va === 'string') {
            va = va.toLowerCase();
            vb = (vb || '').toLowerCase();
        }
        if (va < vb) return sortAsc ? -1 : 1;
        if (va > vb) return sortAsc ? 1 : -1;
        return 0;
    });

    updateStats();
    renderTable();
    renderPagination();
}

// ============ RENDERING ============

function updateStats() {
    const applied = allJobs.filter(j => j.status === 'applied').length;
    const failed = allJobs.filter(j =>
        j.status === 'failed' ||
        j.status === 'uncertain' ||
        j.status === 'manual' ||
        j.status === 'captcha' ||
        j.status === 'login_issue' ||
        j.status === 'expired'
    ).length;
    const pending = allJobs.filter(j => !j.status || j.status === 'pending').length;

    document.getElementById('totalJobs').textContent = allJobs.length.toLocaleString();
    document.querySelector('#statApplied .stat-value').textContent = applied;
    document.querySelector('#statPending .stat-value').textContent = pending;
    document.querySelector('#statFailed .stat-value').textContent = failed;
    document.getElementById('filteredCount').textContent = filteredJobs.length.toLocaleString();
}

function renderTable() {
    const tbody = document.getElementById('jobsBody');
    const start = (currentPage - 1) * PAGE_SIZE;
    const end = start + PAGE_SIZE;
    const pageJobs = filteredJobs.slice(start, end);

    if (pageJobs.length === 0) {
        tbody.innerHTML = `
            <tr><td colspan="9">
                <div class="empty-state">
                    <div class="emoji">🔍</div>
                    <h3>No jobs match your filters</h3>
                    <p>Try adjusting your search or filters, or load a jobs.json file</p>
                </div>
            </td></tr>
        `;
        return;
    }

    tbody.innerHTML = pageJobs.map(job => {
        const status = job.status || 'pending';
        const isChecked = selectedJobIds.has(job.id);

        const categoryClass = job.category === 'FAANG+' ? 'category-faang' :
            job.category === 'Quant' ? 'category-quant' : 'category-other';

        const ageClass = job.age_days <= 7 ? 'age-fresh' :
            job.age_days <= 30 ? 'age-medium' : 'age-old';

        let statusClass, statusText;
        switch (status) {
            case 'applied':
                statusClass = 'status-applied';
                statusText = '✅ Applied';
                break;
            case 'failed':
                statusClass = 'status-failed';
                statusText = '❌ Failed';
                break;
            case 'manual':
                statusClass = 'status-failed';
                statusText = '⚠️ Manual';
                break;
            case 'captcha':
                statusClass = 'status-failed';
                statusText = '🧩 CAPTCHA';
                break;
            case 'login_issue':
                statusClass = 'status-failed';
                statusText = '🔐 Login Issue';
                break;
            case 'expired':
                statusClass = 'status-failed';
                statusText = '⌛ Expired';
                break;
            case 'uncertain':
                statusClass = 'status-uncertain';
                statusText = '⚠️ Uncertain';
                break;
            case 'skipped':
                statusClass = 'status-skipped';
                statusText = '⏭ Skipped';
                break;
            default:
                statusClass = 'status-pending';
                statusText = '⏳ Pending';
        }

        // Build tooltip with log details
        const logEntry = job._logEntry;
        let tooltip = '';
        if (logEntry) {
            const reason = logEntry.reason || logEntry.error || logEntry.result || '';
            tooltip = `title="${esc(reason.substring(0, 200))}"`;
        }

        const runStatus = getRunStatusText(job);
        const actionButton = runStatus.running
            ? `<button class="btn btn-sm btn-stop" onclick="stopApply(${job.id})">Stop</button>`
            : `<button class="btn btn-sm btn-pipeline" onclick="triggerApply(${job.id})">Apply</button>`;

        return `
            <tr data-id="${job.id}">
                <td style="text-align:center"><input type="checkbox" ${isChecked ? 'checked' : ''} onchange="toggleSelect(${job.id})"></td>
                <td>
                    <div class="company-cell">
                        <span class="company-name">${esc(job.company)}</span>
                        ${job.company_url ? `<a href="${job.company_url}" target="_blank" class="company-link">${shortenUrl(job.company_url)}</a>` : ''}
                    </div>
                </td>
                <td><span class="position-text">${esc(job.position)}</span></td>
                <td><span class="location-text">${esc(job.location)}</span></td>
                <td>${job.salary ? `<span class="salary-text">${esc(job.salary)}</span>` : '<span style="color:var(--text-muted)">—</span>'}</td>
                <td><span class="category-badge ${categoryClass}">${esc(job.category)}</span></td>
                <td><span class="age-text ${ageClass}">${job.age_days != null ? job.age_days + 'd' : '—'}</span></td>
                <td><span class="status-badge ${statusClass}" ${tooltip}>${statusText}</span></td>
                <td>
                    <div class="action-buttons action-buttons-col">
                        <div class="action-row">
                            <button class="btn btn-sm btn-view" onclick="openJobView(${job.id})">View</button>
                            ${actionButton}
                        </div>
                        <div class="run-status ${runStatus.className}">${runStatus.text}</div>
                    </div>
                </td>
            </tr>
        `;
    }).join('');
}

function renderPagination() {
    const totalPages = Math.ceil(filteredJobs.length / PAGE_SIZE);
    const container = document.getElementById('pagination');

    if (totalPages <= 1) {
        container.innerHTML = '';
        return;
    }

    let html = '';
    html += `<button ${currentPage === 1 ? 'disabled' : ''} onclick="goToPage(${currentPage - 1})">‹ Prev</button>`;

    const maxVisible = 7;
    let startPage = Math.max(1, currentPage - Math.floor(maxVisible / 2));
    let endPage = Math.min(totalPages, startPage + maxVisible - 1);
    if (endPage - startPage < maxVisible - 1) startPage = Math.max(1, endPage - maxVisible + 1);

    if (startPage > 1) {
        html += `<button onclick="goToPage(1)">1</button>`;
        if (startPage > 2) html += `<button disabled>…</button>`;
    }

    for (let i = startPage; i <= endPage; i++) {
        html += `<button class="${i === currentPage ? 'active' : ''}" onclick="goToPage(${i})">${i}</button>`;
    }

    if (endPage < totalPages) {
        if (endPage < totalPages - 1) html += `<button disabled>…</button>`;
        html += `<button onclick="goToPage(${totalPages})">${totalPages}</button>`;
    }

    html += `<button ${currentPage === totalPages ? 'disabled' : ''} onclick="goToPage(${currentPage + 1})">Next ›</button>`;

    container.innerHTML = html;
}

function getRunForJob(job) {
    if (!job) return null;
    const key = normalizeUrl(job.apply_url || job.url);
    if (!key) return null;
    return applyRuns.find(r => normalizeUrl(r.url) === key) || null;
}

function getRunStatusText(job) {
    const run = getRunForJob(job);
    if (!run) return { text: 'Idle', className: 'run-idle', running: false };

    const db = (run.db_apply_status || '').toLowerCase();
    if (run.state === 'running') return { text: 'Pipeline running', className: 'run-running', running: true };
    if (db === 'applied') return { text: 'Applied', className: 'run-applied', running: false };
    if (db === 'failed') return { text: 'Failed', className: 'run-failed', running: false };
    if (db === 'manual') return { text: 'Manual required', className: 'run-failed', running: false };
    if (db === 'login_issue') return { text: 'Login issue', className: 'run-failed', running: false };
    if (db === 'captcha') return { text: 'CAPTCHA block', className: 'run-failed', running: false };
    if (db === 'expired') return { text: 'Expired', className: 'run-failed', running: false };
    if (run.state === 'stopped') return { text: 'Stopped', className: 'run-idle', running: false };
    if (run.state === 'finished' && run.returncode === 0) return { text: 'Finished', className: 'run-idle', running: false };
    if (run.state === 'finished') return { text: 'Pipeline error', className: 'run-failed', running: false };
    return { text: 'Idle', className: 'run-idle', running: false };
}

async function refreshRuns() {
    try {
        const res = await fetch(`${RUNS_ENDPOINT}?t=${Date.now()}`, { cache: 'no-store' });
        if (!res.ok) return;
        const payload = await res.json();
        applyRuns = Array.isArray(payload.runs) ? payload.runs : [];
        mergeRunStatusesIntoJobs();
        applyFilters();
    } catch (_) {
        // If API server isn't running, keep UI usable in view-only mode.
    }
}

async function refreshDbStatuses() {
    try {
        const res = await fetch(`${JOB_STATUS_ENDPOINT}?t=${Date.now()}`, { cache: 'no-store' });
        if (!res.ok) return;
        const payload = await res.json();
        dbStatusRows = Array.isArray(payload.statuses) ? payload.statuses : [];
        mergeDbStatusesIntoJobs();
        applyFilters();
    } catch (_) {
        // Backend status API is optional when static-hosted.
    }
}

async function triggerApply(jobId) {
    const job = allJobs.find(j => j.id === jobId);
    if (!job || !job.apply_url) return;
    const run = getRunForJob(job);
    if (run && run.state === 'running') return;

    try {
        const res = await fetch(APPLY_ENDPOINT, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                job_id: job.id,
                company: job.company,
                position: job.position,
                url: job.apply_url,
            }),
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) {
            const message = payload.error || `HTTP ${res.status}`;
            alert(`Could not start apply pipeline: ${message}\n\nStart dashboard with: python3 dashboard/server.py`);
            return;
        }
        await refreshRuns();
    } catch (e) {
        alert('Apply API not reachable. Start dashboard server:\npython3 dashboard/server.py');
    }
}

async function stopApply(jobId) {
    const job = allJobs.find(j => j.id === jobId);
    if (!job) return;
    const run = getRunForJob(job);
    if (!run || run.state !== 'running') return;

    try {
        const res = await fetch(STOP_ENDPOINT, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                url: run.url || job.apply_url,
                pid: run.pid,
            }),
        });
        const payload = await res.json().catch(() => ({}));
        if (!res.ok || !payload.ok) {
            const message = payload.message || payload.error || `HTTP ${res.status}`;
            alert(`Could not stop pipeline: ${message}`);
            return;
        }
        await refreshRuns();
        await refreshDbStatuses();
    } catch (_) {
        alert('Stop API not reachable. Restart dashboard server.');
    }
}

function computeFitHint(job) {
    if (!job) return { label: 'Unknown', cls: 'fit-medium', reason: 'No job data.' };

    if (typeof job.fit_score === 'number') {
        if (job.fit_score >= 8) return { label: `Strong (${job.fit_score}/10)`, cls: 'fit-high', reason: 'Based on stored fit score.' };
        if (job.fit_score >= 6) return { label: `Moderate (${job.fit_score}/10)`, cls: 'fit-medium', reason: 'Based on stored fit score.' };
        return { label: `Low (${job.fit_score}/10)`, cls: 'fit-low', reason: 'Based on stored fit score.' };
    }

    const text = `${job.position || ''} ${job.category || ''}`.toLowerCase();
    let score = 0;
    if (/\b(ai|ml|machine learning|llm|nlp|data|research)\b/.test(text)) score += 2;
    if (/\b(engineer|scientist|software|platform)\b/.test(text)) score += 1;
    if (job.category === 'FAANG+' || job.category === 'Quant') score += 1;
    if ((job.age_days || 999) <= 14) score += 1;

    if (score >= 4) return { label: 'Likely Fit', cls: 'fit-high', reason: 'Role keywords align with AI/ML/software profile.' };
    if (score >= 2) return { label: 'Possible Fit', cls: 'fit-medium', reason: 'Partial keyword alignment; review requirements.' };
    return { label: 'Low Fit', cls: 'fit-low', reason: 'Limited title/category overlap with target profile.' };
}

function openJobView(jobId) {
    const job = allJobs.find(j => j.id === jobId);
    if (!job) return;
    const fit = computeFitHint(job);
    const modal = document.getElementById('jobViewModal');
    if (!modal) return;

    document.getElementById('viewTitle').textContent = job.position || 'Job';
    document.getElementById('viewCompany').textContent = job.company || '—';
    document.getElementById('viewLocation').textContent = job.location || '—';
    document.getElementById('viewType').textContent = `${job.type || '—'} / ${job.region || '—'}`;
    document.getElementById('viewSalary').textContent = job.salary || 'Not listed';
    document.getElementById('viewCategory').textContent = job.category || '—';
    document.getElementById('viewAge').textContent = job.age_days != null ? `${job.age_days} days` : 'Unknown';
    const fitEl = document.getElementById('viewFit');
    fitEl.textContent = `${fit.label} — ${fit.reason}`;
    fitEl.className = `fit-pill ${fit.cls}`;
    const openA = document.getElementById('viewOpenLink');
    openA.href = job.apply_url || '#';

    modal.style.display = 'flex';
}

function closeJobViewModal() {
    const modal = document.getElementById('jobViewModal');
    if (modal) modal.style.display = 'none';
}

// ============ ACTIONS ============

function goToPage(page) {
    currentPage = page;
    renderTable();
    renderPagination();
    document.querySelector('.table-section').scrollIntoView({ behavior: 'smooth' });
}

function toggleSelect(jobId) {
    if (selectedJobIds.has(jobId)) selectedJobIds.delete(jobId);
    else selectedJobIds.add(jobId);
    document.getElementById('batchCount').textContent = selectedJobIds.size || 10;
}

function setStatus(jobId, status) {
    const job = allJobs.find(j => j.id === jobId);
    if (job) {
        job.status = status;
        saveData();
        applyFilters();
    }
}

function batchOpen() {
    let jobsToOpen;
    if (selectedJobIds.size > 0) {
        jobsToOpen = allJobs.filter(j => selectedJobIds.has(j.id));
    } else {
        // Open first 10 pending jobs from current filter
        jobsToOpen = filteredJobs.filter(j => !j.status || j.status === 'pending').slice(0, 10);
    }

    if (jobsToOpen.length === 0) {
        alert('No jobs selected. Select jobs using checkboxes or ensure there are pending jobs in current view.');
        return;
    }

    if (!confirm(`Open ${jobsToOpen.length} application links in new tabs?`)) return;

    jobsToOpen.forEach((job, i) => {
        setTimeout(() => window.open(job.apply_url, '_blank'), i * 300);
    });
}

function handleFileLoad(e) {
    const file = e.target.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = (ev) => {
        try {
            const data = JSON.parse(ev.target.result);
            if (Array.isArray(data)) {
                allJobs = data;
                saveData();
                applyFilters();
                document.getElementById('loadModal').style.display = 'none';
            } else {
                alert('Invalid format: expected a JSON array of jobs');
            }
        } catch (err) {
            alert('Error parsing JSON: ' + err.message);
        }
    };
    reader.readAsText(file);
}

// ============ UTILITIES ============

function esc(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function shortenUrl(url) {
    try {
        const u = new URL(url);
        return u.hostname.replace('www.', '');
    } catch (e) {
        return url.slice(0, 30);
    }
}
