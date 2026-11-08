package local.aiagentmini.intellij;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.intellij.ide.util.PropertiesComponent;
import com.intellij.openapi.Disposable;
import com.intellij.openapi.fileEditor.FileEditorManager;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.wm.ToolWindow;
import com.intellij.openapi.wm.ToolWindowFactory;
import com.intellij.ui.content.Content;
import com.intellij.ui.content.ContentFactory;
import org.jetbrains.annotations.NotNull;

import javax.swing.*;
import javax.swing.text.DefaultEditorKit;
import java.awt.*;
import java.awt.event.InputEvent;
import java.awt.event.KeyEvent;
import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Duration;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

public final class AgentToolWindowFactory implements ToolWindowFactory {
    @Override
    public void createToolWindowContent(@NotNull Project project, @NotNull ToolWindow toolWindow) {
        AgentPanel panel = new AgentPanel(project);
        Content content = ContentFactory.getInstance().createContent(panel, "Chat", false);
        content.setDisposer(panel);
        toolWindow.getContentManager().addContent(content);
    }

    private static final class AgentPanel extends JPanel implements Disposable {
        private final Project project;
        private final int port = configuredPort();
        private final ExecutorService executor = Executors.newSingleThreadExecutor(r -> {
            Thread thread = new Thread(r, "ai-agent-mini-intellij");
            thread.setDaemon(true);
            return thread;
        });
        private final AtomicBoolean polling = new AtomicBoolean();
        private final PropertiesComponent properties;
        private final JComboBox<String> provider = new JComboBox<>(new String[]{"Ollama", "Local GGUF", "LM Studio API"});
        private final JTextField endpoint = new JTextField("http://localhost:1234");
        private final JButton chooseGguf = new JButton("Choose GGUF file");
        private final JLabel ggufLabel = new JLabel("No GGUF selected");
        private final JButton refreshModels = new JButton("Refresh models");
        private final JButton load = new JButton("Load model");
        private final JPanel modelRow = new JPanel(new BorderLayout(4, 0));
        private final JPanel ggufRow = new JPanel(new BorderLayout(4, 0));
        private final JPanel endpointRow = new JPanel(new BorderLayout(4, 0));
        private final JPanel lmSourcesRow = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 0));
        private final JLabel lmSourcesLabel = new JLabel("No extra sources selected");
        private final JComboBox<String> model = new JComboBox<>();
        private final JSpinner contextTokens = new JSpinner(new SpinnerNumberModel(16384, 16384, 262144, 1024));
        private final JComboBox<String> effort = new JComboBox<>(new String[]{"off", "low", "medium", "high"});
        private final JComboBox<String> mode = new JComboBox<>(new String[]{"Agent · plan and edit", "Chat · answer only"});
        private final JCheckBox mcp = new JCheckBox("MCP tools");
        private final JCheckBox browser = new JCheckBox("Browser tools");
        private final JPanel thread = new JPanel();
        private final JScrollPane transcriptScroll = new JScrollPane(thread);
        private final JPanel options = new JPanel();
        private final JButton optionsToggle = new JButton("⚙ Options");
        private final Set<Integer> expandedActivity = new HashSet<>();
        private final JTextArea prompt = new JTextArea(4, 20);
        private final JLabel state = new JLabel("Connecting…");
        private final JPanel approval = new JPanel(new BorderLayout(4, 4));
        private final JButton send = new JButton("Send");
        private final Timer timer;
        private String token = "";
        private String session = "";
        private String selectedModel = "";
        private String ggufPath = "";
        private String extraSkillRoot = "";
        private String extraMcpConfig = "";
        private String pendingId = "";
        private JsonObject lastStatus;
        private boolean bootAttempted;
        private boolean started;

        AgentPanel(Project project) {
            super(new BorderLayout(6, 6));
            this.project = project;
            this.properties = PropertiesComponent.getInstance(project);
            selectedModel = properties.getValue("aiAgentMini.model", "");
            ggufPath = properties.getValue("aiAgentMini.gguf", "");
            provider.setSelectedIndex(Math.max(0, Math.min(2, properties.getInt("aiAgentMini.provider", 0))));
            endpoint.setText(properties.getValue("aiAgentMini.endpoint", "http://localhost:1234"));
            effort.setSelectedItem(properties.getValue("aiAgentMini.effort", "off"));
            mcp.setSelected(properties.getBoolean("aiAgentMini.mcp", false));
            browser.setSelected(properties.getBoolean("aiAgentMini.browser", false));
            model.addItem("Choose model");
            model.setEditable(false);

            setBorder(BorderFactory.createEmptyBorder(8, 8, 8, 8));
            JPanel top = new JPanel();
            top.setLayout(new BoxLayout(top, BoxLayout.Y_AXIS));
            JLabel title = new JLabel("✦  AI Agent Mini");
            title.setFont(title.getFont().deriveFont(Font.BOLD, 13f));
            title.setBorder(BorderFactory.createEmptyBorder(2, 3, 3, 3));
            top.add(title);
            JPanel actions = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 2));
            JButton start = new JButton("▶ Start");
            start.addActionListener(e -> execute(() -> {
                SwingUtilities.invokeLater(() -> state.setText("Starting local agent…"));
                ensureBackend();
                started = true;
                if (provider.getSelectedIndex() == 0) {
                    try { api("/api/models", null); }
                    catch (Exception error) { SwingUtilities.invokeLater(() -> state.setText(error.getMessage())); }
                }
                refreshNow();
                try { listModels(); }
                catch (Exception error) { SwingUtilities.invokeLater(() -> state.setText(error.getMessage())); }
            }));
            actions.add(start);
            JButton newChat = new JButton("New chat");
            newChat.addActionListener(e -> execute(() -> {
                ensureBackend();
                started = true;
                JsonObject created = api("/api/sessions", new JsonObject());
                session = string(created, "id");
                properties.setValue("aiAgentMini.session", session);
                lastStatus = null;
                SwingUtilities.invokeLater(expandedActivity::clear);
                refreshNow();
            }));
            actions.add(newChat);
            JButton cancel = new JButton("Stop");
            cancel.addActionListener(e -> execute(() -> {
                if (!started) {
                    SwingUtilities.invokeLater(() -> state.setText("No request is running."));
                    return;
                }
                api("/api/cancel", new JsonObject());
                refreshNow();
            }));
            actions.add(cancel);
            top.add(actions);
            top.add(provider);
            chooseGguf.addActionListener(e -> {
                JFileChooser chooser = new JFileChooser();
                chooser.setFileFilter(new javax.swing.filechooser.FileNameExtensionFilter("GGUF model", "gguf"));
                if (chooser.showOpenDialog(this) == JFileChooser.APPROVE_OPTION) {
                    ggufPath = chooser.getSelectedFile().getAbsolutePath();
                    selectedModel = ggufPath;
                    ggufLabel.setText(chooser.getSelectedFile().getName());
                    properties.setValue("aiAgentMini.gguf", ggufPath);
                }
            });
            ggufRow.add(chooseGguf, BorderLayout.WEST);
            ggufRow.add(ggufLabel, BorderLayout.CENTER);
            modelRow.add(model, BorderLayout.CENTER);
            refreshModels.addActionListener(e -> execute(() -> listModels()));
            JPanel modelActions = new JPanel(new FlowLayout(FlowLayout.RIGHT, 2, 0));
            modelActions.add(refreshModels);
            modelActions.add(load);
            modelRow.add(modelActions, BorderLayout.EAST);
            endpointRow.add(endpoint, BorderLayout.CENTER);
            JButton connectLm = new JButton("Connect");
            connectLm.addActionListener(e -> execute(() -> listModels()));
            endpointRow.add(connectLm, BorderLayout.EAST);
            top.add(modelRow);
            top.add(ggufRow);
            JButton chooseSkills = new JButton("Skill folder");
            chooseSkills.addActionListener(e -> {
                JFileChooser chooser = new JFileChooser();
                chooser.setFileSelectionMode(JFileChooser.DIRECTORIES_ONLY);
                if (chooser.showOpenDialog(this) == JFileChooser.APPROVE_OPTION) {
                    extraSkillRoot = chooser.getSelectedFile().getAbsolutePath();
                    updateSourceLabel();
                }
            });
            JButton chooseMcp = new JButton("MCP JSON");
            chooseMcp.addActionListener(e -> {
                JFileChooser chooser = new JFileChooser();
                chooser.setFileFilter(new javax.swing.filechooser.FileNameExtensionFilter("MCP JSON", "json"));
                if (chooser.showOpenDialog(this) == JFileChooser.APPROVE_OPTION) {
                    extraMcpConfig = chooser.getSelectedFile().getAbsolutePath();
                    updateSourceLabel();
                }
            });
            lmSourcesRow.add(chooseSkills);
            lmSourcesRow.add(chooseMcp);
            lmSourcesRow.add(lmSourcesLabel);
            provider.addActionListener(e -> {
                properties.setValue("aiAgentMini.provider", Integer.toString(provider.getSelectedIndex()));
                selectedModel = provider.getSelectedIndex() == 1 ? ggufPath : "";
                updateProviderUi();
                if (provider.getSelectedIndex() != 1) execute(() -> listModels());
            });
            updateProviderUi();
            JPanel settings = new JPanel(new GridLayout(0, 2, 4, 2));
            load.addActionListener(e -> {
                String chosenModel = String.valueOf(model.getSelectedItem());
                execute(() -> {
                if (chosenModel.isBlank() || chosenModel.equals("Choose model")) throw new IOException("Choose an installed model.");
                ensureBackend();
                started = true;
                selectedModel = chosenModel;
                properties.setValue("aiAgentMini.model", selectedModel);
                if (provider.getSelectedIndex() == 0) {
                    JsonObject request = new JsonObject();
                    request.addProperty("model", selectedModel);
                    api("/api/model/load", request);
                } else if (provider.getSelectedIndex() == 2) {
                    JsonObject request = new JsonObject();
                    request.addProperty("model", selectedModel);
                    request.addProperty("endpoint", endpoint.getText().trim());
                    request.addProperty("context_tokens", (Integer) contextTokens.getValue());
                    api("/api/lmstudio/load", request);
                }
                refreshNow();
                });
            });
            settings.add(new JLabel("Mode"));
            settings.add(mode);
            settings.add(new JLabel("Thinking"));
            settings.add(effort);
            settings.add(new JLabel("Context"));
            settings.add(contextTokens);
            settings.add(mcp);
            settings.add(browser);
            options.setLayout(new BoxLayout(options, BoxLayout.Y_AXIS));
            options.add(endpointRow);
            options.add(lmSourcesRow);
            options.add(settings);
            options.setVisible(false);
            optionsToggle.setAlignmentX(Component.LEFT_ALIGNMENT);
            optionsToggle.addActionListener(e -> {
                options.setVisible(!options.isVisible());
                optionsToggle.setText(options.isVisible() ? "▾ Options" : "⚙ Options");
                top.revalidate();
                top.repaint();
            });
            top.add(optionsToggle);
            top.add(options);
            state.setBorder(BorderFactory.createEmptyBorder(3, 3, 3, 3));
            top.add(state);
            add(top, BorderLayout.NORTH);

            thread.setLayout(new BoxLayout(thread, BoxLayout.Y_AXIS));
            thread.setBorder(BorderFactory.createEmptyBorder(8, 6, 8, 6));
            transcriptScroll.setBorder(BorderFactory.createEmptyBorder());
            transcriptScroll.getVerticalScrollBar().setUnitIncrement(16);
            JPanel center = new JPanel(new BorderLayout(4, 4));
            center.add(transcriptScroll, BorderLayout.CENTER);
            approval.setVisible(false);
            center.add(approval, BorderLayout.SOUTH);
            add(center, BorderLayout.CENTER);

            JPanel bottom = new JPanel(new BorderLayout(4, 4));
            bottom.setBorder(BorderFactory.createEmptyBorder(5, 2, 2, 2));
            prompt.setLineWrap(true);
            prompt.setWrapStyleWord(true);
            prompt.getInputMap().put(KeyStroke.getKeyStroke(KeyEvent.VK_ENTER, 0), "sendPrompt");
            prompt.getActionMap().put("sendPrompt", new AbstractAction() {
                @Override public void actionPerformed(java.awt.event.ActionEvent event) { send.doClick(); }
            });
            prompt.getInputMap().put(KeyStroke.getKeyStroke(KeyEvent.VK_ENTER, InputEvent.SHIFT_DOWN_MASK),
                    DefaultEditorKit.insertBreakAction);
            bottom.add(new JScrollPane(prompt), BorderLayout.CENTER);
            send.addActionListener(e -> {
                String text = prompt.getText().trim();
                if (!text.isEmpty()) {
                    String chosenEffort = String.valueOf(effort.getSelectedItem());
                    boolean useAgent = mode.getSelectedIndex() == 0;
                    boolean useMcp = mcp.isSelected();
                    boolean useBrowser = browser.isSelected();
                    String activeFile = "";
                    String base = project.getBasePath();
                    var selected = FileEditorManager.getInstance(project).getSelectedFiles();
                    if (base != null && selected.length > 0) {
                        Path root = Path.of(base).toAbsolutePath().normalize();
                        Path file = Path.of(selected[0].getPath()).toAbsolutePath().normalize();
                        if (file.startsWith(root)) activeFile = "Active editor: " + root.relativize(file) + "\n";
                    }
                    String instruction = activeFile + text;
                    execute(() -> {
                        submit(instruction, chosenEffort, useAgent, useMcp, useBrowser);
                        SwingUtilities.invokeLater(() -> {
                            if (prompt.getText().trim().equals(text)) prompt.setText("");
                        });
                    });
                }
            });
            bottom.add(send, BorderLayout.EAST);
            bottom.add(new JLabel("Enter sends  ·  Shift+Enter adds a line"), BorderLayout.SOUTH);
            add(bottom, BorderLayout.SOUTH);
            timer = new Timer(1500, e -> { if (started) refresh(); });
            timer.start();
            state.setText("Choose a model, then Start");
            if (provider.getSelectedIndex() != 1) execute(() -> listModels());
        }

        private void updateProviderUi() {
            int index = provider.getSelectedIndex();
            modelRow.setVisible(index != 1);
            ggufRow.setVisible(index == 1);
            endpointRow.setVisible(index == 2);
            lmSourcesRow.setVisible(index == 2);
            load.setVisible(index != 1);
            load.setText(index == 2 ? "Load in LM Studio" : "Load model");
            if (!ggufPath.isBlank()) ggufLabel.setText(Path.of(ggufPath).getFileName().toString());
            revalidate();
        }

        private void updateSourceLabel() {
            String skills = extraSkillRoot.isBlank() ? "none" : Path.of(extraSkillRoot).getFileName().toString();
            String mcpFile = extraMcpConfig.isBlank() ? "none" : Path.of(extraMcpConfig).getFileName().toString();
            lmSourcesLabel.setText("Skills: " + skills + " · MCP: " + mcpFile);
            lmSourcesLabel.setToolTipText("Selected local sources are used only by this agent; MCP calls require approval.");
        }

        private static String string(JsonObject object, String key) {
            JsonElement value = object == null ? null : object.get(key);
            return value == null || value.isJsonNull() ? "" : value.getAsString();
        }

        private void execute(Checked action) {
            executor.execute(() -> {
                try { action.run(); }
                catch (Exception error) { SwingUtilities.invokeLater(() -> state.setText(error.getMessage())); }
            });
        }

        private void refresh() {
            if (!polling.compareAndSet(false, true)) return;
            executor.execute(() -> {
                try { refreshNow(); }
                catch (Exception error) { SwingUtilities.invokeLater(() -> state.setText(error.getMessage())); }
                finally { polling.set(false); }
            });
        }

        private Path backendHome() throws Exception {
            String configured = System.getenv("AI_AGENT_MINI_HOME");
            if (configured != null && Files.isRegularFile(Path.of(configured, "ide_bootstrap.py")))
                return Path.of(configured).toAbsolutePath().normalize();
            Path pluginJar = Path.of(AgentToolWindowFactory.class.getProtectionDomain()
                    .getCodeSource().getLocation().toURI()).toAbsolutePath().normalize();
            if (!Files.isRegularFile(pluginJar)) {
                Path projectRoot = Path.of(project.getBasePath() == null ? "" : project.getBasePath());
                if (Files.isRegularFile(projectRoot.resolve("ide_bootstrap.py"))) return projectRoot;
                throw new IOException("Plugin backend resources are missing. Reinstall AI Agent Mini.");
            }
            Path home = pluginJar.getParent().getParent().resolve("backend").normalize();
            Files.createDirectories(home);
            try (ZipFile archive = new ZipFile(pluginJar.toFile())) {
                var entries = archive.entries();
                while (entries.hasMoreElements()) {
                    ZipEntry entry = entries.nextElement();
                    if (!entry.getName().startsWith("backend/") || entry.isDirectory()) continue;
                    Path target = home.resolve(entry.getName().substring("backend/".length())).normalize();
                    if (!target.startsWith(home)) throw new IOException("Invalid plugin backend path.");
                    try (var input = archive.getInputStream(entry)) {
                        Files.copy(input, target, StandardCopyOption.REPLACE_EXISTING);
                    }
                }
            }
            if (!Files.isRegularFile(home.resolve("ide_bootstrap.py")))
                throw new IOException("Plugin backend resources are missing. Reinstall AI Agent Mini.");
            return home;
        }

        private void ensureBackend() throws Exception {
            if (token.isEmpty()) {
                try { connect(); return; }
                catch (Exception error) {
                    if (error.getMessage().contains("Restart")) throw error;
                }
                if (!bootAttempted) {
                    bootAttempted = true;
                    try {
                        Path home = backendHome();
                        Path venv = home.resolve(System.getProperty("os.name").startsWith("Windows")
                                ? ".venv/Scripts/python.exe" : ".venv/bin/python");
                        String python = Files.isRegularFile(venv) ? venv.toString()
                                : System.getProperty("os.name").startsWith("Windows") ? "python" : "python3";
                        new ProcessBuilder(python, home.resolve("ide_bootstrap.py").toString(),
                                "--port", Integer.toString(port))
                                .directory(home.toFile()).redirectErrorStream(true)
                                .redirectOutput(ProcessBuilder.Redirect.appendTo(home.resolve("backend.log").toFile())).start();
                    } catch (Exception error) {
                        bootAttempted = false;
                        throw error;
                    }
                }
                for (int attempt = 0; attempt < 600; attempt++) {
                    Thread.sleep(500);
                    try { connect(); return; } catch (Exception ignored) { /* Still starting. */ }
                }
                bootAttempted = false;
                throw new IOException("Backend did not start. See backend.log in the installed plugin folder.");
            }
        }

        private void connect() throws Exception {
            String saved = properties.getValue("aiAgentMini.session", "");
            JsonObject connection;
            try { connection = http("/api/session" + (saved.isBlank() ? "" : "?id=" + saved), null); }
            catch (Exception error) { connection = http("/api/session", null); }
            token = string(connection, "token");
            if (connection.get("version").getAsInt() != 19)
                throw new IOException("Restart the AI Agent Mini backend to load the IDE API.");
            session = string(connection, "id");
            properties.setValue("aiAgentMini.session", session);
        }

        private JsonObject api(String path, JsonObject body) throws Exception {
            ensureBackend();
            try { return http(path, body); }
            catch (IOException error) {
                if (error.getMessage().contains("Connection refused") ||
                        error.getMessage().contains("Session expired") ||
                        error.getMessage().contains("Conversation not found")) {
                    token = "";
                    bootAttempted = false;
                }
                throw error;
            }
        }

        private JsonObject http(String path, JsonObject body) throws Exception {
            HttpURLConnection connection = (HttpURLConnection) URI.create("http://127.0.0.1:" + port + path).toURL().openConnection();
            connection.setConnectTimeout((int) Duration.ofSeconds(3).toMillis());
            connection.setReadTimeout((int) Duration.ofSeconds(15).toMillis());
            if (!session.isEmpty()) connection.setRequestProperty("X-Session-Id", session);
            if (body != null) {
                connection.setRequestMethod("POST");
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json");
                connection.setRequestProperty("X-Workflow-Token", token);
                connection.getOutputStream().write(body.toString().getBytes(StandardCharsets.UTF_8));
            }
            int code = connection.getResponseCode();
            byte[] bytes = (code < 400 ? connection.getInputStream() : connection.getErrorStream()).readAllBytes();
            JsonObject result = JsonParser.parseString(new String(bytes, StandardCharsets.UTF_8)).getAsJsonObject();
            connection.disconnect();
            if (code >= 400) throw new IOException(string(result, "error"));
            return result;
        }

        private void listModels() throws Exception {
            int source = provider.getSelectedIndex();
            if (source == 1) return;
            String address = source == 2 ? endpoint.getText().trim() :
                    System.getenv().getOrDefault("OLLAMA_HOST", "http://127.0.0.1:11434");
            if (!address.contains("://")) address = "http://" + address;
            URI uri = URI.create(address);
            if (!"localhost".equals(uri.getHost()) && !"127.0.0.1".equals(uri.getHost())
                    && !"::1".equals(uri.getHost())) throw new IOException("Model discovery is limited to local servers.");
            String base = uri.getScheme() + "://" + uri.getAuthority();
            if (source == 2) {
                properties.setValue("aiAgentMini.endpoint", endpoint.getText().trim());
                if (!uri.getPath().isBlank() && !"/".equals(uri.getPath()) && !"/v1".equals(uri.getPath()))
                    throw new IOException("Use the LM Studio server URL or its /v1 URL.");
            }
            JsonArray items;
            if (source == 2) {
                try { items = externalJson(base + "/api/v1/models").getAsJsonArray("models"); }
                catch (IOException error) {
                    if (!error.getMessage().contains("404")) throw error;
                    items = externalJson(base + "/v1/models").getAsJsonArray("data");
                }
            } else items = externalJson(base + "/api/tags").getAsJsonArray("models");
            JsonArray entries = items;
            SwingUtilities.invokeLater(() -> {
                if (provider.getSelectedIndex() != source) return;
                model.removeAllItems();
                model.addItem("Choose model");
                for (JsonElement item : entries) {
                    JsonObject entry = item.getAsJsonObject();
                    if (source == 2 && entry.has("type") && !"llm".equals(string(entry, "type"))) continue;
                    String name = source == 2 ? entry.has("key") ? string(entry, "key") : string(entry, "id")
                            : entry.has("name") ? string(entry, "name") : string(entry, "model");
                    if (!name.isBlank()) model.addItem(name);
                }
                boolean found = false;
                for (int i = 0; i < model.getItemCount(); i++)
                    if (selectedModel.equals(model.getItemAt(i))) found = true;
                if (found) model.setSelectedItem(selectedModel);
            });
        }

        private JsonObject externalJson(String url) throws Exception {
            HttpURLConnection connection = (HttpURLConnection) URI.create(url).toURL().openConnection();
            connection.setConnectTimeout(3000);
            connection.setReadTimeout(5000);
            try {
                int code = connection.getResponseCode();
                if (code >= 400) throw new IOException("Model server returned " + code);
                return JsonParser.parseString(new String(connection.getInputStream().readAllBytes(),
                        StandardCharsets.UTF_8)).getAsJsonObject();
            } finally { connection.disconnect(); }
        }

        private void submit(String instruction, String chosenEffort, boolean useAgent,
                            boolean useMcp, boolean useBrowser) throws Exception {
            ensureBackend();
            started = true;
            if (project.getBasePath() == null || project.getBasePath().isBlank())
                throw new IOException("Open a local IntelliJ project first.");
            Path root = Path.of(project.getBasePath()).toAbsolutePath().normalize();
            if (!Files.isDirectory(root)) throw new IOException("Open a local IntelliJ project first.");
            String source = provider.getSelectedIndex() == 1 ? "gguf" : provider.getSelectedIndex() == 2 ? "lmstudio" : "ollama";
            String chosenModel = source.equals("gguf") ? ggufPath : String.valueOf(model.getSelectedItem());
            if (chosenModel.isBlank() || chosenModel.equals("Choose model"))
                throw new IOException("Choose a model first.");
            selectedModel = chosenModel;
            if (instruction.length() > 4000) instruction = instruction.substring(0, 4000);
            properties.setValue("aiAgentMini.effort", chosenEffort);
            properties.setValue("aiAgentMini.mcp", useMcp);
            properties.setValue("aiAgentMini.browser", useBrowser);
            JsonObject request = new JsonObject();
            if (lastStatus != null && lastStatus.has("chat_ready") && lastStatus.get("chat_ready").getAsBoolean()
                    && source.equals(string(lastStatus, "provider")) && chosenModel.equals(string(lastStatus, "model"))
                    && lastStatus.has("conversation") && lastStatus.getAsJsonArray("conversation").size() > 0) {
                request.addProperty("question", instruction);
                request.addProperty("agent_mode", useAgent);
                request.addProperty("chat_only", !useAgent);
                request.addProperty("mcp", useAgent && useMcp);
                request.addProperty("browser", useBrowser);
                request.addProperty("thinking_effort", chosenEffort);
                api("/api/chat", request);
            } else {
                if (lastStatus != null && lastStatus.has("conversation")
                        && lastStatus.getAsJsonArray("conversation").size() > 0) {
                    session = string(api("/api/sessions", new JsonObject()), "id");
                    properties.setValue("aiAgentMini.session", session);
                }
                request.addProperty("path", root.toString());
                request.addProperty("instruction", instruction);
                request.addProperty("name", "");
                request.addProperty("provider", source);
                request.addProperty("model", chosenModel);
                request.addProperty("gguf_path", ggufPath);
                if (source.equals("lmstudio")) {
                    request.addProperty("endpoint", endpoint.getText().trim());
                    request.addProperty("extra_skill_root", extraSkillRoot);
                    if (useMcp) request.addProperty("extra_mcp_config", extraMcpConfig);
                }
                request.addProperty("agent_mode", useAgent);
                request.addProperty("chat_only", !useAgent);
                request.addProperty("mcp", useAgent && useMcp);
                request.addProperty("browser", useBrowser);
                request.addProperty("thinking_effort", chosenEffort);
                request.addProperty("max_tokens", 4096);
                request.addProperty("context_tokens", (Integer) contextTokens.getValue());
                request.addProperty("temperature", 0.2);
                api("/api/run", request);
            }
            refreshNow();
        }

        private void refreshNow() throws Exception {
            JsonObject status = api("/api/status", null);
            lastStatus = status;
            if (provider.getSelectedIndex() == 0 && (selectedModel.isBlank() || selectedModel.equals("Choose model"))
                    && status.has("model_preload") && status.get("model_preload").isJsonObject()) {
                String loaded = string(status.getAsJsonObject("model_preload"), "selected");
                if (!loaded.isBlank()) selectedModel = loaded;
            }
            SwingUtilities.invokeLater(() -> render(status));
        }

        private void render(JsonObject status) {
            String phase = status.has("model_preload") && status.get("model_preload").isJsonObject()
                    ? string(status.getAsJsonObject("model_preload"), "phase") : "";
            JsonObject lmPreload = status.has("lmstudio_preload") && status.get("lmstudio_preload").isJsonObject()
                    ? status.getAsJsonObject("lmstudio_preload") : new JsonObject();
            state.setText(project.getName() + "  ·  " + string(status, "status")
                    + (("loading".equals(phase) || "loading".equals(string(lmPreload, "phase"))) ? "  ·  Loading model" : "")
                    + (lmPreload.has("error") && !string(lmPreload, "error").isBlank() ? "  ·  " + string(lmPreload, "error") : "")
                    + (status.has("error") ? "  ·  " + string(status, "error") : ""));
            state.setToolTipText(project.getBasePath());
            boolean loading = status.has("model_preload") && status.get("model_preload").isJsonObject()
                    && status.getAsJsonObject("model_preload").has("busy")
                    && status.getAsJsonObject("model_preload").get("busy").getAsBoolean();
            loading |= lmPreload.has("busy") && lmPreload.get("busy").getAsBoolean();
            send.setEnabled(!"running".equals(string(status, "status")) && !loading);
            JScrollBar scroll = transcriptScroll.getVerticalScrollBar();
            boolean atBottom = scroll.getValue() + scroll.getVisibleAmount() >= scroll.getMaximum() - 24;
            int previous = scroll.getValue();
            thread.removeAll();
            int width = Math.max(170, Math.min(480, (int) (transcriptScroll.getViewport().getWidth() * 0.78)));
            if (status.has("conversation") && status.get("conversation").isJsonArray()) {
                int index = 0;
                for (JsonElement item : status.getAsJsonArray("conversation")) {
                    if (item.isJsonObject()) {
                        JsonObject message = item.getAsJsonObject();
                        JPanel row = new JPanel(new BorderLayout());
                        row.add(messageBubble(message, index, width),
                                "user".equals(string(message, "role")) ? BorderLayout.WEST : BorderLayout.EAST);
                        row.setAlignmentX(Component.LEFT_ALIGNMENT);
                        row.setMaximumSize(new Dimension(Integer.MAX_VALUE, row.getPreferredSize().height));
                        thread.add(row);
                        thread.add(Box.createVerticalStrut(7));
                    }
                    index++;
                }
            }
            thread.revalidate();
            thread.repaint();
            SwingUtilities.invokeLater(() -> scroll.setValue(atBottom ? scroll.getMaximum() : previous));
            if (status.has("pending_tool") && status.get("pending_tool").isJsonObject()) {
                JsonObject pending = status.getAsJsonObject("pending_tool");
                String id = string(pending, "id");
                if (!id.equals(pendingId)) {
                    pendingId = id;
                    approval.removeAll();
                    JTextArea arguments = new JTextArea(string(pending, "name") + "\n" + pending.get("arguments"));
                    arguments.setEditable(false);
                    arguments.setRows(5);
                    approval.add(new JScrollPane(arguments), BorderLayout.CENTER);
                    JPanel actions = new JPanel(new FlowLayout(FlowLayout.LEFT));
                    for (String label : new String[]{"Allow once", "Always allow chat", "Deny"}) {
                        JButton button = new JButton(label);
                        button.addActionListener(e -> execute(() -> {
                            JsonObject decision = new JsonObject();
                            decision.addProperty("id", id);
                            decision.addProperty("allowed", !label.equals("Deny"));
                            decision.addProperty("always", label.startsWith("Always"));
                            api("/api/approve", decision);
                            refreshNow();
                        }));
                        actions.add(button);
                    }
                    approval.add(actions, BorderLayout.SOUTH);
                }
                approval.setVisible(true);
            } else {
                pendingId = "";
                approval.setVisible(false);
            }
            revalidate();
        }

        private JPanel messageBubble(JsonObject message, int index, int width) {
            boolean user = "user".equals(string(message, "role"));
            Color background = UIManager.getColor(user ? "TextField.background" : "EditorPane.background");
            if (background == null) background = getBackground();
            Color outline = UIManager.getColor("Separator.foreground");
            if (outline == null) outline = Color.GRAY;
            JPanel bubble = new JPanel(new BorderLayout(0, 5));
            bubble.setBackground(background);
            bubble.setBorder(BorderFactory.createCompoundBorder(
                    BorderFactory.createLineBorder(outline),
                    BorderFactory.createEmptyBorder(8, 10, 8, 10)));
            JLabel heading = new JLabel(user ? "You" : "Agent");
            heading.setFont(heading.getFont().deriveFont(Font.BOLD));
            bubble.add(heading, BorderLayout.NORTH);
            bubble.add(wrappedText(string(message, "text"), width - 20, background), BorderLayout.CENTER);
            if (!user) {
                JsonArray steps = message.has("steps") && message.get("steps").isJsonArray()
                        ? message.getAsJsonArray("steps") : new JsonArray();
                JsonArray logs = message.has("logs") && message.get("logs").isJsonArray()
                        ? message.getAsJsonArray("logs") : new JsonArray();
                if (steps.size() > 0 || logs.size() > 0) {
                    JPanel activity = new JPanel(new BorderLayout());
                    activity.setBackground(background);
                    JButton toggle = new JButton((expandedActivity.contains(index) ? "▼ " : "▶ ")
                            + "Activity · " + steps.size() + " steps, " + logs.size() + " log lines");
                    toggle.setHorizontalAlignment(SwingConstants.LEFT);
                    toggle.setBorder(BorderFactory.createEmptyBorder(3, 0, 3, 0));
                    toggle.setContentAreaFilled(false);
                    StringBuilder details = new StringBuilder();
                    for (JsonElement item : steps) {
                        if (!item.isJsonObject()) continue;
                        JsonObject step = item.getAsJsonObject();
                        details.append(string(step, "label")).append('\n');
                        String detail = string(step, "detail");
                        if (!detail.isBlank()) details.append(detail).append('\n');
                        details.append('\n');
                    }
                    if (logs.size() > 0) {
                        details.append("Logs\n");
                        for (JsonElement line : logs) details.append(line.getAsString()).append('\n');
                    }
                    Color outputBackground = UIManager.getColor("TextArea.background");
                    JTextArea output = wrappedText(details.toString().trim(), width - 38,
                            outputBackground == null ? background : outputBackground);
                    JScrollPane disclosure = new JScrollPane(output);
                    disclosure.setPreferredSize(new Dimension(width - 20, 125));
                    disclosure.setVisible(expandedActivity.contains(index));
                    toggle.addActionListener(event -> {
                        if (!expandedActivity.add(index)) expandedActivity.remove(index);
                        disclosure.setVisible(expandedActivity.contains(index));
                        toggle.setText((expandedActivity.contains(index) ? "▼ " : "▶ ")
                                + "Activity · " + steps.size() + " steps, " + logs.size() + " log lines");
                        Container row = bubble.getParent();
                        if (row instanceof JPanel) {
                            row.setMaximumSize(new Dimension(Integer.MAX_VALUE, row.getPreferredSize().height));
                        }
                        thread.revalidate();
                        thread.repaint();
                    });
                    activity.add(toggle, BorderLayout.NORTH);
                    activity.add(disclosure, BorderLayout.CENTER);
                    bubble.add(activity, BorderLayout.SOUTH);
                }
            }
            return bubble;
        }

        private JTextArea wrappedText(String text, int width, Color background) {
            JTextArea area = new JTextArea(text.isBlank() ? " " : text);
            area.setEditable(false);
            area.setFocusable(false);
            area.setLineWrap(true);
            area.setWrapStyleWord(true);
            area.setBackground(background);
            Color foreground = UIManager.getColor("Label.foreground");
            if (foreground != null) area.setForeground(foreground);
            area.setBorder(BorderFactory.createEmptyBorder());
            area.setSize(Math.max(120, width), Short.MAX_VALUE);
            area.setPreferredSize(new Dimension(Math.max(120, width), Math.max(18, area.getPreferredSize().height)));
            return area;
        }

        private static int configuredPort() {
            String value = System.getProperty("aiAgentMini.port",
                    System.getenv().getOrDefault("AI_AGENT_MINI_PORT", "8765"));
            try {
                int parsed = Integer.parseInt(value);
                if (parsed >= 1 && parsed <= 65535) return parsed;
            } catch (NumberFormatException ignored) { /* Use the local default. */ }
            return 8765;
        }

        @Override public void dispose() {
            timer.stop();
            executor.shutdownNow();
        }
    }

    @FunctionalInterface private interface Checked { void run() throws Exception; }
}
