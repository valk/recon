#!/usr/bin/env python3
"""
Run Recon benchmarks from the command line with real-time progress output.

Usage examples:
  # Comparative benchmark (Recon vs Baseline on a single repo):
  uv run python scripts/run_benchmark.py comparative ./my-repo "Add bounds checks to Calculator.subtract" --model deepseek/deepseek-chat

  # Claw-SWE-Bench Lite-80 batch benchmark:
  uv run python scripts/run_benchmark.py lite-80 ./workspaces --model deepseek/deepseek-chat --limit 10
"""
import os
import sys
import argparse

# Add package source to Python path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))


def cmd_comparative(args):
    from recon.server import run_comparative_benchmark
    report = run_comparative_benchmark(
        repo_path=os.path.abspath(args.repo_path),
        task_description=args.task_description,
        model_name=args.model
    )
    print("\n" + "=" * 60 + "\n")
    print(report)


def cmd_lite80(args):
    from recon.server import run_claw_lite_benchmark
    report = run_claw_lite_benchmark(
        workspace_dir=os.path.abspath(args.workspace_dir),
        limit=args.limit,
        shuffle=args.shuffle,
        model_name=args.model,
        resume=args.resume
    )
    print("\n" + "=" * 60 + "\n")
    print(report)


class Tee:
    def __init__(self, original_stream, log_file):
        self.original = original_stream
        self.log_file = log_file

    def write(self, data):
        self.original.write(data)
        self.log_file.write(data)
        self.log_file.flush()

    def flush(self):
        self.original.flush()
        self.log_file.flush()


def main():
    parser = argparse.ArgumentParser(
        description="Run Recon benchmarks with real-time terminal progress.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- comparative sub-command ---
    p_comp = subparsers.add_parser(
        "comparative",
        help="Run Recon vs Baseline comparison on a single repository."
    )
    p_comp.add_argument("repo_path", help="Path to the target repository")
    p_comp.add_argument("task_description", help="Task the LLM should perform (e.g. 'Add bounds checks to Calculator.subtract')")
    p_comp.add_argument("--model", default="", help="LLM model name (e.g. deepseek/deepseek-chat). Falls back to RECON_MODEL env var.")

    # --- lite-80 sub-command ---
    p_lite = subparsers.add_parser(
        "lite-80",
        help="Run Claw-SWE-Bench Lite-80 batch benchmark."
    )
    p_lite.add_argument("workspace_dir", help="Directory to clone benchmark repos into")
    p_lite.add_argument("--limit", type=int, default=80, help="Number of instances to evaluate (default: 80)")
    p_lite.add_argument("--shuffle", action="store_true", help="Shuffle dataset items to run in random order")
    p_lite.add_argument("--model", default="", help="LLM model name (e.g. deepseek/deepseek-chat). Falls back to RECON_MODEL env var.")
    p_lite.add_argument("--resume", action="store_true", help="Resume benchmark execution from checkpoint or latest log file.")

    args = parser.parse_args()

    # Set up logging to logs/ directory
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    logs_dir = os.path.join(project_root, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_filename = f"{args.command}_{timestamp}.log"
    log_file_path = os.path.join(logs_dir, log_filename)

    print(f"[*] Logging console output to: logs/{log_filename}\n")

    with open(log_file_path, "w", encoding="utf-8") as log_file:
        sys.stdout = Tee(sys.stdout, log_file)
        sys.stderr = Tee(sys.stderr, log_file)
        try:
            if args.command == "comparative":
                cmd_comparative(args)
            elif args.command == "lite-80":
                cmd_lite80(args)
        finally:
            sys.stdout = sys.stdout.original
            sys.stderr = sys.stderr.original


if __name__ == "__main__":
    main()
