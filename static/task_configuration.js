(() => {
  'use strict';
  const root = document.getElementById('task-config');
  if (!root) return;
  let state = JSON.parse(document.getElementById('tc-data').textContent);
  const $ = id => document.getElementById(`tc-${id}`);
  const dialog = $('editor');
  const form = $('form');
  let editing = null;
  let saving = false;
  let dirty = false;
  let openedFingerprint = null;
  let returnFocus = null;
  let preparingDraft = false;
  let selectedView = 'tasks';
  let lastPreview = null;
  const filters = ['search', 'flow', 'stage', 'kind', 'status'];
  const normalize = value => String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const option = (value, text) => new Option(text, String(value));
  function notice(message, error = false) {
    $('notice').textContent = message;
    $('notice').classList.toggle('tc-error', error);
  }
  async function post(url, data) {
    const response = await fetch(url, {method: 'POST', credentials: 'same-origin', headers: {
      'Content-Type': 'application/json', 'X-CSRF-Token': root.dataset.csrf
    }, body: JSON.stringify(data)});
    if (response.redirected) throw new Error('Sua sessao expirou ou seu acesso mudou. Confira em outra aba; o formulario foi mantido.');
    const body = await response.json().catch(() => null);
    if (!response.ok) throw new Error(body?.error || 'Nao foi possivel salvar. Verifique sua sessao e tente novamente.');
    if (!body?.rows) throw new Error('Resposta inesperada. O formulario foi mantido.');
    return body;
  }
  function stageOptions() {
    const previous = $('stage').value;
    $('stage').replaceChildren(option('', 'Todas as etapas'));
    state.stages.filter(s => !$('flow').value || String(s.workflow_id) === $('flow').value)
      .forEach(s => $('stage').add(option(s.id, s.name)));
    $('stage').value = [...$('stage').options].some(o => o.value === previous) ? previous : '';
  }
  function readURL() {
    const params = new URLSearchParams(location.search);
    for (const name of filters) {
      if (name === 'stage') stageOptions();
      $(name).value = params.get(`tc_${name}`) || (name === 'status' ? 'current' : '');
      if (name !== 'search' && !$(name).value && name === 'status') $(name).value = 'current';
    }
    showView(['review', 'agenda'].includes(params.get('tc_view')) ? params.get('tc_view') : 'tasks', false);
  }
  function writeURL() {
    const url = new URL(location.href);
    for (const name of filters) {
      if ($(name).value) url.searchParams.set(`tc_${name}`, $(name).value);
      else url.searchParams.delete(`tc_${name}`);
    }
    url.searchParams.set('tc_view', selectedView);
    history.replaceState(null, '', url);
  }
  function showView(view, save = true) {
    selectedView = view;
    $('tasks').hidden = view !== 'tasks';
    $('review').hidden = view !== 'review';
    $('agenda').hidden = view !== 'agenda';
    root.querySelectorAll('[data-view]').forEach(button => {
      const active = button.dataset.view === view;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    if (save) writeURL();
  }
  function renderReview() {
    const changed = state.rows.filter(row => row.changed);
    $('change-count').textContent = changed.length;
    $('differences').replaceChildren();
    if (!changed.length) $('differences').append(node('p', 'Nenhuma alteracao no rascunho.', 'text-muted py-3'));
    const fields = {label: 'Nome', description: 'Orientacao', required: 'Obrigatoria', active: 'Ativa', sort_order: 'Ordem', depends_on: 'Tarefa anterior', contacts_per_week: 'Contatos por semana', delay_business_days: 'Prazo em dias uteis'};
    const value = (key, input) => {
      if (typeof input === 'boolean') return input ? 'Sim' : 'Nao';
      if (key === 'depends_on') return state.rows.find(row => row.key === input)?.label || 'Nenhuma';
      if (key === 'contacts_per_week' && input === null) return 'Todos os dias uteis';
      return input == null || input === '' ? '-' : String(input);
    };
    for (const row of changed) {
      const section = node('article', null, 'tc-diff');
      section.append(node('h3', row.label), node('p', `${row.workflow} / ${row.stage} - ${row.change}`, 'text-muted small'));
      const grid = node('div', null, 'tc-diff-grid');
      for (const text of ['Campo', `Origem v${state.base_number}`, 'Rascunho']) grid.append(node('strong', text));
      for (const [key, label] of Object.entries(fields)) {
        if (row.kind === 'conditional' && key !== 'active') continue;
        if (['inss', 'contact'].includes(row.kind) && !['label', 'description', 'active', row.kind === 'inss' ? 'delay_business_days' : 'contacts_per_week'].includes(key)) continue;
        if (row.kind === 'checklist' && ['contacts_per_week', 'delay_business_days'].includes(key)) continue;
        if (row.previous && value(key, row.previous[key]) === value(key, row[key])) continue;
        grid.append(node('span', label), node('span', row.previous ? value(key, row.previous[key]) : '-', 'text-muted'), node('span', value(key, row[key])));
      }
      section.append(grid);
      $('differences').append(section);
    }
  }
  function render() {
    $('version').textContent = state.is_draft ? `Rascunho v${state.number}` : `Em uso v${state.number}`;
    $('version-help').textContent = state.is_draft ? `Em uso: v${state.published_number}. Alteracoes ainda nao publicadas.` : 'Crie um rascunho para preparar alteracoes.';
    $('create').hidden = state.is_draft;
    $('persist').hidden = !state.is_draft;
    $('publish').hidden = !state.is_draft || !state.can_publish;
    $('publish').disabled = !!state.stale || !!state.publication_errors?.length;
    $('publication-errors').hidden = !state.publication_errors?.length;
    $('publication-errors').replaceChildren(...(state.publication_errors || []).map(message => node('li', message)));
    $('new').disabled = !state.is_draft;
    $('new').title = state.is_draft ? 'Adicionar tarefa ao rascunho' : 'Crie um rascunho primeiro';
    $('stale').hidden = !state.stale;
    $('disable').disabled = !state.is_draft;
    $('inactive-warning').hidden = !state.is_draft || !state.automatic_inactive;
    const visible = state.rows.filter(row => {
      const status = $('status').value;
      return normalize(`${row.label} ${row.description || ''}`).includes(normalize($('search').value))
        && (!$('flow').value || String(row.workflow_id) === $('flow').value)
        && (!$('stage').value || String(row.stage_id) === $('stage').value || (row.kind === 'inss' && state.stages.some(s => String(s.id) === $('stage').value && s.workflow_id === row.workflow_id)))
        && (!$('kind').value || row.kind === $('kind').value)
        && (status === 'all' || (status === 'archived' ? row.archived : !row.archived && (status === 'current' || row.active === (status === 'active'))));
    });
    $('rows').replaceChildren();
    for (const row of visible) {
      const tr = node('tr');
      const name = node('td');
      name.append(node('span', row.label, 'tc-task-title'), node('span', row.kind_label, 'tc-secondary'));
      if (row.changed) name.append(node('span', row.change + ' no rascunho', 'tc-chip mt-1'));
      const scope = node('td');
      scope.append(node('span', row.workflow), node('span', row.stage, 'tc-secondary'));
      const timing = node('td');
      timing.append(node('span', row.trigger), node('span', row.timing, 'tc-secondary'));
      const status = node('td');
      status.append(node('span', row.archived ? 'Legado' : row.active ? 'Ativa' : 'Inativa', 'tc-chip tc-chip-muted'));
      if (row.required) status.append(node('span', 'Obrigatoria', 'tc-secondary'));
      const actions = node('td');
      const button = node('button', null, row.editable ? 'tc-icon tc-edit-button' : 'tc-icon');
      button.type = 'button';
      button.dataset.taskKey = row.key;
      button.title = (row.editable ? 'Editar ' : 'Consultar ') + row.label;
      button.setAttribute('aria-label', button.title);
      // Static icons only; all configuration data is inserted through textContent.
      button.innerHTML = row.editable
        ? '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m16 3 5 5-12 12-6 1 1-6Z"/><path d="m14 5 5 5"/></svg>'
        : '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>';
      if (row.editable) button.append(node('span', 'Editar'));
      button.addEventListener('click', () => editTask(row, button));
      actions.append(button);
      tr.append(name, scope, timing, status, actions);
      $('rows').append(tr);
    }
    $('results').textContent = `${visible.length} de ${state.rows.length} modelos`;
    $('empty').hidden = visible.length !== 0;
    renderReview();
  }
  function dependencies(selected = '') {
    $('dependency').replaceChildren(option('', 'Nenhuma: entrada na etapa'));
    state.rows.filter(row => row.kind === 'checklist' && String(row.stage_id) === $('item-stage').value && row.key !== editing?.key)
      .forEach(row => $('dependency').add(option(row.key, row.label + (row.active ? '' : ' (inativa)'))));
    $('dependency').value = selected;
  }
  async function editTask(row, button) {
    if (preparingDraft) return;
    if (!row.editable || state.is_draft) {
      openEditor(row, button);
      return;
    }
    preparingDraft = true;
    button.disabled = true;
    $('create').disabled = true;
    button.setAttribute('aria-busy', 'true');
    try {
      state = await post(root.dataset.createUrl, {fingerprint: state.fingerprint});
      render();
      notice('Edicao em rascunho. Nenhuma regra em uso foi alterada.');
      const current = state.rows.find(item => item.key === row.key);
      if (!current) throw new Error('Esta tarefa mudou. Confira a lista atualizada.');
      const opener = [...$('rows').querySelectorAll('button')].find(item => item.dataset.taskKey === row.key);
      openEditor(current, opener || $('new'));
    } catch (error) { notice(error.message, true); }
    finally {
      preparingDraft = false;
      button.disabled = false;
      button.removeAttribute('aria-busy');
      $('create').disabled = false;
    }
  }
  function openEditor(row, opener) {
    editing = row;
    openedFingerprint = state.fingerprint;
    returnFocus = opener;
    dirty = false;
    form.reset();
    $('form-error').hidden = true;
    const editable = state.is_draft && (!row || row.editable);
    const conditional = row?.kind === 'conditional';
    const automatic = ['inss', 'contact', 'conditional'].includes(row?.kind);
    $('editor-title').textContent = row ? (editable ? 'Editar tarefa' : 'Consultar tarefa') : 'Nova tarefa';
    $('label').value = row?.label || '';
    $('description').value = row?.description || '';
    $('order').value = row?.sort_order ?? 1;
    $('required').checked = row?.required ?? true;
    $('active').checked = row?.active ?? true;
    $('item-stage').replaceChildren(option('', 'Selecione uma etapa'));
    state.stages.filter(s => row ? s.id === row.stage_id : s.active && state.workflows.some(w => w.id === s.workflow_id && w.active && w.slug !== '30-dias'))
      .forEach(s => $('item-stage').add(option(s.id, `${state.workflows.find(w => w.id === s.workflow_id).name} / ${s.name}`)));
    if (row && !row.stage_id) $('item-stage').add(option('automatic', `${row.workflow} / ${row.stage}`));
    $('item-stage').value = row ? row.stage_id || 'automatic' : '';
    dependencies(row?.depends_on || '');
    for (const field of form.querySelectorAll('input, textarea, select')) field.disabled = !editable;
    $('label').disabled = !editable || conditional;
    $('description').disabled = !editable || conditional;
    $('order-field').hidden = automatic;
    $('dependency-field').hidden = automatic;
    $('order').disabled = !editable || automatic;
    $('dependency').disabled = !editable || automatic;
    $('required').disabled = !editable || automatic;
    $('frequency-field').hidden = row?.kind !== 'contact';
    $('frequency').disabled = !editable || row?.kind !== 'contact';
    $('frequency').value = row?.contacts_per_week ?? 'daily';
    $('delay-field').hidden = row?.kind !== 'inss';
    $('delay').disabled = !editable || row?.kind !== 'inss';
    $('delay').required = row?.kind === 'inss';
    $('delay').value = row?.delay_business_days ?? 0;
    $('item-stage').disabled = !!row || !editable;
    $('save').hidden = !editable;
    $('save-help').hidden = !editable;
    $('readonly').hidden = editable;
    $('readonly').textContent = row?.archived ? 'Modelo legado: preservado para consulta e historico.' : !state.is_draft && row?.editable
      ? 'Configuracao em uso. Feche e crie um rascunho para preparar alteracoes.'
      : 'Regra automatica em uso. A edicao sera integrada nas proximas sprints; esta consulta nao altera a operacao.';
    $('responsible').textContent = row?.responsible || 'Dono do contrato / apoio definido';
    $('timing').textContent = row?.timing || 'Prazo da etapa';
    dialog.showModal();
    if (editable) $('label').focus();
  }
  function closeEditor() {
    if (saving || (dirty && !confirm('Descartar as alteracoes nao salvas desta tarefa?'))) return;
    dialog.close();
    returnFocus?.focus();
  }
  dialog.addEventListener('cancel', event => { event.preventDefault(); closeEditor(); });
  dialog.querySelectorAll('[data-close]').forEach(button => button.addEventListener('click', closeEditor));
  form.addEventListener('input', () => { dirty = true; });
  $('item-stage').addEventListener('change', () => dependencies());
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (saving || !state.is_draft || (editing && !editing.editable) || !form.reportValidity()) return;
    saving = true;
    $('save').disabled = true;
    $('save').textContent = 'Salvando...';
    $('form-error').hidden = true;
    let task = {label: $('label').value, description: $('description').value, stage_id: Number($('item-stage').value),
      sort_order: Number($('order').value), depends_on: $('dependency').value || null,
      required: $('required').checked, active: $('active').checked};
    if (['inss', 'contact'].includes(editing?.kind)) {
      task = {label: $('label').value, description: $('description').value, active: $('active').checked};
      if (editing.kind === 'contact') task.contacts_per_week = $('frequency').value === 'daily' ? null : Number($('frequency').value);
      else task.delay_business_days = Number($('delay').value);
    }
    if (editing?.kind === 'conditional') task = {active: $('active').checked};
    try {
      state = await post(root.dataset.saveUrl.replace('/0/task', `/${state.revision_id}/task`), {fingerprint: openedFingerprint, key: editing?.key || null, task});
      dirty = false;
      dialog.close();
      render();
      $('preview-result').hidden = true;
      notice('Tarefa salva no rascunho. A operacao continua usando a versao publicada.');
      $('new').focus();
    } catch (error) {
      $('form-error').textContent = error.message;
      $('form-error').hidden = false;
      $('form-error').scrollIntoView({block: 'nearest'});
    } finally {
      saving = false;
      $('save').disabled = false;
      $('save').textContent = 'Salvar rascunho';
    }
  });
  $('create').addEventListener('click', async () => {
    if (preparingDraft) return;
    preparingDraft = true;
    $('create').disabled = true;
    try {
      state = await post(root.dataset.createUrl, {fingerprint: state.fingerprint});
      render();
      notice('Rascunho pronto. Nenhuma regra em uso foi alterada.');
    } catch (error) { notice(error.message, true); }
    finally { preparingDraft = false; $('create').disabled = false; }
  });
  $('new').addEventListener('click', () => openEditor(null, $('new')));
  async function persistDraft(publish) {
    if (!state.is_draft || saving) return;
    if (publish && !confirm('Publicar esta configuracao para novas tarefas? Tarefas e agendas existentes permanecem preservadas.')) return;
    saving = true;
    const button = $(publish ? 'publish' : 'persist');
    button.disabled = true;
    try {
      const url = (publish ? root.dataset.publishUrl : root.dataset.persistUrl).replace('/0/', `/${state.revision_id}/`);
      state = await post(url, {fingerprint: state.fingerprint, confirm: publish});
      render();
      $('preview-result').hidden = true;
      lastPreview = null;
      notice(publish ? 'Configuracao publicada. Tarefas existentes preservadas.' : 'Rascunho salvo. Nenhuma alteracao aplicada aos clientes.');
    } catch (error) { notice(error.message, true); }
    finally { saving = false; render(); button.disabled = publish ? !!state.stale || !!state.publication_errors?.length : false; }
  }
  $('persist').addEventListener('click', () => persistDraft(false));
  $('publish').addEventListener('click', () => persistDraft(true));
  $('disable').addEventListener('click', async () => {
    if (!state.is_draft || !confirm('Inativar todos os modelos apenas neste rascunho? As tarefas dos clientes e as regras em uso nao serao alteradas.')) return;
    $('disable').disabled = true;
    try {
      state = await post(root.dataset.disableUrl.replace('/0/deactivate-all', `/${state.revision_id}/deactivate-all`), {fingerprint: state.fingerprint});
      render();
      $('preview-result').hidden = true;
      notice('Todos os modelos foram inativados no rascunho. A operacao atual foi preservada.');
    } catch (error) { notice(error.message, true); }
    finally { $('disable').disabled = !state.is_draft; }
  });
  const dayString = day => `${day.getFullYear()}-${String(day.getMonth()+1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`;
  const today = new Date();
  const concession = new Date(today); concession.setDate(concession.getDate() - 45);
  const tomorrow = new Date(today); tomorrow.setDate(tomorrow.getDate() + 1);
  $('concession').value = dayString(concession);
  $('started').value = dayString(today);
  $('effective').value = dayString(tomorrow);
  $('effective').min = dayString(tomorrow);
  const lastDay = new Date(today); lastDay.setDate(lastDay.getDate() + 91);
  $('effective').max = dayString(lastDay);
  $('preview-mode').addEventListener('change', () => {
    const replan = $('preview-mode').value === 'replan';
    root.querySelectorAll('[data-simulation-field]').forEach(el => { el.hidden = replan; el.querySelector('input').disabled = replan; });
    root.querySelectorAll('[data-replan-field]').forEach(el => { el.hidden = !replan; el.querySelector('input,select').disabled = !replan; });
    $('simulate').textContent = replan ? 'Ver impacto' : 'Simular agenda';
    $('preview-result').hidden = true;
    $('preview-error').hidden = true;
  });
  $('preview-form').addEventListener('input', () => { $('preview-result').hidden = true; lastPreview = null; });
  $('preview-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!$('preview-form').reportValidity()) return;
    $('simulate').disabled = true;
    $('preview-result').hidden = true;
    $('preview-error').hidden = true;
    const payload = {revision_id: state.revision_id, fingerprint: state.fingerprint};
    if ($('preview-mode').value === 'replan') Object.assign(payload, {lead_id: Number($('contract').value), effective_from: $('effective').value});
    else Object.assign(payload, {concession: $('concession').value, started_on: $('started').value});
    try {
      const result = await post(root.dataset.previewUrl, payload);
      lastPreview = {payload, confirmation: result.confirmation};
      $('apply').hidden = !result.can_apply;
      const dateLabel = value => value.split('-').reverse().join('/');
      const actions = {preserve: 'Preservar', deactivate: 'Inativar no futuro', create: 'Criar no futuro'};
      $('preview-summary').textContent = result.mode === 'simulation'
        ? `Versao ${result.revision_number} - Desbloqueio: ${dateLabel(result.unlock_date)} - ${result.days_left} dia(s) na entrada - ${result.counts.contact} contatos e ${result.counts.inss} tarefas INSS`
        : `Contrato #${result.lead_id} - ${result.lead_name}: ${result.counts.preserve} preservadas, ${result.counts.deactivate} inativadas e ${result.counts.create} novas na previa`;
      $('preview-assumptions').textContent = result.mode === 'simulation'
        ? 'INSS: datas estimadas supondo conclusao de cada tarefa no proprio prazo. A proxima depende da conclusao real da anterior. Prazo 0 vale no dia da liberacao, inclusive no fim de semana; contatos nunca caem no fim de semana.'
        : 'Somente contatos futuros nao iniciados entram na proposta. Conclusoes, anotacoes, INSS, tarefas avulsas, hoje e atrasos ficam preservados.' + (result.can_apply ? ' Revise antes de confirmar a aplicacao.' : ' Para aplicar, use a versao publicada como ADMIN da matriz.');
      $('preview-warnings').replaceChildren(...(result.warnings || []).map(message => node('li', message)));
      $('preview-rows').replaceChildren();
      for (const row of result.rows) {
        const tr = node('tr');
        tr.append(node('td', dateLabel(row.date)), node('td', row.label));
        if (result.mode === 'simulation') {
          const stage = state.stages.find(s => s.slug === row.stage_slug && state.workflows.some(w => w.id === s.workflow_id && w.slug === '30-dias'));
          tr.append(node('td', row.kind === 'inss' ? 'Sequencia INSS' : stage?.name || row.stage_slug), node('td', row.kind === 'inss' ? (row.depends_on ? 'Data estimada; libera apos a anterior' : 'Data estimada; entrada no fluxo') : 'Dia util; sem conclusao antecipada'));
        } else tr.append(node('td', actions[row.action]), node('td', row.reason));
        $('preview-rows').append(tr);
      }
      $('preview-empty').hidden = result.rows.length !== 0;
      $('preview-result').hidden = false;
    } catch (error) { $('preview-error').textContent = error.message; $('preview-error').hidden = false; }
    finally { $('simulate').disabled = false; }
  });
  $('apply').addEventListener('click', async () => {
    if (!lastPreview || !confirm('Aplicar esta agenda apenas ao contrato selecionado, preservando registros e tarefas iniciadas?')) return;
    $('apply').disabled = true;
    try {
      await post(root.dataset.applyUrl, {...lastPreview.payload, confirmation: lastPreview.confirmation, confirm: true});
      lastPreview = null;
      $('preview-result').hidden = true;
      notice('Agenda replanejada e auditada. Conclusoes e anotacoes preservadas.');
    } catch (error) {
      $('preview-error').textContent = error.message;
      $('preview-error').hidden = false;
      $('apply').hidden = true;
      lastPreview = null;
    } finally { $('apply').disabled = false; }
  });
  root.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));
  for (const name of filters) $(name).addEventListener(name === 'search' ? 'input' : 'change', () => {
    if (name === 'flow') stageOptions();
    writeURL(); render();
  });
  state.workflows.forEach(w => $('flow').add(option(w.id, w.name + (w.active ? '' : ' (legado)'))));
  readURL(); render();
  window.addEventListener('popstate', () => { readURL(); render(); });
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
})();
