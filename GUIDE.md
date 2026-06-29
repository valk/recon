# Step-by-Step Guide: Evaluating Recon on a Target GitHub Repository

This guide details how to configure, run, and evaluate the Recon MCP server against any random Python GitHub repository to compare token consumption (with vs. without Recon) on coding tasks.

---

## Step 1: Clone and Set Up the Target Repository
Choose a Python repository from GitHub that you want to test (for example, a utility library or a web application).

1. Clone the target repository to your local system:
   ```bash
   git clone https://github.com/example/target-repo.git /absolute/path/to/target-repo
   ```
2. Note down the absolute path to `/absolute/path/to/target-repo`.

---

## Step 2: Configure the MCP Client

To let an agent (like Claude, DeepSeek, etc.) use Recon's tools, configure it in your preferred developer tool.

### A. Claude Desktop Configuration
Open your Claude Desktop configuration file:
* **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
* **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

Add the `recon` server configuration pointing to your Recon project directory:

```json
{
  "mcpServers": {
    "recon": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/Users/valu/src/recon",
        "python",
        "-m",
        "recon.server"
      ]
    }
  }
}
```

### B. Roo Code / Roo Cline / Continue / Cursor Configuration
In your extension or IDE settings, add an MCP server configuration of type `command` or `stdio`:
* **Command**: `uv`
* **Args**: `["run", "--project", "/Users/valu/src/recon", "python", "-m", "recon.server"]`

---

## Step 3: Running the Evaluation Tasks

Ask the agent (running with DeepSeek or Claude) to complete any of the following tasks on the **target repository** using the MCP tools.

### Task 1: New Feature Addition
**Prompt to the Agent:**
> "Please add a new feature to the repository located at `/absolute/path/to/target-repo`. Add a custom API endpoint (or class method) called `calculate_metrics` inside the main module that aggregates statistics. Use the `generate_repo_blueprint` tool first to orient yourself, then surgically mutate only the necessary files."

### Task 2: Refactoring Code
**Prompt to the Agent:**
> "In the repository `/absolute/path/to/target-repo`, we need to change the method signature of a helper function to accept an extra config parameter. Find all upstream callers of the function using semantic graph tools, modify their calls, and update the signature safely. Use MCP tools for exploration and mutation."

### Task 3: Bug Fixing
**Prompt to the Agent:**
> "We are encountering a crash when passing empty strings to the parser. Locate the target class, check its references, and fix it. You are restricted to using Recon's hydration and mutation tools."

---

## Step 4: Seeing the Final Token Usage & Comparing Consumption

Once the agent completes a task, you can analyze and compare the token usage.

### 1. Request a Live Token Report from the Agent
You can ask the agent directly to print the token consumption report inside the chat conversation by prompting:
> "Please run the `get_token_metrics_report` tool to show a summary of all the tokens we have used for this session."

The agent will run the tool and output a clean markdown summary table showing total input/output tokens, total tool invocations, and a line-by-line breakdown of tool execution runtimes.

### 2. Inspect the Raw JSON Metrics File
Alternatively, you can view the metrics directly on disk. Open the hidden file `.mcp_token_metrics.json` in the root of the Recon project directory (e.g. `/path/to/recon/.mcp_token_metrics.json`). It stores:
* `total_input_tokens`: Cumulative tokens sent by the client to Recon tools.
* `total_output_tokens`: Cumulative tokens returned by Recon tools to the client.
* `calls`: A detailed log of all tools called, including their execution timestamps, input/output token metrics, and duration in seconds.

### 3. Run a Baseline Test (Without Recon)
To evaluate the exact token efficiency gains:
1. Reset the target repository (or clone it to a fresh directory).
2. Run the exact same task prompt with an agent that does *not* have the Recon MCP server connected (forcing it to use standard filesystem search and file-reading tools like `cat` or `read_file`).
3. Compare the total billing or session token counts in the baseline agent's console against the Recon session metrics report. In most cases, Recon provides a **70% to 90% reduction** in input tokens because the model never has to ingest full implementation files, tests, or unnecessary surrounding blocks.

---

## Automated Comparative Benchmarking

Instead of manually running tasks twice, you can execute the entire evaluation flow automatically using the `run_comparative_benchmark` tool.

### 1. Configure LLM API Keys (Optional)
To run actual completions against models like DeepSeek or Claude, set one of the following environment variables in your terminal before launching the server:
```bash
export OPENROUTER_API_KEY="your-openrouter-key"
# OR
export OPENAI_API_KEY="your-openai-key"
# OR
export DEEPSEEK_API_KEY="your-deepseek-key"
```
*Note: If no API keys are provided, the benchmark tool will automatically execute in a **Simulation Mode**, returning a mock report to verify directory configuration and test coverage.*

### 2. Invoke the Benchmark
Ask the agent (or trigger the tool in the Inspector) to run the comparative pipeline:
> "Run the comparative benchmark on `/absolute/path/to/target-repo` using model `deepseek/deepseek-chat` for the task: 'Add input parameter bounds checks to Calculator.subtract method'."

The server will automatically:
1. Back up the target files.
2. Run the task using Recon optimization (eliding code, requesting target entity identification, hydrating and mutating).
3. Execute the test suite and log the token count.
4. Restore the code, then run the baseline task (loading full context and file contents).
5. Execute the test suite again and compare.
6. Return a comprehensive side-by-side markdown comparison of input tokens, output tokens, overall savings, and unit test compilation success.

---

## Integration Methods (3 Ways to Use Recon)

Depending on your workflow, you can consume the `recon` MCP server and its tools in three distinct ways:

### 1. Direct Web UI (No coding agent/IDE needed)
You can run the benchmark or check the tools inside the official **MCP Inspector** web console.
```bash
npx -y @modelcontextprotocol/inspector uv run python src/recon/server.py
```
Open the URL printed in the terminal (usually `http://localhost:5173`) in your browser, select `run_comparative_benchmark` from the **Tools** list, fill in the arguments, and run the benchmark directly.

### 2. Programmatically in a Python Script
You can trigger benchmarks programmatically (e.g. for batch runs or custom scripts) without starting the MCP transport layer.

Create `run_benchmark.py`:
```python
import os
import sys

# Optional: set API keys for actual LLM execution (runs in simulation otherwise)
os.environ["OPENROUTER_API_KEY"] = "your-api-key"

# Add package source to Python path
sys.path.insert(0, "/absolute/path/to/recon/src")

from recon.server import run_comparative_benchmark

report = run_comparative_benchmark(
    repo_path="/absolute/path/to/target-repo",
    model_name="deepseek/deepseek-chat",
    task_description="Add bounds checks to Calculator.add"
)
print(report)
```
Run with:
```bash
uv run python run_benchmark.py
```

### 3. Inside MCP-Compatible Coding Agents & IDEs
Hook the server up to standard agent tools to allow coding models to use the elision, navigation, and mutation pipeline.

* **Claude Code (Terminal Agent)**:
  Register the server in Claude Code:
  ```bash
  claude mcp add uv --project /Users/valu/src/recon python -m recon.server
  ```
  Then query the agent:
  > *"Run a comparative benchmark on `/path/to/target-repo` using deepseek/deepseek-chat for the task: 'Fix bounds error in subtract'."*

* **Cursor IDE / Roo Code / Continue**:
  Add an MCP server in settings:
  - **Type**: `command`
  - **Command**: `uv`
  - **Args**: `["run", "--project", "/Users/valu/src/recon", "python", "-m", "recon.server"]`

---

## Interactive Local Testing (Without an IDE)

You can inspect and trigger tools manually using the Model Context Protocol Inspector:

```bash
# From `/Users/valu/src/recon`
npx -y @modelcontextprotocol/inspector uv run python src/recon/server.py
```

This will spin up a web interface on `localhost` where you can manually run `generate_repo_blueprint` with your target repository's path and inspect the AST-grounded skeletons and call graphs.
