// static/admin/archive_lot.js — the rebuilt closed-lot page (templates/archive_lot.html): the analyzer's re-run button.
// POST {json_url}/analyze runs the analysis now and caches it; the page reloads to render the new panel server-side.
import {api, toast, pending} from '../ui/state.js';

const panel = document.getElementById('arc-analysis');
const btn = document.getElementById('arc-rerun');
if (panel && btn) {
  btn.addEventListener('click', () => pending(btn, 'analyzing…', async () => {
    panel.setAttribute('aria-busy', 'true');
    try {
      const a = await api(panel.dataset.rerun, {method: 'POST'});
      toast(a.status === 'ok' ? 'Analysis updated' : `Analysis unavailable: ${a.error || 'no answer'}`,
            a.status === 'ok' ? 'ok' : 'err', 6000);
      location.reload();
    } catch (err) {
      toast(`Re-run failed: ${err.message}`, 'err', 6000);
    } finally {
      panel.removeAttribute('aria-busy');
    }
  }));
}
