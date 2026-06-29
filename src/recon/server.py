import os
import sys
from fastmcp import FastMCP

from recon.middleware import log_token_metrics, METRICS_FILE
from recon.parser import elide_source
from recon.graph import SemanticGraph
from recon.mutator import hydrate_node_body as hydrate_body, mutate_node_body as mutate_body

# Initialize FastMCP Server
mcp = FastMCP("recon")

# Initialize global Semantic Graph
semantic_graph = SemanticGraph()

@mcp.tool()
@log_token_metrics("generate_repo_blueprint")
def generate_repo_blueprint(repo_path: str) -> str:
    """
    Generates a dense, markdown-formatted structural blueprint of the repository.
    Includes elided source code skeletons and a Flow-DAG module relationship graph.
    """
    repo_path = os.path.abspath(repo_path)
    if not os.path.exists(repo_path):
        return f"Error: Repository path '{repo_path}' does not exist."
    
    # Store the repository path for future mutations to trigger re-indexing
    semantic_graph.indexed_repo_path = repo_path
    
    # Index the workspace
    semantic_graph.index_repository(repo_path)
    
    # Fetch all indexed files in deterministic sorted order
    cursor = semantic_graph.conn.cursor()
    cursor.execute("SELECT path FROM files ORDER BY path ASC")
    files = [row[0] for row in cursor.fetchall()]
    
    blueprint = [
        "# Repository Blueprint",
        "",
        "## Structural Skeletons",
        "*(Implementation details elided to save context space)*",
        ""
    ]
    
    for file_path in files:
        rel_path = os.path.relpath(file_path, repo_path)
        try:
            with open(file_path, "rb") as f:
                content = f.read()
            elided = elide_source(content).decode("utf8", errors="ignore")
            
            blueprint.append(f"### File: `{rel_path}`")
            blueprint.append("```python")
            blueprint.append(elided)
            blueprint.append("```")
            blueprint.append("")
        except Exception as e:
            blueprint.append(f"### File: `{rel_path}` - Error parsing: {e}")
            blueprint.append("")
            
    blueprint.append("## Flow-DAG (Directed Call Graph)")
    blueprint.append("")
    dag_repr = semantic_graph.get_flow_dag()
    blueprint.append(dag_repr)
    
    return "\n".join(blueprint)

@mcp.tool()
@log_token_metrics("find_symbol_references")
def find_symbol_references(symbol_name: str) -> str:
    """
    Surgically queries the indexed semantic graph to return a list of files
    and line references where a target class or function symbol is invoked.
    """
    return semantic_graph.find_symbol_references(symbol_name)

@mcp.tool()
@log_token_metrics("get_node_dependencies")
def get_node_dependencies(file_path: str, function_name: str) -> str:
    """
    Returns the immediate upstream callers and downstream callees for a given 
    function or method node in the Flow-DAG.
    """
    file_path = os.path.abspath(file_path)
    return semantic_graph.get_node_dependencies(file_path, function_name)

@mcp.tool()
@log_token_metrics("hydrate_node_body")
def hydrate_node_body(file_path: str, target_entity: str) -> str:
    """
    Retrieves the full implementation body text of a single targeted AST node 
    (e.g., 'MyClass.process_payment' or 'my_standalone_function') inside file_path.
    """
    file_path = os.path.abspath(file_path)
    try:
        return hydrate_body(file_path, target_entity)
    except Exception as e:
        return f"Error: {str(e)}"

@mcp.tool()
@log_token_metrics("mutate_node_body")
def mutate_node_body(file_path: str, target_entity: str, new_body_code: str) -> str:
    """
    Replaces the inner implementation block of a single targeted AST node 
    in file_path, aligning indentation and validating compilation before committing.
    """
    file_path = os.path.abspath(file_path)
    try:
        result = mutate_body(file_path, target_entity, new_body_code)
        
        # Trigger re-indexing of repository to update dependencies and references
        if "successful" in result.lower() and hasattr(semantic_graph, "indexed_repo_path"):
            semantic_graph.index_repository(semantic_graph.indexed_repo_path)
            
        return result
    except Exception as e:
        return f"Error: {str(e)}"

@mcp.tool()
@log_token_metrics("run_comparative_benchmark")
def run_comparative_benchmark(repo_path: str, model_name: str, task_description: str) -> str:
    """
    Executes a comparative benchmark on a target repository for a given task.
    Runs the task under two conditions: With Recon (AST-guided node patching) and
    Without Recon (Full-file reading/writing). Computes and compares token consumption
    and execution success rates.
    """
    import shutil
    import subprocess
    import urllib.request
    import json
    
    repo_path = os.path.abspath(repo_path)
    if not os.path.exists(repo_path):
        return f"Error: Repository path '{repo_path}' does not exist."

    # Parse and index target repository first
    semantic_graph.indexed_repo_path = repo_path
    semantic_graph.index_repository(repo_path)
    
    # 1. Helper for LLM Calling
    def call_llm(messages: list) -> tuple[str, int, int]:
        api_key = os.environ.get("OPENROUTER_API_KEY")
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://github.com/google-deepmind/antigravity",
            "X-Title": "Recon comparative benchmark"
        }
        
        if not api_key:
            api_key = os.environ.get("OPENAI_API_KEY")
            url = "https://api.openai.com/v1/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }
            
        if not api_key:
            api_key = os.environ.get("DEEPSEEK_API_KEY")
            url = "https://api.deepseek.com/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }

        # Simulation mode if no keys are set
        if not api_key:
            return "SIMULATION_RESPONSE", 0, 0

        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": 0.0
        }
        
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                res_data = json.loads(response.read().decode("utf-8"))
                content = res_data["choices"][0]["message"]["content"]
                usage = res_data.get("usage", {})
                in_t = usage.get("prompt_tokens", len(json.dumps(messages)) // 4)
                out_t = usage.get("completion_tokens", len(content) // 4)
                return content, in_t, out_t
        except Exception as e:
            raise RuntimeError(f"LLM API Call failed: {e}")

    # 2. Helper for executing tests in the target repository
    def run_repo_tests() -> tuple[bool, str]:
        # Try running pytest
        try:
            res = subprocess.run(
                [".venv/bin/pytest"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30
            )
            return res.returncode == 0, res.stdout + "\n" + res.stderr
        except Exception:
            pass
            
        # Try discovery via unittest
        try:
            res = subprocess.run(
                [".venv/bin/python", "-m", "unittest", "discover", "-s", "tests"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30
            )
            return res.returncode == 0, res.stdout + "\n" + res.stderr
        except Exception as e:
            return False, f"Failed to run test suite: {e}"

    # Extract JSON block
    def extract_json(text: str) -> dict:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end+1])
        raise ValueError(f"Could not extract JSON: {text}")

    # Extract markdown code blocks
    def extract_code(text: str) -> str:
        if "```python" in text:
            return text.split("```python")[1].split("```")[0].strip()
        elif "```" in text:
            return text.split("```")[1].split("```")[0].strip()
        return text.strip()

    # Determine if we are running in simulation
    is_simulation = not (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))

    # Backup the codebase files to restore afterwards
    backup_files = {}
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for file in files:
            if file.endswith(".py") and not file.startswith("test_") and "_test.py" not in file:
                full_p = os.path.join(root, file)
                try:
                    with open(full_p, "rb") as f:
                        backup_files[full_p] = f.read()
                except Exception:
                    pass

    # --- RUN WITH RECON ---
    recon_in_tokens = 0
    recon_out_tokens = 0
    recon_success = False
    recon_log = ""
    recon_mutated_entity = "N/A"
    recon_file = "N/A"

    if is_simulation:
        recon_in_tokens = 1500
        recon_out_tokens = 250
        recon_success = True
        recon_log = "All tests passed (Mocked test run)"
        recon_mutated_entity = "Calculator.add"
        recon_file = "module_a.py"
    else:
        try:
            # Step A: Generate repo blueprint
            blueprint = generate_repo_blueprint(repo_path)
            
            # Step B: Identify the target node to mutate
            messages = [
                {"role": "system", "content": "You are a software engineering agent acting on a codebase. Based on the repo blueprint, identify which single Python file path and method/function FQN (e.g. 'Calculator.add' or 'my_standalone_func') should be modified. Task: " + task_description},
                {"role": "user", "content": f"Repository Blueprint:\n{blueprint}\n\nSpecify the target in JSON. Return ONLY: {{\"file_path\": \"relative/path/to/file.py\", \"target_entity\": \"ClassName.method_name\"}}"}
            ]
            response, in_t, out_t = call_llm(messages)
            recon_in_tokens += in_t
            recon_out_tokens += out_t
            
            target_info = extract_json(response)
            rel_file_path = target_info["file_path"]
            target_entity = target_info["target_entity"]
            recon_file = rel_file_path
            recon_mutated_entity = target_entity
            
            # Step C: Hydrate and mutate
            abs_file_path = os.path.join(repo_path, rel_file_path)
            body = hydrate_body(abs_file_path, target_entity)
            
            messages = [
                {"role": "system", "content": f"You are modifying the body of '{target_entity}'. Code task: {task_description}"},
                {"role": "user", "content": f"Current implementation body of {target_entity}:\n```python\n{body}\n```\n\nReturn ONLY the new replacement code block for the body of this function. Do not write the function header/def statement."}
            ]
            response, in_t, out_t = call_llm(messages)
            recon_in_tokens += in_t
            recon_out_tokens += out_t
            
            new_body = extract_code(response)
            mutation_res = mutate_body(abs_file_path, target_entity, new_body)
            
            if "successful" in mutation_res.lower():
                recon_success, test_log = run_repo_tests()
                recon_log = test_log
            else:
                recon_success = False
                recon_log = f"AST Mutation Failed: {mutation_res}"
        except Exception as ex:
            recon_success = False
            recon_log = f"Recon comparative loop failed: {ex}"

    # Restore codebase from backup
    for p, content in backup_files.items():
        with open(p, "wb") as f:
            f.write(content)

    # --- RUN WITHOUT RECON (BASELINE) ---
    baseline_in_tokens = 0
    baseline_out_tokens = 0
    baseline_success = False
    baseline_log = ""
    baseline_file = "N/A"

    if is_simulation:
        baseline_in_tokens = 12000
        baseline_out_tokens = 1500
        baseline_success = True
        baseline_log = "All tests passed (Mocked test run)"
        baseline_file = "module_a.py"
    else:
        try:
            # Read full codebase contents (simulating standard context feeding)
            full_context = ""
            for p, content in backup_files.items():
                rel_p = os.path.relpath(p, repo_path)
                full_context += f"### File: {rel_p}\n```python\n{content.decode('utf8', errors='ignore')}\n```\n\n"
            
            # Step A: Request modification of full file
            messages = [
                {"role": "system", "content": "You are a software engineering agent acting on a codebase. You must modify the code to satisfy the task. Task: " + task_description},
                {"role": "user", "content": f"Here is the full repository code:\n{full_context}\n\nImplement the changes. Specify which relative file path you modified, and return the ENTIRE updated content of that file inside a python code block."}
            ]
            response, in_t, out_t = call_llm(messages)
            baseline_in_tokens += in_t
            baseline_out_tokens += out_t
            
            # Extract target file and new content
            # Try to locate path name in LLM output
            target_rel_path = None
            for p in backup_files.keys():
                rel_p = os.path.relpath(p, repo_path)
                if rel_p in response:
                    target_rel_path = rel_p
                    break
            
            if not target_rel_path:
                # Fallback to first python file
                target_rel_path = os.path.relpath(list(backup_files.keys())[0], repo_path)
                
            baseline_file = target_rel_path
            new_file_content = extract_code(response)
            
            # Overwrite file
            abs_target_path = os.path.join(repo_path, target_rel_path)
            with open(abs_target_path, "w") as f:
                f.write(new_file_content)
                
            # Compile check and test execution
            compile(new_file_content, abs_target_path, "exec")
            baseline_success, test_log = run_repo_tests()
            baseline_log = test_log
        except Exception as ex:
            baseline_success = False
            baseline_log = f"Baseline comparative loop failed: {ex}"

    # Restore codebase back to original state
    for p, content in backup_files.items():
        with open(p, "wb") as f:
            f.write(content)

    # 3. Format Comparative Report
    report = [
        "# Comparative Evaluation Report: Recon vs. Baseline",
        f"**Model Evaluated**: `{model_name}`",
        f"**Task Description**: *\"{task_description}\"*",
        f"**Target Repository**: `{repo_path}`",
        "",
        "| Evaluation Metric | With Recon (3-Tier) | Without Recon (Baseline) | Savings / Gain |",
        "| :--- | :--- | :--- | :--- |"
    ]
    
    total_recon = recon_in_tokens + recon_out_tokens
    total_baseline = baseline_in_tokens + baseline_out_tokens
    
    in_savings = f"{(1 - recon_in_tokens / max(1, baseline_in_tokens)) * 100:.1f}%" if baseline_in_tokens else "0%"
    out_savings = f"{(1 - recon_out_tokens / max(1, baseline_out_tokens)) * 100:.1f}%" if baseline_out_tokens else "0%"
    total_savings = f"{(1 - total_recon / max(1, total_baseline)) * 100:.1f}%" if total_baseline else "0%"
    
    report.append(f"| Input Tokens | {recon_in_tokens:,} | {baseline_in_tokens:,} | **{in_savings} savings** |")
    report.append(f"| Output Tokens | {recon_out_tokens:,} | {baseline_out_tokens:,} | **{out_savings} savings** |")
    report.append(f"| Total Tokens | {total_recon:,} | {total_baseline:,} | **{total_savings} savings** |")
    report.append(f"| Test Compilation & Run | {'✅ Passed' if recon_success else '❌ Failed'} | {'✅ Passed' if baseline_success else '❌ Failed'} | - |")
    report.append(f"| Mutated Entity | `{recon_mutated_entity}` in `{recon_file}` | `{baseline_file}` (full file overwrite) | - |")
    report.append("")
    
    if is_simulation:
        report.append("> [!NOTE]")
        report.append("> **Simulation Mode Active**: No API keys (OPENROUTER_API_KEY, OPENAI_API_KEY, or DEEPSEEK_API_KEY) were found in the environment. The metrics above represent a standard simulated profile for Python refactoring/mutation runs.")
        report.append("")
        
    report.append("## Detailed Logs")
    report.append("")
    report.append("### Recon Test Run Log:")
    report.append("```")
    report.append(recon_log.strip())
    report.append("```")
    report.append("")
    report.append("### Baseline Test Run Log:")
    report.append("```")
    report.append(baseline_log.strip())
    report.append("```")
    
    return "\n".join(report)

@mcp.tool()
def get_token_metrics_report() -> str:
    """
    Returns a formatted markdown report of the token consumption metrics
    and session summaries from all tool invocations recorded in .mcp_token_metrics.json.
    """
    import json
    if not os.path.exists(METRICS_FILE):
        return "No token metrics recorded yet. Run some tool calls first."
        
    try:
        with open(METRICS_FILE, "r") as f:
            data = json.load(f)
    except Exception as e:
        return f"Error reading token metrics file: {str(e)}"
        
    total_in = data.get("total_input_tokens", 0)
    total_out = data.get("total_output_tokens", 0)
    calls = data.get("calls", [])
    
    report = [
        "# Recon Token Consumption Report",
        "",
        f"- **Total Input Tokens**: {total_in:,}",
        f"- **Total Output Tokens**: {total_out:,}",
        f"- **Total Tool Invocations**: {len(calls)}",
        "",
        "| Time | Tool Invoked | Input Tokens | Output Tokens | Duration (s) |",
        "| :--- | :--- | :--- | :--- | :--- |"
    ]
    
    for call in calls:
        # Format time to HH:MM:SS
        timestamp = call.get("timestamp", "")
        time_part = timestamp.split("T")[-1][:8] if "T" in timestamp else "N/A"
        
        tool = call.get("tool", "unknown")
        in_t = call.get("input_tokens", 0)
        out_t = call.get("output_tokens", 0)
        dur = call.get("duration_seconds", 0.0)
        report.append(f"| {time_part} | `{tool}` | {in_t:,} | {out_t:,} | {dur:.3f} |")
        
    return "\n".join(report)

if __name__ == "__main__":
    mcp.run()
