import os
import sys

# Ensure project src directory is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from recon.server import (
    generate_repo_blueprint,
    find_symbol_references,
    hydrate_node_body,
)

def main():
    repo_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../tests/dummy_repo"))
    file_path = os.path.join(repo_path, "module_a.py")
    
    print("==========================================")
    print("=== TIER 1 PRODUCT: ORIENTATION (AST) ===")
    print("==========================================")
    blueprint = generate_repo_blueprint(repo_path)
    print(blueprint)
    print("\n" + "="*50)

    print("\n==========================================")
    print("=== TIER 2 PRODUCT: EXPLORATION (REFS) ===")
    print("==========================================")
    references = find_symbol_references("Calculator")
    print(references)
    print("\n" + "="*50)

    print("\n==========================================")
    print("=== TIER 3 PRODUCT: MUTATION (BODY)   ===")
    print("==========================================")
    body = hydrate_node_body(file_path, "Calculator.add")
    print(f"Isolated Target Body (Calculator.add):\n{repr(body)}")
    print("\n" + "="*50)

if __name__ == "__main__":
    main()
