package local.aiagentmini.intellij;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.util.zip.ZipFile;

/** Extract bundled resources from the registered plugin path, not CodeSource (nullable in IDEA). */
final class BackendResources {
    static Path prepare(Path pluginRoot) throws IOException {
        Path home = pluginRoot.resolve("backend").toAbsolutePath().normalize();
        Path lib = pluginRoot.resolve("lib");
        if (Files.isDirectory(lib)) {
            try (var jars = Files.list(lib)) {
                for (Path jar : jars.filter(path -> path.toString().endsWith(".jar")).sorted().toList()) {
                    try (ZipFile archive = new ZipFile(jar.toFile())) {
                        if (archive.getEntry("backend/ide_bootstrap.py") == null) continue;
                        Files.createDirectories(home);
                        var entries = archive.entries();
                        while (entries.hasMoreElements()) {
                            var entry = entries.nextElement();
                            if (!entry.getName().startsWith("backend/") || entry.isDirectory()) continue;
                            Path target = home.resolve(entry.getName().substring("backend/".length())).normalize();
                            if (!target.startsWith(home)) throw new IOException("Invalid plugin backend resource path.");
                            Files.createDirectories(target.getParent());
                            try (var input = archive.getInputStream(entry)) {
                                Files.copy(input, target, StandardCopyOption.REPLACE_EXISTING);
                            }
                        }
                        return home;
                    }
                }
            }
        }
        if (Files.isRegularFile(home.resolve("ide_bootstrap.py"))) return home;
        throw new IOException("Bundled backend is missing. Reinstall the AI Agent Mini plugin.");
    }
}
