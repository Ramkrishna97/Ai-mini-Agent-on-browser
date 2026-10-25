'use strict';
const $ = selector => document.querySelector(selector);
const controls = $('#controls');
const configControls = $('#config-controls');
const chatForm = $('#workflow');
const activityPanel = document.querySelector('aside');
const threadScroll = document.createElement('div');
threadScroll.id = 'thread-scroll';
chatForm.insertBefore(threadScroll, controls);
threadScroll.append($('#conversation'), $('#error'), $('#tool-approval'), $('#generation'));
const composerActions = document.createElement('div');
composerActions.className = 'composer-actions';
controls.insertBefore(composerActions, $('#attachment-heading'));
composerActions.append($('#attachment-heading'), $('#new-task'), $('#run'));
$('#attach-file').textContent = '+ Attach files';
$('#run').textContent = 'Send →';
const composerDock = document.createElement('div');
composerDock.className = 'composer-dock';
chatForm.insertBefore(composerDock, controls);
composerDock.append(controls, $('#cancel'));

function makePanel(id, title, content) {
  const dialog = document.createElement('dialog');
  dialog.id = id;
  dialog.className = 'workspace-panel';
  dialog.setAttribute('aria-labelledby', `${id}-title`);
  const heading = document.createElement('div');
  heading.className = 'panel-heading';
  const label = document.createElement('h2');
  label.id = `${id}-title`;
  label.textContent = title;
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'icon-button';
  close.textContent = '×';
  close.setAttribute('aria-label', `Close ${title}`);
  close.addEventListener('click', () => dialog.close());
  heading.append(label, close);
  dialog.append(heading, content);
  chatForm.append(dialog);
  dialog.addEventListener('click', event => { if (event.target === dialog) {
    const box = dialog.getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
  }});
  return dialog;
}
const settingsPanel = makePanel('settings-panel', 'Workspace & model', configControls);
const activityDialog = makePanel('activity-panel', 'Activity & files', $('.progress'));
const helpPanel = makePanel('help-panel', 'Help & commands', $('.help'));
activityPanel.remove();
$('#workflow-config').open = true;
$('#activity-details').open = true;
$('.help').open = true;
function openSettings() { if (!settingsPanel.open) settingsPanel.showModal(); }
chatForm.addEventListener('invalid', event => {
  if (settingsPanel.contains(event.target)) {
    openSettings();
    const section = event.target.closest('details');
    if (section) section.open = true;
  }
}, true);
document.querySelectorAll('[data-panel]').forEach(button => button.addEventListener('click', () => {
  const panel = {settings: settingsPanel, activity: activityDialog, help: helpPanel}[button.dataset.panel];
  if (!panel.open) panel.showModal();
}));
$('#toggle-sidebar').addEventListener('click', () => {
  const hidden = document.body.classList.toggle('sidebar-hidden');
  $('#toggle-sidebar').setAttribute('aria-expanded', String(!hidden));
});
if (matchMedia('(max-width: 760px)').matches) {
  document.body.classList.add('sidebar-hidden');
  $('#toggle-sidebar').setAttribute('aria-expanded', 'false');
}
const instruction = $('#instruction');
const pathInput = $('#path');
const nameInput = $('#name');
const modelSelect = $('#model');
const providerSelect = $('#provider');
const ggufPath = $('#gguf-path');
let token = '', connected = false, running = false, submitting = false;
let pendingPrompt = '';
let latestConversation = [];
let sessionId = new URLSearchParams(location.search).get('session') || '';
let sessionsUpdatedAt = 0;
let choosing = false, loadingModels = false, modelsLoaded = false;
let formError = '', jobError = '', preview = null, previewTimer, previewRevision = 0;
let completionItems = [], completionIndex = 0, completionRange = null, completionTimer, completionRevision = 0;
let inspectingOutput = false, fileSignature = '', cancelRequested = false;
let pendingToolId = '';
let viewerPreference = null, viewerTarget = '', displayedText = '', displayedMode = '';
let attachments = [];
let followupMode = false, newTaskDraft = false, chatReady = false;
let hasConversation = false;
let restoredSession = false, preferredModel = '';
let generationWasRunning = false;
let modelLoading = false, modelRequestChain = Promise.resolve();
let modelResidency = {selected: '', loaded: '', busy: false, phase: 'off', error: ''};
let modelRequestRevision = 0, modelRequestPending = false;

const codeExtensions = /\.(py|pyw|js|jsx|ts|tsx|java|json|html|css|scss|cs|cpp|c|h|go|rs|sh|ps1|sql|xml|yml|yaml|md)$/i;
const codeWords = /\b(?:async|await|break|case|catch|class|const|continue|def|else|enum|except|export|false|False|finally|for|from|function|if|import|interface|let|new|null|None|public|private|return|static|struct|switch|throw|true|True|try|var|while|yield)\b/;
const codeTokens = /<\/?[A-Za-z][^>\n]*>|\/\*.*?\*\/|\/\/[^\n]*|\#[^\n]*|"(?:\\.|[^"\\])*"?|'(?:\\.|[^'\\])*'?|`(?:\\.|[^`\\])*`?|\b(?:async|await|break|case|catch|class|const|continue|def|else|enum|except|export|false|False|finally|for|from|function|if|import|interface|let|new|null|None|public|private|return|static|struct|switch|throw|true|True|try|var|while|yield)\b|\b\d+(?:\.\d+)?\b/g;

function syntaxClass(token) {
  if (token.startsWith('//') || token.startsWith('#') || token.startsWith('/*')) return 'tok-comment';
  if (token.startsWith('<')) return 'tok-tag';
  if (/^["'`]/.test(token)) return 'tok-string';
  if (/^\d/.test(token)) return 'tok-number';
  if (codeWords.test(token)) return 'tok-keyword';
  return 'tok-symbol';
}

function appendHighlighted(parent, line, language) {
  const pattern = new RegExp(codeTokens.source, 'g');
  let cursor = 0, match;
  while ((match = pattern.exec(line))) {
    const token = match[0];
    if (token.startsWith('#') && !/^(py|python|sh|shell|bash|ps1|powershell|yaml|yml)$/.test(language)) continue;
    if (token.startsWith('//') && /^(py|python|sh|shell|bash|ps1|powershell)$/.test(language)) continue;
    parent.append(document.createTextNode(line.slice(cursor, match.index)));
    const span = document.createElement('span');
    span.className = syntaxClass(token);
    span.textContent = token;
    parent.append(span);
    cursor = match.index + token.length;
  }
  parent.append(document.createTextNode(line.slice(cursor)));
}

function languageFromTarget(target) {
  const match = String(target).match(/\.([A-Za-z0-9]+)$/);
  return match ? match[1].toLowerCase() : '';
}

function highlightOutput(text, target) {
  const fragment = document.createDocumentFragment();
  const fileLanguage = languageFromTarget(target);
  let inFence = false, fenceMarker = '', language = fileLanguage;
  const lines = text.match(/[^\n]*\n|[^\n]+$/g) || [];
  for (const line of lines) {
    const fence = line.match(/^\s*(`{3,}|~{3,})([A-Za-z0-9+#.-]*)/);
    if (fence && (!inFence || fence[1][0] === fenceMarker[0])) {
      const span = document.createElement('span');
      span.className = 'tok-fence';
      span.textContent = line;
      fragment.append(span);
      inFence = !inFence;
      fenceMarker = inFence ? fence[1] : '';
      language = inFence ? (fence[2] || fileLanguage) : fileLanguage;
    } else if (inFence || fileLanguage || !text.includes('```')) {
      appendHighlighted(fragment, line, language);
    } else {
      fragment.append(document.createTextNode(line));
    }
  }
  return fragment;
}

function showOutput(text, target) {
  const automatic = codeExtensions.test(target) || /^\s*(```|~~~|\{|\[)/.test(text) || /\n```[A-Za-z]/.test(text) ? 'code' : 'text';
  const mode = viewerPreference || automatic;
  $('#view-code').setAttribute('aria-selected', String(mode === 'code'));
  $('#view-text').setAttribute('aria-selected', String(mode === 'text'));
  if (text === displayedText && mode === displayedMode) return;
  displayedText = text;
  displayedMode = mode;
  const output = $('#generation-output');
  const nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 45;
  output.className = mode === 'code' ? 'code-view' : 'text-view';
  if (mode === 'code' && text.length <= 50000) output.replaceChildren(highlightOutput(text, target));
  else output.textContent = text;
  if (nearBottom) output.scrollTop = output.scrollHeight;
}

for (const mode of ['code', 'text']) {
  $(`#view-${mode}`).addEventListener('click', () => {
    viewerPreference = mode;
    displayedMode = '';
    showOutput(displayedText, viewerTarget);
  });
}

function showErrors() {
  $('#error').textContent = formError || jobError;
  $('#error').hidden = !$('#error').textContent;
  if ($('#error').textContent && jobError) $('#activity-details').open = true;
}

function updateConfigSummary() {
  const model = providerSelect.value === 'gguf' ? ggufPath.value.split(/[\\/]/).pop() : modelSelect.value;
  const workspace = pathInput.value.split(/[\\/]/).filter(Boolean).pop();
  const parts = [model || (providerSelect.value === 'gguf' ? 'Choose GGUF' : 'Choose model')];
  if (workspace) parts.push(workspace);
  if ($('#agent-mode').checked) parts.push('Agent');
  $('#config-summary').textContent = parts.join(' · ');
  $('#header-model').textContent = model || 'Local Agent';
  $('#header-model').title = model || 'Choose a model';
  $('#workspace-caption').textContent = workspace || 'On your machine';
}

function updateControls() {
  controls.disabled = !connected || running || submitting || choosing;
  configControls.disabled = !connected || running || submitting || choosing;
  const ollama = providerSelect.value === 'ollama';
  modelSelect.required = Boolean(!followupMode && ollama && preview && preview.needs_model);
  $('#run').disabled = !instruction.value.trim() || (!followupMode && (!preview || (preview.needs_model &&
    (ollama ? (!modelSelect.value || loadingModels || modelLoading) : !ggufPath.value))));
  $('#refresh-models').disabled = loadingModels;
  $('#offload-model').disabled = !connected || running || submitting || choosing || (!modelSelect.value && !modelResidency.loaded && !modelResidency.busy);
  $('#cancel').hidden = !(running || submitting);
  $('#cancel').disabled = cancelRequested;
  $('#sidebar-new').disabled = !connected;
  $('#run').title = followupMode ? 'Send follow-up' : 'Send task';
  $('#run').setAttribute('aria-label', $('#run').title);
  $('#header-model').classList.toggle('needs-model', Boolean(!followupMode && preview?.needs_model && !(ollama ? modelSelect.value : ggufPath.value)));
  if (!running) {
    $('#top-status').textContent = modelLoading ? 'Loading model…' : modelResidency.phase === 'error' ? 'Model error' : $('#badge').textContent;
    $('#top-status').dataset.state = modelLoading ? 'running' : modelResidency.phase === 'error' ? 'failed' : $('#badge').dataset.state;
  }
  updateConfigSummary();
}

async function request(url, options = {}) {
  const headers = {...options.headers};
  if (sessionId) headers['X-Session-Id'] = sessionId;
  const response = await fetch(url, {...options, headers, cache: 'no-store'});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function post(url, payload) {
  return request(url, {method: 'POST', headers: {
    'Content-Type': 'application/json', 'X-Workflow-Token': token
  }, body: JSON.stringify(payload)});
}

function payload() {
  return {path: pathInput.value, name: $('#agent-mode').checked ? '' : nameInput.value.trim(), instruction: instruction.value.trim(),
    provider: providerSelect.value, model: modelSelect.value, gguf_path: ggufPath.value,
    temperature: Number($('#temperature').value),
    context_tokens: Number($('#context_tokens').value), max_tokens: Number($('#max_tokens').value), agent_mode: $('#agent-mode').checked,
    browser: $('#browser-enabled').checked, mcp: $('#mcp-enabled').checked, attachments};
}

providerSelect.addEventListener('change', () => {
  const ollama = providerSelect.value === 'ollama';
  $('#ollama-picker').hidden = !ollama;
  $('#gguf-picker').hidden = ollama;
  if (ollama && !modelsLoaded) loadModels();
  if (ollama && modelSelect.value) requestModelChange(modelSelect.value);
  if (!ollama && (modelResidency.loaded || modelResidency.busy)) requestModelChange('');
  schedulePreview();
});

$('#agent-mode').addEventListener('change', () => {
  $('#agent-options').hidden = !$('#agent-mode').checked;
  nameInput.disabled = $('#agent-mode').checked;
  schedulePreview();
});

function schedulePreview() {
  clearTimeout(previewTimer);
  const revision = ++previewRevision;
  preview = null;
  updateControls();
  updateComposerLimits();
  if (followupMode) {
    preview = {needs_model: false};
    $('#preview').textContent = instruction.value.trim().startsWith('/run ') ?
      'Your explicit /run command will execute in this workspace; output appears in Activity and the conversation.' :
      $('#agent-mode').checked ? 'Agent follow-up: tool requests use this chat’s approval setting.' :
      'Read-only follow-up. Use /run for an explicit command or enable Agent mode for approved tools.';
    $('#preview-details').open = false;
    $('#preview-details').hidden = true;
    updateControls();
    return;
  }
  if (!instruction.value.trim()) {
    $('#preview').textContent = "Enter a request to see the program's destination checks.";
    $('#preview-details').open = false;
    $('#preview-details').hidden = true;
    return;
  }
  $('#preview-details').hidden = false;
  previewTimer = setTimeout(async () => {
    if (!connected) return;
    try {
      const result = await post('/api/preview', payload());
      if (revision !== previewRevision) return;
      preview = result;
      const lines = [];
      if (result.action === 'agent') lines.push('Agent mode: model may request local tools; Always allow applies to this chat.');
      if (result.project) lines.push(`Generate project: ${result.project}`);
      for (const file of result.files) lines.push(`${file.action === 'edit' ? 'Back up and edit' : 'Create'}: ${file.path}`);
      for (const directory of result.directories) lines.push(`Create folder: ${directory}`);
      for (const command of result.commands) lines.push(`Run PowerShell: ${command}`);
      for (const path of result.attachments || []) lines.push(`Read-only reference: ${path}`);
      $('#preview').textContent = lines.join('\n');
      if ((result.commands || []).length || (result.files || []).some(file => file.action === 'edit')) $('#preview-details').open = true;
    } catch (error) {
      if (revision !== previewRevision) return;
      $('#preview').textContent = error.message;
      $('#preview-details').open = true;
    }
    updateControls();
  }, 350);
}

function updateComposerLimits() {
  const context = Number($('#context_tokens').value).toLocaleString();
  const output = Number($('#max_tokens').value).toLocaleString();
  $('#count').textContent = `${instruction.value.length} / ${instruction.maxLength} characters · Context ${context} tokens · Output ${output} tokens`;
}

for (const id of ['#context_tokens', '#max_tokens']) $(id).addEventListener('input', updateComposerLimits);

instruction.addEventListener('input', schedulePreview);
instruction.addEventListener('input', () => { scheduleCompletions(); });
instruction.addEventListener('click', () => { scheduleCompletions(); });
nameInput.addEventListener('input', schedulePreview);
function showModelResidency(state) {
  modelResidency = state;
  modelLoading = Boolean(state.busy);
  const label = state.selected || state.loaded;
  $('#model-preload').textContent = ({
    loading: `Loading ${label} into memory…`,
    unloading: `Offloading ${state.loaded || 'the current model'}…`,
    ready: `${state.loaded} is loaded and ready for the first prompt.`,
    off: 'No Ollama model loaded by this app.',
    error: `Model load failed: ${state.error}`
  })[state.phase] || 'No Ollama model loaded by this app.';
  updateControls();
}

function requestModelChange(model) {
  const revision = ++modelRequestRevision;
  const offloadTarget = modelSelect.value || modelResidency.loaded;
  modelRequestPending = true;
  showModelResidency({...modelResidency, selected: model, busy: true,
    phase: model ? 'loading' : 'unloading', error: ''});
  modelRequestChain = modelRequestChain.catch(() => {}).then(async () => {
    try {
      const state = await post(model ? '/api/model/load' : '/api/model/offload',
        {model: model || offloadTarget});
      if (revision === modelRequestRevision) {
        modelRequestPending = false;
        showModelResidency(state);
      }
    } catch (error) {
      if (revision === modelRequestRevision) {
        modelRequestPending = false;
        showModelResidency({...modelResidency, busy: false, phase: 'error', error: error.message});
      }
    }
  });
}

modelSelect.addEventListener('change', () => {
  requestModelChange(modelSelect.value);
  updateControls();
});
$('#offload-model').addEventListener('click', () => {
  requestModelChange('');
  modelSelect.value = '';
  preferredModel = '';
  updateControls();
});

function updateConversationMode() {
  followupMode = chatReady && !newTaskDraft;
  $('#composer-label').textContent = followupMode ? 'Ask a follow-up question' : 'Your message';
  instruction.placeholder = followupMode ? 'Ask about the result, generated code, or next steps…' :
    'Describe what to build, or ask for changes to a file…';
  $('#run').textContent = followupMode ? 'Ask follow-up →' : 'Send →';
  $('#followup-note').hidden = !followupMode;
  $('#composer-hint').textContent = followupMode ? 'Enter to ask · Shift+Enter for a new line.' :
    'Enter to send · Shift+Enter for a new line. Type / for commands or @ for output files.';
  $('#new-task').hidden = !hasConversation || running;
  $('#new-task').textContent = 'New task';
  schedulePreview();
}

async function openNewTask() {
  const tab = window.open('about:blank', '_blank');
  try {
    const created = await post('/api/sessions', {});
    const url = `/?session=${encodeURIComponent(created.id)}`;
    if (tab) tab.location.replace(url);
    else location.href = url;
    sessionsUpdatedAt = 0;
    refreshSessions();
  } catch (error) {
    if (tab) tab.close();
    formError = error.message;
    showErrors();
  }
}

async function refreshSessions() {
  if (!token || Date.now() - sessionsUpdatedAt < 4000) return;
  const result = await request('/api/sessions');
  sessionsUpdatedAt = Date.now();
  $('#session-list').replaceChildren(...result.sessions.map(entry => {
    const row = document.createElement('div');
    row.className = 'session-row';
    const link = document.createElement('a');
    link.href = `/?session=${encodeURIComponent(entry.id)}`;
    link.textContent = entry.title;
    link.title = entry.title;
    if (entry.id === sessionId) link.setAttribute('aria-current', 'page');
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.title = `Delete ${entry.title}`;
    remove.setAttribute('aria-label', `Delete ${entry.title}`);
    remove.addEventListener('click', async () => {
      if (!confirm(`Delete this conversation permanently?\n${entry.title}`)) return;
      try {
        await post('/api/sessions/delete', {id: entry.id});
        sessionsUpdatedAt = 0;
        if (entry.id === sessionId) {
          const next = result.sessions.find(item => item.id !== entry.id);
          location.replace(next ? `/?session=${encodeURIComponent(next.id)}` : '/');
        }
        else refreshSessions();
      } catch (error) { formError = error.message; showErrors(); }
    });
    row.append(link, remove);
    return row;
  }));
}

$('#new-task').addEventListener('click', openNewTask);
$('#sidebar-new').addEventListener('click', openNewTask);

async function choose(kind) {
  choosing = true;
  formError = '';
  showErrors();
  updateControls();
  try {
    const result = await post('/api/choose-file', {kind});
    if (result.path) {
      pathInput.value = result.path;
      nameInput.value = '';
      schedulePreview();
    }
  } catch (error) {
    formError = error.message;
    showErrors();
  } finally {
    choosing = false;
    updateControls();
  }
}
$('#choose-folder').addEventListener('click', () => choose('folder'));
$('#choose-file').addEventListener('click', () => choose('file'));
function renderAttachments() {
  $('#attachment-list').replaceChildren(...attachments.map((path, index) => {
    const item = document.createElement('li');
    const label = document.createElement('span');
    label.textContent = path;
    label.title = path;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = 'Remove';
    remove.setAttribute('aria-label', `Remove ${path}`);
    remove.addEventListener('click', () => { attachments.splice(index, 1); renderAttachments(); schedulePreview(); });
    item.append(label, remove);
    return item;
  }));
}
$('#attach-file').addEventListener('click', async () => {
  choosing = true; updateControls();
  try {
    const result = await post('/api/choose-file', {kind: 'attachments'});
    const selected = [...new Set([...attachments, ...(result.paths || [])])];
    if (selected.length > 8) throw new Error('Select at most 8 reference files.');
    attachments = selected;
    renderAttachments(); schedulePreview();
  } catch (error) { formError = error.message; showErrors(); }
  finally { choosing = false; updateControls(); }
});
$('#choose-gguf').addEventListener('click', async () => {
  choosing = true; updateControls();
  try {
    const result = await post('/api/choose-file', {kind: 'gguf'});
    if (result.path) {
      if (!result.path.toLowerCase().endsWith('.gguf')) throw new Error('Choose a .gguf model file.');
      ggufPath.value = result.path;
      schedulePreview();
    }
  } catch (error) { formError = error.message; showErrors(); }
  finally { choosing = false; updateControls(); }
});

async function loadModels() {
  if (loadingModels || providerSelect.value !== 'ollama') return;
  loadingModels = true;
  updateControls();
  const previous = preferredModel || modelSelect.value;
  $('#model-hint').textContent = 'Loading installed models from Ollama…';
  try {
    const data = await request('/api/models');
    modelSelect.replaceChildren(new Option('Choose a model…', ''));
    for (const model of data.models) {
      const suffix = model.can_generate ? (model.can_use_tools ? ' · tools' : ' · tools not advertised') : ` · ${model.reason}`;
      const option = new Option(`${model.name} · ${(model.size / 1e9).toFixed(1)} GB${suffix}`, model.name);
      option.disabled = !model.can_generate;
      modelSelect.add(option);
    }
    const available = data.models.filter(model => model.can_generate);
    if (available.some(model => model.name === previous)) modelSelect.value = previous;
    else if (available.length === 1) modelSelect.value = available[0].name;
    preferredModel = '';
    $('#model-hint').textContent = available.length
      ? `${available.length} local generation models available. Folder and command-only requests need no model.`
      : data.diagnostic || (data.models.length
        ? 'Only non-generation models are installed in Ollama.'
        : 'Ollama reports no installed models. Check its model location, then click Refresh.');
  } catch (error) {
    modelSelect.replaceChildren(new Option('Models unavailable', ''));
    $('#model-hint').textContent = `${error.message} Click Refresh to retry.`;
  } finally {
    modelsLoaded = true;
    loadingModels = false;
    updateControls();
  }
}
$('#refresh-models').addEventListener('click', loadModels);

function completionContext() {
  const caret = instruction.selectionStart;
  const before = instruction.value.slice(0, caret);
  const lineStart = before.lastIndexOf('\n') + 1;
  const line = before.slice(lineStart);
  const command = line.match(/^(\s*)([/!#][a-z]*)$/i);
  if (command) return {kind: 'command', prefix: command[2], start: lineStart + command[1].length, end: caret};
  const directive = line.match(/^(\s*[/!#](?:file|project|mkdir)\s+)(.*)$/i);
  if (directive) return {kind: 'file', prefix: directive[2].replace(/^"/, ''),
    start: lineStart + directive[1].length, end: caret + (instruction.value[caret] === '"' ? 1 : 0), annotation: false};
  const reference = before.match(/@(?:"([^\"]*)|([^\s"@]*))$/);
  if (!reference) return null;
  const start = caret - reference[0].length;
  if (start && /[\w@]/.test(instruction.value[start - 1])) return null;
  return {kind: 'file', prefix: reference[1] ?? reference[2] ?? '', start,
    end: caret + (instruction.value[caret] === '"' ? 1 : 0), annotation: true};
}

function hideCompletions() {
  $('#suggestions').hidden = true;
  completionItems = [];
  instruction.setAttribute('aria-expanded', 'false');
  instruction.removeAttribute('aria-activedescendant');
}

function renderCompletions() {
  const menu = $('#suggestions');
  menu.replaceChildren(...completionItems.map((item, index) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.id = `suggestion-${index}`;
    button.className = 'suggestion';
    button.setAttribute('role', 'option');
    button.setAttribute('aria-selected', String(index === completionIndex));
    const label = document.createElement('span');
    label.textContent = item.label;
    const detail = document.createElement('small');
    detail.textContent = item.detail;
    button.append(label, detail);
    button.addEventListener('mousedown', event => event.preventDefault());
    button.addEventListener('click', () => insertCompletion(index));
    return button;
  }));
  menu.hidden = !completionItems.length;
  menu.classList.remove('above');
  if (!menu.hidden && menu.getBoundingClientRect().bottom > window.innerHeight - 12) menu.classList.add('above');
  instruction.setAttribute('aria-expanded', String(!menu.hidden));
  if (!menu.hidden) instruction.setAttribute('aria-activedescendant', `suggestion-${completionIndex}`);
}

function scheduleCompletions() {
  if (followupMode) { hideCompletions(); return; }
  clearTimeout(completionTimer);
  const revision = ++completionRevision;
  const context = completionContext();
  if (!context || !connected) { hideCompletions(); return; }
  completionTimer = setTimeout(async () => {
    try {
      const result = await post('/api/suggestions', {path: pathInput.value, kind: context.kind, prefix: context.prefix});
      if (revision !== completionRevision) return;
      completionRange = context;
      completionItems = result.items;
      completionIndex = 0;
      renderCompletions();
    } catch { if (revision === completionRevision) hideCompletions(); }
  }, 100);
}

function insertCompletion(index) {
  const item = completionItems[index];
  const context = completionRange;
  if (!item || !context) return;
  const value = item.kind === 'command' ? item.insert : `${context.annotation ? '@' : ''}"${item.insert}"`;
  instruction.setRangeText(value, context.start, context.end, 'end');
  if (item.kind === 'folder') instruction.selectionStart = instruction.selectionEnd = context.start + value.length - 1;
  instruction.focus();
  hideCompletions();
  schedulePreview();
  if (item.kind === 'folder') scheduleCompletions();
}

instruction.setAttribute('aria-controls', 'suggestions');
instruction.setAttribute('aria-autocomplete', 'list');
instruction.addEventListener('keydown', event => {
  if (!completionItems.length) return;
  if (event.key === 'Escape') { event.preventDefault(); hideCompletions(); }
  else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    completionIndex = (completionIndex + (event.key === 'ArrowDown' ? 1 : -1) + completionItems.length) % completionItems.length;
    renderCompletions();
  } else if (event.key === 'Tab' || event.key === 'Enter') {
    event.preventDefault(); insertCompletion(completionIndex);
  }
});
instruction.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.defaultPrevented && !event.shiftKey && !event.isComposing && !completionItems.length) {
    event.preventDefault();
    if (!$('#run').disabled) $('#workflow').requestSubmit();
  }
});
document.addEventListener('click', event => {
  if (!event.target.closest('.request-editor')) hideCompletions();
});

$('#cancel').addEventListener('click', async () => {
  try {
    if (submitting) {
      cancelRequested = true;
      $('#cancel').textContent = 'Stopping…';
      updateControls();
      if (running) await post('/api/cancel', {});
      return;
    }
    await post('/api/cancel', {});
    cancelRequested = true;
    $('#cancel').textContent = 'Stopping…';
    updateControls();
  } catch (error) { formError = error.message; showErrors(); }
});

function renderGeneration(data) {
  const generation = data.generation || {};
  if (inspectingOutput && !running) return;
  if (data.mode === 'chat') {
    $('#generation').hidden = true;
    generationWasRunning = false;
    return;
  }
  $('#generation').hidden = !generation.model && !running && !submitting;
  if (!generation.model) {
    if (running || submitting) {
      $('#generation').open = true;
      generationWasRunning = true;
      $('#generation-phase').textContent = submitting && !running ? 'Preparing request…' :
        data.mode === 'command' ? 'Running command…' : data.mode === 'agent' ? 'Preparing agent tools…' : 'Planning and running workflow…';
      $('#generation-target').textContent = '';
      $('#generation-state').dataset.state = 'waiting';
      $('#generation-state').textContent = 'Working';
      viewerTarget = '';
      viewerPreference = 'text';
      showOutput((data.logs || []).slice(-12).join('\n') || 'Waiting for the first activity update…', '');
    } else generationWasRunning = false;
    return;
  }
  if (running && !generationWasRunning) {
    $('#generation').open = true;
    requestAnimationFrame(() => { threadScroll.scrollTop = threadScroll.scrollHeight; });
  }
  if (!running && generationWasRunning) $('#generation').open = false;
  generationWasRunning = running;
  const target = generation.target || 'Model response';
  if (target !== viewerTarget) {
    viewerTarget = target;
    viewerPreference = null;
  }
  const elapsed = generation.started_at ? Math.max(0, Math.floor(Date.now() / 1000 - generation.started_at)) : 0;
  const phase = {loading: 'Loading model', ready: 'Model loaded', generating: 'Generating',
    tool: running ? 'Using tools' : 'Response complete',
    generated: running ? 'Model turn complete' : 'Response complete'}[generation.phase] || generation.phase;
  const count = generation.generated_tokens ? ` · ${generation.generated_tokens} tokens` : '';
  const activity = generation.phase === 'tool' && generation.activity ? ` · ${generation.activity}` : '';
  $('#generation-phase').textContent = `${phase}: ${generation.model}${count} · ${elapsed}s${activity}`;
  $('#generation-target').textContent = target;
  const state = !running && ['generated', 'tool'].includes(generation.phase) ? 'complete' :
    generation.phase === 'tool' ? 'working' : generation.phase === 'generating' ? 'streaming' : 'waiting';
  $('#generation-state').dataset.state = state;
  $('#generation-state').textContent = {streaming: '● Streaming', working: 'Working', complete: 'Complete', waiting: 'Waiting'}[state];
  const text = generation.text || generation.activity ||
    (generation.phase === 'generated' ? 'Processing the model response…' :
      'Waiting for the model to produce its first token…');
  showOutput(text, target);
}

function renderConversation(items) {
  const thread = $('#conversation');
  const signature = JSON.stringify(items || []);
  if (thread.dataset.signature === signature) return;
  const nearBottom = threadScroll.scrollHeight - threadScroll.scrollTop - threadScroll.clientHeight < 80;
  thread.dataset.signature = signature;
  if (!items?.length) {
    const empty = document.createElement('div');
    empty.className = 'conversation-empty';
    const title = document.createElement('h1');
    title.textContent = 'What can we build today?';
    const subtitle = document.createElement('p');
    subtitle.textContent = 'Your models. Your files. Your workspace.';
    const starters = document.createElement('div');
    starters.className = 'starter-prompts';
    for (const [label, prompt] of [['Build something', 'Build an app that '], ['Explain code', 'Explain the code in '], ['Run a command', '/run ']]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = label;
      button.addEventListener('click', () => {
        instruction.value = prompt;
        schedulePreview();
        instruction.focus();
      });
      starters.append(button);
    }
    empty.append(title, subtitle, starters);
    thread.replaceChildren(empty);
  } else {
    thread.replaceChildren(...items.map(item => {
      const row = document.createElement('div');
      row.className = `message ${item.role === 'user' ? 'user' : 'assistant'}`;
      if (item.state) row.dataset.state = item.state;
      const avatar = document.createElement('span');
      avatar.className = 'message-avatar';
      avatar.setAttribute('aria-hidden', 'true');
      avatar.textContent = item.role === 'user' ? 'You' : 'AI';
      const body = document.createElement('div');
      body.className = 'message-body';
      const name = document.createElement('span');
      name.className = 'message-name';
      name.textContent = item.role === 'user' ? 'You' : 'Assistant';
      const content = document.createElement('div');
      content.className = 'message-text';
      appendMessageContent(content, item.text || (item.state === 'working' ? 'Working on it…' : ''));
      body.append(name, content);
      row.append(avatar, body);
      return row;
    }));
  }
  if (nearBottom) requestAnimationFrame(() => { threadScroll.scrollTop = threadScroll.scrollHeight; });
}

function appendMessageContent(parent, text) {
  const sections = text.split(/(```[a-zA-Z0-9+#.-]*\r?\n[\s\S]*?\r?\n```)/g);
  for (const section of sections) {
    if (!section) continue;
    const fenced = section.match(/^```([a-zA-Z0-9+#.-]*)\r?\n([\s\S]*?)\r?\n```$/);
    if (fenced) {
      const block = document.createElement('pre');
      block.className = 'message-code';
      if (fenced[2].length <= 12000) appendHighlighted(block, fenced[2], fenced[1]);
      else block.textContent = fenced[2];
      parent.append(block);
      continue;
    }
    const inline = /\*\*([^*\n]+)\*\*|`([^`\n]+)`/g;
    let cursor = 0, match;
    while ((match = inline.exec(section))) {
      parent.append(document.createTextNode(section.slice(cursor, match.index)));
      const element = document.createElement(match[1] ? 'strong' : 'code');
      element.textContent = match[1] || match[2];
      parent.append(element);
      cursor = match.index + match[0].length;
    }
    parent.append(document.createTextNode(section.slice(cursor)));
  }
}

function render(data) {
  if (!restoredSession && data.provider) {
    restoredSession = true;
    providerSelect.value = data.provider;
    $('#ollama-picker').hidden = data.provider !== 'ollama';
    $('#gguf-picker').hidden = data.provider !== 'gguf';
    if (data.provider === 'gguf') ggufPath.value = data.model || '';
    else preferredModel = data.model || '';
    $('#agent-mode').checked = Boolean(data.agent_mode);
    $('#agent-options').hidden = !data.agent_mode;
    nameInput.disabled = Boolean(data.agent_mode);
    $('#browser-enabled').checked = Boolean(data.browser);
    $('#mcp-enabled').checked = Boolean(data.mcp);
    if (data.temperature != null) $('#temperature').value = data.temperature;
    if (data.max_tokens != null) $('#max_tokens').value = data.max_tokens;
    if (data.context_tokens != null) $('#context_tokens').value = data.context_tokens;
    updateComposerLimits();
    if (data.path) pathInput.value = data.path;
    if (data.provider === 'ollama' && modelsLoaded && modelSelect.options.length) modelSelect.value = preferredModel;
  }
  if (data.model_preload && !modelRequestPending) {
    if (data.model_preload.selected) {
      preferredModel = data.model_preload.selected;
      if (modelsLoaded && [...modelSelect.options].some(option => option.value === preferredModel)) modelSelect.value = preferredModel;
    } else if (data.model_preload.cleared) {
      preferredModel = '';
      modelSelect.value = '';
    }
    showModelResidency(data.model_preload);
  }
  const wasRunning = running;
  running = data.status === 'running';
  latestConversation = data.conversation || [];
  hasConversation = Boolean(data.conversation?.length);
  const ready = Boolean(data.chat_ready || (running && data.mode === 'chat'));
  if (chatReady !== ready) {
    chatReady = ready;
    updateConversationMode();
  }
  $('#new-task').hidden = !hasConversation || running;
  if (running && !wasRunning) {
    inspectingOutput = false;
    if (!cancelRequested) $('#cancel').textContent = 'Stop generating';
  }
  $('#badge').dataset.state = data.status;
  $('#badge').textContent = {idle: 'Ready', running: 'Running', succeeded: 'Complete', failed: 'Failed', cancelled: 'Stopped'}[data.status];
  if (submitting && !running) {
    $('#badge').textContent = 'Starting';
    $('#badge').dataset.state = 'running';
  }
  $('#top-status').textContent = $('#badge').textContent;
  $('#top-status').dataset.state = data.status;
  if (submitting && !running) {
    $('#top-status').textContent = 'Starting…';
    $('#top-status').dataset.state = 'running';
  }
  $('#status').textContent = {
    idle: 'Choose a destination or reference files in your request. Python checks the operations before running.',
    running: data.mode === 'chat' ? 'Answering your follow-up question…' : data.mode === 'command' ? 'Running your command. Live output appears in Activity.' : data.mode === 'agent' ? 'Agent is working. Refreshing this page will not cancel the job.' : 'Following the fixed workflow. Refreshing this page will not cancel the job.',
    succeeded: data.mode === 'chat' ? 'Follow-up answered. Ask another question or start a new task.' : data.mode === 'command' ? 'Command finished. Read its output in the conversation.' : data.mode === 'agent' ? 'Agent finished. Read its response in the conversation.' : `Done. ${(data.files || []).length} files saved; ${(data.commands || []).length} commands executed.`,
    failed: 'The workflow failed. Check the error and recovery details below.',
    cancelled: 'Workflow stopped. Uncommitted generated files were not saved.'
  }[data.status];
  if (submitting && !running) $('#status').textContent = 'Preparing the request…';
  if (data.pending_tool) $('#status').textContent = 'Waiting for your approval of a model tool request.';
  const pending = data.pending_tool;
  const previousPendingId = pendingToolId;
  pendingToolId = pending?.id || '';
  $('#tool-approval').hidden = !pending;
  if (pending) {
    $('#tool-name').textContent = pending.name;
    $('#tool-arguments').textContent = JSON.stringify(pending.arguments, null, 2);
    $('#always-scope').textContent = 'Always allow approves every future tool request in this chat, including file changes, commands, browser access, and enabled MCP tools. New chats ask again.';
    if (pendingToolId !== previousPendingId) {
      $('#tool-details').open = false;
      requestAnimationFrame(() => $('#tool-approval').scrollIntoView({block: 'nearest'}));
    }
  }
  $('#agent-result').hidden = true;
  $('#agent-answer').textContent = data.answer || '';
  const conversation = submitting && !running && pendingPrompt ?
    [...latestConversation, {role: 'user', text: pendingPrompt},
      {role: 'assistant', text: 'Preparing the request…', state: 'working'}] : latestConversation;
  renderConversation(conversation);
  const text = data.logs.join('\n') || 'Your workflow log will appear here.';
  if ($('#logs').textContent !== text) {
    $('#logs').textContent = text;
    $('#logs').scrollTop = $('#logs').scrollHeight;
  }
  $('#result').hidden = !data.backup;
  $('#backup').textContent = data.backup;
  $('#output-result').hidden = data.status !== 'succeeded';
  $('#output-path').textContent = data.path || '';
  const signature = (data.files || []).join('\n');
  if (signature !== fileSignature) {
    fileSignature = signature;
    $('#output-files').replaceChildren(...(data.files || []).map((path, index) => {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'output-link'; button.textContent = path;
      button.addEventListener('click', async () => {
        try {
          const output = await post('/api/output', {index});
          inspectingOutput = true;
          if (activityDialog.open) activityDialog.close();
          $('#generation').hidden = false;
          $('#generation').open = true;
          requestAnimationFrame(() => $('#generation').scrollIntoView({block: 'nearest'}));
          $('#generation-phase').textContent = 'Saved file contents';
          $('#generation-target').textContent = output.path;
          $('#generation-state').dataset.state = 'complete';
          $('#generation-state').textContent = 'Saved';
          viewerTarget = output.path;
          viewerPreference = null;
          showOutput(output.text + (output.truncated ? '\n[Preview truncated]' : ''), output.path);
        } catch (error) { formError = error.message; showErrors(); }
      });
      item.append(button); return item;
    }));
  }
  renderGeneration(data);
  jobError = data.error;
  showErrors();
  updateControls();
}

$('#workflow').addEventListener('submit', async event => {
  event.preventDefault();
  if (submitting || running || choosing || !instruction.value.trim() || (!followupMode && !preview)) return;
  submitting = true;
  if (matchMedia('(max-width: 760px)').matches) {
    document.body.classList.add('sidebar-hidden');
    $('#toggle-sidebar').setAttribute('aria-expanded', 'false');
  }
  pendingPrompt = instruction.value.trim();
  inspectingOutput = false;
  cancelRequested = false;
  $('#cancel').textContent = 'Stop request';
  hideCompletions();
  formError = '';
  jobError = '';
  showErrors();
  updateControls();
  renderConversation([...latestConversation, {role: 'user', text: pendingPrompt},
    {role: 'assistant', text: 'Preparing the request…', state: 'working'}]);
  renderGeneration({mode: followupMode ? 'chat' : 'auto', generation: {}, logs: []});
  $('#top-status').textContent = 'Starting…';
  $('#top-status').dataset.state = 'running';
  try {
    if (followupMode) await post('/api/chat', {question: instruction.value.trim(), attachments,
      agent_mode: $('#agent-mode').checked, browser: $('#browser-enabled').checked, mcp: $('#mcp-enabled').checked});
    else {
      if (hasConversation) {
        const created = await post('/api/sessions', {});
        sessionId = created.id;
        history.pushState(null, '', `/?session=${encodeURIComponent(sessionId)}`);
        hasConversation = false;
        sessionsUpdatedAt = 0;
      }
      if (cancelRequested) {
        submitting = false;
        pendingPrompt = '';
        cancelRequested = false;
        render(await request('/api/status'));
        return;
      }
      await post('/api/run', payload());
    }
    if (cancelRequested) await post('/api/cancel', {});
    newTaskDraft = false;
    instruction.value = '';
    attachments = [];
    renderAttachments();
    submitting = false;
    pendingPrompt = '';
    render(await request('/api/status'));
    schedulePreview();
  } catch (error) {
    submitting = false;
    pendingPrompt = '';
    cancelRequested = false;
    formError = error.message;
    try { render(await request('/api/status')); }
    catch { connected = false; }
    showErrors();
    updateControls();
  }
});

async function decideTool(allowed, always = false) {
  const id = pendingToolId;
  if (!id) return;
  for (const button of ['#approve-tool', '#approve-always', '#deny-tool']) $(button).disabled = true;
  try { await post('/api/approve', {id, allowed, always}); }
  catch (error) { formError = error.message; showErrors(); }
  finally {
    for (const button of ['#approve-tool', '#approve-always', '#deny-tool']) $(button).disabled = false;
    try { render(await request('/api/status')); } catch { /* next poll reconnects */ }
  }
}
$('#approve-tool').addEventListener('click', () => decideTool(true));
$('#approve-always').addEventListener('click', () => decideTool(true, true));
$('#deny-tool').addEventListener('click', () => decideTool(false));

async function poll() {
  try {
    if (!token) {
      let session;
      try { session = await request(`/api/session${sessionId ? `?id=${encodeURIComponent(sessionId)}` : ''}`); }
      catch (error) {
        if (!sessionId) throw error;
        sessionId = '';
        session = await request('/api/session');
      }
      if (session.version !== 12) throw new Error('Restart the launcher to load the updated workflow server.');
      token = session.token;
      sessionId = session.id;
      if (new URLSearchParams(location.search).get('session') !== sessionId)
        history.replaceState(null, '', `/?session=${encodeURIComponent(sessionId)}`);
      if (!pathInput.value) {
        pathInput.value = session.workspace;
        nameInput.value = session.default_name;
      }
    }
    const polledSessionId = sessionId;
    const data = await request('/api/status');
    if (polledSessionId !== sessionId) return;
    if (!connected) {
      formError = '';
      connected = true;
      schedulePreview();
    }
    render(data);
    refreshSessions().catch(() => {});
    if (providerSelect.value === 'ollama' && !modelsLoaded && !loadingModels) loadModels();
  } catch (error) {
    token = '';
    connected = false;
    modelsLoaded = false;
    $('#badge').textContent = 'Disconnected';
    $('#top-status').textContent = 'Disconnected';
    $('#top-status').dataset.state = 'failed';
    $('#status').textContent = 'Double-click start.bat to start the application. Reconnecting…';
    formError = error.message;
    showErrors();
    updateControls();
  } finally {
    setTimeout(poll, running ? 500 : 1500);
  }
}

if (location.protocol === 'file:') {
  $('#badge').textContent = 'Start the launcher';
  $('#status').textContent = 'Double-click start.bat. It starts the server and opens the browser interface.';
} else {
  poll();
}
