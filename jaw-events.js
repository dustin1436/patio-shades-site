(function () {
  'use strict';

  // JAW Content System — shared event script (T0, measurement readiness).
  // Fires GA4 events (gtag) and Clarity custom events for: tel/mailto clicks,
  // Formspree form submits, and a one-per-session generate_lead on /thank-you.
  // No dependencies. Idempotent to include on every page. Never sends PII —
  // only link URLs/text and form metadata already visible in the DOM.

  function gaEvent(name, params) {
    if (typeof window.gtag === 'function') {
      window.gtag('event', name, Object.assign({ page_location: window.location.href }, params || {}));
    }
  }

  function clarityEvent(name) {
    if (typeof window.clarity === 'function') {
      try { window.clarity('event', name); } catch (e) { /* Clarity not ready */ }
    }
  }

  function linkText(link) {
    return (link.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 100);
  }

  document.addEventListener('click', function (event) {
    var link = event.target.closest('a[href]');
    if (!link) return;
    var href = link.getAttribute('href') || '';
    var common = { link_url: href, link_text: linkText(link) };

    if (href.indexOf('tel:') === 0) {
      gaEvent('phone_click', common);
      clarityEvent('phone_click');
    } else if (href.indexOf('mailto:') === 0) {
      gaEvent('email_click', common);
      clarityEvent('email_click');
    }
  });

  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form || form.tagName !== 'FORM') return;
    var action = form.getAttribute('action') || '';
    if (action.indexOf('formspree.io') === -1) return;
    gaEvent('contact_form_submit', { form_destination: action, form_id: form.id || 'contact-form' });
    clarityEvent('contact_form_submit');
  });

  var path = window.location.pathname.replace(/\/+$/, '') || '/';
  if (path === '/thank-you') {
    var storageKey = 'jaw_generate_lead_sent';
    var alreadySent = false;
    try { alreadySent = window.sessionStorage.getItem(storageKey) === 'true'; } catch (e) { alreadySent = false; }
    if (!alreadySent) {
      gaEvent('generate_lead', { currency: 'USD', value: 0, lead_source: 'website_contact_form' });
      clarityEvent('generate_lead');
      try { window.sessionStorage.setItem(storageKey, 'true'); } catch (e) { /* storage unavailable */ }
    }
  }
})();
