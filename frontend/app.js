const API_BASE = (() => {
  const el = document.querySelector('[data-api-base]');
  return el?.dataset.apiBase || 'http://localhost:8000';
})();

const SUPABASE_URL = 'https://placeholder.supabase.co';
const SUPABASE_ANON = 'placeholder';

const LS_TOKEN = 'lexflow_token';
const LS_USER = 'lexflow_user';
const LS_INVOICES = 'lexflow_invoices';

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
      items: [
        { id: 'line-1', description: 'Family reunion case package', quantity: 1, unit_price: 2380 },
      ],
    },
  ];
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
  return normalized;
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
