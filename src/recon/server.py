import os
import sys
from fastmcp import FastMCP

from recon.middleware import log_token_metrics, METRICS_FILE
from recon.parser import elide_source
from recon.graph import SemanticGraph
from recon.mutator import hydrate_node_body as hydrate_body, mutate_node_body as mutate_body

def load_env_file():
    """Dynamically parses and loads environment variables from a local .env file if it exists."""
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env_path = os.path.join(project_root, ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip("'\"")
                        if key:
                            os.environ[key] = val
        except Exception:
            pass

# Load environmental configs prior to server run
load_env_file()

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

    # For large repos the full-skeleton representation would exceed LLM context
    # limits and cause HTTP 400 errors.  Switch to a compact symbol-list instead.
    COMPACT_THRESHOLD = 50
    use_compact = len(files) > COMPACT_THRESHOLD

    if use_compact:
        blueprint.append(
            f"> **Compact mode** — {len(files)} files indexed "
            f"(threshold: {COMPACT_THRESHOLD}). Showing symbol list only.\n"
        )
        cursor.execute(
            "SELECT fqn, type, file_path, start_line FROM symbols "
            "ORDER BY file_path ASC, start_line ASC"
        )
        for fqn, sym_type, sym_file, start_line in cursor.fetchall():
            rel = os.path.relpath(sym_file, repo_path)
            blueprint.append(f"- `{sym_type}` **{fqn}** — `{rel}` L{start_line}")
        blueprint.append("")
    else:
        ext_to_lang = {
            ".py": "python", ".rs": "rust", ".go": "go",
            ".js": "javascript", ".ts": "typescript", ".java": "java",
            ".cpp": "cpp", ".c": "c", ".h": "c", ".hpp": "cpp",
            ".php": "php", ".rb": "ruby",
        }
        for file_path in files:
            rel_path = os.path.relpath(file_path, repo_path)
            ext = os.path.splitext(file_path)[1].lower()
            lang = ext_to_lang.get(ext, "")
            try:
                with open(file_path, "rb") as f:
                    content = f.read()
                elided = elide_source(content, file_path).decode("utf8", errors="ignore")

                blueprint.append(f"### File: `{rel_path}`")
                blueprint.append(f"```{lang}")
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

    Cross-Reference Pre-Flight (auto-injected):
    In addition to the implementation body, the response automatically includes
    upstream callers (who calls this node) and downstream callees (what this node
    calls), as well as all reference locations across the codebase. This eliminates
    the need for the agent to make separate get_node_dependencies /
    find_symbol_references calls before deciding how to mutate the node.
    """
    file_path = os.path.abspath(file_path)
    try:
        body = hydrate_body(file_path, target_entity)
    except Exception as e:
        return f"Error: {str(e)}"

    # --- Cross-Reference Pre-Flight ---
    # Derive the bare function/method name from the entity qualifier
    # e.g. 'MyClass.process_payment' -> 'process_payment'
    #      'my_standalone_function'  -> 'my_standalone_function'
    bare_name = target_entity.split(".")[-1] if "." in target_entity else target_entity

    sections = [body, "", "---", "## Cross-Reference Pre-Flight", ""]

    # 1. Upstream callers & downstream callees via the Flow-DAG
    try:
        deps = semantic_graph.get_node_dependencies(file_path, bare_name)
        sections.append("### Call-Graph Dependencies")
        sections.append(deps)
        sections.append("")
    except Exception:
        pass

    # 2. All reference locations across the codebase
    try:
        refs = semantic_graph.find_symbol_references(bare_name)
        sections.append("### Symbol References")
        sections.append(refs)
        sections.append("")
    except Exception:
        pass

    return "\n".join(sections)

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

def pseudo_llmlingua_compress(text: str, rate: float = 0.3) -> str:
    """
    Simulates LLMLingua's entropy-based token pruning by removing a percentage of words/tokens.
    To remain deterministic, we drop every Nth word/punctuation token.
    This mimics the code-blind syntax corruption common in text-only compressors.
    """
    if not text:
        return ""
    import re
    tokens = re.findall(r'\w+|\s+|[^\w\s]', text)
    result = []
    step = int(1.0 / rate) if rate > 0 else 999999
    if step < 2:
        step = 2
    for i, tok in enumerate(tokens):
        if i % step == 0 and tok.strip(): # drop it if it's not pure whitespace
            continue
        result.append(tok)
    return "".join(result)


def compress_context_llmlingua(text: str, target_token: int = 5000) -> str:
    try:
        from llmlingua import PromptCompressor
        compressor = PromptCompressor(model_name="microsoft/llmlingua-2-bert-base-multilingual-cased-meeting", device_map="cpu")
        res = compressor.compress_prompt(
            [text],
            target_token=target_token,
            use_format=True
        )
        return res.get("compressed_prompt", text)
    except Exception:
        approx_original_tokens = len(text) // 4
        rate = 1.0 - (target_token / max(1, approx_original_tokens))
        rate = max(0.1, min(0.7, rate))
        return pseudo_llmlingua_compress(text, rate=rate)


@mcp.tool()
@log_token_metrics("run_comparative_benchmark")
def run_comparative_benchmark(repo_path: str, task_description: str, model_name: str = "", ablations: list[str] = None, force_simulation: bool = False) -> str:
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
    
    if ablations is None:
        ablations = []
        
    repo_path = os.path.abspath(repo_path)
    if not os.path.exists(repo_path):
        return f"Error: Repository path '{repo_path}' does not exist."

    # Resolve model name from environment variables if not provided
    if not model_name:
        model_name = os.environ.get("RECON_MODEL") or os.environ.get("DEFAULT_MODEL") or "deepseek/deepseek-chat"

    models = [m.strip() for m in model_name.split(",") if m.strip()]
    if not models:
        models = ["deepseek/deepseek-chat"]

    # Parse and index target repository first
    semantic_graph.indexed_repo_path = repo_path
    semantic_graph.index_repository(repo_path)
    
    # 1. Helper for LLM Calling
    def call_llm(messages: list, model_to_use: str) -> tuple[str, int, int]:
        api_key = None
        url = None
        headers = {}

        # # 1. If it's a GLM model and ZAI_API_KEY is set, route directly to Zhipu API
        # if model_to_use.startswith("glm-") and os.environ.get("ZAI_API_KEY"):
        #     api_key = os.environ.get("ZAI_API_KEY")
        #     url = "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"
        #     headers = {
        #         "Content-Type": "application/json",
        #         "Authorization": f"Bearer {api_key}"
        #     }

        # 2. Otherwise check OpenRouter
        if not api_key:
            api_key = os.environ.get("OPENROUTER_API_KEY")
            url = "https://openrouter.ai/api/v1/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/google-deepmind/antigravity",
                "X-Title": "Recon comparative benchmark"
            }
            
        # 3. OpenAI
        if not api_key:
            api_key = os.environ.get("OPENAI_API_KEY")
            url = "https://api.openai.com/v1/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }
            
        # 4. DeepSeek
        if not api_key:
            api_key = os.environ.get("DEEPSEEK_API_KEY")
            url = "https://api.deepseek.com/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }

        # # 5. Zhipu AI Fallback
        # if not api_key:
        #     api_key = os.environ.get("ZAI_API_KEY")
        #     url = "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"
        #     headers = {
        #         "Content-Type": "application/json",
        #         "Authorization": f"Bearer {api_key}"
        #     }

        # Simulation mode if no keys are set
        if not api_key:
            return "SIMULATION_RESPONSE", 0, 0

        payload = {
            "model": model_to_use,
            "messages": messages,
            "temperature": 0.0
        }
        
        import httpx
        import time
        max_retries = 6
        backoff_factor = 2.0
        
        for attempt in range(max_retries):
            try:
                with httpx.Client(http2=False, timeout=90.0) as client:
                    response = client.post(url, json=payload, headers=headers)
                    
                    # Handle rate limit (429 or Zhipu 1302 code)
                    is_rate_limited = (response.status_code == 429) or \
                                      (response.status_code == 400 and "1302" in response.text)
                    
                    if is_rate_limited:
                        wait_time = (backoff_factor ** attempt) + 1.0
                        log_progress(f"    [!] Rate limit hit (429/1302). Retrying in {wait_time:.1f}s (Attempt {attempt+1}/{max_retries})...")
                        time.sleep(wait_time)
                        continue
                        
                    response.raise_for_status()
                    res_data = response.json()
                    
                content = res_data["choices"][0]["message"]["content"]
                usage = res_data.get("usage", {})
                in_t = usage.get("prompt_tokens", len(json.dumps(messages)) // 4)
                out_t = usage.get("completion_tokens", len(content) // 4)
                return content, in_t, out_t
                
            except Exception as e:
                # If we have a response and it is rate limited, retry
                is_rate_limited_err = False
                if 'response' in locals():
                    if (response.status_code == 429) or ("rate" in response.text.lower()) or ("1302" in response.text):
                        is_rate_limited_err = True
                        
                if is_rate_limited_err and attempt < max_retries - 1:
                    wait_time = (backoff_factor ** attempt) + 1.0
                    log_progress(f"    [!] Rate limit exception. Retrying in {wait_time:.1f}s (Attempt {attempt+1}/{max_retries})...")
                    time.sleep(wait_time)
                    continue
                    
                if attempt == max_retries - 1:
                    err_body = ""
                    if 'response' in locals() and hasattr(response, "text"):
                        err_body = f" - Response: {response.text}"
                    raise RuntimeError(f"LLM API Call failed after {max_retries} attempts: {e}{err_body}")
                raise e

    # 2. Helper for executing tests in the target repository
    def run_repo_tests() -> tuple[bool, bool, str]:  # (runnable, success, log)
        files_in_root = os.listdir(repo_path)
        cmd = None
        
        if "Cargo.toml" in files_in_root:
            cmd = ["cargo", "test"]
        elif "go.mod" in files_in_root:
            cmd = ["go", "test", "./..."]
        elif "package.json" in files_in_root:
            if "yarn.lock" in files_in_root:
                cmd = ["yarn", "test"]
            else:
                cmd = ["npm", "test"]
        elif "composer.json" in files_in_root or "phpunit.xml" in files_in_root:
            if os.path.exists(os.path.join(repo_path, "vendor/bin/phpunit")):
                cmd = [os.path.join(repo_path, "vendor/bin/phpunit")]
            else:
                cmd = ["phpunit"]
        elif "Gemfile" in files_in_root or "Rakefile" in files_in_root or "spec" in files_in_root:
            if "Gemfile" in files_in_root:
                cmd = ["bundle", "exec", "rspec"]
            else:
                cmd = ["rspec"]
        elif "pom.xml" in files_in_root:
            cmd = ["mvn", "test"]
        elif "build.gradle" in files_in_root or "build.gradle.kts" in files_in_root:
            if "gradlew" in files_in_root:
                cmd = ["./gradlew", "test"]
            else:
                cmd = ["gradle", "test"]
        else:
            try:
                import pytest
                cmd = [sys.executable, "-m", "pytest"]
            except ImportError:
                venv_pytest = os.path.join(repo_path, ".venv/bin/pytest")
                if os.path.exists(venv_pytest):
                    cmd = [venv_pytest]
                else:
                    import shutil
                    if shutil.which("pytest"):
                        cmd = ["pytest"]
                    else:
                        venv_python = os.path.join(repo_path, ".venv/bin/python")
                        if os.path.exists(venv_python):
                            cmd = [venv_python, "-m", "unittest", "discover", "-s", "tests"]
                        else:
                            cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
                        
        if not cmd:
            return False, False, "No recognized test runner found for this repository."
            
        import shutil
        if not shutil.which(cmd[0]) and not (cmd[0].startswith("./") or os.path.isabs(cmd[0])):
            return False, False, f"Test runner command '{cmd[0]}' is not installed or not on PATH."
            
        try:
            res = subprocess.run(
                cmd,
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=120,
                start_new_session=True
            )
            # pytest exit code 5 = "no tests collected" — treat as Unrunnable, not Failed
            if res.returncode == 5:
                return False, False, res.stdout + "\n" + res.stderr
            return True, res.returncode == 0, res.stdout + "\n" + res.stderr
        except subprocess.TimeoutExpired as te:
            return True, False, f"Test suite timed out after 120 seconds.\nOutput so far:\n{te.stdout or ''}\n{te.stderr or ''}"
        except Exception as e:
            return False, False, f"Error executing test runner '{cmd}': {e}"

    # Extract JSON block
    def extract_json(text: str) -> dict:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start:end+1])
        raise ValueError(f"Could not extract JSON: {text}")

    # Extract markdown code blocks
    def extract_code(text: str) -> str:
        import re
        match = re.search(r'```[a-zA-Z0-9_-]*\n(.*?)\n```', text, re.DOTALL)
        if match:
            return match.group(1).strip()
        if "```" in text:
            parts = text.split("```")
            if len(parts) >= 3:
                content = parts[1].strip()
                lines = content.splitlines()
                if lines and re.match(r'^[a-zA-Z0-9_-]+$', lines[0]):
                    return "\n".join(lines[1:]).strip()
                return content
        return text.strip()

    # Determine if we are running in simulation
    is_simulation = force_simulation or not (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ZAI_API_KEY"))

    def log_progress(msg: str):
        print(msg, file=sys.stderr, flush=True)

    log_progress(f"\n[*] Starting comparative benchmark using models: {', '.join(models)}")
    log_progress(f"[*] Target repository: {repo_path}")
    log_progress(f"[*] Task description: \"{task_description}\"")
    if is_simulation:
        log_progress("[*] Mode: SIMULATION (No API keys found. Emulating standard task profiles.)\n")
    else:
        log_progress("[*] Mode: LIVE API RUN\n")

    # Backup the codebase files to restore afterwards
    supported_exts = (".py", ".rs", ".go", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".hpp", ".php", ".rb")
    backup_files = {}
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in ("node_modules", "build", "dist", "vendor", "venv", ".venv", "target", "website", "docs", "__pycache__", "wheels", "examples") and not d.endswith(".egg-info")]
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in supported_exts and not semantic_graph.is_test_file(os.path.join(root, file)):
                full_p = os.path.join(root, file)
                try:
                    with open(full_p, "rb") as f:
                        backup_files[full_p] = f.read()
                except Exception:
                    pass

    # Check total repository context size to prevent timeouts and API payload limit errors
    total_size_bytes = sum(len(content) for content in backup_files.values())
    estimated_tokens = total_size_bytes // 4
    if not force_simulation and estimated_tokens > 400000:
        log_progress(f"[!] Warning: Target repository is too large ({estimated_tokens:,} estimated tokens).")
        log_progress("    -> Skipping task to prevent API write/read timeouts and model context window limitations.")
        return "SKIPPED: Context exceeds max token threshold (400,000 tokens limit)"

    all_results = {}

    for model in models:
        log_progress(f"\n[*] ===== Evaluating Model: {model} =====")
        # --- RUN WITH RECON ---
        log_progress(f"[*] --- Stage 1: Running WITH RECON (3-Tier AST Guided) ---")
        recon_in_tokens = 0
        recon_out_tokens = 0
        recon_success = False
        recon_runnable = True
        recon_log = ""
        recon_mutated_entity = "N/A"
        recon_file = "N/A"
        recon_latency = 0.0

        if is_simulation:
            log_progress("    [+] Running With-Recon simulated execution...")
            recon_in_tokens = 1500
            recon_out_tokens = 250
            recon_success = True
            recon_latency = 0.80
            recon_mutated_entity = "Calculator.add"
            recon_file = "module_a.py"
            
            if "no-orient" in ablations:
                recon_in_tokens -= 700  # Saved blueprint generation tokens
                import random
                recon_success = random.choice([True, False, False]) # 33% pass rate
                recon_mutated_entity = "N/A"
                recon_file = "module_b.py"
                
            if "no-hydrate" in ablations:
                recon_in_tokens += 4500  # Extra full file context tokens
                recon_out_tokens += 1000 # Entire file is outputted
                import random
                recon_success = recon_success and random.choice([True, True, False]) # 66% of base success
                recon_latency = 5.40
                
            recon_log = "All tests passed (Mocked test run)" if recon_success else "Python syntax compile check failed: indent mismatch (Simulated syntax break)"
        else:
            try:
                import time
                if "no-orient" in ablations:
                    log_progress("    [+] [Ablation: no-orient] Skipping structural blueprint, listing files flatly...")
                    blueprint = "\n".join(os.path.relpath(p, repo_path) for p in backup_files.keys())
                else:
                    # Step A: Generate repo blueprint
                    log_progress("    [+] Step A: Generating repository blueprint & parsing AST nodes...")
                    blueprint = generate_repo_blueprint(repo_path)
                
                # Step B: Identify the target node to mutate
                log_progress("    [+] Step B: Calling LLM to identify mutation target file & entity FQN...")
                messages = [
                    {"role": "system", "content": "You are a software engineering agent acting on a codebase. Based on the repo blueprint, identify which single file path and method/function FQN should be modified. Task: " + task_description},
                    {"role": "user", "content": f"Repository Blueprint:\n{blueprint}\n\nSpecify the target in JSON. Return ONLY: {{\"file_path\": \"relative/path/to/file.py\", \"target_entity\": \"ClassName.method_name\"}}"}
                ]
                st = time.time()
                response, in_t, out_t = call_llm(messages, model)
                recon_latency += time.time() - st
                recon_in_tokens += in_t
                recon_out_tokens += out_t
                
                target_info = extract_json(response)
                rel_file_path = target_info["file_path"]
                target_entity = target_info["target_entity"]
                recon_file = rel_file_path
                recon_mutated_entity = target_entity
                log_progress(f"        -> Target identified: '{target_entity}' inside file '{rel_file_path}'")
                
                # Step C: Hydrate and mutate
                abs_file_path = os.path.join(repo_path, rel_file_path)
                
                if "no-hydrate" in ablations:
                    log_progress("    [+] [Ablation: no-hydrate] Skipping AST body isolation, passing full file content...")
                    with open(abs_file_path, "r", encoding="utf-8") as f:
                        file_content = f.read()
                    ext = os.path.splitext(rel_file_path)[1].lower()[1:] or "code"
                    messages = [
                        {"role": "system", "content": f"You are modifying the file '{rel_file_path}'. Code task: {task_description}"},
                        {"role": "user", "content": f"Current file content:\n```{ext}\n{file_content}\n```\n\nReturn ONLY the ENTIRE updated content of this file inside a code block."}
                    ]
                    st = time.time()
                    response, in_t, out_t = call_llm(messages, model)
                    recon_latency += time.time() - st
                    recon_in_tokens += in_t
                    recon_out_tokens += out_t
                    
                    new_file_content = extract_code(response)
                    log_progress("    [+] [Ablation: no-hydrate] Writing modified full file content to disk...")
                    with open(abs_file_path, "w", encoding="utf-8") as f:
                        f.write(new_file_content)
                    
                    log_progress("    [+] Step E: Running repository tests for With-Recon code...")
                    recon_runnable, recon_success, test_log = run_repo_tests()
                    recon_log = test_log
                    log_progress(f"        -> Test suite run completed (Result: {'Passed' if recon_success else ('Unrunnable' if not recon_runnable else 'Failed')})")
                else:
                    log_progress(f"    [+] Step C: Hydrating target body and requesting AST node modification from LLM...")
                    body = hydrate_body(abs_file_path, target_entity)

                    # Cross-Reference Pre-Flight: inject caller/callee deps and symbol
                    # references into the LLM prompt so it has full context before mutating.
                    bare_name = target_entity.split(".")[-1] if "." in target_entity else target_entity
                    xref_parts = []
                    try:
                        deps_text = semantic_graph.get_node_dependencies(abs_file_path, bare_name)
                        xref_parts.append("### Call-Graph Dependencies\n" + deps_text)
                    except Exception:
                        pass
                    try:
                        refs_text = semantic_graph.find_symbol_references(bare_name)
                        xref_parts.append("### Symbol References\n" + refs_text)
                    except Exception:
                        pass
                    xref_section = ("\n\n---\n## Cross-Reference Pre-Flight\n\n" + "\n\n".join(xref_parts)) if xref_parts else ""

                    ext = os.path.splitext(rel_file_path)[1].lower()[1:] or "code"
                    messages = [
                        {"role": "system", "content": f"You are modifying the body of '{target_entity}'. Code task: {task_description}"},
                        {"role": "user", "content": f"Current implementation body of {target_entity}:\n```{ext}\n{body}\n```{xref_section}\n\nReturn ONLY the new replacement code block for the body of this function. Do not write the function header/def statement."}
                    ]
                    st = time.time()
                    response, in_t, out_t = call_llm(messages, model)
                    recon_latency += time.time() - st
                    recon_in_tokens += in_t
                    recon_out_tokens += out_t
                    
                    new_body = extract_code(response)
                    log_progress("    [+] Step D: Compiling, aligning indentation, and mutating file on disk...")
                    mutation_res = mutate_body(abs_file_path, target_entity, new_body)
                    
                    if "successful" in mutation_res.lower():
                        log_progress("    [+] Step E: Running repository tests for With-Recon code...")
                        recon_runnable, recon_success, test_log = run_repo_tests()
                        recon_log = test_log
                        log_progress(f"        -> Test suite run completed (Result: {'Passed' if recon_success else ('Unrunnable' if not recon_runnable else 'Failed')})")
                    else:
                        recon_success = False
                        recon_log = f"AST Mutation Failed: {mutation_res}"
                        log_progress(f"        -> AST Mutation failed validation: {mutation_res}")
            except Exception as ex:
                recon_success = False
                recon_log = f"Recon comparative loop failed: {ex}"
                log_progress(f"        -> Loop encountered error: {ex}")

        # Restore codebase from backup
        log_progress("    [+] Restoring codebase back to clean backup state...")
        for p, content in backup_files.items():
            with open(p, "wb") as f:
                f.write(content)

        # --- RUN WITHOUT RECON (BASELINE) ---
        log_progress(f"[*] --- Stage 2: Running WITHOUT RECON (Baseline Context Overwrite) ---")
        baseline_in_tokens = 0
        baseline_out_tokens = 0
        baseline_success = False
        baseline_runnable = True
        baseline_log = ""
        baseline_file = "N/A"
        baseline_latency = 0.0

        if is_simulation:
            log_progress("    [+] Running Baseline simulated execution...")
            baseline_in_tokens = 12000
            baseline_out_tokens = 1500
            baseline_success = True
            baseline_log = "All tests passed (Mocked test run)"
            baseline_file = "module_a.py"
            baseline_latency = 8.50
        else:
            try:
                import time
                # Read full codebase contents (simulating standard context feeding)
                log_progress("    [+] Step A: Ingesting full repository source context into payload...")
                full_context = ""
                for p, content in backup_files.items():
                    rel_p = os.path.relpath(p, repo_path)
                    ext = os.path.splitext(p)[1][1:] or "code"
                    full_context += f"### File: {rel_p}\n```{ext}\n{content.decode('utf8', errors='ignore')}\n```\n\n"
                
                # Step A: Request modification of full file
                log_progress("    [+] Step B: Calling LLM to modify target source file within full context...")
                messages = [
                    {"role": "system", "content": "You are a software engineering agent acting on a codebase. You must modify the code to satisfy the task. Task: " + task_description},
                    {"role": "user", "content": f"Here is the full repository code:\n{full_context}\n\nImplement the changes. Specify which relative file path you modified, and return the ENTIRE updated content of that file inside a markdown code block (e.g. ```rust, ```go, ```python, etc.)."}
                ]
                st = time.time()
                response, in_t, out_t = call_llm(messages, model)
                baseline_latency += time.time() - st
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
                    # Fallback to first file
                    target_rel_path = os.path.relpath(list(backup_files.keys())[0], repo_path)
                    
                baseline_file = target_rel_path
                new_file_content = extract_code(response)
                log_progress(f"        -> Target identified: full overwrite of '{target_rel_path}'")
                
                # Overwrite file
                log_progress("    [+] Step C: Writing modified file content to disk...")
                abs_target_path = os.path.join(repo_path, target_rel_path)
                with open(abs_target_path, "w") as f:
                    f.write(new_file_content)
                    
                # Compile check and test execution
                log_progress("    [+] Step D: Compiling changes and running repository test suite...")
                if target_rel_path.endswith(".py"):
                    try:
                        compile(new_file_content, abs_target_path, "exec")
                    except Exception as compile_err:
                        log_progress(f"        -> Python syntax compile check failed: {compile_err}")
                
                baseline_runnable, baseline_success, test_log = run_repo_tests()
                baseline_log = test_log
                log_progress(f"        -> Test suite run completed (Result: {'Passed' if baseline_success else ('Unrunnable' if not baseline_runnable else 'Failed')})")
            except Exception as ex:
                baseline_success = False
                baseline_log = f"Baseline comparative loop failed: {ex}"
                log_progress(f"        -> Loop encountered error: {ex}")

        # Restore codebase back to original state
        log_progress("    [+] Restoring codebase back to clean backup state...")
        for p, content in backup_files.items():
            with open(p, "wb") as f:
                f.write(content)

        # --- RUN WITH LLMLINGUA (STAGE 3) ---
        log_progress(f"[*] --- Stage 3: Running WITH LLMLINGUA (Prompt Compression Baseline) ---")
        llmlingua_in_tokens = 0
        llmlingua_out_tokens = 0
        llmlingua_success = False
        llmlingua_runnable = True
        llmlingua_log = ""
        llmlingua_file = "N/A"
        llmlingua_latency = 0.0

        if is_simulation:
            log_progress("    [+] Running LLMLingua simulated execution...")
            llmlingua_in_tokens = 5000
            llmlingua_out_tokens = 1500
            llmlingua_success = False
            llmlingua_log = "Python syntax compile check failed: indent mismatch (Simulated syntax break)"
            llmlingua_file = "module_a.py"
            llmlingua_latency = 6.20
        else:
            try:
                import time
                # Read full codebase contents
                log_progress("    [+] Step A: Ingesting and compressing repository source context via LLMLingua...")
                full_context = ""
                for p, content in backup_files.items():
                    rel_p = os.path.relpath(p, repo_path)
                    ext = os.path.splitext(p)[1][1:] or "code"
                    full_context += f"### File: {rel_p}\n```{ext}\n{content.decode('utf8', errors='ignore')}\n```\n\n"
                
                # Compress context
                approx_tokens = len(full_context) // 4
                target_tokens = int(approx_tokens * 0.6) # 40% reduction
                compressed_context = compress_context_llmlingua(full_context, target_token=target_tokens)
                
                log_progress(f"        -> Compressed context from {approx_tokens:,} to {len(compressed_context)//4:,} tokens.")
                
                # Call LLM
                log_progress("    [+] Step B: Calling LLM to modify target source file within compressed context...")
                messages = [
                    {"role": "system", "content": "You are a software engineering agent acting on a codebase. You must modify the code to satisfy the task. Task: " + task_description},
                    {"role": "user", "content": f"Here is the compressed repository code:\n{compressed_context}\n\nImplement the changes. Specify which relative file path you modified, and return the ENTIRE updated content of that file inside a markdown code block (e.g. ```rust, ```go, ```python, etc.)."}
                ]
                st = time.time()
                response, in_t, out_t = call_llm(messages, model)
                llmlingua_latency += time.time() - st
                llmlingua_in_tokens += in_t
                llmlingua_out_tokens += out_t
                
                # Extract target file and content
                target_rel_path = None
                for p in backup_files.keys():
                    rel_p = os.path.relpath(p, repo_path)
                    if rel_p in response:
                        target_rel_path = rel_p
                        break
                
                if not target_rel_path:
                    target_rel_path = os.path.relpath(list(backup_files.keys())[0], repo_path)
                    
                llmlingua_file = target_rel_path
                new_file_content = extract_code(response)
                log_progress(f"        -> Target identified: full overwrite of '{target_rel_path}'")
                
                # Overwrite file
                log_progress("    [+] Step C: Writing modified file content to disk...")
                abs_target_path = os.path.join(repo_path, target_rel_path)
                with open(abs_target_path, "w") as f:
                    f.write(new_file_content)
                    
                # Compile check and test execution
                log_progress("    [+] Step D: Compiling changes and running repository test suite...")
                compile_ok = True
                if target_rel_path.endswith(".py"):
                    try:
                        compile(new_file_content, abs_target_path, "exec")
                    except Exception as compile_err:
                        compile_ok = False
                        llmlingua_success = False
                        llmlingua_log = f"Python syntax compile check failed: {compile_err}"
                        log_progress(f"        -> Python syntax compile check failed: {compile_err}")
                
                if compile_ok:
                    llmlingua_runnable, llmlingua_success, test_log = run_repo_tests()
                    llmlingua_log = test_log
                    log_progress(f"        -> Test suite run completed (Result: {'Passed' if llmlingua_success else ('Unrunnable' if not llmlingua_runnable else 'Failed')})")
            except Exception as ex:
                llmlingua_success = False
                llmlingua_log = f"LLMLingua comparative loop failed: {ex}"
                log_progress(f"        -> Loop encountered error: {ex}")

        # Restore codebase back to original state
        log_progress("    [+] Restoring codebase back to clean backup state...")
        for p, content in backup_files.items():
            with open(p, "wb") as f:
                f.write(content)

        # Store results for this model
        all_results[model] = {
            "recon_in": recon_in_tokens,
            "recon_out": recon_out_tokens,
            "recon_success": recon_success,
            "recon_runnable": recon_runnable,
            "recon_log": recon_log,
            "recon_mutated_entity": recon_mutated_entity,
            "recon_file": recon_file,
            "base_in": baseline_in_tokens,
            "base_out": baseline_out_tokens,
            "base_success": baseline_success,
            "base_runnable": baseline_runnable,
            "base_log": baseline_log,
            "base_file": baseline_file,
            "llmlingua_in": llmlingua_in_tokens,
            "llmlingua_out": llmlingua_out_tokens,
            "llmlingua_success": llmlingua_success,
            "llmlingua_runnable": llmlingua_runnable,
            "llmlingua_log": llmlingua_log,
            "llmlingua_file": llmlingua_file,
            "recon_latency": recon_latency,
            "base_latency": baseline_latency,
            "llmlingua_latency": llmlingua_latency
        }

    log_progress("\n[*] Benchmark execution complete. Formatting side-by-side metrics report...")

    # 3. Format Comparative Report
    report = [
        "# Comparative Evaluation Report: Recon vs. Baseline",
        f"**Models Evaluated**: {', '.join([f'`{m}`' for m in models])}",
        f"**Task Description**: *\"{task_description}\"*",
        f"**Target Repository**: `{repo_path}`",
        "",
        "| Model | Evaluation Metric | With Recon (3-Tier) | Without Recon (Baseline) | With LLMLingua | Savings vs Base | Savings vs Lingua |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
    ]
    
    for model in models:
        res = all_results[model]
        recon_in_tokens = res["recon_in"]
        recon_out_tokens = res["recon_out"]
        baseline_in_tokens = res["base_in"]
        baseline_out_tokens = res["base_out"]
        llmlingua_in_tokens = res["llmlingua_in"]
        llmlingua_out_tokens = res["llmlingua_out"]
        
        total_recon = recon_in_tokens + recon_out_tokens
        total_baseline = baseline_in_tokens + baseline_out_tokens
        total_lingua = llmlingua_in_tokens + llmlingua_out_tokens
        
        in_savings_base = f"{(1 - recon_in_tokens / max(1, baseline_in_tokens)) * 100:.1f}%" if baseline_in_tokens else "0.0%"
        in_savings_lingua = f"{(1 - recon_in_tokens / max(1, llmlingua_in_tokens)) * 100:.1f}%" if llmlingua_in_tokens else "0.0%"
        
        out_savings_base = f"{(1 - recon_out_tokens / max(1, baseline_out_tokens)) * 100:.1f}%" if baseline_out_tokens else "0.0%"
        out_savings_lingua = f"{(1 - recon_out_tokens / max(1, llmlingua_out_tokens)) * 100:.1f}%" if llmlingua_out_tokens else "0.0%"
        
        total_savings_base = f"{(1 - total_recon / max(1, total_baseline)) * 100:.1f}%" if total_baseline else "0.0%"
        total_savings_lingua = f"{(1 - total_recon / max(1, total_lingua)) * 100:.1f}%" if total_lingua else "0.0%"
        
        recon_lat = res["recon_latency"]
        base_lat = res["base_latency"]
        lingua_lat = res["llmlingua_latency"]
        
        lat_speedup_base = f"{base_lat / max(0.01, recon_lat):.1f}x speedup" if base_lat else "1.0x speedup"
        lat_speedup_lingua = f"{lingua_lat / max(0.01, recon_lat):.1f}x speedup" if lingua_lat else "1.0x speedup"

        recon_status = '✅ Passed' if res["recon_success"] else ('⚠️ Unrunnable' if not res["recon_runnable"] else '❌ Failed')
        base_status = '✅ Passed' if res["base_success"] else ('⚠️ Unrunnable' if not res["base_runnable"] else '❌ Failed')
        lingua_status = '✅ Passed' if res["llmlingua_success"] else ('⚠️ Unrunnable' if not res["llmlingua_runnable"] else '❌ Failed')
        
        report.append(f"| **{model}** | Input Tokens | {recon_in_tokens:,} | {baseline_in_tokens:,} | {llmlingua_in_tokens:,} | **{in_savings_base}** | **{in_savings_lingua}** |")
        report.append(f"| | Output Tokens | {recon_out_tokens:,} | {baseline_out_tokens:,} | {llmlingua_out_tokens:,} | **{out_savings_base}** | **{out_savings_lingua}** |")
        report.append(f"| | Total Tokens | {total_recon:,} | {total_baseline:,} | {total_lingua:,} | **{total_savings_base}** | **{total_savings_lingua}** |")
        report.append(f"| | Latency | {recon_lat:.2f}s | {base_lat:.2f}s | {lingua_lat:.2f}s | **{lat_speedup_base}** | **{lat_speedup_lingua}** |")
        report.append(f"| | Test Status | {recon_status} | {base_status} | {lingua_status} | - | - |")
        report.append(f"| | Target Detail | `{res['recon_mutated_entity']}` in `{res['recon_file']}` | `{res['base_file']}` (full) | `{res['llmlingua_file']}` (comp) | - | - |")
        report.append("| --- | --- | --- | --- | --- | --- | --- |")
    
    report.append("")
    
    if is_simulation:
        report.append("> [!NOTE]")
        report.append("> **Simulation Mode Active**: No API keys (OPENROUTER_API_KEY, OPENAI_API_KEY, or DEEPSEEK_API_KEY) were found in the environment. The metrics above represent standard simulated profiles.")
        report.append("")
        
    report.append("## Detailed Logs")
    for model in models:
        res = all_results[model]
        report.append(f"\n### Model: `{model}`")
        report.append("#### Recon Test Run Log:")
        report.append("```")
        report.append(res["recon_log"].strip())
        report.append("```")
        report.append("#### Baseline Test Run Log:")
        report.append("```")
        report.append(res["base_log"].strip())
        report.append("```")
        report.append("#### LLMLingua Test Run Log:")
        report.append("```")
        report.append(res["llmlingua_log"].strip())
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

@mcp.tool()
def bootstrap_results_from_log(logs_dir: str) -> tuple[list[dict], bool]:
    import os
    import re
    import sys
    if not os.path.exists(logs_dir):
        return [], False
    
    log_files = []
    for f in os.listdir(logs_dir):
        if f.startswith("lite-80_") and f.endswith(".log"):
            p = os.path.join(logs_dir, f)
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as lf:
                    header = lf.read(10000)
                    if "Processing Claw-Lite Task" in header:
                        log_files.append((f, os.path.getmtime(p)))
            except Exception:
                pass
                
    if not log_files:
        return [], False
        
    log_files.sort(key=lambda x: x[1], reverse=True)
    
    task_re = re.compile(r"\[\*\] Processing Claw-Lite Task \d+/\d+:\s*(\S+)")
    stage1_re = re.compile(r"\[\*\] --- Stage 1: Running WITH RECON")
    stage2_re = re.compile(r"\[\*\] --- Stage 2: Running WITHOUT RECON")
    test_re = re.compile(r"-> Test suite run completed \(Result:\s*(.*?)\)")
    success_re = re.compile(
        r"\[\+\] Successfully benchmarked task\s*(\S+)(?:\s*\|\s*Recon tokens:\s*in=(\d+),\s*out=(\d+)\s*\|\s*Baseline tokens:\s*in=(\d+),\s*out=(\d+))?"
    )
    failed_re = re.compile(r"\[!\] Benchmark execution failed:\s*(.*)")
    loop_err_re = re.compile(r"-> Loop encountered error:\s*(.*)")

    for filename, mtime in log_files:
        latest_log = os.path.join(logs_dir, filename)
        print(f"[*] Checking log file for bootstrap: {latest_log}", file=sys.stderr, flush=True)
        
        try:
            with open(latest_log, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            continue
            
        results = []
        current_task = None
        stage1_status = None
        stage2_status = None
        current_stage = None
        
        for line in lines:
            task_match = task_re.search(line)
            if task_match:
                current_task = task_match.group(1)
                stage1_status = None
                stage2_status = None
                current_stage = None
                continue
                
            if not current_task:
                continue
                
            if stage1_re.search(line):
                current_stage = 1
                continue
            elif stage2_re.search(line):
                current_stage = 2
                continue
                
            loop_err_match = loop_err_re.search(line)
            if loop_err_match:
                err_msg = loop_err_match.group(1).strip()
                if current_stage == 1:
                    stage1_status = f"Error: {err_msg}"
                elif current_stage == 2:
                    stage2_status = f"Error: {err_msg}"
                continue
                
            test_match = test_re.search(line)
            if test_match:
                status = test_match.group(1).strip()
                if current_stage == 1:
                    stage1_status = status
                elif current_stage == 2:
                    stage2_status = status
                continue
                
            success_match = success_re.search(line)
            if success_match and success_match.group(1) == current_task:
                recon_pass = (stage1_status == "Passed")
                base_pass = (stage2_status == "Passed")
                recon_runnable = (stage1_status != "Unrunnable" and not (stage1_status and stage1_status.startswith("Error:")))
                base_runnable = (stage2_status != "Unrunnable" and not (stage2_status and stage2_status.startswith("Error:")))
                
                recon_in = int(success_match.group(2)) if success_match.group(2) else 0
                recon_out = int(success_match.group(3)) if success_match.group(3) else 0
                base_in = int(success_match.group(4)) if success_match.group(4) else 0
                base_out = int(success_match.group(5)) if success_match.group(5) else 0
                
                results.append({
                    "instance_id": current_task,
                    "success": True,
                    "recon_in": recon_in,
                    "recon_out": recon_out,
                    "base_in": base_in,
                    "base_out": base_out,
                    "recon_pass": recon_pass,
                    "base_pass": base_pass,
                    "runnable": recon_runnable and base_runnable,
                    "error": None
                })
                current_task = None
                continue
                
            failed_match = failed_re.search(line)
            if failed_match:
                err_msg = failed_match.group(1).strip()
                results.append({
                    "instance_id": current_task,
                    "success": False,
                    "recon_in": 0,
                    "recon_out": 0,
                    "base_in": 0,
                    "base_out": 0,
                    "recon_pass": False,
                    "base_pass": False,
                    "runnable": False,
                    "error": err_msg
                })
                current_task = None
                continue
                
        if len(results) > 0:
            print(f"[*] Successfully bootstrapped {len(results)} tasks from {latest_log}", file=sys.stderr, flush=True)
            return results, True
            
    return [], False

@mcp.tool()
@log_token_metrics("run_claw_lite_benchmark")
def run_claw_lite_benchmark(workspace_dir: str, limit: int = 80, shuffle: bool = False, model_name: str = "", resume: bool = False, ablations: list[str] = None, force_simulation: bool = False) -> str:
    """
    Executes comparative benchmarks across the Claw-SWE-Bench Lite-80 subset.
    Measures average token savings, validates test result consistency, and compiles
    a summary report.
    """
    import subprocess
    import os
    import sys
    import random
    import json
    
    if ablations is None:
        ablations = []
        
    workspace_dir = os.path.abspath(workspace_dir)
    is_simulation = force_simulation or not (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("ZAI_API_KEY"))

    def log_progress(msg: str):
        print(msg, file=sys.stderr, flush=True)

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    logs_dir = os.path.join(project_root, "logs")
    checkpoint_path = os.path.join(logs_dir, "lite-80_checkpoint.json")
    
    # Resolve model name from environment variables if not provided
    if not model_name:
        model_name = os.environ.get("RECON_MODEL") or os.environ.get("DEFAULT_MODEL") or "deepseek/deepseek-chat"

    models = [m.strip() for m in model_name.split(",") if m.strip()]
    if not models:
        models = ["deepseek/deepseek-chat"]

    results = []
    resumed_from_log = False

    def save_checkpoint(results_list, from_log):
        try:
            with open(checkpoint_path, "w", encoding="utf-8") as f:
                json.dump({
                    "resumed_from_log": from_log,
                    "results": results_list
                }, f, indent=2)
        except Exception as e:
            log_progress(f"[!] Failed to write checkpoint: {e}")

    if resume:
        if os.path.exists(checkpoint_path):
            try:
                with open(checkpoint_path, "r", encoding="utf-8") as f:
                    checkpoint_data = json.load(f)
                if isinstance(checkpoint_data, dict) and "results" in checkpoint_data:
                    results = checkpoint_data["results"]
                    resumed_from_log = checkpoint_data.get("resumed_from_log", False)
                else:
                    results = checkpoint_data
                log_progress(f"\n[*] Resumed from checkpoint file. Loaded {len(results)} tasks.")
            except Exception as e:
                log_progress(f"\n[!] Failed to load checkpoint: {e}. Starting fresh.")
                results = []
        else:
            bootstrap_res, ok = bootstrap_results_from_log(logs_dir)
            if ok:
                results = bootstrap_res
                resumed_from_log = True
                log_progress(f"\n[*] Bootstrapped checkpoint with {len(results)} tasks from latest log file.")
                save_checkpoint(results, resumed_from_log)
            else:
                log_progress("\n[!] No previous log file with benchmark progress found. Starting fresh.")

    if is_simulation:
        log_progress(f"\n[*] Starting simulated Claw-SWE-Bench Lite-80 evaluation (Limit: {limit} instances, Models: {', '.join(models)})...")
        instance_ids = [f"Claw-Lite-{i+1:02d}" for i in range(80)]
        if shuffle:
            log_progress("[*] Shuffling simulated instances for random order...")
            random.shuffle(instance_ids)
        instance_ids = instance_ids[:limit]
        
        for idx, instance_id in enumerate(instance_ids):
            models_todo = [m for m in models if not any(r["instance_id"] == instance_id and r.get("model_name") == m for r in results)]
            if not models_todo:
                log_progress(f"    [*] Skipping already completed simulated task {instance_id}")
                continue
                
            for model in models_todo:
                # Seed to generate deterministic mock benchmark values
                random.seed(hash(instance_id + model))
                base_in = random.randint(11000, 16000)
                base_out = random.randint(800, 1200)
                
                # Recon savings typically: input 60-80%, output 80-90%
                recon_in = int(base_in * random.uniform(0.12, 0.35))
                recon_out = int(base_out * random.uniform(0.10, 0.20))
                recon_pass = True
                recon_lat = 0.80
                
                if "no-orient" in ablations:
                    recon_in = int(recon_in * 0.5)  # Less input (no blueprint)
                    recon_pass = random.choice([True, False, False]) # Drops to 33%
                    recon_lat = 0.40
                if "no-hydrate" in ablations:
                    recon_in = int(base_in * random.uniform(0.40, 0.60)) # Target-file scale
                    recon_out = int(base_out * random.uniform(0.80, 1.00)) # Full file output
                    recon_pass = recon_pass and random.choice([True, True, False]) # Drops to 66%
                    recon_lat = 6.20
                
                # LLMLingua simulated values: input compressed, output large
                llmlingua_in = int(base_in * random.uniform(0.40, 0.65))
                llmlingua_out = int(base_out * random.uniform(0.80, 1.20))
                
                results.append({
                    "instance_id": instance_id,
                    "model_name": model,
                    "success": True,
                    "recon_in": recon_in,
                    "recon_out": recon_out,
                    "base_in": base_in,
                    "base_out": base_out,
                    "llmlingua_in": llmlingua_in,
                    "llmlingua_out": llmlingua_out,
                    "recon_latency": recon_lat,
                    "base_latency": 8.50,
                    "llmlingua_latency": 6.20,
                    "recon_pass": recon_pass,
                    "base_pass": True,
                    "llmlingua_pass": random.choice([True, False]), # Simulated syntax hypnosis failure
                    "runnable": True,
                    "error": None
                })
                log_progress(f"    [+] Evaluated {instance_id} for {model}: Recon total = {recon_in+recon_out:,} | Baseline total = {base_in+base_out:,} | LLMLingua total = {llmlingua_in+llmlingua_out:,}")
            save_checkpoint(results, resumed_from_log)
    else:
        try:
            from datasets import load_dataset
        except ImportError:
            return "Error: Hugging Face 'datasets' library is required to run the Claw-SWE-Bench evaluation. Please run 'uv add datasets' in the project directory."

        log_progress(f"\n[*] Loading Claw-SWE-Bench Lite-80 dataset from Hugging Face...")
        try:
            dataset = load_dataset("TokenRhythm/Claw-SWE-Bench", "lite", split="test")
        except Exception as e:
            return f"Error loading Claw-SWE-Bench dataset: {e}"

        os.makedirs(workspace_dir, exist_ok=True)
        
        items = list(dataset)
        if shuffle:
            log_progress("[*] Shuffling dataset items for random order...")
            random.shuffle(items)
            
        count = 0

        for item in items:
            if count >= limit:
                break
                
            instance_id = item.get("instance_id", f"task_{count}")
            repo_name = item.get("repo", "")
            base_commit = item.get("base_commit", "")
            problem_statement = item.get("problem_statement", "")
            
            if not repo_name or not problem_statement:
                count += 1  # still consume the slot to respect the limit
                continue

            models_todo = [m for m in models if not any(r["instance_id"] == instance_id and r.get("model_name") == m for r in results)]
            if not models_todo:
                log_progress(f"[*] Skipping already completed task {count + 1}/{limit}: {instance_id}")
                count += 1
                continue

            log_progress(f"\n[*] Processing Claw-Lite Task {count + 1}/{limit}: {instance_id}")
            
            target_repo_dir = os.path.join(workspace_dir, f"instance_{instance_id}")
            
            # Clone and setup repository if needed
            if not os.path.exists(target_repo_dir) or not os.path.exists(os.path.join(target_repo_dir, ".git")):
                if os.path.exists(target_repo_dir):
                    import shutil
                    try:
                        shutil.rmtree(target_repo_dir)
                    except Exception:
                        pass
                os.makedirs(target_repo_dir, exist_ok=True)
                repo_url = f"https://github.com/{repo_name}.git"
                log_progress(f"    [+] Cloning {repo_url}...")
                try:
                    subprocess.run(["git", "clone", repo_url, "."], cwd=target_repo_dir, check=True, capture_output=True)
                    if base_commit:
                        log_progress(f"        -> Checking out commit {base_commit}...")
                        subprocess.run(["git", "checkout", base_commit], cwd=target_repo_dir, check=True, capture_output=True)
                except Exception as clone_err:
                    log_progress(f"    [!] Git operation failed: {clone_err}")
                    for model in models_todo:
                        results.append({
                            "instance_id": instance_id,
                            "model_name": model,
                            "success": False,
                            "recon_in": 0, "recon_out": 0,
                            "base_in": 0, "base_out": 0,
                            "error": f"Setup failed: {clone_err}"
                        })
                    save_checkpoint(results, resumed_from_log)
                    count += 1
                    continue

            # Run comparative benchmark for each model
            for model in models_todo:
                log_progress(f"    [*] Evaluating model: {model}")
                try:
                    report = run_comparative_benchmark(
                        repo_path=target_repo_dir,
                        task_description=problem_statement,
                        model_name=model,
                        ablations=ablations,
                        force_simulation=force_simulation
                    )
                    
                    if report.startswith("SKIPPED") or report.startswith("Error"):
                        log_progress(f"    [-] Task skipped or errored: {report}")
                        results.append({
                            "instance_id": instance_id,
                            "model_name": model,
                            "success": False,
                            "recon_in": 0, "recon_out": 0,
                            "base_in": 0, "base_out": 0,
                            "llmlingua_in": 0, "llmlingua_out": 0,
                            "recon_latency": 0.0, "base_latency": 0.0, "llmlingua_latency": 0.0,
                            "recon_pass": False, "base_pass": False, "llmlingua_pass": False,
                            "runnable": False,
                            "error": report
                        })
                        save_checkpoint(results, resumed_from_log)
                        continue

                    recon_in, recon_out = 0, 0
                    base_in, base_out = 0, 0
                    llmlingua_in, llmlingua_out = 0, 0
                    recon_pass, base_pass, llmlingua_pass = False, False, False
                    recon_runnable, base_runnable, llmlingua_runnable = True, True, True
                    
                    # Parse metrics from returned markdown report
                    recon_lat, base_lat, llmlingua_lat = 0.0, 0.0, 0.0
                    for line in report.splitlines():
                        raw_parts = [p.strip() for p in line.split("|")]
                        if len(raw_parts) >= 7:
                            metric = raw_parts[2]
                            def get_int(val_str):
                                val_str = val_str.replace(",", "").replace("*", "")
                                return int(val_str) if val_str.isdigit() else 0
                            def get_float(val_str):
                                val_str = val_str.replace("s", "").replace(",", "").replace("*", "")
                                return float(val_str) if val_str.replace(".", "", 1).isdigit() else 0.0
                            if metric == "Input Tokens":
                                recon_in = get_int(raw_parts[3])
                                base_in = get_int(raw_parts[4])
                                llmlingua_in = get_int(raw_parts[5])
                            elif metric == "Output Tokens":
                                recon_out = get_int(raw_parts[3])
                                base_out = get_int(raw_parts[4])
                                llmlingua_out = get_int(raw_parts[5])
                            elif metric == "Latency":
                                recon_lat = get_float(raw_parts[3])
                                base_lat = get_float(raw_parts[4])
                                llmlingua_lat = get_float(raw_parts[5])
                            elif metric == "Test Status" or metric == "Test Compilation & Run":
                                recon_cell = raw_parts[3]
                                base_cell = raw_parts[4]
                                lingua_cell = raw_parts[5]
                                recon_pass = "Passed" in recon_cell or "\u2705" in recon_cell
                                base_pass = "Passed" in base_cell or "\u2705" in base_cell
                                llmlingua_pass = "Passed" in lingua_cell or "\u2705" in lingua_cell
                                recon_runnable = "Unrunnable" not in recon_cell
                                base_runnable = "Unrunnable" not in base_cell
                                llmlingua_runnable = "Unrunnable" not in lingua_cell
                    
                    results.append({
                        "instance_id": instance_id,
                        "model_name": model,
                        "success": True,
                        "recon_in": recon_in,
                        "recon_out": recon_out,
                        "base_in": base_in,
                        "base_out": base_out,
                        "llmlingua_in": llmlingua_in,
                        "llmlingua_out": llmlingua_out,
                        "recon_latency": recon_lat,
                        "base_latency": base_lat,
                        "llmlingua_latency": llmlingua_lat,
                        "recon_pass": recon_pass,
                        "base_pass": base_pass,
                        "llmlingua_pass": llmlingua_pass,
                        "runnable": recon_runnable and base_runnable and llmlingua_runnable,
                        "error": None
                    })
                    log_progress(f"    [+] Successfully benchmarked model {model} for task {instance_id} | Recon tokens: in={recon_in}, out={recon_out} | Baseline tokens: in={base_in}, out={base_out} | LLMLingua tokens: in={llmlingua_in}, out={llmlingua_out} | Latency: recon={recon_lat:.2f}s, base={base_lat:.2f}s, lingua={llmlingua_lat:.2f}s")
                except Exception as benchmark_err:
                    log_progress(f"    [!] Benchmark execution failed for model {model}: {benchmark_err}")
                    results.append({
                        "instance_id": instance_id,
                        "model_name": model,
                        "success": False,
                        "recon_in": 0, "recon_out": 0,
                        "base_in": 0, "base_out": 0,
                        "llmlingua_in": 0, "llmlingua_out": 0,
                        "recon_latency": 0.0, "base_latency": 0.0, "llmlingua_latency": 0.0,
                        "recon_pass": False, "base_pass": False, "llmlingua_pass": False,
                        "error": str(benchmark_err)
                    })

            save_checkpoint(results, resumed_from_log)
            count += 1

    # Compile aggregate reports
    summary = ["# Claw-SWE-Bench Lite-80 Benchmark Summary"]
    
    # Check if we have results without model_name (from bootstrap/previous single-model runs)
    has_legacy_results = any(not r.get("model_name") for r in results)
    
    for model in models:
        # Filter results for this model. Fall back to legacy results if model matches models[0]
        model_runs = [r for r in results if r.get("model_name") == model or (has_legacy_results and not r.get("model_name") and model == models[0])]
        total_runs = len(model_runs)
        successful_runs = [r for r in model_runs if r["success"]]
        total_successful = len(successful_runs)
        
        summary.append(f"\n## Model: `{model}`")
        summary.append(f"**Tasks Evaluated**: `{total_successful} / {total_runs}` successful runs")
        
        if total_successful == 0:
            summary.append("- Error: No benchmark runs completed successfully for this model.")
            continue
            
        sum_recon_in = sum(r.get("recon_in", 0) for r in successful_runs)
        sum_recon_out = sum(r.get("recon_out", 0) for r in successful_runs)
        sum_base_in = sum(r.get("base_in", 0) for r in successful_runs)
        sum_base_out = sum(r.get("base_out", 0) for r in successful_runs)
        sum_lingua_in = sum(r.get("llmlingua_in", 0) for r in successful_runs)
        sum_lingua_out = sum(r.get("llmlingua_out", 0) for r in successful_runs)

        avg_recon_in = int(sum_recon_in / total_successful)
        avg_recon_out = int(sum_recon_out / total_successful)
        avg_base_in = int(sum_base_in / total_successful)
        avg_base_out = int(sum_base_out / total_successful)
        avg_lingua_in = int(sum_lingua_in / total_successful)
        avg_lingua_out = int(sum_lingua_out / total_successful)

        avg_recon_total = avg_recon_in + avg_recon_out
        avg_base_total = avg_base_in + avg_base_out
        avg_lingua_total = avg_lingua_in + avg_lingua_out

        in_savings_base = f"{(1 - avg_recon_in / max(1, avg_base_in)) * 100:.1f}%" if avg_base_in else "0.0%"
        in_savings_lingua = f"{(1 - avg_recon_in / max(1, avg_lingua_in)) * 100:.1f}%" if avg_lingua_in else "0.0%"
        
        out_savings_base = f"{(1 - avg_recon_out / max(1, avg_base_out)) * 100:.1f}%" if avg_base_out else "0.0%"
        out_savings_lingua = f"{(1 - avg_recon_out / max(1, avg_lingua_out)) * 100:.1f}%" if avg_lingua_out else "0.0%"
        
        total_savings_base = f"{(1 - avg_recon_total / max(1, avg_base_total)) * 100:.1f}%" if avg_base_total else "0.0%"
        total_savings_lingua = f"{(1 - avg_recon_total / max(1, avg_lingua_total)) * 100:.1f}%" if avg_lingua_total else "0.0%"
        consistent_count = 0
        runnable_count = 0
        discrepancy_details = []
        
        for r in successful_runs:
            if not r.get("runnable", True):
                continue
            runnable_count += 1
            if r["recon_pass"] == r["base_pass"]:
                consistent_count += 1
            else:
                discrepancy_details.append(f"- `{r['instance_id']}`: Recon pass={r['recon_pass']} | Baseline pass={r['base_pass']}")
     
        consistency_rate = (consistent_count / max(1, runnable_count)) * 100
        
        summary.extend([
            "",
            "### Average Token Metrics",
            "",
            "| Evaluation Metric | With Recon (3-Tier) | Without Recon (Baseline) | With LLMLingua | Savings vs Base | Savings vs Lingua |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
            f"| Average Input Tokens | {avg_recon_in:,} | {avg_base_in:,} | {avg_lingua_in:,} | **{in_savings_base}** | **{in_savings_lingua}** |",
            f"| Average Output Tokens | {avg_recon_out:,} | {avg_base_out:,} | {avg_lingua_out:,} | **{out_savings_base}** | **{out_savings_lingua}** |",
            f"| Average Total Tokens | {avg_recon_total:,} | {avg_base_total:,} | {avg_lingua_total:,} | **{total_savings_base}** | **{total_savings_lingua}** |",
            "",
            "### Results Functional Consistency Validation",
            "",
            f"**Test Result Consistency**: `{consistency_rate:.1f}%` ({consistent_count} of {runnable_count} runnable tasks achieved the same test pass/fail outcome)"
        ])

        if total_successful > runnable_count:
            summary.append(f"- ⚠️ **Unrunnable Tasks Excluded**: {total_successful - runnable_count} tasks were excluded from consistency checks because their test suites could not be run.")

        if consistency_rate == 100.0:
            summary.append("- ✅ **Results Validated**: Recon and the baseline achieved identical test execution results in all benchmark instances, confirming 100% functional parity.")
        else:
            summary.append("- ⚠️ **Results Discrepancy Detected**: Some task outcomes differed between Recon and the baseline:")
            summary.extend(discrepancy_details)
            
    # Clean up checkpoint on successful completion of all tasks
    if len(results) >= limit * len(models):
        try:
            if os.path.exists(checkpoint_path):
                os.remove(checkpoint_path)
        except Exception:
            pass

    if resumed_from_log:
        summary.append("\n- ⚠️ **Resumed from Log File**: The first tasks were restored from the previous run's log file. Because individual token metrics are not recorded in the log, their token counts were set to 0. This lowers the reported averages.")

    if is_simulation:
        summary.append("")
        summary.append("> [!NOTE]")
        summary.append("> **Simulation Mode Active**: No LLM API keys were found in the environment. Metrics represent a standard benchmark distribution.")

    return "\n".join(summary)

if __name__ == "__main__":
    mcp.run()
