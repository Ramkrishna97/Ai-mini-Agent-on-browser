package local.aiagentmini.intellij;

import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import static org.junit.Assert.*;

public class AttachmentFilesTest {
    @Rule public TemporaryFolder temporary = new TemporaryFolder();

    @Test public void referencesAreDeduplicatedAndImmutableForQueuedMessages() throws Exception {
        Path file = temporary.newFile("hello.txt").toPath();
        var attached = AttachmentFiles.merge(List.of(), List.of(file, file));
        assertEquals(List.of(file.toRealPath().toString()), attached);
        assertThrows(UnsupportedOperationException.class, () -> attached.add("other.txt"));
    }

    @Test public void invalidBatchesDoNotChangeExistingDraft() throws Exception {
        Path file = temporary.newFile("ok.java").toPath();
        var attached = AttachmentFiles.merge(List.of(), List.of(file));
        Path binary = temporary.newFile("bad.exe").toPath();
        assertThrows(IOException.class, () -> AttachmentFiles.merge(attached, List.of(binary)));
        assertThrows(IOException.class, () -> AttachmentFiles.merge(attached, List.of(temporary.getRoot().toPath())));
        assertEquals(1, attached.size());
    }

    @Test public void fileCountAndSizeAreBounded() throws Exception {
        List<Path> files = new ArrayList<>();
        for (int i = 0; i < 9; i++) files.add(temporary.newFile(i + ".txt").toPath());
        assertThrows(IOException.class, () -> AttachmentFiles.merge(List.of(), files));
        Files.write(files.get(0), new byte[2_000_001]);
        assertThrows(IOException.class, () -> AttachmentFiles.merge(List.of(), List.of(files.get(0))));
    }

    @Test public void pickerCannotEscapeProjectRoot() throws Exception {
        Path project = temporary.newFolder("project").toPath();
        Path inside = Files.createFile(project.resolve("ok.txt"));
        Path outside = temporary.newFile("outside.txt").toPath();
        assertTrue(AttachmentFiles.withinProject(inside, project));
        assertFalse(AttachmentFiles.withinProject(outside, project));
    }
}
