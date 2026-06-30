import sys
import os
from datasets import load_dataset

def main():
    try:
        dataset = load_dataset("TokenRhythm/Claw-SWE-Bench", "lite", split="test")
        print(f"Loaded dataset with {len(dataset)} items.")
        
        repos = {}
        for item in dataset:
            repo = item.get("repo", "unknown")
            repos[repo] = repos.get(repo, 0) + 1
            
        print("\nRepositories and counts:")
        for repo, count in sorted(repos.items(), key=lambda x: x[1], reverse=True):
            print(f"  {repo}: {count}")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
