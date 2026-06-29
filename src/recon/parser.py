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
        # We do not recurse into nested functions/classes inside the body
        return
        
    if node.type == "class_definition":
        body = node.child_by_field_name("body")
        if body:
            # We recurse into class body to find methods
            for child in body.children:
                find_function_blocks(child, blocks)
        return
        
    for child in node.children:
        find_function_blocks(child, blocks)

def elide_source(source_code: bytes) -> bytes:
    """
    Applies the elision pipeline to python source code bytes.
    Replaces implementation blocks of all functions/methods with '...'.
    """
    parser = get_parser()
    tree = parser.parse(source_code)
    
    blocks_to_elide = []
    find_function_blocks(tree.root_node, blocks_to_elide)
    
    # Collect all individual text replacements
    replacements = []
    for block in blocks_to_elide:
        groups = get_elision_groups(block)
        for start, end in groups:
            replacements.append((start, end, b"..."))
            
    # Sort replacements in reverse order of start byte to apply them from end to start
    replacements.sort(key=lambda x: x[0], reverse=True)
    
    # Apply replacements
    result = bytearray(source_code)
    for start, end, new_text in replacements:
        result[start:end] = new_text
        
    return bytes(result)
