import os
import re
import tree_sitter
import tree_sitter_python
from tree_sitter import Language, Parser

# Initialize the Python language and parser
PY_LANGUAGE = Language(tree_sitter_python.language())

def get_parser() -> Parser:
    return Parser(PY_LANGUAGE)

def is_docstring(node: tree_sitter.Node) -> bool:
    """Checks if a node is a docstring."""
    if node.type == "string":
        return True
    if node.type == "expression_statement":
        # In python, a docstring expression statement has a single string child
        if len(node.children) > 0 and node.children[0].type == "string":
            return True
    return False

def get_elision_groups(block_node: tree_sitter.Node) -> list[tuple[int, int]]:
    """
    Identifies ranges (start_byte, end_byte) inside a function block
    that should be replaced with '...' to elide implementation details.
    Preserves docstrings (if first statement) and comments.
    """
    groups = []
    current_group = []
    
    # Get all named children of the block
    named_children = [c for c in block_node.children if c.is_named]
    
    for i, child in enumerate(named_children):
        # Docstring: if it's the first named child and is a string/docstring
        if i == 0 and is_docstring(child):
            continue
            
        # Comment: keep comment
        if child.type == "comment":
            if current_group:
                groups.append((current_group[0].start_byte, current_group[-1].end_byte))
                current_group = []
            continue
            
        # Any other statement: implementation to be elided
        current_group.append(child)
        
    if current_group:
        groups.append((current_group[0].start_byte, current_group[-1].end_byte))
        
    return groups

def find_function_blocks(node: tree_sitter.Node, blocks: list[tree_sitter.Node]):
    """Recursively traverses the AST to locate function bodies to elide."""
    if node.type == "function_definition":
        body = node.child_by_field_name("body")
        if body:
            blocks.append(body)
        return
        
    if node.type == "class_definition":
        body = node.child_by_field_name("body")
        if body:
            for child in body.children:
                find_function_blocks(child, blocks)
        return
        
    for child in node.children:
        find_function_blocks(child, blocks)

def elide_python_source(source_code: bytes) -> bytes:
    """Applies the elision pipeline to python source code bytes."""
    parser = get_parser()
    tree = parser.parse(source_code)
    
    blocks_to_elide = []
    find_function_blocks(tree.root_node, blocks_to_elide)
    
    replacements = []
    for block in blocks_to_elide:
        groups = get_elision_groups(block)
        for start, end in groups:
            replacements.append((start, end, b"..."))
            
    replacements.sort(key=lambda x: x[0], reverse=True)
    
    result = bytearray(source_code)
    for start, end, new_text in replacements:
        result[start:end] = new_text
        
    return bytes(result)

def find_brace_blocks(source: bytes) -> list[tuple[int, int, str]]:
    """
    Returns a list of (start_index, end_index, type) of brace blocks `{ ... }`.
    type is 'container' or 'function'.
    """
    blocks = []
    in_string = None
    in_single_comment = False
    in_multi_comment = False
    stack = []
    last_delimiter_index = 0
    i = 0
    n = len(source)
    
    while i < n:
        c = source[i:i+1]
        
        if in_string:
            if c == in_string and source[i-1:i] != b'\\':
                in_string = None
            i += 1
            continue
        
        if in_single_comment:
            if c == b'\n':
                in_single_comment = False
                last_delimiter_index = i
            i += 1
            continue
            
        if in_multi_comment:
            if c == b'/' and source[i-1:i] == b'*':
                in_multi_comment = False
                last_delimiter_index = i + 1
            i += 1
            continue
            
        if c == b'/' and i + 1 < n and source[i+1:i+2] == b'/':
            in_single_comment = True
            i += 2
            continue
        if c == b'/' and i + 1 < n and source[i+1:i+2] == b'*':
            in_multi_comment = True
            i += 2
            continue
            
        if c in (b'"', b"'", b'`'):
            in_string = c
            i += 1
            continue
            
        if c == b'{':
            prefix_bytes = source[last_delimiter_index:i]
            prefix_text = prefix_bytes.decode('utf-8', errors='ignore').strip()
            
            is_container = False
            container_keywords = ["class", "struct", "impl", "interface", "trait", "namespace", "union", "enum"]
            for kw in container_keywords:
                if re.search(r'\b' + kw + r'\b', prefix_text):
                    is_container = True
                    break
            
            block_type = 'container' if is_container else 'function'
            stack.append((i, block_type))
            last_delimiter_index = i + 1
        elif c == b'}':
            if stack:
                start_idx, b_type = stack.pop()
                blocks.append((start_idx, i, b_type))
            last_delimiter_index = i + 1
        elif c in (b';', b'\n'):
            if not stack:
                last_delimiter_index = i + 1
                
        i += 1
        
    return blocks

def elide_brace_source(source_code: bytes) -> bytes:
    """Elides block contents of outermost function brace blocks to '...'."""
    blocks = find_brace_blocks(source_code)
    outer_function_blocks = []
    for start, end, b_type in blocks:
        if b_type == 'function':
            is_nested = False
            for other_start, other_end, other_type in blocks:
                if other_type == 'function' and other_start < start and other_end > end:
                    is_nested = True
                    break
            if not is_nested:
                outer_function_blocks.append((start, end))
                
    outer_function_blocks.sort(key=lambda x: x[0], reverse=True)
    result = bytearray(source_code)
    for start, end in outer_function_blocks:
        result[start + 1 : end] = b"..."
    return bytes(result)

def elide_ruby_source(source_code: bytes) -> bytes:
    """Elides method implementations in Ruby using def-end tracking."""
    lines = source_code.split(b"\n")
    result_lines = []
    in_def = False
    def_depth = 0
    
    re_def = re.compile(r'^(\s*)def\b')
    re_start = re.compile(r'^\s*(class|module|def|begin|case)\b|(?:\bdo\b\s*(?:\|[^|]*\|)?\s*)$|^\s*(if|unless|while|until)\b')
    re_end = re.compile(r'^\s*end\b')
    
    for line in lines:
        line_str = line.decode('utf-8', errors='ignore')
        
        if not in_def:
            match = re_def.match(line_str)
            if match:
                in_def = True
                def_depth = 1
                result_lines.append(line + b" ...")
                continue
            else:
                result_lines.append(line)
        else:
            if re_start.search(line_str):
                def_depth += 1
            elif re_end.search(line_str):
                def_depth -= 1
                
            if def_depth == 0:
                in_def = False
                result_lines.append(line)
            else:
                pass
                
    return b"\n".join(result_lines)

def elide_source(source_code: bytes, file_path: str = "") -> bytes:
    """Applies the elision pipeline to source code bytes based on file type."""
    if not file_path:
        return elide_python_source(source_code)
        
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".py":
        return elide_python_source(source_code)
    elif ext == ".rb":
        return elide_ruby_source(source_code)
    elif ext in (".rs", ".go", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".hpp", ".php"):
        return elide_brace_source(source_code)
    else:
        return elide_brace_source(source_code)
