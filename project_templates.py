"""Program-selected file contracts for common stacks, not model-selected actions."""

import re
import json
from textwrap import dedent

from prompter import OutputError


def project_template(instruction: str) -> list[dict] | None:
    text = instruction.casefold()
    if not (re.search(r"spring[\s-]*boot", text) and re.search(r"\breact\b", text)):
        return None
    # A fixed structure prevents a tiny model from declaring a one-file JSON
    # 'project' for a full Java/React request. The user task still supplies the
    # application requirements for each independent code-generation subroutine.
    files = {
        "backend/pom.xml": "Maven Spring Boot project, Java 17, Web, Data JPA, H2 dependencies, Spring Boot Maven plugin. Package com.example.app.",
        "backend/src/main/java/com/example/app/Application.java": "package com.example.app; public Application class with @SpringBootApplication and main running SpringApplication.run(Application.class,args).",
        "backend/src/main/java/com/example/app/Item.java": "package com.example.app; public JPA @Entity Item using jakarta.persistence. Long id @Id @GeneratedValue; String name; String description; no-arg constructor and getters/setters. No Lombok.",
        "backend/src/main/java/com/example/app/ItemRepository.java": "package com.example.app; public interface ItemRepository extends JpaRepository<Item,Long>. Import org.springframework.data.jpa.repository.JpaRepository.",
        "backend/src/main/java/com/example/app/ItemController.java": "package com.example.app; @RestController @RequestMapping(\"/api/items\"). Constructor-inject ItemRepository. GET list, GET /{id}, POST, PUT /{id}, DELETE /{id}. ResponseEntity with 404 handling. @CrossOrigin(origins=\"http://localhost:5173\").",
        "backend/src/main/resources/application.properties": "server.port=8080; H2 in-memory datasource jdbc:h2:mem:items; spring.jpa.hibernate.ddl-auto=update. Standard Java properties syntax, one setting per line.",
        "frontend/package.json": "Valid JSON package named crud-ui, private true, type module, scripts dev:vite and build:vite build. Compatible React, React DOM, Vite dependencies. No comments.",
        "frontend/index.html": "HTML entry document with div id=root and script type=module src=/src/main.jsx. Title describes requested app.",
        "frontend/src/main.jsx": "Import React, createRoot from react-dom/client, App from ./App.jsx, ./styles.css. Mount <App/> into document.getElementById('root').",
        "frontend/src/App.jsx": "Default-export React App. Implement requested CRUD UI using fetch('http://localhost:8080/api/items'). Item fields id,name,description. List, add, edit and delete; useState/useEffect, loading and error states.",
        "frontend/src/styles.css": "Responsive CSS only for an item CRUD UI. Style body, button, input, textarea, .app-shell, .toolbar, .item-form, .item-list, .item-card, .actions, .error, .loading. Use clear spacing, readable contrast, responsive cards. No JavaScript or HTML.",
        "frontend/vite.config.js": "Vite configuration enabling the automatic React JSX transform and development server on port 5173.",
        ".gitignore": "Ignore backend/target/, frontend/node_modules/, frontend/dist/, .env, IDE metadata. Plain gitignore patterns.",
    }
    return [{"path": path, "purpose": purpose} for path, purpose in files.items()]


def validate_file_contract(code: str, path: str) -> None:
    """Reject obvious scaffold failures instead of calling any fenced text success."""
    if path == "backend/pom.xml":
        from xml.etree.ElementTree import fromstring
        if fromstring(code).tag.rsplit("}", 1)[-1] != "project":
            raise OutputError("pom.xml must have a Maven <project> root, not generic XML.")
        for dependency in ("spring-boot", "spring-boot-starter-web", "spring-boot-starter-data-jpa", "h2"):
            if dependency not in code:
                raise OutputError(f"pom.xml is missing required dependency {dependency}.")
    if path.startswith("backend/src/main/java/com/example/app/"):
        name = path.rsplit("/", 1)[-1][:-5]
        if not re.search(r"\b(?:class|interface|record|enum)\s+" + re.escape(name) + r"\b", code):
            raise OutputError(f"{name}.java must declare {name}.")
        if not re.search(r"\bpackage\s+com\.example\.app\s*;", code):
            raise OutputError("Java file must declare package com.example.app.")
        required = {"Application": ["SpringBootApplication", "SpringApplication.run"],
                    "Item": ["@Entity", "@Id"], "ItemRepository": ["JpaRepository"],
                    "ItemController": ["@RestController", "@GetMapping", "@PostMapping", "@PutMapping", "@DeleteMapping"]}
        for marker in required.get(name, []):
            if marker not in code:
                raise OutputError(f"{name}.java is missing {marker}.")
    if path == "frontend/package.json":
        data = json.loads(code)
        if not isinstance(data, dict) or not isinstance(data.get("dependencies", {}), dict) or not isinstance(data.get("devDependencies", {}), dict):
            raise OutputError("frontend/package.json must be a package object with dependency objects.")
        dependencies = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        if not {"react", "react-dom", "vite"} <= dependencies.keys():
            raise OutputError("frontend/package.json must declare react, react-dom and vite dependencies.")
        if "dev" not in data.get("scripts", {}):
            raise OutputError("frontend/package.json must contain a dev script.")
    if path == "frontend/src/App.jsx":
        if "export default" not in code or "api/items" not in code:
            raise OutputError("App.jsx must default-export App and use the /api/items backend endpoint.")


def scaffold_content(path: str) -> str | None:
    """Known framework plumbing is program-owned; no model call is needed."""
    fixed = {
        ".gitignore": "backend/target/\nfrontend/node_modules/\nfrontend/dist/\n.env\n.idea/\n.vscode/\n",
        "backend/pom.xml": '''
            <project xmlns="http://maven.apache.org/POM/4.0.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
              xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
              <modelVersion>4.0.0</modelVersion>
              <parent><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-parent</artifactId><version>3.5.0</version><relativePath/></parent>
              <groupId>com.example</groupId><artifactId>crud-app</artifactId><version>0.0.1-SNAPSHOT</version>
              <properties><java.version>17</java.version></properties>
              <dependencies>
                <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency>
                <dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-data-jpa</artifactId></dependency>
                <dependency><groupId>com.h2database</groupId><artifactId>h2</artifactId><scope>runtime</scope></dependency>
              </dependencies>
              <build><plugins><plugin><groupId>org.springframework.boot</groupId><artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build>
            </project>
        ''',
        "backend/src/main/java/com/example/app/Application.java": '''
            package com.example.app;
            import org.springframework.boot.SpringApplication;
            import org.springframework.boot.autoconfigure.SpringBootApplication;
            @SpringBootApplication
            public class Application {
                public static void main(String[] args) { SpringApplication.run(Application.class, args); }
            }
        ''',
        "backend/src/main/java/com/example/app/Item.java": '''
            package com.example.app;
            import jakarta.persistence.Entity;
            import jakarta.persistence.GeneratedValue;
            import jakarta.persistence.GenerationType;
            import jakarta.persistence.Id;
            @Entity
            public class Item {
                @Id @GeneratedValue(strategy = GenerationType.IDENTITY)
                private Long id;
                private String name;
                private String description;
                public Item() {}
                public Long getId() { return id; }
                public void setId(Long id) { this.id = id; }
                public String getName() { return name; }
                public void setName(String name) { this.name = name; }
                public String getDescription() { return description; }
                public void setDescription(String description) { this.description = description; }
            }
        ''',
        "backend/src/main/java/com/example/app/ItemRepository.java": '''
            package com.example.app;
            import org.springframework.data.jpa.repository.JpaRepository;
            public interface ItemRepository extends JpaRepository<Item, Long> {}
        ''',
        "backend/src/main/java/com/example/app/ItemController.java": '''
            package com.example.app;
            import java.net.URI;
            import java.util.List;
            import org.springframework.http.ResponseEntity;
            import org.springframework.web.bind.annotation.*;
            @RestController
            @RequestMapping("/api/items")
            @CrossOrigin(origins = {"http://localhost:5173", "http://127.0.0.1:5173"})
            public class ItemController {
                private final ItemRepository repository;
                public ItemController(ItemRepository repository) { this.repository = repository; }
                @GetMapping public List<Item> list() { return repository.findAll(); }
                @GetMapping("/{id}") public ResponseEntity<Item> get(@PathVariable Long id) {
                    return repository.findById(id).map(ResponseEntity::ok).orElseGet(() -> ResponseEntity.notFound().build());
                }
                @PostMapping public ResponseEntity<Item> create(@RequestBody Item input) {
                    if (input.getName() == null || input.getName().isBlank()) return ResponseEntity.badRequest().build();
                    input.setId(null);
                    Item saved = repository.save(input);
                    return ResponseEntity.created(URI.create("/api/items/" + saved.getId())).body(saved);
                }
                @PutMapping("/{id}") public ResponseEntity<Item> update(@PathVariable Long id, @RequestBody Item input) {
                    if (input.getName() == null || input.getName().isBlank()) return ResponseEntity.badRequest().build();
                    return repository.findById(id).map(item -> {
                        item.setName(input.getName()); item.setDescription(input.getDescription());
                        return ResponseEntity.ok(repository.save(item));
                    }).orElseGet(() -> ResponseEntity.notFound().build());
                }
                @DeleteMapping("/{id}") public ResponseEntity<Void> delete(@PathVariable Long id) {
                    if (!repository.existsById(id)) return ResponseEntity.notFound().build();
                    repository.deleteById(id); return ResponseEntity.noContent().build();
                }
            }
        ''',
        "backend/src/main/resources/application.properties": "server.port=8080\nspring.datasource.url=jdbc:h2:mem:items\nspring.jpa.hibernate.ddl-auto=update\n",
        "frontend/package.json": json.dumps({"name": "crud-ui", "version": "1.0.0", "private": True, "type": "module",
            "scripts": {"dev": "vite --host 127.0.0.1", "build": "vite build"},
            "dependencies": {"react": "^19.0.0", "react-dom": "^19.0.0"}, "devDependencies": {"vite": "^6.0.0"}}, indent=2),
        "frontend/index.html": '<!doctype html>\n<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CRUD App</title></head><body><div id="root"></div><script type="module" src="/src/main.jsx"></script></body></html>\n',
        "frontend/src/main.jsx": '''
            import React from 'react';
            import {createRoot} from 'react-dom/client';
            import App from './App.jsx';
            import './styles.css';
            createRoot(document.getElementById('root')).render(<React.StrictMode><App /></React.StrictMode>);
        ''',
        "frontend/src/App.jsx": '''
            import {useEffect, useState} from 'react';
            const API = 'http://localhost:8080/api/items';
            const emptyForm = {name: '', description: ''};
            async function request(path = '', options = {}) {
                const response = await fetch(API + path, options);
                if (!response.ok) throw new Error(`Request failed (${response.status}). Check the backend and item values.`);
                return response.status === 204 ? null : response.json();
            }
            export default function App() {
                const [items, setItems] = useState([]);
                const [form, setForm] = useState(emptyForm);
                const [editing, setEditing] = useState(null);
                const [busy, setBusy] = useState(false);
                const [error, setError] = useState('');
                const [filter, setFilter] = useState('');
                async function load() {
                    setBusy(true); setError('');
                    try { setItems(await request()); }
                    catch (problem) { setError(problem.message); }
                    finally { setBusy(false); }
                }
                useEffect(() => { load(); }, []);
                async function save(event) {
                    event.preventDefault();
                    if (!form.name.trim()) return;
                    setBusy(true); setError('');
                    try {
                        await request(editing === null ? '' : `/${editing}`, {
                            method: editing === null ? 'POST' : 'PUT',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({name: form.name.trim(), description: form.description})
                        });
                        setEditing(null); setForm(emptyForm); setItems(await request());
                    } catch (problem) { setError(problem.message); }
                    finally { setBusy(false); }
                }
                async function remove(id) {
                    setBusy(true); setError('');
                    try {
                        await request(`/${id}`, {method: 'DELETE'});
                        if (editing === id) { setEditing(null); setForm(emptyForm); }
                        setItems(await request());
                    } catch (problem) { setError(problem.message); }
                    finally { setBusy(false); }
                }
                const visible = items.filter(item => `${item.name} ${item.description || ''}`.toLowerCase().includes(filter.toLowerCase()));
                return <main className="app-shell">
                    <header className="toolbar"><div><h1>Item manager</h1><p>Create, view, edit and delete your items.</p></div>
                        <button type="button" disabled={busy} onClick={load}>Refresh</button></header>
                    {error && <p className="error" role="alert">{error}</p>}
                    {busy && <p className="loading" role="status">Working...</p>}
                    <form className="item-form" onSubmit={save}>
                        <h2>{editing === null ? 'Create item' : 'Edit item'}</h2>
                        <label>Name<input required maxLength={255} value={form.name} disabled={busy}
                            onChange={event => setForm({...form, name: event.target.value})}/></label>
                        <label>Description<textarea maxLength={255} value={form.description} disabled={busy}
                            onChange={event => setForm({...form, description: event.target.value})}/></label>
                        <div className="actions"><button disabled={busy} type="submit">{editing === null ? 'Create' : 'Save changes'}</button>
                            {editing !== null && <button type="button" disabled={busy} onClick={() => {setEditing(null); setForm(emptyForm);}}>Cancel edit</button>}</div>
                    </form>
                    <label>Search items<input value={filter} onChange={event => setFilter(event.target.value)}/></label>
                    <section className="item-list" aria-label="Items">
                        {!busy && !visible.length && <p>No items found.</p>}
                        {visible.map(item => <article className="item-card" key={item.id}>
                            <h2>{item.name}</h2><p>{item.description}</p><div className="actions">
                                <button type="button" disabled={busy} onClick={() => {setEditing(item.id); setForm({name:item.name,description:item.description || ''});}}>Edit</button>
                                <button type="button" disabled={busy} onClick={() => remove(item.id)}>Delete</button>
                            </div></article>)}
                    </section>
                </main>;
            }
        ''',
        "frontend/vite.config.js": "import {defineConfig} from 'vite';\nexport default defineConfig({esbuild:{jsx:'automatic'},server:{host:'127.0.0.1',port:5173}});\n",
    }
    content = fixed.get(path)
    return dedent(content).strip() + "\n" if content is not None else None
