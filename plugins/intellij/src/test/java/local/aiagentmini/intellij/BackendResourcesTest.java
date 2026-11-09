package local.aiagentmini.intellij;

import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;
import static org.junit.Assert.*;

public class BackendResourcesTest {
    @Rule public TemporaryFolder temporary = new TemporaryFolder();

    private void jar(Path root, Map<String, String> resources) throws IOException {
        Files.createDirectories(root.resolve("lib"));
        try (var zip = new ZipOutputStream(Files.newOutputStream(root.resolve("lib/agent.jar")))) {
            for (var entry : resources.entrySet()) {
                zip.putNextEntry(new ZipEntry(entry.getKey()));
                zip.write(entry.getValue().getBytes(StandardCharsets.UTF_8));
                zip.closeEntry();
            }
        }
    }

    @Test public void installedPluginResourcesDoNotRequireCodeSourceOrProjectFolder() throws Exception {
        Path root = temporary.newFolder("installed plugin").toPath();
        jar(root, Map.of("backend/ide_bootstrap.py", "bootstrap", "backend/ui_server.py", "server",
                "unrelated.txt", "ignore"));
        Path home = BackendResources.prepare(root);
        assertEquals(root.resolve("backend").toAbsolutePath(), home);
        assertEquals("server", Files.readString(home.resolve("ui_server.py")));
        assertFalse(Files.exists(home.resolve("unrelated.txt")));
    }

    @Test public void pluginUpdateRefreshesSourcesWithoutReplacingTheEnvironment() throws Exception {
        Path root = temporary.newFolder().toPath();
        jar(root, Map.of("backend/ide_bootstrap.py", "first"));
        Path home = BackendResources.prepare(root);
        Files.createDirectories(home.resolve(".venv"));
        Files.writeString(home.resolve(".venv/keep"), "installed dependencies");
        jar(root, Map.of("backend/ide_bootstrap.py", "updated"));
        BackendResources.prepare(root);
        assertEquals("updated", Files.readString(home.resolve("ide_bootstrap.py")));
        assertEquals("installed dependencies", Files.readString(home.resolve(".venv/keep")));
    }

    @Test public void archiveCannotWriteOutsideTheBackendDirectory() throws Exception {
        Path root = temporary.newFolder().toPath();
        jar(root, Map.of("backend/ide_bootstrap.py", "bootstrap", "backend/../escape.txt", "bad"));
        assertThrows(IOException.class, () -> BackendResources.prepare(root));
        assertFalse(Files.exists(root.resolve("escape.txt")));
    }

    @Test public void missingBundleReportsAnActionableError() throws Exception {
        Path root = temporary.newFolder().toPath();
        IOException error = assertThrows(IOException.class, () -> BackendResources.prepare(root));
        assertTrue(error.getMessage().contains("Reinstall"));
    }
}
