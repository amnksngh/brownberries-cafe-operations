(() => {
  const forms = [...document.querySelectorAll('.menu-sheet-row')];
  const buttons = [...document.querySelectorAll('.menu-grid-save-all')];
  const bulk = document.getElementById('menu-grid-bulk-form');
  const search = document.getElementById('menu-grid-search');
  let saving = false;
  const snapshot = form => JSON.stringify([...new FormData(form)].map(([key, value]) =>
    [key, value instanceof File ? (value.name ? [value.name, value.size, value.lastModified] : null) : value]));
  const original = new Map(forms.map(form => [form, snapshot(form)]));
  const dirty = form => snapshot(form) !== original.get(form);
  function sync() {
    const count = forms.filter(dirty).length;
    forms.forEach(form => {
      form.classList.toggle('is-dirty', dirty(form));
      form.querySelector('.menu-row-save').disabled = saving || !dirty(form);
    });
    buttons.forEach(button => {
      button.disabled = saving || !count;
      button.textContent = saving ? 'Saving…' : count ? `Save All (${count})` : 'Save All';
    });
    document.querySelectorAll('.menu-grid-dirty-status').forEach(status => {
      status.textContent = count ? `${count} unsaved item${count === 1 ? '' : 's'}` : 'All changes saved';
    });
  }
  async function save(selected, all) {
    if (saving || !selected.length) return;
    for (const form of selected) {
      if (!form.checkValidity()) {
        form.hidden = false;
        form.style.display = '';
        search.value = '';
        form.querySelectorAll('details').forEach(details => details.open = true);
        form.scrollIntoView({block: 'center'});
        form.reportValidity();
        return;
      }
    }
    const payload = all ? new FormData() : new FormData(selected[0]);
    if (all) selected.forEach(form => {
      payload.append('item_ids', form.dataset.itemId);
      for (const [name, value] of new FormData(form)) payload.append(`${form.dataset.itemId}__${name}`, value);
    });
    saving = true;
    sync();
    // Lock controls during the request so saved values cannot diverge from the UI.
    const controls = forms.flatMap(form => [...form.elements]).filter(control => !control.disabled);
    controls.forEach(control => control.disabled = true);
    try {
      const response = await fetch(all ? bulk.action : selected[0].action, {
        method: 'POST', body: payload, credentials: 'same-origin', headers: {'Accept': 'application/json'}
      });
      if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('Session expired or unexpected response. Your edits are still here; sign in in another tab and retry.');
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || 'Could not save. Your edits have been kept.');
      controls.forEach(control => control.disabled = false);
      selected.forEach(form => {
        form.querySelectorAll('input[type=file]').forEach(input => input.value = '');
        if (result.images && result.images[form.dataset.itemId]) form.elements.image_url.value = result.images[form.dataset.itemId];
        original.set(form, snapshot(form));
        form.querySelector('.menu-row-status').textContent = 'Saved';
      });
    } catch (error) {
      selected.forEach(form => form.querySelector('.menu-row-status').textContent = 'Not saved');
      window.alert(error.message);
    } finally {
      controls.forEach(control => control.disabled = false);
      saving = false;
      sync();
    }
  }
  forms.forEach(form => {
    form.querySelectorAll(':scope > label').forEach(cell => cell.setAttribute('role', 'cell'));
    form.addEventListener('input', sync);
    form.addEventListener('change', sync);
    form.addEventListener('submit', event => { event.preventDefault(); save([form], false); });
    form.querySelector('.menu-preview-btn')?.addEventListener('click', event => {
      const data = event.currentTarget.dataset;
      ['name','image_url','short_description','description','calories','price'].forEach(key => {
        const target = {image_url:'image', short_description:'shortDescription'}[key] || key;
        data[target] = form.elements[key].value;
      });
    }, true);
  });
  buttons.forEach(button => button.addEventListener('click', () => save(forms.filter(dirty), true)));
  search?.addEventListener('input', () => {
    const query = search.value.toLowerCase().trim();
    forms.forEach(form => {
      const text = [...form.querySelectorAll('input:not([type=file]), select')].map(input =>
        input.tagName === 'SELECT' ? input.selectedOptions[0]?.textContent || '' : input.value).join(' ').toLowerCase();
      form.style.display = !query || text.includes(query) ? '' : 'none';
    });
  });
  window.addEventListener('beforeunload', event => {
    if (saving || forms.some(dirty)) { event.preventDefault(); event.returnValue = ''; }
  });
  sync();
})();
