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
        # Resolve metrics file relative to the package root, matching middleware logic
        package_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        self.metrics_file = os.path.join(package_root, ".mcp_token_metrics.json")
        
        # Ensure clean state by removing any existing metrics file at start
        if os.path.exists(self.metrics_file):
            os.remove(self.metrics_file)
        
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
        # Temporarily clear LLM API keys to force simulation mode during tests
        old_keys = {}
        for key in ["OPENROUTER_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"]:
            if key in os.environ:
                old_keys[key] = os.environ[key]
                del os.environ[key]
                
        try:
            from recon.server import run_comparative_benchmark
            report = run_comparative_benchmark(
                repo_path=self.repo_path,
                task_description="Add logging metrics into Calculator.add method",
                model_name="deepseek/deepseek-chat"
            )
            print("Comparative Benchmark Report:")
            print(report)
            self.assertIn("Comparative Evaluation Report: Recon vs. Baseline", report)
            self.assertIn("With Recon (3-Tier)", report)
            self.assertIn("Without Recon (Baseline)", report)
            self.assertIn("Simulation Mode Active", report)
        finally:
            # Restore original environment keys
            for key, val in old_keys.items():
                os.environ[key] = val

    def test_run_claw_lite_benchmark(self):
        print("\n--- Running Claw-SWE-Bench Lite-80 Test ---")
        # Ensure simulation mode is forced by temporarily clearing API keys
        old_keys = {}
        for key in ["OPENROUTER_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"]:
            if key in os.environ:
                old_keys[key] = os.environ[key]
                del os.environ[key]
                
        try:
            from recon.server import run_claw_lite_benchmark
            report = run_claw_lite_benchmark(
                workspace_dir=self.repo_path,
                limit=5,
                model_name="deepseek/deepseek-chat"
            )
            print("Claw Lite Benchmark Report (Simulation):")
            print(report)
            self.assertIn("Claw-SWE-Bench Lite-80 Benchmark Summary", report)
            self.assertIn("Average Token Metrics", report)
            self.assertIn("Results Functional Consistency Validation", report)
            self.assertIn("Simulation Mode Active", report)
        finally:
            # Restore original environment keys
            for key, val in old_keys.items():
                os.environ[key] = val

    def test_run_claw_lite_benchmark_shuffle(self):
        print("\n--- Running Claw-SWE-Bench Shuffle Test ---")
        import io
        import contextlib
        
        # Ensure simulation mode is forced by temporarily clearing API keys
        old_keys = {}
        for key in ["OPENROUTER_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"]:
            if key in os.environ:
                old_keys[key] = os.environ[key]
                del os.environ[key]
                
        try:
            from recon.server import run_claw_lite_benchmark
            
            # Run without shuffle
            f_no_shuffle = io.StringIO()
            with contextlib.redirect_stderr(f_no_shuffle):
                run_claw_lite_benchmark(
                    workspace_dir=self.repo_path,
                    limit=20,
                    shuffle=False,
                    model_name="deepseek/deepseek-chat"
                )
            
            # Run with shuffle
            f_shuffle = io.StringIO()
            with contextlib.redirect_stderr(f_shuffle):
                run_claw_lite_benchmark(
                    workspace_dir=self.repo_path,
                    limit=20,
                    shuffle=True,
                    model_name="deepseek/deepseek-chat"
                )
                
            # Parse instances from stderr
            import re
            order_no_shuffle = re.findall(r'Claw-Lite-\d+', f_no_shuffle.getvalue())
            order_shuffle = re.findall(r'Claw-Lite-\d+', f_shuffle.getvalue())
            
            print("No shuffle order:", order_no_shuffle[:10])
            print("Shuffle order   :", order_shuffle[:10])
            
            # Without shuffle, it should be in standard ascending order (Claw-Lite-01 to Claw-Lite-20)
            expected_no_shuffle = [f"Claw-Lite-{i:02d}" for i in range(1, 21)]
            self.assertEqual(order_no_shuffle, expected_no_shuffle)
            
            # With shuffle, the order/subset should be different
            self.assertNotEqual(order_shuffle, expected_no_shuffle)
            self.assertEqual(len(order_shuffle), 20)
            
            # All items should belong to the total set of 80 simulated instances
            all_possible = {f"Claw-Lite-{i:02d}" for i in range(1, 81)}
            self.assertTrue(set(order_shuffle).issubset(all_possible))
            
        finally:
            # Restore original environment keys
            for key, val in old_keys.items():
                os.environ[key] = val

    def test_non_python_languages(self):
        print("\n--- Running Multi-Language Test ---")
        
        # Test Elision
        rust_code = b"""struct MyStruct {
    value: i32,
}

impl MyStruct {
    fn calculate(&self, factor: i32) -> i32 {
        let res = self.value * factor;
        res
    }
}"""
        
        from recon.parser import elide_source
        elided_rust = elide_source(rust_code, "test.rs")
        print("Elided Rust:")
        print(elided_rust.decode('utf-8'))
        self.assertIn(b"fn calculate(&self, factor: i32) -> i32 {...}", elided_rust)
        
        ruby_code = b"""class MyClass
  def hello(name)
    puts "Hello #{name}"
  end
end"""
        elided_ruby = elide_source(ruby_code, "test.rb")
        print("Elided Ruby:")
        print(elided_ruby.decode('utf-8'))
        self.assertIn(b"def hello(name) ...", elided_ruby)

        # Write dummy files to test hydration/mutation
        dummy_rust_path = os.path.join(self.repo_path, "dummy_rust.rs")
        dummy_ruby_path = os.path.join(self.repo_path, "dummy_ruby.rb")
        
        try:
            with open(dummy_rust_path, "wb") as f:
                f.write(rust_code)
            with open(dummy_ruby_path, "wb") as f:
                f.write(ruby_code)
                
            # Test Hydration
            from recon.mutator import hydrate_node_body, mutate_node_body
            rust_body = hydrate_node_body(dummy_rust_path, "MyStruct.calculate")
            print("Hydrated Rust body:")
            print(repr(rust_body))
            self.assertIn("let res = self.value * factor;", rust_body)
            
            ruby_body = hydrate_node_body(dummy_ruby_path, "MyClass.hello")
            print("Hydrated Ruby body:")
            print(repr(ruby_body))
            self.assertIn("puts \"Hello #{name}\"", ruby_body)
            
            # Test Mutation
            new_rust_body = """
            let res = self.value * factor + 10;
            res
            """
            mut_res_rust = mutate_node_body(dummy_rust_path, "MyStruct.calculate", new_rust_body)
            print("Rust Mutation result:", mut_res_rust)
            self.assertIn("successful", mut_res_rust.lower())
            
            with open(dummy_rust_path, "r") as f:
                updated_rust = f.read()
            print("Updated Rust:")
            print(updated_rust)
            self.assertIn("let res = self.value * factor + 10;", updated_rust)
            
            new_ruby_body = """
            puts "Hello, hello #{name}!"
            """
            mut_res_ruby = mutate_node_body(dummy_ruby_path, "MyClass.hello", new_ruby_body)
            print("Ruby Mutation result:", mut_res_ruby)
            self.assertIn("successful", mut_res_ruby.lower())
            
            with open(dummy_ruby_path, "r") as f:
                updated_ruby = f.read()
            print("Updated Ruby:")
            print(updated_ruby)
            self.assertIn("puts \"Hello, hello #{name}!\"", updated_ruby)
            
            # Test Indexing
            from recon.graph import SemanticGraph
            sg = SemanticGraph()
            sg.index_repository(self.repo_path)
            
            cursor = sg.conn.cursor()
            cursor.execute("SELECT fqn, type FROM symbols WHERE file_path IN (?, ?)", (dummy_rust_path, dummy_ruby_path))
            symbols = cursor.fetchall()
            print("Indexed non-python symbols:")
            print(symbols)
            symbols_dict = dict(symbols)
            self.assertIn("dummy_rust.MyStruct", symbols_dict)
            self.assertIn("dummy_rust.MyStruct.calculate", symbols_dict)
            self.assertIn("dummy_ruby.MyClass", symbols_dict)
            self.assertIn("dummy_ruby.MyClass.hello", symbols_dict)
            
        finally:
            if os.path.exists(dummy_rust_path):
                os.remove(dummy_rust_path)
            if os.path.exists(dummy_ruby_path):
                os.remove(dummy_ruby_path)

if __name__ == "__main__":
    unittest.main()
