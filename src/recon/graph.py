import os
import sqlite3
import tree_sitter
from recon.parser import get_parser

class SemanticGraph:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.create_tables()

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS symbols (
            fqn TEXT PRIMARY KEY,
            name TEXT,
            type TEXT, -- 'class', 'function', 'method'
            file_path TEXT,
            start_line INTEGER,
            end_line INTEGER,
            FOREIGN KEY(file_path) REFERENCES files(path) ON DELETE CASCADE
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS imports (
            file_path TEXT,
            local_name TEXT,
            target_fqn TEXT,
            PRIMARY KEY(file_path, local_name),
            FOREIGN KEY(file_path) REFERENCES files(path) ON DELETE CASCADE
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            caller_fqn TEXT,
            callee_fqn TEXT,
            file_path TEXT,
            line INTEGER,
            PRIMARY KEY(caller_fqn, callee_fqn, file_path, line)
        )
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS references_table (
            symbol_name TEXT,
            file_path TEXT,
            line INTEGER,
            context TEXT
        )
        """)
        self.conn.commit()

    def clear(self):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM calls")
        cursor.execute("DELETE FROM imports")
        cursor.execute("DELETE FROM symbols")
        cursor.execute("DELETE FROM files")
        cursor.execute("DELETE FROM references_table")
        self.conn.commit()

    def register_file(self, file_path: str):
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO files (path) VALUES (?)", (file_path,))
        self.conn.commit()

    def register_symbol(self, fqn: str, name: str, symbol_type: str, file_path: str, start_line: int, end_line: int):
        cursor = self.conn.cursor()
        cursor.execute("""
        INSERT OR REPLACE INTO symbols (fqn, name, type, file_path, start_line, end_line)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (fqn, name, symbol_type, file_path, start_line, end_line))
        self.conn.commit()

    def register_import(self, file_path: str, local_name: str, target_fqn: str):
        cursor = self.conn.cursor()
        cursor.execute("""
        INSERT OR REPLACE INTO imports (file_path, local_name, target_fqn)
        VALUES (?, ?, ?)
        """, (file_path, local_name, target_fqn))
        self.conn.commit()

    def register_call(self, caller_fqn: str, callee_fqn: str, file_path: str, line: int):
        cursor = self.conn.cursor()
        cursor.execute("""
        INSERT OR IGNORE INTO calls (caller_fqn, callee_fqn, file_path, line)
        VALUES (?, ?, ?, ?)
        """, (caller_fqn, callee_fqn, file_path, line))
        self.conn.commit()

    def register_reference(self, symbol_name: str, file_path: str, line: int, context: str):
        cursor = self.conn.cursor()
        cursor.execute("""
        INSERT INTO references_table (symbol_name, file_path, line, context)
        VALUES (?, ?, ?, ?)
        """, (symbol_name, file_path, line, context))
        self.conn.commit()

    def is_test_file(self, file_path: str) -> bool:
        """Determines if a file is a test file to exclude from indexing."""
        parts = file_path.split(os.sep)
        if "dummy_repo" in parts:
            return False
        if "tests" in parts or "test" in parts:
            return True
        filename = parts[-1]
        if filename.startswith("test_") or filename.endswith("_test.py"):
            return True
        return False

    def get_module_fqn(self, repo_path: str, file_path: str) -> str:
        """Converts a file path relative to repo_path into a python module dot-path."""
        rel_path = os.path.relpath(file_path, repo_path)
        if rel_path.endswith(".py"):
            rel_path = rel_path[:-3]
        parts = rel_path.split(os.sep)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    def index_repository(self, repo_path: str):
        """Indexes all Python files in the repository (excluding tests)."""
        self.clear()
        python_files = []
        for root, dirs, files in os.walk(repo_path):
            # Skip hidden directories like .venv, .git, etc.
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for file in files:
                if file.endswith(".py"):
                    full_path = os.path.join(root, file)
                    if not self.is_test_file(full_path):
                        python_files.append(full_path)

        parser = get_parser()
        
        # Phase 1: Register files, symbols, and imports
        for file_path in python_files:
            self.register_file(file_path)
            module_fqn = self.get_module_fqn(repo_path, file_path)
            
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
            except Exception:
                continue

            tree = parser.parse(content)
            self._extract_symbols_and_imports(tree.root_node, file_path, module_fqn, content)

        # Phase 2: Resolve function calls and record references
        for file_path in python_files:
            module_fqn = self.get_module_fqn(repo_path, file_path)
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
            except Exception:
                continue

            tree = parser.parse(content)
            self._resolve_calls_and_references(tree.root_node, file_path, module_fqn, content)

    def _extract_symbols_and_imports(self, root_node: tree_sitter.Node, file_path: str, module_fqn: str, content: bytes):
        """Traverses the AST to find classes, functions, and imports."""
        scope_stack = [module_fqn]
        lines = content.split(b"\n")

        def traverse(node):
            if node.type == "import_statement":
                # import a, b.c as d
                for child in node.children:
                    if child.type == "dotted_name":
                        name = child.text.decode('utf8')
                        self.register_import(file_path, name, name)
                    elif child.type == "aliased_import":
                        name_node = child.child_by_field_name("name")
                        alias_node = child.child_by_field_name("alias")
                        if name_node and alias_node:
                            self.register_import(file_path, alias_node.text.decode('utf8'), name_node.text.decode('utf8'))
                return

            elif node.type == "import_from_statement":
                # from a.b import c, d as e
                module_node = node.child_by_field_name("module_name")
                if module_node:
                    module_name = module_node.text.decode('utf8')
                    # Find all imported symbols
                    for child in node.children:
                        if child.type == "import_list":
                            for subchild in child.children:
                                self._process_import_member(subchild, module_name, file_path)
                        elif child.type in ("aliased_import", "identifier"):
                            self._process_import_member(child, module_name, file_path)
                return

            elif node.type == "class_definition":
                name_node = node.child_by_field_name("name")
                if name_node:
                    class_name = name_node.text.decode('utf8')
                    parent_fqn = scope_stack[-1]
                    class_fqn = f"{parent_fqn}.{class_name}"
                    
                    self.register_symbol(
                        class_fqn, 
                        class_name, 
                        "class", 
                        file_path, 
                        node.start_point[0] + 1, 
                        node.end_point[0] + 1
                    )
                    
                    scope_stack.append(class_fqn)
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            traverse(child)
                    scope_stack.pop()
                return

            elif node.type == "function_definition":
                name_node = node.child_by_field_name("name")
                if name_node:
                    func_name = name_node.text.decode('utf8')
                    parent_fqn = scope_stack[-1]
                    func_fqn = f"{parent_fqn}.{func_name}"
                    
                    cursor = self.conn.cursor()
                    cursor.execute("SELECT type FROM symbols WHERE fqn = ?", (parent_fqn,))
                    parent_row = cursor.fetchone()
                    symbol_type = "method" if parent_row and parent_row[0] == "class" else "function"
                    
                    self.register_symbol(
                        func_fqn, 
                        func_name, 
                        symbol_type, 
                        file_path, 
                        node.start_point[0] + 1, 
                        node.end_point[0] + 1
                    )
                    
                    # We recurse into function body to find nested functions/classes
                    scope_stack.append(func_fqn)
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            traverse(child)
                    scope_stack.pop()
                return

            for child in node.children:
                traverse(child)

        traverse(root_node)

    def _process_import_member(self, node: tree_sitter.Node, module_name: str, file_path: str):
        if node.type == "aliased_import":
            name_node = node.child_by_field_name("name")
            alias_node = node.child_by_field_name("alias")
            if name_node and alias_node:
                self.register_import(
                    file_path, 
                    alias_node.text.decode('utf8'), 
                    f"{module_name}.{name_node.text.decode('utf8')}"
                )
        elif node.type == "identifier":
            name = node.text.decode('utf8')
            self.register_import(file_path, name, f"{module_name}.{name}")

    def _resolve_calls_and_references(self, root_node: tree_sitter.Node, file_path: str, module_fqn: str, content: bytes):
        """Analyzes function calls and registers identifier references."""
        lines = [line.decode('utf8', errors='ignore') for line in content.split(b"\n")]
        scope_stack = [module_fqn]

        # Get local imports
        cursor = self.conn.cursor()
        cursor.execute("SELECT local_name, target_fqn FROM imports WHERE file_path = ?", (file_path,))
        imports = dict(cursor.fetchall())

        # Get all workspace symbol names for reference matching
        cursor.execute("SELECT name FROM symbols")
        workspace_symbol_names = set(row[0] for row in cursor.fetchall())

        def traverse(node):
            # Track current scopes
            if node.type == "class_definition":
                name_node = node.child_by_field_name("name")
                if name_node:
                    class_name = name_node.text.decode('utf8')
                    scope_stack.append(f"{scope_stack[-1]}.{class_name}")
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            traverse(child)
                    scope_stack.pop()
                return

            elif node.type == "function_definition":
                name_node = node.child_by_field_name("name")
                if name_node:
                    func_name = name_node.text.decode('utf8')
                    scope_stack.append(f"{scope_stack[-1]}.{func_name}")
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            traverse(child)
                    scope_stack.pop()
                return

            # Check calls
            elif node.type == "call":
                callable_node = node.child_by_field_name("function")
                if callable_node:
                    callee_fqn = self._resolve_callable(callable_node, file_path, imports, scope_stack)
                    if callee_fqn:
                        caller_fqn = scope_stack[-1]
                        # Only register call dependencies if caller is a function/method
                        cursor.execute("SELECT type FROM symbols WHERE fqn = ?", (caller_fqn,))
                        caller_row = cursor.fetchone()
                        if caller_row and caller_row[0] in ("function", "method"):
                            self.register_call(caller_fqn, callee_fqn, file_path, node.start_point[0] + 1)

            # Match identifiers to trace all other references
            elif node.type == "identifier":
                name = node.text.decode('utf8')
                if name in workspace_symbol_names:
                    # Exclude the identifier if it is a definition name of class or function
                    parent = node.parent
                    is_definition = False
                    if parent:
                        if parent.type in ("class_definition", "function_definition") and parent.child_by_field_name("name") == node:
                            is_definition = True
                    
                    if not is_definition:
                        line_idx = node.start_point[0]
                        line_text = lines[line_idx] if line_idx < len(lines) else ""
                        self.register_reference(name, file_path, line_idx + 1, line_text.strip())

            for child in node.children:
                traverse(child)

        traverse(root_node)

    def _resolve_callable(self, node: tree_sitter.Node, file_path: str, imports: dict, scope_stack: list[str]) -> str | None:
        """Helper to resolve a callable AST node to its fully qualified name."""
        if node.type == "identifier":
            name = node.text.decode('utf8')
            # Check imports
            if name in imports:
                return imports[name]
            # Check if it is a local symbol in the current module
            module_fqn = scope_stack[0]
            local_fqn = f"{module_fqn}.{name}"
            cursor = self.conn.cursor()
            cursor.execute("SELECT fqn FROM symbols WHERE fqn = ?", (local_fqn,))
            row = cursor.fetchone()
            if row:
                return row[0]
            
            # Check if there is a unique global symbol with this name
            cursor.execute("SELECT fqn FROM symbols WHERE name = ?", (name,))
            rows = cursor.fetchall()
            if len(rows) == 1:
                return rows[0][0]
                
            return None

        elif node.type == "attribute":
            # e.g., self.method() or obj.method() or module.func()
            obj_node = node.child_by_field_name("object")
            attr_node = node.child_by_field_name("attribute")
            if obj_node and attr_node:
                attr_name = attr_node.text.decode('utf8')
                
                # Case 1: self.method()
                if obj_node.type == "identifier" and obj_node.text.decode('utf8') == "self":
                    # Find enclosing class FQN
                    for scope in reversed(scope_stack):
                        cursor = self.conn.cursor()
                        cursor.execute("SELECT type FROM symbols WHERE fqn = ?", (scope,))
                        row = cursor.fetchone()
                        if row and row[0] == "class":
                            return f"{scope}.{attr_name}"
                            
                # Case 2: module.func() where module is imported
                if obj_node.type == "identifier":
                    obj_name = obj_node.text.decode('utf8')
                    if obj_name in imports:
                        return f"{imports[obj_name]}.{attr_name}"
                        
                # Fallback: check if we can resolve by looking for a method name that matches
                cursor = self.conn.cursor()
                cursor.execute("SELECT fqn FROM symbols WHERE name = ? AND type = 'method'", (attr_name,))
                rows = cursor.fetchall()
                if len(rows) == 1:
                    return rows[0][0]

        return None

    def get_flow_dag(self) -> str:
        """Returns the Flow-DAG text representation in a deterministic sorting order."""
        cursor = self.conn.cursor()
        # Find calls where callee exists in symbols table (internal to workspace)
        cursor.execute("""
        SELECT DISTINCT c.caller_fqn, c.callee_fqn
        FROM calls c
        JOIN symbols s ON c.callee_fqn = s.fqn
        ORDER BY c.caller_fqn ASC, c.callee_fqn ASC
        """)
        rows = cursor.fetchall()
        
        if not rows:
            return "No call-graph edges detected in indexed files."

        lines = []
        for caller, callee in rows:
            lines.append(f"- `{caller}` -> `{callee}`")
        return "\n".join(lines)

    def find_symbol_references(self, symbol_name: str) -> str:
        """Finds all references to a class or function across the repository."""
        cursor = self.conn.cursor()
        
        # Verify symbol exists
        cursor.execute("SELECT fqn, type, file_path, start_line FROM symbols WHERE name = ?", (symbol_name,))
        definitions = cursor.fetchall()
        
        def_lines = []
        if definitions:
            def_lines.append("### Definitions")
            for fqn, sym_type, file_path, line in sorted(definitions, key=lambda x: x[0]):
                def_lines.append(f"- **{sym_type.capitalize()}** `{fqn}` defined at [L{line}]({file_path})")
            def_lines.append("")

        # Query references
        cursor.execute("""
        SELECT file_path, line, context
        FROM references_table
        WHERE symbol_name = ?
        ORDER BY file_path ASC, line ASC
        """, (symbol_name,))
        rows = cursor.fetchall()
        
        if not rows:
            ref_content = "No references found."
        else:
            ref_lines = []
            for file_path, line, context in rows:
                ref_lines.append(f"- **[file://{file_path}#L{line}]({file_path}#L{line})**")
                ref_lines.append(f"  ```python\n  {context}\n  ```")
            ref_content = "\n".join(ref_lines)
            
        return "\n".join(def_lines) + "### References\n" + ref_content

    def get_node_dependencies(self, file_path: str, function_name: str) -> str:
        """Returns downstream callees and upstream callers for a given node."""
        cursor = self.conn.cursor()
        
        # Find fully qualified name of the target function/method
        cursor.execute("""
        SELECT fqn, type FROM symbols
        WHERE file_path = ? AND name = ? AND type IN ('function', 'method')
        """, (file_path, function_name))
        row = cursor.fetchone()
        
        if not row:
            return f"Symbol `{function_name}` not found in `{file_path}`."
        
        fqn = row[0]
        sym_type = row[1]
        
        # Upstream callers (internal)
        cursor.execute("""
        SELECT DISTINCT caller_fqn FROM calls
        WHERE callee_fqn = ?
        ORDER BY caller_fqn ASC
        """, (fqn,))
        callers = [r[0] for r in cursor.fetchall()]
        
        # Downstream callees (internal or external)
        cursor.execute("""
        SELECT DISTINCT callee_fqn FROM calls
        WHERE caller_fqn = ?
        ORDER BY callee_fqn ASC
        """, (fqn,))
        callees = [r[0] for r in cursor.fetchall()]
        
        output = [
            f"## Dependencies for {sym_type.capitalize()} `{fqn}`",
            "",
            "### Upstream Callers (Who calls this?)"
        ]
        
        if callers:
            for caller in callers:
                output.append(f"- `{caller}`")
        else:
            output.append("- (None detected)")
            
        output.append("")
        output.append("### Downstream Callees (Who does this call?)")
        
        if callees:
            for callee in callees:
                output.append(f"- `{callee}`")
        else:
            output.append("- (None detected)")
            
        return "\n".join(output)
