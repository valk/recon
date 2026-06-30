import os
import sys
import sqlite3
import tree_sitter
from recon.parser import get_parser

# Maximum number of source files to index in a single repository.
# Very large repos (e.g. Apache Druid with 8 000+ Java files) exhaust available
# RAM during the two-phase walk.  We cap indexing to the first N files so that
# the blueprint and call-graph are still useful while the process stays alive.
MAX_FILES_TO_INDEX = 5000

# When a symbol name has more than this many distinct definitions it is almost
# certainly a generic word ("get", "set", "run", "close" …).  Recording every
# reference to such names bloats the references_table and the call graph without
# adding diagnostic value.  We skip them entirely.
MAX_SYMBOL_DEFINITIONS = 5

class SemanticGraph:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
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

    def register_symbol(self, fqn: str, name: str, symbol_type: str, file_path: str, start_line: int, end_line: int):
        cursor = self.conn.cursor()
        cursor.execute("""
        INSERT OR REPLACE INTO symbols (fqn, name, type, file_path, start_line, end_line)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (fqn, name, symbol_type, file_path, start_line, end_line))

    def register_import(self, file_path: str, local_name: str, target_fqn: str):
        cursor = self.conn.cursor()
        cursor.execute("""
        INSERT OR REPLACE INTO imports (file_path, local_name, target_fqn)
        VALUES (?, ?, ?)
        """, (file_path, local_name, target_fqn))

    def register_call(self, caller_fqn: str, callee_fqn: str, file_path: str, line: int):
        cursor = self.conn.cursor()
        cursor.execute("""
        INSERT OR IGNORE INTO calls (caller_fqn, callee_fqn, file_path, line)
        VALUES (?, ?, ?, ?)
        """, (caller_fqn, callee_fqn, file_path, line))

    def register_reference(self, symbol_name: str, file_path: str, line: int, context: str):
        cursor = self.conn.conn.cursor() if hasattr(self.conn, 'conn') else self.conn.cursor()
        cursor.execute("""
        INSERT INTO references_table (symbol_name, file_path, line, context)
        VALUES (?, ?, ?, ?)
        """, (symbol_name, file_path, line, context))

    def is_test_file(self, file_path: str) -> bool:
        """Determines if a file is a test file to exclude from indexing."""
        parts = file_path.split(os.sep)
        if "dummy_repo" in parts:
            return False
        if any(p in ("tests", "test", "testing", "spec") for p in parts):
            return True
        filename = parts[-1].lower()
        if (filename.startswith("test_") or 
            filename.endswith("_test.py") or 
            filename.endswith("_test.go") or 
            filename.endswith(".test.js") or 
            filename.endswith(".test.ts") or 
            filename.endswith("spec.rb")):
            return True
        return False

    def get_module_fqn(self, repo_path: str, file_path: str) -> str:
        """Converts a file path relative to repo_path into a dot-path module name."""
        rel_path = os.path.relpath(file_path, repo_path)
        root, ext = os.path.splitext(rel_path)
        parts = root.split(os.sep)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    def index_repository(self, repo_path: str):
        """Indexes all supported source files in the repository (excluding tests)."""
        self.clear()
        supported_files = []
        supported_exts = (".py", ".rs", ".go", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".hpp", ".php", ".rb")
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in ("node_modules", "build", "dist", "vendor", "venv", ".venv", "target", "website", "docs", "__pycache__", "wheels", "examples") and not d.endswith(".egg-info")]
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext in supported_exts:
                    full_path = os.path.join(root, file)
                    if not self.is_test_file(full_path):
                        supported_files.append(full_path)
                        if len(supported_files) >= MAX_FILES_TO_INDEX:
                            break
            if len(supported_files) >= MAX_FILES_TO_INDEX:
                break

        parser = get_parser()
        
        num_files = len(supported_files)
        print(f"[*] Found {num_files} files to index. Starting parsing phase...", file=sys.stderr, flush=True)

        # Phase 1: Register files, symbols, and imports
        for idx, file_path in enumerate(supported_files):
            if (idx + 1) % 50 == 0 or (idx + 1) == num_files:
                print(f"    [+] Parsing file {idx + 1}/{num_files}: {os.path.relpath(file_path, repo_path)}", file=sys.stderr, flush=True)
                
            self.register_file(file_path)
            module_fqn = self.get_module_fqn(repo_path, file_path)

            try:
                with open(file_path, "rb") as f:
                    content = f.read()
            except Exception:
                continue

            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".py":
                tree = parser.parse(content)
                self._extract_symbols_and_imports(tree.root_node, file_path, module_fqn, content)
            else:
                self._extract_non_python_symbols(file_path, module_fqn, content)

        # Build shared caches once after Phase 1 to avoid per-file DB round-trips.
        cursor = self.conn.cursor()
        cursor.execute("SELECT name, fqn FROM symbols")
        rows = cursor.fetchall()
        # symbol_map: name -> list of fqns
        symbol_map: dict[str, list[str]] = {}
        for name, fqn in rows:
            symbol_map.setdefault(name, []).append(fqn)
        # Names that appear in <= MAX_SYMBOL_DEFINITIONS places are non-generic.
        workspace_symbol_names: set[str] = {
            name for name, fqns in symbol_map.items()
            if len(fqns) <= MAX_SYMBOL_DEFINITIONS
        }

        # Phase 2: Resolve function calls and record references
        print(f"[*] Starting symbol resolution phase...", file=sys.stderr, flush=True)
        for idx, file_path in enumerate(supported_files):
            if (idx + 1) % 50 == 0 or (idx + 1) == num_files:
                print(f"    [+] Resolving references for file {idx + 1}/{num_files}: {os.path.relpath(file_path, repo_path)}", file=sys.stderr, flush=True)
                
            module_fqn = self.get_module_fqn(repo_path, file_path)
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
            except Exception:
                continue

            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".py":
                tree = parser.parse(content)
                self._resolve_calls_and_references(
                    tree.root_node, file_path, module_fqn, content,
                    workspace_symbol_names, symbol_map
                )
            else:
                self._resolve_non_python_calls_and_references(
                    file_path, module_fqn, content,
                    workspace_symbol_names, symbol_map
                )
        print(f"[*] Repository indexing complete.", file=sys.stderr, flush=True)
        self.conn.commit()

    def _extract_non_python_symbols(self, file_path: str, module_fqn: str, content: bytes):
        import re
        from recon.parser import find_brace_blocks
        
        ext = os.path.splitext(file_path)[1].lower()
        
        if ext == ".rb":
            lines = content.split(b"\n")
            scope_stack = [module_fqn]
            
            re_class = re.compile(r'^\s*(class|module)\s+([A-Za-z0-9_::]+)')
            re_def = re.compile(r'^\s*def\s+([A-Za-z0-9_?!.]+)')
            re_end = re.compile(r'^\s*end\b')
            re_start = re.compile(r'^\s*(class|module|def|begin|case)\b|(?:\bdo\b\s*(?:\|[^|]*\|)?\s*)$|^\s*(if|unless|while|until)\b')
            
            stack = []
            
            for line_idx, line in enumerate(lines):
                line_num = line_idx + 1
                line_str = line.decode('utf-8', errors='ignore')
                
                class_match = re_class.match(line_str)
                if class_match:
                    name = class_match.group(2)
                    fqn = f"{scope_stack[-1]}.{name}"
                    self.register_symbol(fqn, name, "class", file_path, line_num, line_num)
                    scope_stack.append(fqn)
                    stack.append(('class', line_num, name, 1))
                    continue
                    
                def_match = re_def.match(line_str)
                if def_match:
                    name = def_match.group(1)
                    fqn = f"{scope_stack[-1]}.{name}"
                    symbol_type = "method" if len(scope_stack) > 1 else "function"
                    self.register_symbol(fqn, name, symbol_type, file_path, line_num, line_num)
                    stack.append(('def', line_num, name, 1))
                    continue
                    
                if stack:
                    if re_start.search(line_str):
                        t, sl, n, depth = stack[-1]
                        stack[-1] = (t, sl, n, depth + 1)
                    elif re_end.search(line_str):
                        t, sl, n, depth = stack[-1]
                        if depth == 1:
                            stack.pop()
                            if t == 'class':
                                scope_stack.pop()
                            cursor = self.conn.cursor()
                            cursor.execute("UPDATE symbols SET end_line = ? WHERE fqn = ?", (line_num, fqn))
                            self.conn.commit()
                        else:
                            stack[-1] = (t, sl, n, depth - 1)
            return

        blocks = find_brace_blocks(content)
        
        def get_line_num(offset):
            return content[:offset].count(b"\n") + 1
            
        containers = []
        functions = []
        for start, end, b_type in blocks:
            if b_type == 'container':
                containers.append((start, end))
            else:
                functions.append((start, end))
                
        for start, end in containers:
            prev_end = 0
            for s, e, _ in blocks:
                if e < start and e > prev_end:
                    prev_end = e
            prefix = content[prev_end:start].decode('utf-8', errors='ignore').strip()
            
            name = "UnknownContainer"
            match = re.search(r'\b(?:class|struct|impl|interface|trait|enum)\s+([A-Za-z0-9_:]+)', prefix)
            if match:
                name = match.group(1)
            else:
                cleaned = re.sub(r'[:<({].*$', '', prefix).strip()
                words = cleaned.split()
                if words:
                    name = words[-1]
                    
            fqn = f"{module_fqn}.{name}"
            start_line = get_line_num(start)
            end_line = get_line_num(end)
            self.register_symbol(fqn, name, "class", file_path, start_line, end_line)
            
        for start, end in functions:
            prev_end = 0
            for s, e, _ in blocks:
                if e < start and e > prev_end:
                    prev_end = e
            prefix = content[prev_end:start].decode('utf-8', errors='ignore').strip()
            
            name = "unknown_func"
            # Go: func (receiver Type) MethodName(...) – strip receiver before matching
            match = re.search(r'\bfunc\s+\([^)]*\)\s+([A-Za-z0-9_]+)\s*\(', prefix)
            if match:
                name = match.group(1)
            else:
                # Rust/general: fn/func/function Name
                match = re.search(r'\b(?:fn|func|function)\s+([A-Za-z0-9_]+)', prefix)
                if match:
                    name = match.group(1)
                else:
                    # Java/C++ style: ReturnType MethodName(
                    match = re.search(r'\b([A-Za-z0-9_]+)\s*\([^)]*\)\s*(?:const\s*)?(?:\{|$)', prefix)
                    if match:
                        name = match.group(1)
                    else:
                        match = re.search(r'\b([A-Za-z0-9_]+)\s*\(', prefix)
                        if match:
                            name = match.group(1)
            
            parent_container_fqn = module_fqn
            for c_start, c_end in containers:
                if c_start < start and c_end > end:
                    cursor = self.conn.cursor()
                    cursor.execute("SELECT fqn FROM symbols WHERE file_path = ? AND type = 'class' AND start_line <= ? AND end_line >= ?",
                                   (file_path, get_line_num(c_start), get_line_num(c_end)))
                    row = cursor.fetchone()
                    if row:
                        parent_container_fqn = row[0]
                        break
            
            fqn = f"{parent_container_fqn}.{name}"
            symbol_type = "method" if parent_container_fqn != module_fqn else "function"
            start_line = get_line_num(start)
            end_line = get_line_num(end)
            self.register_symbol(fqn, name, symbol_type, file_path, start_line, end_line)

    def _resolve_non_python_calls_and_references(
        self, file_path: str, module_fqn: str, content: bytes,
        workspace_symbol_names: set[str], symbol_map: dict[str, list[str]]
    ):
        import re
        lines = content.split(b"\n")

        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT fqn, start_line, end_line FROM symbols "
            "WHERE file_path = ? AND type IN ('function', 'method')",
            (file_path,)
        )
        file_funcs = cursor.fetchall()

        # Build a per-file module FQN prefix so we can prefer local symbols.
        for line_idx, line in enumerate(lines):
            line_num = line_idx + 1
            line_str = line.decode('utf-8', errors='ignore')

            caller_fqn = module_fqn
            for fqn, start_l, end_l in file_funcs:
                if start_l <= line_num <= end_l:
                    caller_fqn = fqn
                    break

            words = re.findall(r'\b([A-Za-z0-9_]+)\b', line_str)
            for word in words:
                # Only track non-generic workspace symbols.
                if word not in workspace_symbol_names:
                    continue

                self.register_reference(word, file_path, line_num, line_str.strip())

                if re.search(r'\b' + re.escape(word) + r'\s*\(', line_str):
                    candidates = symbol_map.get(word, [])
                    # Prefer a local symbol (same module prefix) to avoid
                    # registering calls to every same-named symbol in the repo.
                    local_fqn = f"{module_fqn}.{word}"
                    if local_fqn in candidates:
                        self.register_call(caller_fqn, local_fqn, file_path, line_num)
                    elif len(candidates) == 1:
                        self.register_call(caller_fqn, candidates[0], file_path, line_num)

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

    def _resolve_calls_and_references(
        self, root_node: tree_sitter.Node, file_path: str, module_fqn: str, content: bytes,
        workspace_symbol_names: set[str], symbol_map: dict[str, list[str]]
    ):
        """Analyzes function calls and registers identifier references."""
        lines = [line.decode('utf8', errors='ignore') for line in content.split(b"\n")]
        scope_stack = [module_fqn]

        # Get local imports (still per-file, small query)
        cursor = self.conn.cursor()
        cursor.execute("SELECT local_name, target_fqn FROM imports WHERE file_path = ?", (file_path,))
        imports = dict(cursor.fetchall())

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
                    callee_fqn = self._resolve_callable(
                        callable_node, file_path, imports, scope_stack, symbol_map
                    )
                    if callee_fqn:
                        caller_fqn = scope_stack[-1]
                        # Only register call dependencies if caller is a function/method
                        cursor.execute("SELECT type FROM symbols WHERE fqn = ?", (caller_fqn,))
                        caller_row = cursor.fetchone()
                        if caller_row and caller_row[0] in ("function", "method"):
                            self.register_call(caller_fqn, callee_fqn, file_path, node.start_point[0] + 1)

            # Match identifiers to trace all other references (skip generic/ambiguous names)
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

    def _resolve_callable(
        self, node: tree_sitter.Node, file_path: str, imports: dict,
        scope_stack: list[str], symbol_map: dict[str, list[str]]
    ) -> str | None:
        """Helper to resolve a callable AST node to its fully qualified name."""
        if node.type == "identifier":
            name = node.text.decode('utf8')
            # Check imports first
            if name in imports:
                return imports[name]
            # Prefer a local symbol in the current module (no DB query needed)
            module_fqn = scope_stack[0]
            local_fqn = f"{module_fqn}.{name}"
            candidates = symbol_map.get(name, [])
            if local_fqn in candidates:
                return local_fqn
            # Accept a unique global symbol
            if len(candidates) == 1:
                return candidates[0]
            return None

        elif node.type == "attribute":
            # e.g., self.method() or obj.method() or module.func()
            obj_node = node.child_by_field_name("object")
            attr_node = node.child_by_field_name("attribute")
            if obj_node and attr_node:
                attr_name = attr_node.text.decode('utf8')

                # Case 1: self.method() — find enclosing class FQN from scope stack
                if obj_node.type == "identifier" and obj_node.text.decode('utf8') == "self":
                    cursor = self.conn.cursor()
                    for scope in reversed(scope_stack):
                        cursor.execute("SELECT type FROM symbols WHERE fqn = ?", (scope,))
                        row = cursor.fetchone()
                        if row and row[0] == "class":
                            return f"{scope}.{attr_name}"

                # Case 2: module.func() where module is imported
                if obj_node.type == "identifier":
                    obj_name = obj_node.text.decode('utf8')
                    if obj_name in imports:
                        return f"{imports[obj_name]}.{attr_name}"

                # Fallback: unique method name in entire workspace (use cache)
                method_candidates = [
                    fqn for fqn in symbol_map.get(attr_name, [])
                    if f".{attr_name}" in fqn
                ]
                if len(method_candidates) == 1:
                    return method_candidates[0]

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
            fence = "```"
            ref_lines = []
            for file_path, line, context in rows:
                ref_lines.append(f"- **[file://{file_path}#L{line}]({file_path}#L{line})**")
                ref_lines.append(f"  {fence}python\n  {context}\n  {fence}")
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
