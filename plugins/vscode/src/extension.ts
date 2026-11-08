import * as vscode from 'vscode';
import { spawn } from 'node:child_process';
import { closeSync, existsSync, openSync } from 'node:fs';
import { join, resolve } from 'node:path';

type Json = Record<string, any>;

class Backend {
  private token = '';
  private session = '';
  private root = '';
  private starting?: Promise<void>;
  private ready = false;

  constructor(private readonly context: vscode.ExtensionContext) {}

  private get base(): string {
    return `http://127.0.0.1:${vscode.workspace.getConfiguration('aiAgentMini').get<number>('port', 8765)}`;
  }

  private async raw(path: string, method = 'GET', body?: Json): Promise<Json> {
    const response = await fetch(this.base + path, {
      method,
      headers: { 'Content-Type': 'application/json', ...(this.token ? { 'X-Workflow-Token': this.token } : {}),
        ...(this.session ? { 'X-Session-Id': this.session } : {}) },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(15000),
    });
    const data = await response.json() as Json;
    if (!response.ok) throw new Error(String(data.error || `Backend returned ${response.status}`));
    return data;
  }

  private async boot(): Promise<void> {
    if (this.ready) return;
    if (this.starting) return this.starting;
    this.starting = (async () => {
      let health: Json | undefined;
      try {
        health = await this.raw('/api/health');
      } catch { /* The backend might be absent or older. */ }
      if (health) {
        if (health.application !== 'local-workflow' || health.version !== 19)
          throw new Error('Restart the AI Agent Mini backend to load the IDE API.');
        this.ready = true;
        return;
      }
      try {
        const previous = await this.raw('/api/session');
        if (previous.application === 'local-workflow' && previous.version !== 19)
          throw new Error('Restart the AI Agent Mini backend to load the IDE API.');
      } catch (error) {
        if (error instanceof Error && error.message.startsWith('Restart the AI Agent Mini')) throw error;
      }
      const configured = vscode.workspace.getConfiguration('aiAgentMini').get<string>('appPath', '') || process.env.AI_AGENT_MINI_HOME || '';
      const bundled = join(this.context.extensionPath, 'backend');
      const checkout = resolve(this.context.extensionPath, '..', '..');
      const home = resolve(configured || (existsSync(join(bundled, 'ui_server.py')) ? bundled : checkout));
      if (!existsSync(join(home, 'ide_bootstrap.py')))
        throw new Error('Plugin backend is missing. Reinstall the VSIX or set aiAgentMini.appPath.');
      const python = process.platform === 'win32' ? join(home, '.venv', 'Scripts', 'python.exe') : join(home, '.venv', 'bin', 'python');
      const executable = existsSync(python) ? python : process.platform === 'win32' ? 'python' : 'python3';
      const log = join(home, 'backend.log');
      const logFile = openSync(log, 'a');
      const child = spawn(executable, [join(home, 'ide_bootstrap.py'), '--port',
        String(vscode.workspace.getConfiguration('aiAgentMini').get<number>('port', 8765))],
        { cwd: home, detached: true, stdio: ['ignore', logFile, logFile], windowsHide: true });
      closeSync(logFile);
      let launchError: Error | undefined;
      child.once('error', error => { launchError = error; });
      child.once('exit', code => { if (code !== 0) launchError = new Error(`Backend exited with code ${code}; see ${log}`); });
      child.unref();
      for (let attempt = 0; attempt < 600; attempt++) {
        await new Promise(r => setTimeout(r, 500));
        let current: Json | undefined;
        try { current = await this.raw('/api/health'); } catch { /* Still starting. */ }
        if (current) {
          if (current.application !== 'local-workflow' || current.version !== 19)
            throw new Error('Restart the AI Agent Mini backend to load the IDE API.');
          this.ready = true; return;
        }
        if (launchError) throw new Error(`Could not start Python: ${launchError.message}`);
      }
      throw new Error('AI Agent Mini did not start. Check that Python 3.12+ is installed and restart the plugin.');
    })();
    try { await this.starting; } finally { this.starting = undefined; }
  }

  async connect(root: string): Promise<void> {
    await this.boot();
    if (root !== this.root || !this.session) {
      this.root = root;
      const saved = this.context.workspaceState.get<string>(`session:${root}`, '');
      let response: Json;
      try { response = await this.raw(`/api/session${saved ? `?id=${encodeURIComponent(saved)}` : ''}`); }
      catch { response = await this.raw('/api/session'); }
      this.token = response.token;
      this.session = response.id;
      await this.context.workspaceState.update(`session:${root}`, this.session);
    }
  }

  async request(path: string, body?: Json): Promise<Json> {
    try { return await this.raw(path, body ? 'POST' : 'GET', body); }
    catch (error) {
      if (error instanceof TypeError || (error instanceof Error &&
          (error.message.includes('Session expired') || error.message.includes('Conversation not found')))) {
        this.ready = false;
        this.session = '';
        this.token = '';
      }
      throw error;
    }
  }

  async newChat(): Promise<void> {
    const response = await this.request('/api/sessions', {});
    this.session = response.id;
    await this.context.workspaceState.update(`session:${this.root}`, this.session);
  }
}

class ChatView implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;
  private timer?: NodeJS.Timeout;
  private lastState?: Json;
  private model = '';
  private provider = 'ollama';
  private ggufPath = '';
  private endpoint = 'http://localhost:1234';
  private contextTokens = 16384;
  private extraSkillRoot = '';
  private extraMcpConfig = '';
  private modelsLoaded = false;
  private effort = 'off';
  private mcp = false;
  private browser = false;
  private busy = false;
  private backend: Backend;

  constructor(private readonly context: vscode.ExtensionContext) { this.backend = new Backend(context); }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = this.html();
    view.webview.onDidReceiveMessage(message => void this.handle(message).catch(error => this.error(error)));
    this.timer = setInterval(() => void this.refresh().catch(error => this.error(error)), 1000);
    void this.refresh().catch(error => this.error(error));
    view.onDidDispose(() => { if (this.timer) clearInterval(this.timer); this.view = undefined; });
  }

  private workspace(): vscode.WorkspaceFolder {
    const editor = vscode.window.activeTextEditor;
    const folder = editor && vscode.workspace.getWorkspaceFolder(editor.document.uri);
    const chosen = folder || vscode.workspace.workspaceFolders?.[0];
    if (!chosen || chosen.uri.scheme !== 'file') throw new Error('Open a local workspace folder first.');
    return chosen;
  }

  private send(value: Json): void { void this.view?.webview.postMessage(value); }
  private error(error: unknown): void { this.send({ type: 'error', message: String(error instanceof Error ? error.message : error) }); }

  private async refresh(): Promise<void> {
    if (!this.view || this.busy) return;
    this.busy = true;
    try {
      const root = this.workspace();
      await this.backend.connect(root.uri.fsPath);
      if (!this.modelsLoaded) {
        this.modelsLoaded = true;
        void this.listModels().catch(error => { this.modelsLoaded = false; this.error(error); });
      }
      const status = await this.backend.request('/api/status');
      this.lastState = status;
      if (!this.model) {
        const candidate = this.provider === 'ollama'
          ? status.model_preload?.selected || (status.provider === 'ollama' ? status.model : '') || ''
          : status.provider === this.provider ? status.model || '' : '';
        if (candidate !== 'Choose model') this.model = candidate;
      }
      if (this.provider === 'gguf' && !this.ggufPath && status.provider === 'gguf') this.ggufPath = status.model || '';
      this.send({ type: 'status', status, workspace: root.uri.fsPath, model: this.model,
        provider: this.provider, ggufPath: this.ggufPath, effort: this.effort,
        extraSkillRoot: this.extraSkillRoot, extraMcpConfig: this.extraMcpConfig });
    } finally { this.busy = false; }
  }

  private async listModels(): Promise<void> {
    const data = this.provider === 'lmstudio'
      ? await this.backend.request('/api/lmstudio/models', { endpoint: this.endpoint })
      : this.provider === 'ollama' ? await this.backend.request('/api/models') : { models: [] };
    this.send({ type: 'models', models: data.models, provider: this.provider });
  }

  private async handle(message: Json): Promise<void> {
    const root = this.workspace();
    await this.backend.connect(root.uri.fsPath);
    if (message.type === 'start') {
      await this.refresh();
    } else if (message.type === 'provider') {
      this.provider = ['ollama', 'gguf', 'lmstudio'].includes(message.provider) ? message.provider : 'ollama';
      this.endpoint = String(message.endpoint || this.endpoint);
      this.model = '';
      this.modelsLoaded = false;
      await this.listModels();
      this.modelsLoaded = true;
      await this.refresh();
    } else if (message.type === 'models') {
      this.endpoint = String(message.endpoint || this.endpoint);
      await this.listModels();
      this.modelsLoaded = true;
    } else if (message.type === 'gguf') {
      const chosen = await vscode.window.showOpenDialog({ canSelectFiles: true, canSelectFolders: false,
        canSelectMany: false, filters: { 'GGUF models': ['gguf'] }, openLabel: 'Use GGUF model' });
      if (chosen?.[0]) { this.ggufPath = chosen[0].fsPath; this.model = this.ggufPath; await this.refresh(); }
    } else if (message.type === 'lmSkills') {
      const chosen = await vscode.window.showOpenDialog({ canSelectFiles: false, canSelectFolders: true,
        canSelectMany: false, openLabel: 'Use skill folder' });
      if (chosen?.[0]) { this.extraSkillRoot = chosen[0].fsPath; await this.refresh(); }
    } else if (message.type === 'lmMcp') {
      const chosen = await vscode.window.showOpenDialog({ canSelectFiles: true, canSelectFolders: false,
        canSelectMany: false, filters: { 'JSON configuration': ['json'] }, openLabel: 'Use MCP configuration' });
      if (chosen?.[0]) { this.extraMcpConfig = chosen[0].fsPath; await this.refresh(); }
    } else if (message.type === 'load') {
      this.model = String(message.model || '');
      if (!this.model) throw new Error('Choose a model from the list.');
      this.contextTokens = Number(message.contextTokens || this.contextTokens);
      if (this.provider === 'ollama') await this.backend.request('/api/model/load', { model: this.model });
      if (this.provider === 'lmstudio') await this.backend.request('/api/lmstudio/load', {
        model: this.model, endpoint: String(message.endpoint || this.endpoint), context_tokens: this.contextTokens });
      await this.refresh();
    } else if (message.type === 'prompt') {
      this.provider = ['ollama', 'gguf', 'lmstudio'].includes(message.provider) ? message.provider : 'ollama';
      this.endpoint = String(message.endpoint || this.endpoint);
      this.contextTokens = Number(message.contextTokens || this.contextTokens);
      this.model = this.provider === 'gguf' ? this.ggufPath : String(message.model || this.model);
      if (!this.model) throw new Error('Choose a model first.');
      this.effort = ['off', 'low', 'medium', 'high'].includes(message.effort) ? message.effort : 'off';
      this.mcp = message.mcp === true;
      this.browser = message.browser === true;
      const text = String(message.text || '').trim();
      if (!text) return;
      const active = vscode.window.activeTextEditor?.document.uri;
      const file = active?.scheme === 'file' && vscode.workspace.getWorkspaceFolder(active)?.uri.fsPath === root.uri.fsPath
        ? `Active editor: ${vscode.workspace.asRelativePath(active, false)}\n` : '';
      const instruction = `${file}${text}`.slice(0, 4000);
      const status = this.lastState;
      if (status?.chat_ready && status.conversation?.length &&
          status.provider === this.provider && status.model === this.model) {
        await this.backend.request('/api/chat', { question: instruction, agent_mode: true, mcp: this.mcp,
          browser: this.browser, thinking_effort: this.effort });
      } else {
        if (status?.conversation?.length) await this.backend.newChat();
        await this.backend.request('/api/run', { path: root.uri.fsPath, instruction, name: '', provider: this.provider,
          model: this.model, gguf_path: this.ggufPath,
          endpoint: this.provider === 'lmstudio' ? this.endpoint : undefined,
          extra_skill_root: this.provider === 'lmstudio' ? this.extraSkillRoot : undefined,
          extra_mcp_config: this.provider === 'lmstudio' && this.mcp ? this.extraMcpConfig : undefined,
          agent_mode: true, mcp: this.mcp, thinking_effort: this.effort,
          browser: this.browser,
          max_tokens: 4096, context_tokens: this.contextTokens, temperature: 0.2 });
      }
      await this.refresh();
      this.send({ type: 'sent', text });
    } else if (message.type === 'approve') {
      await this.backend.request('/api/approve', { id: String(message.id), allowed: message.allowed === true,
        always: message.always === true });
      await this.refresh();
    } else if (message.type === 'cancel') {
      await this.backend.request('/api/cancel', {});
    } else if (message.type === 'new') {
      await this.backend.newChat();
      this.lastState = undefined;
      await this.refresh();
    } else if (message.type === 'open') {
      const uri = vscode.Uri.file(String(message.path));
      await vscode.window.showTextDocument(uri);
    }
  }

  private html(): string {
    const nonce = Math.random().toString(36).slice(2);
    return `<!doctype html><html><head><meta charset="UTF-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';"><style>
      body{font:var(--vscode-font-size) var(--vscode-font-family);color:var(--vscode-foreground);padding:12px}
      button,select,textarea{font:inherit;color:inherit;background:var(--vscode-input-background);border:1px solid var(--vscode-input-border);border-radius:5px;padding:6px}
      button{cursor:pointer}button:hover{background:var(--vscode-list-hoverBackground)}button:disabled{opacity:.5}
      textarea{width:100%;box-sizing:border-box;min-height:82px;resize:vertical}select{max-width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere}
      .row{display:flex;gap:6px;margin:8px 0;flex-wrap:wrap}.message{padding:8px 0;border-bottom:1px solid var(--vscode-panel-border)}
      .user{opacity:.75}.answer{white-space:pre-wrap;overflow-wrap:anywhere}.small{font-size:11px;opacity:.75;overflow-wrap:anywhere}
      #approval{border:1px solid var(--vscode-focusBorder);padding:8px}#error{color:var(--vscode-errorForeground)}
      *{box-sizing:border-box}body{margin:0;padding:0;background:var(--vscode-sideBar-background)}
      .shell{display:flex;flex-direction:column;min-height:100vh}.hero{padding:16px;border-bottom:1px solid var(--vscode-panel-border);background:linear-gradient(135deg,var(--vscode-sideBar-background),var(--vscode-editor-background))}
      .brand{font-size:16px;font-weight:700}.sub{font-size:11px;opacity:.75;margin:3px 0 13px}.content{flex:1;padding:12px;overflow:auto}.composer{position:sticky;bottom:0;padding:12px;border-top:1px solid var(--vscode-panel-border);background:var(--vscode-sideBar-background)}
      .message{padding:11px;margin:8px 0;border:1px solid var(--vscode-panel-border);border-radius:10px;background:var(--vscode-editor-background)}.message strong{display:block;margin-bottom:5px}.answer{line-height:1.45}
      .primary{background:var(--vscode-button-background);color:var(--vscode-button-foreground);border-color:transparent;font-weight:600}.primary:hover{background:var(--vscode-button-hoverBackground)}
      .sendrow{display:flex;justify-content:space-between;align-items:center;gap:8px}.sendrow .small{font-size:10px}#model{flex:1;min-width:110px}#endpoint{width:100%}#ggufRow,#lmRow,#lmExtras{display:none}#contextTokens{width:100px}
    </style></head><body><div class="shell"><div class="hero"><div class="brand">✦ AI Agent Mini</div><div class="sub">Your local coding agent, inside this workspace</div><div class="row"><button id="start" class="primary">▶ Start</button><button id="new">New chat</button><button id="cancel">Stop</button></div>
      <div class="row"><select id="provider" aria-label="Model source"><option value="ollama">Ollama</option><option value="gguf">Local GGUF</option><option value="lmstudio">LM Studio API</option></select></div>
      <div class="row" id="modelRow"><select id="model"><option value="">Choose model</option></select><button id="refresh">Refresh</button><button id="load">Load</button></div>
      <div class="row" id="ggufRow"><button id="chooseGguf">Choose GGUF file</button><span id="ggufPath" class="small"></span></div>
      <div class="row" id="lmRow"><input id="endpoint" type="url" value="http://localhost:1234" aria-label="LM Studio API URL"><button id="connectLm">Connect</button></div>
      <div class="row"><label>Context <input id="contextTokens" type="number" min="16384" max="262144" step="1024" value="16384" aria-label="Context tokens"></label></div>
      <div id="lmExtras"><div class="row"><button id="lmSkills">Choose skill folder</button><button id="lmMcp">Choose MCP JSON</button></div><p id="lmSources" class="small">Optional local sources; MCP tools still require approval.</p></div>
      <div class="row"><label>Thinking <select id="effort"><option value="off">Off</option><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option></select></label><label><input type="checkbox" id="mcp"> MCP</label><label><input type="checkbox" id="browser"> Browser</label></div>
      <p id="workspace" class="small"></p><p id="state" class="small">Connecting…</p></div><div class="content"><div id="plan"></div><div id="approval" hidden></div><div id="thread"></div>
      <p id="error" role="alert"></p></div><div class="composer"><textarea id="prompt" aria-label="Request" placeholder="Ask to plan, edit, run, or fix code in this workspace"></textarea>
      <div class="sendrow"><span class="small">Enter sends · Shift+Enter adds a line</span><button id="send" class="primary">Send</button></div></div></div><script nonce="${nonce}">
      const api=acquireVsCodeApi(),$=id=>document.getElementById(id); let selected='',initialized=false;
      const post=(type,more={})=>api.postMessage({type,...more});
      const showProvider=()=>{const p=$('provider').value;$('modelRow').style.display=p==='gguf'?'none':'flex';$('ggufRow').style.display=p==='gguf'?'flex':'none';$('lmRow').style.display=p==='lmstudio'?'flex':'none';$('lmExtras').style.display=p==='lmstudio'?'block':'none';$('load').textContent=p==='lmstudio'?'Load in LM Studio':'Load';};
      $('provider').onchange=()=>{showProvider();selected='';post('provider',{provider:$('provider').value,endpoint:$('endpoint').value});};
      $('refresh').onclick=()=>post('models',{endpoint:$('endpoint').value});$('connectLm').onclick=$('refresh').onclick;
      $('load').onclick=()=>post('load',{model:$('model').value,endpoint:$('endpoint').value,contextTokens:$('contextTokens').value});$('chooseGguf').onclick=()=>post('gguf');$('start').onclick=()=>post('start');
      $('lmSkills').onclick=()=>post('lmSkills');$('lmMcp').onclick=()=>post('lmMcp');
      $('new').onclick=()=>post('new'); $('cancel').onclick=()=>post('cancel');
      $('send').onclick=()=>{const text=$('prompt').value.trim();if(text)post('prompt',{text,provider:$('provider').value,model:$('model').value,endpoint:$('endpoint').value,contextTokens:$('contextTokens').value,effort:$('effort').value,mcp:$('mcp').checked,browser:$('browser').checked});};
      $('prompt').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();$('send').click();}};
      window.addEventListener('message',event=>{const data=event.data;
        if(data.type==='error'){$('error').textContent=data.message;return;}
        if(data.type==='sent'){if($('prompt').value.trim()===data.text)$('prompt').value='';return;}
        if(data.type==='models'){if(data.provider!==$('provider').value)return;const previous=$('model').value||selected;$('model').replaceChildren(new Option('Choose model',''),...(data.models||[]).filter(m=>m.can_generate).map(m=>new Option(m.label||m.name,m.name)));if(Array.from($('model').options).some(o=>o.value===previous))$('model').value=previous;return;}
        if(data.type!=='status')return;const s=data.status;selected=data.model||selected;
        if(selected&&$('model').value!==selected&&!Array.from($('model').options).some(o=>o.value===selected))$('model').add(new Option(selected,selected));
        if(selected&&data.provider===$('provider').value)$('model').value=selected;
        if(!initialized){$('effort').value=s.thinking_effort||data.effort||'off';initialized=true;}
        $('ggufPath').textContent=data.ggufPath||'';$('workspace').textContent=data.workspace;
        $('lmSources').textContent='Skills: '+(data.extraSkillRoot||'none')+' · MCP: '+(data.extraMcpConfig||'none')+' (approval required)';
        $('state').textContent=s.status+((s.model_preload?.phase==='loading'||s.lmstudio_preload?.phase==='loading')?' · loading model':'')+(s.lmstudio_preload?.phase==='ready'?' · LM Studio ready':'');
        $('send').disabled=s.status==='running'||s.model_preload?.busy||s.lmstudio_preload?.busy;$('error').textContent=s.error||s.lmstudio_preload?.error||'';
        $('plan').replaceChildren();if(s.plan?.steps){const h=document.createElement('strong');h.textContent='Plan';$('plan').append(h);s.plan.steps.forEach((step,i)=>{const p=document.createElement('p');p.className='small';p.textContent=(i===s.plan.current?'→ ':'  ')+(i+1)+'. '+step;$('plan').append(p);});}
        $('approval').hidden=!s.pending_tool;$('approval').replaceChildren();if(s.pending_tool){const p=s.pending_tool;const title=document.createElement('strong');title.textContent='Approve '+p.name;const args=document.createElement('pre');args.textContent=JSON.stringify(p.arguments,null,2);$('approval').append(title,args);for(const [label,allowed,always] of [['Allow once',true,false],['Always allow chat',true,true],['Deny',false,false]]){const b=document.createElement('button');b.textContent=label;b.onclick=()=>post('approve',{id:p.id,allowed,always});$('approval').append(b);}}
        $('thread').replaceChildren();for(const msg of s.conversation||[]){const box=document.createElement('div');box.className='message';const label=document.createElement('strong');label.textContent=msg.role==='user'?'You':'Agent';const body=document.createElement('div');body.className='answer';body.textContent=msg.text||((msg.state==='working')?'Working…':'');box.append(label,body);$('thread').append(box);}for(const file of s.files||[]){const b=document.createElement('button');b.textContent='Open '+file;b.onclick=()=>post('open',{path:file});$('thread').append(b);}
      });showProvider();</script></body></html>`;
  }
}

export function activate(context: vscode.ExtensionContext): void {
  const view = new ChatView(context);
  context.subscriptions.push(vscode.window.registerWebviewViewProvider('aiAgentMini.chat', view));
  context.subscriptions.push(vscode.commands.registerCommand('aiAgentMini.open', () =>
    vscode.commands.executeCommand('workbench.view.extension.aiAgentMini')));
}

export function deactivate(): void {}
