(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const csrf = () => document.body?.dataset.csrf || '';
  const escapeHTML = (value = '') => String(value).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const toast = (message) => {
    const el = $('#toast'); if (!el) return;
    el.textContent = message; el.classList.add('show');
    clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('show'), 3300);
  };
  const api = async (url, options = {}) => {
    const headers = new Headers(options.headers || {});
    if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', csrf());
    const response = await fetch(url, {...options, headers, credentials: 'same-origin'});
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try { const body = await response.json(); message = body.detail || message; } catch {}
      const error = new Error(message); error.status = response.status; throw error;
    }
    return response;
  };
  const initIcons = () => { if (window.lucide) window.lucide.createIcons({attrs:{'stroke-width':1.8}}); };
  const revealInit = () => {
    const items = $$('.reveal');
    if (!('IntersectionObserver' in window)) { items.forEach(el => el.classList.add('is-visible')); return; }
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (entry.isIntersecting) { entry.target.classList.add('is-visible'); observer.unobserve(entry.target); }
    }), {threshold: .12, rootMargin: '0px 0px -35px 0px'});
    items.forEach(el => observer.observe(el));
  };
  const formatDate = value => {
    try { return new Date(value).toLocaleDateString(undefined, {month:'short', day:'numeric', year:'numeric'}); }
    catch { return '—'; }
  };

  initIcons(); revealInit();
  const year = $('#year'); if (year) year.textContent = new Date().getFullYear();

  // Marketing navigation and subtle pointer parallax.
  const mobileMenuButton = $('.mobile-menu-button');
  mobileMenuButton?.addEventListener('click', () => {
    let panel = $('.mobile-nav-panel');
    if (!panel) {
      panel = document.createElement('div');
      panel.className = 'mobile-nav-panel';
      panel.innerHTML = '<a href="#platform">Platform</a><a href="#how">How it works</a><a href="#trust">Built responsibly</a><a href="/auth/google">Sign in with Google</a>';
      $('.site-header')?.append(panel);
    }
    const open = panel.classList.toggle('open');
    mobileMenuButton.setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('keydown', event => {
    if (!(workspace && (event.metaKey || event.ctrlKey))) return;
    if (event.key === '1') { event.preventDefault(); selectTab('tutor'); }
    if (event.key === '2') { event.preventDefault(); selectTab('reports'); }
  });

  const orb = $('.hero-orb');
  if (orb && window.matchMedia('(pointer:fine)').matches && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const art = $('.hero-art');
    art?.addEventListener('pointermove', event => {
      const r = art.getBoundingClientRect();
      const x = (event.clientX - r.left) / r.width - .5;
      const y = (event.clientY - r.top) / r.height - .5;
      orb.style.transform = `translate3d(${x * 10}px,${y * 9}px,0) rotateX(${-y * 8}deg) rotateY(${x * 9}deg)`;
    });
    art?.addEventListener('pointerleave', () => orb.style.transform = '');
  }

  // Workspace: account, navigation, visual tutor and PDF studio.
  const workspace = $('.workspace-page:not(.admin-page)');
  if (workspace) initWorkspace();
  const adminPage = $('.admin-page');
  if (adminPage) initAdmin();

  async function loadMe() {
    const res = await api('/api/me'); return res.json();
  }
  function setUser(user) {
    const name = $('#user-name'), email = $('#user-email'), avatar = $('#user-avatar');
    if (name) name.textContent = user?.name || 'Student workspace';
    if (email) email.textContent = user?.email || 'Not signed in';
    if (avatar) {
      if (user?.picture) avatar.innerHTML = `<img src="${escapeHTML(user.picture)}" alt="">`;
      else avatar.textContent = (user?.name || 'L').slice(0,1).toUpperCase();
    }
  }
  async function initWorkspace() {
    let me;
    try { me = await loadMe(); }
    catch { toast('Please sign in to use your workspace.'); return; }
    if (!me.authenticated) {
      const area = $('#chat-area');
      if (area) area.innerHTML = `<div class="assistant-welcome"><div class="welcome-symbol">✳</div><h2>Your learning space is waiting.</h2><p>Sign in with your Google account to use the visual tutor and report studio.</p><a class="button button-primary" href="/auth/google">Continue with Google <span>→</span></a></div>`;
      const reportForm = $('#report-form'); if (reportForm) reportForm.classList.add('hidden');
      return;
    }
    setUser(me.user);
    const logout = $('#logout-button');
    logout?.addEventListener('click', async () => { try { await api('/auth/logout', {method:'POST'}); location.href='/'; } catch(e) { toast(e.message); } });
    const toggle = $('.mobile-sidebar-toggle');
    toggle?.addEventListener('click', () => $('.app-sidebar')?.classList.toggle('mobile-open'));
    $$('[data-tab]').forEach(button => button.addEventListener('click', () => selectTab(button.dataset.tab)));
    const initialTab = new URLSearchParams(location.search).get('tab');
    selectTab(initialTab === 'reports' ? 'reports' : 'tutor');
    await refreshStudentOverview();
    initTutor(); initReportStudio();
  }
  function selectTab(tab) {
    const isTutor = tab === 'tutor';
    $('#tutor-panel')?.classList.toggle('hidden', !isTutor);
    $('#reports-panel')?.classList.toggle('hidden', isTutor);
    $$('[data-tab]').forEach(button => button.classList.toggle('active', button.dataset.tab === tab));
    const crumb = $('#breadcrumb-current'); if (crumb) crumb.textContent = isTutor ? 'VISUAL LEARNING' : 'REPORT STUDIO';
    history.replaceState({}, '', isTutor ? '/app?tab=tutor' : '/app?tab=reports');
    $('.app-sidebar')?.classList.remove('mobile-open');
  }
  async function refreshStudentOverview() {
    try {
      const response = await api('/api/student/overview'); const data = await response.json();
      const u = data.usage;
      setUsage('tutor', u.tutor, u.tutor_limit); setUsage('report', u.reports, u.report_limit);
    } catch (e) { if (e.status !== 401) toast(e.message); }
  }
  function setUsage(prefix, used, limit) {
    const value = $(`#${prefix}-usage`), total = $(`#${prefix}-limit`), bar = $(`#${prefix}-usage-bar`);
    if (value) value.textContent = used;
    if (total) total.textContent = limit;
    if (bar) bar.style.width = `${Math.min(100, limit ? used / limit * 100 : 0)}%`;
  }
  function initTutor() {
    const form = $('#tutor-form'), input = $('#tutor-message'), chat = $('#chat-area');
    if (!form || !input || !chat) return;
    $$('.prompt-chip').forEach(chip => chip.addEventListener('click', () => { input.value = chip.dataset.prompt || ''; input.focus(); }));
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const message = input.value.trim(); if (message.length < 3) return;
      const welcome = $('.assistant-welcome', chat); welcome?.remove();
      const userBubble = document.createElement('div'); userBubble.className = 'answer-user'; userBubble.textContent = message; chat.append(userBubble);
      const thinking = document.createElement('div'); thinking.className = 'thinking-row'; thinking.innerHTML = '<span class="thinking-dots"><i></i><i></i><i></i></span> LumiDesk is organizing a clear explanation…'; chat.append(thinking);
      const submit = $('button[type="submit"]', form); submit.disabled = true; input.value = '';
      chat.scrollTop = chat.scrollHeight;
      try {
        const response = await api('/api/tutor', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message, level:$('#learner-level')?.value || 'beginner', style:$('#learning-style')?.value || 'visual'})});
        const data = await response.json(); thinking.remove(); renderTutorAnswer(data.result, chat); setUsage('tutor', data.usage.used, data.usage.limit);
      } catch (error) {
        thinking.remove(); const err = document.createElement('div'); err.className='answer-card'; err.innerHTML=`<p>${escapeHTML(error.message)}</p>`; chat.append(err);
      } finally { submit.disabled = false; chat.scrollTop = chat.scrollHeight; }
    });
  }
  function renderTutorAnswer(data, chat) {
    const article = document.createElement('article'); article.className = 'answer-message';
    const keyPoints = Array.isArray(data.key_points) ? data.key_points : [];
    const steps = Array.isArray(data.steps) ? data.steps : [];
    const paragraphs = String(data.explanation || '').split(/\n\s*\n/).filter(Boolean).map(p => `<p>${escapeHTML(p).replace(/\n/g,'<br>')}</p>`).join('');
    article.innerHTML = `<div class="answer-card"><h3>${escapeHTML(data.title || 'Here is the idea')}</h3>${paragraphs}${data.analogy ? `<div class="answer-callout"><b>Think of it this way:</b> ${escapeHTML(data.analogy)}</div>` : ''}${data.example ? `<h4>A practical example</h4><p>${escapeHTML(data.example)}</p>` : ''}${steps.length ? `<h4>Step by step</h4><ol>${steps.map(s=>`<li><b>${escapeHTML(s.title || '')}</b> — ${escapeHTML(s.detail || '')}</li>`).join('')}</ol>` : ''}${keyPoints.length ? `<h4>Remember these</h4><ul>${keyPoints.map(p=>`<li>${escapeHTML(p)}</li>`).join('')}</ul>` : ''}<div class="diagram-wrap ${data.mermaid ? '' : 'hidden'}"><div class="mermaid"></div></div>${Array.isArray(data.quiz) && data.quiz.length ? `<div class="quiz-card"><h4>Quick knowledge check</h4>${data.quiz.slice(0,3).map((q,i)=>`<div class="quiz-question"><b>${i+1}. ${escapeHTML(q.question || '')}</b>${(q.options || []).map((o,j)=>`<button type="button" data-q="${i}" data-option="${j}">${escapeHTML(o)}</button>`).join('')}<small class="quiz-feedback"></small></div>`).join('')}</div>` : ''}<p class="answer-disclaimer">Use this as a learning guide. Check important facts against your course material.</p></div>`;
    chat.append(article);
    if (data.mermaid && window.mermaid && !/[<>]|script/i.test(data.mermaid)) {
      const target = $('.mermaid', article); target.textContent = data.mermaid;
      window.mermaid.initialize({startOnLoad:false,theme:'base',securityLevel:'strict',themeVariables:{primaryColor:'#eee9ff',primaryTextColor:'#4b4660',lineColor:'#9a8bd0',secondaryColor:'#e7f6ef',tertiaryColor:'#fff1e9',fontFamily:'DM Sans'}});
      window.mermaid.run({nodes:[target]}).catch(()=>{target.textContent='Diagram preview unavailable for this response.';});
    } else $('.diagram-wrap', article)?.classList.add('hidden');
    $$('.quiz-question', article).forEach((question, index) => {
      const q = data.quiz[index];
      $$('button', question).forEach(button => button.addEventListener('click', () => {
        $$('button', question).forEach(b => b.disabled = true);
        const chosen = Number(button.dataset.option), correct = chosen === Number(q.answer_index);
        button.classList.add(correct ? 'correct' : 'incorrect');
        const feedback = $('.quiz-feedback', question); if (feedback) feedback.textContent = `${correct ? 'Correct!' : 'Not quite.'} ${q.explanation || ''}`;
      }));
    });
    chat.scrollTop = chat.scrollHeight;
  }
  function initReportStudio() {
    const input = $('#reference-pdf');
    const zone = $('#upload-zone');
    const form = $('#report-form');
    if (!input || !zone || !form) return;
    let latestJobId = '';
    let lastDownloadUrl = '';
    let active = false;
    const progressBox = $('#report-progress');
    const progressTitle = $('#progress-title');
    const progressDetail = $('#progress-detail');
    const errorBox = $('#report-error');
    const successBox = $('#report-success');
    const generateButton = $('#generate-report');
    const regenerateButton = $('#regenerate-report');
    const regenPanel = $('#regenerate-panel');
    const successText = $('#report-success-copy');
    const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

    function setBusy(value) {
      active = value;
      generateButton.disabled = value;
      if (regenerateButton) regenerateButton.disabled = value;
      progressBox?.classList.toggle('hidden', !value);
    }
    function showError(message) {
      if (errorBox) {
        errorBox.textContent = message;
        errorBox.classList.remove('hidden');
      }
      toast(message);
    }
    function updateProgress(job) {
      if (progressTitle) progressTitle.textContent = job.status === 'queued' ?
        'Your document is queued…' : 'Rebuilding the original PDF layout…';
      const fraction = job.total_sections > 0 ?
        ` (${job.done_sections || 0}/${job.total_sections} sections)` : '';
      if (progressDetail) progressDetail.textContent = `${job.detail || 'Working on your report…'}${fraction}`;
      const bar = $('#report-progress-fill');
      if (bar && job.total_sections) bar.style.width = `${Math.round((job.done_sections || 0) / job.total_sections * 100)}%`;
    }
    async function download(job) {
      const response = await api(job.download_url || `/api/reports/jobs/${job.job_id}/download`);
      const blob = await response.blob();
      if (blob.type && !blob.type.includes('pdf')) throw new Error('The download was not a PDF. Please retry.');
      if (blob.size < 100) throw new Error('The generated PDF is unexpectedly small.');
      if (lastDownloadUrl) URL.revokeObjectURL(lastDownloadUrl);
      lastDownloadUrl = URL.createObjectURL(blob);
      const raw = response.headers.get('content-disposition') || '';
      const filename = raw.match(/filename="?([^";]+)"?/i)?.[1] || 'LumiDesk_Report.pdf';
      const anchor = document.createElement('a');
      anchor.href = lastDownloadUrl;
      anchor.download = filename;
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      successBox?.classList.remove('hidden');
      if (regenPanel) regenPanel.classList.remove('hidden');
      const warnings = Array.isArray(job.warnings) ? job.warnings : [];
      const overlap = job.overlap_estimate === null || job.overlap_estimate === undefined ?
        '' : ` Estimated 4-word overlap with the previous draft: ${(Number(job.overlap_estimate) * 100).toFixed(1)}% (not a plagiarism score).`;
      if (successText) successText.textContent =
        `Your ${job.pages || 'multi-page'}-section report is ready.${overlap} ` +
        (warnings.length ? `Review required: ${warnings.slice(0, 2).join(' ')}` :
          'Review source evidence, accuracy and formatting before academic submission.');
      const name = form.querySelector('[name="student_name"]');
      const usn = form.querySelector('[name="usn"]');
      if ($('#regen-name') && name) $('#regen-name').value = name.value;
      if ($('#regen-usn') && usn) $('#regen-usn').value = usn.value;
      await refreshStudentOverview();
      toast('Your PDF draft has downloaded. You can regenerate another variant below.');
    }
    async function waitForJob(jobId) {
      latestJobId = jobId;
      sessionStorage.setItem('lumidesk_report_job', jobId);
      setBusy(true);
      errorBox?.classList.add('hidden');
      successBox?.classList.add('hidden');
      try {
        for (;;) {
          const response = await api(`/api/reports/jobs/${encodeURIComponent(jobId)}`);
          const job = await response.json();
          updateProgress(job);
          if (job.status === 'completed') {
            await download(job);
            return;
          }
          if (job.status === 'failed') throw new Error(job.error || 'The PDF could not be created from this file.');
          await sleep(2300);
        }
      } catch (error) {
        if (error.status === 404) sessionStorage.removeItem('lumidesk_report_job');
        showError(error.message || 'The report could not be completed.');
      } finally {
        setBusy(false);
      }
    }

    $('#browse-pdf')?.addEventListener('click', () => input.click());
    input.addEventListener('change', () => showSelectedFile(input.files?.[0]));
    $('#remove-file')?.addEventListener('click', () => {
      input.value = '';
      $('#file-selected')?.classList.add('hidden');
      $('#template-analysis')?.classList.add('hidden');
    });
    ['dragenter', 'dragover'].forEach(type => zone.addEventListener(type, e => {
      e.preventDefault(); zone.classList.add('dragover');
    }));
    ['dragleave', 'drop'].forEach(type => zone.addEventListener(type, e => {
      e.preventDefault(); zone.classList.remove('dragover');
    }));
    zone.addEventListener('drop', e => {
      const file = e.dataTransfer.files?.[0];
      if (file) { input.files = e.dataTransfer.files; showSelectedFile(file); }
    });

    async function showSelectedFile(file) {
      if (!file) return;
      if (file.type !== 'application/pdf' && !file.name.toLowerCase().endsWith('.pdf')) {
        showError('Please upload a PDF file.'); input.value = ''; return;
      }
      if (file.size > 30 * 1024 * 1024) {
        showError('This PDF is larger than the 30 MB free-tier file limit.'); input.value = ''; return;
      }
      $('#file-name').textContent = `${file.name} · ${(file.size / 1048576).toFixed(1)} MB`;
      $('#file-selected')?.classList.remove('hidden');
      const summary = $('#template-analysis');
      if (summary) { summary.textContent = 'Checking the reference layout and headings…'; summary.classList.remove('hidden'); }
      try {
        const body = new FormData(); body.set('file', file);
        const response = await api('/api/reports/analyze', {method: 'POST', body});
        const info = await response.json();
        const sections = (info.editable_sections || []).map(x => x.name).filter((v, i, a) => a.indexOf(v) === i);
        if (summary) summary.innerHTML =
          `<b>Format recognized · ${Number(info.page_count)} of 70 pages</b>` +
          `<p>College: ${escapeHTML(info.institution || 'Retained from the source')}</p>` +
          `<p>Department: ${escapeHTML(info.department || 'Retained from the source')}</p>` +
          `<p>Original title: ${escapeHTML(info.source_title || 'Not identified')}</p>` +
          `<small>Images: ${Number(info.image_count || 0)}. Editable chapters: ${escapeHTML(sections.join(' · ') || 'No safe body-text sections recognized')}.</small>`;
      } catch (error) {
        if (summary) summary.textContent = `PDF inspection: ${error.message}`;
      }
    }

    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (active) return;
      const file = input.files?.[0];
      if (!file) { showError('Upload your original PDF first.'); return; }
      errorBox?.classList.add('hidden');
      const body = new FormData(form);
      body.set('file', file);
      setBusy(true);
      if (progressTitle) progressTitle.textContent = 'Submitting your PDF…';
      if (progressDetail) progressDetail.textContent = 'Checking the original document, ready for a background job.';
      try {
        const response = await api('/api/reports/start', {method: 'POST', body});
        const job = await response.json();
        await waitForJob(job.job_id);
      } catch (error) {
        showError(error.message);
      } finally {
        setBusy(false);
      }
    });

    regenerateButton?.addEventListener('click', async () => {
      if (active || !latestJobId) return;
      const name = $('#regen-name')?.value.trim();
      const usn = $('#regen-usn')?.value.trim();
      if (!name || !usn) { showError('Enter the new student name and USN.'); return; }
      errorBox?.classList.add('hidden');
      setBusy(true);
      if (progressTitle) progressTitle.textContent = 'Creating a different writing variant…';
      try {
        const body = new FormData();
        body.set('student_name', name); body.set('usn', usn);
        const response = await api(`/api/reports/jobs/${encodeURIComponent(latestJobId)}/regenerate`, {
          method: 'POST', body,
        });
        const job = await response.json();
        await waitForJob(job.job_id);
      } catch (error) { showError(error.message); }
      finally { setBusy(false); }
    });

    $('#download-again')?.addEventListener('click', () => {
      if (!lastDownloadUrl) return;
      const link = document.createElement('a');
      link.href = lastDownloadUrl; link.download = 'LumiDesk_Report.pdf'; link.click();
    });
    $('#create-another')?.addEventListener('click', () => {
      if (active) return;
      form.reset(); input.value = '';
      $('#file-selected')?.classList.add('hidden');
      $('#template-analysis')?.classList.add('hidden');
      successBox?.classList.add('hidden');
      errorBox?.classList.add('hidden');
      regenPanel?.classList.add('hidden');
      latestJobId = '';
      sessionStorage.removeItem('lumidesk_report_job');
      if (lastDownloadUrl) { URL.revokeObjectURL(lastDownloadUrl); lastDownloadUrl = ''; }
    });

    // Refresh/reopen resumes polling for jobs still running in this Render process.
    const pending = sessionStorage.getItem('lumidesk_report_job');
    if (pending) waitForJob(pending);
  }

  // Admin console is server-gated; the browser never grants a role.
  async function initAdmin() {
    const toggle = $('.mobile-sidebar-toggle'); toggle?.addEventListener('click',()=>$('.app-sidebar')?.classList.toggle('mobile-open'));
    try {
      const me = await loadMe();
      if (!me.authenticated) { toast('Sign in with an administrator account.'); return; }
      if (me.user.role !== 'admin') { toast('Administrator access is required.'); setTimeout(()=>location.href='/app',900); return; }
      setUser(me.user);
      $('#logout-button')?.addEventListener('click',async()=>{try{await api('/auth/logout',{method:'POST'});location.href='/';}catch(e){toast(e.message);}});
      const data = await (await api('/api/admin/overview')).json(); renderAdmin(data);
      $$('[data-admin-tab]').forEach(button=>button.addEventListener('click',()=>selectAdminTab(button.dataset.adminTab)));
      $('#user-search')?.addEventListener('input',filterUsers);
    } catch(e) { toast(e.message); }
  }
  let adminData = null;
  function renderAdmin(data) {
    adminData=data; const m=data.metrics;
    [['metric-users',m.users],['metric-active',m.active_7d],['metric-reports',m.reports],['metric-tutor',m.tutor_calls_7d]].forEach(([id,v])=>{const el=$(`#${id}`);if(el)el.textContent=Number(v||0).toLocaleString();});
    setStatus('google-status',data.system.google_configured);setStatus('ai-status',data.system.ai_configured);
    const db=$('#db-status');if(db){db.textContent=data.system.database.includes('ephemeral')?'SQLITE · TEMPORARY':'POSTGRESQL';db.className=data.system.database.includes('ephemeral')?'status-missing':'status-ok';}
    const userRows=data.users.map(u=>`<tr><td><strong>${escapeHTML(u.name)}</strong></td><td>${escapeHTML(u.email)}</td><td><span class="role-tag">${escapeHTML(u.role)}</span></td><td><span class="status-tag ${u.active?'status-active':'status-disabled'}">${u.active?'ACTIVE':'DISABLED'}</span></td><td>${formatDate(u.created_at)}</td></tr>`).join('') || '<tr><td colspan="4" class="table-loading">No students yet.</td></tr>';
    $('#recent-users').innerHTML=userRows;
    $('#all-users').innerHTML=data.users.map(u=>`<tr data-user-row><td><strong>${escapeHTML(u.name)}</strong></td><td>${escapeHTML(u.email)}</td><td><span class="role-tag">${escapeHTML(u.role)}</span></td><td><span class="status-tag ${u.active?'status-active':'status-disabled'}">${u.active?'ACTIVE':'DISABLED'}</span></td><td>${u.role==='admin'?'—':`<button class="table-action ${u.active?'danger':''}" data-toggle-user="${u.id}">${u.active?'Disable':'Enable'}</button>`}</td></tr>`).join('') || '<tr><td colspan="5" class="table-loading">No students yet.</td></tr>';
    $('#all-reports').innerHTML=data.reports.map(r=>`<tr><td><strong>${escapeHTML(r.title)}</strong></td><td>${r.user_id}</td><td>${r.pages}</td><td>${formatDate(r.created_at)}</td></tr>`).join('') || '<tr><td colspan="4" class="table-loading">No reports yet.</td></tr>';
    $$('[data-toggle-user]').forEach(button=>button.addEventListener('click',async()=>{if(!confirm('Change access for this student?'))return;try{await api(`/api/admin/users/${button.dataset.toggleUser}/toggle`,{method:'POST'});const refreshed=await (await api('/api/admin/overview')).json();renderAdmin(refreshed);toast('Student access updated.');}catch(e){toast(e.message);}}));
  }
  function setStatus(id,ok){const el=$(`#${id}`);if(!el)return;el.textContent=ok?'CONNECTED':'NOT CONFIGURED';el.className=ok?'status-ok':'status-missing';}
  function selectAdminTab(tab){$$('[data-admin-tab]').forEach(b=>b.classList.toggle('active',b.dataset.adminTab===tab));['overview','users','reports'].forEach(t=>$(`#admin-${t}`)?.classList.toggle('hidden',t!==tab));$('.app-sidebar')?.classList.remove('mobile-open');}
  function filterUsers(){const q=($('#user-search')?.value||'').toLowerCase();$$('[data-user-row]').forEach(row=>row.classList.toggle('hidden',!row.textContent.toLowerCase().includes(q)));}
})();
