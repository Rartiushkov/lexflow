const API_BASE = (() => {
  const el = document.querySelector('[data-api-base]');
  return el?.dataset.apiBase || 'http://localhost:8000';
})();

const SUPABASE_URL = 'https://placeholder.supabase.co';
const SUPABASE_ANON = 'placeholder';

const LS_TOKEN = 'lexflow_token';
const LS_USER = 'lexflow_user';
const LS_INVOICES = 'lexflow_invoices';
const LS_INVOICE_TEMPLATES = 'lexflow_invoice_templates';
const LS_CASE_DIRECTORY = 'lexflow_case_directory';
const LS_INCOMING_DOCUMENTS = 'lexflow_incoming_documents';

// ─── Demo auth fallback ─────────────────────────────────
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

function requireAuth() {
  if (!getToken()) {
    window.location.href = 'login.html';
  }
}

function requireGuest() {
  if (getToken()) {
    window.location.href = 'dashboard.html';
  }
}

// ─── API helpers ─────────────────────────────────────────
async function api(method, path, body) {
  const url = `${API_BASE}${path}`;
  const opts = {
    method,
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${getToken() || ''}`,
    },
  };
  if (body) opts.body = JSON.stringify(body);
  try {
    const res = await fetch(url, opts);
    const text = await res.text();
    const data = text ? JSON.parse(text) : null;
    if (!res.ok) throw new Error(data?.detail || data?.error || `HTTP ${res.status}`);
    return data;
  } catch (err) {
    showToast(err.message || 'Network error', 'error');
    throw err;
  }
}

function get(path) { return api('GET', path); }
function post(path, body) { return api('POST', path, body); }
function patch(path, body) { return api('PATCH', path, body); }

async function upload(path, file) {
  const url = `${API_BASE}${path}`;
  const form = new FormData();
  form.append('file', file);
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Authorization': `Bearer ${getToken() || ''}` },
    body: form,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new Error(data?.detail || data?.error || `HTTP ${res.status}`);
  return data;
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

function invoiceSeed() {
  return [
    {
      id: 'inv-2026-001',
      number: 'INV-2026-001',
      client_name: 'Marco Rossi',
      client_email: 'marco.rossi@example.com',
      status: 'unpaid',
      issue_date: '2026-07-04',
      due_date: '2026-07-11',
      currency: 'EUR',
      notes: 'Transfer after payment confirmation.',
      case_id: '2',
      template_id: 'tpl-default',
      items: [
        { id: 'line-1', description: 'ICT permit filing', quantity: 1, unit_price: 1190 },
      ],
    },
    {
      id: 'inv-2026-002',
      number: 'INV-2026-002',
      client_name: 'Elena Petrova',
      client_email: 'elena.petrova@example.com',
      status: 'paid',
      issue_date: '2026-07-04',
      due_date: '2026-07-10',
      currency: 'EUR',
      notes: 'Paid and archived.',
      case_id: '3',
      template_id: 'tpl-default',
      items: [
        { id: 'line-1', description: 'Family reunion case package', quantity: 1, unit_price: 2380 },
      ],
    },
  ];
}

function caseDirectorySeed() {
  return [
    {
      id: '1',
      client_name: 'Anna Schmidt',
      client_email: 'anna@example.com',
      case_type: 'Blue Card',
      destination: 'Germany',
      stage: 'documents',
      created_at: new Date().toISOString(),
    },
    {
      id: '2',
      client_name: 'Marco Rossi',
      client_email: 'marco@example.com',
      case_type: 'ICT permit',
      destination: 'Netherlands',
      stage: 'payment',
      created_at: new Date().toISOString(),
    },
    {
      id: '3',
      client_name: 'Elena Petrova',
      client_email: 'elena@example.com',
      case_type: 'Family reunion',
      destination: 'France',
      stage: 'processing',
      created_at: new Date().toISOString(),
    },
  ];
}

function getCaseDirectory() {
  try {
    const raw = JSON.parse(localStorage.getItem(LS_CASE_DIRECTORY));
    if (Array.isArray(raw) && raw.length) return raw;
  } catch {}
  const seeded = caseDirectorySeed();
  localStorage.setItem(LS_CASE_DIRECTORY, JSON.stringify(seeded));
  return seeded;
}

function saveCaseDirectory(cases) {
  localStorage.setItem(LS_CASE_DIRECTORY, JSON.stringify(cases));
}

function setCaseDirectory(cases) {
  const normalized = (cases || []).map(item => ({
    id: item.id,
    client_name: item.client_name,
    client_email: item.client_email || '',
    case_type: item.case_type || item.type || '',
    destination: item.destination || item.country || '',
    stage: item.stage || 'documents',
    created_at: item.created_at || new Date().toISOString(),
  }));
  if (normalized.length) saveCaseDirectory(normalized);
  return normalized;
}

function upsertLocalCase(caseData) {
  const cases = getCaseDirectory();
  const normalized = setCaseDirectory([caseData])[0];
  const idx = cases.findIndex(item => item.id === normalized.id);
  if (idx >= 0) cases[idx] = normalized;
  else cases.unshift(normalized);
  saveCaseDirectory(cases);
  return normalized;
}

function getLocalCaseById(caseId) {
  return getCaseDirectory().find(item => item.id === caseId) || null;
}

function getIncomingDocuments() {
  try {
    return JSON.parse(localStorage.getItem(LS_INCOMING_DOCUMENTS)) || [];
  } catch {
    return [];
  }
}

async function fetchIncomingDocuments(filters = {}) {
  const query = new URLSearchParams();
  if (filters.status) query.set('status', filters.status);
  if (filters.case_id) query.set('case_id', filters.case_id);
  try {
    const docs = await get(`/api/documents${query.toString() ? `?${query}` : ''}`);
    return Array.isArray(docs) ? docs : [];
  } catch {
    return getIncomingDocuments();
  }
}

function saveIncomingDocuments(documents) {
  localStorage.setItem(LS_INCOMING_DOCUMENTS, JSON.stringify(documents));
}

function deleteIncomingDocumentLocal(documentId) {
  saveIncomingDocuments(getIncomingDocuments().filter(item => item.id !== documentId));
}

async function deleteDocumentRemote(documentId) {
  if (!documentId || documentId.startsWith('doc-')) {
    deleteIncomingDocumentLocal(documentId);
    return false;
  }
  try {
    await api('DELETE', `/api/documents/${documentId}`);
    deleteIncomingDocumentLocal(documentId);
    return true;
  } catch {
    deleteIncomingDocumentLocal(documentId);
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
        type: remote.content_type || file.type || 'file',
        data_url: remote.url || '',
      });
    } catch {
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
      });
    }
  }
  saveIncomingDocuments([...created, ...existing]);
  return created;
}

function getInvoices() {
  try {
    const raw = JSON.parse(localStorage.getItem(LS_INVOICES));
    if (Array.isArray(raw) && raw.length) return raw;
  } catch {}
  const seeded = invoiceSeed();
  localStorage.setItem(LS_INVOICES, JSON.stringify(seeded));
  return seeded;
}

function saveInvoices(invoices) {
  localStorage.setItem(LS_INVOICES, JSON.stringify(invoices));
}

function templateSeed() {
  return [
    {
      id: 'tpl-default',
      name: 'Standard DE/EU',
      issuer_name: 'LexFlow Legal GmbH',
      issuer_address: 'Musterstrasse 12, 10115 Berlin, Germany',
      issuer_email: 'billing@lexflow.eu',
      issuer_phone: '+49 30 0000 0000',
      issuer_vat_id: 'DE123456789',
      issuer_tax_number: '30/123/45678',
      iban: 'DE89370400440532013000',
      bic: 'COBADEFFXXX',
      payment_terms: 'Pay within 7 days.',
      footer_note: 'Please include the invoice number in the transfer reference.',
      default_vat_rate: 19,
      accent: '#d8bf8b',
      logo_data_url: '',
    },
  ];
}

function getInvoiceTemplates() {
  try {
    const raw = JSON.parse(localStorage.getItem(LS_INVOICE_TEMPLATES));
    if (Array.isArray(raw) && raw.length) return raw;
  } catch {}
  const seeded = templateSeed();
  localStorage.setItem(LS_INVOICE_TEMPLATES, JSON.stringify(seeded));
  return seeded;
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

function upsertInvoice(invoice) {
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
  const idx = invoices.findIndex(item => item.id === normalized.id);
  if (idx >= 0) invoices[idx] = normalized;
  else invoices.unshift(normalized);
  saveInvoices(invoices);
  persistInvoiceRemote(normalized);
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
    if (btn) btn.addEventListener('click', () => {
      clearSession();
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

// ─── Init ──────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  renderUser();
  bindSignout();
  setActiveNav();
});
