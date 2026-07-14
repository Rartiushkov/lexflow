const API_BASE = (() => {
  const el = document.querySelector('[data-api-base]');
  return el?.dataset.apiBase || 'http://localhost:8000';
})();

let publicConfigPromise = null;
let supabaseClientPromise = null;
let supabaseAuthListenerBound = false;

const LS_TOKEN = 'lexflow_token';
const LS_USER = 'lexflow_user';
const LS_INVOICES = 'lexflow_invoices';
const LS_INVOICE_TEMPLATES = 'lexflow_invoice_templates';
const LS_CASE_DIRECTORY = 'lexflow_case_directory';
const LS_CASE_DETAILS = 'lexflow_case_details';
const LS_INCOMING_DOCUMENTS = 'lexflow_incoming_documents';
const LS_SETTINGS_PROFILE = 'lexflow_settings_profile';
const LS_WORKFLOW_SUMMARY = 'lexflow_workflow_summary';
const OAUTH_REDIRECT_FLAG_KEY = 'lexflow_oauth_redirect_started_at';
const OAUTH_REDIRECT_FLAG_TTL_MS = 2 * 60 * 1000;

function getToken() {
  return localStorage.getItem(LS_TOKEN);
}

function setSession(token, user) {
  localStorage.setItem(LS_TOKEN, token);
  localStorage.setItem(LS_USER, JSON.stringify(user));
}

function clearSession() {
  localStorage.removeItem(LS_TOKEN);
  localStorage.removeItem(LS_USER);
}

function getUser() {
  try {
    return JSON.parse(localStorage.getItem(LS_USER));
  } catch {
    return null;
  }
}

function isEmbeddedBrowser() {
  const ua = navigator.userAgent || '';
  return /FBAN|FBAV|Instagram|Line|wv|WebView|Telegram/i.test(ua);
}

function isAppleMobileWeb() {
  return /iPhone|iPad|iPod/i.test(navigator.userAgent || '');
}

function markOAuthRedirectStart() {
  try {
    localStorage.setItem(OAUTH_REDIRECT_FLAG_KEY, String(Date.now()));
  } catch {}
}

function clearOAuthRedirectStart() {
  try {
    localStorage.removeItem(OAUTH_REDIRECT_FLAG_KEY);
  } catch {}
}

function hasPendingOAuthRedirect() {
  try {
    const raw = localStorage.getItem(OAUTH_REDIRECT_FLAG_KEY);
    const startedAt = Number(raw || 0);
    return Number.isFinite(startedAt) && startedAt > 0 && (Date.now() - startedAt) < OAUTH_REDIRECT_FLAG_TTL_MS;
  } catch {
    return false;
  }
}

async function waitForSessionRecovery(timeoutMs = 10000) {
  const startedAt = Date.now();
  while ((Date.now() - startedAt) < timeoutMs) {
    const user = await syncSessionFromSupabase();
    if (user) {
      clearOAuthRedirectStart();
      return user;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return null;
}

function requireAuth() {
  return (async () => {
    const restored = await syncSessionFromSupabase();
    if (restored) return restored;
    const recovered = await waitForSessionRecovery(hasPendingOAuthRedirect() ? 10000 : 2500);
    if (recovered) return recovered;
    window.location.href = 'login.html';
    return null;
  })();
}

function requireGuest() {
  return (async () => {
    const restored = await syncSessionFromSupabase();
    if (restored) {
      window.location.href = 'dashboard.html';
      return restored;
    }
    const recovered = await waitForSessionRecovery(hasPendingOAuthRedirect() ? 10000 : 1500);
    if (recovered) {
      window.location.href = 'dashboard.html';
      return recovered;
    }
    return null;
  })();
}

async function getPublicConfig() {
  if (!publicConfigPromise) {
    publicConfigPromise = fetch(`${API_BASE}/api/config`)
      .then(async (res) => {
        if (!res.ok) throw new Error(`Config request failed: HTTP ${res.status}`);
        return res.json();
      })
      .catch(() => ({ supabase_url: '', supabase_anon_key: '' }));
  }
  return publicConfigPromise;
}

async function getSupabaseBrowserClient() {
  if (!supabaseClientPromise) {
    supabaseClientPromise = (async () => {
      const config = await getPublicConfig();
      if (!config?.supabase_url || !config?.supabase_anon_key) {
        return null;
      }
      const { createClient } = await import('https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2/+esm');
      const client = createClient(config.supabase_url, config.supabase_anon_key, {
        auth: {
          flowType: 'pkce',
          autoRefreshToken: true,
          persistSession: true,
          detectSessionInUrl: false,
        },
      });
      if (!supabaseAuthListenerBound) {
        client.auth.onAuthStateChange((_event, session) => {
          const user = mapSupabaseUser(session?.user);
          if (session?.access_token && user) {
            setSession(session.access_token, user);
            clearOAuthRedirectStart();
          } else if (!session) {
            clearSession();
          }
        });
        supabaseAuthListenerBound = true;
      }
      return client;
    })();
  }
  return supabaseClientPromise;
}

function mapSupabaseUser(user) {
  if (!user) return null;
  return {
    id: user.id,
    email: user.email || '',
    name: user.user_metadata?.full_name || user.user_metadata?.name || user.email || 'User',
  };
}

function clearAuthCodeFromUrl() {
  const url = new URL(window.location.href);
  url.searchParams.delete('code');
  url.searchParams.delete('error');
  url.searchParams.delete('error_code');
  url.searchParams.delete('error_description');
  window.history.replaceState({}, document.title, url.toString());
}

async function completeSupabaseOAuthIfNeeded() {
  const client = await getSupabaseBrowserClient();
  if (!client) return null;
  const url = new URL(window.location.href);
  const code = url.searchParams.get('code');
  if (!code) return null;
  const { data, error } = await client.auth.exchangeCodeForSession(code);
  if (error) throw error;
  clearAuthCodeFromUrl();
  const user = mapSupabaseUser(data?.user || data?.session?.user);
  if (data?.session?.access_token && user) {
    setSession(data.session.access_token, user);
    clearOAuthRedirectStart();
  }
  return user;
}

async function syncSessionFromSupabase() {
  try {
    const exchanged = await completeSupabaseOAuthIfNeeded();
    if (exchanged) return exchanged;
    const client = await getSupabaseBrowserClient();
    if (!client) return null;
    const { data, error } = await client.auth.getSession();
    if (error) throw error;
    const session = data?.session;
    const user = mapSupabaseUser(session?.user);
    if (!session?.access_token || !user) return null;
    setSession(session.access_token, user);
    clearOAuthRedirectStart();
    return user;
  } catch {
    return null;
  }
}

async function startGoogleLogin() {
  const client = await getSupabaseBrowserClient();
  if (!client) {
    throw new Error('Supabase Google login is not configured');
  }
  markOAuthRedirectStart();
  const redirectTo = `${window.location.origin}${window.location.pathname}`;
  const { error } = await client.auth.signInWithOAuth({
    provider: 'google',
    options: {
      redirectTo,
      queryParams: { prompt: 'select_account' },
      scopes: 'openid email profile',
    },
  });
  if (error) throw error;
}

async function signOutSupabaseSession() {
  const client = await getSupabaseBrowserClient();
  if (!client) return;
  try {
    await client.auth.signOut();
  } catch {}
  clearOAuthRedirectStart();
}

async function resetAllSessions() {
  await signOutSupabaseSession();
  clearSession();
  clearOAuthRedirectStart();
}

// ─── API helpers ─────────────────────────────────────────
async function buildJsonHeaders() {
  if (!getToken()) {
    await syncSessionFromSupabase();
  }
  return {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${getToken() || ''}`,
  };
}

async function fetchWithAuthRetry(url, opts, parseAsJson = true) {
  let res = await fetch(url, opts);
  if (res.status === 401) {
    await syncSessionFromSupabase();
    res = await fetch(url, {
      ...opts,
      headers: {
        ...(opts.headers || {}),
        'Authorization': `Bearer ${getToken() || ''}`,
      },
    });
  }
  const text = await res.text();
  const data = parseAsJson && text ? JSON.parse(text) : (parseAsJson ? null : text);
  if (!res.ok) {
    throw new Error(data?.detail || data?.error || `HTTP ${res.status}`);
  }
  return data;
}

async function api(method, path, body, options = {}) {
  const { toastOnError = true } = options;
  const url = `${API_BASE}${path}`;
  const opts = {
    method,
    headers: await buildJsonHeaders(),
  };
  if (body) opts.body = JSON.stringify(body);
  try {
    return await fetchWithAuthRetry(url, opts, true);
  } catch (err) {
    if (toastOnError) showToast(err.message || 'Network error', 'error');
    throw err;
  }
}

function get(path, options) { return api('GET', path, undefined, options); }
function post(path, body, options) { return api('POST', path, body, options); }
function patch(path, body, options) { return api('PATCH', path, body, options); }

async function upload(path, file) {
  const url = `${API_BASE}${path}`;
  const form = new FormData();
  form.append('file', file);
  if (!getToken()) {
    await syncSessionFromSupabase();
  }
  return await fetchWithAuthRetry(url, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${getToken() || ''}` },
    body: form,
  }, true);
}

// ─── UI helpers ──────────────────────────────────────────
function showToast(message, type = 'info') {
  let toast = document.getElementById('toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast';
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  toast.className = `toast toast-${type}`;
  toast.textContent = message;
  requestAnimationFrame(() => toast.classList.add('show'));
  setTimeout(() => toast.classList.remove('show'), 3000);
}

function initials(name) {
  return (name || '?')
    .split(' ')
    .map(n => n[0])
    .slice(0, 2)
    .join('')
    .toUpperCase();
}

function formatDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' });
}

function formatCurrency(amount, currency = 'EUR') {
  return new Intl.NumberFormat('de-DE', { style: 'currency', currency }).format(amount);
}

function getCaseDirectory() {
  try {
    const raw = JSON.parse(localStorage.getItem(LS_CASE_DIRECTORY));
    if (Array.isArray(raw) && raw.length) return raw;
  } catch {}
  return [];
}

function saveCaseDirectory(cases) {
  localStorage.setItem(LS_CASE_DIRECTORY, JSON.stringify(cases));
}

function getCaseDetailsCache() {
  try {
    const raw = JSON.parse(localStorage.getItem(LS_CASE_DETAILS) || '{}');
    return raw && typeof raw === 'object' ? raw : {};
  } catch {
    return {};
  }
}

function saveCaseDetailsCache(cache) {
  localStorage.setItem(LS_CASE_DETAILS, JSON.stringify(cache || {}));
}

function setCaseDirectory(cases) {
  const normalized = (cases || []).map(item => ({
    id: item.id,
    client_name: item.client_name,
    client_email: item.client_email || '',
    case_type: item.case_type || item.type || '',
    destination: item.destination || item.country || '',
    stage: item.stage || 'documents',
    priority: item.priority || item.control_state?.auto_priority || 'medium',
    created_at: item.created_at || new Date().toISOString(),
  }));
  if (normalized.length) saveCaseDirectory(normalized);
  if (Array.isArray(cases) && cases.length) {
    const cache = getCaseDetailsCache();
    cases.forEach(item => {
      if (item?.id) cache[item.id] = { ...(cache[item.id] || {}), ...item };
    });
    saveCaseDetailsCache(cache);
  }
  return normalized;
}

function upsertLocalCase(caseData) {
  const cases = getCaseDirectory();
  const normalized = setCaseDirectory([caseData])[0];
  const idx = cases.findIndex(item => item.id === normalized.id);
  if (idx >= 0) cases[idx] = normalized;
  else cases.unshift(normalized);
  saveCaseDirectory(cases);
  cacheCaseDetail(caseData);
  return normalized;
}

function getLocalCaseById(caseId) {
  return getCaseDirectory().find(item => item.id === caseId) || null;
}

function cacheCaseDetail(caseData) {
  if (!caseData?.id) return null;
  const cache = getCaseDetailsCache();
  cache[caseData.id] = { ...(cache[caseData.id] || {}), ...caseData };
  saveCaseDetailsCache(cache);
  return cache[caseData.id];
}

function getCachedCaseDetail(caseId) {
  const cache = getCaseDetailsCache();
  if (cache[caseId]) return cache[caseId];
  return getLocalCaseById(caseId);
}

function getIncomingDocuments() {
  try {
    return JSON.parse(localStorage.getItem(LS_INCOMING_DOCUMENTS)) || [];
  } catch {
    return [];
  }
}

function getSettingsProfileCache() {
  try {
    const raw = JSON.parse(localStorage.getItem(LS_SETTINGS_PROFILE) || 'null');
    return raw && typeof raw === 'object' ? raw : null;
  } catch {
    return null;
  }
}

function saveSettingsProfileCache(profile) {
  if (!profile || typeof profile !== 'object') return;
  localStorage.setItem(LS_SETTINGS_PROFILE, JSON.stringify(profile));
}

function getWorkflowSummaryCache() {
  try {
    const raw = JSON.parse(localStorage.getItem(LS_WORKFLOW_SUMMARY) || 'null');
    return raw && typeof raw === 'object' ? raw : null;
  } catch {
    return null;
  }
}

function saveWorkflowSummaryCache(summary) {
  if (!summary || typeof summary !== 'object') return;
  localStorage.setItem(LS_WORKFLOW_SUMMARY, JSON.stringify(summary));
}

async function fetchIncomingDocuments(filters = {}) {
  const query = new URLSearchParams();
  if (filters.status) query.set('status', filters.status);
  if (filters.case_id) query.set('case_id', filters.case_id);
  try {
    const docs = await get(`/api/documents${query.toString() ? `?${query}` : ''}`, { toastOnError: false });
    const normalized = Array.isArray(docs) ? docs : [];
    saveIncomingDocuments(normalized);
    return normalized;
  } catch {
    return getIncomingDocuments();
  }
}

function saveIncomingDocuments(documents) {
  localStorage.setItem(LS_INCOMING_DOCUMENTS, JSON.stringify(documents));
}

function sameDocumentRef(item, documentId, documentKey = '') {
  if (!item) return false;
  return item.id === documentId
    || item.document_id === documentId
    || (documentKey && item.key === documentKey);
}

function removeDocumentFromLocalState(documentId, documentKey = '', caseId = '') {
  const nextIncoming = getIncomingDocuments().filter(item => !sameDocumentRef(item, documentId, documentKey));
  saveIncomingDocuments(nextIncoming);

  const cache = getCaseDetailsCache();
  const touchedCaseIds = new Set();
  Object.entries(cache).forEach(([cachedCaseId, item]) => {
    const docs = Array.isArray(item?.docs) ? item.docs : [];
    const nextDocs = docs.filter(doc => !sameDocumentRef(doc, documentId, documentKey));
    if (nextDocs.length !== docs.length) {
      cache[cachedCaseId] = { ...item, docs: nextDocs };
      touchedCaseIds.add(cachedCaseId);
    }
  });
  if (Object.keys(cache).length) saveCaseDetailsCache(cache);

  const cases = getCaseDirectory();
  const nextCases = cases.map(item => {
    if (!item?.id) return item;
    if (caseId && item.id === caseId) {
      const cached = cache[item.id];
      const docCount = cached?.docs?.length;
      return docCount == null ? item : { ...item, doc_count: docCount };
    }
    if (touchedCaseIds.has(item.id)) {
      const cached = cache[item.id];
      const docCount = cached?.docs?.length;
      return docCount == null ? item : { ...item, doc_count: docCount };
    }
    return item;
  });
  saveCaseDirectory(nextCases);
}

function deleteIncomingDocumentLocal(documentId) {
  removeDocumentFromLocalState(documentId);
}

async function deleteDocumentRemote(documentId) {
  if (!documentId || documentId.startsWith('doc-')) {
    deleteIncomingDocumentLocal(documentId);
    return false;
  }
  try {
    await api('DELETE', `/api/documents/${documentId}`);
    removeDocumentFromLocalState(documentId);
    return true;
  } catch {
    removeDocumentFromLocalState(documentId);
    return false;
  }
}

function normalizeLookup(value) {
  return (value || '')
    .toLowerCase()
    .normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
}

function matchDocumentToCase(fileName, cases = getCaseDirectory()) {
  const haystack = normalizeLookup(fileName);
  return cases.find(item => {
    const parts = normalizeLookup(item.client_name).split(' ').filter(Boolean);
    return parts.length && parts.every(part => haystack.includes(part));
  }) || null;
}

function getDocumentsForCase(caseId) {
  return getIncomingDocuments().filter(item => item.case_id === caseId && item.status === 'assigned');
}

function mergeCaseDocuments(caseData) {
  const serverDocs = Array.isArray(caseData?.docs) ? caseData.docs : [];
  const localDocs = caseData?.id ? getDocumentsForCase(caseData.id) : [];
  const merged = [];
  const seen = new Set();

  const pushDoc = (doc, normalized = false) => {
    if (!doc) return;
    const item = normalized ? doc : {
      id: doc.id || doc.document_id || doc.key || '',
      document_id: doc.document_id || doc.id || '',
      key: doc.key || '',
      name: doc.name || 'Document',
      status: doc.status || 'attached',
      uploaded_at: doc.uploaded_at,
      data_url: doc.data_url || doc.url || '',
      url: doc.url || '',
    };
    const ref = item.document_id || item.id || item.key || `${item.name}:${item.uploaded_at || ''}`;
    if (!ref || seen.has(ref)) return;
    seen.add(ref);
    merged.push(item);
  };

  serverDocs.forEach(doc => pushDoc(doc, false));
  localDocs.forEach(doc => pushDoc({
    id: doc.id,
    document_id: doc.id,
    key: doc.key || '',
    name: doc.name,
    status: doc.status || 'attached',
    uploaded_at: doc.uploaded_at,
    data_url: doc.data_url || doc.url || '',
    url: doc.url || '',
  }, true));
  return merged;
}

async function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function intakeDocuments(files, cases = getCaseDirectory()) {
  const existing = getIncomingDocuments();
  const created = [];
  for (const file of files) {
    try {
      const remote = await upload('/api/documents/intake', file);
      created.push({
        ...remote,
        id: remote.id || remote.document_id,
        name: remote.name || file.name,
        case_id: remote.case_id || (remote.case && remote.case.id) || '',
        case_name: remote.case_name || (remote.case && remote.case.client_name) || '',
        type: remote.content_type || file.type || 'file',
        data_url: remote.url || '',
      });
    } catch (err) {
      showToast(`${file.name}: ${err.message || 'Upload failed'}`, 'error');
      const dataUrl = await fileToDataUrl(file);
      const match = matchDocumentToCase(file.name, cases);
      created.push({
        id: uid('doc'),
        name: file.name,
        type: file.type || 'file',
        size: file.size || 0,
        uploaded_at: new Date().toISOString(),
        data_url: dataUrl,
        status: match ? 'assigned' : 'unrecognized',
        case_id: match?.id || '',
        case_name: match?.client_name || '',
        _local_only: true,
      });
    }
  }
  const createdIds = new Set(created.map(d => d.id).filter(Boolean));
  const deduped = existing.filter(d => !createdIds.has(d.id));
  saveIncomingDocuments([...created, ...deduped]);
  return created;
}

function getInvoices() {
  try {
    const raw = JSON.parse(localStorage.getItem(LS_INVOICES));
    if (Array.isArray(raw) && raw.length) return raw;
  } catch {}
  return [];
}

function saveInvoices(invoices) {
  localStorage.setItem(LS_INVOICES, JSON.stringify(invoices));
}

function getInvoiceTemplates() {
  try {
    const raw = JSON.parse(localStorage.getItem(LS_INVOICE_TEMPLATES));
    if (Array.isArray(raw) && raw.length) return raw;
  } catch {}
  return [];
}

function saveInvoiceTemplates(templates) {
  localStorage.setItem(LS_INVOICE_TEMPLATES, JSON.stringify(templates));
}

function getInvoiceTemplateById(id) {
  return getInvoiceTemplates().find(template => template.id === id) || null;
}

function createInvoiceTemplate(seed = {}) {
  return {
    id: seed.id || uid('tpl'),
    name: seed.name || 'New template',
    issuer_name: seed.issuer_name || '',
    issuer_address: seed.issuer_address || '',
    issuer_email: seed.issuer_email || '',
    issuer_phone: seed.issuer_phone || '',
    issuer_vat_id: seed.issuer_vat_id || '',
    issuer_tax_number: seed.issuer_tax_number || '',
    iban: seed.iban || '',
    bic: seed.bic || '',
    payment_terms: seed.payment_terms || 'Pay within 7 days.',
    footer_note: seed.footer_note || '',
    default_vat_rate: Number(seed.default_vat_rate ?? 19),
    accent: seed.accent || '#d8bf8b',
    logo_data_url: seed.logo_data_url || '',
  };
}

function upsertInvoiceTemplate(template) {
  const templates = getInvoiceTemplates();
  const normalized = createInvoiceTemplate(template);
  const idx = templates.findIndex(item => item.id === normalized.id);
  if (idx >= 0) templates[idx] = normalized;
  else templates.unshift(normalized);
  saveInvoiceTemplates(templates);
  return normalized;
}

function uid(prefix = 'id') {
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}`;
}

function calculateInvoiceTotal(invoice) {
  return (invoice.items || []).reduce((sum, item) => {
    const qty = Number(item.quantity || 0);
    const unit = Number(item.unit_price || 0);
    return sum + qty * unit;
  }, 0);
}

function getInvoiceById(id) {
  return getInvoices().find(invoice => invoice.id === id || invoice.number === id) || null;
}

function getCaseServices(caseId) {
  if (!caseId) return [];
  try {
    const raw = JSON.parse(localStorage.getItem(`lf_case_services_${caseId}`) || '[]');
    return Array.isArray(raw) ? raw : [];
  } catch {
    return [];
  }
}

function normalizeCaseService(service = {}, index = 0) {
  return {
    id: service.id || uid('svc'),
    name: service.name || service.description || '',
    qty: Number(service.qty ?? service.quantity ?? 1),
    price: Number(service.price ?? service.unit_price ?? 0),
    unit: service.unit || 'flat',
    invoice_number: service.invoice_number || '',
    invoice_id: service.invoice_id || '',
  };
}

function saveCaseServices(caseId, services) {
  if (!caseId) return [];
  const normalized = (services || []).map(normalizeCaseService);
  localStorage.setItem(`lf_case_services_${caseId}`, JSON.stringify(normalized));
  return normalized;
}

function invoiceItemsToCaseServices(invoice) {
  return (invoice?.items || []).map((item, index) => normalizeCaseService({
    id: item.id || `svc-${index}`,
    name: item.description || '',
    qty: item.quantity ?? 1,
    price: item.unit_price ?? 0,
    unit: 'flat',
    invoice_number: invoice?.number || '',
    invoice_id: invoice?.id || '',
  }, index));
}

function caseServicesToInvoiceItems(services) {
  return (services || []).map((service, index) => {
    const normalized = normalizeCaseService(service, index);
    return {
      id: normalized.id || uid('line'),
      description: normalized.name || '',
      quantity: Number(normalized.qty || 0),
      unit_price: Number(normalized.price || 0),
    };
  });
}

function getInvoiceForCase(caseId) {
  if (!caseId) return null;
  const all = getInvoices().filter(inv => inv.case_id === caseId);
  if (!all.length) return null;
  // Keep newest, remove duplicates
  all.sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''));
  if (all.length > 1) {
    const keep = all[0];
    const deduped = getInvoices().filter(inv => inv.case_id !== caseId || inv.id === keep.id);
    saveInvoices(deduped);
  }
  return all[0];
}

function syncInvoiceWithCaseServices(caseId, services, options = {}) {
  if (!caseId) return null;
  const linkedInvoice = getInvoiceForCase(caseId);
  if (!linkedInvoice) return null;
  return upsertInvoice({
    ...linkedInvoice,
    items: caseServicesToInvoiceItems(services),
  }, options);
}

function upsertInvoice(invoice, options = {}) {
  const { persistRemote = true } = options;
  const invoices = getInvoices();
  const normalized = {
    ...invoice,
    items: (invoice.items || []).map(item => ({
      id: item.id || uid('line'),
      description: item.description || '',
      quantity: Number(item.quantity || 0),
      unit_price: Number(item.unit_price || 0),
    })),
  };
  let idx = invoices.findIndex(item => item.id === normalized.id);
  if (idx < 0 && normalized.case_id) {
    // Prevent duplicate: if a different invoice already exists for this case, merge into it
    const existingIdx = invoices.findIndex(item => item.case_id === normalized.case_id);
    if (existingIdx >= 0) {
      normalized.id = invoices[existingIdx].id;
      normalized.number = invoices[existingIdx].number;
      idx = existingIdx;
    }
  }
  if (idx >= 0) invoices[idx] = normalized;
  else invoices.unshift(normalized);
  saveInvoices(invoices);
  if (persistRemote) {
    persistInvoiceRemote(normalized);
  }
  return normalized;
}

async function persistInvoiceRemote(invoice) {
  try {
    await post('/api/invoices', invoice);
  } catch {}
}

async function uploadInvoiceAttachmentRemote(invoiceId, name, blob) {
  try {
    const file = new File([blob], name, { type: blob.type || 'application/pdf' });
    return await upload(`/api/invoices/${invoiceId}/attachments`, file);
  } catch {
    return null;
  }
}

async function deleteInvoiceAttachmentRemote(invoiceId, attachmentId) {
  if (!invoiceId || !attachmentId || attachmentId.startsWith('att-')) {
    return false;
  }
  try {
    await api('DELETE', `/api/invoices/${invoiceId}/attachments/${attachmentId}`);
    return true;
  } catch {
    return false;
  }
}

function createInvoiceDraft(seed = {}) {
  return {
    id: seed.id || uid('inv'),
    number: seed.number || `INV-${new Date().getFullYear()}-${String(getInvoices().length + 1).padStart(3, '0')}`,
    client_name: seed.client_name || '',
    client_email: seed.client_email || '',
    status: seed.status || 'draft',
    issue_date: seed.issue_date || new Date().toISOString().slice(0, 10),
    due_date: seed.due_date || new Date(Date.now() + 7 * 86400000).toISOString().slice(0, 10),
    currency: seed.currency || 'EUR',
    notes: seed.notes || '',
    case_id: seed.case_id || '',
    template_id: seed.template_id || getInvoiceTemplates()[0]?.id || '',
    attachments: seed.attachments || [],
    items: seed.items?.length
      ? seed.items
      : [{ id: uid('line'), description: 'Legal service', quantity: 1, unit_price: 0 }],
  };
}

function invoiceStatusBadge(status) {
  const map = {
    draft: 'badge-gray',
    unpaid: 'badge-yellow',
    paid: 'badge-green',
    overdue: 'badge-red',
  };
  return map[status] || 'badge-gray';
}

function invoiceDueState(invoice) {
  if (!invoice?.due_date || invoice.status === 'paid') {
    return { label: 'OK', className: 'badge-gray', daysLeft: null };
  }
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const due = new Date(invoice.due_date);
  due.setHours(0, 0, 0, 0);
  const daysLeft = Math.round((due - today) / 86400000);
  if (daysLeft < 0 || invoice.status === 'overdue') {
    return { label: `${Math.abs(daysLeft)}d overdue`, className: 'badge-red', daysLeft };
  }
  if (daysLeft <= 3) {
    return { label: `Due in ${daysLeft}d`, className: 'badge-yellow', daysLeft };
  }
  return { label: 'On track', className: 'badge-green', daysLeft };
}

function renderUser() {
  const user = getUser();
  if (!user) return;
  const nameEl = document.getElementById('user-name');
  const emailEl = document.getElementById('user-email');
  const avatarEl = document.getElementById('user-initials');
  if (nameEl) nameEl.textContent = user.name || user.email;
  if (emailEl) emailEl.textContent = user.email;
  if (avatarEl) avatarEl.textContent = initials(user.name || user.email);
}

function bindSignout() {
  const ids = ['btn-signout', 'btn-signout-mobile'];
  ids.forEach(id => {
    const btn = document.getElementById(id);
    if (btn) btn.addEventListener('click', async () => {
      await resetAllSessions();
      window.location.href = 'login.html';
    });
  });
}

function setActiveNav() {
  const page = window.location.pathname.split('/').pop() || 'index.html';
  document.querySelectorAll('.nav-item').forEach(el => {
    if (el.getAttribute('href') === page) el.classList.add('active');
    else el.classList.remove('active');
  });
}

function setViewHydrated() {
  document.body.classList.remove('view-hydrating');
}

// ─── Init ──────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  renderUser();
  bindSignout();
  setActiveNav();
  if (!getToken()) {
    await syncSessionFromSupabase();
    renderUser();
  }
});
