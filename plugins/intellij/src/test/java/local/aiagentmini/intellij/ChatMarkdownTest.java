package local.aiagentmini.intellij;

import org.junit.Test;
import javax.swing.*;
import java.awt.Color;
import static org.junit.Assert.*;

public class ChatMarkdownTest {
    @Test public void modelHtmlAndRemoteResourcesRemainInertText() {
        String html = ChatMarkdown.proseHtml("<img src='https://example.com/tracker'>\n<script>alert(1)</script>");
        assertFalse(html.contains("<img"));
        assertFalse(html.contains("<script"));
        assertTrue(html.contains("&lt;img"));
        assertTrue(html.contains("&lt;script&gt;"));
    }

    @Test public void formatsHeadingsListsAndInlineCodeWithoutInterpretingCodeAsMarkup() {
        String html = ChatMarkdown.proseHtml("## Overview\n- **main**\n- `<b>literal</b>`\n\n1. inspect\n2. run");
        assertTrue(html.contains("<h2>Overview</h2>"));
        assertTrue(html.contains("<ul><li><b>main</b></li>"));
        assertTrue(html.contains("<code>&lt;b&gt;literal&lt;/b&gt;</code>"));
        assertTrue(html.contains("<ol><li>inspect</li>"));
        assertTrue(html.endsWith("</ol>"));
    }

    @Test public void fencedCodeIsKeptSeparateAndUnchangedForCopy() {
        var blocks = ChatMarkdown.blocks("Here is code:\n```java\n  if (a < b) {\n    go();\n  }\n```\nDone.");
        assertEquals(3, blocks.size());
        assertFalse(blocks.get(0).code());
        assertTrue(blocks.get(1).code());
        assertEquals("java", blocks.get(1).language());
        assertEquals("  if (a < b) {\n    go();\n  }", blocks.get(1).text());
        assertFalse(blocks.get(2).code());
    }

    @Test public void incompleteStreamingFenceAndNestedShorterFenceRemainCode() {
        var blocks = ChatMarkdown.blocks("~~~~python\nprint('hello')\n~~~");
        assertEquals(1, blocks.size());
        assertTrue(blocks.get(0).code());
        assertEquals("print('hello')\n~~~", blocks.get(0).text());
        assertFalse(ChatMarkdown.blocks("```<img src=x>\nhello\n```").get(0).language().contains("<"));
    }

    @Test public void longCodeIsScrollableInsteadOfWideningTheConversation() throws Exception {
        SwingUtilities.invokeAndWait(() -> {
            JPanel view = ChatMarkdown.view("## Result\nA short reply.\n```text\n" + "long".repeat(500) + "\n```", 320, Color.GRAY);
            assertTrue(view.getPreferredSize().width <= 320);
            assertTrue(view.getPreferredSize().height > 40);
        });
    }
}
