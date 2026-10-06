(() => {
  const dirty = new Set();
  document.querySelectorAll('.nav-layout-form').forEach(form => {
    const group = form.dataset.groupId;
    const table = document.querySelector(`.nav-sort-table[data-group-id="${group}"]`);
    const body = table.tBodies[0];
    const status = document.querySelector(`.nav-layout-status[data-group-id="${group}"]`);
    const save = document.querySelector(`button[form="${form.id}"]`);
    let busy = false;
    const changed = () => { dirty.add(group); status.textContent = 'Unsaved order/default'; };
    table.querySelectorAll('input[type=radio]').forEach(radio => radio.addEventListener('change', changed));
    body.addEventListener('click', event => {
      if (busy) return;
      const row = event.target.closest('tr[data-section-id]');
      if (!row) return;
      if (event.target.closest('.nav-sort-up') && row.previousElementSibling) {
        body.insertBefore(row, row.previousElementSibling); changed();
      }
      if (event.target.closest('.nav-sort-down') && row.nextElementSibling) {
        body.insertBefore(row.nextElementSibling, row); changed();
      }
    });
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (busy) return;
      const selected = table.querySelector('input[type=radio]:checked');
      if (!selected) { status.textContent = 'Choose one active default.'; return; }
      const ids = [...body.querySelectorAll('tr[data-section-id]')].map(row => Number(row.dataset.sectionId));
      busy = true; save.disabled = true; status.textContent = 'Saving…';
      const controls = [...table.querySelectorAll('input, select, button')].filter(control => !control.disabled);
      controls.forEach(control => control.disabled = true);
      try {
        const response = await fetch(form.action, {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json','Accept':'application/json'}, body:JSON.stringify({section_ids:ids, default_section_id:Number(selected.value)})});
        if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Session expired. Sign in in another tab and retry.');
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.error || 'Save failed.');
        ids.forEach((id, index) => body.querySelector(`tr[data-section-id="${id}"] input[name=display_order]`).value = (index+1)*10);
        dirty.delete(group); status.textContent = 'Order and default saved';
      } catch (error) { status.textContent = error.message; }
      finally { controls.forEach(control => control.disabled = false); busy = false; save.disabled = false; }
    });
  });
  window.addEventListener('beforeunload', event => {
    if (dirty.size) { event.preventDefault(); event.returnValue = ''; }
  });
})();
