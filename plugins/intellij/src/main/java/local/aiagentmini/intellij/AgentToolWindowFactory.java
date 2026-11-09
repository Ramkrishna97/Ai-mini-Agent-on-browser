package local.aiagentmini.intellij;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.intellij.icons.AllIcons;
import com.intellij.ide.util.PropertiesComponent;
import com.intellij.ide.plugins.PluginManagerCore;
import com.intellij.openapi.Disposable;
import com.intellij.openapi.actionSystem.AnActionEvent;
import com.intellij.openapi.actionSystem.CustomShortcutSet;
import com.intellij.openapi.actionSystem.Presentation;
import com.intellij.openapi.actionSystem.ex.CustomComponentAction;
import com.intellij.openapi.extensions.PluginId;
import com.intellij.openapi.project.DumbAwareAction;
import com.intellij.openapi.fileEditor.FileEditorManager;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.wm.ToolWindow;
import com.intellij.openapi.wm.ToolWindowFactory;
import com.intellij.ui.content.Content;
import com.intellij.ui.content.ContentFactory;
import com.intellij.util.ui.JBUI;
import org.jetbrains.annotations.NotNull;

import javax.swing.*;
import javax.swing.text.DefaultEditorKit;
import javax.swing.event.DocumentEvent;
import javax.swing.event.DocumentListener;
import java.awt.*;
import java.awt.event.InputEvent;
import java.awt.event.KeyEvent;
import java.awt.event.ComponentAdapter;
import java.awt.event.ComponentEvent;
import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.HashSet;
import java.util.Set;
import java.util.List;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

public final class AgentToolWindowFactory implements ToolWindowFactory {
    @Override
    public void createToolWindowContent(@NotNull Project project, @NotNull ToolWindow toolWindow) {
        AgentPanel panel = new AgentPanel(project);
        Content content = ContentFactory.getInstance().createContent(panel, "Chat", false);
        content.setDisposer(panel);
        toolWindow.getContentManager().addContent(content);
        content.setDisplayName(panel.mode.isSelected() ? "Agent" : "Chat");
        panel.mode.addActionListener(e -> content.setDisplayName(panel.mode.isSelected() ? "Agent" : "Chat"));
        toolWindow.setTitleActions(List.of(new ModeSwitchAction(panel),
                new DumbAwareAction("New chat", "Start a new conversation (clears queued messages)", AllIcons.General.Add) {
                    @Override public void actionPerformed(@NotNull AnActionEvent event) { panel.newChat(); }
                }));
    }

    private static final class ModeSwitchAction extends DumbAwareAction implements CustomComponentAction {
        private final AgentPanel panel;
        ModeSwitchAction(AgentPanel panel) { super("Agent / Chat", "Toggle Agent or Chat mode", null); this.panel = panel; }
        @Override public void actionPerformed(@NotNull AnActionEvent event) { panel.mode.doClick(); }
        @Override public @NotNull JComponent createCustomComponent(@NotNull Presentation presentation, @NotNull String place) {
            return panel.mode;
        }
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
        private final JButton refreshModels = iconButton(AllIcons.Actions.Refresh, "Refresh models");
        private final JButton load = iconButton(AllIcons.Actions.Download, "Load selected model");
        private final JPanel modelRow = new JPanel(new CardLayout());
        private final JButton ggufQuick = new JButton("Choose GGUF…");
        private final JPanel ggufRow = new JPanel(new BorderLayout(4, 0));
        private final JPanel endpointRow = new JPanel(new BorderLayout(4, 0));
        private final JPanel lmSourcesRow = new JPanel(new BorderLayout(0, 4));
        private final JLabel lmSourcesLabel = new JLabel("No extra sources selected");
        private final JLabel advancedHint = new JLabel("No additional settings for Ollama.");
        private final JComboBox<String> model = new JComboBox<>();
        private final JSpinner contextTokens = new JSpinner(new SpinnerNumberModel(16384, 16384, 262144, 1024));
        private final JComboBox<String> effort = new JComboBox<>(new String[]{"off", "low", "medium", "high"});
        private final JToggleButton mode = new JToggleButton("Agent", true);
        private final JCheckBox mcp = new JCheckBox("MCP");
        private final JCheckBox browser = new JCheckBox("Web");
        private final JPanel thread = new JPanel();
        private final JScrollPane transcriptScroll = new JScrollPane(thread);
        private final JPanel options = new JPanel();
        private final JButton optionsToggle = iconButton(AllIcons.General.Settings, "Advanced settings");
        private final JPopupMenu optionsPopup = new JPopupMenu();
        private final JPanel planPanel = new JPanel(new BorderLayout());
        private final JToggleButton planToggle = new JToggleButton("Plan");
        private final JTextArea planText = new JTextArea();
        private final JScrollPane planScroll = new JScrollPane(planText);
        private final Set<Integer> expandedActivity = new HashSet<>();
        private final JTextArea prompt = new JTextArea(2, 20);
        private final JScrollPane promptScroll = new JScrollPane(prompt);
        private final JLabel state = new JLabel("Connecting…");
        private final JPanel approval = new JPanel(new BorderLayout(4, 4));
        private final JButton send = iconButton(AllIcons.Actions.Upload, "Send (Enter)");
        private final JButton steer = iconButton(AllIcons.Actions.Forward, "Queue follow-up (Alt+Enter); send after current response finishes");
        private final JButton start = iconButton(AllIcons.Actions.Execute, "Start local agent");
        private final JButton cancel = iconButton(AllIcons.Actions.Suspend, "Stop current request and clear queued messages");
        private final JButton queueState = new JButton();
        private final JPanel toolbar = new JPanel(new BorderLayout(0, 3));
        private final JComboBox<String> sortModels = new JComboBox<>(new String[]{"Name A–Z", "Size: smallest first"});
        private final Map<String, JsonObject> catalog = new HashMap<>();
        private final Map<String, Boolean> thinkingCache = new HashMap<>();
        private final DeferredMessages<PromptRequest> queued = new DeferredMessages<>();
        private boolean rebuildingModels;
        private boolean submissionPending;
        private boolean requestRunning;
        private boolean loadingModel;
        private boolean thinkingAvailable;
        private boolean queuePaused;
        private boolean resumeQueue;
        private int conversationEpoch;
        private boolean wideToolbar;
        private volatile long lastCapabilityCheck;
        private volatile ModelSelection selection = new ModelSelection(0, "", "");
        private final Timer timer;
        private String token = "";
        private String session = "";
        private String selectedModel = "";
        private String ggufPath = "";
        private String extraSkillRoot = "";
        private String extraMcpConfig = "";
        private String pendingId = "";
        private volatile JsonObject lastStatus;
        private boolean bootAttempted;
        private Process backendProcess;
        private Path backendLog;
        private volatile boolean started;

        private record ModelSelection(int provider, String model, String endpoint) {}
        private record PromptRequest(String text, String instruction, ModelSelection selection, int context,
                                     String effort, boolean agent, boolean mcp, boolean browser,
                                     String skills, String mcpConfig) {}

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
            mode.setSelected(properties.getBoolean("aiAgentMini.agentMode", true));
            mode.setText(mode.isSelected() ? "Agent" : "Chat");
            compactControl(mode, 68, "Click to switch Agent / Chat mode");
            mode.addActionListener(e -> {
                mode.setText(mode.isSelected() ? "Agent" : "Chat");
                properties.setValue("aiAgentMini.agentMode", mode.isSelected());
            });
            model.addItem("Choose model");
            model.setEditable(false);

            setBorder(JBUI.Borders.empty(4));
            JPanel top = new JPanel(new BorderLayout());
            start.addActionListener(e -> execute(() -> {
                SwingUtilities.invokeLater(() -> state.setText("Starting local agent…"));
                ensureBackend();
                started = true;
                if (provider.getSelectedIndex() == 0) {
                    try { api("/api/models", null); }
                    catch (Exception error) { SwingUtilities.invokeLater(() -> showError(error)); }
                }
                refreshNow();
                try { listModels(); }
                catch (Exception error) { SwingUtilities.invokeLater(() -> showError(error)); }
            }));
            cancel.setEnabled(false);
            cancel.addActionListener(e -> {
                conversationEpoch++;
                submissionPending = false;
                queued.clear();
                queuePaused = false;
                resumeQueue = false;
                updateQueueLabel();
                execute(() -> {
                if (!started) {
                    SwingUtilities.invokeLater(() -> state.setText("No request is running."));
                    return;
                }
                api("/api/cancel", new JsonObject());
                refreshNow();
                });
            });
            compactControl(provider, 88, "Model source: Ollama, local GGUF, or LM Studio API");
            chooseGguf.addActionListener(e -> {
                JFileChooser chooser = new JFileChooser();
                chooser.setFileFilter(new javax.swing.filechooser.FileNameExtensionFilter("GGUF model", "gguf"));
                if (chooser.showOpenDialog(this) == JFileChooser.APPROVE_OPTION) {
                    ggufPath = chooser.getSelectedFile().getAbsolutePath();
                    selectedModel = ggufPath;
                    ggufLabel.setText(chooser.getSelectedFile().getName());
                    properties.setValue("aiAgentMini.gguf", ggufPath);
                    updateProviderUi();
                }
            });
            ggufRow.add(chooseGguf, BorderLayout.WEST);
            ggufRow.add(ggufLabel, BorderLayout.CENTER);
            compactControl(model, 180, "Choose a model, then click the load icon");
            model.setMinimumSize(JBUI.size(105, 26));
            model.addActionListener(e -> {
                if (rebuildingModels) return;
                selectedModel = String.valueOf(model.getSelectedItem());
                updateSelection();
            });
            ggufQuick.addActionListener(e -> chooseGguf.doClick());
            compactControl(ggufQuick, 160, "Choose a local GGUF file");
            modelRow.add(model, "model");
            modelRow.add(ggufQuick, "gguf");
            modelRow.setMinimumSize(JBUI.size(105, 26));
            modelRow.setPreferredSize(JBUI.size(180, 26));
            refreshModels.addActionListener(e -> execute(() -> listModels()));
            // Stable preferred widths; a long status/error must never squeeze every control to "…".
            state.setMinimumSize(JBUI.size(0, 26));
            state.setPreferredSize(JBUI.size(0, 26));
            top.add(toolbar, BorderLayout.CENTER);
            addComponentListener(new ComponentAdapter() {
                @Override public void componentResized(ComponentEvent event) {
                    if (toolbarFitsOneRow() != wideToolbar) layoutToolbar();
                }
            });
            endpointRow.add(endpoint, BorderLayout.CENTER);
            endpoint.setToolTipText("LM Studio local API URL");
            endpointRow.add(new JLabel("API "), BorderLayout.WEST);
            JButton connectLm = new JButton("Connect");
            connectLm.addActionListener(e -> execute(() -> listModels()));
            endpointRow.add(connectLm, BorderLayout.EAST);
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
            JPanel sourceActions = new JPanel(new FlowLayout(FlowLayout.LEFT, 4, 0));
            sourceActions.add(chooseSkills);
            sourceActions.add(chooseMcp);
            lmSourcesRow.add(sourceActions, BorderLayout.NORTH);
            lmSourcesRow.add(lmSourcesLabel, BorderLayout.SOUTH);
            provider.addActionListener(e -> {
                properties.setValue("aiAgentMini.provider", Integer.toString(provider.getSelectedIndex()));
                selectedModel = provider.getSelectedIndex() == 1 ? ggufPath : "";
                catalog.clear();
                rebuildingModels = true;
                model.removeAllItems();
                model.addItem("Choose model");
                rebuildingModels = false;
                updateProviderUi();
                updateSelection();
                if (provider.getSelectedIndex() != 1) execute(() -> listModels());
            });
            updateProviderUi();
            load.addActionListener(e -> {
                updateSelection();
                ModelSelection chosen = selection;
                int context = (Integer) contextTokens.getValue();
                String chosenModel = chosen.model();
                if (chosenModel.isBlank() || chosenModel.equals("Choose model")) {
                    showError(new IOException("Choose an installed model."));
                    return;
                }
                loadingModel = true;
                send.setEnabled(false);
                execute(() -> {
                try {
                ensureBackend();
                started = true;
                selectedModel = chosenModel;
                properties.setValue("aiAgentMini.model", selectedModel);
                if (chosen.provider() == 0) {
                    JsonObject request = new JsonObject();
                    request.addProperty("model", selectedModel);
                    api("/api/model/load", request);
                } else if (chosen.provider() == 2) {
                    JsonObject request = new JsonObject();
                    request.addProperty("model", selectedModel);
                    request.addProperty("endpoint", chosen.endpoint());
                    request.addProperty("context_tokens", context);
                    api("/api/lmstudio/load", request);
                }
                refreshNow();
                } catch (Exception error) {
                    SwingUtilities.invokeLater(() -> { loadingModel = false; showError(error); });
                }
                });
            });
            compactControl(effort, 82, "Thinking effort (only for a loaded model advertising thinking support)");
            effort.setMinimumSize(JBUI.size(76, 26));
            effort.setVisible(false);
            compactControl(contextTokens, 90, "Context window (tokens)");
            contextTokens.setMinimumSize(JBUI.size(84, 26));
            effort.setRenderer(new DefaultListCellRenderer() {
                @Override public Component getListCellRendererComponent(JList<?> list, Object value, int index,
                                                                        boolean selected, boolean focused) {
                    String label = "medium".equals(value) ? "Med" : String.valueOf(value);
                    if (!label.isEmpty()) label = Character.toUpperCase(label.charAt(0)) + label.substring(1);
                    return super.getListCellRendererComponent(list, label, index, selected, focused);
                }
            });
            mcp.setToolTipText("Enable MCP tools; tool calls require approval");
            mcp.getAccessibleContext().setAccessibleName("MCP tools");
            browser.setToolTipText("Enable browser tools");
            browser.getAccessibleContext().setAccessibleName("Browser tools");
            mcp.setMargin(JBUI.emptyInsets());
            browser.setMargin(JBUI.emptyInsets());
            state.setFont(state.getFont().deriveFont(Math.max(11f, state.getFont().getSize2D() - 1)));
            options.setLayout(new BoxLayout(options, BoxLayout.Y_AXIS));
            options.setBorder(JBUI.Borders.empty(8));
            options.add(advancedHint);
            options.add(ggufRow);
            options.add(endpointRow);
            options.add(lmSourcesRow);
            JPanel sorting = new JPanel(new BorderLayout(4, 0));
            sorting.add(new JLabel("Sort models"), BorderLayout.WEST);
            sorting.add(sortModels, BorderLayout.CENTER);
            sortModels.addActionListener(e -> populateModels());
            options.add(sorting);
            optionsPopup.add(options);
            optionsToggle.addActionListener(e -> {
                if (optionsPopup.isVisible()) optionsPopup.setVisible(false);
                else {
                    optionsPopup.setPopupSize(JBUI.scale(350), options.getPreferredSize().height + JBUI.scale(6));
                    optionsPopup.show(optionsToggle, optionsToggle.getWidth() - optionsPopup.getPreferredSize().width,
                            optionsToggle.getHeight());
                }
            });
            layoutToolbar();
            add(top, BorderLayout.NORTH);

            thread.setLayout(new BoxLayout(thread, BoxLayout.Y_AXIS));
            thread.setBorder(BorderFactory.createEmptyBorder(8, 6, 8, 6));
            transcriptScroll.setBorder(BorderFactory.createEmptyBorder());
            transcriptScroll.getVerticalScrollBar().setUnitIncrement(16);
            JPanel center = new JPanel(new BorderLayout(4, 4));
            planToggle.setHorizontalAlignment(SwingConstants.LEFT);
            planToggle.setContentAreaFilled(false);
            planToggle.setBorder(JBUI.Borders.empty(3, 5));
            planToggle.setToolTipText("Expand or collapse the current plan");
            planToggle.getAccessibleContext().setAccessibleName("Current plan");
            planText.setEditable(false);
            planText.setLineWrap(true);
            planText.setWrapStyleWord(true);
            planText.setMargin(JBUI.insets(4));
            planScroll.setPreferredSize(JBUI.size(0, 90));
            planScroll.setVisible(false);
            planToggle.addActionListener(e -> {
                planScroll.setVisible(planToggle.isSelected());
                planPanel.revalidate();
            });
            planPanel.add(planToggle, BorderLayout.NORTH);
            planPanel.add(planScroll, BorderLayout.CENTER);
            planPanel.setVisible(false);
            center.add(planPanel, BorderLayout.NORTH);
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
            new DumbAwareAction("Queue follow-up") {
                @Override public void actionPerformed(@NotNull AnActionEvent event) { steer.doClick(); }
            }.registerCustomShortcutSet(new CustomShortcutSet(KeyStroke.getKeyStroke(KeyEvent.VK_ENTER,
                    InputEvent.ALT_DOWN_MASK)), prompt, this);
            prompt.getDocument().addDocumentListener(new DocumentListener() {
                private void changed() { SwingUtilities.invokeLater(() -> resizeComposer()); }
                @Override public void insertUpdate(DocumentEvent event) { changed(); }
                @Override public void removeUpdate(DocumentEvent event) { changed(); }
                @Override public void changedUpdate(DocumentEvent event) { changed(); }
            });
            promptScroll.addComponentListener(new ComponentAdapter() {
                @Override public void componentResized(ComponentEvent event) { resizeComposer(); }
            });
            bottom.add(promptScroll, BorderLayout.CENTER);
            send.addActionListener(e -> capturePrompt(false));
            steer.addActionListener(e -> capturePrompt(true));
            JPanel sendActions = new JPanel(new GridLayout(1, 2, 2, 0));
            sendActions.add(send);
            sendActions.add(steer);
            bottom.add(sendActions, BorderLayout.EAST);
            JLabel keys = new JLabel("Enter: send · Shift+Enter: line · Alt+Enter: queue");
            keys.setFont(keys.getFont().deriveFont(10f));
            bottom.add(keys, BorderLayout.SOUTH);
            queueState.setVisible(false);
            queueState.setHorizontalAlignment(SwingConstants.LEFT);
            queueState.setContentAreaFilled(false);
            queueState.setBorder(JBUI.Borders.empty(2));
            queueState.addActionListener(e -> { queuePaused = false; resumeQueue = true; drainQueue(); });
            bottom.add(queueState, BorderLayout.NORTH);
            add(bottom, BorderLayout.SOUTH);
            resizeComposer();
            timer = new Timer(1500, e -> { if (started) refresh(); });
            timer.start();
            state.setText("Ready");
            state.setToolTipText("Choose a model, then Start. Workspace: " + project.getBasePath());
            if (provider.getSelectedIndex() != 1) execute(() -> listModels());
            updateSelection();
        }

        private boolean toolbarFitsOneRow() {
            int needed = JBUI.scale(12);
            for (JComponent control : new JComponent[]{start, cancel, provider, modelRow, refreshModels,
                    load, optionsToggle, effort, contextTokens, mcp, browser})
                if (control.isVisible()) needed += control.getPreferredSize().width + JBUI.scale(3);
            return getWidth() >= needed;
        }

        private void layoutToolbar() {
            wideToolbar = toolbarFitsOneRow();
            toolbar.removeAll();
            JPanel first = new JPanel(new GridBagLayout());
            JPanel second = wideToolbar ? first : new JPanel(new GridBagLayout());
            JComponent[] primary = {start, cancel, provider, modelRow, refreshModels, load, optionsToggle};
            JComponent[] secondary = {effort, contextTokens, mcp, browser, state};
            int column = 0;
            for (JComponent control : primary) toolbarCell(first, control, column++, 0);
            if (!wideToolbar) {
                toolbarCell(first, new JLabel(), column, 1);
                column = 0;
            }
            for (JComponent control : secondary)
                toolbarCell(second, control, column++, control == state ? 1 : 0);
            toolbar.add(first, BorderLayout.NORTH);
            if (!wideToolbar) toolbar.add(second, BorderLayout.SOUTH);
            toolbar.revalidate();
            toolbar.repaint();
        }

        private void resizeComposer() {
            int width = Math.max(JBUI.scale(100), promptScroll.getViewport().getWidth());
            int lineHeight = prompt.getFontMetrics(prompt.getFont()).getHeight();
            // Measure visual wrapping as well as explicit newlines, then cap at four lines.
            prompt.setSize(width, Short.MAX_VALUE);
            int height = prompt.getUI().getPreferredSize(prompt).height;
            int rows = Math.max(2, Math.min(4, (height + lineHeight - 1) / lineHeight));
            if (prompt.getRows() != rows) prompt.setRows(rows);
            promptScroll.setPreferredSize(new Dimension(0, rows * lineHeight + JBUI.scale(8)));
            promptScroll.revalidate();
            revalidate();
        }

        private void newChat() {
            conversationEpoch++;
            queued.clear();
            queuePaused = false;
            resumeQueue = false;
            updateQueueLabel();
            submissionPending = true;
            execute(() -> {
                try {
                    ensureBackend();
                    started = true;
                    session = string(api("/api/sessions", new JsonObject()), "id");
                    properties.setValue("aiAgentMini.session", session);
                    lastStatus = null;
                    SwingUtilities.invokeLater(expandedActivity::clear);
                    refreshNow();
                } finally { SwingUtilities.invokeLater(() -> submissionPending = false); }
            });
        }

        private void capturePrompt(boolean queue) {
            String text = prompt.getText().trim();
            if (text.isEmpty()) return;
            updateSelection();
            ModelSelection selected = selection;
            if (selected.model().isBlank() || selected.model().equals("Choose model")) {
                showError(new IOException("Choose a model first."));
                return;
            }
            String activeFile = "";
            var editors = FileEditorManager.getInstance(project).getSelectedFiles();
            if (project.getBasePath() != null && editors.length > 0) {
                Path root = Path.of(project.getBasePath()).toAbsolutePath().normalize();
                Path file = Path.of(editors[0].getPath()).toAbsolutePath().normalize();
                if (file.startsWith(root)) activeFile = "Active editor: " + root.relativize(file) + "\n";
            }
            PromptRequest request = new PromptRequest(text, activeFile + text, selected,
                    (Integer) contextTokens.getValue(), thinkingAvailable ? String.valueOf(effort.getSelectedItem()) : "off",
                    mode.isSelected(), mcp.isSelected(), browser.isSelected(), extraSkillRoot, extraMcpConfig);
            if (queue) {
                queued.add(request);
                prompt.setText("");
                updateQueueLabel();
                if (!started) execute(() -> { ensureBackend(); started = true; refreshNow(); });
                else drainQueue();
            } else if (!requestRunning && !submissionPending && !loadingModel && queued.size() == 0) dispatch(request, false);
        }

        private void dispatch(PromptRequest request, boolean fromQueue) {
            int epoch = conversationEpoch;
            submissionPending = true;
            send.setEnabled(false);
            execute(() -> {
                try {
                    submit(request);
                    SwingUtilities.invokeLater(() -> {
                        if (epoch != conversationEpoch) return;
                        if (!fromQueue && prompt.getText().trim().equals(request.text())) prompt.setText("");
                    });
                } catch (Exception error) {
                    SwingUtilities.invokeLater(() -> {
                        if (epoch != conversationEpoch) return;
                        if (fromQueue) { queued.restore(request); queuePaused = true; }
                        updateQueueLabel();
                        showError(error);
                    });
                } finally { SwingUtilities.invokeLater(() -> { if (epoch == conversationEpoch) submissionPending = false; }); }
            });
        }

        private void drainQueue() {
            if (!started || lastStatus == null || queuePaused) return;
            PromptRequest request = queued.poll(resumeQueue ? "idle" : string(lastStatus, "status"),
                    submissionPending || requestRunning || loadingModel);
            if (request != null) { resumeQueue = false; dispatch(request, true); }
            updateQueueLabel();
        }

        private void updateQueueLabel() {
            queueState.setVisible(queued.size() > 0);
            boolean failed = lastStatus != null && List.of("failed", "cancelled").contains(string(lastStatus, "status"));
            queueState.setText("Queued: " + queued.size() + (queuePaused || failed ? " · paused; click to resume" : " · sends after this response"));
            queueState.setToolTipText(queued.snapshot().stream().map(PromptRequest::text)
                    .collect(java.util.stream.Collectors.joining(" | ")));
        }

        private void updateProviderUi() {
            int index = provider.getSelectedIndex();
            ((CardLayout) modelRow.getLayout()).show(modelRow, index == 1 ? "gguf" : "model");
            ggufRow.setVisible(index == 1);
            endpointRow.setVisible(index == 2);
            lmSourcesRow.setVisible(index == 2);
            load.setVisible(index != 1);
            refreshModels.setEnabled(index != 1);
            load.setToolTipText(index == 2 ? "Load selected model in LM Studio" : "Load selected Ollama model");
            advancedHint.setText(index == 0 ? "No additional settings for Ollama."
                    : index == 1 ? "Local GGUF file (loads on first request)" : "LM Studio local sources");
            if (!ggufPath.isBlank()) {
                String name = Path.of(ggufPath).getFileName().toString();
                ggufLabel.setText(name.length() > 32 ? name.substring(0, 29) + "…" : name);
                ggufLabel.setToolTipText(ggufPath);
                ggufQuick.setText(name.length() > 24 ? name.substring(0, 21) + "…" : name);
                ggufQuick.setToolTipText(ggufPath);
            }
            updateSelection();
            revalidate();
        }

        private void updateSelection() {
            int source = provider.getSelectedIndex();
            String chosen = source == 1 ? ggufPath : model.getSelectedItem() == null ? "" : model.getSelectedItem().toString();
            ModelSelection next = new ModelSelection(source, chosen == null ? "" : chosen,
                    source == 2 ? endpoint.getText().trim() : System.getenv().getOrDefault("OLLAMA_HOST", "http://127.0.0.1:11434"));
            if (!next.equals(selection)) {
                selection = next;
                lastCapabilityCheck = 0;
                thinkingAvailable = false;
                effort.setVisible(false);
                effort.setSelectedItem("off");
                if (toolbar.getComponentCount() > 0) layoutToolbar();
            }
        }

        private static JButton iconButton(Icon icon, String name) {
            JButton button = new JButton(icon);
            button.setToolTipText(name);
            button.getAccessibleContext().setAccessibleName(name);
            button.setMargin(JBUI.emptyInsets());
            button.setPreferredSize(JBUI.size(24, 24));
            button.setMinimumSize(JBUI.size(24, 24));
            return button;
        }

        private static void compactControl(JComponent control, int width, String name) {
            control.setPreferredSize(JBUI.size(width, 26));
            control.setMinimumSize(JBUI.size(Math.min(width, 70), 26));
            control.setToolTipText(name);
            control.getAccessibleContext().setAccessibleName(name);
            control.setFont(control.getFont().deriveFont(Math.max(11f, control.getFont().getSize2D() - 1)));
        }

        private static void toolbarCell(JPanel row, JComponent control, int column, double weight) {
            toolbarCell(row, control, column, 0, weight);
        }

        private static void toolbarCell(JPanel row, JComponent control, int column, int line, double weight) {
            GridBagConstraints constraints = new GridBagConstraints();
            constraints.gridx = column;
            constraints.gridy = line;
            constraints.weightx = weight;
            constraints.fill = GridBagConstraints.HORIZONTAL;
            constraints.anchor = GridBagConstraints.WEST;
            constraints.insets = JBUI.insets(line == 0 ? 0 : 3, column == 0 ? 0 : 3, 0, 0);
            row.add(control, constraints);
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
                catch (Exception error) { SwingUtilities.invokeLater(() -> showError(error)); }
            });
        }

        private void refresh() {
            if (!polling.compareAndSet(false, true)) return;
            executor.execute(() -> {
                try { refreshNow(); }
                catch (Exception error) { SwingUtilities.invokeLater(() -> showError(error)); }
                finally { polling.set(false); }
            });
        }

        private Path backendHome() throws Exception {
            String configured = System.getenv("AI_AGENT_MINI_HOME");
            if (configured != null && Files.isRegularFile(Path.of(configured, "ide_bootstrap.py")))
                return Path.of(configured).toAbsolutePath().normalize();
            var descriptor = PluginManagerCore.getPlugin(PluginId.getId("local.aiagentmini.intellij"));
            if (descriptor == null) throw new IOException("IntelliJ could not find the registered AI Agent Mini plugin.");
            return BackendResources.prepare(descriptor.getPluginPath());
        }

        private void showError(Exception error) {
            state.setText("Error ⓘ");
            state.setToolTipText(error.getMessage() == null ? error.toString() : error.getMessage());
            state.getAccessibleContext().setAccessibleDescription(state.getToolTipText());
            cancel.setEnabled(started);
        }

        private void ensureBackend() throws Exception {
            if (token.isEmpty()) {
                try { connect(); return; }
                catch (Exception error) {
                    if (String.valueOf(error.getMessage()).contains("Restart")) throw error;
                }
                if (!bootAttempted) {
                    bootAttempted = true;
                    try {
                        Path home = backendHome();
                        Path venv = home.resolve(System.getProperty("os.name").startsWith("Windows")
                                ? ".venv/Scripts/python.exe" : ".venv/bin/python");
                        String python = Files.isRegularFile(venv) ? venv.toString()
                                : System.getProperty("os.name").startsWith("Windows") ? "python" : "python3";
                        backendLog = home.resolve("backend.log");
                        backendProcess = new ProcessBuilder(python, home.resolve("ide_bootstrap.py").toString(),
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
                    if (backendProcess != null && !backendProcess.isAlive()) {
                        bootAttempted = false;
                        throw new IOException("Backend exited with code " + backendProcess.exitValue() + "; see " + backendLog);
                    }
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
            if (!connection.has("version") || connection.get("version").getAsInt() != 19)
                throw new IOException("Restart the AI Agent Mini backend to load the IDE API.");
            token = string(connection, "token");
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
                    started = false;
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
                catalog.clear();
                for (JsonElement item : entries) {
                    JsonObject entry = item.getAsJsonObject();
                    if (source == 2 && entry.has("type") && !"llm".equals(string(entry, "type"))) continue;
                    String name = source == 2 ? entry.has("key") ? string(entry, "key") : string(entry, "id")
                            : entry.has("name") ? string(entry, "name") : string(entry, "model");
                    if (!name.isBlank() && !name.endsWith(":cloud") && !name.endsWith("-cloud")) catalog.put(name, entry);
                }
                populateModels();
            });
        }

        private void populateModels() {
            String previous = selectedModel;
            List<String> names = new ArrayList<>(catalog.keySet());
            Comparator<String> byName = String.CASE_INSENSITIVE_ORDER.thenComparing(Comparator.naturalOrder());
            names.sort(sortModels.getSelectedIndex() == 1
                    ? Comparator.<String>comparingLong(name -> modelSize(catalog.get(name))).thenComparing(byName) : byName);
            rebuildingModels = true;
            model.removeAllItems();
            model.addItem("Choose model");
            for (String name : names) model.addItem(name);
            if (names.contains(previous)) model.setSelectedItem(previous);
            rebuildingModels = false;
            updateSelection();
        }

        private static long modelSize(JsonObject model) {
            JsonElement size = model.has("size_bytes") ? model.get("size_bytes") : model.get("size");
            return size != null && size.isJsonPrimitive() ? size.getAsLong() : Long.MAX_VALUE;
        }

        private JsonObject externalJson(String url) throws Exception {
            return externalJson(url, null);
        }

        private JsonObject externalJson(String url, JsonObject body) throws Exception {
            HttpURLConnection connection = (HttpURLConnection) URI.create(url).toURL().openConnection();
            connection.setConnectTimeout(3000);
            connection.setReadTimeout(5000);
            try {
                if (body != null) {
                    connection.setRequestMethod("POST");
                    connection.setDoOutput(true);
                    connection.setRequestProperty("Content-Type", "application/json");
                    try (var output = connection.getOutputStream()) { output.write(body.toString().getBytes(StandardCharsets.UTF_8)); }
                }
                int code = connection.getResponseCode();
                if (code >= 400) throw new IOException("Model server returned " + code);
                return JsonParser.parseString(new String(connection.getInputStream().readAllBytes(),
                        StandardCharsets.UTF_8)).getAsJsonObject();
            } finally { connection.disconnect(); }
        }

        private void checkThinking() {
            ModelSelection chosen = selection;
            if (System.nanoTime() - lastCapabilityCheck < 3_000_000_000L) return;
            lastCapabilityCheck = System.nanoTime();
            boolean supported = false;
            try {
                if (chosen.provider() == 0 && !chosen.model().isBlank() && !chosen.model().equals("Choose model")) {
                    String address = chosen.endpoint().contains("://") ? chosen.endpoint() : "http://" + chosen.endpoint();
                    URI uri = URI.create(address);
                    if (!List.of("localhost", "127.0.0.1", "::1").contains(uri.getHost())) return;
                    String base = uri.getScheme() + "://" + uri.getAuthority();
                    JsonArray running = externalJson(base + "/api/ps").getAsJsonArray("models");
                    boolean loaded = false;
                    for (JsonElement item : running) {
                        JsonObject entry = item.getAsJsonObject();
                        String name = entry.has("name") ? string(entry, "name") : string(entry, "model");
                        if (name.equals(chosen.model())) loaded = true;
                    }
                    if (loaded) {
                        String key = base + "/" + chosen.model();
                        if (!thinkingCache.containsKey(key)) {
                            JsonObject request = new JsonObject();
                            request.addProperty("model", chosen.model());
                            JsonObject details = externalJson(base + "/api/show", request);
                            boolean thinks = false;
                            if (details.has("capabilities") && details.get("capabilities").isJsonArray())
                                for (JsonElement capability : details.getAsJsonArray("capabilities"))
                                    if ("thinking".equals(capability.getAsString())) thinks = true;
                            thinkingCache.put(key, thinks);
                        }
                        supported = thinkingCache.get(key);
                    }
                }
            } catch (Exception ignored) { /* Unknown capability is never advertised as supported. */ }
            boolean available = supported;
            SwingUtilities.invokeLater(() -> {
                if (!chosen.equals(selection)) return;
                if (thinkingAvailable == available && effort.isVisible() == available) return;
                thinkingAvailable = available;
                effort.setVisible(available);
                if (!available) effort.setSelectedItem("off");
                layoutToolbar();
            });
        }

        private void submit(PromptRequest promptRequest) throws Exception {
            String instruction = promptRequest.instruction();
            String chosenEffort = promptRequest.effort();
            boolean useAgent = promptRequest.agent();
            boolean useMcp = promptRequest.mcp();
            boolean useBrowser = promptRequest.browser();
            ensureBackend();
            started = true;
            if (project.getBasePath() == null || project.getBasePath().isBlank())
                throw new IOException("Open a local IntelliJ project first.");
            Path root = Path.of(project.getBasePath()).toAbsolutePath().normalize();
            if (!Files.isDirectory(root)) throw new IOException("Open a local IntelliJ project first.");
            String source = promptRequest.selection().provider() == 1 ? "gguf"
                    : promptRequest.selection().provider() == 2 ? "lmstudio" : "ollama";
            String chosenModel = promptRequest.selection().model();
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
                request.addProperty("gguf_path", source.equals("gguf") ? chosenModel : "");
                if (source.equals("lmstudio")) {
                    request.addProperty("endpoint", promptRequest.selection().endpoint());
                    request.addProperty("extra_skill_root", promptRequest.skills());
                    if (useMcp) request.addProperty("extra_mcp_config", promptRequest.mcpConfig());
                }
                request.addProperty("agent_mode", useAgent);
                request.addProperty("chat_only", !useAgent);
                request.addProperty("mcp", useAgent && useMcp);
                request.addProperty("browser", useBrowser);
                request.addProperty("thinking_effort", chosenEffort);
                request.addProperty("max_tokens", 4096);
                request.addProperty("context_tokens", promptRequest.context());
                request.addProperty("temperature", 0.2);
                api("/api/run", request);
            }
            refreshNow();
        }

        private void refreshNow() throws Exception {
            JsonObject status = api("/api/status", null);
            lastStatus = status;
            checkThinking();
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
            String error = string(status, "error");
            if (error.isBlank()) error = string(lmPreload, "error");
            boolean modelLoading = "loading".equals(phase) || "loading".equals(string(lmPreload, "phase"));
            state.setText(!error.isBlank() ? "Error" : modelLoading ? "Loading…" : string(status, "status"));
            state.setToolTipText(project.getBasePath() + " · " + string(status, "status")
                    + (error.isBlank() ? "" : " · " + error));
            boolean loading = status.has("model_preload") && status.get("model_preload").isJsonObject()
                    && status.getAsJsonObject("model_preload").has("busy")
                    && status.getAsJsonObject("model_preload").get("busy").getAsBoolean();
            loading |= lmPreload.has("busy") && lmPreload.get("busy").getAsBoolean();
            requestRunning = "running".equals(string(status, "status"));
            loadingModel = loading;
            cancel.setEnabled(started);
            send.setEnabled(!requestRunning && !loadingModel && !submissionPending && queued.size() == 0);
            renderPlan(status);
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
            drainQueue();
        }

        private void renderPlan(JsonObject status) {
            JsonObject plan = status.has("plan") && status.get("plan").isJsonObject()
                    ? status.getAsJsonObject("plan") : new JsonObject();
            JsonArray steps = plan.has("steps") && plan.get("steps").isJsonArray()
                    ? plan.getAsJsonArray("steps") : new JsonArray();
            planPanel.setVisible(steps.size() > 0);
            if (steps.isEmpty()) {
                planToggle.setSelected(false);
                planScroll.setVisible(false);
                return;
            }
            int current = plan.has("current") && plan.get("current").isJsonPrimitive()
                    ? plan.get("current").getAsInt() : -1;
            planToggle.setText("Plan · " + steps.size() + " steps"
                    + (current >= 0 && current < steps.size() ? " · step " + (current + 1) : ""));
            StringBuilder text = new StringBuilder();
            for (int index = 0; index < steps.size(); index++) {
                text.append(index == current ? "→ " : "   ").append(index + 1).append(". ")
                        .append(steps.get(index).getAsString()).append('\n');
            }
            String updated = text.toString();
            if (!updated.equals(planText.getText())) planText.setText(updated);
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
