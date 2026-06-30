import os
import re
import textwrap
import tree_sitter
from recon.parser import get_parser, find_brace_blocks

def find_target_node(node: tree_sitter.Node, target_entity: str, current_class: str = None) -> tree_sitter.Node | None:
    """Recursively walks AST to find the function/method definition matching target_entity."""
    if node.type == "class_definition":
        name_node = node.child_by_field_name("name")
        if name_node:
            class_name = name_node.text.decode("utf8")
            body = node.child_by_field_name("body")
            if body:
                for child in body.children:
                    res = find_target_node(child, target_entity, class_name)
                    if res:
                        return res
        return None

    if node.type == "function_definition":
        name_node = node.child_by_field_name("name")
        if name_node:
            func_name = name_node.text.decode("utf8")
            entity_name = f"{current_class}.{func_name}" if current_class else func_name
            if entity_name == target_entity:
                return node

    for child in node.children:
        res = find_target_node(child, target_entity, current_class)
        if res:
            return res
            
    return None

def get_base_indentation(source_bytes: bytes, body_node: tree_sitter.Node, func_node: tree_sitter.Node) -> str:
    """Calculates the baseline indentation for the function body."""
    named_children = [c for c in body_node.children if c.is_named]
    if named_children:
        first_child = named_children[0]
        start_byte = first_child.start_byte
        i = start_byte - 1
        while i >= 0 and source_bytes[i] != ord("\n"):
            i -= 1
        line_prefix = source_bytes[i + 1 : start_byte]
        indent = ""
        for b in line_prefix:
            c = chr(b)
            if c in (" ", "\t"):
                indent += c
            else:
                break
        return indent
    
    start_byte = func_node.start_byte
    i = start_byte - 1
    while i >= 0 and source_bytes[i] != ord("\n"):
        i -= 1
    line_prefix = source_bytes[i + 1 : start_byte]
    func_indent = ""
    for b in line_prefix:
        c = chr(b)
        if c in (" ", "\t"):
            func_indent += c
        else:
            break
    return func_indent + "    "

def find_non_python_target_bounds(content: bytes, target_entity: str, file_path: str) -> tuple[int, int]:
    """
    Finds the start and end byte offsets of the body block of target_entity in non-Python files.
    Returns (body_start_byte, body_end_byte).
    """
    ext = os.path.splitext(file_path)[1].lower()
    
    if ext == ".rb":
        lines = content.split(b"\n")
        re_class = re.compile(r'^\s*(class|module)\s+([A-Za-z0-9_::]+)')
        re_def = re.compile(r'^\s*def\s+([A-Za-z0-9_?!.]+)')
        re_end = re.compile(r'^\s*end\b')
        re_start = re.compile(r'^\s*(class|module|def|begin|case)\b|(?:\bdo\b\s*(?:\|[^|]*\|)?\s*)$|^\s*(if|unless|while|until)\b')
        
        scope_stack = [""]
        stack = []
        
        target_parts = target_entity.split(".")
        target_name = target_parts[-1]
        target_class = target_parts[0] if len(target_parts) > 1 else ""
        
        line_offsets = []
        offset = 0
        for line in lines:
            line_offsets.append(offset)
            offset += len(line) + 1
            
        for line_idx, line in enumerate(lines):
            line_num = line_idx + 1
            line_str = line.decode('utf-8', errors='ignore')
            
            class_match = re_class.match(line_str)
            if class_match:
                name = class_match.group(2)
                scope_stack.append(name)
                stack.append(('class', name, 1))
                continue
                
            def_match = re_def.match(line_str)
            if def_match:
                name = def_match.group(1)
                current_class = scope_stack[-1] if len(scope_stack) > 1 else ""
                
                match_found = False
                if target_class:
                    if target_class == current_class and target_name == name:
                        match_found = True
                else:
                    if target_name == name:
                        match_found = True
                        
                if match_found:
                    body_start = line_offsets[line_idx] + len(line) + 1
                    
                    depth = 1
                    for sub_idx in range(line_idx + 1, len(lines)):
                        sub_line = lines[sub_idx]
                        sub_str = sub_line.decode('utf-8', errors='ignore')
                        if re_start.search(sub_str):
                            depth += 1
                        elif re_end.search(sub_str):
                            depth -= 1
                            if depth == 0:
                                body_end = line_offsets[sub_idx]
                                return body_start, body_end
                
                stack.append(('def', name, 1))
                continue
                
            if stack:
                if re_start.search(line_str):
                    t, n, depth = stack[-1]
                    stack[-1] = (t, n, depth + 1)
                elif re_end.search(line_str):
                    t, n, depth = stack[-1]
                    if depth == 1:
                        stack.pop()
                        if t == 'class':
                            scope_stack.pop()
                    else:
                        stack[-1] = (t, n, depth - 1)
                        
        raise ValueError(f"Entity '{target_entity}' not found in Ruby file '{file_path}'")
        
    blocks = find_brace_blocks(content)
    
    containers = []
    functions = []
    for start, end, b_type in blocks:
        if b_type == 'container':
            containers.append((start, end))
        else:
            functions.append((start, end))
            
    target_parts = target_entity.split(".")
    target_name = target_parts[-1]
    target_class = target_parts[0] if len(target_parts) > 1 else ""
    
    for start, end in functions:
        prev_end = 0
        for s, e, _ in blocks:
            if e < start and e > prev_end:
                prev_end = e
        prefix = content[prev_end:start].decode('utf-8', errors='ignore').strip()
        
        name = "unknown"
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
                # Java/C++ style: ReturnType MethodName( or ClassName::MethodName(
                match = re.search(r'\b([A-Za-z0-9_]+)\s*\([^)]*\)\s*(?:const\s*)?(?:\{|$)', prefix)
                if match:
                    name = match.group(1)
                else:
                    match = re.search(r'\b([A-Za-z0-9_]+)\s*\(', prefix)
                    if match:
                        name = match.group(1)
                
        if name == target_name:
            if target_class:
                current_class = ""
                
                # Go-style: extract receiver type from the func signature itself
                go_recv = re.search(r'\bfunc\s+\(\s*\w+\s+\*?([A-Za-z0-9_]+)\s*\)', prefix)
                if go_recv:
                    current_class = go_recv.group(1)
                
                if not current_class:
                    # Nesting-based: Rust impl, Java/C++ class { method { } }
                    for c_start, c_end in containers:
                        if c_start < start and c_end > end:
                            c_prev = 0
                            for s, e, _ in blocks:
                                if e < c_start and e > c_prev:
                                    c_prev = e
                            c_prefix = content[c_prev:c_start].decode('utf-8', errors='ignore').strip()
                            c_match = re.search(r'\b(?:class|struct|impl|interface|trait|enum)\s+([A-Za-z0-9_:]+)', c_prefix)
                            if c_match:
                                current_class = c_match.group(1)
                                break
                
                if current_class == target_class:
                    return start + 1, end
            else:
                return start + 1, end
                
    raise ValueError(f"Entity '{target_entity}' not found in file '{file_path}'")

def hydrate_node_body(file_path: str, target_entity: str) -> str:
    """Locates target_entity inside file_path and returns its body implementation text."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, "rb") as f:
        content = f.read()

    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".py":
        parser = get_parser()
        tree = parser.parse(content)
        
        func_node = find_target_node(tree.root_node, target_entity)
        if not func_node:
            raise ValueError(f"Entity '{target_entity}' not found in file '{file_path}'")

        body_node = func_node.child_by_field_name("body")
        if not body_node:
            return ""

        return content[body_node.start_byte : body_node.end_byte].decode("utf8")
    else:
        start_byte, end_byte = find_non_python_target_bounds(content, target_entity, file_path)
        return content[start_byte:end_byte].decode("utf8")

def mutate_node_body(file_path: str, target_entity: str, new_body_code: str) -> str:
    """Replaces the inner implementation block of target_entity in file_path."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, "rb") as f:
        content = f.read()

    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".py":
        parser = get_parser()
        tree = parser.parse(content)

        func_node = find_target_node(tree.root_node, target_entity)
        if not func_node:
            raise ValueError(f"Entity '{target_entity}' not found in file '{file_path}'")

        body_node = func_node.child_by_field_name("body")
        if not body_node:
            raise ValueError(f"Entity '{target_entity}' has no body block node")

        base_indent = get_base_indentation(content, body_node, func_node)
        normalized_body = textwrap.dedent(new_body_code)
        lines = normalized_body.splitlines()
        indented_lines = []
        for line in lines:
            if line.strip():
                indented_lines.append(base_indent + line)
            else:
                indented_lines.append("")
                
        indented_body = "\n".join(indented_lines)
        if not indented_body.startswith("\n") and body_node.start_byte < body_node.end_byte:
            orig_body_text = content[body_node.start_byte : body_node.end_byte]
            if orig_body_text.startswith(b"\n") or orig_body_text.startswith(b"\r\n"):
                newline = "\r\n" if b"\r\n" in orig_body_text else "\n"
                indented_body = newline + indented_body

        new_content_bytes = (
            content[:body_node.start_byte]
            + indented_body.encode("utf8")
            + content[body_node.end_byte:]
        )

        new_content_str = new_content_bytes.decode("utf8")
        try:
            compile(new_content_str, file_path, "exec")
        except SyntaxError as se:
            return f"Compilation Failed: Syntax Error in proposed mutation:\n{se.msg} (line {se.lineno}, col {se.offset})\nLine context: {se.text}"
        except Exception as e:
            return f"Compilation Failed: {str(e)}"

        with open(file_path, "wb") as f:
            f.write(new_content_bytes)

        return "Mutation successful and compiled successfully!"
    else:
        # Non-Python files
        start_byte, end_byte = find_non_python_target_bounds(content, target_entity, file_path)
        
        # Get baseline indentation from existing body
        body_content = content[start_byte:end_byte]
        lines = body_content.split(b"\n")
        base_indent = "    "
        for line in lines:
            line_str = line.decode('utf-8', errors='ignore')
            if line_str.strip():
                match = re.match(r'^([ \t]+)', line_str)
                if match:
                    base_indent = match.group(1)
                    break

        normalized_body = textwrap.dedent(new_body_code)
        body_lines = normalized_body.splitlines()
        indented_lines = []
        for line in body_lines:
            if line.strip():
                indented_lines.append(base_indent + line)
            else:
                indented_lines.append("")
        indented_body = "\n".join(indented_lines)
        
        # Ensure correct spacing at the boundaries
        if not indented_body.startswith("\n") and body_content.startswith(b"\n"):
            indented_body = "\n" + indented_body
        if not indented_body.endswith("\n") and body_content.endswith(b"\n"):
            indented_body = indented_body + "\n"

        new_content_bytes = (
            content[:start_byte]
            + indented_body.encode("utf8")
            + content[end_byte:]
        )

        # Basic language check (if cargo or go is available on path)
        compilation_error = None
        import subprocess
        if ext == ".rs":
            p = os.path.dirname(file_path)
            repo_path = None
            while p and p != os.path.dirname(p):
                if os.path.exists(os.path.join(p, "Cargo.toml")):
                    repo_path = p
                    break
                p = os.path.dirname(p)
            if repo_path:
                try:
                    res = subprocess.run(["cargo", "check"], cwd=repo_path, capture_output=True, text=True, timeout=10)
                    if res.returncode != 0:
                        compilation_error = f"Rust compilation check failed:\n{res.stderr}"
                except Exception:
                    pass
        elif ext == ".go":
            p = os.path.dirname(file_path)
            repo_path = None
            while p and p != os.path.dirname(p):
                if os.path.exists(os.path.join(p, "go.mod")):
                    repo_path = p
                    break
                p = os.path.dirname(p)
            if repo_path:
                try:
                    res = subprocess.run(["go", "build", "-o", os.devnull], cwd=repo_path, capture_output=True, text=True, timeout=10)
                    if res.returncode != 0:
                        compilation_error = f"Go compilation check failed:\n{res.stderr}"
                except Exception:
                    pass

        if compilation_error:
            return compilation_error

        with open(file_path, "wb") as f:
            f.write(new_content_bytes)

        return "Mutation successful and compiled successfully!"
