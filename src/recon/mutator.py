import os
import textwrap
import tree_sitter
from recon.parser import get_parser

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
            # Build entity identifier
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
    # Try to get indentation of first statement in the body block
    named_children = [c for c in body_node.children if c.is_named]
    if named_children:
        first_child = named_children[0]
        start_byte = first_child.start_byte
        # Read backward to start of line to find whitespace
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
    
    # Fallback: get function definition's indentation and add 4 spaces
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

def hydrate_node_body(file_path: str, target_entity: str) -> str:
    """Locates target_entity inside file_path and returns its body implementation text."""
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, "rb") as f:
        content = f.read()

    parser = get_parser()
    tree = parser.parse(content)
    
    func_node = find_target_node(tree.root_node, target_entity)
    if not func_node:
        raise ValueError(f"Entity '{target_entity}' not found in file '{file_path}'")

    body_node = func_node.child_by_field_name("body")
    if not body_node:
        return ""

    return content[body_node.start_byte : body_node.end_byte].decode("utf8")

def mutate_node_body(file_path: str, target_entity: str, new_body_code: str) -> str:
    """
    Replaces the inner implementation block of target_entity in file_path.
    Aligns indentation, verifies compilation, and writes to disk.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    with open(file_path, "rb") as f:
        content = f.read()

    parser = get_parser()
    tree = parser.parse(content)

    func_node = find_target_node(tree.root_node, target_entity)
    if not func_node:
        raise ValueError(f"Entity '{target_entity}' not found in file '{file_path}'")

    body_node = func_node.child_by_field_name("body")
    if not body_node:
        raise ValueError(f"Entity '{target_entity}' has no body block node")

    # Get baseline indentation
    base_indent = get_base_indentation(content, body_node, func_node)

    # Normalize new body using dedent, then indent every non-empty line
    normalized_body = textwrap.dedent(new_body_code)
    lines = normalized_body.splitlines()
    indented_lines = []
    
    # If the first child statement is a docstring, we might want to check
    # if it was already included or if we just format the lines.
    # We will format all lines to have the target base indentation.
    for line in lines:
        if line.strip():
            indented_lines.append(base_indent + line)
        else:
            indented_lines.append("")
            
    indented_body = "\n".join(indented_lines)
    # Ensure there is a newline at the start of the block if it's block-indented
    if not indented_body.startswith("\n") and body_node.start_byte < body_node.end_byte:
        # Check if the original body started with a newline (standard block)
        orig_body_text = content[body_node.start_byte : body_node.end_byte]
        if orig_body_text.startswith(b"\n") or orig_body_text.startswith(b"\r\n"):
            # Prefix with a newline matching line endings
            newline = "\r\n" if b"\r\n" in orig_body_text else "\n"
            indented_body = newline + indented_body

    # Replace bytes
    new_content_bytes = (
        content[:body_node.start_byte]
        + indented_body.encode("utf8")
        + content[body_node.end_byte:]
    )

    # Validate compilation
    new_content_str = new_content_bytes.decode("utf8")
    try:
        compile(new_content_str, file_path, "exec")
    except SyntaxError as se:
        return f"Compilation Failed: Syntax Error in proposed mutation:\n{se.msg} (line {se.lineno}, col {se.offset})\nLine context: {se.text}"
    except Exception as e:
        return f"Compilation Failed: {str(e)}"

    # Write changes to disk
    with open(file_path, "wb") as f:
        f.write(new_content_bytes)

    return "Mutation successful and compiled successfully!"
