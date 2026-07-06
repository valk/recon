import os
import sys

# Ensure project src directory is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from recon.server import compress_context_llmlingua

def main():
    repo_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../tests/dummy_repo"))
    print(f"[*] Ingesting files from dummy repo: {repo_path}")
    
    full_context = ""
    for root, _, files in os.walk(repo_path):
        for file in files:
            if file.endswith(".py"):
                p = os.path.join(root, file)
                rel_p = os.path.relpath(p, repo_path)
                with open(p, "r", encoding="utf8") as f:
                    content = f.read()
                full_context += f"### File: {rel_p}\n```python\n{content}\n```\n\n"
                
    print("\n--- ORIGINAL CONTEXT ---")
    print(full_context)
    
    approx_original_tokens = len(full_context) // 4
    target_tokens = int(approx_original_tokens * 0.6) # 40% reduction
    print(f"Original Approx Tokens: {approx_original_tokens}")
    print(f"Target Compressed Tokens: {target_tokens}")
    print("\n[*] Initializing PromptCompressor and running compression (this downloads the model on first run)...")
    
    compressed = compress_context_llmlingua(full_context, target_token=target_tokens)
    
    print("\n--- COMPRESSED CONTEXT ---")
    print(compressed)
    
    approx_compressed_tokens = len(compressed) // 4
    print(f"Compressed Approx Tokens: {approx_compressed_tokens}")
    print(f"Actual Reduction: {100.0 * (1 - (approx_compressed_tokens / approx_original_tokens)):.1f}%")

if __name__ == "__main__":
    main()
