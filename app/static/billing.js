(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const notice = (message, error = false) => {
    const element = $('billing-notice');
    element.textContent = message;
    element.classList.toggle('error', error);
  };
  const button = $('billing-checkout');
  const authenticated = document.body.dataset.authenticated === 'yes';
  const csrf = document.body.dataset.csrf || '';
  let configured = false;

  async function loadPricing() {
    try {
      const response = await fetch('/api/billing/config', {credentials:'same-origin', cache:'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || 'Payment gateway is unavailable.');
      configured = Boolean(data.ready);
      $('gateway-state').textContent = configured ? (data.environment === 'test' ? 'Test mode' : 'Available') : 'Not configured';
      $('gateway-state').classList.toggle('ready', configured);
      $('billing-description').textContent = data.purpose || 'Optional support for LumiDesk.';
      $('billing-price').textContent = configured ? data.price_display : 'Not available';
      $('billing-frequency').textContent = configured
        ? (data.mode === 'subscription' ? ('Recurring payment / ' + (data.interval || 'billing period')) : 'One-time payment')
        : 'There is no active payment offer.';
      button.disabled = !configured || !authenticated;
      button.textContent = !configured ? 'Checkout not available' : !authenticated ? 'Sign in to continue' : 'Continue to secure Checkout';
      if (!configured) notice('The project owner has not activated payments. Learning and report tools remain available.');
      else if (!authenticated) notice('Sign in with Google before starting a Checkout session.');
      else notice('You will review the final price and payment method on Stripe.');
    } catch (e) {
      configured = false;
      $('gateway-state').textContent = 'Unavailable';
      $('billing-price').textContent = 'Not available';
      button.disabled = true;
      button.textContent = 'Checkout not available';
      notice(e.message || 'Unable to load payment configuration.', true);
    }
  }

  async function loadHistory() {
    const holder = $('billing-history');
    if (!authenticated || !holder) return;
    try {
      const res = await fetch('/api/billing/history', {credentials:'same-origin', cache:'no-store'});
      const data = await res.json();
      if (!res.ok) throw Error(data.detail || 'Your payment history is unavailable.');
      const payments = Array.isArray(data.payments) ? data.payments : [];
      if (!payments.length) { holder.textContent = 'No payments have been recorded on this account.'; return; }
      holder.replaceChildren();
      for (const p of payments) {
        const row = document.createElement('div');
        row.className = 'payment-row';
        const date = document.createElement('span');
        date.textContent = new Date(p.created_at).toLocaleDateString();
        const status = document.createElement('span');
        status.className = 'payment-status';
        status.textContent = String(p.status || 'pending');
        const amount = document.createElement('span');
        const currency = String(p.currency || '').toUpperCase();
        const zero = new Set(['BIF','CLP','DJF','GNF','JPY','KMF','KRW','MGA','PYG','RWF','UGX','VND','VUV','XAF','XOF','XPF']);
        const decimal = zero.has(currency) ? 0 : 2;
        amount.textContent = currency + ' ' + (Number(p.amount_minor || 0) / (decimal ? 100 : 1)).toFixed(decimal);
        row.append(date, status, amount);
        holder.append(row);
      }
    } catch (e) { holder.textContent = e.message || 'Unable to load billing history.'; }
  }

  button.addEventListener('click', async () => {
    if (!configured || !authenticated) return;
    button.disabled = true;
    button.textContent = 'Opening Checkout…';
    try {
      const res = await fetch('/api/billing/checkout', {
        method:'POST', credentials:'same-origin',
        headers:{'x-csrf-token': csrf, 'content-type':'application/json'}, body:'{}'
      });
      const data = await res.json();
      if (!res.ok) throw Error(data.detail || 'Checkout could not start.');
      if (!data.url || !data.url.startsWith('https://checkout.stripe.com/')) throw Error('Invalid Stripe Checkout URL.');
      window.location.assign(data.url);
    } catch (e) {
      notice(e.message || 'Unable to open secure Checkout.', true);
      button.disabled = false;
      button.textContent = 'Retry secure Checkout';
    }
  });
  const q = new URLSearchParams(window.location.search);
  if (q.get('checkout') === 'returned') notice('Returned from Stripe. Payment is not confirmed until Stripe sends a signed confirmation.');
  if (q.get('checkout') === 'cancelled') notice('Checkout cancelled. No purchase is recorded.');
  loadPricing().then(() => {
    if (q.get('checkout') === 'returned') notice('Returned from Stripe. Check payment history for verified confirmation; processing can take a moment.');
  });
  loadHistory();
})();
