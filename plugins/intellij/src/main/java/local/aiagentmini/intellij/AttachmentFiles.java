package local.aiagentmini.intellij;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/** Read-only references; actual contents are read and revalidated by the backend at send time. */
final class AttachmentFiles {
    private static final Set<String> EXTENSIONS = Set.of(
            "py", "pyw", "js", "jsx", "ts", "tsx", "java", "json", "html", "htm", "css", "scss",
            "cs", "cpp", "c", "h", "go", "rs", "sh", "bat", "cmd", "ps1", "sql", "xml", "yaml",
            "yml", "toml", "ini", "md", "txt", "csv", "svg", "php", "rb", "kt", "ipynb", "log", "pdf", "docx");

    static List<String> merge(List<String> current, List<Path> additions) throws IOException {
        List<String> result = new ArrayList<>(current);
        for (Path input : additions) {
            Path file = input.toRealPath();
            if (!Files.isRegularFile(file) || !Files.isReadable(file))
                throw new IOException("Attach readable files, not folders: " + file.getFileName());
            String name = file.getFileName().toString();
            int dot = name.lastIndexOf('.');
            if (dot < 0 || !EXTENSIONS.contains(name.substring(dot + 1).toLowerCase(Locale.ROOT)))
                throw new IOException("Unsupported attachment type: " + name);
            if (Files.size(file) > 2_000_000) throw new IOException("Attachment exceeds 2 MB: " + name);
            if (result.stream().noneMatch(existing -> existing.equalsIgnoreCase(file.toString())))
                result.add(file.toString());
            if (result.size() > 8) throw new IOException("Attach at most 8 files per message.");
        }
        return List.copyOf(result);
    }

    static boolean withinProject(Path file, Path root) throws IOException {
        return file.toRealPath().startsWith(root.toRealPath());
    }
}
