package local.aiagentmini.intellij;

import javax.swing.*;
import javax.swing.text.html.HTMLEditorKit;
import javax.swing.text.html.StyleSheet;
import java.awt.*;
import java.awt.datatransfer.StringSelection;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Pattern;

/** Small, inert Markdown presentation: model output never becomes executable HTML or remote resources. */
final class ChatMarkdown {
    record Block(boolean code, String language, String text) {}
    private static final Pattern FENCE = Pattern.compile("^ {0,3}(`{3,}|~{3,})(.*)$");

    static List<Block> blocks(String text) {
        List<Block> blocks = new ArrayList<>();
        StringBuilder current = new StringBuilder();
        String fence = "", language = "";
        for (String line : text.replace("\r\n", "\n").split("\n", -1)) {
            var match = FENCE.matcher(line);
            if (fence.isEmpty() && match.matches()) {
                if (!current.isEmpty()) blocks.add(new Block(false, "", trimEnd(current.toString())));
                current.setLength(0);
                fence = match.group(1);
                language = match.group(2).strip().replaceAll("[^a-zA-Z0-9_+#. -]", "");
                if (language.length() > 32) language = language.substring(0, 32);
            } else if (!fence.isEmpty() && line.strip().matches(Pattern.quote(fence.substring(0, 1))
                    + "{" + fence.length() + ",}")) {
                blocks.add(new Block(true, language, trimEnd(current.toString())));
                current.setLength(0);
                fence = "";
                language = "";
            } else current.append(line).append('\n');
        }
        if (!current.isEmpty() || !fence.isEmpty())
            blocks.add(new Block(!fence.isEmpty(), language, trimEnd(current.toString())));
        return List.copyOf(blocks);
    }

    private static String trimEnd(String text) {
        return text.endsWith("\n") ? text.substring(0, text.length() - 1) : text;
    }

    static String escape(String text) {
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                .replace("\"", "&quot;").replace("'", "&#39;");
    }

    private static String inline(String text) {
        // Escape first; only markup emitted here can reach the HTML renderer.
        StringBuilder output = new StringBuilder();
        String[] code = text.split("`", -1);
        for (int i = 0; i < code.length; i++) {
            String escaped = escape(code[i]);
            if (i % 2 == 1) output.append("<code>").append(escaped).append("</code>");
            else output.append(escaped.replaceAll("\\*\\*([^*]+)\\*\\*", "<b>$1</b>")
                    .replaceAll("(?<!\\*)\\*([^*]+)\\*(?!\\*)", "<i>$1</i>"));
        }
        return output.toString();
    }

    static String proseHtml(String text) {
        StringBuilder html = new StringBuilder();
        String list = "";
        for (String line : text.split("\n", -1)) {
            String stripped = line.strip();
            String kind = stripped.matches("[-*+]\\s+.*") ? "ul" : stripped.matches("\\d+[.)]\\s+.*") ? "ol" : "";
            if (!kind.equals(list)) {
                if (!list.isEmpty()) html.append("</").append(list).append('>');
                if (!kind.isEmpty()) html.append('<').append(kind).append('>');
                list = kind;
            }
            if (!list.isEmpty()) html.append("<li>").append(inline(stripped.replaceFirst("^(?:[-*+]|\\d+[.)])\\s+", ""))).append("</li>");
            else if (stripped.matches("#{1,6}\\s+.*")) {
                int level = Math.min(4, stripped.indexOf(' '));
                html.append("<h").append(level).append('>').append(inline(stripped.replaceFirst("^#+\\s+", "")))
                        .append("</h").append(level).append('>');
            } else if (stripped.startsWith("> ")) html.append("<blockquote>").append(inline(stripped.substring(2))).append("</blockquote>");
            else if (stripped.isEmpty()) html.append("<div style='margin-top:8px'></div>");
            else html.append(inline(line)).append("<br>");
        }
        if (!list.isEmpty()) html.append("</").append(list).append('>');
        return html.toString();
    }

    static JPanel view(String text, int width, Color background) {
        JPanel body = new JPanel();
        body.setLayout(new BoxLayout(body, BoxLayout.Y_AXIS));
        body.setBackground(background);
        for (Block block : blocks(text)) {
            JComponent part = block.code() ? codeView(block, width) : proseView(block.text(), width, background);
            part.setAlignmentX(Component.LEFT_ALIGNMENT);
            part.setMaximumSize(new Dimension(Integer.MAX_VALUE, part.getPreferredSize().height));
            body.add(part);
            body.add(Box.createVerticalStrut(8));
        }
        return body;
    }

    private static JComponent proseView(String text, int width, Color background) {
        JEditorPane pane = new JEditorPane();
        HTMLEditorKit kit = new HTMLEditorKit();
        StyleSheet style = new StyleSheet();
        style.addStyleSheet(kit.getStyleSheet());
        kit.setStyleSheet(style);
        Font font = UIManager.getFont("Label.font");
        Color foreground = UIManager.getColor("Label.foreground");
        if (foreground == null) foreground = Color.GRAY;
        String color = String.format("#%06x", foreground.getRGB() & 0xffffff);
        kit.getStyleSheet().addRule("body { font-family: '" + font.getFamily() + "'; font-size: "
                + font.getSize() + "pt; color: " + color + "; margin: 0; }");
        kit.getStyleSheet().addRule("h1,h2,h3,h4 { margin-top: 10px; margin-bottom: 6px; }");
        kit.getStyleSheet().addRule("ul,ol { margin-left: 22px; margin-top: 4px; margin-bottom: 8px; }");
        pane.setEditorKit(kit);
        pane.setText("<html><body>" + proseHtml(text) + "</body></html>");
        pane.setEditable(false);
        pane.setBackground(background);
        pane.setBorder(BorderFactory.createEmptyBorder());
        pane.setSize(Math.max(120, width), Short.MAX_VALUE);
        pane.setPreferredSize(new Dimension(Math.max(120, width), Math.max(20, pane.getPreferredSize().height)));
        return pane;
    }

    private static JComponent codeView(Block block, int width) {
        JPanel panel = new JPanel(new BorderLayout(0, 4));
        Color background = UIManager.getColor("TextArea.background");
        if (background == null) background = Color.DARK_GRAY;
        panel.setBackground(background);
        panel.setBorder(BorderFactory.createEmptyBorder(8, 10, 8, 10));
        JPanel header = new JPanel(new BorderLayout());
        header.setOpaque(false);
        header.add(new JLabel(block.language().isBlank() ? "Code" : block.language()), BorderLayout.WEST);
        JButton copy = new JButton("Copy");
        copy.setContentAreaFilled(false);
        copy.setBorder(BorderFactory.createEmptyBorder(1, 5, 1, 5));
        copy.setToolTipText("Copy this code block (does not execute it)");
        copy.addActionListener(event -> Toolkit.getDefaultToolkit().getSystemClipboard().setContents(
                new StringSelection(block.text()), null));
        header.add(copy, BorderLayout.EAST);
        panel.add(header, BorderLayout.NORTH);
        JTextArea code = new JTextArea(block.text());
        code.setEditable(false);
        code.setBackground(background);
        code.setFont(new Font(Font.MONOSPACED, Font.PLAIN, UIManager.getFont("Label.font").getSize()));
        JScrollPane scroll = new JScrollPane(code);
        scroll.setBorder(BorderFactory.createEmptyBorder());
        int lines = Math.max(1, Math.min(16, block.text().split("\n", -1).length));
        scroll.setPreferredSize(new Dimension(Math.max(100, width - 20),
                lines * code.getFontMetrics(code.getFont()).getHeight() + 18));
        panel.add(scroll, BorderLayout.CENTER);
        panel.setPreferredSize(new Dimension(width, panel.getPreferredSize().height));
        return panel;
    }
}
