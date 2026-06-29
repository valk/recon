import functools
import json
import os
import sys
import time
from datetime import datetime

# Resolve metrics file to the directory where the recon package lives
# so it's always the same location regardless of the server's working directory.
_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
METRICS_FILE = os.path.join(_PACKAGE_DIR, ".mcp_token_metrics.json")

def count_tokens(text: str) -> int:
    """
    Estimates the number of tokens in the given text using a purely offline estimator.
    This prevents any network requests from tiktoken that would be blocked by the sandbox.
    For Python code, character count divided by 3.8 provides a very close approximation
    to the cl100k_base tokenizer.
    """
    if not text:
        return 0
    return max(1, int(len(text) / 3.8))

def log_token_metrics(tool_name: str):
    """Decorator to log inputs/outputs token metrics to .mcp_token_metrics.json."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Track input arguments
            input_data = {
                "args": args,
                "kwargs": kwargs
            }
            try:
                input_str = json.dumps(input_data)
            except Exception:
                input_str = str(input_data)
            
            input_tokens = count_tokens(input_str)
            
            start_time = time.time()
            try:
                result = func(*args, **kwargs)
                error_occurred = False
                error_msg = None
            except Exception as e:
                result = None
                error_occurred = True
                error_msg = str(e)
                raise e
            finally:
                duration = time.time() - start_time
                
                # Track output response
                if error_occurred:
                    output_str = f"Error: {error_msg}"
                else:
                    try:
                        output_str = str(result)
                    except Exception:
                        output_str = ""
                
                output_tokens = count_tokens(output_str)
                
                # Write to .mcp_token_metrics.json
                try:
                    metrics = {"total_input_tokens": 0, "total_output_tokens": 0, "calls": []}
                    if os.path.exists(METRICS_FILE):
                        try:
                            with open(METRICS_FILE, "r") as f:
                                metrics = json.load(f)
                        except Exception:
                            pass # Re-initialize if corrupted
                    
                    metrics["total_input_tokens"] = metrics.get("total_input_tokens", 0) + input_tokens
                    metrics["total_output_tokens"] = metrics.get("total_output_tokens", 0) + output_tokens
                    
                    metrics.setdefault("calls", []).append({
                        "timestamp": datetime.now().isoformat(),
                        "tool": tool_name,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "duration_seconds": round(duration, 3)
                    })
                    
                    with open(METRICS_FILE, "w") as f:
                        json.dump(metrics, f, indent=2)
                except Exception as ex:
                    print(f"Error logging token metrics for tool '{tool_name}': {ex}", file=sys.stderr)
                    
            return result
        return wrapper
    return decorator
