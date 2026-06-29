import os
import sys
import json
import shutil
import unittest

# Ensure the src folder is in Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../src")))

from recon.server import (
    generate_repo_blueprint,
    find_symbol_references,
    get_node_dependencies,
    hydrate_node_body,
    mutate_node_body
)

class TestReconServer(unittest.TestCase):
    def setUp(self):
        self.repo_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "dummy_repo"))
        self.metrics_file = ".mcp_token_metrics.json"
        
        # Backup dummy repo files to restore after mutation tests
        self.backup_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "dummy_repo_backup"))
        if os.path.exists(self.backup_dir):
            shutil.rmtree(self.backup_dir)
        shutil.copytree(self.repo_path, self.backup_dir)

    def tearDown(self):
        # Restore dummy repo files from backup
        if os.path.exists(self.backup_dir):
            shutil.rmtree(self.repo_path)
            shutil.copytree(self.backup_dir, self.repo_path)
            shutil.rmtree(self.backup_dir)
            
        # Clean up metrics file if it exists
        if os.path.exists(self.metrics_file):
            os.remove(self.metrics_file)

    def test_tier1_blueprint(self):
        print("\n--- Running Tier 1: Orientation Test ---")
        blueprint = generate_repo_blueprint(self.repo_path)
        print("Generated Blueprint snippet:")
        print("\n".join(blueprint.splitlines()[:30]))
        
        # Verify skeleton features
        self.assertIn("class Calculator", blueprint)
        self.assertIn("def add(self, x: int, y: int) -> int:", blueprint)
        self.assertIn("def calculate_average(x: int, y: int) -> float:", blueprint)
        
        # Check that logic is elided but docstrings/comments remain
        self.assertIn("# Add logic", blueprint)
        self.assertIn("...", blueprint)
        self.assertNotIn("result = x + y", blueprint)  # This should be stripped!
        self.assertNotIn("return x - y", blueprint)  # This should be stripped!

        # Verify Flow-DAG output exists and is alphabetically ordered
        self.assertIn("Flow-DAG (Directed Call Graph)", blueprint)
        self.assertIn("`module_b.calculate_average` -> `module_a.Calculator.add`", blueprint)
        self.assertIn("`module_b.main` -> `module_b.calculate_average`", blueprint)

    def test_tier2_graph_queries(self):
        print("\n--- Running Tier 2: Exploration Test ---")
        # Trigger indexing first
        generate_repo_blueprint(self.repo_path)
        
        # Find symbol references
        refs = find_symbol_references("Calculator")
        print("Calculator References:")
        print(refs)
        self.assertIn("module_a.py", refs)
        self.assertIn("module_b.py", refs)
        self.assertIn("calc = Calculator()", refs)
        
        # Get node dependencies
        module_b_path = os.path.join(self.repo_path, "module_b.py")
        deps = get_node_dependencies(module_b_path, "calculate_average")
        print("Dependencies of calculate_average:")
        print(deps)
        self.assertIn("Upstream Callers", deps)
        self.assertIn("module_b.main", deps)
        self.assertIn("Downstream Callees", deps)
        self.assertIn("module_a.Calculator.add", deps)

    def test_tier3_mutation(self):
        print("\n--- Running Tier 3: Mutation Test ---")
        # Trigger indexing
        generate_repo_blueprint(self.repo_path)
        
        module_a_path = os.path.join(self.repo_path, "module_a.py")
        
        # Hydrate node body
        body = hydrate_node_body(module_a_path, "Calculator.add")
        print("Hydrated body before mutation:")
        print(repr(body))
        self.assertIn("result = x + y", body)
        
        # Mutate node body with valid code
        new_body = """
        # Custom logging added
        print("Adding values:", x, "and", y)
        result = x + y + 10
        return result
        """
        mutation_result = mutate_node_body(module_a_path, "Calculator.add", new_body)
        print("Mutation Result:", mutation_result)
        self.assertIn("successful", mutation_result.lower())
        
        # Verify content on disk changed and is formatted correctly
        with open(module_a_path, "r") as f:
            updated_content = f.read()
        print("Updated module_a.py:")
        print(updated_content)
        self.assertIn("print(\"Adding values:\", x, \"and\", y)", updated_content)
        self.assertIn("result = x + y + 10", updated_content)
        # Verify class method indentation (should be 8 spaces base)
        self.assertIn("        # Custom logging added", updated_content)
        self.assertIn("        result = x + y + 10", updated_content)

        # Mutate with invalid code (Syntax Error) and verify rejection
        invalid_body = """
        def invalid syntax:
            unclosed brace {
        """
        mutation_result_invalid = mutate_node_body(module_a_path, "Calculator.add", invalid_body)
        print("Invalid Mutation Result:", mutation_result_invalid)
        self.assertIn("failed", mutation_result_invalid.lower())

    def test_middleware_token_metrics(self):
        print("\n--- Running Token Metrics Middleware Test ---")
        # Run a few tools
        generate_repo_blueprint(self.repo_path)
        find_symbol_references("Calculator")
        
        # Verify metrics file is created
        self.assertTrue(os.path.exists(self.metrics_file))
        with open(self.metrics_file, "r") as f:
            metrics = json.load(f)
            
        print("Token Metrics Log:")
        print(json.dumps(metrics, indent=2))
        
        self.assertIn("total_input_tokens", metrics)
        self.assertIn("total_output_tokens", metrics)
        self.assertGreater(metrics["total_input_tokens"], 0)
        self.assertGreater(metrics["total_output_tokens"], 0)
        self.assertEqual(len(metrics["calls"]), 2)
        self.assertEqual(metrics["calls"][0]["tool"], "generate_repo_blueprint")
        self.assertEqual(metrics["calls"][1]["tool"], "find_symbol_references")

        # Test report generation tool
        from recon.server import get_token_metrics_report
        report = get_token_metrics_report()
        print("Generated Token Report:")
        print(report)
        self.assertIn("Recon Token Consumption Report", report)
        self.assertIn("generate_repo_blueprint", report)
        self.assertIn("find_symbol_references", report)

    def test_run_comparative_benchmark(self):
        print("\n--- Running Comparative Benchmark Test ---")
        from recon.server import run_comparative_benchmark
        report = run_comparative_benchmark(
            self.repo_path,
            "deepseek/deepseek-chat",
            "Add logging metrics into Calculator.add method"
        )
        print("Comparative Benchmark Report:")
        print(report)
        self.assertIn("Comparative Evaluation Report: Recon vs. Baseline", report)
        self.assertIn("With Recon (3-Tier)", report)
        self.assertIn("Without Recon (Baseline)", report)
        self.assertIn("Simulation Mode Active", report)

if __name__ == "__main__":
    unittest.main()
